# On-demand hosting on Google Cloud

The whole marker runs on **one GPU VM** that is off most of the time. A tiny
**start gate** on Cloud Run is the address people bookmark:

1. They open the gate. It says whether the marker is running.
2. If it is off, they sign in with Google and press **Start the marker**.
3. The page shows each stage of the start (reserving the GPU machine,
   starting the operating system, preparing the database, loading) with the
   time elapsed and an estimate of what is left.
4. When the marker answers, the page sends them to its normal login.
5. Once nobody has used the marker for `IDLE_SHUTDOWN_MINUTES` and no job is
   queued or running, the VM switches itself off. A stopped VM bills only its
   disk; all data stays on it.

```
Browser ──► Cloud Run: start gate (min instances 0)
               │  Compute API: get / start / guest attributes
               ▼
            GCE VM with GPU: the whole app (API + frontend + local queue + Postgres or SQLite)
               │  writes its boot stage to the guest attribute osce/boot
               └─ switches itself off when idle
```

How long a start takes depends on the machine and the image. Expect a few
minutes. The gate measures every start (from the VM's start time to the app
reporting ready) and uses the median of the recent ones as its estimate.

## What counts as "in use"

Implemented in `fastapi_backend/app/services/idle_shutdown_service.py` and
`app/core/activity.py`.

- **A signed-in person acting**: any authenticated request, except the ones
  an open tab makes on its own (the change stream, the notification poll,
  stream tickets, health probes). A forgotten browser tab does not keep the
  GPU running overnight.
- **Work in flight**: a job queued or running, or an upload assembling. This
  also restarts the window, so the marker stays up for a full
  `IDLE_SHUTDOWN_MINUTES` after the last job finishes.
- An upload in progress sends a request per part, so it counts as activity
  too.

Admins can see the countdown in `GET /api/admin/health/diagnostics` under
`idleShutdown`.

## 1. The VM

Create a GPU VM (for example `g2-standard-8` with one L4) from an image with
the NVIDIA driver installed. Then:

- **Enable guest attributes** so the app can publish its boot stage:
  `gcloud compute instances add-metadata osce --zone ZONE --metadata enable-guest-attributes=TRUE`
- **Reserve a static external IP** and point a DNS name at it, so the gate's
  link never changes. Serve the app over HTTPS, either with the
  nginx example in `deploy/nginx/` plus a certificate, or with the Cloudflare
  tunnel in `deploy/cloudflared/`.
- Install the app as in `docs/deployment-vm.md` and run it as a service
  (`deploy/systemd/osce-marker.service.example`), so it starts on every boot.
  Bake the WhisperX weights into the disk by running one transcription before
  you rely on the gate, so a start never waits for a multi-GB download.

Add to the app's `.env`:

```
SERVE_FRONTEND=true
IDLE_SHUTDOWN_MINUTES=30
BOOT_STAGE_REPORTING=true
# default; shown for clarity
IDLE_SHUTDOWN_COMMAND=sudo -n /usr/sbin/shutdown -h now
```

Let the service user run that one command without a password
(`sudo visudo -f /etc/sudoers.d/osce-shutdown`):

```
osce ALL=(root) NOPASSWD: /usr/sbin/shutdown -h now
```

A shutdown from inside the guest leaves the instance **stopped**
(`TERMINATED`), exactly as if it were stopped from the console.

## 2. The gate

Everything is in `deploy/gcp/gate/`.

**Google sign-in.** In *APIs & Services → Credentials*, create an OAuth client
ID of type *Web application*. Add the gate's URL (after the first deploy) as an
*Authorized JavaScript origin*. Use the client ID as `GATE_GOOGLE_CLIENT_ID`.

**A service account that can only start this VM:**

```bash
gcloud iam roles create osceGate --project PROJECT \
  --permissions=compute.instances.get,compute.instances.start,compute.instances.getGuestAttributes,compute.zoneOperations.get,compute.instances.setMachineType,compute.instances.setMachineResources
gcloud iam service-accounts create osce-gate --project PROJECT
gcloud compute instances add-iam-policy-binding osce --zone ZONE \
  --member=serviceAccount:osce-gate@PROJECT.iam.gserviceaccount.com \
  --role=projects/PROJECT/roles/osceGate
```

`compute.zoneOperations.get` is project-level, so also grant the role on the
project, or grant `roles/compute.viewer` there if you prefer a predefined role
for the read side.

**Deploy:**

```bash
cd deploy/gcp/gate
gcloud run deploy osce-gate --source . --region REGION \
  --service-account osce-gate@PROJECT.iam.gserviceaccount.com \
  --allow-unauthenticated --min-instances 0 --max-instances 2 \
  --set-env-vars GATE_PROJECT=PROJECT,GATE_ZONE=ZONE,GATE_INSTANCE=osce \
  --set-env-vars GATE_APP_URL=https://marker.example.org \
  --set-env-vars GATE_GOOGLE_CLIENT_ID=xxxx.apps.googleusercontent.com \
  --set-env-vars GATE_ALLOWED_DOMAINS=school.edu \
  --set-env-vars GATE_ALLOWED_EMAILS=someone@gmail.com \
  --set-secrets GATE_SESSION_SECRET=osce-gate-session:latest
```

`--allow-unauthenticated` is right here: the page must load for people
who are not signed in yet. The gate itself checks the allowlist before it
starts anything.

