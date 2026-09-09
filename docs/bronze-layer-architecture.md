# Bronze Layer Architecture — AI FinOps Accelerator (Fabric Medallion)

> **Scope:** Data acquisition and Bronze design across all AI cost/usage sources.
> Silver/Gold normalization is referenced but not the focus. All schemas assume
> Fabric Lakehouse (Delta) landing with source fidelity preserved.
> **Status of every table below is a DESIGN target.** Mock equivalents already exist
> for 5 feeds; the rest are proposed collectors (see §3, §4).

> **The M365 slice now has a REAL collector.**
> `platform/fabric/extract_m365_graph.py` implements the §1.1 design against live
> Microsoft Graph and emits the documented Bronze schemas with `_data_class=REAL`.
> Verified against a production tenant: 122 subscribed SKUs, 50,949 users and
> 19,291 AI seat assignments returned. Two findings worth carrying forward:
>
> 1. **`getMicrosoft365CopilotUsageUserDetail` is Graph *beta* only.** The v1.0 path
>    returns `400 Resource not found for the segment`. The collector calls
>    `/beta/copilot/reports/...` first, then falls back to `/beta/reports/...`.
> 2. **`subscribedSkus` + `users?$select=assignedLicenses` need only delegated
>    read access**, but the usage report needs `Reports.Read.All` and returns
>    `403` without it. Seat *entitlement* and seat *activity* therefore have
>    different permission floors — plan for tenants where you get one and not
>    the other.

---

## 0. Design principles (Bronze)

1. **Land raw, transform never (in Bronze).** Store the source payload shape as-is,
   plus ingestion metadata. No joins, no identity resolution — that is Silver's job.
2. **Append-only + historical retention.** Every pull is stamped and kept; Bronze is
   the audit record and the replay source.
3. **One Bronze table per source *endpoint*, not per concept.** A concept like "GitHub
   Copilot" spans 3 endpoints → 3 Bronze tables.
4. **Cost and usage arrive separately** on most platforms and are stitched in Silver.
5. **Every row carries lineage:** `_ingested_at`, `_source_system`, `_source_api`,
   `_watermark`, `_batch_id`.

---

## 1. Source system analysis

### 1.1 Microsoft 365 Copilot

| Aspect | Detail |
|---|---|
| **APIs** | Microsoft Graph: `reports/getMicrosoft365CopilotUsageUserDetail(period)`; `copilot/users/{id}/aiInteractionHistory` & `copilot/interactionHistory/getAllEnterpriseInteractions` (aiInteraction); `subscribedSkus`; `users?$select=assignedLicenses`; directory `users` (identity/dimensions). |
| **Telemetry** | Per-user **last-activity DATES per app** (Teams, Word, Excel, Outlook, PowerPoint, OneNote, Loop, Copilot chat). Interaction-level via aiInteraction (appClass, interactionType, from, body, sessionId, timestamps). |
| **Cost data** | **None from Graph.** M365 Copilot is a **flat per-seat license (~$30/user/mo)**. Cost = seat count × rate (from license/EA data), not telemetry. **Copilot Credits** (new PAYG for agents/Cowork) surface via Power Platform / billing meters, not Graph usage report. |
| **Usage data** | Adoption/activity dates, enabled-app breadth; interaction counts (aiInteraction). Not per-prompt cost. |
| **Identity** | **UPN / Entra object id** (strong, human). |
| **Dimensions** | Department, job title, office location, manager (Entra profile) → BU/hierarchy. |
| **Limitations** | Usage report returns **dates, not counts/tokens**; privacy delays; aiInteraction needs elevated consent; no dollar figures; Credits/Cowork/Autopilot are separate feeds. |

### 1.2 Copilot Studio

