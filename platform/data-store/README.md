# AI FinOps Data Store (`finops.db`)

A **portable, Fabric-free** SQLite database that holds the entire AI FinOps data
set in one file. This is the interchange format: it runs anywhere today (no
Fabric license, no Power BI, no cloud), and a teammate can lift it straight into
a Fabric Lakehouse or Azure SQL when the tenant is unblocked.

> All fact/usage/cost rows are **MOCK** (tagged `_data_class=MOCK` with lineage
> columns). The *schema, grains, and field catalog* are real and production-shaped.

## What's inside

`build_store.py` loads two things into `finops.db`:

1. **16 Bronze tables** — the raw per-platform telemetry from
   `platform/fabric/bronze_out/*.csv` (~2,792 rows).
2. **`extractable_data_catalog`** — a 74-row queryable metadata table listing
   every extractable field for all 7 products (product, category, field,
   description, source API, grain). This is the machine-readable companion to
   `docs/extractable-data-by-product.md`.

| Table | Rows | What it is |
|---|---|---|
| `bronze_azure_ai_cost` | 360 | Foundry/AOAI real $ by day/meter |
| `bronze_azure_ai_metrics` | 180 | AOAI tokens/requests/latency (resource-grain, no identity) |
| `bronze_foundry_gateway` | 534 | Per-user Foundry tokens via APIM AI Gateway (preferred over metrics) |
| `bronze_fabric_capacity_cost` | 60 | Fabric CU + capacity $ |
| `bronze_ghc_seats` | 480 | GitHub Copilot seat assignments + last activity |
| `bronze_ghc_premium_usage` | 32 | GHC premium-request overage (legacy; AI Credits are current) |
| `bronze_m365_copilot_seats` | 480 | M365 Copilot licenses |
| `bronze_m365_copilot_usage` | 480 | M365 per-app last activity |
| `bronze_m365_copilot_credits` | 40 | M365 Copilot Credits |
| `bronze_m365_cowork_usage` | 32 | M365 Copilot Cowork add-on (Copilot Credits @ $0.01) |
| `bronze_studio_credits` | 72 | Copilot Studio credits by agent/action |
| `bronze_ref_*` (5 tables) | 42 | Identity map, app/BU hierarchy, agents, rate card |
| `extractable_data_catalog` | 74 | Every extractable field per product |

## Run it

```bash
# (re)generate the Bronze CSVs first if needed
python3 platform/fabric/gen_bronze_data.py

# build the database
python3 platform/data-store/build_store.py

# ad-hoc query
python3 platform/data-store/build_store.py --query \
  "SELECT product, COUNT(*) fields FROM extractable_data_catalog GROUP BY product ORDER BY 2 DESC"
```

## Handoff to Fabric

The teammate standing up Fabric can:
1. Open `finops.db` (any SQLite client) or the source CSVs in `bronze_out/`.
2. Load each Bronze table into a Lakehouse (`platform/fabric/load_bronze.py`
   does exactly this once a Power BI license is assigned).
3. Build Silver/Gold on top per `docs/bronze-layer-architecture.md`.
