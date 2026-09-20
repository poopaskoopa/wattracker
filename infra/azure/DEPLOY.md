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

## Normal path: reconcile and deploy explicitly

After the deployment parameter file is complete, run the reconciler from a
checkout of the current `main` commit with no changes except that explicit
untracked parameter file. It requires the arm64 Homebrew
GitHub CLI at `/opt/homebrew/bin/gh`, uses the existing operator CLI's endpoint
and token loading, and never prints parameter contents, credentials, or cloud
response bodies:

```sh
.venv/bin/python scripts/deploy_cloud.py infra/azure/main.local.bicepparam \
  --resource-group "$RESOURCE_GROUP"
```

The command exits without deploying when the image is current or when the
published-to-main gap is docs/tests/infra-only. For image drift it resolves a
digest only from a successful `cloud-publish.yml` run for current `main`,
updates both image parameters atomically, runs Azure `validate` before
`create`, and prints the commit reported by `admin version`. It refuses a dirty
checkout or a checkout other than `main`; a failed post-update step restores
the parameter file byte-for-byte. These checks and the focused tests are the
only verification performed in this environment; no Azure command or live
subscription deployment has been run here.

Before `az deployment group create`, run the read-only drift check against the
untracked local parameter file and the commit intended for deployment:

```sh
DEPLOYMENT_COMMIT='FULL_COMMIT_SHA'
python scripts/check_cloud_image_drift.py infra/azure/main.local.bicepparam \
  --deployment-commit "$DEPLOYMENT_COMMIT"
```

The checker resolves the full digest through successful `main` publishing runs
and reports the published commit, current `main` commit, run ID, and how many
commits `main` is ahead or behind. A nonzero `main ahead` count means the
deployment would still succeed but would deliberately run an older image;
review and choose the image deliberately. The command returns success when the
digest matches the explicitly requested deployment commit, even when that
commit is intentionally older than `main`. It never edits the parameter file
or updates a digest, and it does not verify Azure state. Keep the cosign
command above as the signature check.

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
Flex Consumption availability in `eastus2` must be confirmed by the owner
with `az functionapp list-flexconsumption-locations` and by the deployment;
registry-backed immutable **signed** cloud image reference from the
successful #316 publishing run; and permission to create the resources, assign
roles, list Function host keys, and create Azure budgets. The final
digest remains an owner deployment input even though its build and signature
are produced and verified by the repository workflow.

Install and authenticate Azure CLI with Bicep support, Azure Functions Core
Tools, and Docker in the real deployment environment. Do not copy this
skeleton as-is: replace every `TODO_...` in `main.bicepparam`, retaining no
tenant-specific values in source control. Keep secret values in the process
environment or an approved secret system, not in the parameter file or shell
history:

Azure Functions Core Tools is not in Homebrew core. The Homebrew route is
`brew tap azure/functions`, `brew trust azure/functions`, then
`brew install azure-functions-core-tools@4`; on the owner's machine that
installation path failed while the installed Command Line Tools were
outdated. As a prebuilt alternative that worked without that toolchain issue,
use
`npm install --global azure-functions-core-tools@4`.

During Function publication, a warning that the local Python version differs
from the remote app's configured version is expected. For the owner's deployed
`Python|3.12` app, Oryx performed the remote build with 3.12.14; do not install
another local Python solely to remove that warning.

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
uses it to obtain the host key for the action-group callback URLs. The budget
Function is an externally bootstrapped Flex Consumption app integrated with
the dedicated `budget-hook-flex` subnet that this template declares. Storage
access is admitted by that subnet's virtual-network rule; there is no Function
IP allowlist. Its system-assigned identity is used for the separate
CloudControl role assignment, so the Function must exist and have identity
enabled before the main deployment. The Function needs the application
Storage account and its `CloudControl` table, which `main.bicep` creates,
before its settings are completed and the hook is published.

1. Confirm that `eastus2` is listed by `az functionapp
   list-flexconsumption-locations`. Create the bootstrap VNet and the empty
   Flex Function externally. The VNet is `wattracker-vnet` with address space
   `10.42.0.0/16`; its dedicated Function subnet is
   `budget-hook-flex`, `10.42.2.0/27`, delegated to
   `Microsoft.App/environments`, with the `Microsoft.Storage` service
   endpoint. The ACA subnet is not shared with Flex. This yields the Function
   name, `defaultHostName`, and new system identity `principalId` for
   `budgetHookFunctionAppName`, `budgetHookHost`, and
   `budgetHookPrincipalId`.
