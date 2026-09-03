# Cloud sync operations contract

## Boundary and identities

The selected deployment has no managed API gateway. The read and sync
Container Apps expose public HTTPS ingress, while the application enforces
server-issued credentials, signed request envelopes, durable quotas, and the
durable kill switch. Registration is not open: an operator creates the first
enrollment for a rider and issues a short-lived one-time invitation. Every
device added afterwards is paired by the rider's desktop with a one-time code
and no operator credentials — see the pairing section below. Tokens are
revocable and never logged. The gateway decision and pricing evidence are in
[`docs/azure-gateway-decision.md`](azure-gateway-decision.md).

The Container Apps environment remains VNet-integrated but is public so
clients can reach it directly. Storage uses the `Microsoft.Storage` service
endpoint from the ACA subnet with a deny-by-default firewall; the external
budget Function is admitted by a same-tenant resource-instance rule and its
explicitly supplied possible outbound IPs.
Shared keys, anonymous blobs, private endpoints, and private DNS are not used.
The storage firewall admits only the ACA subnet, the budget Function resource
instance, and those explicitly supplied Function egress IPs.
Managed identities and Azure RBAC remain required. Production deployment
inputs require a high-entropy operator token of at least 32 characters.
Certificate presence and caller-controlled gateway headers are not application
authentication factors.

## Public routes and controls

The published API is HTTPS-only and application-authenticated. The versioned
contract is:

| Route | Plane | Authentication | Capability |
|---|---|---|---|
| `POST /api/v1/enrollment/start` | read | operator token + conditional attested subject | — |
| `POST /api/v1/enrollment/complete` | read | one-time invitation + writer public key + conditional attested subject | — |
| `POST /api/v1/context/refresh` | read | signed device credential + conditional attested subject | `read` |
| `POST /api/v1/devices/pairing-codes` | read | server-issued writer credential + signed request + conditional attested subject | `write` |
| `POST /api/v1/devices/pair` | read | one-time pairing code | — |
| `GET /api/v1/devices` | read | signed writer-or-device request + conditional attested subject | `read` |
| `POST /api/v1/devices/{credential_id}/revoke` | read | signed writer-or-device request + conditional attested subject | `read` |
| `GET /api/v1/context` | read | reader context + conditional attested subject | — |
| `GET /api/v1/context/calendar` | read | reader context | — |
| `GET /api/v1/context/activities` | read | reader context | — |
| `GET /api/v1/context/activities/{id}` | read | reader context | — |
| `GET /api/v1/context/races` | read | reader context | — |
| `GET /api/v1/context/dashboard` | read | reader context | — |
| `GET /api/v1/context/volume` | read | reader context | — |
| `GET /api/v1/context/curve` | read | reader context | — |
| `POST /api/v1/sync/batches` | sync | server-issued writer credential + signed request | `write` |
| `GET /api/v1/sync/status` | sync | server-issued writer credential + signed request | `write` |

### Mobile read context

The dashboard route returns object kinds `profile`, `training_state`,
`load_point`, and `curve`; `volume` returns `volume_week`; and `curve` returns
`curve`. Each route returns an envelope containing `items`, a scope
`revision` to checkpoint (see "Checkpointing" below), and `next_cursor` (or
`null`):

```json
{"items":[{"id":"...","kind":"...","revision":7,"data":{}}],
 "revision":7,"next_cursor":null}
```

`?since=N` returns only objects whose object revision is greater than `N`;
delta responses also include matching tombstones (`"deleted":true`), while
full reads omit tombstones. `?limit=` is bounded to 100 and a non-null
`next_cursor` is passed as `?cursor=` for the next page. Cursors are opaque,
deterministic, scope-bound, and bound to the route and `since` value; they
cannot be reused across scopes or to change ordering/filter semantics.

**Checkpointing.** The `revision` in the envelope is *pinned when pagination
starts*: the first request of a walk (the one with no `?cursor=`) reads the
scope revision, and every subsequent page of that walk returns the same value,
carried inside the signed cursor. A client checkpoints that `revision` only
after consuming all pages -- and because it is pinned, every page of a walk
reports the identical number, so a client that checkpoints early is merely
redundant rather than wrong.

The pin is what makes the checkpoint safe. Pages are ordered by object id and
a cursor only moves forward, so an object delivered on an early page sorts
*behind* the cursor and no later page can carry it. If a write changed that
object mid-walk, a checkpoint recomputed on the last page would sit *past* the
change while the client never received it -- the object would be silently
dropped from every future delta. Pinning holds the checkpoint at the value
observed before the walk began, so any write that lands during pagination has
a higher revision and is simply re-delivered on the next poll.

The consequence to design the client around is that this feed is
**at-least-once, never at-most-once**: an object may arrive again on a later
poll even though nothing about it changed since the client last saw it.
Applying an object is therefore required to be idempotent -- compare the
per-object `revision` and ignore one that is not newer than the stored copy.
The same property holds inside a single page: the server reads the scope
revision *before* reading the page, so the checkpoint is a floor for that
page, not a claim that the page reflects it.

