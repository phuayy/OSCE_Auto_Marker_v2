#!/usr/bin/env bash
# One-command setup of the on-demand OSCE marker on Google Cloud.
#
#   bash deploy/gcp/setup.sh
#
# Builds everything deploy/gcp/README.md describes, in a Google Cloud project
# of your choice:
#
#   * a GPU VM (L4, with a T4 fallback where the zone and quota allow) that
#     installs the app on first boot, serves it over HTTPS with its own
#     certificate, and switches itself off when idle;
#   * a static IP and an HTTPS name for it (your own domain, or a free
#     <ip>.sslip.io name that needs no DNS);
#   * the start gate on Cloud Run, with a service account that can only start
#     that one VM;
#   * the secrets (first admin password, gate cookie key, HuggingFace token)
#     in Secret Manager.
#
# Safe to run again at any time, from any computer: it finds what exists,
# creates what is missing, and ships the current code to the VM (applied on
# its next start). Settings are kept in gs://<project>-osce-deploy/config.env.
#
# Options:
#   --project ID     Google Cloud project to use (asked otherwise)
#   --region NAME    Region, e.g. asia-southeast1 (asked otherwise)
#   --domain NAME    Your own domain for the app (default: <ip>.sslip.io)
#   --yes            Accept every default; no questions except the ones only
#                    a person can answer (the Google sign-in client ID)
#   --update         Only ship the current code to an existing deployment
#   -h, --help

set -Eeuo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/../.." && pwd)"
# shellcheck source=setup/common.sh source-path=SCRIPTDIR
source "$HERE/setup/common.sh"

trap 'die "an unexpected error on line $LINENO. Fix what the message above says and run the same command again: finished steps are skipped."' ERR

ARG_PROJECT="" ARG_REGION="" ARG_DOMAIN="" UPDATE_ONLY=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --project) ARG_PROJECT="$2"; shift 2 ;;
    --region)  ARG_REGION="$2"; shift 2 ;;
    --domain)  ARG_DOMAIN="$2"; shift 2 ;;
    --yes|-y)  export OSCE_ASSUME_YES=1; shift ;;
    --update)  UPDATE_ONLY=1; shift ;;
    -h|--help) sed -n '2,30p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "Unknown option $1 (see --help)" ;;
  esac
done

REQUIRED_APIS=(
  compute.googleapis.com
  run.googleapis.com
  cloudbuild.googleapis.com
  artifactregistry.googleapis.com
  secretmanager.googleapis.com
  iam.googleapis.com
  storage.googleapis.com
  cloudresourcemanager.googleapis.com
  cloudbilling.googleapis.com
)

# ===========================================================================
# 1. Account and project
# ===========================================================================
choose_project() {
  step "Google Cloud project"
  PROJECT="${ARG_PROJECT:-}"
  if [[ -z "$PROJECT" ]]; then
    local current; current="$(gcloud config get-value project 2>/dev/null || true)"
    info "Projects you can use:"
    gcloud projects list --format='table[no-heading](projectId,name)' --limit 20 2>/dev/null | sed 's/^/      /' || true
    info "Type an existing project ID, or a new ID (6-30 lowercase letters, digits, hyphens) to create one."
    ask PROJECT "Project ID" "$current"
  fi
  [[ -n "$PROJECT" ]] || die "No project chosen."

  if ! gcloud projects describe "$PROJECT" >/dev/null 2>&1; then
    [[ "$PROJECT" =~ ^[a-z][a-z0-9-]{4,28}[a-z0-9]$ ]] || die "'$PROJECT' is not a valid project ID."
    confirm "Project '$PROJECT' does not exist. Create it?" || die "No project to use."
    gcloud projects create "$PROJECT" --name "OSCE Marker" ||
      die "Could not create '$PROJECT' (IDs are global: try another)."
    ok "Created project $PROJECT"
  fi
  gcloud config set project "$PROJECT" >/dev/null 2>&1
  ok "Using project $PROJECT (signed in as $ACTIVE_ACCOUNT)"
}

