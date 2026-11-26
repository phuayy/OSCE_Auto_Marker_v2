#!/bin/bash
# Startup script for the marker's GPU VM. deploy/gcp/setup.sh attaches it as
# the instance's `startup-script` metadata; Google runs it as root on every
# boot.
#
# Every boot it compares the app bundle named in metadata with the one
# installed. Same bundle: nothing to do (a second at most, so a start from
# the gate is not slowed down). New bundle, or first boot: install or update
# the app, then report the outcome to the guest attribute osce/install, which
# setup.sh watches.
#
# Everything it needs comes from instance metadata, written by setup.sh:
#   osce-bundle         gs://bucket/path/to/bundle.tar.gz
#   osce-domain         the HTTPS name Caddy serves (and gets a certificate for)
#   osce-database       postgres | sqlite
#   osce-idle-minutes   IDLE_SHUTDOWN_MINUTES
#   osce-admin-email    DEFAULT_ADMIN_EMAIL (optional)
#   osce-admin-secret   Secret Manager name holding the first admin's password
#   osce-hf-secret      Secret Manager name holding the HuggingFace token (optional)
#
#   osce-watchdog-minutes        boot grace before the watchdog powers off a VM whose
#                                app never became ready (default 60)
#   osce-failure-grace-minutes   delay before a failed install powers the VM off (default 15)
#
# Two safety nets, because idle shutdown lives inside the app and a VM whose
# app is not healthy would otherwise bill its GPU until someone noticed:
#
#   * The boot watchdog (osce-boot-watchdog.timer) is installed on EVERY boot,
#     before any step that can fail, and is independent of the app.
#   * A failed install or update schedules a delayed power-off (cancellable).
#
# An update never destroys the running version before the new one is proven:
# the old code is kept as $APP_DIR.old and the database is backed up first, and
# a new version that does not become ready is rolled back (code and database).
#
# Log: /var/log/osce-install.log (also in the VM's serial console).

set -Eeuo pipefail

LOG="${OSCE_LOG:-/var/log/osce-install.log}"
APP_DIR="${OSCE_APP_DIR:-/opt/osce-marker}"
DATA_DIR="${OSCE_DATA_DIR:-/var/lib/osce-marker}"
STATE_FILE="$DATA_DIR/installed-bundle"
SECRETS_FILE="$DATA_DIR/secrets.env"      # generated once, never replaced
EXTRA_ENV="$DATA_DIR/extra.env"            # operator overrides, appended last
SYSTEMD_DIR="${OSCE_SYSTEMD_DIR:-/etc/systemd/system}"
SBIN_DIR="${OSCE_SBIN_DIR:-/usr/local/sbin}"
BACKUPS_DIR="$DATA_DIR/backups"            # pre-update database backups
NEEDS_OPERATOR="$DATA_DIR/update-needs-operator"
SERVICE_USER=osce
UV_BIN="${OSCE_UV_BIN:-/usr/local/bin/uv}"
READY_URL="${OSCE_READY_URL:-http://127.0.0.1:8787/api/health/ready}"
READY_TRIES="${OSCE_READY_TRIES:-120}"     # x READY_SLEEP seconds = 10 minutes
READY_SLEEP="${OSCE_READY_SLEEP:-5}"
KEEP_BACKUPS="${OSCE_KEEP_BACKUPS:-3}"
MD=http://metadata.google.internal/computeMetadata/v1

CURRENT_STEP="starting"
FAIL_GRACE_MIN=15          # replaced from metadata once that has been read
WATCHDOG_GRACE_MIN=60
UPDATING=0                 # 1 when a working version is installed and being replaced
OLD_BUNDLE=""
PRE_UPDATE_BACKUP=""
MIGRATION_ATTEMPTED=0      # 1 once the new app may have run Alembic

md() { curl -fsS -H 'Metadata-Flavor: Google' "$MD/$1" 2>/dev/null || true; }
attr() { md "instance/attributes/$1"; }
put_guest_attribute() {  # put_guest_attribute key "state detail"
  curl -fsS -X PUT -H 'Metadata-Flavor: Google' --data "$2 $(date +%s)" \
    "$MD/instance/guest-attributes/osce/$1" >/dev/null 2>&1 || true
}
report() { put_guest_attribute install "$1"; }       # setup.sh watches this one
report_boot() { put_guest_attribute boot "$1"; }     # the start gate watches this one
stage() { CURRENT_STEP="$1"; echo "=== $(date -Is) $1"; report "installing $1"; }
own() { chown "$SERVICE_USER:$SERVICE_USER" "$@"; }