| Aspect | Detail |
|---|---|
| **APIs / sources** | Dataverse table **`conversationtranscript`** (per-conversation activity JSON); Copilot Studio Analytics; **Power Platform Admin Center** capacity/licensing; **Azure Cost Management** meter "Copilot Studio…" for PAYG. |
| **Telemetry** | Conversations, sessions, messages, agent (bot) actions, outcomes, escalations, per-activity type. |
| **Cost data** | **Copilot Credits consumption** — pure variable. Rate by action type (classic answer=1, generative=2, agent action=3+). Prepaid **Credit packs** (pooled tenant-wide) or **PAYG meter** (real $ in Azure Cost Mgmt). No seats. |
| **Usage data** | Credits by agent, action type, conversation volume, session length. |
| **Identity** | **Bot/agent id** (non-human) + end-user id *if* authenticated channel; often anonymous/no user. |
| **Dimensions** | Environment, agent (bot) name, channel, action type. |
| **Limitations** | Credit→$ needs the pack/PAYG rate; transcript detail retention ~28 days (analytics up to 360d); end-user identity often absent; environment→BU mapping is external. |

### 1.3 GitHub Copilot

| Aspect | Detail |
|---|---|
| **APIs** | `GET /orgs/{org}/copilot/billing/seats` (seat assignments + last activity); `GET /orgs/{org}/copilot/metrics` (aggregate usage); `GET /organizations/{org}/settings/billing/usage` (premium requests / overage $, filter product=copilot). |
| **Telemetry** | Per-seat `last_activity_at` + `last_activity_editor`; org metrics (active users, acceptances, chats) by day/editor/language/model; premium-request quantities. |
| **Cost data** | **Seats** (Business $19 / Enterprise $39 per user/mo) **+ premium-request overage** ($0.04 × model multiplier; e.g. code review ≈ 13×) beyond monthly allowance. |
| **Usage data** | Premium requests by model/feature; acceptance/engagement metrics. |
| **Identity** | **`github_login`** (needs mapping to UPN in Silver). |
| **Dimensions** | Plan type, editor, language, model, repository (in usage). |
| **Limitations** | Metrics require **≥5 active users** (privacy floor); `last_activity_at` needs **IDE telemetry ON**; login↔UPN mapping is external; metrics policy must be enabled. |

### 1.4 Azure AI Foundry / Azure OpenAI

| Aspect | Detail |
|---|---|
| **APIs** | **Cost:** `Microsoft.Consumption/usageDetails` (or Cost Management Query API). **Usage:** `microsoft.insights/metrics` on the resource. **Logs:** Diagnostic settings → Log Analytics (request/response, `AzureDiagnostics`). |
| **Telemetry** | Prompt/completion/total tokens, requests, latency, TPM/RPM, error codes, model/deployment name, streaming. |
| **Cost data** | **Real $** from Azure Cost Management (per meter, per resource, per day) — the authoritative cost source in the whole platform. |
| **Usage data** | Tokens (input/output/cached), requests, latency, throttles by deployment. |
| **Identity** | **Resource + (optionally) caller** — typically a **Service Principal / Managed Identity**, or an APIM subscription key. Often **no human**. |
| **Dimensions** | Subscription, resource group, resource, deployment, model, region, **resource tags** (app/BU/env). |
| **Limitations** | Cost is per-resource/meter, **not per-user** — attribution needs tags or APIM. Metrics ≠ cost granularity; token-level user attribution requires the **APIM AI gateway** (emit-token-metric). |

### 1.5 Azure Machine Learning

| Aspect | Detail |
|---|---|
| **APIs** | Cost Management `usageDetails`; Azure Monitor metrics for `Microsoft.MachineLearningServices` (compute, online-endpoint); AML REST/SDK for jobs, endpoints, deployments; diagnostic logs (`AmlComputeJobEvent`, `AmlOnlineEndpointTrafficLog`). |
| **Telemetry** | Compute node hours, job runs, endpoint request counts/latency, deployment instance hours, GPU/CPU utilization. |
| **Cost data** | **Real $** (Cost Management) for compute (VM/cluster), managed endpoints, storage. |
| **Usage data** | Job/run counts, endpoint QPS, model deployment uptime, quota consumption. |
| **Identity** | **Workspace + SP/MI**; submitting user for jobs (Entra). |
| **Dimensions** | Subscription, RG, workspace, compute target, endpoint, deployment, tags. |
| **Limitations** | Cost is infra-shaped (VM hours), not "per inference"; mapping runs→cost needs allocation logic; user attribution weak for shared compute. |

