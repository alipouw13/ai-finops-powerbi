# Worked Examples — one story traced through Bronze → Silver → Gold

Companion to `docs/medallion-tables.md`. Same table list, but with **example rows**
so you can see what the data literally looks like at each layer.

To make lineage visible, everything below follows **one cast of characters**:

| Who / what | Detail |
|---|---|
| **Priya Nair** | human, `priya.nair@contoso.com`, GitHub login `pnair`, team Data Science, BU Technology |
| **checkout-service** | service principal (non-human), object id `0999cfae…` |
| **HR Helpdesk Bot** | Copilot Studio agent, botid `b7f2…` |
| Platforms | Azure OpenAI (gpt-4o), M365 Copilot, GitHub Copilot, Copilot Studio |
| Day | `2026-08-07` |

> ⚠ All values below are illustrative MOCK examples (except where noted as REAL shapes
> from the live model). Real deployments land these from the APIs in `docs/extractable-fields.md`.

---

## BRONZE — raw, one row per source feed (as the API returns it)

### Azure AI Foundry / Azure OpenAI

`bronze_aoai_metrics` (Azure Monitor, deployment × minute)
```json
{ "timestamp":"2026-08-07T14:03:00Z", "deployment":"gpt-4o-prod", "model":"gpt-4o",
  "metric_name":"ProcessedPromptTokens", "value":1840, "aggregation":"Total",
  "_source":"azure_monitor", "_ingested_at":"2026-08-07T14:05:11Z" }
```

`bronze_aoai_requestresponse` 🔒 (Diagnostic log, per API call)
```json
{ "TimeGenerated":"2026-08-07T14:03:02Z", "OperationName":"ChatCompletions_Create",
  "DurationMs":842, "ResultSignature":"200", "CallerIPAddress":"20.51.x.x",
  "modelDeploymentName":"gpt-4o-prod", "modelName":"gpt-4o", "apiVersion":"2024-08-01",
  "caller_object_id":"0999cfae-085e-464f-a49d-f8851e3e5195", "streamType":"non-stream" }
```

`bronze_foundry_gateway` (APIM AI Gateway → Log Analytics — the **only** per-user Foundry token attribution path)
```json
{ "time_generated":"2026-08-07T00:00:00Z", "oid":"", "client_id":"checkout-service",
  "app_id":"checkout-service", "upn_or_app_name":"checkout-service",
  "business_unit_claim":"Retail", "cost_center_claim":"CC-1000",
  "model_name":"gpt-4.1", "model_version":"2025-04-14", "deployment_region":"eastus2",
  "prompt_tokens":184000, "completion_tokens":42000, "cached_prompt_tokens":3100,
  "requests":920, "status_code":200, "is_error":"False" }
```
Silver **prefers** this feed over resource-grain `bronze_azure_ai_metrics` (which carries
no identity → `unknown`) and falls back when it is absent — never both, so tokens can't
double-count.

`bronze_azure_cost_usage` (Cost Management — the $ authority)
```json
{ "billingPeriod":"2026-08", "date":"2026-08-07", "meterCategory":"Azure OpenAI",
  "meterName":"gpt-4o Input regional Tokens", "quantity":184000, "effectivePrice":0.0000025,
  "costInBillingCurrency":0.46, "resourceId":"/subscriptions/.../aoai-prod",
  "resourceGroup":"rg-ai-prod", "tags":{"app":"checkout","env":"prod"} }
```

### Microsoft 365 Copilot

`bronze_m365_copilot_usage` (getMicrosoft365CopilotUsageUserDetail — exact CSV columns)
```csv
Report Refresh Date,Report Period,User Principal Name,Display Name,Last Activity Date,Microsoft Teams Copilot Last Activity Date,Word Copilot Last Activity Date,Excel Copilot Last Activity Date,PowerPoint Copilot Last Activity Date,Outlook Copilot Last Activity Date,OneNote Copilot Last Activity Date,Loop Copilot Last Activity Date,Copilot Chat Last Activity Date
2026-08-07,D7,priya.nair@contoso.com,Priya Nair,2026-08-06,2026-08-06,2026-08-05,,,2026-08-06,,,2026-08-06
2026-08-07,D7,sam.oketch@contoso.com,Sam Oketch,,,,,,,,,          ← licensed but NO activity = idle seat
```