2. Resolve owner inputs and the successful #316 publishing run. They yield
   `location`, `storageName`, `allowedOrigin`, `billingEmail`,
   `budgetStartDate`, `budgetEndDate`, and one signed image reference to use
   for both `readImage` and `syncImage` (plus, if used, Static Web App
   repository inputs).
3. Build, validate, review what-if, then create `main.bicep`. This creates
   the application Storage account and `CloudControl`, the VNet/ACA
   environment and apps, managed identities and RBAC, action groups, and
   budget. It also reconciles the Flex subnet and assigns the newly recreated
   Function identity the narrowly scoped CloudControl read/upsert role. Record
   the storage name and Container App endpoint/identity outputs obtained from
   Azure; the current template has no Bicep `output` declarations, so
   portal/`az` queries are required.
4. After `az functionapp create` and the main deployment, re-set the
   Function's application Storage name and budget-hook token, stage and
   publish the hook. Do not run the drill until both the settings update and
   the publish have succeeded; then verify the Function and execute a
   non-production drill.

The bootstrap storage is intentionally separate from the application storage:
a Flex Function needs host/deployment storage before `main.bicep` can create
the application storage it will later access. `dailyMemoryTimeQuota` is a Y1
Consumption setting, not a Flex setting; do not carry it into the recreated
app. Flex uses per-instance memory sizing and regional quotas/scaling instead;
choose only a currently supported instance-memory size and confirm the
owner's regional quota during deployment. The exact provider-supported
bootstrap storage/account and runtime setup must be confirmed on the first
deployment; do not infer that this has been tested here.

## 1. Bootstrap or recreate the Flex Function App (unverified commands)

From a clean deployment shell, choose non-secret names. The following is an
expected Azure CLI flow to confirm against the installed CLI and subscription.
The existing Y1 app is recreated rather than converted: capture its name and
settings, delete the old site after the replacement window is approved, and
create the Flex site with the same or a new name. A recreation changes the
system-assigned principal ID, host key, and default host name; re-read all of
them after creation and before filling `main.bicepparam`.

```sh
export SUBSCRIPTION_ID='TODO_SUBSCRIPTION_ID'
export RESOURCE_GROUP='TODO_RESOURCE_GROUP'
export LOCATION='TODO_RESOURCE_GROUP_LOCATION'
export BOOTSTRAP_STORAGE_NAME='TODO_UNIQUE_FUNCTION_HOST_STORAGE_NAME'
export FUNCTION_APP_NAME='TODO_UNIQUE_BUDGET_HOOK_FUNCTION_APP_NAME'
export VNET_NAME='wattracker-vnet'
export FLEX_SUBNET_NAME='budget-hook-flex'
export VNET_ID="/subscriptions/$SUBSCRIPTION_ID/resourceGroups/$RESOURCE_GROUP/providers/Microsoft.Network/virtualNetworks/$VNET_NAME"

az login
az account set --subscription "$SUBSCRIPTION_ID"
az group create --name "$RESOURCE_GROUP" --location "$LOCATION"
az functionapp list-flexconsumption-locations --query "[?name=='$LOCATION']" --output table
az provider register --namespace Microsoft.App
az network vnet create --name "$VNET_NAME" --resource-group "$RESOURCE_GROUP" --location "$LOCATION" --address-prefixes 10.42.0.0/16
az network vnet subnet create --name "$FLEX_SUBNET_NAME" --resource-group "$RESOURCE_GROUP" --vnet-name "$VNET_NAME" --address-prefixes 10.42.2.0/27 --delegations Microsoft.App/environments --service-endpoints Microsoft.Storage
az storage account create --name "$BOOTSTRAP_STORAGE_NAME" --resource-group "$RESOURCE_GROUP" --location "$LOCATION" --sku Standard_LRS
az functionapp delete --name "$FUNCTION_APP_NAME" --resource-group "$RESOURCE_GROUP" # only when replacing the existing Y1 site
az functionapp create --name "$FUNCTION_APP_NAME" --resource-group "$RESOURCE_GROUP" --storage-account "$BOOTSTRAP_STORAGE_NAME" --flexconsumption-location "$LOCATION" --runtime python --runtime-version 3.12 --functions-version 4 --vnet "$VNET_ID" --subnet "$FLEX_SUBNET_NAME"
az functionapp identity assign --name "$FUNCTION_APP_NAME" --resource-group "$RESOURCE_GROUP"

az functionapp show --name "$FUNCTION_APP_NAME" --resource-group "$RESOURCE_GROUP" --query '{name:name,host:defaultHostName,plan:kind,flexSubnet:siteConfig.virtualNetworkSubnetId}' --output json
az functionapp identity show --name "$FUNCTION_APP_NAME" --resource-group "$RESOURCE_GROUP" --query principalId --output tsv
```

