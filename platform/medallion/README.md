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

## Azure billed cost: FOCUS export via OneLake shortcut

Azure Cost Management writes a **FOCUS** Parquet export into an ADLS Gen2
account in the customer's own subscription. Fabric reads it through a OneLake
shortcut — no copy — and `bronze/03_ingest_azure_costmgmt_focus.py` materialises
it as the Delta table `bronze_azure_cost_focus`:

```
Files/azure_costmgmt_focus/focus/{exportName}/{dateRange}/{runId}/part_0_0001.parquet
                                                                  + manifest.json
        │  (shortcut: zero copy, storage stays in the customer subscription)
        ▼
bronze_real.dbo.bronze_azure_cost_focus        (Delta, de-duplicated, 100 FOCUS cols + lineage)
        │  (OneLake table shortcut into the silver lakehouse)
        ▼
silver.dbo.silver_usage_azure                  (conformed, unit_type=azure_meter)
silver.dbo.silver_azure_cost_detail            (billed / effective / list / contracted)
```

A **Files** shortcut is not queryable as a table — the SQL endpoint, Direct Lake
and every downstream notebook need Delta — so the shortcut stays the append-only
system of record and the table is the conformed current view. The table is then
re-exposed to the silver lakehouse as a **OneLake table shortcut** at
`Tables/dbo/bronze_azure_cost_focus`, so silver reads bronze without copying.

`manifest.json` is the validation record: `dataRowCount`, `runInfo.runId` and
`exportConfig` confirm the run you ingested is the run Cost Management produced.

### De-duplicate at period level, never row level

This is the third member of the silent-failure family documented in the root
README, and the most expensive one.

Overlap is real: each run lands in a **new** `runId` folder, and the recurring
MonthToDate export re-emits the whole current month every day. The obvious fix —
key a dedupe on resource + meter + charge period — is **wrong**. FOCUS legitimately
emits many rows sharing those values (different pricing tiers, tags, SKU details),
so the key collapses genuine charges. Measured on this dataset it deleted **~75% of
rows and 30% of the cost** (\$7,048.73 → \$4,919.71) and returned a plausible total
with no error.

Bronze resolves it structurally instead, before silver sees a row:

1. newest `runId` wins per `(exportName, dateRange)`;
2. where a monthly backfill and the rolling MonthToDate export both cover a month,
   one export wins **wholesale** — the closed-month backfill is the complete
   snapshot.

Because bronze hands silver clean rows, the generic `dedupe()` / `grain_guard()`
pair is deliberately **not** applied to this feed; their natural key would
re-introduce exactly the collapse described above.

### AI vs infrastructure comes from the spec

`ServiceCategory` drives the split, replacing string-matching on ARM resource ids:
`AzureAI` for Cognitive Services / AI Foundry / ML, `AzureInfra` for the storage,
search, database and network tier those workloads run on.

`bronze_azure_cost_focus` is preferred over the legacy `bronze_azure_cost`
extract whenever it exists, with the old path kept as a fallback. Only one is ever
read, so Azure cost cannot double-count across the two.

Verified live: 79,426 rows / \$7,048.73 → `fact_ai_usage` 81,172 rows /
\$12,818.69, reconciling exactly (`AzureInfra` \$6,342.24 + `AzureAI` \$706.49).

### FOCUS version: 1.2-preview, tolerant of 1.0

Microsoft's current FOCUS dataset is **`1.2-preview`**, not the `1.0` this fork was
first built against. 1.2-preview promotes several vendor-prefixed 1.0 columns to
standard names and drops the old spelling — `x_InvoiceId` → `InvoiceId`,
`x_PricingCurrency` → `PricingCurrency`, `x_SkuMeterName` → `SkuMeter`. A plain
`F.coalesce(F.col("x_SkuMeterName"), F.col("SkuMeter"))` throws `AnalysisException`
on whichever version is missing one of the two columns, so `10_conform_usage.py`
uses a `focus_col` helper that picks each column by **presence first** (tolerating
either schema) and then coalesces the survivors for per-row nulls. The 1.2 `ListCost`
column feeds the new `fact_ai_usage[list_cost_usd]` / `[has_rate_card]` columns and
the `Rate Card Cost` measure; Azure rows therefore carry a genuine list price without
any modelled rate-card row of their own.

