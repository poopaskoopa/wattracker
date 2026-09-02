# Budget hook

This Azure Functions Consumption app hosts the authenticated budget callbacks
that remain reachable when the Container Apps public API is disabled or scaled
to zero.

Deploy from the repository checkout so the `-e ../../../` requirement can
install the Wattracker package. Set these application settings:

- `WATTRACKER_STORAGE_ACCOUNT_NAME`: the Azure Storage account name.
- `WATTRACKER_BUDGET_HOOK_TOKEN`: the deployment-only app-level token used by
  the non-Functions test/operator seam; it is not the Function host key.

Enable a system-assigned managed identity on the Function App and pass its
object ID to the main Bicep deployment as `budgetHookPrincipalId`. The hook
uses that identity for the `CloudControl` Table data plane; it does not use an
account key or SAS token. Keep the Function's complete possible outbound IPv4
list in the main deployment's `budgetHookIpRules` parameter so the storage
firewall can admit the hook.

The fixed POST endpoints are:

- `/api/budget/disable-writes` — budget 80%; keeps the public API enabled.
- `/api/budget/disable-public-api` — budget 100%; disables both levels.

Azure Function authentication is `FUNCTION`; Azure Action Groups should use
the complete function URL with its `code` query parameter. The main Bicep
deployment resolves the existing Function App's default host key with
`listKeys` rather than accepting the key as a template parameter. The
Functions host validates that key before the ASGI app receives the request; an
`x-functions-key` header is the equivalent form. Treat the URL as a secret and
rotate the Function key before redeploying the main template.
`X-Wattracker-Budget-Token` is accepted only by the app-level test/operator
seam and cannot bypass deployed Function authentication. Request bodies and
caller-selected actions are ignored.
