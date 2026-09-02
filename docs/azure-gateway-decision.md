# Azure gateway decision — #164

Status: selected and implemented, 2026-09-01.

The deployment does not provision a managed API gateway. The read and sync
Container Apps are public HTTPS origins, and application credentials,
signed request envelopes, durable quotas, and the durable kill switch are the
authoritative controls. This is an intentional reopening of the public-origin
surface identified as B3; it is not a claim that Container Apps provides a
managed WAF or gateway policy layer.

## Cost and topology comparison

Prices below are USD retail list estimates for East US, 730 hours/month,
queried from the [Azure Retail Prices API](https://learn.microsoft.com/en-us/rest/api/cost-management/retail-prices/azure-retail-prices)
on 2026-09-01. Actual offers, regions, currencies, egress, requests, and
support charges can differ. Recheck the [APIM pricing page](https://azure.microsoft.com/en-us/pricing/details/api-management/)
and the [Front Door pricing page](https://azure.microsoft.com/en-us/pricing/details/frontdoor/)
before deployment.

| Candidate | Private backend path | Quota-by-key suitability | East US base estimate | Decision |
|---|---|---|---:|---|
| APIM classic Standard | The existing classic/VNet shape is not the current supported path for a private backend; moving to supported v2 networking requires redesign | `quota-by-key` is service-level; `rate-limit-by-key` is per gateway replica/region | `$0.9407/h` ≈ `$686.71/mo` | Reject |
| APIM Basic v2 | No VNet connectivity in the current tier matrix | v2 quota policy exists, but it does not solve the missing private route | `$0.20548/h` ≈ `$149.70/mo` | Reject |
| APIM Standard v2 | Outbound VNet integration can reach a private backend; inbound exposure still needs an explicit public/private design | `quota-by-key` is durable at service level; rate limits are replica-local | `$0.9589/h` ≈ `$700.00/mo` | Technically works; reject on cost |
| Front Door Standard | Requires a public origin; no Premium Private Link | No APIM quota-by-key; WAF/routing is not a durable application quota | `$35/mo` base + usage | Reject |
| Front Door Premium | Private Link can reach an internal Container Apps origin | No APIM quota-by-key; application quotas remain necessary | `$330/mo` base + usage | Reject on cost |
| No managed gateway | Public ACA HTTPS ingress; app-level credentials/signatures and durable state enforce access and cost | #179 durable quotas and #181 durable kill switch survive scale-to-zero | No gateway base charge; expected deployment baseline `$2–5/mo` | Select |

The APIM network comparison follows [APIM v2 service tiers](https://learn.microsoft.com/en-us/azure/api-management/v2-service-tiers-overview),
[APIM virtual-network concepts](https://learn.microsoft.com/en-us/azure/api-management/virtual-network-concepts?tabs=stv2),
and [outbound VNet integration](https://learn.microsoft.com/en-us/azure/api-management/integrate-vnet-outbound).
The quota distinction follows the [quota-by-key policy](https://learn.microsoft.com/en-us/azure/api-management/quota-by-key-policy)
and the [multitenant APIM architecture guidance](https://learn.microsoft.com/en-us/azure/architecture/guide/multitenant/service/api-management).
Front Door's private-origin requirement is documented in [Private Link for Front Door](https://learn.microsoft.com/en-us/azure/frontdoor/private-link)
and its tier requirement in the [Front Door FAQ](https://learn.microsoft.com/en-us/azure/frontdoor/front-door-faq).

## Resulting controls

- [Container Apps ingress](https://learn.microsoft.com/en-us/azure/container-apps/ingress-overview)
  is external and HTTPS-only (`allowInsecure: false`). The environment remains
  VNet-integrated but is not internal, because an internal environment would
  make the apps reachable only from the VNet.
- The app is the only API authentication boundary. Writer credentials are
  server-issued; writes and sync status require signed envelopes, nonce replay
  claims, and stored capabilities. Reader contexts and paired-device
  credentials are short-lived or revocable. Daily byte/object/request limits
  are durable in Azure Table state, and production refuses non-durable state.
- No production route trusts `X-Verified-Entra-Subject` or
  `X-Gateway-Request-Proof`. The runtime explicitly disables both gateway
  compatibility gates. `CloudConfig` retains those options only for tests or a
  separately configured proxy deployment, and production refuses to claim an
  attested subject without a proof-carrying gateway.
- Storage uses the [Microsoft.Storage service endpoint](https://learn.microsoft.com/en-us/azure/virtual-network/virtual-network-service-endpoints-overview)
  from the ACA subnet. Its public endpoint is enabled because service endpoints
  use it, but the [storage firewall](https://learn.microsoft.com/en-us/azure/storage/common/storage-network-security-virtual-networks)
  is deny-by-default and allows only that subnet, the same-tenant external
  Function resource instance, and its explicitly supplied possible outbound
  IPs. Shared keys and anonymous blobs remain disabled; HTTPS and TLS 1.2
  remain mandatory.
- The budget callback is an externally deployed Azure Function on Consumption,
  because the handler must remain alive when the apps are scaled to zero. The
  classic Consumption plan has no VNet integration, so Bicep accepts the
  Function's possible outbound IPs as `budgetHookIpRules` and keeps the storage
  firewall deny-by-default. Bicep derives each Azure Monitor Action Group URL
  from the Function hostname and its existing default host key via `listKeys`;
  Bicep constructs HTTPS URLs;
  the hook uses managed identity
  plus the least-privilege `CloudControl` role from Bicep. The [Functions pricing
  page](https://azure.microsoft.com/en-us/pricing/details/functions/)
  applies to its invocation and compute usage; it has no APIM/Front Door idle
  gateway charge.

## Budget policy

The monthly budget is `$10`, chosen against the observed normal baseline of
roughly `$2–5` rather than the former `$50` budget. Notifications are:

| Threshold | Action |
|---:|---|
| 50% of amount | Billing notification |
| 80% of amount | `POST /budget/disable-writes`, persists `writes_enabled=false`, reason `budget 80%` |
| 100% of amount | `POST /budget/disable-public-api`, persists both levels disabled, reason `budget 100%` |

The budget callbacks accept only their fixed route action. They reject
unauthenticated calls, do not select an action or reason from the alert body,
and return a generic 503 when durable state cannot be written. Clearing is a
separate operator route that requires the Function host key and the
app-level `X-Wattracker-Budget-Token` header, then writes both levels enabled.
A deployment drill must verify the state through `read_kill_switch`, restart an
app, and verify that the state still holds.

## Subject and enrollment inventory

The gateway removal changes who can attest an identity, not the invitation or
signing model:

- `_attested_subject` and `_subject_binding` read the subject header only when
  `require_verified_subject` is explicitly enabled. The production runtime sets
  it false, so a caller-supplied header is ignored.
- Enrollment start requires the operator token. Enrollment complete requires
  the one-time invitation and writer public key. A subject is an additional
  binding only when a separately configured gateway attests it; an invitation
  bound to an identity cannot be redeemed without that attestation.
- Pairing, context refresh, and reader routes use their durable credential or
  context and the conditional subject binding. No API route treats a boolean
  certificate header as authentication.
- The gateway proof header is absent from Bicep, secrets, and the runtime
  deployment. Its remaining compatibility code is not a current security
  control and is covered only where a deliberately configured reverse proxy is
  under test.
