# Cloud sync — security review follow-ups

Working document for the remaining work on PR #92 (`agent2/issue59`). Delete this file in the
commit that closes the last open item.

Baseline: PR head `307b5b3`, in the dedicated `agent2/pr92-readiness` worktree. The PR was
`MERGEABLE`/`CLEAN` with no reported checks or formal approval.

## Current execution plan

Step 1 is complete: work continues from PR head `307b5b3` in a dedicated worktree. Steps 2--4
are implemented here; the remaining work is verification and review:

1. Run the cloud-enabled suite, image/runtime checks, Bicep validation, and `git diff --check`.
2. Request a fresh security review and approval.

For this three-user deployment, do not provision Azure resources or spend time changing
APIM/VNet-specific infrastructure until the deployment profile is chosen. The preferred
lean profile should be evaluated before retaining the current enterprise-shaped topology.
If GitHub Actions billing remains disabled, record the local-only validation gate rather
than treating a non-running workflow as green.

Local validation so far: all 50 cloud tests pass with `.[cloud]`; the broader suite passes
`2181` tests with `13` expected skips when the two socket-restricted test files are excluded.
This host has no Docker or Azure CLI, so the workflow's image import/build and Bicep steps remain
to be exercised in CI or on a host with those tools.

## How to work in this repo

- **Use your own git worktree.** Two agents working in the primary checkout at once has already
  caused one near-miss on this PR: the same five findings were fixed twice in parallel on
  divergent branches, and the weaker fix was pushed first.
- **Rebase before you start.** `git fetch && git rebase origin/main`. Do not work from a base
  older than current `main`.
- **Run pytest with `PYTHONPATH` pinned to your worktree root.** Without it pytest silently
  imports the primary checkout's code and you will be testing the wrong tree:
  ```
  cd <worktree> && PYTHONPATH=<worktree> <venv>/bin/python -m pytest tests/ -q
  ```
- **Never run ad-hoc scripts that touch the database.** Only pytest is DB-isolated via
  `conftest.py`; standalone scripts hit the real user database.
- Do not weaken an assertion to make a test pass. If a ported or existing test no longer makes
  sense, say so explicitly rather than quietly relaxing it.

## Already resolved — do not redo

All five blockers from the security review are fixed and verified in `6431b82` (rebased as
`a75e5b9`), plus `b90f87b`:

| ID | Finding | Resolution |
|----|---------|-----------|
| B1 | Replay guard used the client's `X-Writer-Timestamp` as its clock; pruning was global, so one writer could expire another's nonce | `now=now` at the `nonces.accept` call site; `MIN_REPLAY_TTL_SECONDS = 600`; `CloudConfig.replay_ttl_seconds` validated against the freshness window; durable `claim_replay` on a `CloudReplay` table with etag-guarded compare-and-swap |
| B2 | Public container ingress; "mTLS" was a forgeable `x-apim-client-certificate-verified` header | `require_mtls`/`mtls_header` deleted outright; ACA environment `internal: true`, `external: false` on both apps |
| B3 | `validate-client-certificate` bound no identity | Policy and the cert-as-auth-factor claim removed; APIM on Standard SKU with `virtualNetworkType: 'External'` |
| B4 | Daily quotas were per-process dicts under `minReplicas: 0` | `QuotaManager.durable = False`, documented as a best-effort backstop with APIM `quota-by-key` as the durable control |
| B5 | `hmac.compare_digest` raised `TypeError` on non-ASCII latin-1 headers, including a pre-auth site on every request | `_safe_compare_text` at all three sites; the `marker == "true"` fallback in `_apim_proof_valid` also removed |

Regression coverage for B1/B5 lives in `tests/test_cloud_api.py`
(`test_future_skewed_nonce_is_not_pruned_early` and the non-ASCII header tests) and
`tests/test_cloud_deployment.py` asserts the bicep posture. `test_future_skewed_nonce_is_not_pruned_early`
was mutation-checked: reverting `now=now` to `now=timestamp` makes it fail.

Also resolved: the branch previously carried a duplicate `Bound forward FTP rescoring memory`
commit. That work landed independently on `main` (`9502152`, `ca61a7d`, `eadb38e`) and main's
version is authoritative. It has been dropped from this branch. **Do not resurrect it.**

---

## Completed in this worktree, pending final verification

### Cloud readiness

- `Dockerfile.cloud` explicitly installs `.[cloud]`, runs the cloud runtime, and has a dependency
  import health check; the ordinary local-server `Dockerfile` is unchanged.