| Variable | Required | Meaning |
|---|---|---|
| `GATE_PROJECT` / `GATE_ZONE` / `GATE_INSTANCE` | yes | The VM to start |
| `GATE_APP_URL` | yes | Where people are sent once it is ready (the app's HTTPS address) |
| `GATE_PROBE_URL` | no | Where the gate checks `/api/health/ready`; defaults to `GATE_APP_URL` |
| `GATE_GOOGLE_CLIENT_ID` | yes | OAuth Web client ID for Google sign-in |
| `GATE_ALLOWED_EMAILS` / `GATE_ALLOWED_DOMAINS` | one of them | Who may start it. Both empty means nobody can |
| `GATE_SESSION_SECRET` | yes | 32+ random characters, signs the gate's cookie. Keep it in Secret Manager |
| `GATE_HISTORY_BUCKET` | no | GCS bucket for start durations, so the estimate survives the gate scaling to zero. The service account needs object read/write on it |
| `GATE_EXPECTED_START_SECONDS` | no | Estimate before any start has been measured (default 240) |
| `GATE_APP_NAME` | no | Title on the page |
| `GATE_MACHINE_PROFILES` | no | GPU machines to try, in order (see **GPU fallback** below). Empty: start the VM as it is |

The gate's tests run with the app's suite:
`cd fastapi_backend && uv run --no-sync pytest tests/test_start_gate.py tests/test_idle_shutdown.py`.

## GPU fallback: L4 first, T4 when no L4 is free

A zone runs out of a GPU model from time to time, and the start then fails
with `ZONE_RESOURCE_POOL_EXHAUSTED`. Set

```
GATE_MACHINE_PROFILES=g2-standard-8, n1-standard-8+nvidia-tesla-t4
```

and every start does this (`gate/osce_gate/hardware.py`):

1. Switch the stopped VM to the first profile if it is not already on it
   (`g2-standard-8`, which carries one L4), and start it.
2. If Google Cloud answers "no GPU free" (or "GPU quota used up"), switch the
   still-stopped VM to the next profile (`n1-standard-8` + one T4) and start
   that instead.
3. If none is free, the page says which GPUs it tried and offers *Try again*.

Each start begins at the top of the list again, so a VM that fell back to a T4
yesterday goes back to an L4 today. Any other start failure is reported as it
is, without trying the next profile. The page shows which GPU the marker is
running on, and says so when it is the fallback. Switching takes a few seconds
per step, so a fallback start costs about a minute more than a normal one.

Only the machine type and GPU change. The disk, its data, the static IP and the
DNS name stay the same, because all of them stay in the same zone. Profiles in
another zone are not supported: a disk belongs to one zone, so moving the VM
means snapshotting and recreating the disk, which is a migration, not a start.

The same logic from a terminal, instead of `gcloud compute instances start`:

```bash
cd deploy/gcp/gate
uv run --no-project --with google-auth --with requests \
  python -m osce_gate.cli --project PROJECT --zone asia-southeast1-b --instance osce-marker
```

### One-time setup for the fallback

1. **Disk type.** N1 machines cannot boot from Hyperdisk. Check that the boot
   disk is `pd-balanced` or `pd-ssd`:
   `gcloud compute disks describe osce-marker --zone ZONE --format='value(type)'`.
2. **T4 quota.** In *IAM & Admin → Quotas*, check *NVIDIA T4 GPUs* for the
   region is at least 1 (and *GPUs (all regions)* at least 1). New projects
   often have 0.
3. **Gate permissions.** Add `compute.instances.setMachineType` and
   `compute.instances.setMachineResources` to the gate's role:
   `gcloud iam roles update osceGate --project PROJECT --add-permissions=compute.instances.setMachineType,compute.instances.setMachineResources`
4. **Rehearse the T4 once** while the VM is stopped, so a problem shows up
   now rather than on a busy morning: run the CLI above with
   `--profiles "n1-standard-8+nvidia-tesla-t4"`, check that the marker
   transcribes a short video, stop the VM, then run the CLI again without
   `--profiles` to go back to the L4.
5. **Deploy the gate** again with `GATE_MACHINE_PROFILES` added to its
   `--set-env-vars`.

### Running on a T4

The app needs no change. A T4 has 16 GB of video memory (an L4 has 24 GB),
and one job at a time holds the GPU (`GPU_SLOTS=1`):

| Step | Model | Video memory | On a T4 |
|---|---|---|---|
| Transcription | WhisperX `large-v3`, `float16` | about 4-5 GB with alignment and diarisation | fits; about 1.5-2x slower than on an L4 |
| Person detection | RT-DETRv2 r18 | about 1.5 GB | fits |
| Canary-Qwen (optional engine) | 2.5B parameters | about 5-6 GB in 16-bit | fits, but set its precision to `float16` in Settings: the T4 has no native bfloat16 (the default), so `auto` is slow or fails |

The CUDA stack carries over: the same NVIDIA driver supports both cards, and
the PyTorch cu128 wheels and CTranslate2 (WhisperX) include the T4's compute
capability 7.5. `n1-standard-8` has 30 GB of RAM against 32 GB on
`g2-standard-8`, which is ample. Keep `WHISPERX_COMPUTE_TYPE=float16`; if you
raised `WHISPERX_BATCH_SIZE` for the L4, 8 or below is safe on a T4.

## Limits worth knowing

- **GPU capacity.** A zone can run out of GPUs. The start then fails with
  `ZONE_RESOURCE_POOL_EXHAUSTED`, and the page says so in plain words and
  offers *Try again*. `GATE_MACHINE_PROFILES` (above) falls back to another
  GPU in the same zone first. If both run out often, use a reservation, or
  move to a zone with more capacity.
- **Two accounts.** People sign in twice: once with Google at the gate (it
  costs money to start the machine), then with their marker account in the
  app. The app's accounts live on the VM, which is off when the gate needs
  to decide.
- **Nobody is warned before shutdown.** The window only closes after
  `IDLE_SHUTDOWN_MINUTES` with no action from anyone, so a person who comes
  back after that finds the page unreachable and starts it again from the
  gate.