access_token() {
  md "instance/service-accounts/default/token" |
    python3 -c 'import json,sys; print(json.load(sys.stdin)["access_token"])'
}

# Secret Manager over REST, so nothing depends on gcloud being on the image.
read_secret() {
  local name="$1" project token
  [[ -n "$name" ]] || return 0
  project="$(md project/project-id)"
  token="$(access_token)"
  curl -fsS -H "Authorization: Bearer $token" \
    "https://secretmanager.googleapis.com/v1/projects/$project/secrets/$name/versions/latest:access" 2>/dev/null |
    python3 -c 'import base64,json,sys; print(base64.b64decode(json.load(sys.stdin)["payload"]["data"]).decode(), end="")' ||
    true
}

download_bundle() {  # gs://bucket/object → file
  local uri="$1" dest="$2" bucket object token
  bucket="${uri#gs://}"; bucket="${bucket%%/*}"
  object="${uri#gs://"$bucket"/}"
  object="$(python3 -c 'import sys,urllib.parse; print(urllib.parse.quote(sys.argv[1], safe=""))' "$object")"
  token="$(access_token)"
  curl -fsS -o "$dest" -H "Authorization: Bearer $token" \
    "https://storage.googleapis.com/storage/v1/b/$bucket/o/$object?alt=media"
}

apt_install() {
  DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=900 install -y --no-install-recommends "$@"
}

as_user() {
  sudo -u "$SERVICE_USER" -H env \
    HOME="$DATA_DIR/home" \
    UV_CACHE_DIR=/var/cache/osce-uv \
    UV_PYTHON_INSTALL_DIR=/opt/osce-python \
    PATH="/opt/node/bin:/usr/local/bin:/usr/bin:/bin" \
    "$@"
}

# ---------------------------------------------------------------------------
# Readiness, failure handling, and the update transaction. Functions only: the
# tests source this file (OSCE_STARTUP_SOURCE_ONLY=1) and drive them with fakes.
# ---------------------------------------------------------------------------
app_ready() { curl -fsS --max-time 5 "$READY_URL" 2>/dev/null | grep -q '"ready": *true'; }

wait_for_app() {
  local i
  for ((i = 0; i < READY_TRIES; i++)); do
    if app_ready; then return 0; fi
    sleep "$READY_SLEEP"
  done
  return 1
}

# A failed start must not leave a GPU VM billing. Idle shutdown lives inside the
# app, which is not healthy here, so power off after a grace period that still
# lets someone who is watching log in and cancel it.
schedule_poweroff() {
  echo "!!! The marker is not running. This machine powers off in $FAIL_GRACE_MIN minutes,"
  echo "    so a failed start does not keep billing for the GPU until someone notices."
  echo "    To keep it running and investigate, cancel BOTH safety nets:"
  echo "      sudo shutdown -c"
  echo "      sudo systemctl stop osce-boot-watchdog.timer"
  echo "    Log: $LOG"
  shutdown -h "+$FAIL_GRACE_MIN" "OSCE marker failed to start; powering off (sudo shutdown -c cancels)" || true
}

