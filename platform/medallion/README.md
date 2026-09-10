# Medallion pipeline — bronze → silver → gold

Fabric notebook scripts (PySpark) that land the telemetry in a Lakehouse and
materialise the exact star the semantic model reads. The CSVs + TMDL remain the
source of truth — gold is written to *match* them, never to replace them.

## Runnable vs design

| File | Status |
|---|---|
| `bronze/00_load_bronze_csv.py` | **RUNNABLE** — `Files/bronze/*.csv` → `dbo.bronze_*` Delta |
| `bronze/01_load_bronze_real_csv.py` | **RUNNABLE** — REAL extracts → `dbo.bronze_*` (append + dedupe) |
| `silver/10_conform_usage.py` | **RUNNABLE** — bronze → the 14 curated `dbo.silver_*` entities |
| `gold/20_build_star.py` | **RUNNABLE** — silver → the 11-table star |
| `bronze/01_ingest_foundry_apim.py` | design scaffold — placeholder paths |
| `bronze/02_ingest_copilot_platforms.py` | design scaffold — placeholder paths |
| `*.design.py` | the original scaffolds, kept for reference |

The scaffolds contain literals like `LAKE = "abfss://finops@<lake>..."` and read
from landing paths that do not exist; they fail immediately if executed.
`fabric_deploy.py` runs only the runnable set unless you pass `--only`.

## One lakehouse per layer

Each notebook writes to **its own** lakehouse and reads upstream ones by abfss
path, so the layers stay separable and independently securable:

```bash
python platform/deploy/fabric_deploy.py \
  --steps preflight workspace lakehouse upload notebooks run \
  --workspace       <workspace-guid> \
  --lakehouse       <bronze-guid> \
  --lakehouse-silver <silver-guid> \
  --lakehouse-gold   <gold-guid>
```

`fabric_deploy.py` injects `WS_ID` / `BRONZE_ID` / `SILVER_ID` / `GOLD_ID` as a
parameters cell at import time, so no GUID is ever hard-coded in the repo.

```
Files/bronze/*.csv ──► BRONZE lakehouse          SILVER lakehouse            GOLD lakehouse
                       dbo.bronze_m365_*   ──┐
                       dbo.bronze_ghc_*      ├─► dbo.usage_conformed ──► dbo.fact_ai_usage
                       dbo.bronze_studio_*   │   (date×platform×identity     + dbo.dim_* (10)
                       dbo.bronze_ref_*    ──┘    ×model×unit_type, USD)
```

| Layer | Rule | Why |
|---|---|---|
| **Bronze** | Raw, source columns preserved, ingest lineage added | Historical system of record; Cost-Management exports *replace* MTD, so keeping bronze protects prior days. Azure Monitor metrics retention is 93 days — bronze is the durable copy. |
| **Silver** | Curated **entities**, not one conformed fact: normalize, de-duplicate, grain-guard, then join to the BU/application map | Silver is where cross-source correctness is established once. A single fact-shaped output leaves nowhere to resolve identity, hold a de-duplicated reference entity, or run a quality gate — and forces Gold to reach back into Bronze. |
| **Gold** | Emit the star under a fixed **column contract** identical to the CSV headers, reading **only** Silver | The TMDL partition casts are keyed to those exact columns; gold asserts the contract and fails loudly on drift. It also fails on orphaned foreign keys and on duplicate dimension keys. |

### Silver: the four responsibilities

| # | Responsibility | Implementation |
|---|---|---|
| 1 | Normalize attributes across sources | every `silver_usage_*` is forced onto `USAGE_CONTRACT`; `silver_model_map` canonicalises model names |
| 2 | De-duplicate | `dedupe()` for usage feeds, `dedupe_key()` for reference entities, `dedupe_month()` for monthly snapshots |
| 3 | Grain guard: cumulative vs delta | `ACCUMULATION` declares each feed; `grain_guard()` differences cumulative series and *challenges* anything declared delta |
| 4 | Join telemetry to BU / application | resolved once into `silver_usage_unified`, so Gold consumes business keys rather than re-deriving them |

**The grain guard is the one that saves real money.** A month-to-date cumulative
series summed across 30 days overstates spend by roughly an order of magnitude,
and nothing errors — the number is simply wrong. `silver_grain_audit` records the
verdict per feed, and a feed declared `delta` whose values are non-decreasing
across ≥95% of consecutive periods is flagged as probably cumulative.

**De-duplicate on the whole natural key, never a hand-picked subset.** Keying
Azure cost on `(date, resource, meter, price)` looked reasonable and silently
merged two genuinely different charges that differed only by resource tag —
dropping 824 rows and $166.99 of real spend. `dedupe()` therefore keys on every
descriptive column and treats only the declared measures as non-key.

### Two gold assertions worth knowing

`write_gold()` refuses to publish a table that would break the semantic model,
because both failures otherwise surface late and cryptically:

1. **Column contract** — a missing column makes Direct Lake refuse to frame with
   `Delta protocol violation: the column 'sla_tier' is not found in delta table
   'dim_environment'`.