A cursor must carry its pinned revision. A cursor without that field, or with
a non-integer or negative one, is rejected with `400 invalid cursor` rather
than being given a fresh revision -- silently defaulting it would reintroduce
the drop described above. The field is inside the HMAC-signed payload, so a
client cannot move its own checkpoint by editing it; the scope binding stays
in the signing key, so a cursor still cannot be replayed into another scope.

Every "attested subject" above is conditional on
`CloudConfig.require_verified_subject`, which a deployment may only set while a
gateway actually attests one — see "The subject is an optional binding" below.
`POST /api/v1/devices/pair` is the one route that never requires a subject at
all: the pairing code is the authorization.

Enrollment and pairing validate device binding, expiry, nonce, and replay state.
Enrollment returns a server-generated writer subscription key; it is an app
credential, not a gateway subscription, and is never copied from a caller.
CORS is limited to the exact configured PWA origin. Durable application quotas
and the kill switch are authoritative; the process-local per-second window is
only a per-replica load shaper. Set the kill switch to return 503 before
emergency maintenance.

## Paired devices, capabilities, and reader-context refresh

A reader context lives 300 seconds. A phone that sleeps past that would
otherwise need a fresh operator-gated enrollment, so a paired device gets a
durable credential of its own and trades it for a new context.

`POST /api/v1/enrollment/complete` optionally accepts `device_public_key` and
`device_signature_algorithm` alongside the writer's `public_key`. When present
it registers a `DeviceCredential` bound to the same server-derived
`(namespace, local_user_scope)` as the writer, and returns
`device_credential`, `device_subscription_key`, `device_signature_algorithm`,
and `device_capabilities`. The device subscription secret is generated by the
server, exactly as the writer's is.

What a credential may do is **data on the credential**, not an assumption in
the code. Devices are issued with `capabilities = {"read"}`; writers carry
`{"read", "write"}`. Every route asserts the capability it needs, and the sync
routes assert `"write"`. Granting a device mobile writes later is a capability
change on the stored record plus the assertion that already exists — no route
infers read-only from the credential's type.

`POST /api/v1/context/refresh` is signed with the device credential using the
same `canonical_request` framing, the same 300-second timestamp-freshness
window, and the same nonce-replay guard as the writer path. Headers are
`X-Device-Credential`, `X-Device-Timestamp`, `X-Device-Nonce`, and
`X-Device-Signature`; the canonical request uses the fixed idempotency key
`context-refresh` and an empty revision, because the request has no body and
no idempotent effect — the nonce is what makes it single-use. A successful
response returns `reader_context`, `expires_in`, and `capabilities`.

Three signature algorithms are supported: `hmac-sha256`, `ed25519`, and
`ecdsa-p256-sha256`. P-256 exists because Apple's Secure Enclave generates
only P-256 keys. **The algorithm is always selected from the stored
credential, never from the request.** P-256 public keys are uncompressed SEC1
points (65 bytes, `0x04` prefix); P-256 signatures are raw `r || s` presented
as exactly 128 lowercase hexadecimal characters. DER is rejected on shape:
there is no ASN.1 parser at this trust boundary. An Ed25519 signature is also
128 hexadecimal characters, so length distinguishes nothing and only the
stored algorithm separates the two.

Every refresh rejection — unknown device, revoked device, bad signature, stale
timestamp, replayed nonce, subject mismatch, missing capability — returns the
same 404 body and headers as an unknown reader context.

Because refresh consumes replay nonces, the read plane now claims them
durably. It writes those claims to the `CloudAuth` table its managed identity
already writes; it still never touches `CloudReplay`, for which it holds no
role. Revocation of a device is reachable over HTTP — see "Revoking a lost
device" below. Writer and reader revocation remain library-only.

Both Container Apps use `minReplicas=0` and `maxReplicas=1`; cold starts and
single-instance throughput are accepted operational tradeoffs.

## Pairing a second device, and the same-namespace-per-rider rule

**Rule: one rider, one namespace.** Every device a rider pairs — the desktop,
the iPhone, the iPad — reads and writes the same
`(namespace, local_user_scope)` pair. A namespace is
`HMAC(server_secret, installation_id)` and an `installation_id` is minted once,
at the desktop's operator-gated enrollment. Nothing after that mints another
one. A second device does not enroll; it *pairs into* the namespace the
desktop already owns. This is what stops the iPad opening an empty account,
which is exactly what a second `enrollment/start` used to produce.

The rider's desktop install is the identity authority. Operator credentials
bootstrap the *first* install and are never needed again.

