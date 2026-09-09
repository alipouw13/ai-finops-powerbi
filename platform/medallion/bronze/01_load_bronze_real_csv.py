# Fabric notebook — BRONZE (REAL): land Files/bronze_real/*.csv as Delta tables
# ---------------------------------------------------------------------------
# Twin of 00_load_bronze_csv.py, with one behavioural difference that matters:
# this loader APPENDS and DEDUPES instead of overwriting.
#
# The mock feed is regenerated wholesale every run, so overwrite is correct
# there. The real feed is a rolling window — Consumption usageDetails only
# serves ~90 days and Graph only ever returns "now" — so overwriting would
# silently truncate history the moment a row aged out of the API window.
# Appending and then de-duplicating on the business columns means:
#   * re-running the extractor on the same day is a no-op
#   * overlapping windows merge instead of duplicating
#   * a daily snapshot of a reference table accumulates as real history
#
# De-dup key = every column except the per-run lineage stamps. Two rows that
# describe the same observation collapse; a genuinely new observation (a new
# _watermark on a snapshot table) is kept. Gold takes the latest snapshot per
# key, so accumulating history here never breaks dimension uniqueness.
#
# Requires a default lakehouse (the *real* bronze one) attached by
# platform/deploy/fabric_deploy.py at import time.
# ---------------------------------------------------------------------------
from pyspark.sql import functions as F
from pyspark.sql.window import Window
from pyspark.sql.utils import AnalysisException

SRC = "Files/bronze_real"
SCHEMA = "dbo"

# Stamped fresh on every extract, so they must not take part in de-duplication
# or every re-run would look like new data.
RUN_STAMPS = ["_ingested_at", "_batch_id", "_loaded_at", "_source_file"]

# The measure columns per table. The de-dup key is every other business column,
# which is exactly the grain the extractor aggregates to — so the key is unique
# within any one extract by construction.
#
# De-duplicating on *every* column looks safer but is wrong for anything the
# source restates: Azure revises recent usage, so a restated row differs in
# cost_usd, fails to match the row it supersedes, and both survive — silently
# double-counting real money. Keying on the grain and keeping the newest extract
# makes a restatement replace rather than accumulate, while genuinely new days
# still append. Reference tables declare no measures, so their whole row
# (including _watermark) is the key and each daily snapshot is retained.
MEASURES = {
    "bronze_azure_cost": ["quantity", "cost_usd", "line_item_count"],
    "bronze_azure_ai_metrics": ["processed_prompt_tokens", "generated_tokens",
                                "total_tokens", "requests", "latency_ms",
                                "throttled_count"],
    "bronze_m365_license_inventory": ["prepaid_enabled", "prepaid_warning",
                                      "prepaid_suspended", "consumed_units"],
}

try:
    files = [f for f in mssparkutils.fs.ls(SRC) if f.name.endswith(".csv")]
except Exception as e:                                            # noqa: BLE001
    print(f"no {SRC} folder ({str(e)[:100]}) — nothing to load")
    files = []

print(f"found {len(files)} REAL CSV(s) under {SRC}")

ok, failed = 0, 0
problems = []
for f in sorted(files, key=lambda x: x.name):
    table = f.name[:-4]
    target = f"{SCHEMA}.{table}"
    try:
        incoming = (spark.read
                    .option("header", "true")
                    .option("inferSchema", "true")
                    .option("multiLine", "true")
                    .option("escape", '"')
                    .csv(f"{SRC}/{f.name}")
                    .withColumn("_loaded_at", F.current_timestamp())
                    .withColumn("_source_file", F.lit(f.name)))

        if "_data_class" not in incoming.columns:
            raise ValueError(
                "every real bronze row must carry _data_class — provenance is "
                "the one column this accelerator is not allowed to lose")

        try:
            existing = spark.table(target)
            has_existing = True
        except AnalysisException:
            has_existing = False

        if has_existing:
            # allowMissingColumns keeps the merge total when the extractor gains
            # a column (e.g. cost_center) between runs.
            merged = existing.unionByName(incoming, allowMissingColumns=True)
            before = existing.count()
        else:
            merged = incoming
            before = 0

        business = [c for c in merged.columns if c not in RUN_STAMPS]
        key = [c for c in business if c not in MEASURES.get(table, [])]
        # Newest extract wins for a given grain, so a restated row replaces the
        # one it supersedes instead of sitting beside it.
        merged = (merged
                  .withColumn("_rn", F.row_number().over(
                      Window.partitionBy(*key)
                            .orderBy(F.col("_loaded_at").desc(),
                                     F.col("_ingested_at").desc())))
                  .filter(F.col("_rn") == 1).drop("_rn"))

        (merged.write.format("delta").mode("overwrite")
               .option("overwriteSchema", "true").saveAsTable(target))

        n = spark.table(target).count()
        print(f"  + {target:40} {n:7,} rows (+{n - before:,} new)")
        ok += 1
    except Exception as e:                                        # noqa: BLE001
        print(f"  ! {table:40} FAILED: {str(e)[:200]}")
        problems.append(f"{table}: {type(e).__name__}: {str(e)[:400]}")
        failed += 1

print(f"\n{ok} REAL table(s) written, {failed} failure(s).")
if failed:
    # RuntimeError, not SystemExit: the deploy wrapper catches Exception and
    # persists the traceback to Files/_errors. SystemExit is a BaseException,
    # slips past it, and leaves nothing but "System cancelled the Spark session".
    raise RuntimeError("real bronze load failed:\n  " + "\n  ".join(problems))
