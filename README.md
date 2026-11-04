# AI FinOps & Tokenomics — FOCUS 1.2 & Multi-Platform Power BI Accelerator

A working Power BI Project (PBIP) and Microsoft Fabric Medallion pipeline delivering a unified **FOCUS 1.2-native** FinOps & Tokenomics view across **all Azure AI Services** (**Azure AI Foundry / Azure OpenAI PAYG & PTUs**, **Document Intelligence**, **Speech**, **Content Safety**, **Azure Machine Learning**, and **Supporting AI Infrastructure** like **AI Search**, **APIM AI Gateway**, **Cosmos DB**, **Managed Redis**, and **Fabric Capacity**), plus **Microsoft 365 Copilot**, **M365 Copilot Cowork / Work IQ**, **Microsoft Copilot Studio**, **GitHub Copilot Enterprise**, and **Agent 365 Governance**.

Real vs. modelled provenance is a **first-class column in the model** (`dim_platform[data_source]` and `fact_ai_usage[cost_is_estimated]`) surfaced as **`Cost Confidence %`** on Page 1.

> **Deploying for an enterprise customer with live FOCUS 1.2 exports, APIM AI Gateway, Agent 365 & GitHub Copilot APIs?**
> See **[docs/CUSTOMER-IMPLEMENTATION-GUIDE.md](docs/CUSTOMER-IMPLEMENTATION-GUIDE.md)** for the complete production blueprint, APIM XML policies, Graph/GitHub scopes, and FOCUS 1.2 column mappings.
>
> **Want the runnable demo (persona dashboards + AI Q&A layer, no Fabric/license)?**
> See **[RUNBOOK.md](RUNBOOK.md)** — clone, run two commands, open http://localhost:8080.

---

## Quick start

```bash
git clone https://github.com/alipouw13/ai-finops-powerbi.git
cd ai-finops-powerbi
python3 platform/fabric/gen_bronze_data.py   # regenerate FOCUS 1.2 & multi-platform Bronze CSVs
python3 build_data.py                        # conform Bronze -> 16 Gold star-schema CSVs (0 orphans, tied-out)
python3 build_pbip.py                        # regenerate TMDL semantic model (16 tables, 25 relationships, 80 measures)
python3 build_report.py                      # regenerate all 13 Power BI report pages
```