2. **Dimension key uniqueness** — a duplicate key makes Power BI reject the
   whole relationship, and it only shows up when a visual runs:
   `Column 'application_key' in Table 'dim_application' contains a duplicate
   value 'APP-UNKNOWN' and this is not allowed for columns on the one side of a
   relationship`. Every visual touching that dimension fails at once.

## Two Fabric constraints worth knowing

1. **Schema-enabled lakehouses cannot use the Load Table REST API.** A lakehouse
   with `properties.defaultSchema` (`"dbo"` by default on new lakehouses) rejects
   `/tables/{name}/load` with `UnsupportedOperationForSchemasEnabledLakehouse`.
   That is why the CSV→Delta step is a Spark notebook rather than a REST call.
2. **A notebook needs an attached lakehouse to write tables.** The import sets
   `metadata.dependencies.lakehouse.default_lakehouse`; without it Spark has no
   default catalog and every `saveAsTable()` fails at runtime.

## Debugging a failed run

Fabric only reports *"System cancelled the Spark session due to statement
execution failures"* — the traceback is not in the job API. The notebook wrapper
therefore writes it to `Files/_errors/<script>.log` in that layer's lakehouse,
and `fabric_deploy.py` prints it automatically when a run fails.

Before deploying, catch the common PySpark trap locally:

```bash
python platform/validate/check_notebooks.py
```

It flags attribute-style column access that collides with a DataFrame member —
`resolve.alias` returns the bound *method*, and the resulting join dies with
`'function' object has no attribute '_get_object_id'` 40 seconds into a remote run.

## Provenance never blurs
Real vs mock is `dim_platform.data_source` + `fact.cost_is_estimated`, a
**column**, not a code branch. Silver preserves per-row provenance: GitHub
`net_amount` and the Studio/M365 credit meters are billed
(`cost_is_estimated=False`); seat costs are modelled from the rate card
(`True`). Nothing ever relabels modelled dollars as billed.

## Direct Lake

The PoC reads CSVs via the `DataFolder` parameter so it opens with zero Fabric
dependency. That same import model cannot refresh in the service without a
gateway, so Direct Lake is the production path — and it is implemented and
verified end to end.

`platform/validate/build_directlake.py` generates a **separate**
`AIFinOps.DirectLake.SemanticModel` from the committed import model: same
tables, columns, relationships and all 42 measures, with every partition swapped
from `m`/CSV to `entity`/`directLake`. `AIFinOps.pbip` is never modified, so the
offline demo keeps working.

```bash
python platform/validate/build_directlake.py \
    --workspace <workspace-guid> --lakehouse <gold-lakehouse-guid>
python platform/deploy/deploy_semantic_model.py \
    --workspace <workspace-guid> --model-dir AIFinOps.DirectLake.SemanticModel
```

Then trigger one refresh to **frame** the model. Framing binds it to the Delta
files; until it succeeds the tables are not queryable. A framing failure names
the offending table and column, e.g.
`Delta protocol violation: the column 'sla_tier' is not found in delta table 'dim_environment'`.
`GOLD_CONTRACT` in the gold notebook asserts every table's columns up front so
that mismatch fails in Spark, where the message is clearer.

| Option | Effort | Tradeoff |
|---|---|---|
| CSV export from gold | S | Zero model change; still an import refresh |
| Direct Lake on gold | M | Live data, no refresh, no gateway; needs Fabric capacity |

### Three TMDL traps this surfaced

All three are accepted by Power BI Desktop and rejected — or silently broken — by
the Fabric TMDL parser. `platform/validate/validate_pbip.py` now checks all
three; `platform/validate/fix_tmdl_measures.py` repairs the third.

1. **`///` description followed by a blank line** -> `Unexpected line type: Empty!`
2. **`//` comment directly above a `///` description** -> `Invalid indentation was detected`
3. **Multi-line DAX not wrapped in triple backticks** -> the following
   `formatString:` line is folded *into* the expression. The measure still
   deploys, then fails at query time with `Failed to resolve name 'SYNTAXERROR'`.
   **21 of this repo's 42 measures** were affected; every visual bound to them
   would have rendered blank with no error surfaced anywhere.

### Known gap: `[M365 Prompts]` and `[Error Rate]` are blank

Microsoft Graph returns per-app **last-activity dates**, not prompt counts, so
silver emits `unit_type = "active_day"` (one row per user per day they were
active) rather than inventing a prompt count. `[M365 Prompts]` therefore returns
blank against the medallion data. Likewise Azure Monitor metrics carry no
per-request error flag, so `[Error Rate]` is blank — `throttled_count` is a
capacity signal, not a failure count, and mapping it would report a false ~90%
error rate.

Both are the honest result; the committed CSV demo fabricates these numbers, the
pipeline does not. `check_report.py --blank` keeps them off the report.

### Shelfware is modelled deliberately

`gen_bronze_data.py` marks two users as holding paid seats with **zero activity
on every platform**. Without them no user is ever idle for a full 28 days,
`[Idle Licensed Users]` is always blank, and the Waste & Utilisation and License
Optimization pages have nothing to show. `build_data.py` models the same two
users for the CSV demo, so both paths tell the same story.