# The boot watchdog: a host-level check that does not depend on the app.
#   * never ready this boot: power off once the boot grace has passed;
#   * was ready, then stops answering: power off after MISS_LIMIT checks in a row
#     (checks are 10 minutes apart), so a brief restart is forgiven and a
#     crash-loop is not;
#   * ready: nothing. From then on the app's own idle shutdown owns the VM.
boot_watchdog_script() {
  cat <<'WATCHDOG'
#!/bin/bash
# Installed by deploy/gcp/setup/vm-startup.sh. Run by osce-boot-watchdog.timer.
set -u
GRACE_MIN="${OSCE_WATCHDOG_GRACE_MINUTES:-60}"
MISS_LIMIT="${OSCE_WATCHDOG_MISSES:-3}"
STATE_DIR="${OSCE_WATCHDOG_STATE_DIR:-/run/osce-boot-watchdog}"   # tmpfs: empty on every boot
READY_URL="${OSCE_READY_URL:-http://127.0.0.1:8787/api/health/ready}"
UPTIME_FILE="${OSCE_UPTIME_FILE:-/proc/uptime}"
MD=http://metadata.google.internal/computeMetadata/v1

mkdir -p "$STATE_DIR"
if curl -fsS --max-time 10 "$READY_URL" 2>/dev/null | grep -q '"ready": *true'; then
  : >"$STATE_DIR/ever-ready"
  echo 0 >"$STATE_DIR/misses"
  exit 0
fi

if [[ -f "$STATE_DIR/ever-ready" ]]; then
  misses=$(( $(cat "$STATE_DIR/misses" 2>/dev/null || echo 0) + 1 ))
  echo "$misses" >"$STATE_DIR/misses"
  echo "osce-boot-watchdog: the marker is not answering ($misses of $MISS_LIMIT checks)"
  (( misses >= MISS_LIMIT )) || exit 0
  reason="the marker stopped answering"
else
  read -r uptime _ <"$UPTIME_FILE"
  if (( ${uptime%.*} < GRACE_MIN * 60 )); then
    echo "osce-boot-watchdog: still starting (grace is $GRACE_MIN minutes)"
    exit 0
  fi
  reason="the marker never became ready within $GRACE_MIN minutes"
fi

echo "osce-boot-watchdog: powering off: $reason"
curl -fsS -X PUT -H 'Metadata-Flavor: Google' --data "failed $(date +%s)" \
  "$MD/instance/guest-attributes/osce/boot" >/dev/null 2>&1 || true
exec shutdown -h now "OSCE boot watchdog: $reason"
WATCHDOG
}

install_boot_watchdog() {
  boot_watchdog_script >"$SBIN_DIR/osce-boot-watchdog"
  chmod 755 "$SBIN_DIR/osce-boot-watchdog"
  cat >"$SYSTEMD_DIR/osce-boot-watchdog.service" <<UNIT
# Written by deploy/gcp/setup/vm-startup.sh on every boot.
[Unit]
Description=Power off the VM when the OSCE marker is not healthy

[Service]
Type=oneshot
Environment=OSCE_WATCHDOG_GRACE_MINUTES=$WATCHDOG_GRACE_MIN
ExecStart=$SBIN_DIR/osce-boot-watchdog
UNIT
  cat >"$SYSTEMD_DIR/osce-boot-watchdog.timer" <<'UNIT'
# Written by deploy/gcp/setup/vm-startup.sh on every boot.
[Unit]
Description=Check every 10 minutes that the OSCE marker is healthy

[Timer]
OnBootSec=10min
OnUnitActiveSec=10min

[Install]
WantedBy=timers.target
UNIT
  systemctl daemon-reload
  systemctl enable --now osce-boot-watchdog.timer >/dev/null
}

# Before the new code can touch the database: a consistent backup, taken with
# the old app stopped so it is exactly the state a rollback must return to.
# Failing here aborts the update with the old code still in place.
backup_before_update() {
  local id dest file
  id="$(printf '%s' "$(basename "$OLD_BUNDLE")" | tr -c 'A-Za-z0-9._-' '_')"
  dest="$BACKUPS_DIR/pre-update-$(date -u +%Y%m%dT%H%M%SZ)-$id"
  mkdir -p "$dest"
  own "$BACKUPS_DIR" "$dest"
  as_user bash -c "cd '$APP_DIR' && '$UV_BIN' run --no-sync python scripts/backup_database.py --dest '$dest'"
  file="$(find "$dest" -maxdepth 1 -type f -name 'osce-db-*' | head -n1)"
  if [[ -z "$file" ]]; then
    echo "The backup script reported success but wrote no file into $dest."
    return 1
  fi
  PRE_UPDATE_BACKUP="$file"
  echo "Pre-update database backup: $PRE_UPDATE_BACKUP"
}

prune_backups() {
  local old
  while IFS= read -r old; do
    echo "Pruning old pre-update backup: $old"
    rm -rf "$old"
  done < <(find "$BACKUPS_DIR" -maxdepth 1 -mindepth 1 -type d -name 'pre-update-*' 2>/dev/null |
    sort -r | tail -n +"$((KEEP_BACKUPS + 1))")
}

