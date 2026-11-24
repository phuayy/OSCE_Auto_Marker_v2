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
# Log: /var/log/osce-install.log (also in the VM's serial console).

set -Eeuo pipefail

LOG=/var/log/osce-install.log
exec > >(tee -a "$LOG") 2>&1

APP_DIR=/opt/osce-marker
DATA_DIR=/var/lib/osce-marker
STATE_FILE="$DATA_DIR/installed-bundle"
SECRETS_FILE="$DATA_DIR/secrets.env"      # generated once, never replaced
EXTRA_ENV="$DATA_DIR/extra.env"            # operator overrides, appended last
SERVICE_USER=osce
MD=http://metadata.google.internal/computeMetadata/v1

CURRENT_STEP="starting"

md() { curl -fsS -H 'Metadata-Flavor: Google' "$MD/$1" 2>/dev/null || true; }
attr() { md "instance/attributes/$1"; }
report() {  # report "state detail" → guest attribute osce/install
  curl -fsS -X PUT -H 'Metadata-Flavor: Google' --data "$1 $(date +%s)" \
    "$MD/instance/guest-attributes/osce/install" >/dev/null 2>&1 || true
}
stage() { CURRENT_STEP="$1"; echo "=== $(date -Is) $1"; report "installing $1"; }

on_error() {
  local code=$?
  echo "!!! install failed during: $CURRENT_STEP (exit $code)"
  report "failed $CURRENT_STEP"
  exit "$code"
}
trap on_error ERR

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
BUNDLE="$(attr osce-bundle)"
DOMAIN="$(attr osce-domain)"
DATABASE="$(attr osce-database)"; DATABASE="${DATABASE:-postgres}"
IDLE_MINUTES="$(attr osce-idle-minutes)"; IDLE_MINUTES="${IDLE_MINUTES:-30}"
ADMIN_EMAIL="$(attr osce-admin-email)"
ADMIN_SECRET="$(attr osce-admin-secret)"
HF_SECRET="$(attr osce-hf-secret)"

if [[ -z "$BUNDLE" || -z "$DOMAIN" ]]; then
  echo "No osce-bundle/osce-domain metadata: nothing to install."
  exit 0
fi

if [[ -f "$STATE_FILE" && "$(cat "$STATE_FILE")" == "$BUNDLE" ]] && systemctl is-enabled osce-marker >/dev/null 2>&1; then
  echo "$(date -Is) bundle unchanged ($BUNDLE); nothing to do."
  report "done unchanged"
  exit 0
fi

echo "=== $(date -Is) installing $BUNDLE for https://$DOMAIN"

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
# Keep the built environments: a re-sync over them is seconds, not minutes.
for keep in .venv node_modules; do
  if [[ -d "$APP_DIR/$keep" ]]; then mv "$APP_DIR/$keep" "$APP_DIR.new/$keep"; fi
done
systemctl stop osce-marker >/dev/null 2>&1 || true
rm -rf "$APP_DIR.old"
if [[ -d "$APP_DIR" ]]; then mv "$APP_DIR" "$APP_DIR.old"; fi
mv "$APP_DIR.new" "$APP_DIR"
rm -rf "$APP_DIR.old"
chown -R "$SERVICE_USER:$SERVICE_USER" "$APP_DIR"

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
systemctl restart osce-marker

stage "waiting-for-app"
ready=0
for _ in $(seq 1 120); do
  if curl -fsS http://127.0.0.1:8787/api/health/ready 2>/dev/null | grep -q '"ready": *true'; then
    ready=1; break
  fi
  sleep 5
done
if [[ "$ready" != 1 ]]; then
  echo "The app did not report ready within 10 minutes. Last log lines:"
  journalctl -u osce-marker -n 60 --no-pager || true
  false
fi

echo "$BUNDLE" >"$STATE_FILE"
trap - ERR
report "done $GPU_DEVICE"
echo "=== $(date -Is) installed and ready at https://$DOMAIN"
