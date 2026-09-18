# Azure deployment runbook

This is an unexecuted, phased deployment procedure. Azure CLI, Bicep CLI,
Functions Core Tools, Docker, and an Azure subscription are unavailable or
unverified in this environment; **no Azure command below has been run**.

Facts explicitly visible in `main.bicep` (resources, dependencies, roles,
routes, and parameters) are template-derived. CLI/provider behavior, chosen
SKUs and runtime availability, permissions, networking, image publication,
and every command result must be confirmed during the first real deployment.

## Immutable cloud image handoff

The repository workflow `.github/workflows/cloud-publish.yml` builds
`Dockerfile.cloud` once for `linux/amd64`, publishes
`ghcr.io/poopaskoopa/wattracker-cloud:sha-<short-sha>`, signs the resulting
digest with keyless cosign through GitHub OIDC, and verifies that signature in
the same run. The existing `cloud.yml` workflow remains the fork-safe local
container build; the publishing workflow is separate so pull requests never
receive package-write or OIDC signing permissions.

To get the digest for a commit, find its successful publishing run and print
the immutable reference that the run logs and Summary contain:

```sh
COMMIT='FULL_COMMIT_SHA'
RUN_ID="$(gh run list --workflow cloud-publish.yml --commit "$COMMIT" --status success --limit 1 --json databaseId --jq '.[0].databaseId')"
gh run view "$RUN_ID" --log | rg 'ghcr.io/poopaskoopa/wattracker-cloud@sha256:'
```

Before deploying, verify the exact digest from a clean checkout or deployment
shell (replace the placeholder with the full `@sha256:...` reference printed
by the run):

```sh
IMAGE_REF='ghcr.io/poopaskoopa/wattracker-cloud@sha256:FULL_DIGEST'
cosign verify \
  --certificate-identity-regexp '^https://github\.com/poopaskoopa/wattracker/\.github/workflows/cloud-publish\.yml@refs/heads/main$' \
  --certificate-oidc-issuer 'https://token.actions.githubusercontent.com' \
  "$IMAGE_REF"
```

The one verified digest fills both `readImage` and `syncImage` in
`main.bicepparam`; the two Container Apps run the same entrypoint and differ
only by `WATTRACKER_CLOUD_PLANE`. Keep the parameter file's placeholders in
source control and copy the same full image reference—and therefore the same
exact digest—into both values only in the deployment copy.

The package is **public** and needs no registry pull credential. A package
published by Actions from a public repository inherits that repository's
visibility, so `wattracker-cloud` was public from its first push — there is no
visibility flip to perform, and `main.bicep` needs no `registries` block or
stored PAT. Verified 2026-09-18 by fetching the manifest from `ghcr.io` with an
anonymous pull token.

The published digest is an **OCI image index, not a single manifest**, because
`docker/build-push-action` attaches a provenance attestation by default. The
index for the first published digest holds two entries:

```text
application/vnd.oci.image.index.v1+json
  linux/amd64       sha256:ae4982dcb892bee2...
  unknown/unknown   sha256:3847548a06c26e79...   attestation-manifest
```

That is expected, and the `unknown/unknown` entry is the attestation rather
than a broken platform — a runtime selects `linux/amd64` and ignores it. Pin
the **index** digest (the one the run Summary prints), which is what cosign
signed and what the verify command above checks.

Confirm the digest actually pulls on Container Apps before completing a
deployment with it. If a pull path ever rejects the attestation-bearing index,
the fix is `provenance: false` in the build step followed by a re-publish and a
fresh digest — never a switch to a mutable tag, which would discard the
signature this whole path exists to produce.

## Prerequisites and secret handling

Have an Azure subscription; an owner-approved region, resource-group name,
globally unique storage name, PWA origin, billing email and budget period;
registry-backed immutable **signed** cloud image reference from the
successful #316 publishing run; and permission to create the resources, assign
roles, list Function host keys, and create Consumption budgets. The final
digest remains an owner deployment input even though its build and signature
are produced and verified by the repository workflow.

