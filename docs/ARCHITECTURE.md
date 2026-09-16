# AI FinOps platform — architecture & decision record

Scope: evolve the PoC into a reusable enterprise **AI Cost Management** accelerator
on Microsoft Fabric + Power BI, giving one view of AI usage, licensing, governance,
chargeback and optimization across Azure AI Foundry, Azure OpenAI, APIM AI Gateway,
M365 Copilot, GitHub Copilot, Copilot Studio, and future platforms.

Every decision below carries **rationale / tradeoffs / business value / effort**
(effort: S ≈ hours, M ≈ days, L ≈ weeks).

---

## 1. Medallion on Fabric (bronze/silver/gold)
See `platform/medallion/`. Bronze = raw append-only history; silver = conformed
grain (USD + `unit_type`); gold = the semantic star.

- **Rationale.** AI telemetry is fragmented and lossy (Azure Monitor 93-day
  retention; Cost-Management MTD *replaces*; GitHub metrics 1-yr). A retained
  bronze layer is the only way to answer year-over-year cost questions.
- **Tradeoffs.** Three physical copies of data vs one. Justified: cheap Delta
  storage, and silver/gold are always rebuildable from bronze.
- **Value.** Auditability + historical trend + a clean contract to the model.
- **Effort.** M for Foundry (connector exists); M each for the mock platforms.

## 2. Cost is the only conformed measure; `unit_type` is a dimension
- **Rationale.** Only Foundry exposes tokens. A literal cross-platform
  "tokenomics" view is impossible; USD with a `unit_type` (`token · copilot_credit
  · premium_request · seat_day · prompt`) is the only reconciliation. Microsoft 365
  Copilot **Cowork** rides this design unchanged: its usage is billed in **Copilot
  Credits** (there is no "Cowork unit"), so it conforms as `copilot_credit` on
  `platform_key = M365Copilot` — a *consumptive* add-on inside `Variable Cost`, not a
  second seat SKU — surfaced by the `Cowork Credits` / `Cowork Add-on Cost` measures.
- **Tradeoffs.** Loses a single physical activity unit; gains a coherent total.
- **Value.** A defensible headline number Finance can reconcile to invoices.
- **Effort.** Already implemented (`fact_ai_usage`).

## 3. Conformed dimensions added in v2
`dim_business_unit`, `dim_application`, `dim_environment`, plus universal identity
on `dim_identity`. Each new dim has a **single** relationship to the fact (clean
star, no ambiguous paths); BU is reached through the fact key, not a second hop
through identity.

| Dim | Rationale | Value | Effort |
|---|---|---|---|
| dim_business_unit | identity and cost_center carried *divergent* BU labels — conform once | chargeback, budget variance, BU allocation | S (done) |
| dim_application | "spend by application" is the App-Owner persona's core question | optimization targeting, showback | S (done) |
| dim_environment | dev/test/prod split exposes non-prod waste | reclaimable spend | S (done) |
| universal identity | not every request is a person (SP/MI/agent) | correct attribution across platforms | S (done) |

## 4. Universal identity model
`identity_class ∈ {Human, ServicePrincipal, ManagedIdentity, Agent, Application}`
with `is_human`. Foundry attribution *requires* the APIM gateway because Azure
Monitor token metrics have no identity dimension at all. Silver now realises this:
it **prefers** the per-identity `bronze_foundry_gateway` feed (APIM AI Gateway →
Log Analytics) over the resource-grain `bronze_azure_ai_metrics`, falling back only
when the gateway feed is absent — never reading both, the same anti-double-count
pattern as FOCUS-over-`usageDetails`. What remains unattributable lands honestly on
the `Unattributed Identity` (`Unknown`) member, surfaced by `Unattributed Requests`
/ `Unattributed Request %`; it can never reach zero while invoice-grain rows are in
scope.

- **Rationale.** Agents and service principals now drive material AI spend with no
  human in the loop; per-user assumptions silently misattribute cost.
- **Tradeoffs.** Some platforms (M365) only emit human UPNs; the vocabulary is
  wider than today's data exercises — intentional, so future rows conform without
  a schema change.
- **Value.** Honest attribution; enables "spend with no human owner" governance.
- **Effort.** S (done); ManagedIdentity/Agent rows arrive when those sources land.

## 5. Cost model: actual / discounted / forecast / chargeback
Measures in `fact_ai_usage.tmdl`. Discount is a per-platform attribute
(`dim_platform.enterprise_discount_pct`, MOCK per contract). Chargeback grosses up
unallocated spend pro-rata. Budget lives on `dim_business_unit` (MOCK).

