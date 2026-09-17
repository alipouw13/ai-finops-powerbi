# Definitively Extractable Fields — M365 Copilot · Copilot Studio · GitHub Copilot · Azure AI Foundry / Azure OpenAI

This is the **field-level companion** to `AIFinOps.SemanticModel/data/extractable_data_catalog.csv`
(which is the signal-level summary that drives the *Extractable Data Spectrum*
report page). Everything below is grounded in current Microsoft / GitHub API
documentation (links inline). Each source lists: **endpoint · permission ·
grain · refresh/retention · fields · cost authority · analytics it feeds**.

> Provenance rule (unchanged): every field lands with a `data_source` /
> `availability` tag. REAL, AVAILABLE, MOCK, ROADMAP never blur.

---

## 0. TL;DR on the architecture question

> *"Take ALL extractable data into Bronze, all relevant data into Silver, and the
> actual tables that back the Power BI visuals into Gold — is that good?"*

**Yes — that is exactly right, and it's the standard Fabric medallion pattern.**
Bronze = source-faithful raw, Silver = conformed/relevant, Gold =
star tables the semantic model binds to. Five refinements make it production-grade
for FinOps specifically — see [§6](#6-architecture-verdict--refinements). The short
version:

1. **Bronze fidelity = "as fine as each API allows"** — several of these sources
   are *pre-aggregated* (GitHub = daily org/enterprise rollups; M365 = per-user
   *last-activity dates*, not per-prompt). You cannot land finer than the API grain.
2. **Cost authority ≠ usage telemetry.** Actual $ comes from **Azure Cost
   Management** (Foundry/AOAI/Studio PAYG) and **seat × price** (M365, GitHub).
   Token/message telemetry *explains* cost; it doesn't *define* it. Reconcile, don't derive.
3. **Identity resolution is the hard, high-value Silver step** (GitHub login ↔
   Entra user ↔ UPN ↔ service principal ↔ agent). Budget for it.
4. **Content/PII (prompts, transcripts) needs a governed Bronze zone**, not the
   broadly-readable lakehouse.
5. **Gold stays a star** (import or Direct Lake) — don't over-normalize.

---

## 1. Azure AI Foundry / Azure OpenAI

The only platform with **true per-request, per-token, real-dollar** granularity.

### 1a. Azure Monitor platform metrics — `GET .../providers/microsoft.insights/metrics`
Grain: per **deployment / model / resource**, 1-min+ aggregations. Retention 93 days (metrics).
Permission: `Monitoring Reader`. Cost authority: **driver only** (not $).
Ref: [Monitor Azure OpenAI](https://learn.microsoft.com/en-us/azure/ai-foundry/openai/how-to/monitor-openai).

| Metric (representative, documented) | Meaning | Feeds |
|---|---|---|
| `AzureOpenAIRequests` | request count | Engineering, error rate |
| `ProcessedPromptTokens` | input tokens | tokenomics, unit cost |
| `GeneratedTokens` | output/completion tokens | tokenomics, unit cost |
| `ProcessedInferenceTokens` / `ActiveTokens` | inference token volume | capacity, cost driver |
| `TokenTransaction` (billed tokens) | billable token count | cost reconciliation |
| `PromptTokenCacheMatchRate` | cache hit % | optimization (cache savings) |
| `TimeToResponse` / normalized latency | latency | Engineering SLO |
| `AzureOpenAIProvisionedManagedUtilizationV2` | PTU utilization % | fixed-cost (PTU) optimization |
| Fine-tuning metrics (training hrs, etc.) | tuning usage | cost, governance |

### 1b. Diagnostic logs (Diagnostic Setting → Log Analytics / Eventhouse)
Grain: **per API call**. Categories: `RequestResponse`, `Audit`, `Trace`.
Fields: `TimeGenerated`, `OperationName`, `DurationMs`, `ResultSignature` (HTTP status),
`CallerIPAddress`, `properties` → `modelDeploymentName`, `modelName`, `apiName`,
`apiVersion`, `streamType`, and the **caller identity** (AAD object id / API-key hash).
Feeds: per-request cost attribution, identity-level usage, error/latency, abuse detection.

### 1c. GenAI tracing (App Insights, if apps are instrumented)
OpenTelemetry GenAI semantic conventions: `gen_ai.request.model`, `gen_ai.response.model`,
`gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`, `gen_ai.operation.name`,
span latency, prompt/response (if logging enabled). Feeds: **application**-level attribution,
agent traces. *(AVAILABLE — requires app instrumentation.)*

### 1d. **Cost authority** — Cost Management export, **FOCUS 1.0r2**
Grain: one **provider charge line** (typically meter × resource × day). Fields:
`BilledCost`, `EffectiveCost`, `ListCost`, `ConsumedQuantity`/`ConsumedUnit`,
`PricingQuantity`/`PricingUnit`, `ResourceId`, `SubAccountId`, `Tags`,
`x_ResourceGroupName`, `x_SkuMeterName`, `x_SkuDetails`, `ChargePeriodStart`.
**This is the authoritative actual $** for Foundry/AOAI, Copilot Studio PAYG, and
Fabric capacity — one contract for all of them. Feeds: CFO actuals, realised discount
(`ListCost − BilledCost`), chargeback by tag/resource.

> Use the [FOCUS schema](https://learn.microsoft.com/en-us/azure/cost-management-billing/dataset-schema/cost-usage-details-focus),
> not the [legacy EA `UsageDetails` shape](https://learn.microsoft.com/en-us/azure/cost-management-billing/dataset-schema/cost-usage-details-ea).
> The EA equivalents of the fields above are `Date`, `Quantity`, `EffectivePrice`,
> `CostInBillingCurrency`, `SubscriptionId`, and `MeterId` — do not mix the two.
> The charge line carries **no identity**: pair it with the APIM gateway feed (1a) and
> allocate by token share to attribute real dollars to a person or service.

---

## 2. Microsoft 365 Copilot

Seat-licensed (flat ~$30/user/mo). **No per-token cost.** FinOps value = **utilization
vs. assigned seats** (idle-seat reclaim) + adoption + governance/data-access risk.

### 2a. Usage — `GET /reports/getMicrosoft365CopilotUsageUserDetail(period='D7')`
Permission: `Reports.Read.All`. Grain: **per user, last-activity dates** (NOT per prompt).
Refresh: daily; periods D7/D30/D90/D180. Returns 302 → CSV.
Ref: [getMicrosoft365CopilotUsageUserDetail](https://learn.microsoft.com/en-us/graph/api/reportroot-getmicrosoft365copilotusageuserdetail?view=graph-rest-beta).
**Exact CSV columns:**
`Report Refresh Date`, `Report Period`, `User Principal Name`, `Display Name`,
`Last Activity Date`, `Microsoft Teams Copilot Last Activity Date`,
`Word Copilot Last Activity Date`, `Excel Copilot Last Activity Date`,
`PowerPoint Copilot Last Activity Date`, `Outlook Copilot Last Activity Date`,
`OneNote Copilot Last Activity Date`, `Loop Copilot Last Activity Date`,
`Copilot Chat Last Activity Date`.
Feeds: **idle-seat detection** (license assigned but no `Last Activity Date`), adoption by app.

### 2b. Interaction history — `GET /copilot/users/{id}/interactionHistory/getAllEnterpriseInteractions`
Permission: `AiEnterpriseInteraction.Read.All`. Grain: **per prompt / per response**.
Ref: [aiInteraction](https://learn.microsoft.com/en-us/microsoft-365/copilot/extensibility/api/ai-services/interaction-export/resources/aiinteraction).
**Properties:** `id`, `appClass` (e.g. `IPM.SkypeTeams.Message.Copilot.Excel`),
`conversationType` (`bizchat`/`appchat`), `interactionType` (`userPrompt`/`aiResponse`),
`from` (identitySet), `createdDateTime`, `sessionId`, `requestId`, `body` (itemBody),
`attachments`, `contexts`, `links`, `mentions`, `locale`, `etag`.
Feeds: adoption depth, app distribution, governance. **⚠ contains prompt/response content
→ governed Bronze zone + Purview.**

### 2c. Licensing — `GET /subscribedSkus`, `GET /users?$select=assignedLicenses`
Fields: `skuPartNumber` (`Microsoft_365_Copilot`), `prepaidUnits` (enabled/suspended),
`consumedUnits`; per user `assignedLicenses`, assignment date (via audit/directory).
**This × seat price = M365 Copilot cost.** Feeds: CFO seat cost, License Optimization.

### 2d. Audit (governance) — Purview Unified Audit Log / `auditLogs`
`CopilotInteraction` events: `AppHost`, `AccessedResources` (files/sites referenced),
`Operation`, `UserId`, `CreationTime`, `ClientIP`. Feeds: Governance risk, oversharing.

---

## 3. GitHub Copilot

Seat-licensed ($19 Business / $39 Enterprise) **plus** metered premium features.
FinOps value = **idle-seat reclaim** + adoption + (new) premium-request overage cost.

### 3a. **Seats (idle detection)** — `GET /orgs/{org}/copilot/billing/seats`
Permission: `manage_billing:copilot` or `read:org`. Grain: **per assigned user**.
Ref: [Copilot user management](https://docs.github.com/en/rest/copilot/copilot-user-management).
Per-seat fields: `assignee.login`, `assignee.id`, `created_at` (seat assigned),
**`last_activity_at`**, `last_activity_editor`, `pending_cancellation_date`, `plan_type`.
Feeds: **idle-seat reclaim** (`last_activity_at` null/stale), seat tenure.

### 3b. Billing summary — `GET /orgs/{org}/copilot/billing`
`seat_breakdown` → `total`, `added_this_cycle`, `pending_invitation`,
`pending_cancellation`, `active_this_cycle`, `inactive_this_cycle`; plus
`seat_management_setting`, `plan_type`, `public_code_suggestions`, `ide_chat`, `cli`.
Feeds: CFO seat cost, **`inactive_this_cycle` = reclaimable spend**.

### 3c. Usage metrics — `GET /orgs|enterprises/{}/copilot/metrics` + report-download endpoints
Permission: metrics policy enabled. Grain: **daily, aggregated** (org/enterprise/repo;
newer report exports add user-level & team-level). 28-day window (older endpoint); the
report endpoints (`enterprise-1-day`, `enterprise-28-day/latest`, `repos-1-day`) return
signed NDJSON/CSV download links, up to 1 yr history.
Refs: [usage metrics API](https://docs.github.com/en/rest/copilot/copilot-usage-metrics) ·
[field reference](https://docs.github.com/en/copilot/reference/copilot-usage-metrics/copilot-usage-metrics).
Representative fields/metrics: `date`, `total_active_users`, `total_engaged_users`;
IDE completions: suggestions/acceptances, **lines suggested/accepted**, by `language`,
`editor`, `model`; IDE chat: `total_chats`, insertions, copies; dotcom chat & PR summaries;
model usage per chat mode (ask/edit/plan/agent); agent adoption. Feeds: Engineering
adoption, acceptance rate, model/language distribution.
> Privacy floor: metrics need ≥5 active Copilot users to return data.

### 3d. **Cost authority** — Enhanced Billing Platform usage `GET /organizations/{org}/settings/billing/usage`
Fields per line: `date`, `product`, `sku`, `quantity`, `unitType`, `netAmount`,
`grossAmount`, `discountAmount`, `repositoryName`. Captures **premium-request overage**
(metered) beyond flat seats. Feeds: CFO actuals incl. variable GitHub cost.

---

## 4. Copilot Studio

Consumption-billed in **messages** (capacity packs) or **pay-as-you-go** Azure meter.
Data lives in **Dataverse** + Application Insights.
Ref: [Copilot Studio analytics](https://learn.microsoft.com/en-us/microsoft-copilot-studio/analytics-overview)
(Monitor data 360 days; session/transcript detail 28 days; UTC).

### 4a. Conversation transcripts — Dataverse `conversationtranscript` table
Grain: **per conversation** (transcript = JSON of activities/turns). Fields: `conversationtranscriptid`,
`botid` (agent), `conversationid`, `content` (activities JSON: messages, triggered topics,
timestamps, channel), `createdon`, `schematype`. Access: Dataverse Web API / dataflow
(needs *Bot Transcript Viewer*). Feeds: per-agent usage, topic analytics, deflection.
**⚠ contains user content → governed zone.**

### 4b. Analytics / Monitor metrics (Dataverse + Monitor page)
Sessions, engagement rate, resolution rate, escalation rate, abandonment, CSAT,
topics triggered, outcomes, per-agent/per-environment. Feeds: App-Owner effectiveness,
Governance adoption.

### 4c. Billing / capacity — Power Platform Admin + Azure meter (PAYG)
Messages consumed per agent/environment; generative "answers" & AI Builder credit
consumption; environment capacity. **PAYG → Azure Cost Management meter = actual $.**
Feeds: CFO actuals, cost-per-agent, cost-per-resolution.

### 4d. App Insights telemetry (optional wiring)
`customEvents`/`dependencies` with `conversationId`, `activityId`, `botId`, latency,
generative-answer calls. Feeds: Engineering latency, error, model calls.

---

## 5. What each platform can and cannot answer (grain honesty)

| Question | Foundry/AOAI | M365 Copilot | GitHub Copilot | Copilot Studio |
|---|---|---|---|---|
| Per-request / per-token cost | ✅ real $ + tokens | ❌ (flat seat) | ⚠ premium-req metered only | ⚠ per-message |
| Per-user usage | ✅ (via caller id) | ✅ last-activity + interactions | ✅ seats + user report | ⚠ via transcripts |
| Idle-license reclaim | n/a (PAYG) | ✅ seats vs usage | ✅ `last_activity_at` | n/a (consumption) |
| Per-application attribution | ✅ (SP/tags/traces) | ⚠ appClass only | ⚠ repo-level | ✅ per agent |
| Real dollars authority | Cost Mgmt | seats × price | seats × price + billing usage | Cost Mgmt (PAYG) |

**Design consequence:** the unified fact grain is **daily × platform × identity ×
application × model × cost**, and each source fills the columns it *can* — nulls where
the API grain doesn't reach. Don't fake a finer grain than the source supports.

---

## 6. Architecture verdict + refinements

**Verdict: the Bronze(all) → Silver(relevant/conformed) → Gold(PBI star) design is
correct and is what a customer accelerator should ship.** Map it like this:

**BRONZE — source-faithful raw, one landing zone per source above**
- Land each API/report *as returned* (JSON/CSV/NDJSON), plus ingestion metadata
  (`_source`, `_ingested_at`, `_watermark`, `_report_refresh_date`).
- **Incremental** per source cadence: AOAI logs near-real-time; GitHub 28-day/daily
  reports; M365 daily refresh; Studio 28-day transcript window.
- **Governed sub-zone** for content (M365 interaction bodies, Studio transcripts,
  AOAI prompt/response) with restricted access + retention policy. Everything else
  in the open analytics zone.
- Keep raw even if unused today — that's the "source fidelity / historical retention"
  the brief asks for and future-proofs new metrics.

**SILVER — conform to the unified taxonomy (the hard, valuable work)**
- **Identity resolution**: map GitHub `login` ↔ Entra user ↔ UPN ↔ service principal ↔
  agent/bot id → one `identity_key` with `identity_class`/`is_human` (already in the model).
- **Application / BU / environment** normalization from tags, SP ownership, repo→app,
  agent→app maps.
- **Model** normalization (deployment name → canonical model family).
- **Cost normalization & reconciliation**: join usage drivers to **Cost Management**
  actuals and to a **rate/price dimension** (seat prices, token prices, message prices,
  EA/MCA discounts). Flag `cost_is_estimated` where modelled vs billed.
- Harmonize everything to a **daily** grain (the coarsest common denominator).

**GOLD — the exact star the semantic model binds to**
- Materialize `fact_ai_usage` + the dims (`dim_date/platform/identity/model/cost_center/
  rate_card/business_unit/application/environment/data_source`). This is what the 10 report
  pages query — no over-normalization, keep it a clean star.
- **Direct Lake** (recommended at scale): point the semantic model at Gold delta tables →
  no import refresh, near-real-time. Import mode is fine for the demo (today's CSVs).

**Five refinements to call out to a customer:**
1. *Grain honesty* (per §5) — set expectations that M365/GitHub are seat+adoption, not per-token.
2. *Cost authority* — Cost Management + price sheet is the source of $; telemetry explains it.
3. *Identity graph* — the single biggest engineering effort and the thing that makes
   cross-platform chargeback possible.
4. *Content governance* — prompts/transcripts are sensitive; isolate + Purview + retention.
5. *Reconciliation loop* — modelled cost vs billed cost variance is itself a Gold metric
   (already surfaced as `Cost Confidence %`).

**Effort / value:** Bronze connectors ≈ per-source S; Silver identity+cost conform ≈ L
(highest value); Gold ≈ S (already designed here). The current repo *is* the Gold + semantic
contract — Fabric bronze/silver just have to deliver these columns.
