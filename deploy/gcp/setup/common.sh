# shellcheck shell=bash disable=SC2034  # constants are read by the scripts that source this
# Shared helpers for deploy/gcp/setup.sh and deploy/gcp/teardown.sh.
# Sourced, never run. Plain bash + gcloud: everything here is already in
# Google Cloud Shell, so nothing needs installing first.

# ---------------------------------------------------------------------------
# Names. Fixed, so a second run (or a run from another computer) finds the
# resources the first one made instead of making new ones.
# ---------------------------------------------------------------------------
OSCE_INSTANCE_DEFAULT="osce-marker"
OSCE_GATE_SERVICE="osce-gate"
OSCE_VM_SA="osce-vm"
OSCE_GATE_SA="osce-gate"
OSCE_GATE_ROLE="osceGate"            # instance-level: get/start/resize this VM
OSCE_GATE_OPS_ROLE="osceGateOps"     # project-level: read the start operation
OSCE_ADDRESS="osce-marker-ip"
OSCE_FIREWALL_WEB="osce-marker-allow-web"
OSCE_NETWORK_TAG="osce-marker"
OSCE_SECRET_ADMIN="osce-admin-password"
OSCE_SECRET_GATE="osce-gate-session"
OSCE_SECRET_HF="osce-hf-token"
OSCE_L4_PROFILE="g2-standard-8"
OSCE_T4_PROFILE="n1-standard-8+nvidia-tesla-t4"

# Every setting a later run needs, and nothing secret. Saved to the deploy
# bucket so a run from a new computer picks up where the last one stopped.
OSCE_CONFIG_KEYS=(
  PROJECT REGION ZONE INSTANCE DOMAIN DATABASE IDLE_MINUTES DISK_SIZE_GB
  ALLOWED_EMAILS ALLOWED_DOMAINS OAUTH_CLIENT_ID MACHINE_PROFILES
  IMAGE_PROJECT IMAGE_FAMILY ADMIN_EMAIL
)

# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------
if [[ -t 1 ]]; then
  _B=$'\e[1m'; _G=$'\e[32m'; _Y=$'\e[33m'; _R=$'\e[31m'; _C=$'\e[36m'; _N=$'\e[0m'
else
  _B=""; _G=""; _Y=""; _R=""; _C=""; _N=""
fi

step()  { printf '\n%s==> %s%s\n' "$_B$_C" "$*" "$_N"; }
info()  { printf '    %s\n' "$*"; }
ok()    { printf '    %s✓%s %s\n' "$_G" "$_N" "$*"; }
warn()  { printf '    %s!%s %s\n' "$_Y" "$_N" "$*" >&2; }
die()   { printf '\n%sStopped:%s %s\n' "$_R$_B" "$_N" "$*" >&2; exit 1; }

# A box for things only a person can do. Printed, then the script either
# waits (ask) or stops (die) depending on the step.
manual() {
  local title="$1"; shift
  printf '\n%s┌─ ACTION NEEDED: %s%s\n' "$_Y$_B" "$title" "$_N"
  local line
  for line in "$@"; do printf '%s│%s %s\n' "$_Y" "$_N" "$line"; done
  printf '%s└─%s\n' "$_Y" "$_N"
}

# ask VAR "Question" "default"   — prompt unless --yes, keep default on empty
ask() {
  local __var="$1" question="$2" default="${3:-}" reply=""
  if [[ "${OSCE_ASSUME_YES:-0}" == 1 || ! -t 0 ]]; then
    printf -v "$__var" '%s' "$default"; return
  fi
  if [[ -n "$default" ]]; then
    read -r -p "    $question [$default]: " reply || true
  else
    read -r -p "    $question: " reply || true
  fi
  printf -v "$__var" '%s' "${reply:-$default}"
}

# confirm "Question" → 0 yes / 1 no. Default yes. --yes answers yes.
confirm() {
  [[ "${OSCE_ASSUME_YES:-0}" == 1 ]] && return 0
  [[ -t 0 ]] || return 0
  local reply=""
  read -r -p "    $1 [Y/n]: " reply || true
  [[ -z "$reply" || "$reply" =~ ^[Yy] ]]
}

random_string() {  # URL- and .env-safe
  local n="${1:-32}"
  python3 -c 'import secrets, string, sys; a = string.ascii_letters + string.digits; print("".join(secrets.choice(a) for _ in range(int(sys.argv[1]))), end="")' "$n"
}

# ---------------------------------------------------------------------------
# gcloud
# ---------------------------------------------------------------------------
require_gcloud() {
  command -v gcloud >/dev/null 2>&1 || die "gcloud is not installed. Open Google Cloud Shell (https://shell.cloud.google.com) and run this there: it has everything already."
  local account
  account="$(gcloud auth list --filter=status:ACTIVE --format='value(account)' 2>/dev/null | head -n1)"
  if [[ -z "$account" ]]; then
    step "Signing in to Google Cloud"
    gcloud auth login --update-adc || die "Sign-in did not finish."
    account="$(gcloud auth list --filter=status:ACTIVE --format='value(account)' | head -n1)"
  fi
  ACTIVE_ACCOUNT="$account"
}

gc() { gcloud --project "$PROJECT" --quiet "$@"; }

project_number() {
  gcloud projects describe "$PROJECT" --format='value(projectNumber)'
}

deploy_bucket()  { printf '%s-osce-deploy' "$PROJECT"; }
history_bucket() { printf '%s-osce-gate-history' "$PROJECT"; }