ensure_billing() {
  step "Billing"
  gcloud services enable cloudbilling.googleapis.com --project "$PROJECT" >/dev/null 2>&1 || true
  local enabled
  enabled="$(gcloud billing projects describe "$PROJECT" --format='value(billingEnabled)' 2>/dev/null || echo "")"
  if [[ "$enabled" == "True" || "$enabled" == "true" ]]; then
    ok "Billing is enabled"
    return
  fi

  local accounts count account
  accounts="$(gcloud billing accounts list --filter=open=true --format='value(name.basename(),displayName)' 2>/dev/null || true)"
  count="$(grep -c . <<<"$accounts" || true)"
  if [[ "$count" -eq 0 ]]; then
    manual "Turn on billing" \
      "This project has no billing account, and you have none to link." \
      "1. Open https://console.cloud.google.com/billing/create" \
      "2. Create a billing account (a card is needed). A free-trial account cannot" \
      "   use GPUs: press 'Activate full account' / 'Upgrade' when offered." \
      "3. Run this same command again."
    exit 1
  fi
  if [[ "$count" -eq 1 ]]; then
    account="$(cut -f1 <<<"$accounts")"
  else
    info "Billing accounts you can use:"
    printf "      %s\n" "${accounts//$'\n'/$'\n      '}"
    ask account "Billing account ID to link" "$(head -n1 <<<"$accounts" | cut -f1)"
  fi
  confirm "Link billing account $account to $PROJECT?" || die "Billing is required for a GPU VM."
  gcloud billing projects link "$PROJECT" --billing-account "$account" >/dev/null ||
    die "Could not link billing. You need the 'Billing Account User' role on that account."
  ok "Billing linked ($account)"
}

enable_apis() {
  step "Turning on the Google Cloud services this needs (first time: 1-3 minutes)"
  gcloud services enable "${REQUIRED_APIS[@]}" --project "$PROJECT" >/dev/null
  ok "Services enabled"
}

# ===========================================================================
# 2. Settings: region, zone, GPU, name, who may start it
# ===========================================================================
# GPUs this deployment's own VM holds against the quotas right now: a running
# (or starting, or still stopping) VM is counted in the quota usage, but it is
# the one that will use that GPU again, so a re-run must not see it as taken.
# own_gpus_held [ACCEL] — those of type ACCEL (e.g. nvidia-l4), or all of them.
own_gpus_held() {
  local status
  status="$(gc compute instances describe "$INSTANCE" --zone "${ZONE:-}" --format='value(status)' 2>/dev/null || true)"
  case "$status" in
    RUNNING|STAGING|PROVISIONING|STOPPING) ;;
    *) printf '0'; return ;;
  esac
  gc compute instances describe "$INSTANCE" --zone "$ZONE" --flatten=guestAccelerators \
    --format='value(guestAccelerators.acceleratorType.basename(),guestAccelerators.acceleratorCount)' 2>/dev/null |
    awk -v want="${1:-}" '(want == "" || $1 == want) {n += $2} END {printf "%d", n}'
}

gpu_quota() {  # gpu_quota METRIC ACCEL → limit minus usage plus our own VM's share, region-level
  local free
  free="$(gc compute regions describe "$REGION" --flatten=quotas \
    --filter="quotas.metric=$1" --format='value(quotas.limit,quotas.usage)' 2>/dev/null |
    awk '{printf "%d\n", $1 - $2}' | head -n1)"
  [[ -n "$free" ]] || return 0   # metric absent: the caller reads that as none
  printf '%d\n' $(( free + $(own_gpus_held "$2") ))
}

global_gpu_quota() {
  local v
  v="$(gc compute project-info describe --flatten=quotas \
    --filter='quotas.metric=GPUS_ALL_REGIONS' --format='value(quotas.limit,quotas.usage)' 2>/dev/null |
    awk '{printf "%d\n", $1 - $2}' | head -n1)"
  if [[ -z "$v" ]]; then printf '1'; return; fi   # metric absent: no all-regions cap on this project
  printf '%d' $(( v + $(own_gpus_held) ))
}

zones_with() {  # zones_with ACCEL → zones in $REGION offering it
  gc compute accelerator-types list --filter="name=$1 AND zone~^$REGION-" \
    --format='value(zone.basename())' 2>/dev/null | sort -u
}