- `.github/workflows/cloud.yml` installs `.[dev,cloud]`, runs the full suite, validates Bicep, builds
  the cloud image, and imports the Azure/cloud runtime inside that image.
- The restart/enrollment coverage now performs a real Ed25519 `POST /api/v1/sync/batches`; the
  existing HMAC batch path remains covered separately.
- If Actions billing remains disabled, the workflow is a declared gate but its local equivalent is
  still required; a non-running workflow is not evidence of a green check.

### M1 — public-key-to-HMAC credential path removed

`EnrollmentRegistry.consume` now returns only an `InvitationBinding`. It has no `public_key` branch,
and the regression test proves the removed call shape cannot manufacture an HMAC writer.

### M2 — signed sync status

`GET /api/v1/sync/status` now verifies the writer timestamp, nonce, empty-body digest, canonical
request signature, and replay claim using the credential's trusted algorithm. Tests cover missing,
tampered, and replayed status requests.

### M3 — server-generated writer secret

Enrollment always takes the random subscription-key path in `CredentialRegistry.enroll_writer`. The
APIM subscription header is independent and is never copied into the writer credential.

### M4 — unused cleanup identity removed

The Bicep deployment no longer creates `cleanupIdentity` or its Blob/Table contributor assignments.
Retention and recovery deletion remain a separate future cleanup-job feature.

### Binary runbook

The redundant `docs/azure-cloud-sync-secure-deployment-runbook.docx` was removed. Markdown remains
the reviewable source of truth.

### Reader-context refresh — closed by #151

Reader contexts still expire in 300 s, but re-issuance no longer requires the operator token. A
paired device holds a durable `DeviceCredential` (public key bound to `(namespace,
local_user_scope)` plus an explicit `capabilities` set) and trades it for a fresh context at
`POST /api/v1/context/refresh`, signed with the existing `canonical_request` framing and guarded by
the same 300 s freshness window and nonce-replay claim as the writer path. The TTL was deliberately
**not** widened and tokens are still never shared: the fix was to make re-issuance cheap, not to
make the token long-lived.

Two consequences worth knowing:

- The read plane now consumes replay nonces, so `CloudState.create` requires a replay backend on
  every plane. The read plane's claims go to `CloudAuth`, which its managed identity already
  writes; it still never opens `CloudReplay`, for which it holds no role. No Bicep change was
  needed. `test_container_runtime_read_plane_does_not_open_replay_table` still pins the
  no-`CloudReplay` invariant; only its incidental "no replay backend at all" assertion moved.
- `ecdsa-p256-sha256` joins `hmac-sha256` and `ed25519`, because the Secure Enclave is P-256 only.
  Signatures are raw `r || s` as exactly 128 hex characters and the algorithm is still selected
  from stored credential state, never from the wire. Ed25519 signatures are the same length, so
  that selection is now load-bearing in a second way, not just against HMAC downgrade.

### Disjoint namespaces per enrollment — decided by #152

**Decision: one rider, one namespace, and `installation_id` reuse is never the mechanism.**

Each `enrollment/start` still mints a fresh random installation id, and a namespace is still
`HMAC(server_secret, installation_id)` — that derivation is unchanged and must stay unchanged.
The pressure this item predicted, to reuse installation ids so a rider's second device lands in
the first one's namespace, is refused: reusing an id would make the namespace a function of a
value that has, at some point, been outside the server, and would turn a leaked or guessed id
into an account selector.

Instead, a second device never enrolls at all. The desktop, which already holds a writer
credential bound to `(namespace, local_user_scope)`, mints a single-use pairing code bound to
*its own* binding (`POST /api/v1/devices/pairing-codes`), and the device redeems it
(`POST /api/v1/devices/pair`) for a `DeviceCredential` in that same namespace and scope. The
device supplies only a public key; every partition-naming field it might send is ignored, the
way `SyncBatch.from_wire` ignores a client-supplied `installation_id`. Two enrollments still
produce two disjoint namespaces — that is now correct rather than a wart, because two
enrollments mean two riders.

See `docs/cloud-sync.md`, "Pairing a second device, and the same-namespace-per-rider rule", for
the flow, the indistinguishability rules, and the 60-bit code entropy arithmetic.

Pairing deliberately depends on nothing a gateway provides. `POST /api/v1/devices/pair` does
not require a verified subject: the code is the authorization, and requiring an identity provider
on the phone would defeat the point of a code read off the desktop. Where a gateway does attest a
subject it is applied as an additional binding on top of the code — and
`CloudState.create(..., require_persistent_security=True)` now refuses to boot if a deployment
claims `require_verified_subject` while nothing attests one, so removing the gateway is a
configuration change rather than a silent downgrade.

