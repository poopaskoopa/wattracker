# Azure cloud-sync contract

This Bicep is a review skeleton, not a zero-cost guarantee. Azure billing can
include DNS, private endpoints, APIM, egress, logs, and quota overages; budgets
are alerts, not spend caps. The deployment must be reviewed with tenant-specific
identity, certificate, image, origin, and billing values before production.
Supply signed `readImage`/`syncImage` values, a base64 256-bit
`cloudServerSecret`, and an `operatorToken` at deployment time; none have
repository defaults.

## Security invariants

- APIM is the only user-facing boundary. Container App ingress is reachable
  only with the APIM client certificate; direct requests without that
  certificate are rejected before the application or Storage is reached.
- Storage has public network access, blob anonymous access, Shared Key access,
  and default network access disabled; HTTPS and TLS 1.2 are mandatory.
- The read identity has Blob/Table Data Reader access only on the object
  container/table plus a manager role on the separate `CloudAuth` table. The
  sync identity has custom Blob/Table writer roles with no delete action and
  read-only access to `CloudAuth`; a separate cleanup identity alone has the
  built-in contributor roles needed for retention and recovery deletion. No
  account keys are used.
- APIM subscriptions require approval, are limited to one per product, and
  enforce quotas/rate limits, explicit CORS, and a deployment kill switch.
- APIM's managed identity receives only Key Vault Secrets User access to the
  named certificate secret; no storage or database role is granted to APIM.
- APIM injects a deployment-supplied private proof value into backend requests;
  the services do not trust a caller-controlled boolean header.
- The outbound APIM client certificate is a separately installed/rotated
  certificate referenced by ID in policy; it is not the caller certificate.
- The signed container images run `python -m wattracker.cloud.runtime`; the
  runtime constructs `AzureTenantStore` with managed identity. Bicep supplies
  image inputs and injects only the server secret, operator token, and APIM
  proof value into the containers.
- Images, certificate URIs, allowed origins, publisher/billing contacts, and
  tenant-specific enrollment configuration are placeholders—never commit
  credentials or fake secrets.
