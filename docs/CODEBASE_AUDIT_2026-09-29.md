# Codebase audit — 29 September 2026

Reviewed checkout: `eb8c521`. Application code was not modified. This is a risk-focused engineering audit, not a claim that every execution path is defect-free.

## Method and scope

CodeGraph MCP was not callable in this session. Its existing `.codegraph/codegraph.db` was queried read-only as a fallback: 514 indexed files, 9,758 symbols and 11,330 call edges. SHA-256 comparison found 503 indexed files unchanged and 11 changed; current source, rather than indexed line numbers, was authoritative. The graph does not cover the entire current deployment surface.

Reviewed request authentication, authorization and ownership boundaries; uploads and storage; job/session lifecycle; provider credential handoff; outbound URL guards; frontend authentication, request retries and resumable uploads; deployment and CI. Five isolated probes used fake credentials and mocked I/O, without calling production services or altering application data. See `.codegraph/audit_probes.py` and `.codegraph/audit-probes.log`.

## Findings, ordered by priority

### 1. High — Custom provider IDs can cause one vendor's key to be sent to another vendor

Evidence: `fastapi_backend/app/llm/custom.py:112` and `fastapi_backend/app/llm/credentials.py:108`; production caller: `fastapi_backend/app/services/llm_settings_service.py:326`.

Allowed IDs such as `clinic-a` and `clinic_a` both normalize to `OSCE_LLM_KEY_CLINIC_A`. Neither provider creation nor catalog construction rejects this collision. When both targets occur in a scoring subprocess's routing, the later credential overwrites the earlier one. Each provider subsequently resolves the same key while retaining its own endpoint.

The isolated probe built two accepted provider definitions with fake keys A and B. Both resolved to B after `credential_env_for`. This can disclose a vendor credential to a different configured vendor and cause authentication failures. It requires configured colliding custom providers; it is not an unauthenticated arbitrary-provider creation vulnerability.

Fix: use a collision-free encoding or stable digest of the complete provider ID in environment names, with a migration strategy for existing environment configuration. Reject collisions at catalog construction as defense in depth. Add a round-trip test with multiple providers and fallback targets.

### 2. High — Chunked request bodies bypass the application memory limit

Evidence: `fastapi_backend/app/core/body_limit.py:62`; `fastapi_backend/app/api/routes/async_uploads.py:43`.

The general middleware checks only declared Content-Length. Missing Content-Length permits unbounded body consumption. Upload parts are exempt from this middleware, and their handler calls `request.body()` before the authoritative byte-length check in storage. A chunked body therefore reaches buffering without a streaming cap. Login is public and its JSON body is parsed before the login function applies rate limiting.

The isolated ASGI probe passed 2,048 bytes through a middleware configured for 1,024 bytes with a chunked request. No excessive-memory test was performed. Repeated large requests can exhaust API memory unless a separately configured ingress enforces a suitable cap.

Fix: count received bytes in an ASGI receive wrapper and stop consumption at the limit. Use the larger part-size limit only on the upload-part route. Verify chunked requests, missing lengths, and requests that exceed the limit across multiple frames.

### 3. High for GCS deployments — Successfully uploaded objects cannot be finalized

Evidence: `fastapi_backend/app/services/async_upload_service.py:301`; `fastapi_backend/app/storage/gcs.py:187`; `src/lib/resumableUpload.js:16`.

GCS receives file bytes directly through a resumable URL. The application initializes `parts` to an empty list, and GCS explicitly refuses the local part endpoint. Nevertheless, `_complete_locked` unconditionally requires nonempty local part metadata and matching accumulated sizes before invoking the storage-specific completion routine.

The isolated probe using a GCS upload record returned HTTP 400, `video upload has no parts.` This happens before checking the uploaded bucket object, even if that object exists and is complete. No live GCS credentials were used.

Fix: make pre-completion validation storage-specific. Local multipart uploads should validate recorded parts; GCS should validate object existence, size, generation and checksum through its completion implementation. Test the complete initiate → direct upload → finalize flow, not just the storage adapter in isolation.

### 4. High for GCS deployments — Retention and session deletion leave source objects in the bucket

Evidence: `fastapi_backend/app/services/session_maintenance_service.py:193` and `:400`; `fastapi_backend/app/storage/gcs.py:241`.

Retention unlinks the materialized local video and sets `purgedAt`, but does not delete the GCS object. Session deletion likewise cleans local artifacts rather than committed bucket objects. The GCS adapter has blob deletion for aborted uploads, but the session lifecycle does not use it for committed sources. The retention code itself acknowledges this limitation.

Consequently, the application can report a video purged or a session deleted while the original recording remains in cloud storage. An independently configured bucket lifecycle could mitigate this, but none was verified in this audit. This is a data-lifecycle correctness issue; no legal compliance conclusion is implied.

Fix: add storage-aware, generation-specific deletion of owned source objects, with durable retries. Record purge completion only after confirmed deletion. Preserve shared rubric assets and shared parent/child clips using explicit ownership/reference rules.