### 1.6 Microsoft Fabric (self-cost)

| Aspect | Detail |
|---|---|
| **APIs / sources** | **Fabric Capacity Metrics app** (semantic model behind it); Azure Monitor metrics for `Microsoft.Fabric/capacities` (CU utilization, throttling); Cost Management for capacity $; Fabric Admin APIs (workspace/item inventory, activity events). |
| **Telemetry** | Capacity Units (CU) consumed by workload (Warehouse, Spark, Pipelines, **Copilot**, Power BI), interactive vs background, throttling/overload, smoothing. |
| **Cost data** | **Real $** — reserved/PAYG capacity cost (F-SKU) from Cost Management; CU→$ via SKU rate. **Copilot-in-Fabric compute is a CU line** here. |
| **Usage data** | CU-seconds by operation/user/item, refresh counts, query volume. |
| **Identity** | Executing user/SP (Entra), by workspace/item. |
| **Dimensions** | Capacity, workspace, item, operation type, workload. |
| **Limitations** | CU→$ allocation to BU needs a chargeback model; Metrics app is a semantic model (extract via XMLA/API), not a clean REST feed; smoothing complicates daily attribution. |

---

## 2. Recommended BRONZE schema (per-table spec)

> Convention: all tables also carry lineage columns
> `_ingested_at (timestamp)`, `_source_system (string)`, `_source_api (string)`,
> `_watermark (string)`, `_batch_id (string)`. Omitted from lists below for brevity.

### bronze_m365_copilot_usage
- **PK:** `report_date + user_principal_name`
- **Grain:** one row per user per report snapshot
- **Refresh:** daily (period D7)
- **Source API:** Graph `getMicrosoft365CopilotUsageUserDetail`
- **Permissions:** `Reports.Read.All` (app)
- **Columns:** user_principal_name, display_name, report_date, last_activity_date,
  copilot_chat_last_activity, teams_last_activity, word_last_activity,
  excel_last_activity, powerpoint_last_activity, outlook_last_activity,
  onenote_last_activity, loop_last_activity, report_period
- **Descriptions:** per-app last-activity dates used for **seat utilization / idle** detection.

### bronze_m365_copilot_seats
- **PK:** `snapshot_date + user_principal_name + sku_id`
- **Grain:** one row per licensed user per SKU per snapshot
- **Refresh:** daily
- **Source API:** Graph `subscribedSkus` + `users?$select=assignedLicenses`
- **Permissions:** `Directory.Read.All` / `User.Read.All`, `Organization.Read.All`
- **Columns:** snapshot_date, user_principal_name, sku_id, sku_part_number,
  capability_status, assigned_date, service_plans_enabled
- **Descriptions:** the **fixed seat cost** basis (who holds a Copilot license).

### bronze_m365_copilot_interactions  *(Phase 2)*
- **PK:** `interaction_id`
- **Grain:** one row per AI interaction
- **Refresh:** daily/hourly
- **Source API:** Graph aiInteraction (`getAllEnterpriseInteractions`)
- **Permissions:** `AiEnterpriseInteraction.Read.All`
- **Columns:** interaction_id, user_id, app_class, interaction_type, from_id,
  session_id, created_datetime, body_preview, attachments_count, mentions
- **Descriptions:** interaction-level depth for adoption/engagement analytics.