### Foundry token telemetry: prefer the gateway, fall back to metrics

Foundry has two possible token sources, and silver reads **exactly one** — the same
prefer-and-fall-back rule as FOCUS-over-`usageDetails`, so the two can never
double-count:

* `bronze_foundry_gateway` (**new**, APIM AI Gateway → Log Analytics) is per-request,
  **per identity** (Entra `oid` / app client id) with the `cc:`/`bu:` claims the
  caller forwards. It is the **only** per-user Foundry token attribution path, so it
  wins when present. Its client-id → application ownership map lets a row resolve a
  real `application_key` instead of Foundry's default `APP-UNKNOWN`.
* `bronze_azure_ai_metrics` (Azure Monitor metrics) is **resource-grain and carries
  no principal**, so every row lands on `identity_key = "unknown"`. It is the fallback
  when the gateway feed is absent.

This is what shrinks the "Unattributed Identity" bar: the richer source *genuinely
knows* the identity, rather than the numbers being spread around. The residual
`Unknown` volume is surfaced honestly by the `Unattributed Requests` /
`Unattributed Request %` measures and can never reach zero while invoice-grain rows
are in scope. Gold's `_foundry_feed()` names whichever source actually produced the
numbers so the Governance page never cites a feed it did not read.

### M365 Copilot Cowork add-on, and the Copilot Credits double-count guard

`bronze_m365_cowork_usage` (**new**) is the Microsoft 365 Copilot **Cowork** add-on —
a *consumptive* add-on on top of the seat, conformed by the **new** silver entity
`silver_usage_m365_cowork` as `unit_type = "copilot_credit"` on
`platform_key = "M365Copilot"`. There is no "Cowork unit"; the billing unit is the
**Copilot Credit at \$0.01**. Because it is consumptive it sits inside `Variable Cost`,
never `Fixed Cost`.

The trap: on the Azure bill, Cowork, Copilot Studio **and** Work IQ credits are all
billed through **one Azure service labelled `Microsoft Copilot Studio`** — there is no
separate Cowork line item. Those credits are already in the model via the M365 and
Studio credit feeds, so the FOCUS branch of `10_conform_usage.py` **excludes** that
service from `silver_usage_azure` (printing the dollar amount it drops). Prepaid
capacity-pack draw-down is additionally invisible in Azure Cost Management, so the
credit feed — not the Azure line — is authoritative for this consumption.

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
tables, columns, relationships and all 57 measures, with every partition swapped
from `m`/CSV to `entity`/`directLake`. `AIFinOps.pbip` is never modified, so the
offline demo keeps working.

```bash
python platform/validate/build_directlake.py \
    --workspace <workspace-guid> --lakehouse <gold-lakehouse-guid>
python platform/deploy/deploy_semantic_model.py \
    --workspace <workspace-guid> --model-dir AIFinOps.DirectLake.SemanticModel
```

Then the model must **frame** — re-read the Delta schema — before its tables are
queryable. `deploy_semantic_model.py` now does this for you: it calls the dataset
refresh after `updateDefinition` and polls it to completion, unconditionally.
That is not a convenience, it is a correctness fix (finding 4 in the root README):
Direct Lake maps *rows* with no refresh, but a **newly declared column stays
unmapped until the model reframes**. After `list_cost_usd` / `has_rate_card` were
added to the gold fact, the deploy returned 200 and the table bound, yet
`Rate Card Cost` / `Rate Card Coverage %` failed at query time with
`The value for 'list_cost_usd' cannot be determined` and rendered as blank cards —
no error at deploy time. The reframe is issued on the **Power BI REST surface**
(`api.powerbi.com/.../datasets/{id}/refreshes`), not the Fabric one, so the script
acquires a second token audience (`https://analysis.windows.net/powerbi/api`).
A framing failure names the offending table and column, e.g.
`Delta protocol violation: the column 'sla_tier' is not found in delta table 'dim_environment'`.
`GOLD_CONTRACT` in the gold notebook asserts every table's columns up front so
that mismatch fails in Spark, where the message is clearer.

