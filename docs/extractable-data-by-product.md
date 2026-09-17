# Extractable Data by Product — AI FinOps

Every field that can be pulled from each AI product/service, for FinOps analytics.
Billing model is intentionally omitted here — this is the **data surface** itself.
All of this is loaded into a portable database by
`platform/data-store/build_store.py` (table `extractable_data_catalog`).

Products covered: **Copilot Studio · GitHub Copilot · Microsoft 365 Copilot ·
Foundry/Azure OpenAI · Copilot Cowork/Autopilot · Azure ML · Microsoft Fabric**.

---

## 1 · Copilot Studio
| Field | What it is | Source (API / location) | Grain |
|---|---|---|---|
| environment_id | Power Platform environment | PPAC / Dataverse | env |
| agent_id / agent_name | The bot/agent | Dataverse `bot` | agent |
| conversation_id | Session identifier | Dataverse `conversationtranscript` | conversation |
| activities_json | Turn-by-turn transcript | `conversationtranscript` | conversation |
| action_type | classic / generative / agent action | Analytics + transcript | message |
| credits_consumed | Copilot Credits used | PPAC billing / Cost Mgmt | day/agent/action |
| session_count | Conversations handled | Studio Analytics | day |
| message_count | Messages per conversation | transcript | conversation |
| outcome / resolution | Resolved, escalated, abandoned | Analytics | conversation |
| channel | Teams, web, etc. | transcript | conversation |
| created_on | Timestamp | Dataverse | conversation |
| end_user_id | Authenticated user (if any) | transcript | conversation |

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
| premium_requests quantity | Metered requests | `/settings/billing/usage` | day/user |
| model | Model used | usage / metrics | day/model |
| model_multiplier | Cost weight (code review 13×) | usage | request |
| net_amount | Overage $ | usage | day |
| repository_name | Repo context | usage | day/repo |
| active/engaged users | Adoption counts | `/copilot/metrics` (≥5 users) | day |
| suggestions/acceptances | Code accept rate | metrics | day/lang/editor |
| chat counts | Copilot Chat usage | metrics | day |

## 3 · Microsoft 365 Copilot
| Field | What it is | Source (API) | Grain |
|---|---|---|---|
| user_principal_name | The user | Graph usage report | user |
| display_name | Name | Graph | user |
| last_activity_date | Overall last use | `getMicrosoft365CopilotUsageUserDetail` | user/day |
| {app}_last_activity | Teams/Word/Excel/Outlook/PowerPoint/OneNote/Loop/Chat | Graph usage report | user/app |
| sku_id / sku_part_number | License held | `subscribedSkus` | user |
| capability_status | Enabled/suspended | `assignedLicenses` | user |
| assigned_date | License grant date | Graph | user |
| interaction_id | Individual AI interaction | aiInteraction API | interaction |
| app_class / interaction_type | Where/how used | aiInteraction | interaction |
| from / body_preview | Who + content | aiInteraction | interaction |
| session_id | Conversation grouping | aiInteraction | interaction |

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
| BilledCost / EffectiveCost | Authoritative billed $ | Cost Management FOCUS 1.0r2 | charge line |
| ConsumedQuantity / PricingQuantity | Billed tokens and 1K-token blocks | Cost Management FOCUS 1.0r2 | charge line |
| ListCost | List vs billed = realised discount | Cost Management FOCUS 1.0r2 | charge line |
| Tags | app / bu / env tags | Cost Management FOCUS 1.0r2 | resource |
| ClientId / Oid | Who called (the only identity path) | APIM → Log Analytics | request |
| PromptTokens / CompletionTokens / CachedPromptTokens | Per-request token split | APIM → Log Analytics | request |
| RequestId / StatusCode / IsError | Per-request detail | APIM → Log Analytics | request |

## 5 · Copilot Cowork / Autopilot
| Field | What it is | Source | Grain |
|---|---|---|---|
| consumer_id | User/agent consuming | PPAC billing / Cost Mgmt | day/consumer |
| capability | Cowork vs Autopilot | billing meter | consumer |
| credits_consumed | Copilot Credits | PPAC / Cost Mgmt | day |
| action_count | Autonomous actions taken | agent telemetry | day |
| cost_usd | $ from credits | Cost Mgmt | day |
| (shares M365 identity/license fields) | — | Graph | user |

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