# The old code stays beside the new until the new app is proven ready.
swap_in_new_app() {
  local keep
  rm -rf "$APP_DIR.old" "$APP_DIR.failed"
  if [[ -d "$APP_DIR" ]]; then mv "$APP_DIR" "$APP_DIR.old"; fi
  mv "$APP_DIR.new" "$APP_DIR"
  # Keep the built environments: a re-sync over them is seconds, not minutes.
  for keep in .venv node_modules; do
    if [[ -d "$APP_DIR.old/$keep" ]]; then mv "$APP_DIR.old/$keep" "$APP_DIR/$keep"; fi
  done
  own -R "$APP_DIR"
}

# Only after the new app answered ready.
commit_update() {
  echo "$BUNDLE" >"$STATE_FILE"
  rm -rf "$APP_DIR.old" "$APP_DIR.failed"
  prune_backups
}

# Stop the old app, back up (updates only), then swap the new code in.
replace_app_code() {
  systemctl stop osce-marker >/dev/null 2>&1 || true
  if [[ "$UPDATING" == 1 ]]; then
    stage "backup"
    backup_before_update
  fi
  stage "install-code"
  swap_in_new_app
}

# The new app is only trusted once it answers ready. A failure here goes through
# the ERR trap: an update is rolled back, a first install schedules the power-off.
finish_install() {
  if ! wait_for_app; then
    echo "The app did not report ready within 10 minutes. Last log lines:"
    journalctl -u osce-marker -n 60 --no-pager || true
    false
  fi
  commit_update
}

# Put the previous version back and serve it. Returns 0 when it answers ready.
# Every step is `|| return 1`: this runs with the ERR trap off, and a rollback
# that half-succeeds must say so rather than carry on.
rollback_update() {
  local keep
  if [[ ! -d "$APP_DIR.old" && "$MIGRATION_ATTEMPTED" != 1 ]] && app_ready; then
    echo "The previous version is still serving; nothing to roll back."
    return 0
  fi
  echo "=== $(date -Is) rolling back to the previous version ($OLD_BUNDLE)"
  systemctl stop osce-marker >/dev/null 2>&1 || true
  if [[ -d "$APP_DIR.old" ]]; then
    rm -rf "$APP_DIR.failed"
    if [[ -d "$APP_DIR" ]]; then mv "$APP_DIR" "$APP_DIR.failed" || return 1; fi
    for keep in .venv node_modules; do
      if [[ -d "$APP_DIR.failed/$keep" ]]; then mv "$APP_DIR.failed/$keep" "$APP_DIR.old/$keep" || return 1; fi
    done
    mv "$APP_DIR.old" "$APP_DIR" || return 1
    own -R "$APP_DIR" || return 1
    # The shared .venv was synced to the new lock; put it back on the old one.
    as_user bash -c "cd '$APP_DIR' && '$UV_BIN' sync --frozen --no-dev" || return 1
  fi
  if [[ "$MIGRATION_ATTEMPTED" == 1 ]]; then
    if [[ -z "$PRE_UPDATE_BACKUP" ]]; then
      echo "The new app may have migrated the database but there is no backup to restore."
      return 1
    fi
    echo "Restoring the database from $PRE_UPDATE_BACKUP"
    as_user bash -c "cd '$APP_DIR' && '$UV_BIN' run --no-sync python scripts/backup_database.py --restore '$PRE_UPDATE_BACKUP' --confirm" || return 1
  fi
  systemctl restart osce-marker || return 1
  wait_for_app
}

# Everything that must happen when a step fails (the ERR trap's body). Runs with
# the trap disarmed so the recovery cannot re-enter it.
fail_boot() {
  local step="$1" code="$2" outcome="$1"   # the report is "failed <step> [rolled-back|rollback-failed]"
  trap - ERR
  echo "!!! install failed during: $step (exit $code)"
  if [[ "$UPDATING" == 1 ]]; then
    if rollback_update; then
      echo "!!! The update failed; the previous version ($OLD_BUNDLE) is serving again."
      echo "    The next start retries the update. Failed copy kept at $APP_DIR.failed for inspection."
      report "failed $step rolled-back"
      exit "$code"
    fi
    outcome="$step rollback-failed"
    echo "!!! The rollback failed too. Nothing is deleted: the previous code is in $APP_DIR.old (or $APP_DIR),"
    echo "    the pre-update backup in ${PRE_UPDATE_BACKUP:-<none taken>}. Further updates are refused until"
    echo "    an operator fixes this and removes $NEEDS_OPERATOR."
    echo "update=$BUNDLE previous=$OLD_BUNDLE backup=${PRE_UPDATE_BACKUP:-none} failedStep=$step at=$(date -Is)" >"$NEEDS_OPERATOR" || true
  fi
  report "failed $outcome"
  report_boot failed
  schedule_poweroff
  exit "$code"
}

