# Cloud sync — security review follow-ups

Working document for the remaining work on PR #92 (`agent2/issue59`). Delete this file in the
commit that closes the last open item.

Baseline: PR head `b90f87b`, rebased onto `main`, `MERGEABLE`/`CLEAN`, suite green at
**2216 passed / 4 skipped**.

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

## Open work, in priority order

### 1. CI runs none of this — highest value, do this first

Nothing verifies this PR except someone running pytest by hand. `gh pr checks 92` reports no
checks at all.

- Only `.github/workflows/windows.yml` triggers on `pull_request`.
- Every workflow installs `.[dev]`, never `.[cloud]`, so `cryptography` is absent.
- Consequence: `tests/test_cloud_api.py::test_read_enrollment_is_usable_by_restarted_sync_and_read_planes`
  — the only Ed25519 enrollment test — is **always skipped in CI**.
- Roughly 40 cloud tests run nowhere.

Also: every end-to-end `POST /api/v1/sync/batches` test builds its writer via `register_writer(...)`,
which produces `hmac-sha256`. **No test ever pushes a batch with an Ed25519 credential**, which is
what `enroll_writer` — the only production path — issues. If `cryptography` is missing from the
container image, `verify_signature` returns `False` silently (`security.py:418-421`) and every
write 401s. Fail-closed, but a total outage no test would catch.

**Do:** add a Linux `pull_request` job installing `.[cloud]` and running the full suite; add one
end-to-end batch test using an Ed25519 credential.

**Also confirm:** that the `cloud` extra is actually installed in `readImage`/`syncImage`. Nothing
in this repo enforces it. If it isn't, that is a production outage waiting to happen and is
higher priority than everything else on this list.

### 2. M1 — delete the "HMAC secret is a public key" branch

`wattracker/cloud/security.py:882-891`: `EnrollmentRegistry.consume(token, public_key=...)` returns
a credential with `verification_key = <caller-supplied public key>` and
`signature_algorithm = "hmac-sha256"`. Anyone who learns that public key can forge signatures.
This contradicts the `verify_signature` docstring at `security.py:404`.

Not reachable over HTTP today — `api.py` calls `consume(token, subject=subject)` without a key and
never registers the result — but `tests/test_cloud_security.py:110` asserts it succeeds, which
locks the unsafe shape in.

**Do:** remove the `public_key` branch (or make it emit `ed25519`) and invert that test.

### 3. M2 — `GET /api/v1/sync/status` has no request signature

`_writer_auth` alone: credential id + subscription key digest, no proof of possession, no nonce, no
timestamp. Returns scope revision and full quota counters. Both factors are bearer values sent on
every request. It is the one signed-plane route where the signature was dropped.

**Do:** sign it (GET with empty-body digest), or document it as a deliberately weaker read.

### 4. M3 — the writer's second factor is client-chosen at enrollment

`api.py:418`: whatever the caller sends as `Ocp-Apim-Subscription-Key` at enrollment becomes the
stored writer secret, making it identical to the APIM subscription key — one leak compromises both
layers.

**Do:** use the server-generated random key (the `None` branch) unconditionally; treat the APIM key
as an independent APIM-layer control.

### 5. M4 — standing high-privilege identity with no consumer

`infra/azure/main.bicep:171,335-344`: `cleanupIdentity` holds Storage Blob Data Contributor and
Table Data Contributor (including delete) and nothing uses it — a dormant principal that can delete
every tenant's objects.

**Do:** add the cleanup job that owns it, or remove the identity and its role assignments until
that job exists.

### 6. Smaller items

- `api.py:430-432` — comment says `signing_namespace` is "not a storage partition key"; `storage.py:315`
  builds the partition as `f"{namespace}:{local_user_scope}"`. Fix the comment; it misstates the
  trust model.
- Reader contexts expire in 300 s with **no refresh endpoint** (`security.py:27`, `api.py:423`);
  re-issuance requires the operator token, so the read plane is unusable past five minutes. Add
  refresh before someone widens the TTL or starts sharing tokens.
- No revocation route exists in `create_cloud_app` despite `docs/cloud-sync.md` claiming tokens are
  revocable. `revoke_writer`/`revoke_reader` are library-only, and the sync identity's CloudAuth
  RBAC is read-only (`main.bicep:330-334`), so it could not persist a revocation anyway. Either
  wire it up or correct the doc.
- `storage.py:301,305` — `_is_conflict`/`_not_found` substring-match `str(exc).lower()`; a 403 whose
  message contains "not found" silently becomes a missing object.
- `api.py:210,148` — `HTTPException` responses skip the `Cache-Control: no-store` header that
  `_error`/`_not_found` set.
- `limits.py:69` + `api.py:469,497,559` — global `max_backend_concurrency: 2`; two slow backend
  calls 429 every other tenant.
- `api.py:63` — `operator_token` minimum length is 8 characters.
- `main.bicep:354` — `stagingEnvironmentPolicy: 'Enabled'` creates publicly reachable PR preview
  environments.
- Each `enrollment/start` mints a fresh random installation id (`api.py:367`), so the same Entra
  subject enrolling twice gets two disjoint namespaces with no shared data. Expect pressure to
  reuse installation ids, which would collapse the namespace derivation — decide the intended
  behaviour now.
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
