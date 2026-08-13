# Cloud sync operations contract

## Boundary and identities

APIM is the sole public HTTPS boundary. Clients use an approved APIM
subscription and tenant-issued enrollment/pairing token. Registration is not
open: an operator creates or approves an enrollment, binds the device to the
tenant/user, and issues a short-lived one-time pairing token. Tokens are
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
for writes, and APIM-validated Entra JWT plus a bound reader context for reads.
Certificate presence is not an application authentication factor.

## Public routes and controls

The published API is HTTPS-only and subscription-protected. The versioned
contract covers `POST /api/v1/enrollment/start`,
`POST /api/v1/enrollment/complete`, `GET /api/v1/context`,
`GET /api/v1/context/calendar`, `GET /api/v1/context/activities`,
`GET /api/v1/context/activities/{id}`, `GET /api/v1/context/races`,
`GET /api/v1/sync/status`, and `POST /api/v1/sync/batches`. Enrollment and
pairing validate tenant, user/device binding, expiry, nonce, and replay state.
APIM applies an allow-listed CORS origin, 60 requests/minute and 1,000
requests/day per subscription. Set the deployment kill switch to return 503
before emergency maintenance.

Both Container Apps use `minReplicas=0` and `maxReplicas=1`; cold starts and
single-instance throughput are accepted operational tradeoffs.

## Local offline contract

The desktop app remains the source of truth while offline. The opt-in client
builds bounded, idempotent batches from a separate read-only SQLite connection;
an enclosing worker may queue those batches and retry with bounded exponential
backoff. A failed sync returns `Cloud sync offline — local data and features
are unaffected.` and never blocks local rides, planning, import, or export.
Pairing is explicit and revocable; local data is not deleted because cloud sync
is unavailable.

The deployed container entrypoint is `python -m wattracker.cloud.runtime`. It
constructs `AzureTenantStore` with managed identity and private Blob/Table
endpoints; shared credential/context state is persisted in the separate
`CloudAuth` table and replay claims in `CloudReplay`. The in-memory stores are
test-only. Images, server secret,
operator token, APIM proof secret, storage account, and exact origin are
deployment inputs. APIM overwrites a private proof header on every backend
request; containers reject requests that merely forge a boolean marker or
certificate header.

## Operations and cost

The Bicep budget alerts at 50%, 80%, and 100% actual usage. APIM's quota and
rate-limit policies are the durable production request limits. The app-side
quota counters are best-effort process-local backstops and reset after restart
or replica replacement. Operators must also alert on APIM 4xx/5xx, auth
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
- [ ] Test offline queueing, restart/retry, idempotent replay, and conflict
      reporting without blocking local use.
- [ ] Trigger budget thresholds in a non-production subscription and confirm
      billing/on-call delivery; document real monthly estimates.