on_error() {
  local code=$?
  fail_boot "$CURRENT_STEP" "$code"
}
trap on_error ERR

if [[ "${OSCE_STARTUP_SOURCE_ONLY:-0}" == 1 ]]; then return 0 2>/dev/null || exit 0; fi

exec > >(tee -a "$LOG") 2>&1

# ---------------------------------------------------------------------------
BUNDLE="$(attr osce-bundle)"
DOMAIN="$(attr osce-domain)"
DATABASE="$(attr osce-database)"; DATABASE="${DATABASE:-postgres}"
IDLE_MINUTES="$(attr osce-idle-minutes)"; IDLE_MINUTES="${IDLE_MINUTES:-30}"
ADMIN_EMAIL="$(attr osce-admin-email)"
ADMIN_SECRET="$(attr osce-admin-secret)"
HF_SECRET="$(attr osce-hf-secret)"
FAIL_GRACE_MIN="$(attr osce-failure-grace-minutes)"; FAIL_GRACE_MIN="${FAIL_GRACE_MIN:-15}"
WATCHDOG_GRACE_MIN="$(attr osce-watchdog-minutes)"; WATCHDOG_GRACE_MIN="${WATCHDOG_GRACE_MIN:-60}"

if [[ -z "$BUNDLE" || -z "$DOMAIN" ]]; then
  echo "No osce-bundle/osce-domain metadata: nothing to install."
  exit 0
fi

# Before anything that can fail, and on every boot (also the "nothing to do"
# ones): the host-level net that powers the VM off if the app never gets healthy.
install_boot_watchdog

if [[ -f "$STATE_FILE" && "$(cat "$STATE_FILE")" == "$BUNDLE" ]] && systemctl is-enabled osce-marker >/dev/null 2>&1; then
  echo "$(date -Is) bundle unchanged ($BUNDLE); nothing to do."
  report "done unchanged"
  exit 0
fi

if [[ -f "$STATE_FILE" && -d "$APP_DIR" ]]; then
  UPDATING=1
  OLD_BUNDLE="$(cat "$STATE_FILE")"
fi
if [[ "$UPDATING" == 1 && -f "$NEEDS_OPERATOR" ]]; then
  echo "!!! An earlier update and its rollback both failed ($(cat "$NEEDS_OPERATOR"))."
  echo "    Not touching anything. Fix it, then remove $NEEDS_OPERATOR to let updates run again."
  report "failed needs-operator"
  report_boot failed
  schedule_poweroff
  exit 1
fi

echo "=== $(date -Is) installing $BUNDLE for https://$DOMAIN${OLD_BUNDLE:+ (replacing $OLD_BUNDLE)}"

# ---------------------------------------------------------------------------
stage "system-packages"
DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=900 update -y
apt_install ca-certificates curl gnupg tar gzip openssl sudo python3 ffmpeg git \
  build-essential debian-keyring debian-archive-keyring apt-transport-https
if [[ "$DATABASE" == "postgres" ]]; then
  apt_install postgresql
fi

stage "caddy"
if ! command -v caddy >/dev/null 2>&1; then
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' |
    gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
    >/etc/apt/sources.list.d/caddy-stable.list
  DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=900 update -y
  apt_install caddy
fi

stage "nodejs"
node_major() { /opt/node/bin/node -v 2>/dev/null | sed -E 's/^v([0-9]+).*/\1/' || echo 0; }
if [[ "$(node_major)" -lt 22 ]]; then
  base=https://nodejs.org/dist/latest-v22.x
  tarball="$(curl -fsS "$base/SHASUMS256.txt" | awk '/linux-x64\.tar\.xz$/ {print $2; exit}')"
  sum="$(curl -fsS "$base/SHASUMS256.txt" | awk -v f="$tarball" '$2 == f {print $1}')"
  curl -fsSL -o /tmp/node.tar.xz "$base/$tarball"
  echo "$sum  /tmp/node.tar.xz" | sha256sum -c -
  rm -rf /opt/node && mkdir -p /opt/node
  tar -xJf /tmp/node.tar.xz -C /opt/node --strip-components=1
  rm -f /tmp/node.tar.xz
