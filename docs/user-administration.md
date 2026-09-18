# User administration: administrators and invited markers

The deployment used to have one account — a username and a bcrypt hash in
`storage/auth/credentials.json` — and a bearer token was a signed username.
That was right for a single operator. It is wrong the moment the app is shared:
there was no way to add a second person without handing over the one password,
and no way to take access away again.

This document is the reasoning behind the account system that replaced it. The
mechanics are in the code and in `CLAUDE.md`; what is here is *why* it has the
shape it has, and what was rejected.

## What was wanted

* Two roles. **Admin**: everything, plus account management. **Marker**:
  everything the app does — upload, assess, split, settings, analytics — but
  no account management.
* A first administrator that exists on first boot, and an upgraded deployment
  that keeps the credentials it already had.
* Markers added by **email address**: the marker proves control of the mailbox
  by following an emailed link and choosing their own password. The
  administrator never sees or sets it.
* An administrators-only screen to invite, re-invite, disable, re-enable,
  change the role of, send a password reset to, and delete accounts.
* Self-service on the account holder's side: change your own password, and
  recover a forgotten one by email.

## What the market does, and what was adopted

**No authentication framework.** `fastapi-users` is the only widely used
batteries-included option for FastAPI; it is in maintenance mode, its model is
open self-registration rather than admin-only invitation, and it would have
replaced an auth layer this project already owned (HMAC tokens, bcrypt, stream
tickets, revocation, rate limiting). A hosted identity provider is overkill for
a two-role, one-institution tool and puts a third party in front of every
sign-in. The existing `AuthService` was extended instead.

**Invitation and reset links follow the OWASP guidance for reset tokens**, which
the magic-link literature agrees with:

| Rule | Where it lives |
|---|---|
| Token from a CSPRNG, long enough to be unguessable (256 bits) | `core/security.new_action_token` |
| Store only a hash; look up by hash | `user_action_tokens.token_hash` (SHA-256 — a fast hash is right for a high-entropy token) |
| Single use, enforced atomically | `UserRepository.consume_token`: `UPDATE … WHERE used_at IS NULL`, row count decides |
| Expiry: days for an invitation, minutes for a reset | `INVITE_TOKEN_TTL_HOURS` = 72, `PASSWORD_RESET_TOKEN_TTL_MINUTES` = 30 |
| A resend voids the earlier link | `UserRepository.issue_token` stamps every live token of that purpose |
| Judged against the account too, not only its own expiry | `UserAdminService._token_problem` — an invitation cannot activate an account that was disabled meanwhile |
| Uniform responses on anything an outsider can call | `/password-reset/request` answers identically for known and unknown identifiers; login costs one bcrypt verify either way (a dummy hash at the configured cost factor) |
| Per-IP rate limit on the public link endpoints | `token_rate_limiter`, separate from the login throttle |
| After a password is set: end every other session, notify by email, sign in normally | `token_version` bump, `templates.password_changed`, no session issued by the accept/confirm endpoints |

**Role-based access is a router-level dependency**, the usual FastAPI shape, with
one refinement the plain pattern lacks: the role and the account's status are
read from the **row** on every request, not from the token. See the next section.

**Email is `aiosmtplib` behind a Protocol.** `smtplib` is synchronous and would
block the event loop; `fastapi-mail` wraps `aiosmtplib` and Jinja in five
dependencies for two paragraphs of text. The transport sits behind
`EmailSender` exactly as object storage sits behind `ObjectStorage`, with a
`console` backend for a laptop and an HTTP-API vendor as an obvious future
backend on the existing `httpx`.

## The decisions that shape the code

### A bearer token names an account and a version

```
before: {username, issuedAt, expiresAt, tokenId}
after:  {sub, username, role, tokenVersion, issuedAt, expiresAt, tokenId}
```

`AuthService.verify_token` checks the signature and expiry as before, then
resolves `sub` against the accounts table: still present, still `active`, and
`tokenVersion` still equal to the row's `token_version`. Disabling an account,
resetting or changing its password bumps that number, so every token it holds
dies on its next request — no shared revocation store, no waiting for the
eight-hour expiry. The role returned to the request is the row's, so a
promotion or demotion applies without a new sign-in.