choose_region_and_gpu() {
  step "Region and GPU"
  if [[ -n "${ARG_REGION:-}" && "$ARG_REGION" != "${REGION:-}" ]]; then
    REGION="$ARG_REGION"
    ZONE=""   # a zone belongs to one region
  fi
  if [[ -z "${REGION:-}" ]]; then
    info "Pick a region near your users with L4 GPUs, e.g. asia-southeast1 (Singapore),"
    info "europe-west4 (Netherlands), us-central1 (Iowa), us-east4 (Virginia)."
    ask REGION "Region" "asia-southeast1"
  fi
  gc compute regions describe "$REGION" >/dev/null 2>&1 || die "'$REGION' is not a Compute Engine region."

  if [[ -n "${ZONE:-}" ]] && gc compute instances describe "$INSTANCE" --zone "$ZONE" >/dev/null 2>&1; then
    ok "The VM already exists in $ZONE; keeping it there"
  else
    local l4_zones t4_zones both=""
    l4_zones="$(zones_with nvidia-l4)"
    t4_zones="$(zones_with nvidia-tesla-t4)"
    [[ -n "$l4_zones$t4_zones" ]] || die "No L4 or T4 GPUs exist in $REGION. Run again and pick another region."
    both="$(comm -12 <(printf '%s\n' "$l4_zones") <(printf '%s\n' "$t4_zones") | head -n1)"
    ZONE="${ZONE:-}"
    if [[ -z "$ZONE" ]]; then
      ZONE="${both:-$(head -n1 <<<"${l4_zones:-$t4_zones}")}"
    fi
    ok "Zone $ZONE (L4 zones: $(echo "$l4_zones" | xargs); T4 zones: $(echo "$t4_zones" | xargs))"
  fi

  local l4 t4 all l4_here t4_here
  l4="$(gpu_quota NVIDIA_L4_GPUS nvidia-l4)"; l4="${l4:-0}"
  t4="$(gpu_quota NVIDIA_T4_GPUS nvidia-tesla-t4)"; t4="${t4:-0}"
  all="$(global_gpu_quota)"
  l4_here="$(zones_with nvidia-l4 | grep -cx "$ZONE" || true)"
  t4_here="$(zones_with nvidia-tesla-t4 | grep -cx "$ZONE" || true)"
  info "GPU quota free in $REGION: L4 $l4, T4 $t4; all regions: $all"

  local profiles=()
  if [[ "$l4_here" -gt 0 && "$l4" -gt 0 ]]; then profiles+=("$OSCE_L4_PROFILE"); fi
  if [[ "$t4_here" -gt 0 && "$t4" -gt 0 ]]; then profiles+=("$OSCE_T4_PROFILE"); fi

  if [[ "$all" -lt 1 || ${#profiles[@]} -eq 0 ]]; then
    # Only a brand-new VM needs free quota (an existing one's own share is
    # already counted as free above).
    if gc compute instances describe "$INSTANCE" --zone "$ZONE" >/dev/null 2>&1; then
      warn "No free GPU quota right now (the existing VM may be using it). Keeping the current GPU settings."
      return
    fi
    manual "Ask Google for GPU quota (usually approved within minutes to a day)" \
      "New projects start with 0 GPUs. Request at least 1 of each below:" \
      "1. Open https://console.cloud.google.com/iam-admin/quotas?project=$PROJECT" \
      "2. In the filter box search for each name, tick it, press 'Edit quotas', ask for 1:" \
      "     - GPUs (all regions)                      (metric GPUS_ALL_REGIONS)" \
      "     - NVIDIA L4 GPUs,  region $REGION        (metric NVIDIA_L4_GPUS)" \
      "     - NVIDIA T4 GPUs,  region $REGION        (optional fallback)" \
      "   Reason: 'One GPU VM for an exam video-marking application.'" \
      "3. Wait for the approval email, then run this same command again." \
      "A free-trial billing account cannot get GPU quota: upgrade it first."
    config_save
    exit 1
  fi

  MACHINE_PROFILES="$(IFS=,; echo "${profiles[*]}")"
  MACHINE_PROFILES="${MACHINE_PROFILES//,/, }"
  ok "GPU machines, in order of preference: $MACHINE_PROFILES"
  if [[ ${#profiles[@]} -eq 1 ]]; then
    info "(No fallback GPU: if the zone runs out, the gate says so and offers Try again."
    info " Request T4 quota in $REGION and run setup again to add one.)"
  fi
}

choose_settings() {
  step "Settings"
  INSTANCE="${INSTANCE:-$OSCE_INSTANCE_DEFAULT}"
  DATABASE="${DATABASE:-postgres}"
  IDLE_MINUTES="${IDLE_MINUTES:-30}"
  DISK_SIZE_GB="${DISK_SIZE_GB:-200}"
  if [[ -n "$ARG_DOMAIN" ]]; then DOMAIN="$ARG_DOMAIN"; fi

  if [[ -z "${ALLOWED_EMAILS:-}${ALLOWED_DOMAINS:-}" ]]; then
    info "Who may switch the marker on from the start page (Google accounts)."
    info "Separate several with commas. You can change this later by running setup again."
    ask ALLOWED_EMAILS "Allowed Google emails" "$ACTIVE_ACCOUNT"
    ask ALLOWED_DOMAINS "Allowed whole email domains, e.g. school.edu (blank for none)" ""
  fi
  ALLOWED_EMAILS="${ALLOWED_EMAILS// /}"
  ALLOWED_DOMAINS="${ALLOWED_DOMAINS// /}"
  if [[ -z "${ADMIN_EMAIL:-}" ]]; then
    ADMIN_EMAIL="$ACTIVE_ACCOUNT"
  fi
  ok "Start page allowlist: emails [${ALLOWED_EMAILS:-none}] domains [${ALLOWED_DOMAINS:-none}]"
  ok "VM $INSTANCE, ${DISK_SIZE_GB} GB disk, database $DATABASE, switches off after ${IDLE_MINUTES} idle minutes"
}

# ===========================================================================
# 3. Storage, secrets, identities
# ===========================================================================
ensure_buckets() {
  step "Storage buckets"
  bucket_ensure "$(deploy_bucket)"
  bucket_ensure "$(history_bucket)"
  config_save
}

ensure_secrets() {
  step "Secrets (Secret Manager)"
  secret_ensure "$OSCE_SECRET_ADMIN" "$(random_string 20)"
  secret_ensure "$OSCE_SECRET_GATE" "$(random_string 48)"

  if ! secret_exists "$OSCE_SECRET_HF"; then
    manual "Optional: a HuggingFace token for speaker labels" \
      "Without it transcripts still work, but have no Student/Examiner labels." \
      "1. Sign in at https://huggingface.co (free account)." \
      "2. Open https://huggingface.co/pyannote/speaker-diarization-community-1 and accept the terms." \
      "3. Create a 'Read' token at https://huggingface.co/settings/tokens and paste it below." \
      "Leave blank to skip; run setup again later to add it."
    local token=""
    if [[ "${OSCE_ASSUME_YES:-0}" != 1 && -t 0 ]]; then
      read -r -s -p "    HuggingFace token (hidden, blank to skip): " token || true
      echo
    fi
    if [[ -n "$token" ]]; then
      secret_set "$OSCE_SECRET_HF" "$token"
      ok "Saved the HuggingFace token"
    else
      info "Skipped the HuggingFace token"
    fi
  else
    ok "Secret $OSCE_SECRET_HF exists (kept)"
  fi
}

ensure_identities() {
  step "Service accounts and permissions"
  sa_ensure "$OSCE_VM_SA" "OSCE marker VM"
  sa_ensure "$OSCE_GATE_SA" "OSCE marker start gate"
  local vm_sa gate_sa
  vm_sa="$(sa_email "$OSCE_VM_SA")"
  gate_sa="$(sa_email "$OSCE_GATE_SA")"

  # The VM reads its code bundle and its own secrets; nothing else.
  retry 5 gcloud storage buckets add-iam-policy-binding "gs://$(deploy_bucket)" \
    --member "serviceAccount:$vm_sa" --role roles/storage.objectViewer
  local s
  for s in "$OSCE_SECRET_ADMIN" "$OSCE_SECRET_HF"; do
    if secret_exists "$s"; then
      retry 5 gc secrets add-iam-policy-binding "$s" \
        --member "serviceAccount:$vm_sa" --role roles/secretmanager.secretAccessor
    fi
  done
  ok "VM account can read its code and secrets"

  # The gate: start / resize this one VM (bound on the instance, below),
  # read the zone operation it started, keep its history, read its cookie key.
  role_ensure "$OSCE_GATE_ROLE" "OSCE gate: start the marker VM" \
    "compute.instances.get,compute.instances.start,compute.instances.getGuestAttributes,compute.instances.setMachineType,compute.instances.setMachineResources"
  role_ensure "$OSCE_GATE_OPS_ROLE" "OSCE gate: read start operations" \
    "compute.zoneOperations.get"
  retry 5 gcloud projects add-iam-policy-binding "$PROJECT" \
    --member "serviceAccount:$gate_sa" --role "projects/$PROJECT/roles/$OSCE_GATE_OPS_ROLE" --condition=None
  retry 5 gcloud storage buckets add-iam-policy-binding "gs://$(history_bucket)" \
    --member "serviceAccount:$gate_sa" --role roles/storage.objectAdmin
  retry 5 gc secrets add-iam-policy-binding "$OSCE_SECRET_GATE" \
    --member "serviceAccount:$gate_sa" --role roles/secretmanager.secretAccessor
  ok "Gate account can start only the marker VM"

  # `gcloud run deploy --source` builds with the Compute default account on
  # new projects; it needs the builder role there.
  local number; number="$(project_number)"
  retry 3 gcloud projects add-iam-policy-binding "$PROJECT" \
    --member "serviceAccount:$number-compute@developer.gserviceaccount.com" \
    --role roles/run.builder --condition=None || warn "Could not grant the build role; the gate deploy may need it."
}

# ===========================================================================
# 4. Network: static IP, firewall
# ===========================================================================
ensure_network() {
  step "Network"
  if ! gc compute networks describe default >/dev/null 2>&1; then
    gc compute networks create default --subnet-mode auto >/dev/null
    ok "Created the default network"
  fi
  if ! gc compute addresses describe "$OSCE_ADDRESS" --region "$REGION" >/dev/null 2>&1; then
    gc compute addresses create "$OSCE_ADDRESS" --region "$REGION" >/dev/null
    ok "Reserved a static IP"
  fi
  STATIC_IP="$(gc compute addresses describe "$OSCE_ADDRESS" --region "$REGION" --format='value(address)')"
  ok "Static IP $STATIC_IP"

  if ! gc compute firewall-rules describe "$OSCE_FIREWALL_WEB" >/dev/null 2>&1; then
    gc compute firewall-rules create "$OSCE_FIREWALL_WEB" --network default \
      --direction INGRESS --allow tcp:80,tcp:443 --target-tags "$OSCE_NETWORK_TAG" \
      --source-ranges 0.0.0.0/0 --description "HTTPS to the OSCE marker (80 is for the certificate check)" >/dev/null
    ok "Opened ports 80/443 to the VM"
  else
    ok "Firewall rule exists"
  fi

  if [[ -n "${DOMAIN:-}" ]]; then
    local resolved
    resolved="$(getent hosts "$DOMAIN" 2>/dev/null | awk '{print $1}' | head -n1 || true)"
    if [[ "$resolved" != "$STATIC_IP" ]]; then
      manual "Point your domain at the marker" \
        "Create a DNS 'A' record:  $DOMAIN  ->  $STATIC_IP" \
        "(at whoever manages that domain). The VM gets its HTTPS certificate once" \
        "this resolves; until then the app is unreachable. Currently: ${resolved:-not resolving}."
    else
      ok "$DOMAIN points at $STATIC_IP"
    fi
  fi
  ok "The marker's address will be https://$(app_domain)"
}

# ===========================================================================
# 5. Code bundle and the VM
# ===========================================================================
upload_bundle() {
  step "Packaging the app"
  local tmp version
  tmp="$(mktemp -d)"
  if git -C "$REPO_ROOT" rev-parse --git-dir >/dev/null 2>&1; then
    version="$(git -C "$REPO_ROOT" rev-parse --short HEAD)"
    if [[ -n "$(git -C "$REPO_ROOT" status --porcelain --untracked-files=no 2>/dev/null)" ]]; then
      warn "You have uncommitted changes; only committed code is shipped ($version)."
    fi
    git -C "$REPO_ROOT" archive --format=tar.gz -o "$tmp/bundle.tar.gz" HEAD
  else
    version="snapshot"
    tar -czf "$tmp/bundle.tar.gz" -C "$REPO_ROOT" \
      --exclude=./.git --exclude=./node_modules --exclude=./.venv --exclude=./storage --exclude=./dist --exclude=./.env .
  fi
  BUNDLE_URI="gs://$(deploy_bucket)/app/osce-marker-$(date -u +%Y%m%dT%H%M%SZ)-$version.tar.gz"
  gcloud storage cp --quiet "$tmp/bundle.tar.gz" "$BUNDLE_URI" >/dev/null
  rm -rf "$tmp"
  ok "Uploaded $version to $BUNDLE_URI"
}

choose_image() {
  if [[ -n "${IMAGE_FAMILY:-}" ]] && gcloud compute images describe-from-family "$IMAGE_FAMILY" \
      --project "${IMAGE_PROJECT:-deeplearning-platform-release}" >/dev/null 2>&1; then
    return
  fi
  IMAGE_PROJECT="deeplearning-platform-release"
  # Google's Deep Learning VM images: Ubuntu with the NVIDIA driver
  # preinstalled. Pick the newest CUDA 12.8+ family the app's wheels support.
  IMAGE_FAMILY="$(gcloud compute images list --project "$IMAGE_PROJECT" --no-standard-images \
      --filter='family~^common-cu12[89].*ubuntu-2[24]04.*nvidia AND -family~arm' \
      --format='value(family)' 2>/dev/null | sort -uV | tail -n1)"
  if [[ -z "$IMAGE_FAMILY" ]]; then
    IMAGE_FAMILY="$(gcloud compute images list --project "$IMAGE_PROJECT" --no-standard-images \
        --filter='family~^common-cu12.*ubuntu AND -family~arm' --format='value(family)' 2>/dev/null | sort -uV | tail -n1)"
  fi
  [[ -n "$IMAGE_FAMILY" ]] || die "Could not find a Deep Learning VM image. Set IMAGE_FAMILY in $(config_local_path) and run again."
  ok "Image family $IMAGE_PROJECT/$IMAGE_FAMILY"
}

vm_metadata() {
  local hf=""
  if secret_exists "$OSCE_SECRET_HF"; then hf="$OSCE_SECRET_HF"; fi
  printf '%s' "enable-guest-attributes=TRUE,install-nvidia-driver=True,osce-bundle=$BUNDLE_URI,osce-domain=$(app_domain),osce-database=$DATABASE,osce-idle-minutes=$IDLE_MINUTES,osce-admin-email=$ADMIN_EMAIL,osce-admin-secret=$OSCE_SECRET_ADMIN,osce-hf-secret=$hf"
}

create_vm() {
  local profile machine accel args
  local IFS_SAVE="$IFS"
  IFS=',' read -r -a candidates <<<"${MACHINE_PROFILES// /}"
  IFS="$IFS_SAVE"
  for profile in "${candidates[@]}"; do
    machine="${profile%%+*}"
    accel=""
    if [[ "$profile" == *+* ]]; then accel="${profile#*+}"; fi
    args=(compute instances create "$INSTANCE" --zone "$ZONE"
      --machine-type "$machine"
      --maintenance-policy TERMINATE --restart-on-failure
      --image-project "$IMAGE_PROJECT" --image-family "$IMAGE_FAMILY"
      --boot-disk-size "${DISK_SIZE_GB}GB" --boot-disk-type pd-balanced
      --address "$STATIC_IP" --tags "$OSCE_NETWORK_TAG"
      --service-account "$(sa_email "$OSCE_VM_SA")" --scopes cloud-platform
      --metadata "$(vm_metadata)"
      --metadata-from-file "startup-script=$HERE/setup/vm-startup.sh"
      --labels app=osce-marker)
    if [[ -n "$accel" ]]; then args+=(--accelerator "type=$accel,count=1"); fi
    info "Creating the VM on $profile ..."
    local out
    if out="$(gc "${args[@]}" 2>&1)"; then
      ok "VM $INSTANCE created on $profile"
      return 0
    fi
    if grep -qE 'ZONE_RESOURCE_POOL_EXHAUSTED|does not have enough resources|QUOTA_EXCEEDED|Quota .* exceeded' <<<"$out"; then
      warn "No $profile available right now: $(grep -m1 -oE 'ZONE_RESOURCE_POOL_EXHAUSTED|QUOTA_EXCEEDED|does not have enough resources[^.]*' <<<"$out")"
      continue
    fi
    printf '%s\n' "$out" >&2
    die "Could not create the VM (message above)."
  done
  die "None of the GPUs ($MACHINE_PROFILES) is free in $ZONE right now. This is temporary on Google's side: wait 15-60 minutes and run the same command again."
}

ensure_vm() {
  step "The marker VM"
  choose_image
  if gc compute instances describe "$INSTANCE" --zone "$ZONE" >/dev/null 2>&1; then
    gc compute instances add-metadata "$INSTANCE" --zone "$ZONE" \
      --metadata "$(vm_metadata)" \
      --metadata-from-file "startup-script=$HERE/setup/vm-startup.sh" >/dev/null
    ok "VM exists; its settings and code now point at this version"
    local status; status="$(gc compute instances describe "$INSTANCE" --zone "$ZONE" --format='value(status)')"
    if [[ "$status" == "RUNNING" ]]; then
      if confirm "The marker is running. Restart it now to apply the update? (Anyone using it is interrupted.)"; then
        INSTALL_SINCE="$(date +%s)"
        gc compute instances stop "$INSTANCE" --zone "$ZONE" >/dev/null
        gc compute instances start "$INSTANCE" --zone "$ZONE" >/dev/null
        VM_STARTED=1
        ok "Restarted; installing the update"
      else
        info "The update is applied the next time the marker is started."
      fi
    else
      info "The marker is stopped; the update is applied the next time it starts from the gate."
    fi
  else
    INSTALL_SINCE="$(date +%s)"
    create_vm
    VM_STARTED=1
  fi
  config_save
}

# The VM reports its install to the guest attribute osce/install. A report is
# "ours" when it is newer than INSTALL_SINCE, taken just before the VM is
# started or created: the VM can finish (or fail) long before wait_for_install
# is reached, because the gate is deployed in between, and a reference time
# taken at the wait would call that report an old one.
wait_for_install() {
  [[ "${VM_STARTED:-0}" == 1 ]] || return 0
  step "Installing the app on the VM (first time: 15-30 minutes; updates: a few)"
  info "Safe to close this window: the install continues on its own."
  info "Log on the VM: sudo tail -f /var/log/osce-install.log"
  local deadline=$(( $(date +%s) + 60 * 60 )) value="" last="" since="${INSTALL_SINCE:?ensure_vm marks the trigger time}"
  while (( $(date +%s) < deadline )); do
    value="$(gc compute instances get-guest-attributes "$INSTANCE" --zone "$ZONE" \
      --query-path=osce/install --format='value(value)' 2>/dev/null || true)"
    # Ignore a "done" left over from before this start.
    local ts="${value##* }"
    if [[ -n "$value" && "$ts" =~ ^[0-9]+$ && "$ts" -ge $((since - 30)) ]]; then
      case "$value" in
        done*)   ok "Installed: ${value% *}"; return 0 ;;
        failed*rolled-back*) die "The update failed at step '$(awk '{print $2}' <<<"$value")'; the VM rolled back and the previous version is serving again. See the log: gcloud compute ssh $INSTANCE --zone $ZONE --project $PROJECT -- sudo tail -n 100 /var/log/osce-install.log" ;;
        failed*) die "The install on the VM failed at step '$(awk '{print $2}' <<<"$value")'. See the log: gcloud compute ssh $INSTANCE --zone $ZONE --project $PROJECT -- sudo tail -n 100 /var/log/osce-install.log   — then run setup again." ;;
      esac
      if [[ "$value" != "$last" ]]; then
        info "$(date +%H:%M) $(awk '{print $2}' <<<"$value")"
        last="$value"
      fi
    fi
    sleep 20
  done
  warn "Still installing after an hour. Check: gcloud compute ssh $INSTANCE --zone $ZONE --project $PROJECT -- sudo tail -n 100 /var/log/osce-install.log"
}

# ===========================================================================
# 6. The start gate
# ===========================================================================
ensure_oauth_client() {
  step "Google sign-in for the start page"
  local number
  number="$(project_number)"
  GATE_URL="https://$OSCE_GATE_SERVICE-$number.$REGION.run.app"
  if [[ -n "${OAUTH_CLIENT_ID:-}" ]]; then
    ok "Using sign-in client $OAUTH_CLIENT_ID"
    return
  fi
  manual "Create the Google sign-in client (about 3 minutes, Google offers no command for it)" \
    "1. Open https://console.cloud.google.com/auth/overview?project=$PROJECT and press 'Get started':" \
    "     App name: OSCE Marker   ·   Support email: yours" \
    "     Audience: 'Internal' if everyone uses your organisation's Google accounts," \
    "               otherwise 'External'   ·   Contact email: yours   ·   Agree, Create." \
    "2. If you chose External: open https://console.cloud.google.com/auth/audience?project=$PROJECT" \
    "   and press 'Publish app' (only name/email are requested, so no review is needed)." \
    "3. Open https://console.cloud.google.com/auth/clients/create?project=$PROJECT" \
    "     Application type: Web application   ·   Name: OSCE gate" \
    "     Authorized JavaScript origins -> Add URI:  $GATE_URL" \
    "     Press Create, then copy the Client ID (ends in .apps.googleusercontent.com)."
  while :; do
    if [[ ! -t 0 ]]; then die "Run setup in a terminal to paste the Client ID."; fi
    read -r -p "    Paste the Client ID here: " OAUTH_CLIENT_ID || true
    OAUTH_CLIENT_ID="${OAUTH_CLIENT_ID//[[:space:]]/}"
    [[ "$OAUTH_CLIENT_ID" =~ ^[0-9]+-[a-z0-9]+\.apps\.googleusercontent\.com$ ]] && break
    warn "That does not look like a Client ID (expected 1234-abc....apps.googleusercontent.com)."
  done
  config_save
  ok "Saved the sign-in client"
}

deploy_gate() {
  step "Deploying the start page (Cloud Run; first time: 3-5 minutes)"
  local envfile; envfile="$(mktemp)"
  cat >"$envfile" <<EOF
GATE_PROJECT: "$PROJECT"
GATE_ZONE: "$ZONE"
GATE_INSTANCE: "$INSTANCE"
GATE_APP_URL: "https://$(app_domain)"
GATE_GOOGLE_CLIENT_ID: "$OAUTH_CLIENT_ID"
GATE_ALLOWED_EMAILS: "$ALLOWED_EMAILS"
GATE_ALLOWED_DOMAINS: "$ALLOWED_DOMAINS"
GATE_HISTORY_BUCKET: "$(history_bucket)"
GATE_MACHINE_PROFILES: "$MACHINE_PROFILES"
EOF
  gc run deploy "$OSCE_GATE_SERVICE" --source "$HERE/gate" --region "$REGION" \
    --service-account "$(sa_email "$OSCE_GATE_SA")" \
    --allow-unauthenticated --min-instances 0 --max-instances 2 \
    --env-vars-file "$envfile" \
    --set-secrets "GATE_SESSION_SECRET=$OSCE_SECRET_GATE:latest" >/dev/null
  rm -f "$envfile"

  # Only this VM: the gate's start permission is bound on the instance.
  retry 5 gc compute instances add-iam-policy-binding "$INSTANCE" --zone "$ZONE" \
    --member "serviceAccount:$(sa_email "$OSCE_GATE_SA")" \
    --role "projects/$PROJECT/roles/$OSCE_GATE_ROLE"
  ok "Start page live at $GATE_URL"
}

check_app() {
  [[ "${VM_STARTED:-0}" == 1 ]] || return 0
  step "Checking the marker answers over HTTPS"
  for _ in $(seq 1 30); do
    if curl -fsS --max-time 8 "https://$(app_domain)/api/health/ready" 2>/dev/null | grep -q '"ready": *true'; then
      ok "https://$(app_domain) is up"
      return 0
    fi
    sleep 10
  done
  warn "https://$(app_domain) did not answer yet. If you use your own domain, check its DNS record points at $STATIC_IP."
}

summary() {
  local password
  password="$(secret_read "$OSCE_SECRET_ADMIN" || echo '(run: gcloud secrets versions access latest --secret '"$OSCE_SECRET_ADMIN"')')"
  cat <<EOF

${_B}${_G}All set.${_N}

  Start page (bookmark this, share it):   ${_B}$GATE_URL${_N}
  The marker itself:                      https://$(app_domain)
  First sign-in to the marker:            user ${_B}admin${_N}, password ${_B}$password${_N}
      (also any time: gcloud secrets versions access latest --secret $OSCE_SECRET_ADMIN --project $PROJECT)

  Next, in the marker (as admin):
    1. Settings -> Provider API keys: add the key for your scoring model (e.g. NVIDIA).
    2. Settings -> Users: invite the markers. Without an email server, each invite
       shows a link to copy and send yourself.
    3. Change the admin password from the Account page.

  The machine switches itself off after ${IDLE_MINUTES} idle minutes; people switch it on
  from the start page. While it is off you pay only for its disk and its fixed
  address (roughly US\$25-30 a month together; see deploy/gcp/QUICKSTART.md).

  Update to newer code later:   git pull && bash deploy/gcp/setup.sh --update
  Remove everything:            bash deploy/gcp/teardown.sh

EOF
}

# ===========================================================================
main() {
  require_gcloud
  choose_project
  ensure_billing
  enable_apis
  config_load || true
  # Arguments win over saved settings.
  PROJECT="${ARG_PROJECT:-$PROJECT}"
  INSTANCE="${INSTANCE:-$OSCE_INSTANCE_DEFAULT}"

  if [[ "$UPDATE_ONLY" == 1 ]]; then
    [[ -n "${ZONE:-}" ]] || die "No earlier setup found in $PROJECT; run without --update first."
    STATIC_IP="$(gc compute addresses describe "$OSCE_ADDRESS" --region "$REGION" --format='value(address)')"
    upload_bundle
    ensure_vm
    wait_for_install
    GATE_URL="https://$OSCE_GATE_SERVICE-$(project_number).$REGION.run.app"
    if confirm "Redeploy the start page as well (picks up gate code changes)?"; then deploy_gate; fi
    check_app
    ok "Updated."
    return
  fi

  choose_region_and_gpu
  choose_settings
  config_save
  ensure_buckets
  ensure_secrets
  ensure_identities
  ensure_network
  upload_bundle
  ensure_vm
  ensure_oauth_client      # done while the VM installs
  deploy_gate
  wait_for_install
  check_app
  config_save
  summary
}

if [[ "${OSCE_SETUP_SOURCE_ONLY:-0}" != 1 ]]; then main "$@"; fi