```
operator token ──► enrollment, once ──► installation_id ──► namespace
                                                              │
desktop, holding a writer credential bound to (namespace, scope)
   │
   ├─► POST /api/v1/devices/pairing-codes
   │      signed with the writer key; no request field names a namespace
   │   ◄── ABCD-EFGH-JKMN   single use, <= 900 s   shown on screen or as a QR
   │
   │   (the rider carries the code to the phone)
   ▼
phone, holding no credential at all
   │
   └─► POST /api/v1/devices/pair  { code, public_key }
       ◄── device credential + its own subscription key + reader context,
           in the SAME namespace and scope as the desktop
```

### Minting

`POST /api/v1/devices/pairing-codes` takes the writer's ordinary signed
envelope — `X-Writer-Credential`, `-Timestamp`, `-Nonce`,
`-Idempotency-Key`, `-Revision`, `-Signature` over the same
`canonical_request` framing, the same 300-second freshness window, and the
same nonce-replay claim. The idempotency key is the fixed string
`device-pairing-code` and the revision is `0`, because there is nothing here
for a caller to choose.

The code is bound to the `(namespace, local_user_scope)` **of the
authenticated credential**. No request field names a namespace, a scope, an
installation, or an account; a caller can only ever mint into the account it
already controls.

Minting stays strict, and it can afford to: a writer-signed request is real
authentication that borrows nothing from a gateway. Where a gateway attests a
subject, the mint route requires it, matches it against the subject bound into
the writer at enrollment, and the code inherits it. Where none is attested the
header is not read and the code binds no subject.

Capability is `write`. Pairing authority *is* installation authority, and a
read-only paired device therefore cannot mint codes for further devices.
Both routes live on the **read plane**: minting persists a record in
`CloudAuth`, and only the read plane's managed identity may write that table.

### Redeeming

`POST /api/v1/devices/pair` takes `{code, public_key}` and an optional
`signature_algorithm` (`ed25519` or `ecdsa-p256-sha256`; a device keeps its
private half in hardware, so a symmetric algorithm is refused). It returns
`device_credential`, `device_subscription_key`, `device_signature_algorithm`,
`device_capabilities`, `signing_namespace`, an initial `reader_context`, and
`expires_in`.

**The device never chooses its namespace or scope.** Both come from the
code's binding, the same way `SyncBatch.from_wire` parses and discards a
client-supplied `installation_id`. A `namespace`, `local_user_scope`,
`installation_id` or `capabilities` field in the pair body is simply not
read. Devices are issued `capabilities = {"read"}` here, as at enrollment.

The public key is fully validated — encoding, length, and the on-curve proof
— *before* the single-use code is spent, so a rejected key never strands a
rider holding a burnt code and no device.

Every code failure — unknown, malformed, expired, already consumed, or
(where a subject is attested) redeemed by the wrong one — returns the identical
404 body and headers as an unknown reader context. A subject mismatch
deliberately does not spend the code: a rider redeeming on the wrong account
should not lose it, and the response says nothing either way. A 400 is
reachable only for a request malformed independently of the code, which reveals
nothing because the wire format is public.

A cross-namespace read is a 404, never a 403: a 403 would confirm that an
object exists somewhere.

### The subject is an optional binding, never the authorization

`POST /api/v1/devices/pair` deliberately does **not** require a verified
subject. Demanding one would mean the rider signing in to an identity provider
on the phone before pairing, which is the thing the code exists to avoid — the
whole point is that there is no password in the cloud and no IdP on the device,
just a code read off the desktop. It is the same shape as pairing a TV app.

A subject header is also worth exactly as much as the gateway that overwrites
it. With a gateway in front, it is attested. Without one it is whatever the
caller typed, and the gateway proof secret degrades into a static shared
secret. So:

- `CloudConfig.require_verified_subject` declares whether this deployment has
  an issuer. When it is false **no route reads the header at all** — reading a
  header nobody vouches for is worse than not having one, because it looks
  like a check.
- `CloudConfig.gateway_attests_subject` is true only when the proof is both
  required and configured.
- `CloudState.create(..., require_persistent_security=True)` — which is how
  the container entrypoint builds the app — **refuses to boot** when a
  deployment claims `require_verified_subject` while no gateway attests one.
  Removing the gateway is therefore a deliberate configuration change, not a
  silent downgrade of every subject check in the app.

Where a subject *is* attested it is applied as an additional binding on top of
the code, never instead of it: the mint route requires it and matches it
against the subject bound into the writer at enrollment, and the code inherits
it. At redeem, the demand then comes from **the code, not the route** — the
stored record enforces a subject if and only if it carries one, so omitting the
header cannot bypass a binding that exists, and a code that carries no subject
needs no header.

Where nothing is attested, the code binds no subject and the paired device
carries none. That is deliberate rather than incidental: a subject on the
device credential is compared on every later request, so pinning one that
nothing can verify would add no security and would leave a phone unable to read,
holding a string it has no way to obtain.

### Code shape and entropy