### bronze_m365_copilot_credits  *(NEW — see §3)*
- **PK:** `usage_date + consumer_id + meter_id`
- **Grain:** daily credit consumption per consumer (agent/user)
- **Refresh:** daily
- **Source:** Power Platform admin billing / Azure Cost Management meter
- **Permissions:** Power Platform admin + Cost Management Reader
- **Columns:** usage_date, consumer_id, consumer_type, meter_id, meter_name,
  credits_consumed, unit_price_usd, cost_usd, capability (Cowork/Autopilot/agent)
- **Descriptions:** **variable** M365 Copilot spend (Credits) incl. Cowork/Autopilot.

### bronze_studio_credits
- **PK:** `usage_date + agent_id + action_type`
- **Grain:** daily credits per agent per action type
- **Refresh:** daily
- **Source:** Power Platform admin center / Cost Management meter
- **Permissions:** Power Platform admin, Cost Management Reader
- **Columns:** usage_date, environment_id, agent_id, agent_name, action_type,
  credit_rate, credits_consumed, cost_usd, session_count
- **Descriptions:** Copilot Studio **variable credit** consumption by action.

### bronze_studio_transcripts  *(Phase 2)*
- **PK:** `conversation_id`
- **Grain:** one row per conversation
- **Refresh:** daily (28-day window)
- **Source:** Dataverse `conversationtranscript`
- **Permissions:** Dataverse SP (Bot Transcript Viewer)
- **Columns:** conversation_id, bot_id, environment_id, created_on, activities_json,
  channel, message_count, outcome
- **Descriptions:** conversation detail for agent-level engagement/quality.

### bronze_ghc_seats
- **PK:** `snapshot_date + assignee_login`
- **Grain:** one row per seat per snapshot
- **Refresh:** daily
- **Source API:** `GET /orgs/{org}/copilot/billing/seats`
- **Permissions:** PAT/App `manage_billing:copilot` or `read:org`
- **Columns:** snapshot_date, assignee_login, assignee_id, created_at,
  last_activity_at, last_activity_editor, plan_type, pending_cancellation_date
- **Descriptions:** GitHub Copilot **fixed seat** + idle detection.

### bronze_ghc_premium_usage
- **PK:** `usage_date + login + sku`
- **Grain:** daily metered usage per user per SKU
- **Refresh:** daily
- **Source API:** `GET /organizations/{org}/settings/billing/usage` (product=copilot)
- **Permissions:** `manage_billing:copilot` (metrics policy enabled)
- **Columns:** usage_date, login, sku, unit_type, quantity, model, model_multiplier,
  gross_amount, discount_amount, net_amount, repository_name
- **Descriptions:** **variable** premium-request overage $ by model.

### bronze_ghc_metrics  *(Phase 2)*
- **PK:** `metric_date + editor + language + model`
- **Grain:** daily aggregate (≥5 users)
- **Refresh:** daily
- **Source API:** `GET /orgs/{org}/copilot/metrics`
- **Permissions:** `manage_billing:copilot` / `read:org`
- **Columns:** metric_date, total_active_users, total_engaged_users, editor,
  language, model, suggestions_count, acceptances_count, chat_count
- **Descriptions:** engagement/ROI (acceptance rates) — no cost.

### bronze_azure_ai_cost
- **PK:** `usage_date + resource_id + meter_id`
- **Grain:** daily cost per resource per meter
- **Refresh:** daily
- **Source API:** Cost Management `usageDetails` / Query
- **Permissions:** Cost Management Reader
- **Columns:** usage_date, subscription_id, resource_group, resource_id, meter_id,
  meter_name, meter_category, quantity, unit_price, cost_usd, currency, tags_json
- **Descriptions:** **authoritative real $** for Foundry/AOAI/ML/Fabric (tag-driven attribution).

### bronze_azure_ai_metrics
- **PK:** `metric_time + resource_id + deployment + metric_name`
- **Grain:** hourly/daily metric per deployment
- **Refresh:** hourly
- **Source API:** Azure Monitor `microsoft.insights/metrics`
- **Permissions:** Monitoring Reader
- **Columns:** metric_time, resource_id, deployment_name, model_name, metric_name,
  processed_prompt_tokens, generated_tokens, total_tokens, requests, latency_ms,
  throttled_count