1. Open `AIFinOps.pbip` in **Power BI Desktop** (Dec 2023+, with *Preview → Power BI Project* on).
2. **Transform data → Manage Parameters → `DataFolder`** — defaults automatically to your local `AIFinOps.SemanticModel\data\` folder.
3. **Refresh.**

> The semantic model is the durable artifact — **16 tables, 25 relationships, 80 DAX measures, and 13 report pages**.

---

## Data provenance — read this before demoing

| Platform (`dim_platform`) | Billing & Telemetry Source | Status in Repo |
|---|---|---|
| **Azure AI Foundry & OpenAI** (`Foundry`) | `law-apim-finops` → `ApimAiGateway_CL` + `bronze_focus_cost.csv` (PAYG, MaaS, PTU Reservations) | ✅ **REAL** (176 live APIM requests on `2026-08-07`) + **FOCUS 1.2** multi-day |
| **Azure AI Services** (`AzureAI`) | `bronze_focus_cost.csv` (Document Intelligence, Speech STT/TTS, Content Safety) | ✅ **FOCUS 1.2 Schema** (Mock values, production schema) |
| **Azure Machine Learning** (`AzureML`) | `bronze_focus_cost.csv` (`Standard_NC24ads_A100_v4` GPU + Managed Endpoints) | ✅ **FOCUS 1.2 Schema** (Mock values, production schema) |
| **Azure AI Supporting Infra** (`AzureInfra`) | `bronze_focus_cost.csv` (AI Search S2, APIM v2, Cosmos DB, Redis Semantic Cache, Log Analytics, Fabric F8 CU) | ✅ **FOCUS 1.2 Schema** (Captures RAG & gateway TCO) |
| **Agent 365 Registry** (`dim_agent`) | M365 Admin Center > Agents export (`393` tenant agents) | ✅ **REAL** tenant registry export (`2026-10-26`), linked via `agent_key` |
| **Microsoft 365 Copilot** (`M365Copilot`) | Graph `subscribedSkus` + `getMicrosoft365CopilotUsageUserDetail` | ✅ **2 REAL seats** verified in tenant + 8 persona mock seats |
| **M365 Copilot Cowork / Work IQ** (`CopilotCowork`) | M365 Admin Cost Mgmt / FOCUS `Microsoft Copilot Studio` meter | ⚠️ **MOCK** (Cowork, Autopilot, Work IQ API, SharePoint Agents) |
| **Microsoft Copilot Studio** (`CopilotStudio`) | Dataverse `msdyn_aievents` + FOCUS P3 Commit & PAYG meters | ⚠️ **MOCK** (Models Prepaid Pack vs P3 Commit vs PAYG, M365 zero-rating & BYOM exclusion) |
| **GitHub Copilot Enterprise** (`GitHubCopilot`) | REST `/copilot/billing/seats`, `/settings/billing/usage`, `/copilot/metrics` | ⚠️ **MOCK** (Seats + Premium Request overages w/ 13× Code Review multiplier) |

### Live M365 source check — 2026-10-23

A read-only check of the selected demo tenant found **5 enabled Microsoft 365
Copilot seats, 2 assigned, and 3 unassigned**. The signed-in admin's Copilot
service plans were successfully provisioned. These are license counts, not
invoice amounts; Graph does not supply the customer's seat price.

The Graph v1.0 Copilot usage report (`D7`, `v2`) returned HTTP 403,
`S2SUnauthorized / Invalid permission`, with the existing Azure CLI credentials.
It requires `Reports.Read.All` on the calling client and a supported reporting
role for delegated access. A follow-up interactive sign-in did not complete.

Azure Cost Management returned 1,910 month-to-date daily service/meter rows,
with no further pages and usage dates through October 23. No M365 Copilot,
Cowork, or Copilot Credit meter was observed in that subscription. Six
**Microsoft Fabric** `Copilot and AI Capacity Usage CU` rows totaled **$17.64
USD**; these are not M365 Cowork charges. Missing M365 meters do not establish
zero consumption or prove that pay-as-you-go is configured.

The bundled CSVs, portable database, and dashboards remain unchanged and MOCK
for M365. A successful licensing check alone must not relabel synthetic usage
or modeled seat costs as real.

The deployed Fabric flow was subsequently wired and successfully run on October
23: two live seat assignments reached the Gold fact, with seat costs still
estimated. All 1,910 billing rows and explicit source-access statuses passed
through separate Bronze/Silver/Gold evidence tables. See the
[deployed pipeline runbook](RUNBOOK.md#deployed-fabric-pipeline). This does not
change the provenance of the bundled local demo or make the new evidence tables
automatically visible in Power BI.

The Foundry rows come from the sibling
[`ai-gateway-apim-finops`](https://github.com/natesanshreyas/ai-gateway-apim-finops) gateway:
Entra JWT → claims (`cc:`, `bu:`) → APIM policy → DCR → Log Analytics. That gateway is the
**only** way to get per-user Foundry attribution — Azure Monitor's token metrics have no
identity dimension at all.

All real rows land on `2026-08-07` (one load-test day). Mocked platforms span 60 days so the
trend visuals are usable. Run `scripts/traffic.py` in the gateway repo for more real days.

---

## Model

```
                    dim_date ──┐
                dim_platform ──┤
                dim_identity ──┤   (universal identity: Human · ServicePrincipal ·
                   dim_model ──┼──►  fact_ai_usage  (1,892 rows)   ManagedIdentity · Agent)
             dim_cost_center ──┤     grain: date × platform × identity × model × unit_type
            dim_business_unit ──┤
              dim_application ──┤
              dim_environment ──┘
                                     dim_rate_card  (disconnected — the input)