Still open in the same area, and deliberately not in #152's scope:

- APIM does not declare the `/devices/*` operations, so neither route is reachable through the
  gateway until #165 adds them. Both are exercised end to end against the ASGI app.
- **If #164 removes the gateway, `enrollment/start` and `enrollment/complete` still require a
  verified-subject header unconditionally, and no JWT is validated to produce it.** Those routes
  are operator-token gated and their real secret is the one-time invitation, so this is not an
  open hole, but the subject check there becomes decorative and should be either removed or
  re-anchored in the same change. Pairing, reads, and refresh already handle a gateway-less
  deployment.
- There is still no revocation route, so a lost paired device is revoked only by
  `CredentialRegistry.revoke_device` in library code (#153).

## Remaining open work, in priority order

### Smaller items

- `api.py:430-432` — comment says `signing_namespace` is "not a storage partition key"; `storage.py:315`
  builds the partition as `f"{namespace}:{local_user_scope}"`. Fix the comment; it misstates the
  trust model.
- No revocation route exists in `create_cloud_app` despite `docs/cloud-sync.md` claiming tokens are
  revocable. `revoke_writer`/`revoke_reader`/`revoke_device` are library-only, and the sync
  identity's CloudAuth RBAC is read-only (`main.bicep:330-334`), so it could not persist a
  revocation anyway. Either wire it up or correct the doc. The read identity *can* write CloudAuth,
  so a revocation route on the read plane is the cheap version of this.
- Replay-nonce rows written by the read plane land in `CloudAuth` and, like expired
  invitation/context rows, are never deleted. A device refreshing every four minutes adds roughly
  360 rows a day. Harmless at three users; fold it into whatever cleanup job the row-growth item
  below gets.
- `storage.py:301,305` — `_is_conflict`/`_not_found` substring-match `str(exc).lower()`; a 403 whose
  message contains "not found" silently becomes a missing object.
- `api.py:210,148` — `HTTPException` responses skip the `Cache-Control: no-store` header that
  `_error`/`_not_found` set.
- `limits.py:69` + `api.py:469,497,559` — global `max_backend_concurrency: 2`; two slow backend
  calls 429 every other tenant.
- `api.py:63` — `operator_token` minimum length is 8 characters.
- `main.bicep:354` — `stagingEnvironmentPolicy: 'Enabled'` creates publicly reachable PR preview
  environments.
- Expired invitation/context rows in `CloudAuth` are never deleted (no role has a delete action);
  the table grows monotonically.

### 7. Drop the binary runbook

`azure-cloud-sync-secure-deployment-runbook.docx` is a 49 KB binary — undiffable and unreviewable
in git, and it is a *security* runbook, the thing most in need of review. `tests/test_cloud_deployment.py`
already asserts against `docs/cloud-sync.md`, so the markdown is the source of truth and the
`.docx` is redundant as well as opaque.

**Do:** delete it; fold anything it contains that markdown lacks into `docs/cloud-sync.md`.

---

## Verified clean — no work needed

Probed specifically during review; do not spend time re-auditing these without new evidence:

- **Tenant isolation.** Every storage call takes `(namespace, local_user_scope)` from server-verified
  state; `SyncBatch.from_wire` parses and discards client-supplied `installation_id`,
  `local_user_scope`, and `partition_key`. Writer A's object confirmed invisible to reader B on both
  collection and detail routes.
- **Path traversal.** `_partition`/`_row_key`/`_batch_row`/`_blob_name` regex-validate against a
  charset that cannot express `/`, `:`, a `..` prefix, or a quote.
- **Injection.** Table `query_filter` interpolates only 64-hex namespaces and charset-restricted
  scopes; `db.py` uses bound parameters and scopes both SELECT and UPDATE by `user_id`.
- **Algorithm confusion.** The algorithm comes from the stored credential, never the wire. Missing
  `cryptography` returns `False`, not `True`.
- **Decompression.** A 400 MiB gzip bomb is refused with 413 at 64 MiB peak allocation.
- **Secrets.** No hardcoded credentials, no logging in `wattracker/cloud/`, no secret or exception
  text in any response body.
- **Storage account posture.** Public network access disabled, shared key disabled, HTTPS-only,
  TLS 1.2 minimum, deny-by-default network ACLs with `bypass: 'None'`, private endpoints and DNS
  for blob and table, least-privilege custom data-plane roles without delete for the sync identity.
