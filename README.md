# AI FinOps & Tokenomics — Power BI PoC

A working Power BI Project (PBIP) showing unified AI spend across **Microsoft 365 Copilot**,
**Copilot Studio**, **GitHub Copilot Enterprise**, and **Azure AI Foundry**.

One slice is real. Three are mocked. **Which is which is a first-class column in the model**
(`dim_platform[data_source]`) rather than a footnote, because the difference between billed
and modelled dollars is the single most important thing to be honest about in an AI FinOps
conversation.

> **Want the runnable demo (persona dashboards + AI layer, no Fabric/license)?**
> See **[RUNBOOK.md](RUNBOOK.md)** — clone, run two commands, open http://localhost:8080.

---

## Quick start

```bash
git clone https://github.com/natesanshreyas/ai-finops-powerbi.git
cd ai-finops-powerbi
python3 build_data.py        # regenerate CSVs
```

1. Open `AIFinOps.pbip` in **Power BI Desktop** (Dec 2023+, with *Preview → Power BI Project* on).
2. **Transform data → Manage Parameters → `DataFolder`** — set to the absolute path of
   `AIFinOps.SemanticModel/data/` (trailing slash, escaped backslashes on Windows).
3. **Refresh.**

> If Desktop rejects `report.json`, delete it and reopen the `.pbip`. Desktop regenerates a
> blank report bound to the same semantic model and you drag the measures on. The semantic
> model is the durable artifact — 11 tables, 8 relationships, 42 measures.

---

## Data provenance — read this before demoing

| Platform | Source | Status |
|---|---|---|
| **Azure AI Foundry** | `law-apim-finops` → `ApimAiGateway_CL` | ✅ **REAL** — 176 requests, real tokens, real per-user identity |
| GitHub Copilot Enterprise | — | ⚠️ MOCK — org has **0 Copilot seats**; metrics policy disabled |
| Copilot Studio | — | ⚠️ MOCK — Dataverse readable, `msdyn_aievents` returned **0 rows** |
| Microsoft 365 Copilot | — | ⚠️ MOCK — tenant has **no M365 Copilot SKU** |

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

### Pages
1. **Spend Overview** — total, fixed vs variable, confidence, platform capability matrix
2. **Foundry Tokenomics** — the only real tokenomics; in/out/cached, cache hit rate, $/1K
3. **Waste & Utilisation** — idle seats and recoverable spend
4. **Rate Card** — the editable input, plus billed-vs-modelled by platform

**Persona pages (v2)** — one page per stakeholder, built on the conformed dims:
5. **CFO — Finance** — spend, discounts, forecast, budget variance, chargeback by BU
6. **Governance** — adoption by principal type, platform usage, REAL-vs-MOCK risk register
7. **Engineering** — token consumption, unit economics by model, latency, error rate (Foundry REAL)
8. **Application Owner** — spend by application, trend, MoM delta, criticality
9. **License Optimization** — idle licensed users, reclaimable spend, seat utilisation
10. **Extractable Data Spectrum** — full catalogue of every AI cost signal per platform (source API, identity grain, cost fidelity) and its status: REAL / AVAILABLE / MOCK / ROADMAP

Persona pages are (re)generated additively by `python3 build_personas.py`, which
preserves pages 1–4 and only touches sections named `PERSONA_*` / `DATA_SPECTRUM`.

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

M365 Copilot has **zero** variable cost. A user sending 5,000 prompts and one sending zero
bill identically. Splitting the headline number is what stops the dashboard being a
restatement of the invoice.

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
platform/fabric/gen_bronze_data.py  generates the MOCK raw extracts (FOCUS, APIM, M365, Dataverse, GitHub)
platform/fabric/bronze_out/*.csv    those extracts, source-faithful column for column
platform/fabric/load_bronze.py      pushes the raw extracts into a Fabric Lakehouse
platform/data-store/build_store.py  runs bronze→silver→gold into finops.db (no Fabric needed)
platform/medallion/               Fabric bronze/silver/gold notebooks (→ the gold star)
platform/localhost/app.py         5 persona dashboards + AI insight layer over finops.db
docs/ARCHITECTURE.md              decision record (rationale/tradeoffs/value/effort)
docs/bronze-layer-architecture.md raw-layer contracts, per-table specs, collector gap analysis
docs/extractable-fields.md        per-platform field catalog (M365/GHC/Studio/Foundry) + medallion verdict
docs/medallion-tables.md          full Bronze/Silver/Gold table inventory + Gold column schemas
docs/medallion-examples.md        real example rows for every layer, traced Bronze→Silver→Gold
docs/ai-insight-layer.md          Fabric Copilot + NL + RAG strategy
data/                             raw Log Analytics exports (real Foundry)
```

## References
- [Copilot Credits billing rates](https://learn.microsoft.com/en-us/microsoft-copilot-studio/requirements-messages-management)
- [FOCUS cost and usage details file schema](https://learn.microsoft.com/en-us/azure/cost-management-billing/dataset-schema/cost-usage-details-focus)
- [EA cost and usage details file schema](https://learn.microsoft.com/en-us/azure/cost-management-billing/dataset-schema/cost-usage-details-ea)
- [msdyn_AIEvent table reference](https://learn.microsoft.com/en-us/power-apps/developer/data-platform/reference/entities/msdyn_aievent)
- [Azure OpenAI monitoring data reference](https://learn.microsoft.com/en-us/azure/foundry/openai/monitor-openai-reference)
- [getMicrosoft365CopilotUsageUserDetail](https://learn.microsoft.com/en-us/microsoft-365/copilot/extensibility/api/admin-settings/reports/copilotreportroot-getmicrosoft365copilotusageuserdetail)
- [GitHub Copilot metrics REST](https://docs.github.com/en/rest/copilot/copilot-metrics)
- [GitHub billing usage REST](https://docs.github.com/en/enterprise-cloud@latest/rest/billing/usage)
- Sibling: [`ai-gateway-apim-finops`](https://github.com/natesanshreyas/ai-gateway-apim-finops)
