#!/usr/bin/env bash
# Remove everything deploy/gcp/setup.sh created, so nothing keeps billing.
#
#   bash deploy/gcp/teardown.sh                 # asks before deleting
#   bash deploy/gcp/teardown.sh --keep-disk     # delete the VM but keep its disk (all data)
#   bash deploy/gcp/teardown.sh --delete-project
#                                               # shut down the whole project instead
#
# WARNING: without --keep-disk this deletes every video, transcript, score and
# account stored on the marker. There is no undo.
#
# Options:
#   --project ID       the project (default: the gcloud default project)
#   --keep-disk        keep the VM's disk (about US$0.10 per GB per month)
#   --delete-project   shut down the whole Google Cloud project (30 days to undo)
#   --yes              do not ask (still requires --project)

set -Euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=setup/common.sh source-path=SCRIPTDIR
source "$HERE/setup/common.sh"

ARG_PROJECT="" KEEP_DISK=0 DELETE_PROJECT=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --project) ARG_PROJECT="$2"; shift 2 ;;
    --keep-disk) KEEP_DISK=1; shift ;;
    --delete-project) DELETE_PROJECT=1; shift ;;
    --yes|-y) export OSCE_ASSUME_YES=1; shift ;;
    -h|--help) sed -n '2,17p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "Unknown option $1 (see --help)" ;;
  esac
done

require_gcloud
PROJECT="${ARG_PROJECT:-$(gcloud config get-value project 2>/dev/null || true)}"
if [[ -z "$ARG_PROJECT" ]]; then
  ask PROJECT "Project to remove the marker from" "$PROJECT"
fi
[[ -n "$PROJECT" ]] || die "No project given."
if [[ "${OSCE_ASSUME_YES:-0}" == 1 && -z "$ARG_PROJECT" ]]; then
  die "--yes needs --project, so nothing is deleted by accident."
fi
gcloud projects describe "$PROJECT" >/dev/null 2>&1 || die "Project $PROJECT not found (or you cannot see it)."

config_load || warn "No saved settings found; looking for resources by their default names."
INSTANCE="${INSTANCE:-$OSCE_INSTANCE_DEFAULT}"

if [[ -z "${REGION:-}" || -z "${ZONE:-}" ]]; then
  found="$(gcloud compute instances list --project "$PROJECT" --filter="name=$INSTANCE" \
    --format='value(zone.basename())' 2>/dev/null | head -n1)"
  if [[ -n "$found" ]]; then ZONE="$found"; REGION="${found%-*}"; fi
fi

if [[ "$DELETE_PROJECT" == 1 ]]; then
  printf '\n%sThis shuts down the whole project %s%s and everything in it,\n' "$_R$_B" "$PROJECT" "$_N"
  printf 'not only the marker. Google keeps it recoverable for 30 days.\n'
else
  printf '\n%sThis deletes the OSCE marker from project %s:%s\n' "$_R$_B" "$PROJECT" "$_N"
  printf '  the VM %s%s, the start page, the static IP, the firewall rule,\n' "$INSTANCE" "${ZONE:+ ($ZONE)}"
  printf '  the service accounts, the secrets and the two storage buckets.\n'
  if [[ "$KEEP_DISK" == 1 ]]; then
    printf '  The VM disk (all marker data) is %sKEPT%s.\n' "$_G" "$_N"
  else
    printf '  %sThe VM disk and every video, score and account on it are deleted.%s\n' "$_R" "$_N"
  fi
fi

if [[ "${OSCE_ASSUME_YES:-0}" != 1 ]]; then
  read -r -p "    Type the project ID ($PROJECT) to confirm: " reply || true
  [[ "$reply" == "$PROJECT" ]] || die "Not confirmed; nothing was deleted."
fi

if [[ "$DELETE_PROJECT" == 1 ]]; then
  gcloud projects delete "$PROJECT" --quiet
  ok "Project $PROJECT is shut down (restore within 30 days: gcloud projects undelete $PROJECT)"
  exit 0