- **Descriptions:** token/request/latency usage for Foundry/AOAI (cost via join to $ meter).

### bronze_azure_ai_logs  *(Phase 2)*
- **PK:** `request_id`
- **Grain:** one row per model request
- **Refresh:** near real-time
- **Source:** Log Analytics (`AzureDiagnostics` / APIM AI-gateway logs)
- **Permissions:** Log Analytics Reader
- **Columns:** request_id, timestamp, resource_id, deployment, caller_ip,
  api_subscription_id, prompt_tokens, completion_tokens, total_tokens, status_code,
  duration_ms, user_or_sp_id
- **Descriptions:** per-request attribution (the **only** path to per-user token cost).

### bronze_azureml_cost  *(NEW — see §3)*
- **PK:** `usage_date + resource_id + meter_id`
- **Grain:** daily cost per AML resource/meter
- **Refresh:** daily
- **Source:** Cost Management `usageDetails`
- **Permissions:** Cost Management Reader
- **Columns:** usage_date, workspace_id, resource_id, meter_name, compute_target,
  quantity, cost_usd, tags_json
- **Descriptions:** AML compute/endpoint **real $**.

### bronze_azureml_usage  *(NEW — see §3, Phase 2)*
- **PK:** `event_time + workspace_id + entity_id`
- **Grain:** per job/endpoint event
- **Refresh:** hourly
- **Source:** Monitor metrics + AML diagnostic logs
- **Permissions:** Monitoring Reader
- **Columns:** event_time, workspace_id, compute_target, job_id, endpoint_id,
  deployment_id, node_hours, request_count, latency_ms, gpu_utilization, submitted_by
- **Descriptions:** AML compute/endpoint usage for allocation.

### bronze_fabric_capacity  *(NEW — see §3)*
- **PK:** `usage_date + capacity_id + workspace_id + operation_type`
- **Grain:** daily CU consumption per workspace/operation
- **Refresh:** daily
- **Source:** Fabric Capacity Metrics semantic model (XMLA) + Monitor metrics
- **Permissions:** Fabric Admin / Capacity Admin; Monitoring Reader
- **Columns:** usage_date, capacity_id, sku, workspace_id, item_id, operation_type,
  workload, cu_seconds, interactive_cu, background_cu, throttled, user_or_sp_id
- **Descriptions:** Fabric self-cost incl. **Copilot-in-Fabric** CU line.

### bronze_fabric_capacity_cost  *(NEW)*
- **PK:** `usage_date + capacity_id + meter_id`
- **Grain:** daily $ per capacity
- **Refresh:** daily
- **Source:** Cost Management
- **Permissions:** Cost Management Reader
- **Columns:** usage_date, capacity_id, sku, meter_name, quantity, cost_usd, tags_json
- **Descriptions:** capacity **real $** (CU→$ basis for chargeback of the platform itself).

### Reference / master-data collectors (non-telemetry, but Bronze-landed)

### bronze_ref_identity_map  *(NEW — critical)*
- **PK:** `identity_key`
- **Grain:** one row per resolved principal
- **Refresh:** daily (Entra) / on-change
- **Source:** Entra `users` + `servicePrincipals` + manual login↔UPN map
- **Permissions:** `Directory.Read.All`, `Application.Read.All`
- **Columns:** identity_key, display_name, principal_type, upn, entra_object_id,
  github_login, is_human, department, manager_upn, account_enabled
- **Descriptions:** the join key that makes cross-platform identity resolution possible.

### bronze_ref_app_inventory  *(NEW)*
- **PK:** `application_key`
- **Grain:** one row per application/workload
- **Source:** CMDB / app-ownership registry / Azure resource tags
- **Columns:** application_key, application_name, application_type, owner_upn,
  owner_business_unit_key, environment, criticality, cost_center_key