The code is 12 symbols of Crockford Base32 shown as `XXXX-XXXX-XXXX`.
Crockford's alphabet is 32 symbols with `I`, `L`, `O` and `U` removed, so
there is no 1/I/l or 0/O confusion; typed input is uppercased, hyphens and
spaces are dropped, and `I`/`L` fold to `1` and `O` to `0`. Generated codes
never contain the folded letters, so folding cannot merge two codes or shrink
the space. `U` is not a legal symbol at all.

Fourteen characters of `A–Z`, `0–9` and `-` are all inside QR alphanumeric
mode, so the code encodes as a version-2 QR at error-correction level H
(25×25 modules) — the smallest practical symbol, scannable from a laptop
screen at arm's length — and stays short enough to type on a phone in
landscape.

The entropy arithmetic, because the number is the control:

| Quantity | Value |
|---|---|
| Alphabet | 32 symbols → 5 bits/symbol |
| Length | 12 symbols → **60 bits** |
| Code space | 2^60 = 1,152,921,504,606,846,976 ≈ 1.15 × 10^18 |
| TTL ceiling | 900 s (default 600 s) |
| Deployment shaping | 100 requests/second pre-auth admission and two backend slots per warm replica; not a global quota |

The selected profile has no provider per-key quota in front of the pairing
route. The 60-bit, single-use code and 900-second ceiling are therefore the
guessing bound; the process-local request window is only load shaping and may
multiply with replicas. A pre-auth global admission window limits anonymous
work on each warm replica, but it is not a durable or deployment-wide rate
policy. A failed guess does not reveal whether a code exists and does not
spend a valid code. Durable application quotas protect authenticated scopes
after a credential or code establishes one. An unauthenticated pairing probe
still performs one bounded credential-table lookup before a rider scope exists;
there is no provider or durable anonymous rate limiter in this deployment.

The TTL ceiling is enforced in `DevicePairingRegistry`, not left to callers,
because the whole argument above is stated against a bounded window.

### Direct public ingress

The selected deployment reaches `/api/v1/devices/*`, enrollment, reads, and
sync directly through the public Container App HTTPS endpoints; there is no
gateway operation inventory to keep in sync. The app's exact-origin CORS
middleware and route-level credentials apply to every route.

`require_verified_subject=False` is explicit in the production runtime.
Enrollment start therefore uses the operator token alone, enrollment complete
uses the one-time invitation and public key, and both routes ignore any
caller-supplied subject header. A subject remains an optional additional
binding only for a separately configured proxy deployment; an invitation bound
to a subject cannot be redeemed when that attestation is absent. Pairing and
refresh continue to work without an identity provider on the device.

## Revoking a lost device

A phone goes missing. The rider does not have to be at the desktop, and they
must not have to wait for a credential to expire — a device credential is
durable by design, which is exactly what makes revoking it the only way to end
its access.

```
GET  /api/v1/devices                          list this rider's devices
POST /api/v1/devices/{credential_id}/revoke   end one device's access
```

Both live on the **read plane**, because both write `CloudAuth` and the read
plane's managed identity is the only one that may. Both are signed with the
writer envelope (`X-Writer-*`, the same `canonical_request` framing, freshness
window and replay guard as every other signed route), and `_writer_auth`
resolves a writer credential *or* a paired device from it. The capability
asserted is `read`, which every writer and every device carries.

