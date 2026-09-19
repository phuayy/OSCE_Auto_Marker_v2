# Deploying on a single VM (local storage)

One machine runs the API, the job queue, the GPU steps and — optionally — the
built frontend. Other devices open it in a browser and upload to it over the
network. This is the deployment the default settings are built for; the only
thing that stops it working out of the box is that those defaults bind
loopback, on purpose.

## What already works

Uploads never assume the browser is on the server. With `STORAGE_BACKEND=local`
the browser sends the video in parts to `PUT /api/uploads/{id}/parts/{n}`, the
API writes them under `storage/objects/.uploads/`, assembles them, verifies the
SHA-256 and hands the file to the pipeline. The client never names a path or a
URL; the server owns the disk. Every URL the frontend uses is relative
(`/api/...`, `/media/...`), resolved against whatever origin the page was
loaded from, and nothing in the upload path needs a secure context, so plain
`http://<vm-address>` from a phone or a laptop on the same network works.

What does not, until configured: the API listens on `127.0.0.1` and nothing
serves the built frontend.

## Steps

1. **Install** — as in [LOCAL_SETUP.md](../LOCAL_SETUP.md): `uv sync`
   (`uv sync --group canary` for the optional engine), `npm ci`, ffmpeg on PATH
   or in `FFMPEG_BIN`.

2. **Build the frontend once**: `npm run build` → `dist/`. Rebuild after every
   pull; the API serves whatever is there, no restart needed.

3. **`.env`** (copy `.env.example`). The lines that make it reachable:

   ```dotenv
   API_HOST=0.0.0.0            # every interface; or one address, e.g. 10.0.0.12
   API_PORT=8787
   SERVE_FRONTEND=true         # the API serves dist/ at /
   APP_PUBLIC_URL=http://10.0.0.12:8787     # what the browser loads; used in emailed links
   CORS_ALLOW_ORIGINS=http://10.0.0.12:8787 # same origin as above; clears the '*' warning
   STORAGE_ROOT=/var/lib/osce-marker        # data off the checkout (optional)
   ```

   The rest — credentials, the LLM keys, `AUTH_SECRET`,
   `CREDENTIAL_ENCRYPTION_KEY` — as for any install. `MAX_VIDEO_UPLOAD_MB`
   bounds a single upload; `UPLOAD_PART_SIZE_MB` is the size of each part the
   browser sends and matters again in step 6.

4. **Open the port** on the VM's firewall (`ufw allow 8787/tcp`; on Windows
   `netsh advfirewall firewall add rule name="OSCE AI Marker" dir=in action=allow protocol=TCP localport=8787`)
   and in the cloud provider's security group.

5. **Run it**: `uv run --no-sync python scripts/run_api.py` — or as a service:
   [deploy/systemd/osce-marker.service.example](../deploy/systemd/osce-marker.service.example).
   The startup log lists configuration warnings; a missing `dist/` is one of
   them.

   Check from another device: `curl http://10.0.0.12:8787/api/health` answers
   `{"ok": true}`, and `http://10.0.0.12:8787/` in a browser shows the login
   page.

6. **Optional: a reverse proxy** for TLS or a hostname —
   [deploy/nginx/osce-marker.conf.example](../deploy/nginx/osce-marker.conf.example).
   Three of its settings are load-bearing for this app and wrong by default in
   every proxy:

   | Setting | Why |
   |---|---|
   | request body limit ≥ `UPLOAD_PART_SIZE_MB` (`client_max_body_size 16m`) | nginx's default is 1 MB; every 8 MB part is refused before the API sees it |
   | no buffering and a long read timeout on `/api/events` | the change feed is an open SSE stream; buffering delays every event, the default 60 s timeout cuts it |
   | `TRUSTED_PROXY_COUNT=1` in `.env` | behind a proxy the socket peer is the proxy, so every login shares one rate-limit bucket until `X-Forwarded-For` is trusted to that depth |
   | `TRUSTED_PROXY_IPS=127.0.0.1` in `.env` | hop-counting alone cannot tell a header nginx added from one a client forged by reaching the API port directly; this names nginx's own address so only *its* `X-Forwarded-For` is trusted |

   With a proxy in front, put the API back on `API_HOST=127.0.0.1` — nothing
   else needs to reach it — and set `APP_PUBLIC_URL` / `CORS_ALLOW_ORIGINS` to
   the proxied origin.