`bronze_m365_copilot_interactions` 🔒 (aiInteraction, per prompt/response)
```json
{ "id":"AAMk…", "appClass":"IPM.SkypeTeams.Message.Copilot.Excel", "conversationType":"appchat",
  "interactionType":"userPrompt", "from":{"user":{"id":"…","userPrincipalName":"priya.nair@contoso.com"}},
  "createdDateTime":"2026-08-06T09:12:04Z", "sessionId":"19:abc…", "requestId":"req-771",
  "body":{"contentType":"text","content":"Summarize Q2 revenue by region"} }
```

`bronze_m365_subscribed_skus` + `bronze_m365_user_licenses`
```json
{ "skuPartNumber":"Microsoft_365_Copilot", "prepaidUnits":{"enabled":250}, "consumedUnits":243 }
{ "userId":"…", "UPN":"priya.nair@contoso.com", "skuPartNumber":"Microsoft_365_Copilot",
  "assignedDateTime":"2026-02-01T00:00:00Z" }
```

`bronze_m365_cowork_usage` (Cowork add-on — M365 admin center Cost Management / Cowork usage report, UI/CSV only, **no API**)
```json
{ "report_date":"2026-08-07", "user_id":"…", "user_principal_name":"priya.nair@contoso.com",
  "service":"Cowork", "spending_policy_name":"Cowork - Engineering",
  "total_tasks":14, "scheduled_tasks":4, "user_initiated_tasks":10,
  "credits_consumed":820, "last_activity_date":"2026-08-07" }
```
The billing unit is the **Copilot Credit at $0.01** — there is no "Cowork unit". On the
Azure bill these credits hide inside one service labelled `Microsoft Copilot Studio`, so
silver's Azure conform excludes that service to avoid double-counting.

### GitHub Copilot

`bronze_ghc_seats` (per assigned user — the idle-seat source)
```json
{ "assignee":{"login":"pnair","id":80421}, "created_at":"2026-01-15T00:00:00Z",
  "last_activity_at":"2026-08-06T18:44:00Z", "last_activity_editor":"vscode/1.93.0",
  "pending_cancellation_date":null, "plan_type":"enterprise" }
{ "assignee":{"login":"jdoe","id":80999}, "created_at":"2026-01-15T00:00:00Z",
  "last_activity_at":null, "pending_cancellation_date":null, "plan_type":"enterprise" }   ← idle
```

`bronze_ghc_usage_metrics` (daily aggregate — ≥5 active users required)
```json
{ "date":"2026-08-07", "total_active_users":118, "total_engaged_users":97,
  "copilot_ide_code_completions":{"total_code_suggestions":48120,"total_code_acceptances":15840,
    "languages":[{"name":"python","total_code_acceptances":6210}],
    "editors":[{"name":"vscode","models":[{"name":"gpt-4o","total_code_acceptances":9100}]}]},
  "copilot_ide_chat":{"total_chats":2140} }
```

`bronze_ghc_billing_usage` (enhanced billing — GitHub AI Credit usage; premium requests are **legacy**)
```json
{ "date":"2026-08-07", "product":"copilot", "sku":"copilot_premium_request",
  "quantity":320, "unitType":"request", "netAmount":12.80, "repositoryName":"contoso/payments" }
```
The current billing unit is the **GitHub AI Credit** ($0.01; Business 1,900 / Enterprise
3,900 per user/month, pooled, no carry-over) via
`GET /organizations/{org}/settings/billing/ai_credit/usage`; the premium-request line above
is kept only for historical rows. Code completions are not billed.

### Copilot Studio

`bronze_studio_transcripts` 🔒 (Dataverse conversationtranscript)
```json
{ "conversationtranscriptid":"c1…", "botid":"b7f2…", "conversationid":"conv-5521",
  "createdon":"2026-08-07T10:22:00Z",
  "content":{"activities":[{"type":"message","from":"user","text":"How much PTO do I have?"},
                          {"type":"message","from":"bot","text":"You have 12 days."}]} }
```