**The scope is the caller's, always.** It is `(namespace,
local_user_scope)` taken from the authenticated credential. No request field,
path segment or header names a namespace, a scope, an installation or another
rider, and nothing reads one if it does.

**Who may revoke.** Any credential in the same scope: the desktop writer,
another paired device, or the target device revoking itself. Requiring the
desktop would mean the revocation waits until the rider gets home, with the
lost phone reading their data in the meantime. The cost is that a stolen phone
can revoke its siblings — bounded on purpose, because only *device*
credentials can be revoked here. The desktop's writer credential cannot, so a
stolen device can force a re-pair and nothing more; it can never cut the rider
off from their own sync plane.

**Cross-namespace is 404, never 403.** An id in another rider's namespace, an
id in another local scope, an unknown id, a malformed id, and an id that names
a writer credential, a quota counter or the kill-switch row all return the
identical body, status and headers as every other not-found in this API. A 403
would confirm that a credential exists somewhere the caller cannot see, which
is the whole attack. Revocation is idempotent for the same reason: revoking an
already-revoked device answers exactly as revoking a live one does, so a retry
after a timeout is safe and the status code carries no state either.

**A revoked device fails the way an unknown one does.** `resolve_device`
returns `None` for revoked and unknown alike, so refresh answers the same 404
either way.

**And it fails immediately, not in five minutes.** A reader context is a
bearer token with a 300-second life, so a revocation that only stopped
*refresh* would leave the lost phone reading for up to five minutes — a delay,
not a revocation. Every context minted for a device therefore records the
device that minted it, and `read_context_token` re-resolves that device on
each use: revoked or unknown, the context stops resolving and the read answers
the same 404 as an unknown token. Contexts minted at enrollment record no
device and are unaffected. The cost is one extra table read per read request
on a deployment whose whole monthly bill is a few dollars.

**A quota refusal never blocks a revocation.** The call is metered after the
fact rather than admitted before it, as pairing already is: a rider who has
spent the day's read allowance must still be able to revoke a lost phone. The
budget kill switch does stop it, deliberately — that is a deployment-wide
decision, not a per-rider one.

**The listing carries no key material.** Each entry is `credential_id`,
`label`, `capabilities`, `created_at`, `last_seen_at`, `revoked` and `self`,
built by naming those fields rather than by filtering a stored record, so a
field added to `DeviceCredential` later cannot appear here by default. No
verification key, no subscription key, not even a digest of one, no signature
algorithm, no namespace and no local scope. Revoked devices stay listed and
flagged: "did the revocation stick?" is the question the rider asks next.

`label` is the optional name a device gives itself at pairing (bounded, no
control characters, validated *before* the single-use code is spent).
`last_seen_at` is written on each successful refresh into a row of its own —
never onto the credential record. Rewriting the credential on the refresh path
would race a concurrent revocation and could write the pre-revocation record
back, silently resurrecting the device the rider just revoked.

A device row now carries its own `credential_id` alongside the digest that
addresses it, because a digest cannot be inverted and a rider who cannot see
an id cannot revoke it. The stored id is re-hashed and checked against its own
row key before it is used, so an edited payload describes a row that is
skipped rather than a device that appears in someone else's listing. Devices
paired before this change carry no id in their row and are not listed; they
remain revocable by id.

### The expired-row sweep

`CloudAuth` accumulates rows that nothing will ever read again: a reader
context and its index per refresh, a replay claim per signed request, spent
invitations and spent pairing codes. Azure Tables has no TTL, so until now the
table grew without bound — nothing could delete a row, because no managed
identity held a table `entities/delete` action.

`readAuthSweepRole` grants the read identity one, through
`authSweeperRoleDefinition`: a custom role whose only data action is
`entities/delete`, assignable only to the `CloudAuth` table. It is a separate
role rather than a fourth action on `authManagerRoleDefinition` so the grant
that removes rows is legible and assignable on its own. The sync identity is
unchanged and holds read-only on `CloudAuth`.

`ExpiredRecordSweeper` runs the sweep opportunistically, on routes that have
already written: a pass bounded twice over — 200 rows examined per record
kind and at most 200 deleted in total — at most once every 900 seconds per
replica, logged and swallowed on failure, after the request's real work is
durable. Both bounds are latency somebody's refresh is paying for, and both
are set against the write rate they must keep up with: four devices refreshing
every five minutes write roughly 3,500 expiring rows a day, about 36 per
quarter-hour, so a steady-state pass is short and a backlog drains over a few
hours instead of in one long request. A retention rule with no enforcement was
the alternative and was rejected — there is no operator CLI and no cleanup job,
so a documented rule would have bounded nothing.

**It cannot delete the kill switch.** Azure table roles cannot be conditioned
on a row key, so the grant itself reaches every row in `CloudAuth`. Three
things in the application stand between it and the two rows that matter:

- **An allowlist of record kinds.** A row is a candidate only because its kind
  is named in `SWEEPABLE_RECORD_KINDS` (`context`, `context-index`,
  `invitation`, `device-pairing`, `nonce`), never because a scope or expiry
  test happened to match it.
- **A denylist, checked at construction and again on every pass.**
  `NEVER_SWEEP_RECORD_KINDS` names `kill-switch`, `quota-counter`, `writer`,
  `device`, `device-seen` and `health`. Constructing a sweeper over any of
  them raises.
- **Expiry.** Only a row already past its own `expires_at` plus a 300-second
  grace window is removed. The kill-switch and counter rows carry no
  `expires_at` at all, so even inside the allowlist they would never qualify;
  and a row whose payload will not decode is treated as live and left alone.

An absent kill-switch row reads as *enabled*. Deleting one would therefore
re-enable spending on a deployment somebody deliberately killed — no error, no
log, and the next `state()` refresh inside the 30-second TTL reporting a
healthy deployment. `tests/test_cloud_security.py` sets the switch, runs the
sweep and asserts `read_kill_switch(backend)` still reports it disabled.

Deleting an *expired* replay claim cannot re-open a replay: a request carrying
that nonce is outside the 300-second timestamp-freshness window long before
its claim expires at 600 seconds. Deleting an unexpired one could, which is
why expiry is the whole test.

## Local offline contract

The desktop app remains the source of truth while offline. The opt-in client
builds bounded, idempotent batches from a separate read-only SQLite connection;
an enclosing worker may queue those batches and retry with bounded exponential
backoff. A failed sync returns `Cloud sync offline — local data and features
are unaffected.` and never blocks local rides, planning, import, or export.
Pairing is explicit and revocable; local data is not deleted because cloud sync
is unavailable.

Publisher audit: activity summary fields (`start_time`, duration, distance,
power/heart-rate summaries, NP, IF, TSS, and activity RPE) match the local
activity read summary; hidden duplicate rides are excluded; stream power uses
the active correction ranges used by local activity reads; and no weight is
published (weight is resolved at read time for display/scaling only). Filename,
local IDs, and other local-only fields are intentionally excluded from the
cloud payload. History cutoffs and dirty/republish tracking remain out of
scope for #173 (#172 and #157).
Legacy read-only snapshots without `duplicate_of` retain all activities, and
snapshots without a complete correction table publish streams without masking.
The payload validator permits up to 16,384 array items for realistic long
streams; the existing 512 KiB object and decompression limits still apply.

The deployed container entrypoint is `python -m wattracker.cloud.runtime`. It
constructs `AzureTenantStore` with managed identity and Storage service
endpoints; shared credential/context state is persisted in the separate
`CloudAuth` table, and replay claims plus daily quota counters in `CloudReplay`
on the sync plane and in `CloudAuth` on the read plane. The `CloudControl` table
holds the kill switch and is read by both planes. The in-memory stores are
test-only. Images, server secret,
operator token, storage account, and exact origin are deployment inputs. The
production runtime disables gateway proof and verified-subject compatibility
gates; containers reject requests that merely forge a boolean marker or
certificate header.

### Reproducible cloud-image verification

Run these commands from the repository root on a Docker host with Buildx. They
build the same `linux/amd64` target used for deployment and deliberately name
the local candidate so all subsequent commands inspect the image that was
built. Docker and Azure tooling are not prerequisites for editing this document;
when either is unavailable, record the command below as **unverified** rather
than inferring its result.

```sh
WATTRACKER_CLOUD_IMAGE=wattracker-cloud-verify:local
docker buildx build --platform linux/amd64 --load --tag "$WATTRACKER_CLOUD_IMAGE" -f Dockerfile.cloud .
docker run --rm --platform linux/amd64 --entrypoint python "$WATTRACKER_CLOUD_IMAGE" -c \
  'import cryptography, azure.identity, azure.storage.blob, azure.data.tables; import wattracker.cloud.api, wattracker.cloud.runtime'