That row check would put a query in front of every API call, so it goes
through `UserDirectory`: one `SnapshotCache` over the small, rarely written
`users` table, evicted by the change feed exactly as the settings and
credential caches are. Warm on PostgreSQL that is zero queries per request; on
SQLite it is one `table_versions` read. The `users` table is in
`TRACKED_TABLES` and revision `0008` installs its trigger, so an
administrator's "disable" reaches every API process on the marker's next
request.

Tokens issued before this change carry no `sub` and are refused; everyone
signs in once after the upgrade.

An explicit logout (`POST /api/auth/logout`) is a narrower case than a
version bump: it revokes one token, not every token the account holds, so it
cannot ride `token_version`. `revoked_tokens` records that one token's id the
same way — a `SnapshotCache` over a change-tracked table — so a logout in one
process is honoured by every other process (and survives a restart) rather
than only the process that handled the request remembering it.

### Two guards, in a deliberate order

* **The last active administrator cannot be demoted, disabled or deleted** —
  the next action would need an administrator and there would be none.
* **Nobody changes their own access** — an administrator cannot disable,
  delete or demote themselves; a slip of the mouse would lock them out
  mid-session. Their own password is the one thing they may change.

The last-admin guard runs first. A sole administrator trying to demote
themselves should hear "promote another account first", which is actionable,
rather than "ask another administrator" when there is none. With a second
administrator present the same click hits the self-guard, and the second
administrator can act on the first.

### Delivery is best-effort at the request, never a rollback

An invitation whose email bounced is still an invited account. The response
says `mailSent: false` with the relay's own wording, and the screen offers
Resend and — when this deployment has no relay — Copy link. Rolling the account
back would turn a mail-server hiccup into a lost onboarding.

### The copy-link fallback

With `EMAIL_BACKEND=console` nothing can deliver an email, so the invitation
link is the only way an invitation reaches anyone. The Users screen is then
handed the link to copy (`inviteLink` in the response; the same for a reset,
`resetLink`). The administrator is trusted with every other account action
already, and the link still works once and expires. Once a relay is configured
the link is never returned; `INVITE_LINK_VISIBLE_TO_ADMIN` overrides either way.

### The link rides in the URL fragment

`https://…/#/accept-invite/<token>`. A browser never sends the fragment to the
server, so the raw token appears in no access log on the way in. The hash
router already existed for deep links; the three pre-login routes reuse it.

### The first administrator is seeded at startup, not by the migration

Seeding needs either the legacy `credentials.json` or `DEFAULT_ADMIN_PASSWORD`,
neither of which a migration should read. `UserAdminService.ensure_bootstrap_admin`
runs in the API role only: with an empty table it migrates the legacy file
(keeping its username and hash, so the password an operator already knows keeps
working), else creates the account from the environment. Once any account
exists it does nothing — an administrator who deleted the bootstrap account on
purpose must not find it back after a restart.

### Passwords: length over complexity

NIST SP 800-63B: a minimum length (10) and no character-class theatre. Two extra
rules: at most 72 bytes, because bcrypt silently ignores the rest and would
verify against a prefix; and not equal to the account's own username or email
address, the first thing anyone guesses. The policy is checked *before* an
invitation token is consumed, so a weak first attempt does not burn the link.

## What is deliberately not here

* **Per-session ownership.** Every marker sees every session, as asked. Each
  session does record its creator (`createdBy`, a snapshot of the account at
  upload time, mirrored into the indexed `sessions.created_by`) — attribution
  for the card and the workspace, not a permission.
* **Self-registration.** There is no sign-up form. Accounts exist because an
  administrator created them.

## Sources consulted

* OWASP Forgot Password Cheat Sheet — https://cheatsheetseries.owasp.org/cheatsheets/Forgot_Password_Cheat_Sheet.html
* OWASP Authentication Cheat Sheet — https://cheatsheetseries.owasp.org/cheatsheets/Authentication_Cheat_Sheet.html
* NIST SP 800-63B, memorized secrets — https://pages.nist.gov/800-63-3/sp800-63b.html
* Magic-link guidance — https://mojoauth.com/blog/are-magic-links-secure-technical-deep-dive , https://supertokens.com/blog/magiclinks
* FastAPI role-based access pattern — https://developer.auth0.com/resources/code-samples/api/fastapi/basic-role-based-access-control
* `fastapi-users` status — https://github.com/fastapi-users/fastapi-users
