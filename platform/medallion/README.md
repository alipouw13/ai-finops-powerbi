# Medallion pipeline - bronze → silver → gold

Reference implementation for landing the PoC's mock/real telemetry in Microsoft
Fabric and materializing the exact 10-table star the semantic model reads. These
are Fabric notebook scripts (PySpark); they are **not** wired into the PBIP, so
they cannot affect whether `AIFinOps.pbip` opens. The CSVs + TMDL remain the
source of truth - gold is written to *match* them, never to replace them.

```
landing (ADLS/OneLake)          BRONZE (Delta, append-only)        SILVER (conformed)          GOLD (star)
──────────────────────          ───────────────────────────        ──────────────────          ───────────
APIM AI Gateway  ──────────────► finops_bronze.foundry_gateway_raw ─┐
  (Entra JWT claims → DCR → LA)  finops_bronze.foundry_model_rate   │
                                 finops_bronze.foundry_*_ownership  ├─► finops_silver.usage_conformed ─► finops_gold.fact_ai_usage
M365 / GitHub / Studio APIs ───► finops_bronze.{platform}_raw ──────┘   (date×platform×identity×          + finops_gold.dim_*
                                                                          model×unit_type, USD)
```

| Layer | Rule | Why |
|---|---|---|
| **Bronze** | Raw, append-only, source columns preserved, partitioned by source/ingest day | Historical system of record; Cost-Management exports *replace* MTD, so append protects prior days. Azure Monitor metrics retention is 93 days - bronze is the durable copy. |
| **Silver** | Conform to one grain; USD is the only common measure; `unit_type` is a dimension | Four platforms, four incompatible billing units, only Foundry exposes tokens. USD is the sole thing that reconciles. Identity/app/model normalization lives here. |
| **Gold** | Emit the star under a fixed **column contract** identical to the CSV headers | The semantic model's TMDL partition casts are keyed to those exact columns; gold asserts the contract so a schema drift fails loudly, not silently. |

## Provenance never blurs
Real vs mock is `dim_platform.data_source` + `fact.cost_is_estimated`, a **column**,
not a code branch. To take a mock platform live you swap one bronze reader
(`02_ingest_copilot_platforms.py`); silver/gold/model are unchanged and the
dashboards immediately show REAL. Nothing ever relabels modelled dollars as billed.

## Going to DirectLake
The PoC reads CSVs via the `DataFolder` parameter so it opens with zero Fabric
dependency. In production, either (a) export gold to the same CSV layout, or
(b) repoint the semantic model to the gold Lakehouse and convert import
partitions to **DirectLake** for no-refresh, near-real-time cost.

| Option | Effort | Tradeoff |
|---|---|---|
| CSV export from gold | S | Zero model change; still an import refresh |
| DirectLake on gold | M | Live data, no refresh; requires Fabric capacity + partition rewrite |