- **Descriptions:** maps SPs/resources/tags → owning app & BU (attribution backbone).

### bronze_ref_business_hierarchy  *(NEW)*
- **PK:** `business_unit_key`
- **Grain:** one row per BU/cost center
- **Source:** Finance master data / Entra department rollups
- **Columns:** business_unit_key, business_unit_name, division, parent_bu_key,
  cost_center_key, monthly_budget_usd, executive_owner
- **Descriptions:** BU/budget dimension for chargeback + variance.

### bronze_ref_agent_inventory  *(NEW)*
- **PK:** `agent_key`
- **Grain:** one row per agent/bot
- **Source:** Copilot Studio env inventory + M365 agent registry
- **Columns:** agent_key, agent_name, platform, environment_id, owner_upn,
  owner_business_unit_key, purpose, created_on
- **Descriptions:** attributes non-human agent spend to an owner/BU.

### bronze_ref_rate_card  *(NEW)*
- **PK:** `rate_key`
- **Grain:** one row per priced unit
- **Source:** EA/MCA price sheet + published list prices
- **Columns:** rate_key, platform, unit_type, model, unit_price_usd, currency,
  discount_pct, effective_from, effective_to
- **Descriptions:** converts seats/tokens/credits/premium-requests → comparable $.

---

## 3. Missing collectors (gap analysis)

Current mock model has: `bronze_m365_usage`, `bronze_ghc_seats`,
`bronze_ghc_premium_usage`, `bronze_studio_credits`, `bronze_azure_cost`.

| Missing collector | Needed? | Why | Phase |
|---|---|---|---|
| **M365 Copilot Credits** | ✅ Yes | The only **variable** M365 spend; without it, agent/PAYG cost is invisible | MVP |
| **Copilot Cowork telemetry** | ✅ Yes | New agentic workload; consumes Credits; unattributed otherwise | Phase 2 (folds into Credits) |
| **Copilot Autopilot telemetry** | ✅ Yes | Same — autonomous agent actions bill Credits | Phase 2 (folds into Credits) |
| **Fabric Capacity telemetry** | ✅ Yes | The platform **bills itself** (Copilot-in-Fabric = CU); needed for true TCO | MVP (cost) / Phase 2 (CU detail) |
| **Azure ML telemetry** | ✅ Yes | Custom-model compute is real AI spend | Phase 2 (MVP if AML in scope) |
| **Application inventory** | ✅ Yes | No attribution to app/BU without it | **MVP (blocker)** |
| **Business hierarchy** | ✅ Yes | No chargeback/budget variance without it | **MVP (blocker)** |
| **Agent inventory** | ✅ Yes | Non-human spend can't reach a BU without it | MVP |
| **Identity map** | ✅ Yes | `github_login`↔UPN↔SP resolution — the core IP | **MVP (blocker)** |

**Key insight:** Cowork, Autopilot, and M365 Credits are **not separate APIs** — they
are **capabilities that consume Copilot Credits**, captured by one
`bronze_m365_copilot_credits` collector with a `capability` column. Don't build three
collectors; build one and dimension it.

---

## 4. Final recommended Bronze architecture

