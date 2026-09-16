# Extractable Data by Product — AI FinOps

Every field that can be pulled from each AI product/service, for FinOps analytics.
Billing model is intentionally omitted here — this is the **data surface** itself.
All of this is loaded into a portable database by
`platform/data-store/build_store.py` (table `extractable_data_catalog`).

Products covered: **Copilot Studio · GitHub Copilot · Microsoft 365 Copilot ·
Foundry/Azure OpenAI · Microsoft 365 Copilot Cowork · Azure ML · Microsoft Fabric**.

---

## 1 · Copilot Studio
| Field | What it is | Source (API / location) | Grain |
|---|---|---|---|
| environment_id | Power Platform environment | PPAC / Dataverse | env |
| agent_id / agent_name | The bot/agent | Dataverse `bot` | agent |
| conversation_id | Session identifier | Dataverse `conversationtranscript` | conversation |
| activities_json | Turn-by-turn transcript | `conversationtranscript` | conversation |
| action_type | classic / generative / agent action | Analytics + transcript | message |
| credits_consumed | Copilot Credits used | PPAC → Licensing → Products → Copilot Studio | day/agent/action |
| session_count | Conversations handled | Studio Analytics | day |
| message_count | Messages per conversation | transcript | conversation |
| outcome / resolution | Resolved, escalated, abandoned | Analytics | conversation |
| channel | Teams, web, etc. | transcript | conversation |
| created_on | Timestamp | Dataverse | conversation |
| end_user_id | Authenticated user (if any) | transcript | conversation |

> **Documented vs community source.** The **documented** Microsoft surface for
> Copilot Credit capacity and consumption is **Power Platform admin center →
> Licensing → Products → Copilot Studio** (prepaid + session-based capacity,
> environments, daily for 3 months / monthly for 12, plus downloadable session
> data). The Dataverse `msdyn_aievent` table exists and is readable, but Microsoft
> **does not document it as a billing/consumption source** and no guidance maps its
> rows to Copilot Credits — treat it as community/unsupported practice, not
> guidance. See <https://learn.microsoft.com/power-platform/admin/manage-copilot-studio-copilot-credits-capacity>.

## 2 · GitHub Copilot
| Field | What it is | Source (API) | Grain |
|---|---|---|---|
| assignee_login | GitHub user | `/orgs/{org}/copilot/billing/seats` | seat |
| assignee_id | Numeric user id | seats | seat |
| created_at | Seat assigned date | seats | seat |
| last_activity_at | Last Copilot use | seats (needs IDE telemetry) | seat |
| last_activity_editor | vscode / VS / JetBrains | seats | seat |
| plan_type | business / enterprise | seats | seat |
| pending_cancellation_date | Scheduled removal | seats | seat |
| ai_credit usage | **Current** billing: GitHub AI Credits ($0.01/credit) | `GET /organizations/{org}/settings/billing/ai_credit/usage` | day/user |
| premium_requests quantity | **LEGACY** metered requests (superseded by AI Credits) | `/settings/billing/usage` | day/user |
| model | Model used | usage / metrics | day/model |
| model_multiplier | Cost weight (legacy premium-request weighting) | usage | request |
| net_amount | Overage $ | usage | day |
| repository_name | Repo context | usage | day/repo |
| active/engaged users | Adoption counts | `GET /enterprises/{e}/copilot/metrics/reports/...` (`X-GitHub-Api-Version: 2026-03-10`) | day |
| suggestions/acceptances | Code accept rate (completions are **not** billed) | metrics reports | day/lang/editor |
| chat counts | Copilot Chat usage | metrics reports | day |

> **Billing moved to GitHub AI Credits.** The current billing unit is the **GitHub
> AI Credit** — 1 credit = $0.01, Business **1,900**/user/month and Enterprise
> **3,900**/user/month, pooled across users with **no carry-over**. **Premium
> requests are legacy** and superseded by AI Credits; code completions are **not**
> billed. The Copilot metrics API is now a set of **report-download endpoints**
> (`GET /enterprises/{e}/copilot/metrics/reports/...`) that require
> `X-GitHub-Api-Version: 2026-03-10`. See
> <https://docs.github.com/en/copilot/concepts/billing-and-usage/organizations-and-enterprises/billing>.

