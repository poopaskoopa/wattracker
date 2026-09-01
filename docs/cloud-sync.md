# Cloud sync operations contract

## Boundary and identities

APIM is the sole public HTTPS boundary. Clients use an approved APIM
subscription and tenant-issued enrollment/pairing token. Registration is not
open: an operator creates or approves the *first* enrollment for a rider,
binds it to the tenant/user, and issues a short-lived one-time pairing token.
Every device that rider adds afterwards is paired by their own desktop
install, which is the identity authority for its namespace, with a one-time
code and no operator credentials — see the pairing section below. Tokens are
revocable and never logged. The exact Entra tenant, issuer, audience, scopes,
certificate, and secret references are deployment inputs, not repository
secrets.

The read service serves API routes through APIM; the sync service accepts only
APIM-proofed traffic and performs writes. The Container Apps managed
environment is internal and both app ingresses are non-external, so their
private FQDNs are reachable only through the APIM Standard External VNet
integration in the same VNet. Managed identities, private Blob/Table
endpoints, and private DNS are required. No storage account key,
anonymous blob URL, or public storage firewall exception is permitted.
The authentication factors are APIM subscription plus signed writer requests
for writes and sync status, and APIM-validated Entra JWT plus a bound reader
context for reads. Certificate presence is not an application authentication
factor.

## Public routes and controls

The published API is HTTPS-only and subscription-protected. The versioned
contract is:

| Route | Plane | Authentication | Capability |
|---|---|---|---|
| `POST /api/v1/enrollment/start` | read | operator token + APIM proof + verified subject | — |
| `POST /api/v1/enrollment/complete` | read | one-time invitation + APIM proof + verified subject | — |
| `POST /api/v1/context/refresh` | read | signed device credential + APIM proof + verified subject | `read` |
| `POST /api/v1/devices/pairing-codes` | read | APIM subscription + signed writer request + APIM proof + verified subject | `write` |
| `POST /api/v1/devices/pair` | read | one-time pairing code + APIM proof | — |
| `GET /api/v1/context` | read | reader context + APIM proof + verified subject | — |
| `GET /api/v1/context/calendar` | read | reader context | — |
| `GET /api/v1/context/activities` | read | reader context | — |
| `GET /api/v1/context/activities/{id}` | read | reader context | — |
| `GET /api/v1/context/races` | read | reader context | — |
| `GET /api/v1/context/dashboard` | read | reader context | — |
| `GET /api/v1/context/volume` | read | reader context | — |
| `GET /api/v1/context/curve` | read | reader context | — |
| `POST /api/v1/sync/batches` | sync | APIM subscription + signed request | `write` |
| `GET /api/v1/sync/status` | sync | APIM subscription + signed request | `write` |

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

Every "verified subject" above is conditional on
`CloudConfig.require_verified_subject`, which a deployment may only set while a
gateway actually attests one — see "The subject is an optional binding" below.
`POST /api/v1/devices/pair` is the one route that never requires a subject at
all: the pairing code is the authorization.

Enrollment and pairing validate tenant, user/device binding, expiry, nonce, and
replay state. Enrollment returns a server-generated writer subscription key;
the caller's APIM subscription key is never reused as the writer credential.
APIM applies an allow-listed CORS origin, 60 requests/minute and 1,000
requests/day per subscription. Set the deployment kill switch to return 503
before emergency maintenance.

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
role. Revocation of a device is `CredentialRegistry.revoke_device`, which is
library-only for the same reason writer and reader revocation is: there is no
revocation route yet.

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
same nonce-replay claim — plus the APIM subscription and the
gateway-verified Entra subject. The idempotency key is the fixed string
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
| APIM limits | 60 requests/minute and 1,000/day per subscription key |

Inside the 900-second ceiling one subscription key buys at most
15 × 60 = **900** guesses — below the 1,000/day cap, so the per-minute limit
is what binds. One key therefore succeeds with probability
900 / 2^60 = 7.8 × 10^-16.

Sizing it from the other direction: to keep an attacker holding 1,000
subscription keys — each spending a full daily budget inside one code's
lifetime, so 9 × 10^5 guesses — below a 2^-32 chance, the code needs
2^b ≥ 9 × 10^5 × 2^32 ≈ 3.9 × 10^15, i.e. **b ≥ 52 bits**. Sixty bits clears
that floor by 8 bits, a factor of 256. The APIM product issues one
subscription per approved account (`approvalRequired: true`,
`subscriptionsLimit: 1`), so 1,000 keys is already a generous overestimate of
a real attacker; the limits are keyed on
`context.Subscription?.Id ?? context.Request.IpAddress`.

The TTL ceiling is enforced in `DevicePairingRegistry`, not left to callers,
because the whole argument above is stated against a bounded window.

### Not yet reachable through the gateway

`main.bicep` enumerates APIM operations explicitly and does not yet declare
`/devices/pairing-codes` or `/devices/pair`, so neither is reachable through
APIM until #165 adds them.

If the gateway survives, note that APIM's API-level policy requires a validated
Entra JWT for every path not containing `/sync/`, which is what supplies
`X-Verified-Entra-Subject`; the desktop would then need a live rider sign-in to
mint, not only its writer credential. APIM's `allowed-headers` CORS list also
still lacks the `x-device-*` headers the app's own CORS middleware permits.