fi

stage "uv"
if ! command -v /usr/local/bin/uv >/dev/null 2>&1; then
  curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin UV_NO_MODIFY_PATH=1 sh
fi

stage "service-user"
if ! id "$SERVICE_USER" >/dev/null 2>&1; then
  useradd --system --home-dir "$DATA_DIR/home" --shell /usr/sbin/nologin "$SERVICE_USER"
fi
mkdir -p "$DATA_DIR/home" "$DATA_DIR/storage" /var/cache/osce-uv /opt/osce-python
chown -R "$SERVICE_USER:$SERVICE_USER" "$DATA_DIR/home" "$DATA_DIR/storage" /var/cache/osce-uv /opt/osce-python
chown "$SERVICE_USER:$SERVICE_USER" "$DATA_DIR"

stage "gpu-driver"
# Deep Learning VM images ship the driver; give a first boot's driver set-up
# time to finish before anything tries CUDA.
for _ in $(seq 1 90); do
  nvidia-smi >/dev/null 2>&1 && break
  sleep 10
done
if nvidia-smi >/dev/null 2>&1; then
  nvidia-smi --query-gpu=name,driver_version --format=csv,noheader
  GPU_DEVICE=cuda
else
  echo "WARNING: no working NVIDIA driver after 15 minutes; the marker will run on the CPU (slow)."
  GPU_DEVICE=cpu
fi

stage "secrets"
if [[ ! -f "$SECRETS_FILE" ]]; then
  umask 077
  {
    echo "AUTH_SECRET=$(openssl rand -hex 32)"
    echo "CREDENTIAL_ENCRYPTION_KEY=$(openssl rand -base64 32)"
    echo "POSTGRES_PASSWORD=$(openssl rand -hex 24)"
  } >"$SECRETS_FILE"
  umask 022
fi
chmod 600 "$SECRETS_FILE"
# shellcheck disable=SC1090
source "$SECRETS_FILE"
ADMIN_PASSWORD="$(read_secret "$ADMIN_SECRET")"
HF_TOKEN="$(read_secret "$HF_SECRET")"
if [[ -z "$ADMIN_PASSWORD" ]]; then
  echo "Could not read the admin password secret '$ADMIN_SECRET'."
  false
fi

if [[ "$DATABASE" == "postgres" ]]; then
  stage "database"
  cd /tmp
  systemctl enable --now postgresql
  for _ in $(seq 1 30); do sudo -u postgres psql -qtAc 'select 1' >/dev/null 2>&1 && break; sleep 2; done
  if [[ "$(sudo -u postgres psql -qtAc "select 1 from pg_roles where rolname='osce'")" != 1 ]]; then
    sudo -u postgres psql -qc "create role osce login password '$POSTGRES_PASSWORD'"
  else
    sudo -u postgres psql -qc "alter role osce password '$POSTGRES_PASSWORD'"
  fi
  if [[ "$(sudo -u postgres psql -qtAc "select 1 from pg_database where datname='osce_marker'")" != 1 ]]; then
    sudo -u postgres psql -qc "create database osce_marker owner osce"
  fi
  DATABASE_LINE="APP_DATABASE_URL=postgresql+psycopg://osce:$POSTGRES_PASSWORD@127.0.0.1:5432/osce_marker"
else
  DATABASE_LINE="APP_DATABASE_URL="
fi

stage "app-code"
rm -rf "$APP_DIR.new" && mkdir -p "$APP_DIR.new"
download_bundle "$BUNDLE" /tmp/osce-bundle.tar.gz
tar -xzf /tmp/osce-bundle.tar.gz -C "$APP_DIR.new"
rm -f /tmp/osce-bundle.tar.gz
replace_app_code