- **Rationale.** Finance needs list→net→forecast→showback, not just a total.
- **Tradeoffs.** Forecast is straight-line MTD projection — simple and explainable;
  swap for a time-series model (Fabric AutoML) when history is deep enough.
- **Value.** Budget variance and chargeback are the CFO's two headline asks.
- **Effort.** M (done); AutoML forecast is a later M.

## 6. Rate card and provenance as first-class data
Every price lives only in `dim_rate_card` (disconnected input). Real-vs-mock is
`dim_platform.data_source` + `fact.cost_is_estimated`, surfaced as `Cost
Confidence %` on page 1. List/rate-card cost is carried per row on
`fact_ai_usage[list_cost_usd]` with a `[has_rate_card]` flag — Azure rows take the
list price straight from FOCUS `ListCost`, every other platform prices from
`dim_rate_card`, and rows a rate card never priced fall back to `cost_usd` (flagged
`has_rate_card = FALSE`) rather than showing a misleading \$0. Page 4 compares
`Billed Cost` vs `Rate Card Cost` per platform on that basis.

- **Rationale.** Nobody pays list; a FinOps programme loses credibility the first
  time modelled dollars are mistaken for billed ones.
- **Value.** One-CSV customer onboarding; honesty in the demo.
- **Effort.** S (done).

## 7. Persona reporting
The report is 8 pages. `build_report.py` builds pages 1–4 (Spend Overview,
Engineering Tokenomics, Licence Seats/Waste & Utilisation, Rate Card);
`build_personas.py` appends 4 persona pages — **CFO Finance, Governance,
Application Owner, Extractable Data Spectrum** — over the same model. The old
standalone *Engineering* and *License Optimization* persona pages are removed;
their content folds into pages 2 and 3. Report pages are additive/idempotent so the
model and pages 1–4 are never at risk.

## 8. Azure Cost Management FOCUS export via ADLS shortcut
Azure billed spend now lands as Cost Management **FOCUS** Parquet exports in a
customer-owned ADLS Gen2 account, surfaced to Fabric through a OneLake shortcut:
Cost Management export → `stfinopscost848055/costexports/focus` →
`Files/azure_costmgmt_focus` in lakehouse `LH_tokenomics_bronze_real`.

Verified implementation:

| Surface | Detail |
|---|---|
| Azure scope | Tenant `840a80c0-e4a5-47be-8a1d-7ecfa61e839c`, subscription `a699796c-ab5c-48bf-8bd7-adb31e225f11`, resource group `rg-finops-costexport` in `eastus2` |
| Storage | ADLS Gen2 account `stfinopscost848055` (`StorageV2`, `Standard_LRS`, hierarchical namespace enabled), container `costexports`, root folder `focus` |
| Exports | `finops-focus-daily` Active Daily MonthToDate; `finops-focus-bf-202606`, `finops-focus-bf-202607`, `finops-focus-bf-202608` Inactive Custom monthly backfills |
| Export contract | Cost Management api-version `2023-07-01-preview`; `definition.type = FocusCost`; `dataVersion = 1.0` (as deployed; the ingest also tolerates the current **1.2-preview** dataset — see note below); Parquet; `partitionData = true`; `granularity = Daily` |
| Fabric | Workspace `AI-tokenomics`; cloud connection `finops-costexports-adls` (`AzureDataLakeStorage`, `server` + `path`, account-key auth); OneLake shortcut `Files/azure_costmgmt_focus` in lakehouse `LH_tokenomics_bronze_real` |

- **Rationale.** FOCUS is the FinOps Foundation's open cost specification:
  vendor-neutral column names that let the same silver logic later absorb AWS/GCP
  exports. It also carries `BilledCost`, `EffectiveCost`, `ListCost`, and
  `ContractedCost`, which is the actual/discounted/list split the CFO persona
  needs (and `ListCost` now feeds `fact_ai_usage[list_cost_usd]` / `Rate Card
  Cost`). That replaces the old modelled discount assumption for the Azure slice.
  The export was deployed at `dataVersion = 1.0`, but Microsoft's current dataset is
  **`1.2-preview`**, which renames `x_InvoiceId → InvoiceId`, `x_PricingCurrency →
  PricingCurrency` and `x_SkuMeterName → SkuMeter`; `10_conform_usage.py` now picks
  each FOCUS column by presence (`focus_col`), so it reads either version.
