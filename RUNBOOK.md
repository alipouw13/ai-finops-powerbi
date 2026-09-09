# AI FinOps Accelerator — Clone & Run Runbook

Everything here runs **Fabric-free** with the Python standard library only (no pip
installs, no Power BI license, no cloud). All data is **MOCK** and tagged as such.
A coworker can clone this repo and see the full story — per-persona BI dashboards +
the AI insight layer — in under a minute.

```bash
git clone https://github.com/natesanshreyas/ai-finops-powerbi.git
cd ai-finops-powerbi

# 1. Build the portable data store (Bronze + Gold + extractable-data catalog)
python3 platform/data-store/build_store.py        # -> platform/data-store/finops.db

# 2. Launch the persona dashboards + AI layer
python3 platform/localhost/app.py                 # -> http://localhost:8080
```

On Windows use `python` rather than `python3`:

```powershell
python platform\data-store\build_store.py
python platform\localhost\app.py
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
| 2. Bronze  | Raw per-platform tables | `platform/fabric/gen_bronze_data.py` → `bronze_out/*.csv` (MOCK)<br>`platform/fabric/extract_m365_graph.py` → `bronze_real/*.csv` (**REAL**, Microsoft Graph) |
| 3. Store   | Portable data source | `platform/data-store/build_store.py` → `finops.db` |
| 4. Gold + BI + AI | Semantic model + dashboards + Q&A | `AIFinOps.SemanticModel/`, `platform/localhost/app.py` |
| 5. **Fabric push** | Land Bronze in a Lakehouse | `platform/fabric/load_bronze.py` (run once a Power BI license is assigned) |

Steps 1–4 run today with no license. Step 5 is the handoff to whoever owns the
Fabric workspace — see `docs/bronze-layer-architecture.md`.

## Also runnable
- **Power BI Desktop:** open `AIFinOps.pbip` (10 persona report pages, no Fabric needed).
  Run `python platform/validate/validate_pbip.py --fix-data-folder` first.
- **Validate the PBIP offline:** `python platform/validate/validate_pbip.py`
- **REAL M365 licence data:** `python platform/fabric/extract_m365_graph.py --probe`
  (see `platform/fabric/README-m365-graph.md`)
- **Ad-hoc SQL:** `python3 platform/data-store/build_store.py --query "SELECT product, COUNT(*) FROM extractable_data_catalog GROUP BY product"`

> All figures in the committed CSVs are **MOCK** demo data. Nothing here is
> relabeled as real customer spend. The one REAL path is
> `extract_m365_graph.py`, which writes to a gitignored directory and reports
> seat counts as REAL while keeping derived dollars `cost_is_estimated=TRUE`.
