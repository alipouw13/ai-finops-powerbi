# AI FinOps Accelerator — Clone & Run Runbook

Everything here runs **Fabric-free** with the Python standard library only (no pip
installs, no Power BI license, no cloud). All data is **MOCK** and tagged as such.
A coworker can clone this repo and see the full story — per-persona BI dashboards +
the AI insight layer — in under a minute.

```bash
git clone https://github.com/natesanshreyas/ai-finops-powerbi.git
cd ai-finops-powerbi

# 1. Build the portable data store (raw extracts → Silver → Gold)
python3 platform/fabric/gen_bronze_data.py     # -> platform/fabric/bronze_out/*.csv
python3 platform/data-store/build_store.py     # -> platform/data-store/finops.db

# 2. Launch the persona dashboards + AI layer
python3 platform/localhost/app.py                 # -> http://localhost:8080
```

Open <http://localhost:8080> and click through the tabs.

---

## What you get

### 5 persona BI dashboards (KPIs + charts)
| Tab | Answers |
|-----|---------|
| **CFO / Finance** | Total spend, spend by business unit, budget variance, forecast, fixed vs variable |
| **Governance** | Spend by platform (REAL vs MOCK honesty), model mix, human vs non-human identities, unallocated $ |
| **Engineering** | Tokens, requests, errors, latency, per-model performance |
| **App Owner** | Cost by application, app × model spend, trends |
| **License Optimization** | Idle licensed seats + reclaimable $ |

### AI insight layer (🤖 Ask AI tab)
Natural-language question → intent routing → parameterized SQL over the Gold model →
grounded answer + **the SQL it ran** (auditable) + evidence rows. This mocks Fabric
Copilot / semantic-model Q&A but runs 100% locally. Try:
- "Which business unit spent the most last month?"
- "Which licenses should be reclaimed?"
- "What are the highest cost models?"
- "Where can we reduce AI spend?"

---

## The 5-step pipeline (ends at the Fabric push)

| Step | Layer | Artifact |
|------|-------|----------|
| 1. Extract | Source telemetry | `docs/extractable-data-by-product.md`, `extractable_data_catalog` |
| 2. Raw/Bronze | Source-faithful extracts (FOCUS, APIM, M365, Dataverse, GitHub) | `platform/fabric/gen_bronze_data.py` → `bronze_out/*.csv` |
| 3. Silver + Gold | Conform, allocate billed cost onto identities, emit the star | `platform/data-store/build_store.py` → `finops.db` |
| 4. BI + AI | Semantic model + dashboards + Q&A | `AIFinOps.SemanticModel/`, `platform/localhost/app.py` |
| 5. **Fabric push** | Land Bronze in a Lakehouse, then run the notebooks | `platform/fabric/load_bronze.py`, `platform/medallion/` |

Steps 1–4 run today with no license. Step 5 is the handoff to whoever owns the
Fabric workspace — see `docs/bronze-layer-architecture.md`.

The step-3 build **fails loudly** if allocated cost does not equal the billed invoice,
if the Gold column contract drifts from the semantic model, or if any fact row has no
matching dimension row.

## Also runnable
- **Power BI Desktop:** open `AIFinOps.pbip` (10 persona report pages, no Fabric needed).
- **Ad-hoc SQL:** `python3 platform/data-store/build_store.py --query "SELECT product, COUNT(*) FROM extractable_data_catalog GROUP BY product"`

> All figures are **MOCK** demo data. Nothing here is relabeled as real customer spend.