## 3 · Microsoft 365 Copilot
| Field | What it is | Source (API) | Grain |
|---|---|---|---|
| user_principal_name | The user | `getMicrosoft365CopilotUsageUserDetail` (copilotReportRoot) | user |
| display_name | Name | Graph | user |
| last_activity_date | Overall last use | `getMicrosoft365CopilotUsageUserDetail` (copilotReportRoot) | user/day |
| {app}_last_activity | Teams/Word/Excel/Outlook/PowerPoint/OneNote/Loop/Chat | Graph usage report | user/app |
| active users / trend | Adoption counts | `getMicrosoft365CopilotUserCountSummary` / `...UserCountTrend` (copilotReportRoot) | tenant |
| sku_id / sku_part_number | License held | `subscribedSkus` | user |
| capability_status | Enabled/suspended | `assignedLicenses` | user |
| assigned_date | License grant date | Graph | user |
| interaction_id | Individual AI interaction | aiInteraction API | interaction |
| app_class / interaction_type | Where/how used | aiInteraction | interaction |
| from / body_preview | Who + content | aiInteraction | interaction |
| session_id | Conversation grouping | aiInteraction | interaction |

> **Usage APIs moved under `/copilot`; no prompt counts.** The Microsoft 365 Copilot
> usage reports now live under the `/copilot` URL segment (`copilotReportRoot`):
> `getMicrosoft365CopilotUserCountSummary`, `getMicrosoft365CopilotUserCountTrend`,
> `getMicrosoft365CopilotUsageUserDetail`. The older `reportRoot` beta path is
> **superseded**. The payload exposes **last-activity dates only — never prompt
> counts** — which is why the model's `[M365 Prompts]` measure is blank by design.
> Permission `Reports.Read.All`; not available in US Gov or 21Vianet. See
> <https://learn.microsoft.com/microsoft-365-copilot/extensibility/api/admin-settings/reports/resources/copilotreportroot>.

## 4 · Foundry / Azure OpenAI
| Field | What it is | Source | Grain |
|---|---|---|---|
| resource_id | AOAI/Foundry resource | Cost Mgmt / Monitor | resource |
| deployment_name / model_name | Deployed model | Monitor metrics | deployment |
| processed_prompt_tokens | Input tokens | Monitor `metrics` | hour/deploy |
| generated_tokens | Output tokens | Monitor | hour/deploy |
| total_tokens | Sum | Monitor | hour/deploy |
| requests | Call count | Monitor | hour |
| latency_ms | Response time | Monitor | hour |
| throttled_count | 429s | Monitor | hour |
| meter_name / quantity / cost_usd | Real $ | Cost Management FOCUS export (1.2-preview); legacy `usageDetails` fallback | day/meter |
| tags_json | app / bu / env tags | Cost Mgmt | resource |
| caller / api_subscription_id | Who called (via APIM) | Log Analytics | request |
| request_id / status_code | Per-request detail | Diagnostic logs | request |

> **Per-user attribution vs cost.** Azure Monitor metrics
> (`Microsoft.CognitiveServices/accounts`: `ProcessedPromptTokens`,
> `GeneratedTokens`, `TokenTransaction`, `TotalTokenCalls`) are **resource-grain and
> carry no identity**, and Azure Monitor is explicitly **not authoritative for
> cost** — use Cost Management (~5 h lag). The **APIM AI Gateway → Log Analytics**
> path is the only per-user token attribution surface, and it is *this repo's*
> pattern, not documented Microsoft cost guidance. The current Microsoft **FOCUS**
> dataset is **`1.2-preview`** (not 1.0); the 1.0→1.2 renames `x_InvoiceId →
> InvoiceId`, `x_PricingCurrency → PricingCurrency`, `x_SkuMeterName → SkuMeter` are
> tolerated by `focus_col` in `10_conform_usage.py` (picks each column by presence),
> and 1.2's `ListCost` feeds the rate-card comparison. See
> <https://learn.microsoft.com/azure/ai-foundry/foundry-models/how-to/monitor-models>
> and <https://learn.microsoft.com/azure/cost-management-billing/dataset-schema/schema-index>.