fi

# Each step tolerates "already gone", so a half-finished teardown can be run again.
try() {
  local what="$1"; shift
  local out
  if out="$("$@" 2>&1)"; then
    ok "$what"
  elif grep -qiE 'not found|was not found|does not exist|NOT_FOUND' <<<"$out"; then
    ok "$what (already gone)"
  else
    warn "$what failed: $(head -n3 <<<"$out")"
    FAILED=1
  fi
}
FAILED=0

step "Removing the start page"
if [[ -n "${REGION:-}" ]]; then
  try "Deleted Cloud Run service $OSCE_GATE_SERVICE" gc run services delete "$OSCE_GATE_SERVICE" --region "$REGION"
fi

step "Removing the VM"
if [[ -n "${ZONE:-}" ]]; then
  if [[ "$KEEP_DISK" == 1 ]]; then
    try "Deleted VM $INSTANCE (disk kept as '$INSTANCE')" gc compute instances delete "$INSTANCE" --zone "$ZONE" --keep-disks boot
  else
    try "Deleted VM $INSTANCE and its disk" gc compute instances delete "$INSTANCE" --zone "$ZONE" --delete-disks all
  fi
else
  warn "No VM named $INSTANCE found."
fi

step "Removing the network pieces"
if [[ -n "${REGION:-}" ]]; then
  try "Released static IP" gc compute addresses delete "$OSCE_ADDRESS" --region "$REGION"
fi
try "Deleted firewall rule" gc compute firewall-rules delete "$OSCE_FIREWALL_WEB"

step "Removing secrets"
for s in "$OSCE_SECRET_ADMIN" "$OSCE_SECRET_GATE" "$OSCE_SECRET_HF"; do
  try "Deleted secret $s" gc secrets delete "$s"
done

step "Removing service accounts and roles"
try "Removed the gate's project permission" gcloud projects remove-iam-policy-binding "$PROJECT" \
  --member "serviceAccount:$(sa_email "$OSCE_GATE_SA")" --role "projects/$PROJECT/roles/$OSCE_GATE_OPS_ROLE" --condition=None --quiet
try "Deleted service account $OSCE_GATE_SA" gc iam service-accounts delete "$(sa_email "$OSCE_GATE_SA")"
try "Deleted service account $OSCE_VM_SA" gc iam service-accounts delete "$(sa_email "$OSCE_VM_SA")"
try "Deleted role $OSCE_GATE_ROLE" gc iam roles delete "$OSCE_GATE_ROLE"
try "Deleted role $OSCE_GATE_OPS_ROLE" gc iam roles delete "$OSCE_GATE_OPS_ROLE"

step "Removing storage buckets"
try "Deleted gs://$(history_bucket)" gcloud storage rm --recursive --quiet "gs://$(history_bucket)"
try "Deleted gs://$(deploy_bucket)" gcloud storage rm --recursive --quiet "gs://$(deploy_bucket)"
local_cfg="$(config_local_path)"
if [[ -f "$local_cfg" ]]; then
  : >"$local_cfg"
  ok "Cleared saved settings on this computer"
fi

cat <<EOF

Left in place (they cost nothing, or almost nothing):
  - The Google sign-in client (APIs & Services -> Credentials); delete it there if you like.
  - Built start-page images in Artifact Registry repository 'cloud-run-source-deploy'
    (a few hundred MB, cents a month): https://console.cloud.google.com/artifacts?project=$PROJECT
  - The enabled APIs and billing link.
EOF
if [[ "$KEEP_DISK" == 1 ]]; then
  echo "  - The disk '$INSTANCE' in ${ZONE:-its zone}, with all the marker's data."
fi

if [[ "$FAILED" == 1 ]]; then
  die "Some steps failed (see the ! lines above). Run the same command again to retry them."
fi
ok "Done."