docker image inspect "$WATTRACKER_CLOUD_IMAGE" --format 'image={{.Id}} size_bytes={{.Size}}'
docker buildx imagetools inspect python:3.12-slim
```

The import command verifies the Azure Identity, Blob Storage, and Tables
dependencies, plus both cloud application modules. `docker image inspect`
prints the actual local image size in bytes. `imagetools inspect` resolves the
base-image manifest and its `linux/amd64` digest; retain that output as the
base-image provenance. The final PR must report the actual image size and
base-image provenance from these command outputs, not estimates or tag names
alone.

The Dockerfile `HEALTHCHECK` is deliberately only an import check for
`wattracker.cloud.runtime`; it does not start the web app, authenticate to
Azure, or prove HTTP readiness. The production entrypoint requires real,
managed deployment configuration: a base64 server secret of at least 32 bytes,
an operator token of at least 32 characters, storage-account name, Azure client
ID, and a usable managed identity with the required storage/table access. Do
not put those values in a command, shell history, CI log, or committed env file.

Only when a real deployment environment makes those values and managed identity
available, start the candidate with a non-committed, permission-restricted env
file and then check the unauthenticated root refusal:

```sh
WATTRACKER_CLOUD_IMAGE=wattracker-cloud-verify:local
set -e
cleanup() {
  docker rm --force wattracker-cloud-verify >/dev/null 2>&1 || true
  rm -f /tmp/wattracker-cloud-root.txt
}
trap cleanup EXIT
docker run --detach --name wattracker-cloud-verify --platform linux/amd64 \
  --env-file .cloud-runtime.env --publish 127.0.0.1:8000:8000 "$WATTRACKER_CLOUD_IMAGE"