## 5 · Microsoft 365 Copilot Cowork (Copilot Credits add-on)
| Field | What it is | Source | Grain |
|---|---|---|---|
| consumer_id / user_id | User consuming Cowork | M365 admin center → Copilot → Cost Management / Cowork usage report | day/consumer |
| total/scheduled/user_initiated_tasks | Cowork task counts | Cowork usage report (UI + CSV, from 1 Apr 2026) | user/day |
| credits_consumed | **Copilot Credits** ($0.01 each) | Cost Management credit export (credits, not USD) | day |
| last_activity_date | Last Cowork activity | Cowork usage report | user |
| cost_usd | Modelled $ from credits × $0.01 | rate card (`M365Copilot|copilot_credit|`) | day |
| (shares M365 identity/license fields) | — | Graph | user |

> **There is no "Cowork unit".** Microsoft 365 Copilot **Cowork** went GA **June
> 2026**. It is **not a separate per-seat SKU**: it requires an existing M365 Copilot
> licence **plus** usage-based billing enabled, and access is granted by a **spending
> policy**, not a licence assignment. The billing unit is the **Copilot Credit at
> $0.01** — the same currency Copilot Studio and Work IQ use. Credits **per task are
> not published** and vary by model, context, runtime and tools (Microsoft ships
> estimators only). There is **no API** — the surfaces are the M365 admin center
> Cost Management consumption tab / CSV export (in credits) and the Cowork usage
> report; Viva Insights offers a reference dashboard. **Double-count warning:** on the
> Azure bill Cowork, Copilot Studio and Work IQ credits all appear under **one service
> labelled `Microsoft Copilot Studio`** — there is no separate Cowork line item — so
> ingesting **both** the M365 credit export **and** the Azure/FOCUS rows for that
> service double-counts the same dollars; the silver conform excludes that service.
> Prepaid capacity-pack draw-down is additionally **invisible in Azure Cost
> Management**, so an Azure-only model under-counts prepaid consumption. See
> <https://learn.microsoft.com/microsoft-365/copilot/cowork/get-started> and
> <https://learn.microsoft.com/microsoft-365/copilot/usage-based-billing-overview-copilot-credits>.

## 6 · Azure ML
| Field | What it is | Source | Grain |
|---|---|---|---|
| workspace_id | AML workspace | Cost Mgmt / Monitor | workspace |
| compute_target | Cluster / instance | Monitor metrics | compute |
| node_hours | CPU/GPU hours | Monitor / logs | job |
| job_id / run | Training run | AML REST / `AmlComputeJobEvent` | job |
| endpoint_id / deployment_id | Online endpoint | Monitor | endpoint |
| request_count | Inference calls | `AmlOnlineEndpointTrafficLog` | endpoint/hour |
| latency_ms | Endpoint latency | Monitor | endpoint |
| gpu_utilization | Hardware use | Monitor | compute |
| submitted_by | User who ran job | AML logs (Entra) | job |
| meter_name / cost_usd | Real compute $ | Cost Management | day/resource |
| tags_json | app/bu/env | Cost Mgmt | resource |

## 7 · Microsoft Fabric (self-cost)
| Field | What it is | Source | Grain |
|---|---|---|---|
| capacity_id / sku | The F-capacity | Capacity Metrics / Cost Mgmt | capacity |
| workspace_id / item_id | Where consumed | Metrics app (XMLA) | workspace/item |
| operation_type / workload | Warehouse, Spark, Pipeline, Copilot, Power BI | Metrics app | operation |
| cu_seconds | Capacity Units consumed | Metrics app | operation/day |
| interactive_cu / background_cu | Split | Metrics app | day |
| throttled / overload | Capacity pressure | Monitor metrics | day |
| user_or_sp_id | Who ran it | activity events | operation |
| meter_name / cost_usd | Capacity $ | Cost Management | day |

---

**Handoff note:** the Bronze CSVs in `platform/fabric/bronze_out/` and the portable
`platform/data-store/finops.db` are the exact interchange a teammate can lift into a
Fabric Lakehouse (via `platform/fabric/load_bronze.py`) once a Power BI license is
assigned. Nothing here depends on Fabric being available.