Install and authenticate Azure CLI with Bicep support, Azure Functions Core
Tools, and Docker in the real deployment environment. Do not copy this
skeleton as-is: replace every `TODO_...` in `main.bicepparam`, retaining no
tenant-specific values in source control. Keep secret values in the process
environment or an approved secret system, not in the parameter file or shell
history:

```sh
export WATTRACKER_CLOUD_SERVER_SECRET="$(openssl rand -base64 32)" # base64 256-bit material
export WATTRACKER_OPERATOR_TOKEN="$(openssl rand -hex 32)"         # 64 characters; template floor is 32
export WATTRACKER_STATIC_REPOSITORY_TOKEN=''                         # leave empty only while Static Web App is disabled
export WATTRACKER_BUDGET_HOOK_TOKEN="$(openssl rand -hex 32)" # store in the approved secret system
```

The Function host key is a separate platform secret. Do not put it in the
parameter file: `main.bicep` obtains the existing app's default host key with
`listKeys` when it creates the action-group callback URLs.

## Phase order and parameter handoffs

`main.bicep` declares the budget Function App as an existing resource and
uses it for both `listKeys` and the Storage `resourceAccessRules` entry. The
Function therefore must exist and have its system-assigned identity enabled
before the main deployment. Conversely, the Function needs the application
Storage account and its `CloudControl` table, which `main.bicep` creates,
before its settings are completed and the hook is published.

1. Select subscription/resource group and create bootstrap host storage plus
   the empty Consumption Function. This yields the Function name,
   `defaultHostName`, system identity `principalId`, and complete possible
   outbound IPv4 list for `budgetHookFunctionAppName`, `budgetHookHost`,
   `budgetHookPrincipalId`, and `budgetHookIpRules`.
2. Resolve owner inputs and the successful #316 publishing run. They yield
   `location`, `storageName`, `allowedOrigin`, `billingEmail`,
   `budgetStartDate`, `budgetEndDate`, and one signed image reference to use
   for both `readImage` and `syncImage` (plus, if used, Static Web App
   repository inputs).
3. Build, validate, review what-if, then create `main.bicep`. This creates
   the application Storage account and `CloudControl`, the VNet/ACA
   environment and apps, managed identities and RBAC, action groups, and
   budget. It also assigns the pre-existing Function identity the narrowly
   scoped CloudControl read/upsert role. Record the storage name and Container
   App endpoint/identity outputs obtained from Azure; the current template has
   no Bicep `output` declarations, so portal/`az` queries are required.
4. Set the Function's application Storage name and budget-hook token, stage
   and publish it, then verify the Function and execute a non-production drill.

The bootstrap storage is intentionally separate from the application storage:
a Consumption Function needs host storage before `main.bicep` can create the
application storage it will later access. The exact provider-supported
bootstrap storage/account and Consumption runtime setup must be confirmed on
the first deployment; do not infer that this has been tested here.

## 1. Bootstrap the existing Function App (unverified commands)

From a clean deployment shell, choose non-secret names. The following is an
expected Azure CLI flow to confirm against the installed CLI and subscription:

```sh
export SUBSCRIPTION_ID='TODO_SUBSCRIPTION_ID'
export RESOURCE_GROUP='TODO_RESOURCE_GROUP'
export LOCATION='TODO_RESOURCE_GROUP_LOCATION'
export BOOTSTRAP_STORAGE_NAME='TODO_UNIQUE_FUNCTION_HOST_STORAGE_NAME'
export FUNCTION_APP_NAME='TODO_UNIQUE_BUDGET_HOOK_FUNCTION_APP_NAME'

az login
az account set --subscription "$SUBSCRIPTION_ID"
az group create --name "$RESOURCE_GROUP" --location "$LOCATION"
az storage account create --name "$BOOTSTRAP_STORAGE_NAME" --resource-group "$RESOURCE_GROUP" --location "$LOCATION" --sku Standard_LRS
az functionapp create --name "$FUNCTION_APP_NAME" --resource-group "$RESOURCE_GROUP" --consumption-plan-location "$LOCATION" --storage-account "$BOOTSTRAP_STORAGE_NAME" --runtime python --runtime-version 3.11 --functions-version 4
az functionapp identity assign --name "$FUNCTION_APP_NAME" --resource-group "$RESOURCE_GROUP"

az functionapp show --name "$FUNCTION_APP_NAME" --resource-group "$RESOURCE_GROUP" --query '{name:name,host:defaultHostName,possibleOutboundIps:possibleOutboundIpAddresses}' --output json
az functionapp identity show --name "$FUNCTION_APP_NAME" --resource-group "$RESOURCE_GROUP" --query principalId --output tsv
```

Confirm the actual supported Python/runtime flags and the Function plan in the
first deployment. Put `defaultHostName` (without `https://`) and the app name
in the matching parameter entries. Split `possibleOutboundIpAddresses` into
one string per `budgetHookIpRules` array item; preserve every returned IPv4.
Put the identity `principalId` in `budgetHookPrincipalId`.

## 2. Complete parameters and deploy the main template (unverified commands)

Use the immutable signed image reference from the successful #316 publishing
run. It is one `linux/amd64` GHCR image, and its full `@sha256` digest must be
verified before deployment. Fill `readImage` and `syncImage` with that same
reference only after the run exists. Keep `staticRepositoryUrl = ''` to disable the
optional Static Web App, or supply owner-approved repository values and the
environment-provided deployment token.

```sh
cd infra/azure
az bicep build --file main.bicep
# main.bicepparam has using './main.bicep', so do not add --template-file here.
az deployment group validate --resource-group "$RESOURCE_GROUP" --parameters main.bicepparam
az deployment group what-if --resource-group "$RESOURCE_GROUP" --parameters main.bicepparam
az deployment group create --name wattracker-initial --resource-group "$RESOURCE_GROUP" --parameters main.bicepparam
```

Review the what-if before `create`, especially Storage firewall rules, Function
principal role assignment, action-group URLs, budget dates, role scopes, and
Container App image digests. Capture actual app endpoints and identity IDs,
for example:

```sh
az containerapp show --name wattracker-read --resource-group "$RESOURCE_GROUP" --query '{fqdn:properties.configuration.ingress.fqdn,identity:identity.userAssignedIdentities}' --output json
az containerapp show --name wattracker-sync --resource-group "$RESOURCE_GROUP" --query '{fqdn:properties.configuration.ingress.fqdn,identity:identity.userAssignedIdentities}' --output json

# Set this to the exact storageName value used in main.bicepparam.
export STORAGE_NAME='TODO_STORAGE_NAME'
```

## 3. Stage and publish the budget hook (unverified commands)

Run this from the repository root only after the main deployment has created
the application storage. Configure the Function settings before publishing so
the module-level settings checks in `function_app.py` can load the hook. The
staging helper refuses to overwrite an existing output directory. For an
intentional restage, remove **only**
`build/azure-budget-hook`, then stage again; do not delete a broader build
directory. Publish from the staged directory, not `infra/azure/budget-hook`.

```sh
az functionapp config appsettings set --name "$FUNCTION_APP_NAME" --resource-group "$RESOURCE_GROUP" --settings WATTRACKER_STORAGE_ACCOUNT_NAME="$STORAGE_NAME" WATTRACKER_BUDGET_HOOK_TOKEN="$WATTRACKER_BUDGET_HOOK_TOKEN"
python scripts/package_budget_hook.py
cd build/azure-budget-hook
func azure functionapp publish "$FUNCTION_APP_NAME"
cd ../..
```

For an explicit restage after a prior stage:

```sh
rm -rf -- build/azure-budget-hook
python scripts/package_budget_hook.py
cd build/azure-budget-hook
func azure functionapp publish "$FUNCTION_APP_NAME"
```

Confirm the Function's app settings, enabled system identity, logs, host key,
and its ability to reach the application storage before treating callbacks as
live. If the Function changes hosting resource or possible outbound IPs,
refresh `budgetHookIpRules` and redeploy `main.bicep`.

## 4. Non-production budget drill (unverified commands)

Run only against an isolated non-production subscription/resource group. Get a
Function host key through an approved secret path and call `/budget/clear`
with both host authentication (`x-functions-key`, or `?code=`) and the
independent app token. A successful response is exactly `{"status":"ok"}`
with HTTP 200:

```sh
export FUNCTION_HOST='TODO_FUNCTION_DEFAULT_HOSTNAME'
export FUNCTION_HOST_KEY='RETRIEVE_OUTSIDE_SOURCE_CONTROL'
curl --fail-with-body --request POST "https://$FUNCTION_HOST/budget/clear" \
  --header "x-functions-key: $FUNCTION_HOST_KEY" \
  --header "X-Wattracker-Budget-Token: $WATTRACKER_BUDGET_HOOK_TOKEN"

WATTRACKER_KILL_SWITCH_ROW_KEY="$(python -c 'import hashlib; print("kill-switch:" + hashlib.sha256(b"wattracker-cloud-kill-switch-v1\x00deployment").hexdigest())')"
az storage entity show --account-name "$STORAGE_NAME" --table-name CloudControl --auth-mode login \
  --partition-key '__wattracker_auth_v1__' --row-key "$WATTRACKER_KILL_SWITCH_ROW_KEY" \
  --select PartitionKey RowKey Payload --output json
```

The source derives the expected entity as `PartitionKey` =
`__wattracker_auth_v1__` and `RowKey` = `kill-switch:` plus the SHA-256 of
`b"wattracker-cloud-kill-switch-v1\x00deployment"`. Inspect the returned
payload for the enabled write/public levels and `operator clear` reason. The
query/portal view needs an Entra principal with Storage Table Data Reader on
`CloudControl` and is the proof that the durable row landed; a 200 with no
CloudControl row is a failed drill, not evidence of recovery.

This drill proves or refutes two template assumptions: that
`Microsoft.Web/sites` `resourceAccessRules` is eligible for this Function
resource, and that the Function identity has the CloudControl Table
Insert-Or-Merge (`upsert_entity`) write permission. A non-production
isolation/retest may be necessary because the Function IP allow-list can mask
whether the resource-instance rule itself works.

Missing or invalid Function keys are rejected by the Functions host; a missing
or invalid app token is rejected by the app. Storage RBAC or firewall denial
during `budget_hook.py` apply/clear is surfaced as HTTP 503, as can a network
failure. The same outside HTTP error can therefore represent firewall denial:
inspect Function logs and the CloudControl row to distinguish it. Treat HTTP
200 without the row as failure and investigate logs, storage data-plane RBAC,
firewall/IP/resource-instance rules, and connectivity.

## Actual-deployment evidence checklist

- [ ] `az account show` identifies the intended non-production subscription.
- [ ] Bicep build, validate, reviewed what-if, and create outputs are saved.
- [ ] The existing Function name, host, identity principal ID, and all possible
      outbound IPv4 addresses match `main.bicepparam`.
- [ ] Deployment activity shows the Function host-key lookup, role assignment,
      firewall/resource-instance rule, tables, action groups, budget, and
      Container Apps succeeded.
- [ ] The one immutable signed image reference was produced by the #316
      publishing run, verified with the documented cosign command, and copied
      identically into `readImage` and `syncImage`.
- [ ] Function settings/publish logs show the staged package was published and
      the Function can access CloudControl with managed identity.
- [ ] The authenticated clear drill returned 200 JSON and the CloudControl row
      payload proves both levels enabled; any rule-isolation retest is recorded.