WATTRACKER_HTTP_STATUS=$(curl --retry 20 --retry-connrefused --retry-delay 1 --silent --show-error \
  --output /tmp/wattracker-cloud-root.txt \
  --write-out '%{http_code}' http://127.0.0.1:8000/ || true)
docker logs wattracker-cloud-verify
test "$WATTRACKER_HTTP_STATUS" = 404
test -f /tmp/wattracker-cloud-root.txt
! rg --fixed-strings 'Traceback' /tmp/wattracker-cloud-root.txt
```

`GET /` is intentionally not a health endpoint or API route: an unauthenticated
request to it returns `404`. The check above verifies that refusal has no
traceback; it must not be substituted with an invented root or health route.
If the runtime cannot be started against real Azure configuration, the run and
HTTP checks are unverified locally. The `EXIT` trap cleans up the temporary
container and response file after either a successful or failed attempt; if
startup fails, use `docker logs wattracker-cloud-verify` before exit.

CI enforcement options: a PR job can run the build, imports, and image/base
inspection without Azure credentials, which catches Dockerfile and dependency
regressions but cannot prove runtime Azure access or HTTP behavior. A separate
manual or protected-environment deployment smoke test can run the startup and
`GET /` refusal check with managed identity, but it adds cloud cost, credential
scope, and environment coordination. Keep the currently disabled containerized
job in `.github/workflows/cloud.yml` unchanged until one of those tradeoffs is
explicitly accepted.

## Operations and cost

The Bicep monthly budget is `$10`, with percentage alerts at 50%, 80%, and
100% of that amount. The 80% and 100% notifications invoke authenticated
Azure Function routes outside Container Apps: `disable-writes` persists the
80% state with reason `budget 80%`, and `disable-public-api` persists the 100%
state with reason `budget 100%`. The Function is a separately deployed
Consumption app; keep its possible outbound IP list synchronized in
`budgetHookIpRules`. See
[`docs/azure-gateway-decision.md`](azure-gateway-decision.md) for the deployment
contract and drill. Supply `budgetStartDate` and `budgetEndDate` explicitly so
the budget period is current and is not inherited from a stale template date.

**The app-side daily quota counters are durable.** Uploaded bytes, stored
objects, read bytes, and read requests are each counted per rider scope *and*
per installation in a shared table row, charged with the same etag-guarded
compare-and-swap that `claim_replay` uses for nonce claims. A counter therefore
survives a process restart and is shared by every replica charging it, which is
what makes it a real limit here: the container apps run at `minReplicas: 0` and
cycle constantly, so a process-local counter is reset by the platform several
times a day and enforces nothing. `CloudState.create(...,
require_persistent_security=True)` refuses a non-durable quota manager at boot,
the same way it refuses an in-memory auth backend. The process-local manager
remains for tests and local development.

The unauthenticated admission guard is a process-local load shaper for each
replica; it is not a durable gateway rate policy. Durable daily counters apply
after the request has passed the relevant authentication boundary, and
exact-origin CORS constrains browsers but does not constrain non-browser
callers. These controls must not be described as equivalent to the gateway
rate policy that was removed.

Counters are addressed by `(namespace, scope-or-installation, metric)` and
carry their UTC day inside the row; the first charge of a new day reclaims that
row in place and discards yesterday's total. A counter row is never deleted —
`quota-counter` is one of the record kinds the expired-row sweep is forbidden
to touch, and it carries no expiry to qualify on — and nothing accumulates: the
row count is bounded by the number of (subject, metric) pairs, not by elapsed
days. Counter rows live in the same table each plane already
writes for replay claims — `CloudAuth` for the read plane, `CloudReplay` for
the sync plane — so no new table and no new role is required. The two planes
therefore count into separate tables, which bounds a rider's worst case at one
daily allowance per plane rather than one per replica or per restart.

`max_stored_bytes_per_scope` is a level rather than a daily counter: it is
asserted against the object store's own usage, which is already durable, and it
is checked before anything is charged so a scope over its storage cap does not
also burn its daily upload allowance.

Two app-side limits stay deliberately process-local, because they shape one
replica's instantaneous load rather than a day's spend: the
100-request-per-second global window and the two-slot backend concurrency
semaphore. They are load shapers, not global security or billing quotas; the
durable application counters are the authoritative daily limits.

**The budget kill switch is durable.** It is the last line of cost protection,
so it is not a boolean in a replica that scales to zero — it is one row in the
dedicated shared `CloudControl` table, read on the admission path of every
request.

*Two levels, matching the two budget thresholds.* `public_enabled` is the wider
one: every route, read or write, checks it, so clearing it stops the
deployment. `writes_enabled` stops only the sync plane's admissions. They are
stored independently, because the 80% and 100% actions fire independently and
may arrive in either order.

*It lives in `CloudControl`, not in the plane's own quota table.* The counters
split by plane because each identity may write only its own table; a kill
switch that split the same way would be two switches, and throwing one would
leave the other plane serving. Every plane can *read* `CloudControl`, so every
plane obeys the switch; only the external budget Function can write it, so the
Container App identities cannot mutate the switch they obey.

*Staleness window: 30 seconds.* A replica caches what it read for 30 seconds
and no longer — `KILL_SWITCH_TTL_SECONDS`, capped at 60 by
`KILL_SWITCH_MAX_TTL_SECONDS`, which is refused at construction rather than
left to review. The window is a ceiling, not a hint: the cache is replaced,
never extended, so a cached "enabled" cannot outlive it, and a refresh that
fails drops the cache instead of falling back to it. The bound is chosen
between two costs. A switch that takes an hour to bite is a log entry, not a
kill switch; a read per request puts a table transaction in front of every
admission on a deployment whose whole bill is a few dollars a month. At 30
seconds a replica reads at most twice a minute regardless of traffic, and the
worst case after the switch is thrown is 30 seconds of continued service on
replicas that were already warm. A replica that starts *after* the switch was
thrown has no cache at all and reads the row on its first request, which is the
property the process-local flag never had.

*It fails closed.* An unreadable or unintelligible kill state refuses the
request — 503 with `Retry-After: 30` — on every route that admits traffic. This
is deliberately the opposite of what the quota paths do with a damaged row:
there, an unparseable counter is reset and healed, because refusing forever
would strand a rider and the ceiling immediately re-applies. The kill switch
has no such bound. It is thrown precisely when spending has already gone wrong,
so reading "carry on" out of an error is the exact failure it exists to
prevent. An absent row is the one reading of "enabled" that is not a guess: a
row is created the first time the switch is set and is never removed.

*Clearing it is an update, never a delete.* Clearing writes both levels
enabled back into the same row — the same shape that makes the quota counters
reclaim their row in place. Since #153 the read identity does hold a table
`entities/delete` action on `CloudAuth`, for the expired-row sweep, so this is
no longer guaranteed by RBAC alone: `kill-switch` is named in
`NEVER_SWEEP_RECORD_KINDS`, it carries no `expires_at` to qualify on, and
`tests/test_cloud_kill_switch.py` fails the build if any kill-switch path
reaches for a delete. See "The expired-row sweep" above.

*Operator path.* `wattracker.cloud` exports `read_kill_switch`,
`set_kill_switch`, `disable_writes` (the 80% action), `disable_public_api` (the
100% action), and `clear_kill_switch`. They take a state backend rather than a
running app, so the switch can be thrown and cleared while every replica is
scaled to zero. `set_kill_switch` requires both levels: a partial update would
have to read the level it is not changing, and a read that fails during an
incident is exactly when the write most needs to land. `disable_writes` uses an
etag-guarded partial update, preserving a concurrent 100% shutdown rather than
allowing a delayed 80% notification to re-enable the public API. It still
checks availability first and raises rather than guessing when that read
fails. `disable_public_api` uses a monotonic full declaration that only ever
removes capability and therefore needs no readable prior state. The operator
CLI in #169 is the intended caller of all five.

`CloudState.create(..., require_persistent_security=True)` refuses a
process-local kill switch at boot, for the same reason it refuses non-durable
quota counters: in a scale-to-zero deployment a process-local switch does not
merely forget, it re-enables.

Operators must also alert on auth failures, sync conflicts, queue age, storage
availability, Function failures, and Container App restarts. Budgets and
quotas reduce surprise but do not guarantee a zero bill: Azure may charge for
provisioned services, egress, monitoring, Functions, and usage before an alert
fires. The 80% action disables new writes; the 100% action disables public APIs.
The external Function is a deployment-supplied authenticated automation hook
and must be exercised before production. A budget notification alone is not a
hard billing ceiling.

## Acceptance checklist

- [ ] `az deployment group validate` and Bicep build pass with tenant values.
- [ ] Confirm both Container Apps expose HTTPS ingress with `allowInsecure:
      false`, and all route credentials/signature checks work without gateway
      headers.
- [ ] Confirm storage public network access is enabled only for service-endpoint
      routing; anonymous blobs, Shared Key, and HTTP are disabled; TLS 1.2 is
      enforced; the firewall is deny-by-default.
- [ ] Resolve Blob/Table names from the ACA service-endpoint subnet and the
      budget Function resource instance and current egress IPs; verify the
      firewall admits only those sources.
- [ ] Verify each managed identity has only its documented data-plane role —
      in particular, `entities/delete` on `CloudAuth` is held by the read
      identity alone through `authSweeperRoleDefinition`.
- [ ] Throw the kill switch, let the read plane sweep, and confirm the switch
      still reads disabled. An absent row is enabled; a missing table refuses
      startup.
- [ ] Revoke a device and confirm its refresh and its reads both fail exactly
      as an unknown device's do, across a replica restart.
- [ ] Verify enrollment rejects an unapproved operator/device and pairing tokens
      are one-time, expiring, revocable, and absent from logs.
- [ ] Exercise read, push, status, exact-origin CORS, durable application
      quotas, and the 503 kill switch.
- [ ] Drill the kill switch against a *cold* deployment: throw it, scale every
      replica to zero, and confirm the next request is still refused. A drill
      against a warm replica proves only that the cache was invalidated.
- [ ] Test offline queueing, restart/retry, idempotent replay, and conflict
      reporting without blocking local use.
- [ ] Trigger the 50%, 80%, and 100% budget thresholds in a non-production
      subscription; confirm the Function hook persists both kill-switch levels,
      survives app restart, and can be cleared explicitly.