A recreated Function App starts empty. After `az functionapp create` and after
Phase 2 has created the application Storage, run the Phase 3 app-settings
command again, restage the project, and publish the hook before Phase 4. Do not
send the new site through the drill with old settings or an empty code package.

Confirm the actual supported Python/runtime flags, Flex plan, instance-memory
choice, and subnet integration in the first deployment. Put the new
`defaultHostName` (without `https://`) and app name in the matching parameter
entries. Put the new identity `principalId` in `budgetHookPrincipalId`.
There is no outbound-IP handoff or allowlist parameter. If the owner keeps the
same app name, the new host key still must be re-read by the main deployment's
`listKeys` lookup; treat any old callback URL as stale.

## 2. Complete parameters and deploy the main template — manual fallback (unverified commands)

Use the immutable signed image reference from the successful #316 publishing
run. It is one `linux/amd64` GHCR image, and its full `@sha256` digest must be
verified before deployment. Fill `readImage` and `syncImage` with that same
reference only after the run exists. Keep `staticRepositoryUrl = ''` to disable the
optional Static Web App, or supply owner-approved repository values and the
environment-provided deployment token.

If this parameter file was copied from the earlier Y1 deployment, delete the
entire `budgetHookIpRules` parameter block before running any command below.
`main.bicep` no longer declares that parameter; leaving it in the parameter
file causes `BCP259` and the deployment cannot validate or create.

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
set -euo pipefail
az functionapp config appsettings set --name "$FUNCTION_APP_NAME" --resource-group "$RESOURCE_GROUP" --settings WATTRACKER_STORAGE_ACCOUNT_NAME="$STORAGE_NAME" WATTRACKER_BUDGET_HOOK_TOKEN="$WATTRACKER_BUDGET_HOOK_TOKEN"
.venv/bin/python scripts/package_budget_hook.py
cd build/azure-budget-hook
func azure functionapp publish "$FUNCTION_APP_NAME" --python
cd ../..
```

For an explicit restage after a prior stage:

```sh
set -euo pipefail
rm -rf -- build/azure-budget-hook
.venv/bin/python scripts/package_budget_hook.py
cd build/azure-budget-hook
func azure functionapp publish "$FUNCTION_APP_NAME" --python
```

Confirm the Function's app settings, enabled system identity, Flex plan,
`budget-hook-flex` integration, logs, new host key, and ability to reach the
application storage before treating callbacks as live. If the Function is
recreated, re-read its principal ID, host, and host key and redeploy the main
template; no outbound-IP synchronization is required.

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

Before or alongside the drill, inspect the deployed subnet and Storage ACLs to
prove that the Function's `budget-hook-flex` subnet is delegated as
`Microsoft.App/environments`, has the `Microsoft.Storage` service endpoint,
and appears as a `virtualNetworkRules` entry on the application Storage
account alongside the ACA subnet. Do not use or add an IP allowlist for this
proof. The drill then proves that this virtual-network path and the recreated
Function identity's CloudControl Table Insert-Or-Merge (`upsert_entity`)
permission work together.

Missing or invalid Function keys are rejected by the Functions host; a missing
or invalid app token is rejected by the app. Storage RBAC or firewall denial
during `budget_hook.py` apply/clear is surfaced as HTTP 503, as can a network
failure. The same outside HTTP error can therefore represent firewall denial:
inspect Function logs and the CloudControl row to distinguish it. Treat HTTP
200 without the row as failure and investigate logs, storage data-plane RBAC,
firewall virtual-network rules, subnet delegation/service endpoint, and
connectivity.

## Actual-deployment evidence checklist

- [ ] `az account show` identifies the intended non-production subscription.
- [ ] Bicep build, validate, reviewed what-if, and create outputs are saved.
- [ ] The recreated Flex Function name, host, and new identity principal ID
      match `main.bicepparam`; no outbound IPv4 list is supplied.
- [ ] The `budget-hook-flex` subnet is `10.42.2.0/27`, delegated to
      `Microsoft.App/environments`, has the `Microsoft.Storage` service
      endpoint, and is integrated with the Function.
- [ ] Deployment activity shows the Function host-key lookup, role assignment,
      ACA and budget-hook virtual-network firewall rules, tables, action groups, budget, and
      Container Apps succeeded.
- [ ] The one immutable signed image reference was produced by the #316
      publishing run, verified with the documented cosign command, and copied
      identically into `readImage` and `syncImage`.
- [ ] Function settings/publish logs show the staged package was published and
      the Function can access CloudControl with managed identity.
- [ ] The authenticated clear drill returned 200 JSON and the CloudControl row
      payload proves both levels enabled; any rule-isolation retest is recorded.