`bronze_studio_analytics` + `bronze_studio_capacity` + `bronze_studio_cost_usage`
```json
{ "botid":"b7f2…", "date":"2026-08-07", "sessions":540, "resolutionRate":0.71,
  "escalationRate":0.14, "csat":4.2, "topicsTriggered":{"pto":210,"benefits":140} }
{ "botid":"b7f2…", "date":"2026-08-07", "messagesConsumed":1290, "generativeAnswers":540,
  "environmentId":"env-hr-prod" }
{ "date":"2026-08-07", "meterName":"Copilot Studio Messages", "quantity":1290, "cost":3.23 }
```

### Reference / master data (customer inputs — MOCK 📝)
```json
bronze_app_ownership:  { "app":"HR Helpdesk", "owner_upn":"maria.li@contoso.com",
                         "owner_bu":"HR", "agent_id":"b7f2…", "environment":"prod" }
bronze_org_hierarchy:  { "business_unit":"Technology", "division":"Product & Eng",
                         "cost_center":"CC-3000", "executive_owner":"CTO", "monthly_budget":25000 }
bronze_rate_card:      { "platform":"Foundry", "unit_type":"token", "model":"gpt-4o",
                         "unit_price_usd":0.0000025, "effective_from":"2026-01-01", "discount_pct":0.15 }
```

---

## SILVER — conformed, resolved, reconciled

`silver_identity_resolved` — **the identity graph** (github login ↔ UPN ↔ SP all unified)
```csv
identity_key,display_name,principal_type,upn,github_login,team,business_unit,identity_class,is_human,home_business_unit_key
a11f…,Priya Nair,User,priya.nair@contoso.com,pnair,Data Science,Technology,User,TRUE,BU-TECH
0999cfae…,checkout-service,ServicePrincipal,,,Checkout,Retail,ServicePrincipal,FALSE,BU-RETAIL
b7f2…,HR Helpdesk Bot,Agent,,,HR Ops,HR,Agent,FALSE,BU-HR
```
> Note: Priya's `pnair` (GitHub) and `priya.nair@contoso.com` (M365/Entra) collapse into **one** `identity_key`.

`silver_application_map`
```csv
application_key,application_name,application_type,owner_business_unit_key,owner_upn,default_environment_key,criticality
APP-HRBOT,HR Helpdesk,CopilotStudioAgent,BU-HR,maria.li@contoso.com,ENV-PROD,High
APP-CHECKOUT,Checkout Service,Microservice,BU-RETAIL,,ENV-PROD,Critical
```

`silver_model_map`  (deployment "gpt-4o-prod" → canonical model_key "gpt-4o")
```csv
model_key,model_name,model_version,provider,modality,source_deployment
gpt-4o,gpt-4o,2024-08-01,Azure OpenAI,text,gpt-4o-prod
```

`silver_rate_card`  (one price per platform/unit/model/date incl. discount)
```csv
platform,unit_type,model,unit_price_usd,effective_from,discount_pct
Foundry,token,gpt-4o,0.0000025,2026-01-01,0.15
M365Copilot,seat_day,,1.0,2026-01-01,0.0
GitHubCopilot,seat_day,,1.28,2026-01-01,0.05
```

`silver_usage_foundry`  (per-call tokens joined to **billed $** from Cost Mgmt)
```csv
usage_date,identity_key,model_key,application_key,input_tokens,output_tokens,requests,cost_usd,cost_is_estimated
2026-08-07,0999cfae…,gpt-4o,APP-CHECKOUT,184000,42000,920,0.46,FALSE
```

`silver_usage_m365`  (per user/day: licensed? active? → seat cost)
```csv
usage_date,identity_key,application_key,licensed,active,seat_cost_usd
2026-08-07,a11f…,APP-M365,TRUE,TRUE,1.00
2026-08-07,samoketch…,APP-M365,TRUE,FALSE,1.00      ← idle seat: licensed, not active
```

