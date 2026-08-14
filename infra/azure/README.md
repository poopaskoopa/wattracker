# Azure cloud-sync contract

From the repository root, build the cloud runtime image with the cloud
dependency extra explicitly enabled:

```sh
docker build -f Dockerfile.cloud -t wattracker-cloud .
```

This Bicep is a review skeleton, not a zero-cost guarantee. Azure billing can
include DNS, private endpoints, APIM, egress, logs, and quota overages; budgets
are alerts, not spend caps. The deployment must be reviewed with tenant-specific
identity, certificate, image, origin, and billing values before production.
Supply signed `readImage`/`syncImage` values, a base64 256-bit
`cloudServerSecret`, and an `operatorToken` at deployment time; none have
repository defaults.

## Security invariants

- APIM is the only user-facing boundary. The Container Apps managed
  environment is internal and both app ingresses are non-external; their
  private FQDNs are reachable by the APIM Standard External VNet integration,
  while direct public origin requests have no route.
- Storage has public network access, blob anonymous access, Shared Key access,
  and default network access disabled; HTTPS and TLS 1.2 are mandatory.
- The read identity has Blob/Table Data Reader access only on the object
  container/table plus a manager role on the separate `CloudAuth` table. The
  sync identity has custom Blob/Table writer roles with no delete action,
  read-only access to `CloudAuth`, and a narrowly scoped replay-claim writer
  role on `CloudReplay`. Retention and recovery deletion require a separate
  cleanup job, which is not part of this deployment. No account keys are used.
- APIM subscriptions require approval, are limited to one per product, and
  enforce quotas/rate limits, explicit CORS, and a deployment kill switch.
- APIM's managed identity receives only Key Vault Secrets User access to the
  named certificate secret; no storage or database role is granted to APIM.
- APIM injects a deployment-supplied private proof value into backend requests;
  the services do not trust caller-controlled certificate-verification or
  boolean headers. The proof is the gateway-to-origin trust factor.
- The signed container images built from `Dockerfile.cloud` run
  `python -m wattracker.cloud.runtime`; the
  runtime constructs `AzureTenantStore` with managed identity. Bicep supplies
  image inputs and injects only the server secret, operator token, and APIM
  proof value into the containers.
- Images, certificate URIs, allowed origins, publisher/billing contacts, and
  tenant-specific enrollment configuration are placeholders—never commit
  credentials or fake secrets.