```

**Grain:** one row per `usage_date × platform × identity × model × unit_type`.
`unit_type` ∈ `token · copilot_credit · premium_request · seat_day · prompt`.

**Conformed dimensions (v2).** `dim_business_unit`, `dim_application`, and
`dim_environment` each join to the fact on a single key (clean star, no ambiguous
paths). They unlock *spend by business unit / application / environment* and the
CFO, App-Owner and Optimization personas. `dim_identity` carries a universal
`identity_class` (Human · ServicePrincipal · ManagedIdentity · Agent · Application)
because not every AI request maps to a person. Budgets, criticality and SLA tiers
on these dims are **MOCK** (`is_mock` / `is_mock_budget` columns) — overwrite with
customer values. Regenerate keys with `python3 build_dimensions.py` (additive; reads
the CSVs as source of truth, never regenerates the model).

Key measures: `Total AI Cost`, `Billed Cost`, `Modelled Cost`, **`Cost Confidence %`**,
`Fixed Cost`, `Variable Cost`, `Total Tokens`, `Cache Hit Rate`, `Cost per 1K Tokens`,
`Copilot Credits`, `Premium Requests`, **`Idle Licensed Users`**, `Idle Seat Waste (monthly)`,
`Cost per Active User`, `MoM Cost Delta %`.

**Cost-model measures (v2):** `Discounted Cost`, `Discount Savings` (per-platform
negotiated rates), `Forecast Cost (EOM)`, `Forecast Cost (next 30d, net)`,
`Attributable Cost`, `Unallocated Cost`, **`Chargeback Coverage %`**, `Chargeback Cost`
(direct + pro-rata unallocated), `Monthly Budget`, `Budget Variance`, `Budget Variance %`.
These answer actual / discounted / forecast / chargeback for the CFO persona.

### Pages (13 Report Pages)
1. **1 · FOCUS & FinOps Executive Overview** — FOCUS List → Contracted → Effective waterfall, FinOps Effective Savings Rate (`ESR %`), Billed vs Modelled `Cost Confidence %`, Actionable Waste
2. **2 · Foundry, APIM & Azure AI Tokenomics** — LLM tokens, prompt cache hit %, $/1K tokens, PTU reservation spend, PTU & P3 utilization %, APIM Gateway coverage %, and full Azure AI Service & Supporting Infra TCO breakdown
3. **3 · Waste, PTU & Seat Optimization** — Idle Copilot seats (28d), Unused PTU reservation capacity (`CommitmentDiscountStatus = 'Unused'`), Bypassed APIM Gateway spend, and per-principal reclamation table
4. **4 · Rate Card & FOCUS Discounts** — `dim_rate_card` customer input + EA/MCA Negotiated Discount Savings vs PTU/P3 Commitment Discount Savings
5. **5 · CFO — Finance & Chargeback** — FOCUS Effective Cost, ESR %, EOM Forecast, Budget Variance, and full BU Chargeback (Direct + Pro-Rata Unallocated)
6. **6 · Governance & Gateway Control** — APIM Gateway routing coverage %, Bypassed Gateway shadow-AI spend, Supporting Infra % of TCO, Agent Attributed spend, and Human vs SP vs Agent mix
7. **7 · Engineering — APIM & Model SLOs** — Token consumption trend, $/1K tokens across OpenAI/MaaS/Reasoning models, P95/avg latency, and error rates
8. **8 · Application & RAG TCO Owner** — Total Application TCO (combining LLM tokens + AI Search + Cosmos DB + Redis Semantic Cache + DocIntel), accountable owners, and MoM velocity
9. **9 · License & Credit Optimization** — Reclaimable seat waste by BU and Copilot Credit breakdown (`CreditPack` vs `Committed P3` vs `Standard PAYG` vs `ZeroRated`)
10. **10 · Extractable Data Spectrum** — Full catalogue of every AI cost & telemetry signal across FOCUS 1.2, APIM, Agent 365, M365, Studio, and GitHub (`REAL` / `AVAILABLE` / `MOCK`)
11. **11 · User Cost Allocation** — `fact_user_cost_allocation` re-keying 100% of AI spend to human accountability (`DIRECT` vs `OWNER` vs `UNALLOCATED`)
12. **12 · Agent 365 Governance & Spend** — 393 real Agent 365 registry agents (`Owner assigned`, `Microsoft-managed`, `Publisher-managed`, `Unowned tenant agent`) linked directly to attributed AI spend
13. **13 · Persona Coverage** — Spend and primary vs secondary tool affinity across the 7 enterprise stakeholder personas

---

## Cost rationalization — the rationale

### 1. There is no common unit, so cost is the only conformed measure

Four platforms, four incompatible billing units, and **only one exposes tokens**:

| Platform | Unit | Tokens? | Native $? |
|---|---|---|---|
| Azure AI Foundry | tokens | ✅ | ✅ |
| GitHub Copilot | premium requests | ❌ | ✅ |
| Copilot Studio | Copilot Credits | ⚠️ partial¹ | ❌ |
| M365 Copilot | seats | ❌ | ❌ |

¹ Copilot Studio's *Text and generative AI tools* meter is token-denominated — 0.1 / 1.5 / 10
credits per 1K tokens for basic / standard / premium. Everything else is per-event.

A literal cross-platform "tokenomics" dashboard **cannot be built.** Normalising on **USD**
with a `unit_type` dimension is the only thing that reconciles. That's why the fact table
carries `quantity` + `unit_type` rather than a token column.

### 2. Fixed vs variable matters more than the total

```
FIXED — already on your invoice, telemetry adds nothing
  M365 Copilot seats      × $30/user/mo   ← does not move with usage
  GitHub Copilot seats    × $39/user/mo   ← does not move with usage

