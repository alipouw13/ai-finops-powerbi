# Bronze Layer Architecture — AI FinOps Accelerator (Fabric Medallion)

> **Scope:** Data acquisition and Bronze design across all AI cost/usage sources.
> Silver/Gold normalization is referenced but not the focus. All schemas assume
> Fabric Lakehouse (Delta) landing with source fidelity preserved.
> **Status of every table below is a DESIGN target.** Mock equivalents already exist
> for 5 feeds; the rest are proposed collectors (see §3, §4).

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
| **APIs** | **Cost:** Cost Management **Export → FOCUS 1.0r2** dataset (not the legacy `usageDetails` shape). **Identity + usage:** APIM AI gateway → Log Analytics. **Logs:** Diagnostic settings → Log Analytics (`AzureDiagnostics`). |
| **Telemetry** | Prompt/completion/cached/total tokens, requests, latency, TPM/RPM, error codes, model/deployment name, streaming. |
| **Cost data** | **Real $** from the FOCUS export (per meter, per resource, per day) — the authoritative cost source in the whole platform. Carries no identity, so it is allocated by gateway token share. |
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
- **PK:** `Report Refresh Date + User Principal Name`
- **Grain:** one row per user per report snapshot
- **Refresh:** daily (period D7)
- **Source API:** Graph `getMicrosoft365CopilotUsageUserDetail`
- **Permissions:** `Reports.Read.All` (app)
- **Columns (the report's own headers, spaces and all):** `Report Refresh Date`,
  `User Principal Name`, `Display Name`, `Last Activity Date`,
  `Copilot Chat Last Activity Date`, `Microsoft Teams Copilot Last Activity Date`,
  `Word Copilot Last Activity Date`, `Excel Copilot Last Activity Date`,
  `PowerPoint Copilot Last Activity Date`, `Outlook Copilot Last Activity Date`,
  `OneNote Copilot Last Activity Date`, `Loop Copilot Last Activity Date`,
  `Report Period`
- **Descriptions:** per-app last-activity **dates**, never counts or tokens. A blank
  `Last Activity Date` is the idle-seat signal, and Silver turns a non-blank one into
  an `active_day` row so a seat with no activity becomes reclaimable.

### bronze_m365_copilot_seats
- **PK:** `snapshotDate + userPrincipalName + skuId`
- **Grain:** one row per licensed user per SKU per snapshot
- **Refresh:** daily
- **Source API:** Graph `subscribedSkus` + `users?$select=assignedLicenses`
- **Permissions:** `Directory.Read.All` / `User.Read.All`, `Organization.Read.All`
- **Columns:** snapshotDate, userId, userPrincipalName, displayName, skuId,
  skuPartNumber, servicePlanId, servicePlanName, provisioningStatus, appliesTo,
  capabilityStatus, assignedDateTime, prepaidUnitsEnabled, consumedUnits
- **Descriptions:** the **fixed seat cost** basis (who holds a Copilot licence).
  `prepaidUnitsEnabled` vs `consumedUnits` exposes purchased-but-unassigned seats.

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
- **PK:** `usageDate + consumerId + meterId`
- **Grain:** daily credit consumption per consumer (agent/user)
- **Refresh:** daily
- **Source:** M365 / Power Platform admin billing (Copilot Credits)
- **Permissions:** Power Platform admin + billing reader
- **Columns:** usageDate, tenantId, consumerId, consumerType, capability, meterId,
  meterName, billingType, creditsConsumed, unitPriceUsd, costUsd, currency
- **Descriptions:** **variable** M365 Copilot spend (Credits) incl. Cowork/Autopilot.
  The feed carries its own dollars, so Silver uses them directly rather than the
  rate card.

### bronze_dataverse_msdyn_aievent
- **PK:** `msdyn_aieventid`
- **Grain:** one row per billed Copilot Studio event
- **Refresh:** daily, **per environment** (msdyn_aievent is not tenant-wide)
- **Source:** Dataverse `msdyn_aievents` (OData)
- **Permissions:** Dataverse app registration + environment URL
- **Columns:** @odata.etag, msdyn_aieventid, createdon, msdyn_eventtimestamp,
  msdyn_eventtype, msdyn_billingtype, msdyn_creditconsumed, msdyn_ismeteredevent,
  msdyn_conversationid, msdyn_sessionid, _msdyn_botid_value,
  `_msdyn_botid_value@OData.Community.Display.V1.FormattedValue`,
  _msdyn_environmentid_value, msdyn_channel, msdyn_outcome, statecode, statuscode,
  versionnumber
- **Descriptions:** Copilot Studio **credit consumption per agent**.
  `msdyn_creditconsumed` is **already net of zero-rating** — use it directly and never
  recompute credits from an action-rate table. Lookups keep both the `_value` id and
  the OData formatted-value column. Exclude bring-your-own-model rows so Foundry spend
  is not counted twice.
- **Cost note:** this feed carries **no dollars**. The PAYG meter in the FOCUS export
  does, so Silver allocates that billed amount across agents by credit share.

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
- **Columns:** snapshot_date, assignee_login, assignee_id, assignee_type,
  assigning_team, created_at, updated_at, last_activity_at, last_activity_editor,
  pending_cancellation_date, plan_type
- **Descriptions:** GitHub Copilot **fixed seat** + idle detection. `last_activity_at`
  requires IDE telemetry to be on, otherwise every seat looks idle. Silver turns a
  non-blank value into an `active_day` row.

### bronze_ghc_premium_usage
- **PK:** `date + username + sku`
- **Grain:** daily metered usage per user per SKU
- **Refresh:** daily
- **Source API:** `GET /organizations/{org}/settings/billing/usage` (product=copilot)
- **Permissions:** `manage_billing:copilot` (metrics policy enabled)
- **Columns:** date, product, sku, model, modelMultiplier, quantity, unitType,
  pricePerUnit, grossAmount, discountAmount, netAmount, organizationName,
  repositoryName, username
- **Descriptions:** **variable** premium-request overage $ by model. `netAmount` is
  the billed figure, so Silver uses it directly instead of the rate card.

### bronze_ghc_metrics  *(Phase 2)*
- **PK:** `metric_date + editor + language + model`
- **Grain:** daily aggregate (≥5 users)
- **Refresh:** daily
- **Source API:** `GET /orgs/{org}/copilot/metrics`
- **Permissions:** `manage_billing:copilot` / `read:org`
- **Columns:** metric_date, total_active_users, total_engaged_users, editor,
  language, model, suggestions_count, acceptances_count, chat_count
- **Descriptions:** engagement/ROI (acceptance rates) — no cost.

### bronze_focus_cost
- **Contract:** Microsoft Cost Management **FOCUS 1.0r2** cost and usage details.
  This is intentionally not the
  [legacy EA `UsageDetails` shape](https://learn.microsoft.com/azure/cost-management-billing/dataset-schema/cost-usage-details-ea).
  The source field contract follows the
  [Microsoft FOCUS schema](https://learn.microsoft.com/azure/cost-management-billing/dataset-schema/cost-usage-details-focus).
- **Scope:** ONE export covers every Azure-billed AI charge line — Azure OpenAI /
  Foundry usage, the Copilot Studio pay-as-you-go credit meter, and Fabric capacity.
  One contract, one collector; the service is a column, not a table.
- **PK:** no provider-guaranteed row key; dedupe in Silver using charge period,
  resource, SKU price, charge category, and source batch
- **Grain:** one provider charge line (typically resource × meter × day for AOAI usage)
- **Refresh:** daily
- **Source:** Cost Management Export configured for the FOCUS cost and usage dataset
- **Permissions:** Cost Management Reader
- **Columns:** all 96 Microsoft FOCUS 1.0 fields, including `BilledCost`,
  `EffectiveCost`, `ListCost`, `ConsumedQuantity`, `PricingQuantity`, `ResourceId`,
  `SkuPriceId`, `SubAccountId`, `Tags`, and Microsoft extension fields such as
  `x_ResourceGroupName`, `x_SkuMeterId`, `x_SkuMeterName`, and `x_SkuDetails`;
  Bronze lineage is appended after the source fields.
- **Descriptions:** **the authoritative dollars for the whole platform.**
  `SubAccountId` is the Azure subscription, `ConsumedQuantity`/`ConsumedUnit` describe
  raw usage (tokens, credits, hours), and `PricingQuantity`/`PricingUnit` describe
  billable units (1K-token blocks). `Tags` drives application/BU attribution, and
  `ListCost` minus `BilledCost` is the realised discount.
- **Mock fidelity:** `bronze_focus_cost.csv` uses the exact FOCUS 1.0r2 header order
  and ISO timestamps with seconds. Empty fields are intentional because many FOCUS
  columns are conditional or not applicable to ordinary usage rows.
- **EA comparison:** the linked EA schema uses fields such as `Date`, `Quantity`,
  `EffectivePrice`, `CostInBillingCurrency`, `SubscriptionId`, and `MeterId`.
  Do not mix that contract with FOCUS. The corresponding FOCUS concepts are
  `ChargePeriodStart`, `ConsumedQuantity`, `x_EffectiveUnitPrice`, `BilledCost`,
  `SubAccountId`, and `x_SkuMeterId`.
- **Append, never overwrite:** a Cost Management export **replaces** the
  month-to-date file on every run, so Bronze appends each stamped extract and Silver
  reads the latest per charge period.

### bronze_apim_gateway_requests
- **PK:** `RequestId`
- **Grain:** one row per model request
- **Refresh:** near real-time (DCR stream) / daily batch
- **Source:** APIM AI gateway → Data Collection Rule → Log Analytics `ApimAiGateway_CL`
- **Permissions:** Log Analytics Reader
- **Columns:** ApiName, Appid, BackendId, BusinessUnitClaim, CachedPromptTokens,
  ClientId, CompletionTokens, CostCenterClaim, DeploymentRegion, IsError,
  IsStreaming, ModelName, ModelVersion, Oid, OperationName, PromptTokens, RequestId,
  StatusCode, TableName, TimeGenerated, TotalLatencyMs, TotalTokens, UpnOrAppName
- **Descriptions:** the **only per-identity attribution path for Foundry**. Carries no
  dollars; it supplies the token shares Silver uses to split the FOCUS bill.
- **Type fidelity:** Log Analytics returns **every field as a string**, including
  numbers and booleans, and unset values arrive as `""` or the literal `"None"`.
  Bronze keeps them exactly that way; Silver casts.
- **Missing `ClientId` is data, not dirt:** a request without the JWT claim still
  burned billed tokens, so it stays in the extract and surfaces as unallocated spend
  instead of silently disappearing.
- **Why Azure Monitor metrics are NOT collected:** `microsoft.insights/metrics` token
  counts carry **no identity dimension**, so they would only duplicate numbers this
  feed already provides, without the one column that makes attribution possible.

### bronze_apim_client_ownership
- **PK:** `ClientId`
- **Grain:** one row per registered gateway client
- **Refresh:** on change (current-state snapshot, overwrite)
- **Source:** Log Analytics `ApimClientOwnership_CL` (customer-maintained)
- **Permissions:** Log Analytics Reader
- **Columns:** AppName, BusinessUnit, ClientId, CostCenter, TableName, Team,
  TenantId, TimeGenerated, Type, _ResourceId
- **Descriptions:** the client registry. Silver uses it as the **fallback owner** when
  a caller is absent from the identity map; because it names the business unit rather
  than keying it, Silver conforms that name to a `business_unit_key`.

### bronze_azure_ai_logs  *(Phase 2)*
- **PK:** `request_id`
- **Grain:** one row per model request
- **Refresh:** near real-time
- **Source:** Log Analytics `AzureDiagnostics` (resource-level diagnostic settings)
- **Permissions:** Log Analytics Reader
- **Columns:** request_id, timestamp, resource_id, deployment, caller_ip,
  api_subscription_id, prompt_tokens, completion_tokens, total_tokens, status_code,
  duration_ms, user_or_sp_id
- **Descriptions:** resource-side request detail. Only worth adding where traffic does
  **not** flow through the APIM gateway; `bronze_apim_gateway_requests` already gives
  richer, claim-based attribution for everything that does.

### bronze_azureml_cost  *(NEW — see §3)*
- **Covered by `bronze_focus_cost`.** AML compute, managed endpoints and storage are
  ordinary Azure charge lines, so they arrive on the same FOCUS export with
  `ServiceName = 'Azure Machine Learning'`. No separate collector is needed; Silver
  only needs a service→platform mapping entry.

### bronze_azureml_usage  *(NEW — see §3, Phase 2)*
- **PK:** `event_time + workspace_id + entity_id`
- **Grain:** per job/endpoint event
- **Refresh:** hourly
- **Source:** Monitor metrics + AML diagnostic logs
- **Permissions:** Monitoring Reader
- **Columns:** event_time, workspace_id, compute_target, job_id, endpoint_id,
  deployment_id, node_hours, request_count, latency_ms, gpu_utilization, submitted_by
- **Descriptions:** AML compute/endpoint usage for allocation.

### bronze_fabric_capacity  *(NEW — see §3, Phase 2)*
- **PK:** `usage_date + capacity_id + workspace_id + operation_type`
- **Grain:** daily CU consumption per workspace/operation
- **Refresh:** daily
- **Source:** Fabric Capacity Metrics semantic model (XMLA) + Monitor metrics
- **Permissions:** Fabric Admin / Capacity Admin; Monitoring Reader
- **Columns:** usage_date, capacity_id, sku, workspace_id, item_id, operation_type,
  workload, cu_seconds, interactive_cu, background_cu, throttled, user_or_sp_id
- **Descriptions:** Fabric CU **detail** incl. **Copilot-in-Fabric**. The capacity
  **dollars** already arrive on `bronze_focus_cost`; this feed only adds the CU
  breakdown needed to charge them back to a workspace.

### Reference / master-data collectors (non-telemetry, but Bronze-landed)

### bronze_ref_identity_map  *(NEW — critical)*
- **PK:** `identity_key`
- **Grain:** one row per resolved principal
- **Refresh:** daily (Entra) / on-change
- **Source:** Entra `users` + `servicePrincipals` + manual login↔UPN map
- **Permissions:** `Directory.Read.All`, `Application.Read.All`
- **Columns:** identity_key, display_name, principal_type, upn, entra_object_id,
  github_login, is_human, team, business_unit, home_business_unit_key, cost_center_key
- **Descriptions:** the join key that makes cross-platform identity resolution possible.

### bronze_ref_app_inventory  *(NEW)*
- **PK:** `application_key`
- **Grain:** one row per application/workload
- **Source:** CMDB / app-ownership registry / Azure resource tags
- **Columns:** application_key, application_name, application_type, owner_upn,
  owner_business_unit_key, default_environment_key, criticality, gateway_app_name,
  gateway_client_id, azure_resource_name, is_mock
- **Descriptions:** maps SPs/resources/tags → owning app & BU (attribution backbone).
  `azure_resource_name` is what resolves a gateway `BackendId` to an application, and
  `gateway_client_id` ties the calling principal to the same app.

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
- **Columns:** agent_key, agent_name, bot_id, platform, environment_id,
  environment_name, owner_upn, owner_business_unit_key, cost_center_key, purpose,
  created_on
- **Descriptions:** attributes non-human agent spend to an owner/BU. `bot_id` is the
  join to Dataverse `_msdyn_botid_value`; without it, credits cannot reach a BU.

### bronze_ref_rate_card  *(NEW)*
- **PK:** `rate_key`
- **Grain:** one row per priced unit
- **Source:** EA/MCA price sheet + published list prices
- **Columns:** rate_key, platform, unit_type, model, unit_price_usd, currency,
  discount_pct, effective_from, effective_to
- **Descriptions:** converts seats/tokens/credits/premium-requests → comparable $.

---

## 3. Missing collectors (gap analysis)

Current mock model has: `bronze_focus_cost`, `bronze_apim_gateway_requests`,
`bronze_apim_client_ownership`, `bronze_m365_copilot_usage`/`_seats`/`_credits`,
`bronze_dataverse_msdyn_aievent`, `bronze_ghc_seats`, `bronze_ghc_premium_usage`,
and the 5 reference feeds.

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
| bronze_focus_cost | **All Azure-billed $** (AOAI, Studio PAYG, Fabric) | Cost Management FOCUS 1.0r2 | charge period+resource+SkuPriceId | charge line | BilledCost, EffectiveCost, ConsumedQuantity, PricingQuantity, Tags |
| bronze_apim_gateway_requests | Per-identity token attribution | APIM → Log Analytics | RequestId | request | ClientId, Oid, PromptTokens, CompletionTokens, ModelName |
| bronze_apim_client_ownership | Client registry / fallback owner | Log Analytics | ClientId | client | AppName, BusinessUnit, CostCenter, Team |
| bronze_m365_copilot_usage | Seat utilization / idle | Graph usage report | refresh date+UPN | user/day | Last Activity Date, per-app activity dates |
| bronze_m365_copilot_seats | Fixed seat basis | Graph subscribedSkus | date+UPN+SKU | user/SKU/day | skuPartNumber, capabilityStatus, consumedUnits |
| bronze_m365_copilot_credits | Variable M365 (Cowork/Autopilot) | M365 billing | date+consumer+meter | consumer/day | capability, creditsConsumed, costUsd |
| bronze_dataverse_msdyn_aievent | Studio credits per agent | Dataverse msdyn_aievents | msdyn_aieventid | event | msdyn_creditconsumed, _msdyn_botid_value, msdyn_billingtype |
| bronze_ghc_seats | GitHub fixed seat + idle | GH billing/seats | date+login | seat/day | last_activity_at, plan_type |
| bronze_ghc_premium_usage | GitHub variable overage | GH billing/usage | date+username+sku | user/day | quantity, modelMultiplier, netAmount |
| bronze_ref_identity_map | Identity resolution | Entra + map | identity_key | principal | upn, github_login, is_human |
| bronze_ref_app_inventory | App/BU attribution | CMDB/tags | application_key | app | azure_resource_name, gateway_client_id, owner_business_unit_key |
| bronze_ref_business_hierarchy | Chargeback/budget | Finance MD | business_unit_key | BU | monthly_budget_usd |
| bronze_ref_agent_inventory | Agent→owner/BU | Studio/M365 | agent_key | agent | bot_id, owner_business_unit_key |
| bronze_ref_rate_card | Unit→$ where nothing is billed | Price sheet | rate_key | priced unit | unit_price_usd |
| *(Phase 2)* bronze_m365_copilot_interactions | Engagement depth | Graph aiInteraction | interaction_id | interaction | app_class, interaction_type |
| *(Phase 2)* bronze_studio_transcripts | Conversation detail | Dataverse | conversation_id | conversation | activities_json, outcome |
| *(Phase 2)* bronze_ghc_metrics | ROI/acceptance | GH metrics | date+editor+lang+model | agg/day | acceptances_count |
| *(Phase 2)* bronze_azure_ai_logs | Non-gateway request detail | Log Analytics | request_id | request | total_tokens, user_or_sp_id |
| *(Phase 2)* bronze_azureml_usage | AML compute usage | Monitor/logs | time+ws+entity | job/endpoint | node_hours, request_count |
| *(Phase 2)* bronze_fabric_capacity | CU detail (Copilot-in-Fabric) | Metrics app XMLA | date+capacity+ws+op | op/day | cu_seconds, workload |

### MVP vs Phase 2 — what to load first

**MVP (load these 14 — proves the whole FinOps story end-to-end):**
- **One cost contract** (`bronze_focus_cost`) for every Azure-billed service, plus the
  attribution feeds that contract cannot provide: APIM gateway + ownership, Dataverse
  events, M365 usage/seats/credits, GitHub seats/premium.
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
- **Silver** types and conforms: `silver_cost_charge` (typed FOCUS, Tags decoded),
  `silver_gateway_request` (identity + app resolved, LA strings cast),
  `silver_studio_event` (Dataverse credits per agent), then the two allocations
  (`silver_foundry_allocation`, `silver_studio_allocation`) that split **billed**
  dollars onto identities by token/credit share, and `silver_usage_conformed`
  (every platform on one daily grain, `cost_is_estimated` separating invoiced from
  modelled, untagged → `BU-UNALLOC`).
- **Gold** = the existing star: `fact_ai_usage` + `dim_platform/identity/model/
  application/business_unit/cost_center/date/environment/rate_card`.
- Implementation: `platform/medallion/` (Spark) and `platform/data-store/build_store.py`
  (the same logic in SQLite, runnable with no Fabric capacity).
