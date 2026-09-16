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

## Real Azure billed cost (FOCUS 1.0 export)

Azure cost comes from a Cost Management **FOCUS 1.0** Parquet export, surfaced in
Fabric through a zero-copy OneLake shortcut. Two stdlib-only scripts stand it up
(`az login` for auth; both support `--dry-run` and `--steps`):

```bash
# 1. Azure side: resource group, ADLS Gen2 account, container, FOCUS exports + runs
python3 platform/deploy/create_cost_export.py

# 2. Fabric side: ADLS cloud connection + OneLake shortcut into bronze_real
python3 platform/deploy/create_onelake_shortcut.py

# 3. In Fabric, run the medallion notebooks in order, then refresh the model:
#      03_bronze_azure_focus  ->  bronze_azure_cost_focus
#      10_silver_conform      ->  silver_usage_azure + silver_azure_cost_detail
#      20_gold_star           ->  fact_ai_usage + dim_*
```

| Piece | Value |
|---|---|
| Dataset | FOCUS 1.0 (FinOps Foundation open spec), Parquet, daily granularity |
| Landing | `focus/{exportName}/{dateRange}/{runId}/part_0_0001.parquet` + `manifest.json` |
| Files shortcut | `Files/azure_costmgmt_focus` in `bronze_real` — storage stays in the customer's subscription |
| Bronze table | `bronze_azure_cost_focus` — a Files shortcut is not queryable as a table |
| Table shortcut | `Tables/dbo/bronze_azure_cost_focus` in the silver lakehouse |
| Provenance | `cost_is_estimated = false` — billed invoice cost, never rate-card modelled |

**Verified end to end:** 79,426 FOCUS rows / **$7,048.73** billed Azure spend →
`fact_ai_usage` 81,172 rows / $12,818.69, reconciling exactly (`AzureInfra`
$6,342.24 + `AzureAI` $706.49) at 100% cost confidence on both platforms.

Two non-obvious constraints the scripts handle:

- Cost Management **refuses to create an export when shared-key auth is disabled**
  on the destination storage account (HTTP 400). Tenants with a Modify policy that
  forces `allowSharedKeyAccess=false` need a policy exemption first.
- A Custom-timeframe export's `from`/`to` must sit **inside one calendar month**, so
  historical backfill is one export per month.

> **De-duplicate at period level, never row level** — see
> `platform/medallion/README.md`. A row-key dedupe silently drops ~30% of the cost.

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
