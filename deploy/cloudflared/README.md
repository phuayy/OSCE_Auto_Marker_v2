# Cloudflare Tunnel for the OSCE AI Marker (Windows host)

Recommended path: a **remotely-managed** tunnel, configured in the Cloudflare
dashboard rather than in a local file. `deploy/cloudflared/config.yml.example`
documents the locally-managed alternative for the rare case a
dashboard-editable config isn't wanted, but the steps below are for the
remote path.

## Setup

1. **Install cloudflared**: `winget install Cloudflare.cloudflared` (as an
   administrator).
2. **Create the tunnel**: Cloudflare dashboard -> Zero Trust -> Networking ->
   Tunnels -> Create a tunnel -> Cloudflared -> name it (e.g. `osce-marker`).
3. **Install it as a Windows service**, as an administrator, using the token
   the dashboard shows on the tunnel's install step:
   ```
   cloudflared.exe service install <token>
   ```
   This registers and starts the `cloudflared` Windows service — the same
   service name `deploy/windows/host.config.json`'s `tunnelServiceName`
   defaults to and that `Deploy-Release.ps1` / `Stop-OsceStack.ps1` stop and
   start around a deploy.
4. **Add a published application route**: in the tunnel's "Public Hostnames"
   tab, add `osce.example.edu` (your real hostname) -> Type `HTTP` -> URL
   `127.0.0.1:8787` (matches `API_HOST`/`API_PORT` in `.env`).

**Treat the install token as a credential.** It authorizes registering (and
re-registering) this tunnel. If it leaks, rotate it from the dashboard's
tunnel settings and reinstall the service with the new token.

## Settings this app specifically needs, and why

| Setting | Value | Why |
|---|---|---|
| Cache Rule for `/api/*` and `/media/*` | Bypass cache | Defense in depth on top of the app's own `Cache-Control: private, no-store` on these paths. Student videos are served behind short-lived `?ticket=` URLs — if the edge ever cached one, a stale ticket's response could be replayed after the ticket expired. |
| Rocket Loader | OFF | Rewrites/defers `<script>` tags. This app ships a strict CSP that admits only `'self'` plus the SHA-256 hash of `index.html`'s inline theme-boot script (`THEME_BOOT_SCRIPT_HASH` in `fastapi_backend/app/core/security_headers.py`) — any script Cloudflare injects or rewrites will not match that hash and the browser will refuse to run it, silently breaking the page. |
| Email Address Obfuscation | OFF | Same class of problem: it rewrites page HTML/JS to obscure `mailto:` links, which can also trip the CSP. |
| Automatic Web Analytics injection | OFF | Injects a `<script>` tag Cloudflare controls, which the CSP has no reason to trust and will block — and this app has no use for it. |
| Any other "Rewrite HTML" style feature | OFF | Same reasoning as the three above: this app's CSP is deliberately strict, and anything that injects or rewrites markup at the edge fights it. |
| Bot Fight Mode | OFF (or excluded for `/api/*`) | Serves JS challenges to suspicious clients. A challenge page in place of a JSON response breaks `fetch`/`EventSource` calls outright (the browser gets HTML where it expected `application/json` or an SSE stream) rather than degrading gracefully. |
| SSL/TLS -> Always Use HTTPS | ON | The app's `ENABLE_HSTS` overlay setting assumes the site is reachable over https end to end; this closes the plain-http path Cloudflare would otherwise still accept. |
| SSL/TLS -> Minimum TLS Version | 1.2 | Baseline modern TLS; nothing in this app needs anything older. |
| Rate limiting rule on `/api/auth/login` (optional, free plan) | e.g. 20 req / 1 min per IP | A second layer on top of the app's own `LOGIN_RATE_LIMIT_MAX_ATTEMPTS` (10/60s) — cheap insurance at the edge before a request even reaches the tunnel. |
| Cloudflare Access in front of the hostname (optional) | Email OTP allowlist | A second authentication layer ahead of this app's own accounts. **Two caveats**: add a bypass policy for `/api/health*` so an external uptime monitor can still reach it without an OTP challenge; and any invitee must already be on the Access allowlist *before* their emailed invite link is sent, or the link itself will be blocked at the edge before the app ever sees the request. |

## Request size and timeout facts that matter here

- **100 MB per request** on Cloudflare's Free/Pro plans. This app sends
  uploads in `UPLOAD_PART_SIZE_MB` parts (8 MB by default — see
  `deploy/windows/production.env.example`), so a multi-gigabyte video upload
  is fine; a single request is never anywhere near the limit. **Never** raise
  `UPLOAD_PART_SIZE_MB` toward 100 — there is no benefit (parts already
  upload in parallel) and it removes the safety margin.
- **~100 second origin response timeout.** Every genuinely long operation in
  this app (transcription, scoring, exports) is a `202` plus a background
  job — no request the tunnel proxies is ever minutes long. The one
  exception, the change-feed stream (`GET /api/events`), is a long-lived SSE
  connection, not a long *response* — `ChangeFeedService`
  (`fastapi_backend/app/services/change_feed_service.py`) sends a keepalive
  comment every 25 seconds specifically so the stream never sits idle long
  enough for any intermediary's timeout (Cloudflare's included) to close it.
- **Quick Tunnels (`trycloudflare.com`) do not support SSE.** Test the change
  feed and any other streaming behaviour against a real named tunnel, not a
  quick tunnel — a quick tunnel will make `/api/events` look broken when the
  app is fine.

## The four `.env` lines this tunnel setup requires

From `deploy/windows/production.env.example`:

```
API_HOST=127.0.0.1
TRUSTED_PROXY_COUNT=1
TRUSTED_PROXY_IPS=127.0.0.1,::1
TRUSTED_CLIENT_IP_HEADER=CF-Connecting-IP
```

- `API_HOST=127.0.0.1` — the API is never reachable except through the
  tunnel; `cloudflared` is the only thing that connects to it.
- `TRUSTED_PROXY_COUNT=1` — exactly one hop of proxying (cloudflared's local
  connection into the API) is trusted; without this, every request would
  share the tunnel's own address for rate-limiting purposes, and ten bad
  login attempts from anyone would lock out everyone.
- `TRUSTED_PROXY_IPS=127.0.0.1,::1` — hop-counting alone can't tell a header
  cloudflared added from one a client forged by reaching the API port
  directly; naming cloudflared's own (loopback) address is what makes that
  distinction possible.
- `TRUSTED_CLIENT_IP_HEADER=CF-Connecting-IP` — Cloudflare's own
  proxy-chain header (`X-Forwarded-For`) reflects Cloudflare's internal
  hops, not necessarily the visitor; `CF-Connecting-IP` is the header
  Cloudflare sets to the actual client address, so the app resolves rate
  limits and audit fields against the real visitor rather than an edge node.
