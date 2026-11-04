# Fabric notebook — BRONZE: Azure Cost Management (FOCUS 1.0) via OneLake shortcut
# ---------------------------------------------------------------------------
# The first REAL cost feed in the accelerator. Azure Cost Management writes a
# FOCUS 1.0 parquet export into an ADLS Gen2 account in the CUSTOMER's own
# subscription; a OneLake shortcut surfaces it inside this lakehouse with zero
# copy. Nothing is duplicated into OneLake and the customer keeps retention and
# control of the raw billing extract.
#
# Provenance: every row is BILLED cost off the Microsoft invoice surface, so
# downstream `cost_is_estimated` is FALSE. It is never rate-card modelled.
#
# Why materialise a table at all, when the shortcut already exposes the files?
#   A Files shortcut is not queryable as a table: the SQL endpoint, Direct Lake
#   and downstream notebooks all need Delta. The shortcut stays the append-only
#   system of record; this table is the conformed, de-duplicated current view.
#
# Shortcut layout (created by platform/deploy/create_onelake_shortcut.py):
#   Files/azure_costmgmt_focus/
#     focus/{exportName}/{yyyyMMdd-yyyyMMdd}/{runId}/part_0_0001.parquet
#     focus/{exportName}/{yyyyMMdd-yyyyMMdd}/{runId}/manifest.json
# ---------------------------------------------------------------------------
from pyspark.sql import functions as F

SHORTCUT   = spark.conf.get("finops.focus_path", "Files/azure_costmgmt_focus/focus")
BRONZE_TBL = "finops_bronze.bronze_azure_cost_focus"
EXTRACT_ID = spark.conf.get("finops.extract_id", "manual")

# The glob mirrors the export folder contract exactly; if Cost Management ever
# changes the layout this fails loudly rather than silently reading nothing.
raw = spark.read.parquet(f"{SHORTCUT}/*/*/*/part_*.parquet")

# input_file_name() is the only way to recover which export/run a row came from,
# because those coordinates live in the PATH, not in the parquet payload.
raw = (raw
    .withColumn("_file", F.input_file_name())
    .withColumn("_export_name", F.regexp_extract("_file", r"/focus/([^/]+)/", 1))
    .withColumn("_date_range",  F.regexp_extract("_file", r"/focus/[^/]+/([^/]+)/", 1))
    .withColumn("_run_id",      F.regexp_extract("_file", r"/focus/[^/]+/[^/]+/([^/]+)/", 1)))

if raw.rdd.isEmpty():
    raise ValueError(
        f"No FOCUS parquet under '{SHORTCUT}'. Check the OneLake shortcut exists "
        "and that an export run reached status DataReady.")

# ---- de-duplication --------------------------------------------------------
# 1. newest run wins per (export, charge period)
newest = (raw.groupBy("_export_name", "_date_range")
             .agg(F.max("_run_id").alias("_run_id")))
latest = raw.join(newest, ["_export_name", "_date_range", "_run_id"], "inner")

# 2. Overlap BETWEEN exports is resolved at PERIOD level, never row level.
#    A monthly backfill and the recurring MonthToDate export can both cover the
#    same month; when that happens one must win wholesale.
#    Row-level keying was tried first and is WRONG: FOCUS legitimately emits many
#    rows sharing resource + meter + charge period (different pricing tiers, tags
#    and sku details), so keying on them silently deleted ~75% of rows and 30% of
#    the cost. Never de-duplicate FOCUS on a synthetic row key.
#    The Custom backfill is a complete closed-month snapshot, so it beats the
#    rolling MonthToDate feed for any month both cover.
latest = latest.withColumn("_charge_month", F.date_format("ChargePeriodStart", "yyyyMM"))
winner = (latest
    .withColumn("_prio", F.when(F.col("_export_name").contains("-bf-"), F.lit(1))
                          .otherwise(F.lit(2)))
    .groupBy("_charge_month")
    .agg(F.min(F.struct("_prio", "_export_name")).alias("_win"))
    .select("_charge_month", F.col("_win._export_name").alias("_export_name")))
deduped = latest.join(winner, ["_charge_month", "_export_name"], "inner")

out = (deduped
    .withColumn("_ingested_at",   F.current_timestamp())
    .withColumn("_source_system", F.lit("AzureCostManagement/FOCUS"))
    .withColumn("_source_api",    F.lit("Microsoft.CostManagement/exports"))
    .withColumn("_watermark",     F.col("ChargePeriodStart"))
    .withColumn("_batch_id",      F.col("_run_id"))
    .withColumn("_extract_id",    F.lit(EXTRACT_ID))
    .withColumn("_data_class",    F.lit("REAL"))
    .withColumn("_loaded_at",     F.current_timestamp())
    .withColumn("_source_file",   F.col("_file"))
    .drop("_file"))

(out.write.format("delta").mode("overwrite")
    .option("overwriteSchema", "true").saveAsTable(BRONZE_TBL))

print(f"bronze: {out.count():,} rows -> {BRONZE_TBL}")
(out.groupBy("_export_name", "_date_range")
    .agg(F.count("*").alias("rows"),
         F.round(F.sum(F.col("BilledCost").cast("double")), 2).alias("billed_usd"))
    .orderBy("_export_name").show(50, truncate=False))