If it does not — #164 is weighing the gateway's $330–700/month against a
$2–5/month deployment — then the replacement deployment sets
`require_verified_subject=False` and pairing continues to work untouched,
because the code was never leaning on the gateway for anything. What that
deployment still owes is a replacement for the parts that *do* lean on it:
`enrollment/start` and `enrollment/complete` still require a subject header
unconditionally, and the operator token plus the one-time invitation are the
only real secrets on those routes once no JWT is validated. That is bootstrap,
it is operator-driven, and it is out of scope here — but it must not be
forgotten when the gateway goes.

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
constructs `AzureTenantStore` with managed identity and private Blob/Table
endpoints; shared credential/context state is persisted in the separate
`CloudAuth` table, and replay claims plus daily quota counters in `CloudReplay`
on the sync plane and in `CloudAuth` on the read plane. The in-memory stores are
test-only. Images, server secret,
operator token, APIM proof secret, storage account, and exact origin are
deployment inputs. APIM overwrites a private proof header on every backend
request; containers reject requests that merely forge a boolean marker or
certificate header.

## Operations and cost

The Bicep budget alerts at 50%, 80%, and 100% actual usage.

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

Counters are addressed by `(namespace, scope-or-installation, metric)` and
carry their UTC day inside the row; the first charge of a new day reclaims that
row in place and discards yesterday's total. Nothing is deleted, because no
managed identity holds a table `entities/delete` action, and nothing
accumulates: the row count is bounded by the number of (subject, metric) pairs,
not by elapsed days. Counter rows live in the same table each plane already
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
semaphore. APIM's `quota-by-key` and `rate-limit-by-key` policies remain
configured while the gateway exists, but the daily budgets no longer depend on
them.

**The budget kill switch is durable.** It is the last line of cost protection,
so it is not a boolean in a replica that scales to zero — it is one row in the
shared `CloudAuth` table, read on the admission path of every request.

*Two levels, matching the two budget thresholds.* `public_enabled` is the wider
one: every route, read or write, checks it, so clearing it stops the
deployment. `writes_enabled` stops only the sync plane's admissions. They are
stored independently, because the 80% and 100% actions fire independently and
may arrive in either order.

*It lives in `CloudAuth`, not in the plane's own quota table.* The counters
split by plane because each identity may write only its own table; a kill
switch that split the same way would be two switches, and throwing one would
leave the other plane serving. Every plane can *read* `CloudAuth`, so every
plane obeys the switch; only the read identity and an operator can write it,
so the sync plane cannot touch the switch it obeys.

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

*Clearing it is an update, never a delete.* No deployed managed identity holds
a table `entities/delete` action, so clearing writes both levels enabled back
into the same row — the same constraint that made the quota counters reclaim
their row in place.

*Operator path.* `wattracker.cloud` exports `read_kill_switch`,
`set_kill_switch`, `disable_writes` (the 80% action), `disable_public_api` (the
100% action), and `clear_kill_switch`. They take a state backend rather than a
running app, so the switch can be thrown and cleared while every replica is
scaled to zero. `set_kill_switch` requires both levels: a partial update would
have to read the level it is not changing, and a read that fails during an
incident is exactly when the write most needs to land. `disable_writes` does
read first, so a late 80% action cannot re-enable a public API that 100%
already disabled, and it raises rather than guessing when that read fails;
`disable_public_api` reads nothing, because it only ever removes capability and
must work when nothing can be read. The operator CLI in #169 is the intended
caller of all five.

`CloudState.create(..., require_persistent_security=True)` refuses a
process-local kill switch at boot, for the same reason it refuses non-durable
quota counters: in a scale-to-zero deployment a process-local switch does not
merely forget, it re-enables.

Operators must also alert on APIM 4xx/5xx, auth
failures, sync conflicts, queue age, storage availability, and Container App
restarts. Budgets and quotas reduce surprise
but do not guarantee a zero bill: Azure may charge for provisioned services,
private endpoints, DNS, egress, monitoring, and usage before an alert fires.
The 80% action disables new write forwarding; the 100% action disables public
APIs. The action endpoint is a deployment-supplied, authenticated automation
hook and must be exercised before production. A budget notification alone is
not a hard billing ceiling.

## Acceptance checklist

- [ ] `az deployment group validate` and Bicep build pass with tenant values.
- [ ] Confirm APIM is reachable over HTTPS, the managed environment is
      internal, both app ingresses are non-external, and APIM resolves and
      reaches both private Container App FQDNs.
- [ ] Confirm storage public network access, anonymous blobs, Shared Key, and
      HTTP are disabled; TLS 1.2 is enforced.
- [ ] Resolve Blob/Table names through the private DNS zones from both apps.
- [ ] Verify each managed identity has only its documented data-plane role.
- [ ] Verify enrollment rejects an unapproved tenant/device and pairing tokens
      are one-time, expiring, revocable, and absent from logs.
- [ ] Exercise read, push, status, CORS, subscription quota/rate limits, and
      the 503 kill switch.
- [ ] Drill the kill switch against a *cold* deployment: throw it, scale every
      replica to zero, and confirm the next request is still refused. A drill
      against a warm replica proves only that the cache was invalidated.
- [ ] Test offline queueing, restart/retry, idempotent replay, and conflict
      reporting without blocking local use.
- [ ] Trigger budget thresholds in a non-production subscription and confirm
      billing/on-call delivery; document real monthly estimates.