| Table Name | Purpose | Source | Key | Grain | Critical Fields |
|---|---|---|---|---|---|
| bronze_m365_copilot_usage | Seat utilization / idle | Graph usage report | date+UPN | user/day | last_activity_date, per-app activity |
| bronze_m365_copilot_seats | Fixed seat basis | Graph subscribedSkus | date+UPN+SKU | user/SKU/day | sku_part_number, assigned_date |
| bronze_m365_copilot_credits | Variable M365 (Cowork/Autopilot) | PPAC/Cost Mgmt | date+consumer+meter | consumer/day | capability, credits_consumed, cost_usd |
| bronze_studio_credits | Studio variable credits | PPAC/Cost Mgmt | date+agent+action | agent/action/day | action_type, credits_consumed, cost_usd |
| bronze_ghc_seats | GitHub fixed seat + idle | GH billing/seats | date+login | seat/day | last_activity_at, plan_type |
| bronze_ghc_premium_usage | GitHub variable overage | GH billing/usage | date+login+sku | user/day | quantity, model_multiplier, net_amount |
| bronze_azure_ai_cost | Foundry/AOAI real $ | Cost Management | date+resource+meter | resource/day | meter_name, cost_usd, tags_json |
| bronze_azure_ai_metrics | Token/request usage | Azure Monitor | time+resource+deploy | deploy/hour | total_tokens, requests, latency_ms |
| bronze_fabric_capacity_cost | Platform self-cost $ | Cost Management | date+capacity+meter | capacity/day | sku, cost_usd |
| bronze_ref_identity_map | Identity resolution | Entra + map | identity_key | principal | upn, github_login, is_human |
| bronze_ref_app_inventory | App/BU attribution | CMDB/tags | application_key | app | owner_business_unit_key |
| bronze_ref_business_hierarchy | Chargeback/budget | Finance MD | business_unit_key | BU | monthly_budget_usd |
| bronze_ref_agent_inventory | Agent→owner/BU | Studio/M365 | agent_key | agent | owner_business_unit_key |
| bronze_ref_rate_card | Unit→$ conversion | Price sheet | rate_key | priced unit | unit_price_usd, discount_pct |
| *(Phase 2)* bronze_m365_copilot_interactions | Engagement depth | Graph aiInteraction | interaction_id | interaction | app_class, interaction_type |
| *(Phase 2)* bronze_studio_transcripts | Conversation detail | Dataverse | conversation_id | conversation | activities_json, outcome |
| *(Phase 2)* bronze_ghc_metrics | ROI/acceptance | GH metrics | date+editor+lang+model | agg/day | acceptances_count |
| *(Phase 2)* bronze_azure_ai_logs | Per-request attribution | Log Analytics | request_id | request | total_tokens, user_or_sp_id |
| *(Phase 2)* bronze_azureml_cost | AML compute $ | Cost Management | date+resource+meter | resource/day | compute_target, cost_usd |
| *(Phase 2)* bronze_azureml_usage | AML compute usage | Monitor/logs | time+ws+entity | job/endpoint | node_hours, request_count |
| *(Phase 2)* bronze_fabric_capacity | CU detail (Copilot-in-Fabric) | Metrics app XMLA | date+capacity+ws+op | op/day | cu_seconds, workload |

### MVP vs Phase 2 — what to load first

**MVP (load these 14 — proves the whole FinOps story end-to-end):**
- All 4 platform **cost/seat/usage** feeds: m365 usage+seats+credits, studio credits,
  ghc seats+premium, azure_ai cost+metrics, fabric_capacity_cost.
- **All 5 reference feeds** (identity_map, app_inventory, business_hierarchy,
  agent_inventory, rate_card) — these are **hard blockers** for attribution and
  chargeback; without them Bronze is just disconnected numbers.

> Rationale: MVP must answer "total AI spend, by BU, with idle-seat waste." That needs
> **one cost feed per platform + the reference/master data**. Everything else is depth.

**Phase 2 (depth & advanced analytics):**
- Interaction/transcript/metrics/log feeds (engagement, ROI, per-request attribution).
- Azure ML feeds (if custom models in scope).
- Fabric CU detail (self-chargeback of Copilot-in-Fabric).

### Silver/Gold normalization (forward reference)
- **Silver** conforms: `silver_identity` (resolve login↔UPN↔SP via identity_map),
  `silver_usage_unified` (all feeds → one daily grain, every unit → $ via rate_card,
  `cost_type` fixed/variable), `silver_cost_allocated` (attribute to app/BU via
  app/agent inventory; untagged → `BU-UNALLOC`).
- **Gold** = the existing star: `fact_ai_usage` + `dim_platform/identity/model/
  application/business_unit/cost_center/date/environment/rate_card`.