stage "configuration"
umask 077
{
  echo "# Written by deploy/gcp/setup/vm-startup.sh on every update."
  echo "# Put your own overrides in $EXTRA_ENV: they are appended last and win."
  echo "ENVIRONMENT=production"
  echo "API_HOST=127.0.0.1"
  echo "API_PORT=8787"
  echo "SERVE_FRONTEND=true"
  echo "APP_PUBLIC_URL=https://$DOMAIN"
  echo "CORS_ALLOW_ORIGINS=https://$DOMAIN"
  echo "STORAGE_ROOT=$DATA_DIR/storage"
  echo "$DATABASE_LINE"
  echo "TRUSTED_PROXY_COUNT=1"
  echo "TRUSTED_PROXY_IPS=127.0.0.1"
  echo "IDLE_SHUTDOWN_MINUTES=$IDLE_MINUTES"
  echo "IDLE_SHUTDOWN_COMMAND=sudo -n /usr/sbin/shutdown -h now"
  echo "BOOT_STAGE_REPORTING=true"
  echo "WHISPERX_DEVICE=$GPU_DEVICE"
  echo "DEFAULT_ADMIN_USERNAME=admin"
  echo "DEFAULT_ADMIN_PASSWORD=$ADMIN_PASSWORD"
  echo "DEFAULT_ADMIN_EMAIL=$ADMIN_EMAIL"
  echo "AUTH_SECRET=$AUTH_SECRET"
  echo "CREDENTIAL_ENCRYPTION_KEY=$CREDENTIAL_ENCRYPTION_KEY"
  echo "WHISPERX_HF_TOKEN=$HF_TOKEN"
  if [[ -f "$EXTRA_ENV" ]]; then
    echo "# --- $EXTRA_ENV"
    cat "$EXTRA_ENV"
  fi
} >"$APP_DIR/.env"
umask 022
chown "$SERVICE_USER:$SERVICE_USER" "$APP_DIR/.env"
chmod 600 "$APP_DIR/.env"

stage "python-dependencies"
as_user bash -c "cd '$APP_DIR' && /usr/local/bin/uv sync --frozen --no-dev"

stage "frontend-build"
as_user bash -c "cd '$APP_DIR' && npm ci --no-audit --no-fund && npm run build"

stage "services"
cat >/etc/sudoers.d/osce-shutdown <<EOF
$SERVICE_USER ALL=(root) NOPASSWD: /usr/sbin/shutdown -h now
EOF
chmod 440 /etc/sudoers.d/osce-shutdown
visudo -cf /etc/sudoers.d/osce-shutdown

AFTER_DB=""
if [[ "$DATABASE" == "postgres" ]]; then AFTER_DB="postgresql.service"; fi
cat >/etc/systemd/system/osce-marker.service <<EOF
# Written by deploy/gcp/setup/vm-startup.sh (from deploy/systemd/osce-marker.service.example).
[Unit]
Description=OSCE AI Marker API
After=network-online.target $AFTER_DB
Wants=network-online.target $AFTER_DB

[Service]
Type=simple
User=$SERVICE_USER
Group=$SERVICE_USER
WorkingDirectory=$APP_DIR
Environment=HOME=$DATA_DIR/home
Environment=UV_CACHE_DIR=/var/cache/osce-uv
Environment=UV_PYTHON_INSTALL_DIR=/opt/osce-python
Environment=PATH=/opt/node/bin:/usr/local/bin:/usr/bin:/bin
ExecStart=/usr/local/bin/uv run --no-sync python scripts/run_api.py
Restart=on-failure
RestartSec=5
TimeoutStopSec=20
LimitNOFILE=65536

[Install]
WantedBy=multi-user.target
EOF

# Caddy gets and renews the HTTPS certificate by itself. flush_interval -1
# keeps the change feed (SSE) unbuffered; Caddy sets X-Forwarded-For, which
# the app trusts from 127.0.0.1 only (TRUSTED_PROXY_IPS above).
cat >/etc/caddy/Caddyfile <<EOF
# Written by deploy/gcp/setup/vm-startup.sh.
$DOMAIN {
	reverse_proxy 127.0.0.1:8787 {
		flush_interval -1
	}
}
EOF

systemctl daemon-reload
systemctl enable caddy osce-marker >/dev/null
systemctl restart caddy
MIGRATION_ATTEMPTED=1   # the app runs Alembic at boot: from here a rollback restores the database
systemctl restart osce-marker

stage "waiting-for-app"
finish_install
trap - ERR
report "done $GPU_DEVICE"
echo "=== $(date -Is) installed and ready at https://$DOMAIN"