### 5. Medium — Failed database deletion is reported as successful deletion

Evidence: `fastapi_backend/app/services/session_maintenance_service.py:104`, `:108`, `:92`, and `:458`.

Every database teardown step, including deletion of the session itself and cancellation/purge of its jobs, is wrapped in `_safe`, which logs and suppresses exceptions. The caller still returns the ID in `deletedSessionIds` and proceeds with artifact removal. A database failure can leave a visible session whose files have been deleted, or leave work running after the UI was told deletion succeeded.

The isolated probe made the session repository raise a simulated database error. The service still returned `{"deletedSessionIds": ["audit"]}`.

Fix: distinguish required database operations from optional cleanup. Persist a deleting state and retry cleanup durably; return an incomplete/failed operation when required work fails. Do not destroy artifacts after an unconfirmed job stop or session transition.

### 6. Medium — Queue insertion failure leaves a session stuck as queued

Evidence: `fastapi_backend/app/services/session_maintenance_service.py:312`; recovery boundary: `fastapi_backend/app/services/job_queue_service.py:139`.

`_queue_job` commits the session's queued state before calling `jobs.enqueue`. If enqueue fails before creating a job, there is no compensating transition. Subsequent start/rerun requests reject the session as already in flight, although no worker has a job to claim.

The isolated probe injected an enqueue exception and observed `status=queued` with no job. Startup reconciliation can mark such orphaned sessions failed, so this is recoverable through restart; it is not automatically repaired by the failed request.

Fix: reserve the job and session transition atomically where possible, or use an outbox/reconciliation design. At minimum, handle failed admission with a concurrency-safe transition to a retryable failed state. Also serialize rerun admission before destructive artifact cleanup.

### 7. High under deployment failure — Automatic restore assumes tunnel closure without checking it

Evidence: `deploy/windows/Deploy-Release.ps1:394`, `:395`, `:122`, and `:570`.

Tunnel shutdown suppresses Stop-Service errors and discards the boolean result of `Wait-OsceServiceStatus`. Unlike the worker/API stop helper, it never throws when the tunnel remains running. Rollback then reasons that `$tunnelReopened == false` means no external writes were possible, and can automatically restore the pre-migration database.

If tunnel shutdown fails and the API is restarted during deployment, clients may reach it before the script explicitly reopens the tunnel. A subsequent failure in that interval can take the automatic restore branch and discard those writes. This is a source-traced failure scenario, not a live service-stop experiment.

Fix: require confirmed tunnel shutdown and abort safely if it fails. Apply the same rule inside rollback. Track verified ingress isolation rather than merely whether Start-Service has been called. Test false timeout results and service-stop exceptions.

## Additional boundaries and follow-up work

- The deployment drain check occurs while the public tunnel still accepts new work (`Deploy-Release.ps1:343`, `:394`). Introduce an admission gate before draining, then recheck before stopping workers; otherwise new work can arrive after the successful snapshot.
- Webhook DNS validation and connection use separate DNS resolutions. Custom provider DNS checks occur on save. These are incomplete SSRF defenses against a changing hostname; webhook/provider administration is admin-only, which limits exposure. Pin validated addresses in the transport or enforce egress restrictions where administrators should not have arbitrary network reach.
- Authenticated markers intentionally share read access to all sessions, transcripts and media; ownership limits mutations. This is an explicit product policy, not an accidental missing read-owner check. It needs redesign if the product will host unrelated institutions or isolated marker groups.
- Logout revokes the bearer token but previously minted stream tickets retain independent IDs and can remain valid until their expiry (default 600 seconds). Confirm that this matches the required logout semantics for media.
- The tests inspected have substantial unit coverage, but browser behavior, real GCS lifecycle, live PostgreSQL/Hatchet failures, GPU processing and scoring validity need integration/runtime validation. Passing tests do not establish the absence of the defects reproduced above.
- No external dependency advisory lookup or live penetration test was performed. No claim is made that the dependency versions are vulnerability-free.

## Validation

- Frontend unit tests: 409 passed.
- Frontend ESLint: passed.
- Frontend production build: passed.
- Backend Ruff (`fastapi_backend/app scripts`): passed.
- Five isolated defect probes: reproduced the body-limit bypass, credential collision, GCS finalization rejection, false deletion success, and orphan queued state.
- Backend test suite: 1,618 passed, 10 skipped, 6 warnings in 208.55 seconds on the authorized rerun. Warning details were suppressed for this run and were not individually assessed. The initial sandboxed run encountered 882 temporary-directory permission errors and is not a valid application failure assessment.

## Suggested repair order

First fix credential isolation and streaming body limits. If GCS is in use, treat finalization and committed-object deletion as release blockers. Then make deployment isolation fail closed and make session deletion/job admission report and recover from partial failures. Add integration tests around these boundaries before expanding feature work.