VARIABLE — the only half FinOps can influence
  Foundry tokens · GitHub premium requests · Copilot Studio credits
```

For ordinary usage included in the M365 Copilot seat license, prompt volume does
not change the seat fee. **Cowork and other usage-based services are separate**:
they can consume Copilot Credits and require their own billing/consumption feed.
Do not infer their charges from prompt counts or from Fabric Copilot capacity
meters. Splitting fixed seat fees from separately metered consumption is what
stops the dashboard being a restatement of the invoice.

### 3. The rate card is customer-specific, and that's a feature

Nobody pays list. Foundry has PTU vs PAYG vs reservations vs EA/MCA discounts. GitHub has
volume tiers and included allowances. Copilot Studio has 25k packs ($0.008/credit) vs PAYG
($0.01) vs CCCU prepurchase. M365 Copilot is whatever your EA says.

So **every price lives in exactly one place** — `dim_rate_card` — and nowhere else in the
model. Swapping a customer's real rates is editing one CSV. Ship the PoC with list prices,
let them overwrite. Microsoft publishes no price API for M365 Copilot or Copilot Studio,
so this is manual by necessity, not by design.

### 4. Label modelled dollars or lose the room

`cost_is_estimated` flows into **`Cost Confidence %`** and it belongs on page 1. In this
build only ~10% of spend is billed; the rest is rate-card arithmetic. A dashboard that mixes
billed and modelled dollars without saying so is how a FinOps programme loses credibility the
first time Finance reconciles it against an invoice.

### 5. The real money is waste, not unit price

`Idle Licensed Users` — paid seats with zero activity in 28 days — needs **no cost telemetry
at all**, and at $30/seat, 200 idle users is $6,000/month recoverable. Higher ROI than any
token optimisation, and it works on the platform with the *worst* telemetry.

### 6. Don't double-count bring-your-own-model

Copilot Studio agents on your own Foundry deployment are billed **separately** — Microsoft's
rates *"exclude bring-your-own-model configurations, including Azure Foundry models."* That
usage appears in Foundry cost, not credits. Summing both naively double-counts.

### 7. Don't model Copilot Studio credits from activity counts

M365 Copilot–licensed users are **zero-rated** for classic answers, generative answers, agent
actions, tenant graph grounding, and agent flows. Identical activity costs 0 or 12 credits
depending purely on the invoker's licence. `msdyn_creditconsumed` is already net of this —
**use it directly.** The rate table is for forecasting only.

---

## Going live

| Platform | What you need |
|---|---|
| **Foundry** | Already live. More days: run `scripts/traffic.py` in the gateway repo. |
| **Copilot Studio** | Publish an agent, have a few conversations. Credits land in `msdyn_aievents` within hours. Dataverse read access already works — no new credentials. |
| **GitHub Copilot** | Copilot **Business** ($19/user/mo) or Enterprise on the org, ≥1 seat, and the **"Copilot usage metrics" policy enabled**. Premium-request USD additionally needs GitHub **Enterprise Cloud** + a classic PAT with `admin:enterprise`. |
| **M365 Copilot** | An M365 Copilot SKU in the tenant, plus an app registration with **`Reports.Read.All` (Application)** and admin consent. Also **disable** *"Display concealed user names"* in M365 admin → Settings → Org settings → Reports, or UPNs arrive hashed and attribution is impossible. |

### Known limits
- Foundry token metrics carry **no user identity** — the APIM gateway is the only path
- M365 Copilot: Global cloud only (no GCC High / DoD / 21Vianet)
- GitHub metrics: no data before 2025-10-10, 1-year retention; premium requests 24 months
- `msdyn_aievent` is **per-environment**, not tenant-wide
- Cost Management month-to-date exports **replace, never append**
- Azure Monitor metrics retention is 93 days — export or lose history

---

## Files

```
build_data.py                     real gateway JSON + mock → 7 CSVs
build_dimensions.py               additive: conformed BU/app/env dims + universal identity
build_personas.py                 additive: 5 persona pages + extractable data spectrum
build_pbip.py                     → TMDL semantic model
build_report.py                   → 4-page report layout
AIFinOps.pbip                     open this
AIFinOps.SemanticModel/
  definition/model.tmdl           relationships + DataFolder parameter
  definition/tables/*.tmdl        11 tables, 42 measures
  data/*.csv                      ← swap these for live extracts
  synonyms.linguistic.json        Q&A / Fabric Copilot synonyms (standalone, apply-on-demand)
AIFinOps.Report/report.json       10 pages (4 original + 5 persona + data spectrum)
platform/medallion/               Fabric bronze/silver/gold notebooks (→ the gold star)
docs/ARCHITECTURE.md              decision record (rationale/tradeoffs/value/effort)
docs/extractable-fields.md        per-platform field catalog (M365/GHC/Studio/Foundry) + medallion verdict
docs/medallion-tables.md          full Bronze/Silver/Gold table inventory + Gold column schemas
docs/medallion-examples.md        worked example rows for every table, traced Bronze→Silver→Gold
docs/ai-insight-layer.md          Fabric Copilot + NL + RAG strategy
data/                             raw Log Analytics exports (real Foundry)
```

## References
- [Copilot Credits billing rates](https://learn.microsoft.com/en-us/microsoft-copilot-studio/requirements-messages-management)
- [msdyn_AIEvent table reference](https://learn.microsoft.com/en-us/power-apps/developer/data-platform/reference/entities/msdyn_aievent)
- [Azure OpenAI monitoring data reference](https://learn.microsoft.com/en-us/azure/foundry/openai/monitor-openai-reference)
- [getMicrosoft365CopilotUsageUserDetail](https://learn.microsoft.com/en-us/microsoft-365/copilot/extensibility/api/admin-settings/reports/copilotreportroot-getmicrosoft365copilotusageuserdetail)
- [GitHub Copilot metrics REST](https://docs.github.com/en/rest/copilot/copilot-metrics)
- [GitHub billing usage REST](https://docs.github.com/en/enterprise-cloud@latest/rest/billing/usage)
- Sibling: [`ai-gateway-apim-finops`](https://github.com/natesanshreyas/ai-gateway-apim-finops)