## Resource admission and URL credentials

`RATE_LIMIT_RERUN_PER_HOUR=10` sets one shared hourly attempt budget per signed-in
account for session rerun/process/auto-crop, clip assessment/export/recrop, job
rerun, and upload initiation. Administrators have the same budget; changing IP,
session, or endpoint does not reset it. Ownership is checked before charging
session/job operations. Refused attempts return HTTP 429 through the normal API
error path; mutating requests are not automatically retried by the browser.
The unchanged in-memory limiter resets on API restart and requires the existing
single-API-process deployment restriction. It is not a distributed billing quota.

`MAX_CONCURRENT_UPLOADS_PER_USER=3` independently caps active uploads per account.
Admission counts persisted `uploads.status` under the same transaction that
creates the upload, session, and waiting job. PostgreSQL serializes admission on
the account row; SQLite uses `BEGIN IMMEDIATE`. Unexpired `initiated`/`uploading`/
`failed` records and all `assembling` records consume capacity. Failed uploads
retain their reservation because their parts can be retried through `/complete`;
abort them to release it immediately. Committed, aborted, and expired uploads
release capacity. Expired transfers do not block a new transfer, but expired
assembly still does until it finishes or recovery marks it failed. Values below one for
either knob are clamped to one, not interpreted as disabling protection.

These controls bound admission, not exact GPU time or money: jobs differ in cost,
clip exports may contain multiple clips, and committed uploads release their
transfer slots before processing finishes. Existing queue concurrency and file
size limits still apply. Adjust the hourly budget for legitimate cohort marking.

**The hourly budget is per account, not a shared pool — plan capacity from
`GPU_SLOTS` instead.** Every signed-in marker gets their own `RATE_LIMIT_RERUN_PER_HOUR`
budget, so N markers marking at once can admit up to N times that many expensive
operations in the same hour; nothing here caps the *total* rate across accounts.
That is by design — it is an abuse guard, not a capacity plan. Actual throughput
is bounded elsewhere and shared by everyone regardless of how many accounts are
admitting work: `GPU_SLOTS` (default 1) serialises the one step that needs the
accelerator (transcription, person detection) and `JOB_WORKER_CONCURRENCY`
bounds jobs in flight. A larger cohort does not overrun the machine — GPU-bound
jobs still queue for the lease — but it does mean queue latency grows roughly
with the number of markers running sessions at the same time, not just with
upload volume. `GET /api/admin/health/diagnostics` (admin-only) reports the
GPU lease's current `inUse`/`waiting` counts, which is the number to watch if
sessions feel slow to start under load.

Bearer tokens are accepted only in the `Authorization` header, never through
`?token=` (including media and SSE). The frontend never downgrades a failed
stream-ticket request to a bearer URL; media reports unavailable and live updates
use their normal reconnect path. Rebuild the frontend when deploying this change.

Keep the nginx `log_format` unchanged. Application-generated query strings are
**free of long-lived bearer tokens, not credential-free**: `/media/*` and SSE
still use short-lived `?ticket=` credentials. Access logs containing those URLs
remain sensitive until ticket expiry; restrict log access and retention. Emailed
action-token API paths also remain sensitive. TLS is required on shared networks.

## Two things to know

**Plain HTTP is fine for uploads, not for secrets.** The bearer token and the
provider API keys entered in Settings travel over whatever the page is served
on. On a shared network put TLS in front (step 6). The one browser feature
that needs HTTPS is the copy-to-clipboard button on the webhooks page.

**Local storage is one filesystem.** The API, the job queue and every
subprocess read the same `STORAGE_ROOT`, which is what makes a single VM
simple. It is also the limit: a Hatchet worker on a *second* machine never
sees the uploaded parts. That deployment is what `STORAGE_BACKEND=gcs` is for
(see `.env.example`), not a bigger disk.
