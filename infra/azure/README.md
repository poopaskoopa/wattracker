# Azure cloud-sync contract

From the repository root, build the cloud runtime image with the cloud
dependency extra enabled:

```sh
docker build -f Dockerfile.cloud -t wattracker-cloud .
```

This Bicep is a review skeleton, not a zero-cost guarantee. Azure billing can
include egress, logs, storage transactions, Functions invocations, and quota
overages; budgets are alerts and the webhook is a durable safety action, not a
provider-enforced spend cap. Review the tenant-specific image, origin, identity,
and billing values before production. The gateway decision and its pricing
assumptions are recorded in [`docs/azure-gateway-decision.md`](../../docs/azure-gateway-decision.md).

## Current topology

- The Container Apps environment is VNet-integrated but public, and both apps
  expose HTTPS ingress with `allowInsecure: false`. There is no APIM, Front
  Door, custom certificate, or public reverse-proxy dependency.
- The app is the authentication boundary. Enrollment uses the operator token
  and one-time invitation; reads use reader contexts or paired-device
  credentials; writes use server-issued writer credentials and signed request
  envelopes. `X-Verified-Entra-Subject` and `X-Gateway-Request-Proof` are not
  production trust inputs.
- The VNet has an ACA infrastructure subnet using the `Microsoft.Storage`
  service endpoint. Storage keeps its public endpoint enabled because service
  endpoints use it, but its firewall is deny by default and allows only that
  subnet, the same-tenant budget Function resource instance, and the explicitly
  supplied Function egress IPs.
- Storage uses managed identity and Azure RBAC only. Shared keys, anonymous
  blobs, TLS below 1.2, private endpoints, and private DNS are not part of this
  profile. The read identity, sync identity, and budget-hook identity have
  separate least-privilege roles; no role grants table or blob deletion.

## Budget hook deployment contract

Provision the external Azure Functions Consumption app before applying this
template. The classic Consumption plan has no VNet integration, so obtain all
possible outbound IPv4 addresses from the Function resource and pass them as
`budgetHookIpRules`. Keep that list synchronized when the Function's hosting
resource changes. Stage and deploy `infra/azure/budget-hook` with a
system-assigned managed identity and these settings; see [Azure Functions networking
options](https://learn.microsoft.com/en-us/azure/azure-functions/functions-networking-options):

```sh
python scripts/package_budget_hook.py
cd build/azure-budget-hook
func azure functionapp publish APP_NAME
```

The staging command copies the repository's cloud package into the Function
project. A raw publish from `infra/azure/budget-hook` cannot resolve a parent
checkout, and the Function project intentionally has no editable parent
requirement.

- `WATTRACKER_STORAGE_ACCOUNT_NAME`: the storage account name.
- `WATTRACKER_BUDGET_HOOK_TOKEN`: a separate app-level token for the
  non-Functions test/operator seam; it is not the Azure Function key.

Pass the Function identity's object ID as `budgetHookPrincipalId`. Bicep injects
each Container App's user-assigned `AZURE_CLIENT_ID`; the budget Function uses
its system-assigned identity by default. Pass its resource name as
`budgetHookFunctionAppName` and its hostname (without a scheme, path, or query
string) as `budgetHookHost`. Bicep resolves the existing Function App's default
host key with `listKeys` at deployment time, constructs the two HTTPS
route-specific URLs, and appends the `code` query parameter. The deployment
principal therefore needs permission to list host keys for that Function App;
the key is never passed as a Bicep parameter. Rotate it with the Function
deployment and redeploy this template. Pass the Function's complete
possible outbound IP list as `budgetHookIpRules`; the Storage firewall also
allows the same-tenant Function resource instance, while its `bypass` remains
`None`. The fixed routes are:

Set `budgetStartDate` to the first day of the current budget period and
`budgetEndDate` to its end date explicitly on every deployment. These are
required parameters so a redeploy cannot silently reuse an obsolete period or
reset the budget window to the date the template was authored.

- `/budget/disable-writes`: at the 80% alert, persistently disables
  writes and leaves reads enabled, with reason `budget 80%`.
- `/budget/disable-public-api`: at the 100% alert, persistently
  disables the public API and writes, with reason `budget 100%`.
- `/budget/clear`: operator recovery; requires the Function host key and
  the `X-Wattracker-Budget-Token` app-level header and restores both levels.

The hook runs outside Container Apps so it remains callable when the public
API is disabled or scaled to zero. Its managed identity can read and upsert
only the durable kill-switch row in `CloudControl`; clearing the switch is an
explicit operator action that writes both levels enabled.

## Review checklist

- [ ] Images are signed and the cloud runtime imports successfully.
- [ ] Both Container Apps have external HTTPS ingress and application-level
      credentials/signature checks; no gateway headers are trusted.
- [ ] Storage firewall rules contain only the ACA subnet, the same-tenant
      budget-hook Function resource instance, and current Function egress IPs;
      shared-key and anonymous-blob access remain disabled.
- [ ] The Function's managed identity object ID is passed to Bicep, its full
      possible egress-IP list is current, and both callback URLs use the
      current default host key resolved by Bicep.
- [ ] A non-production budget drill confirms the 80% and 100% routes persist in
      `CloudControl`, survive an app restart, and are cleared explicitly.