- **Tradeoffs.** OneLake shortcuts are zero-copy: no duplicated storage bill and
  no second retention policy to govern. The customer keeps the export data in
  their own subscription and controls retention/access there. The cost is an
  external dependency: if the ADLS account, connection, or shortcut breaks, bronze
  cannot read new Azure cost rows. The export is append-per-run, not a mutable
  single object, so overlap must be resolved at **period** level — never on a
  composed row key, which silently deletes genuine charges (see the gotchas).
- **Value.** Azure/Foundry resource cost is now REAL/billed, not mock. Verified end
  to end: **79,426 FOCUS rows / $7,048.73** billed spend over 2026-06-01 →
  2026-09-17, flowing into `fact_ai_usage` (81,172 rows, $12,818.69) and
  reconciling exactly — `AzureInfra` $6,342.24 + `AzureAI` $706.49 = $7,048.73, at
  100% cost confidence on both platforms. Real AI/analytics spend included
  Microsoft.Fabric, Azure AI Search, Azure Machine Learning, Azure AI Services,
  Databricks and Cosmos DB. M365 Copilot, GitHub Copilot and Copilot Studio remain
  MOCK until their live feeds are wired.
- **Downstream shape.** `03_ingest_azure_costmgmt_focus.py` materialises the
  shortcut into the Delta table `bronze_azure_cost_focus` (a Files shortcut is not
  queryable as a table), which is re-exposed to the silver lakehouse as a OneLake
  **table** shortcut. Silver emits `silver_usage_azure` (conformed,
  `unit_type = azure_meter`) plus `silver_azure_cost_detail`, which keeps FOCUS's
  billed/effective/list/contracted measures that the single-cost fact cannot carry.
  FOCUS is preferred over the legacy `bronze_azure_cost` extract, with fallback, so
  Azure cost never double-counts across the two feeds.
- **Effort.** M (done) for Azure export, policy exemption, ADLS container, Fabric
  cloud connection, OneLake shortcut, and verified parquet reads.

Operational gotchas:

| Gotcha | Impact | Fix |
|---|---|---|
| Management-group policy assignment `MCAPSGovDeployPolicies` silently forces `publicNetworkAccess=Disabled` and `allowSharedKeyAccess=false` on storage writes via a Modify effect | Cost Management export creation fails with HTTP 400: `"Key-based authentication is currently disabled on this storage account."`; the storage PATCH may return HTTP 200 and still be reverted | Create a policy exemption (`Waiver`) scoped to the resource group, then PATCH the storage account to `publicNetworkAccess=Enabled` and `allowSharedKeyAccess=true`. While public data plane access is disabled, create the blob container through the ARM management plane, not the data plane. |
| Custom timeframe `from` and `to` must be inside the same calendar month | Multi-month backfills fail with HTTP 400: `"'From' and 'To' dates should be within same month."` | Use one inactive Custom export per backfill month (`finops-focus-bf-202606`, `finops-focus-bf-202607`, `finops-focus-bf-202608`). |
| De-duplicating FOCUS on a composed row key (resource + meter + charge period) | Silently deletes genuine charges: measured at **~75% of rows and 30% of the cost** ($7,048.73 → $4,919.71), with no error and a plausible-looking total | Resolve overlap at period level in bronze: newest `runId` wins per `(export, dateRange)`, then one export wins wholesale per charge month (the closed-month backfill beats the rolling MonthToDate feed). |

## Target-state diagram
```
 Foundry/AOAI usage ─┐
 APIM Gateway ───────┤
 Azure Cost Mgmt ─► ADLS Gen2 ─► OneLake shortcut ─┐
 M365 Copilot ───────┤                             │
 GitHub Copilot ─────┤   Fabric: Bronze ─► Silver ─┴► Gold ─► Power BI semantic model ─► Persona reports
 Copilot Studio ─────┘   (Delta, OneLake)             (star)       (import today /            + Fabric Copilot Q&A
                                                                    DirectLake later)          + RAG insight layer
```

## Roadmap (not yet built)
| Item | Value | Effort |
|---|---|---|
| Live connectors for remaining mock platforms | Azure/Foundry billed cost is REAL; M365 Copilot, GitHub Copilot, and Copilot Studio still need live feeds | M each |
| DirectLake gold + scheduled bronze ingest | live cost; no refresh for new *rows* (a **schema** change reframes — see the deploy findings in the root and medallion READMEs) | M |
| AutoML forecast replacing straight-line | tighter budget calls | M |
| Anomaly detection (cost spikes) + alerts | proactive FinOps | M |
| Fabric Copilot Q&A + RAG insight layer | NL self-serve | see `ai-insight-layer.md` |