# ---------------------------------------------------------------------------
# Saved configuration (KEY=value, no secrets). Parsed, never sourced.
# ---------------------------------------------------------------------------
_is_config_key() {
  local k
  for k in "${OSCE_CONFIG_KEYS[@]}"; do [[ "$k" == "$1" ]] && return 0; done
  return 1
}

config_parse() {   # stdin → shell variables for known keys only
  local line key value
  while IFS= read -r line || [[ -n "$line" ]]; do
    [[ "$line" =~ ^([A-Z_]+)=(.*)$ ]] || continue
    key="${BASH_REMATCH[1]}"; value="${BASH_REMATCH[2]}"
    _is_config_key "$key" || continue
    printf -v "$key" '%s' "$value"
  done
}

config_render() {
  local k
  printf '# OSCE marker deployment settings. Written by deploy/gcp/setup.sh.\n'
  printf '# No secrets in here: those live in Secret Manager.\n'
  for k in "${OSCE_CONFIG_KEYS[@]}"; do
    printf '%s=%s\n' "$k" "${!k:-}"
  done
}

config_local_path() { printf '%s/.osce-gcp/%s.env' "$HOME" "$PROJECT"; }

config_load() {
  local remote text
  remote="gs://$(deploy_bucket)/config.env"
  if text="$(gcloud storage cat "$remote" 2>/dev/null)"; then
    config_parse <<<"$text"
    ok "Found settings from an earlier setup ($remote)"
    return 0
  fi
  if [[ -f "$(config_local_path)" ]]; then
    config_parse <"$(config_local_path)"
    ok "Found settings from an earlier setup on this computer"
    return 0
  fi
  return 1
}

config_save() {
  local path; path="$(config_local_path)"
  mkdir -p "$(dirname "$path")"
  config_render >"$path"
  if gcloud storage buckets describe "gs://$(deploy_bucket)" >/dev/null 2>&1; then
    gcloud storage cp --quiet "$path" "gs://$(deploy_bucket)/config.env" >/dev/null
  fi
}

# ---------------------------------------------------------------------------
# Small idempotent building blocks
# ---------------------------------------------------------------------------
secret_exists() { gc secrets describe "$1" >/dev/null 2>&1; }

# secret_ensure NAME VALUE  — create once; never overwrite an existing value.
secret_ensure() {
  local name="$1" value="$2"
  if secret_exists "$name"; then
    ok "Secret $name already exists (kept)"
    return
  fi
  gc secrets create "$name" --replication-policy=automatic >/dev/null
  printf '%s' "$value" | gc secrets versions add "$name" --data-file=- >/dev/null
  ok "Created secret $name"
}

# secret_set NAME VALUE — add a new version (used for values the person typed).
secret_set() {
  local name="$1" value="$2"
  secret_exists "$name" || gc secrets create "$name" --replication-policy=automatic >/dev/null
  printf '%s' "$value" | gc secrets versions add "$name" --data-file=- >/dev/null
}

secret_read() { gc secrets versions access latest --secret "$1" 2>/dev/null; }

sa_email() { printf '%s@%s.iam.gserviceaccount.com' "$1" "$PROJECT"; }

sa_ensure() {
  local name="$1" display="$2"
  if gc iam service-accounts describe "$(sa_email "$name")" >/dev/null 2>&1; then
    ok "Service account $name exists"
    return
  fi
  gc iam service-accounts create "$name" --display-name "$display" >/dev/null
  # IAM is eventually consistent: a brand-new account can be "not found" to
  # the next binding for a few seconds.
  local i
  for i in 1 2 3 4 5 6 7 8 9 10; do
    gc iam service-accounts describe "$(sa_email "$name")" >/dev/null 2>&1 && break
    sleep 3
  done
  ok "Created service account $name"
}

# role_ensure ID TITLE PERMS(comma-separated)
# A deleted custom role keeps its id for weeks, so a re-setup after a
# teardown undeletes it rather than failing on "already exists".
role_ensure() {
  local id="$1" title="$2" perms="$3" deleted
  deleted="$(gc iam roles describe "$id" --format='value(deleted)' 2>/dev/null || echo missing)"
  case "$deleted" in
    missing)
      gc iam roles create "$id" --title "$title" --permissions "$perms" --stage GA >/dev/null
      ok "Created custom role $id" ;;
    True|true)
      gc iam roles undelete "$id" >/dev/null
      gc iam roles update "$id" --permissions "$perms" >/dev/null
      ok "Restored custom role $id" ;;
    *)
      gc iam roles update "$id" --permissions "$perms" >/dev/null
      ok "Custom role $id is up to date" ;;
  esac
}

# retry N CMD... — for IAM bindings that race a just-created account.
retry() {
  local n="$1"; shift
  local i
  for ((i = 1; i <= n; i++)); do
    if "$@" >/dev/null 2>&1; then return 0; fi
    sleep $((i * 3))
  done
  "$@"   # last try, with its error visible
}

bucket_ensure() {
  local bucket="$1"
  if gcloud storage buckets describe "gs://$bucket" >/dev/null 2>&1; then
    ok "Bucket gs://$bucket exists"
    return
  fi
  gcloud storage buckets create "gs://$bucket" --project "$PROJECT" \
    --location "$REGION" --uniform-bucket-level-access --public-access-prevention >/dev/null
  ok "Created bucket gs://$bucket"
}

# The domain the app answers on. With no custom domain, an sslip.io name
# that resolves to the static IP, so HTTPS works with no DNS to set up.
app_domain() {
  if [[ -n "${DOMAIN:-}" ]]; then printf '%s' "$DOMAIN"; return; fi
  printf '%s.sslip.io' "${STATIC_IP//./-}"
}