`silver_usage_m365_cowork`  (Cowork add-on → `unit_type = copilot_credit`, cost modelled at $0.01/credit)
```csv
usage_date,identity_key,platform_key,unit_type,quantity,requests,cost_usd,cost_is_estimated
2026-08-07,a11f…,M365Copilot,copilot_credit,820,14,8.20,TRUE
```

`silver_usage_ghc`
```csv
usage_date,identity_key,application_key,licensed,last_activity_at,acceptances,seat_cost_usd,premium_req_cost_usd
2026-08-07,a11f…,APP-GHC,TRUE,2026-08-06T18:44:00Z,142,1.28,0.00
2026-08-07,jdoe…,APP-GHC,TRUE,,0,1.28,0.00                ← idle seat
```

`silver_usage_studio`
```csv
usage_date,identity_key,application_key,sessions,messages_consumed,resolution_rate,cost_usd,cost_is_estimated
2026-08-07,b7f2…,APP-HRBOT,540,1290,0.71,3.23,FALSE
```

`silver_usage_unified`  — **all five unioned to one daily grain** (m365, m365_cowork, ghc, studio, foundry; nulls where a source can't reach)
```csv
usage_date,platform_key,identity_key,model_key,application_key,unit_type,quantity,input_tokens,output_tokens,requests,cost_usd,list_cost_usd,cost_is_estimated,has_rate_card
2026-08-07,Foundry,0999cfae…,gpt-4o,APP-CHECKOUT,token,226000,184000,42000,920,0.46,0.46,FALSE,TRUE
2026-08-07,M365Copilot,a11f…,,APP-M365,seat_day,1,0,0,0,1.00,1.00,FALSE,TRUE
2026-08-07,M365Copilot,a11f…,,APP-M365,copilot_credit,820,0,0,14,8.20,8.20,TRUE,TRUE
2026-08-07,GitHubCopilot,a11f…,,APP-GHC,seat_day,1,0,0,0,1.28,1.28,FALSE,TRUE
2026-08-07,CopilotStudio,b7f2…,,APP-HRBOT,copilot_credit,1290,0,0,540,3.23,3.23,FALSE,TRUE
```
`has_rate_card` is TRUE where a genuine list/rate-card price backed the row; when no rate
exists, `list_cost_usd` falls back to `cost_usd` and `has_rate_card` is FALSE.

`silver_cost_reconciliation`  (modelled vs billed → drives Cost Confidence %)
```csv
usage_date,platform_key,modelled_cost,billed_cost,variance_pct
2026-08-07,Foundry,0.44,0.46,-4.3
```

---

## GOLD — the star the Power BI model binds to (exact repo schema)

`fact_ai_usage`  (grain: date × platform × identity × model × application × environment × BU × cost center)
```csv
usage_date,platform_key,identity_key,model_key,cost_center_key,unit_type,quantity,input_tokens,output_tokens,cached_tokens,requests,cost_usd,list_cost_usd,cost_is_estimated,has_rate_card,is_error,latency_ms,application_key,environment_key,business_unit_key
2026-08-07,Foundry,0999cfae-085e-464f-a49d-f8851e3e5195,gpt-4o,CC-1000,token,226000,184000,42000,3100,920,0.46,0.46,FALSE,TRUE,False,842,APP-CHECKOUT,ENV-PROD,BU-RETAIL
2026-08-07,M365Copilot,a11f…,,CC-3000,seat_day,1,0,0,0,0,1.00,1.00,FALSE,TRUE,False,0,APP-M365,ENV-PROD,BU-TECH
2026-08-07,M365Copilot,a11f…,,CC-3000,copilot_credit,820,0,0,0,14,8.20,8.20,TRUE,TRUE,False,0,APP-M365,ENV-PROD,BU-TECH
2026-08-07,GitHubCopilot,a11f…,,CC-3000,seat_day,1,0,0,0,0,1.28,1.28,FALSE,TRUE,False,0,APP-GHC,ENV-PROD,BU-TECH
2026-08-07,CopilotStudio,b7f2…,,CC-4000,copilot_credit,1290,0,0,0,540,3.23,3.23,FALSE,TRUE,False,0,APP-HRBOT,ENV-PROD,BU-HR
```
`list_cost_usd` is the list / rate-card cost (Azure rows take FOCUS `ListCost`; other
platforms price from `dim_rate_card`), falling back to `cost_usd` when no rate exists;
`has_rate_card` records whether a genuine rate backed the row. The `copilot_credit` row on
`M365Copilot` is the **Cowork** add-on — consumptive, so it lands in Variable, not Fixed, cost.

### Dimension rows (exact current columns)

`dim_identity`
```csv
identity_key,display_name,principal_type,upn,github_login,team,business_unit,cost_center_key,identity_class,is_human,home_business_unit_key
a11f…,Priya Nair,User,priya.nair@contoso.com,pnair,Data Science,Technology,CC-3000,User,TRUE,BU-TECH
b7f2…,HR Helpdesk Bot,Agent,,,HR Ops,HR,CC-4000,Agent,FALSE,BU-HR
```

`dim_platform`
```csv
platform_key,platform_name,billing_model,native_unit,addon_unit,addon_billing_model,has_token_telemetry,has_native_cost,is_variable_cost,data_source,enterprise_discount_pct
Foundry,Azure AI Foundry,Consumption (tokens),token,,,TRUE,TRUE,TRUE,REAL - APIM gateway / Log Analytics,0.15
M365Copilot,Microsoft 365 Copilot,Per-seat licence,seat_day,copilot_credit,Usage-based (Copilot Credits) - Cowork,FALSE,FALSE,FALSE,MOCK - needs Graph app (Reports.Read.All),0.0
```
Only Microsoft 365 Copilot carries an `addon_unit` today (the Cowork Copilot-Credit
add-on); every other platform gets empty strings, never null.

`dim_application`
```csv
application_key,application_name,application_type,owner_business_unit_key,owner_upn,default_environment_key,criticality,is_mock
APP-HRBOT,HR Helpdesk,CopilotStudioAgent,BU-HR,maria.li@contoso.com,ENV-PROD,High,TRUE
```

`dim_business_unit`
```csv
business_unit_key,business_unit_name,division,monthly_budget_usd,executive_owner,is_mock_budget
BU-TECH,Technology,Product & Eng,25000,CTO,TRUE
BU-HR,Human Resources,Corporate,4000,CHRO,TRUE
```

`dim_model`
```csv
model_key,model_name,model_version,provider,modality
gpt-4o,gpt-4o,2024-08-01,Azure OpenAI,text
```

`dim_environment` · `dim_date` · `dim_cost_center` · `dim_rate_card`
```csv
ENV-PROD,Production,TRUE,Tier-1
2026-08-07,2026,Q3,8,August,7,Friday,TRUE,2026-08
CC-3000,Platform Engineering,Technology,cto@contoso.com
rate-foundry-4o,Foundry,token,gpt-4o,0.0000025,2026-01-01,USD,price-sheet,EA rate
```

### What the report then shows (measures over these rows)
- **Total AI Cost** = Σ `cost_usd` = 0.46 + 1.00 + 1.28 + 3.23 = **$5.97** for the day
- **Idle Licensed Users** = seats with a license but no activity → catches Sam (M365) and jdoe (GitHub)
- **Idle Seat Waste (monthly)** = idle seats × monthly seat price → reclaim candidates
- **Cost by BU** → Technology $2.28, HR $3.23, Retail $0.46
- **Cost Confidence %** → high where `cost_is_estimated = FALSE` (all billed here)

---

## The point of the example

Trace **Priya**: she shows up in 3 Bronze feeds (M365 usage, GitHub seats, AOAI caller
via her apps), gets **collapsed to one `identity_key`** in Silver, and appears as clean
fact rows in Gold that every persona page can slice by her BU (Technology), her apps,
and her platforms — while **Sam and jdoe** surface as **idle-seat reclaim** because they
hold licenses with no activity. That end-to-end lineage is exactly what the demo tells.