| Option | Effort | Tradeoff |
|---|---|---|
| CSV export from gold | S | Zero model change; still an import refresh |
| Direct Lake on gold | M | Live data, no gateway; no refresh for new *rows*, but a **schema** change reframes; needs Fabric capacity |

### Three TMDL traps this surfaced

All three are accepted by Power BI Desktop and rejected — or silently broken — by
the Fabric TMDL parser. `platform/validate/validate_pbip.py` now checks all
three; `platform/validate/fix_tmdl_measures.py` repairs the third.

1. **`///` description followed by a blank line** -> `Unexpected line type: Empty!`
2. **`//` comment directly above a `///` description** -> `Invalid indentation was detected`
3. **Multi-line DAX not wrapped in triple backticks** -> the following
   `formatString:` line is folded *into* the expression. The measure still
   deploys, then fails at query time with `Failed to resolve name 'SYNTAXERROR'`.
   **21 of the model's measures** were affected; every visual bound to them
   would have rendered blank with no error surfaced anywhere.

### Known gap: `[M365 Prompts]` and `[Error Rate]` are blank

Microsoft Graph returns per-app **last-activity dates**, not prompt counts, so
silver emits `unit_type = "active_day"` (one row per user per day they were
active) rather than inventing a prompt count. `[M365 Prompts]` therefore returns
blank against the medallion data.

`[Error Rate]` is subtler after the gateway feed was added. On the **Azure Monitor
metrics fallback** path the reason is unchanged: metrics carry no per-request error
flag, `throttled_count` is a capacity signal not a failure count, and mapping it
would report a false ~90% error rate. But the **`bronze_foundry_gateway`** feed
*does* carry a genuine per-request `status_code` / `is_error`, so on gateway-fronted
Foundry traffic `[Error Rate]` is meaningful. It is still not bound to any visual
and is passed to `check_report.py --blank` **by choice**, not because the data is
always absent — so the docs no longer claim "no per-request error flag" as a blanket
reason.

The committed CSV demo fabricates these numbers, the pipeline does not.
`check_report.py --blank` keeps them off the report.

### The `inferSchema` type trap (finding 5): a clean cast that is wrong

The bronze loader runs `inferSchema`, so a column's Spark type depends on the data
in each CSV. `bronze_foundry_gateway` writes `is_error` as `True`/`False`, which
`inferSchema` reads as a **BOOLEAN**, while the silver `USAGE_CONTRACT` declares
`is_error` as a **STRING** (the other feeds write it as a string). Coalescing a
boolean against a string literal fails the whole conform loudly with
`DATATYPE_MISMATCH.DATA_DIFF_TYPES` — which is not the interesting part. The trap is
the obvious fix, `F.col("is_error").cast("string")`: it runs clean and is **wrong**,
because Spark renders a boolean as lowercase `'true'`/`'false'`, and `[Error Rate]`
filters on `is_error = "True"`. The measure would have read 0% forever, silently.
Silver therefore normalises explicitly —
`when(coalesce(is_error.cast("boolean"), false), "True").otherwise("False")` — so
both the boolean-typed gateway feed and the string-typed feeds land on the exact
`"True"`/`"False"` the measure expects, rather than a bare cast that only *looks*
right.

### Shelfware is modelled deliberately

`gen_bronze_data.py` marks two users as holding paid seats with **zero activity
on every platform**. Without them no user is ever idle for a full 28 days,
`[Idle Licensed Users]` is always blank, and the Waste & Utilisation and License
Optimization pages have nothing to show. `build_data.py` models the same two
users for the CSV demo, so both paths tell the same story.