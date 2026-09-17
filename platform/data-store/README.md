# AI FinOps Data Store (`finops.db`)

A **portable, Fabric-free** SQLite database that holds the entire medallion in one
file: raw extracts, conformed silver, and the gold star. It runs anywhere today (no
Fabric licence, no Power BI, no cloud, no dependencies), and a teammate can lift it
straight into a Fabric Lakehouse or Azure SQL when the tenant is unblocked.

`build_store.py` is not a loader — it is **the pipeline**, and the runnable twin of the
Spark notebooks in [`platform/medallion/`](../medallion/). Same table names, same
column names, same logic, different engine. That is what makes the design reviewable
and verifiable without a capacity.

> All fact/usage/cost rows are **MOCK** (tagged `_data_class=MOCK` with lineage
> columns). The *schemas, grains, contracts and transformations* are production-shaped.

## Run it

```bash
# 1. (re)generate the raw extracts
python3 platform/fabric/gen_bronze_data.py

# 2. run bronze -> silver -> gold
python3 platform/data-store/build_store.py

# 3. ad-hoc SQL
python3 platform/data-store/build_store.py --query \
  "SELECT platform_key, ROUND(SUM(cost_usd),2) FROM fact_ai_usage GROUP BY 1 ORDER BY 2 DESC"
```

The build prints a reconciliation and **fails** if allocated cost does not equal the
billed invoice, if the gold column contract drifts from the semantic model, or if any
fact row has no matching dimension row.

## BRONZE — raw, source-faithful extracts

Column names, casing, types and null behaviour match what the real API hands over.

| Table | Rows | Source contract |
|---|---|---|
| `bronze_focus_cost` | 1,147 | Cost Management export, **FOCUS 1.0r2** — 96 provider columns. Covers Azure OpenAI, the Copilot Studio PAYG credit meter, and Fabric capacity |
| `bronze_apim_gateway_requests` | 4,750 | APIM AI gateway → Log Analytics, one row per model request, **every value a string** |
| `bronze_apim_client_ownership` | 4 | Log Analytics `ApimClientOwnership_CL` client registry |
| `bronze_dataverse_msdyn_aievent` | 862 | Dataverse `msdyn_aievents` (OData), `msdyn_creditconsumed` already net of zero-rating |
| `bronze_m365_copilot_usage` | 480 | Graph `getMicrosoft365CopilotUsageUserDetail` — last-activity **dates**, not counts |
| `bronze_m365_copilot_seats` | 480 | Graph `subscribedSkus` + `assignedLicenses` |
| `bronze_m365_copilot_credits` | 140 | M365 Copilot Credits billing (Cowork/Autopilot) |
| `bronze_ghc_seats` | 480 | `GET /orgs/{org}/copilot/billing/seats` |
| `bronze_ghc_premium_usage` | 112 | GitHub enhanced billing usage API (`netAmount` = billed overage) |
| `bronze_ref_*` (5 tables) | 40 | Customer master data: identity map, app inventory, business hierarchy, agent inventory, rate card |

## SILVER — typed, conformed, identity-resolved, cost-allocated

**The provider's bill is the only source of dollars.** FOCUS has cost but no identity;
the gateway and Dataverse have identity but no cost. Silver splits the billed amount by
each identity's share of the billed unit, so the star ties to the invoice.

| Table | Rows | What it does |
|---|---|---|
| `silver_cost_charge` | 1,147 | Types FOCUS, decodes `Tags`/`x_SkuDetails`, maps service → platform, exposes billed vs list |
| `silver_gateway_request` | 4,750 | Casts LA strings (`""`/`"None"` → 0), resolves caller → identity and backend → application |
| `silver_studio_event` | 862 | Types credits, resolves `_msdyn_botid_value` → agent + owning BU |
| `silver_foundry_allocation` | 1,017 | Splits billed AOAI cost by token share **per direction** (input/output/cached price differently) |
| `silver_studio_allocation` | 84 | Splits the billed PAYG credit meter across agents by credit share |
| `silver_usage_conformed` | 2,843 | One daily grain across all platforms, with `cost_is_estimated` and `cost_method` |

## GOLD — the star the semantic model binds to

| Table | Rows |
|---|---|
| `fact_ai_usage` | 2,843 |
| `dim_identity` | 16 (people, service principals, agents, and the unattributed caller) |
| `dim_application` | 8 |
| `dim_date` | 60 |
| `dim_rate_card` | 11 |
| `dim_business_unit` / `dim_model` / `dim_platform` / `dim_cost_center` / `dim_environment` | 6 / 5 / 4 / 4 / 4 |
| `extractable_data_catalog` | 52 — every extractable field per product, named after the raw source field |

## Handoff to Fabric

The Bronze CSVs in `platform/fabric/bronze_out/` are what
[`load_bronze.py`](../fabric/load_bronze.py) pushes into a Lakehouse; the notebooks in
[`platform/medallion/`](../medallion/) then rebuild silver and gold there with the same
logic this file runs locally.
