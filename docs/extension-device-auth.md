# Independent extension device authorization

Server routes are implemented in `api/extension_auth.py`. Python deployment requires an operator-authorized WebUI restart. This document does not assert that a running server has been restarted.

## Pairing and credentials

- `POST /api/auth/extension/start`: extension Origin exactly `chrome-extension://<32 a-p characters>`, no cookie or authorization header. Body `{extension_id,client_name,code_challenge,code_challenge_method:"S256",scopes}`. `controls` is an alias for `control`; default scope is `chat`.
- Response: `{device_code,user_code,verification_uri:"/extension-pair",expires_in:600,interval:3}`. Only the user code goes in `/extension-pair#user_code=...`; device secrets never go in URLs.
- Cookie/CSRF-authenticated review: `POST inspect {user_code}`, `POST approve {user_code,profile,scopes,confirm:true}` or `POST deny {user_code}`, all beneath `/api/auth/extension/`. Anonymous/local auth-disabled mode cannot approve devices. The consent page names the device identity, profile and capabilities.
- `POST /api/auth/extension/token`: same extension Origin, no cookies; `{device_code,code_verifier,extension_id?}`. S256 verifier and optional extension ID must match. Pending: 400 `authorization_pending`; too-fast polling: 429 `slow_down`.
- Successful one-time exchange returns `{access_token,device_id,expires_at,scopes,profile,profiles}`. Epoch timestamps are seconds. Grants last 30 days.
- Bearer `GET /api/auth/extension/me`: non-secret metadata, scopes, profile and profiles. Cookie `GET devices` lists permitted device metadata. Cookie/CSRF `POST revoke {device_id}` revokes a listed grant; bearer `POST revoke` revokes only itself, regardless of the supplied body.

## Direct API authentication

All independent API calls use an Authorization bearer header and `credentials: "omit"`. `X-Hermes-Profile` must equal the approved profile. Invalid, expired or revoked credentials never fall back to cookies, trusted headers or auth-disabled mode.

Chrome emits an extension Origin on POST but can omit it on GET. An authenticated request with no Origin must carry `X-Hermes-Extension-Id` matching the bearer's stored extension identity. A supplied Origin must always be the matching extension Origin, even when the identity header is correct. Both paths still require a valid bearer, no cookies, the approved profile, and an explicitly granted method/path pair. Originless requests can use granted POST routes, including self-revocation; public pairing start/token requests still require an actual extension Origin. The ID is a public-client identifier, **not cryptographic browser attestation**; possession of a stolen bearer remains sufficient for a non-browser attacker with that public ID.

Only reviewed GET/POST paths are granted. Auth administration, profile mutation, onboarding secrets, unknown paths, unsafe verbs and deployment operations are denied. Ordinary cookie clients retain existing CSRF behavior.

- `chat`: conversation/session/attachment capabilities. Agent chat itself can invoke tools.
- `control`: additionally powerful file/terminal/Git/settings/memory/scheduling capabilities and explicitly disclosed **shared Kanban boards across profiles**. Kanban grants only board listing/reading and task read/create/edit routes. No board administration, deletion, dispatch or event-feed grant.
- `cdp`: only device-owned relay IDs. Registration binds the relay; list exposes own relays, and poll/respond/unregister/command reject another device's relay. This does not enable server CDP configuration.

The global cross-profile `/api/sessions/events` feed is not granted. Already-open device SSE connections recheck durable expiry/revocation on every write, including heartbeat writes, and close through normal subscriber cleanup.

## Storage and media

State is locked and atomically replaced under `STATE_DIR`. Only hashes of device/access secrets are stored; requests/devices are bounded and expired records pruned. POSIX uses mode 0600 and `flock`; Windows uses inherited user-directory ACLs and `msvcrt` byte-range locking. File fsync is required; directory fsync is POSIX-only.

Bearer media is narrower than cookie media: `/api/media?path=...&session_id=...` can read only the granted profile's session attachment inbox or that session's explicit workspace, with anchored file opens and a sensitive-path denylist. Upload-inbox URLs can infer session identity from the inbox path. Ambient temporary/Hermes directories and `MEDIA_ALLOWED_ROOTS` do not confer device access. Device media supports live bytes, not historical snapshot selection. Cookie-media behavior is unchanged.

`/api/sidecar/identity` is optional; a server without it can return 404. `/api/background` supports task creation. Clients must not equate optional-feature failure with a reason to bypass device authentication.

## Tests

`tests/test_extension_auth.py` exercises pairing, PKCE, request origins, originless Chrome GET identity, expiry/revocation, profile/scope/relay isolation, media limits and cookie/CSRF behavior. `tests/test_extension_kanban_scope.py` asserts the narrow administrative board routes and denied dispatch/destructive/feed routes. Existing auth/session/passkey/CSRF/profile tests must also remain green.
