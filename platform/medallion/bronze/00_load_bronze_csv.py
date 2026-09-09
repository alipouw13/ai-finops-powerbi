# Fabric notebook — BRONZE: land Files/bronze/*.csv as Delta tables
# ---------------------------------------------------------------------------
# Unlike the other scripts in platform/medallion/ (which are DESIGN scaffolds
# with placeholder paths), this one is RUNNABLE as-is. It is what
# fabric_deploy.py --steps notebooks run executes.
#
# Why it exists: the Fabric "Load Table" REST API refuses schema-enabled
# lakehouses (errorCode UnsupportedOperationForSchemasEnabledLakehouse), which
# is now the default for new lakehouses. Spark has no such restriction, so the
# CSV -> Delta step runs here instead.
#
# Requires the notebook to have a default lakehouse attached; fabric_deploy.py
# sets that in the notebook metadata at import time.
# ---------------------------------------------------------------------------
from pyspark.sql import functions as F

SRC = "Files/bronze"
SCHEMA = "dbo"  # schema-enabled lakehouses namespace tables; "dbo" is the default

files = [f for f in mssparkutils.fs.ls(SRC) if f.name.endswith(".csv")]
print(f"found {len(files)} CSV(s) under {SRC}")

ok, failed = 0, 0
for f in sorted(files, key=lambda x: x.name):
    table = f.name[:-4]  # strip .csv
    try:
        df = (spark.read
              .option("header", "true")
              .option("inferSchema", "true")
              .option("multiLine", "true")
              .option("escape", '"')
              .csv(f"{SRC}/{f.name}"))

        # Bronze contract: land raw, add ingest lineage, never transform.
        df = (df.withColumn("_loaded_at", F.current_timestamp())
                .withColumn("_source_file", F.lit(f.name)))

        target = f"{SCHEMA}.{table}"
        df.write.format("delta").mode("overwrite") \
          .option("overwriteSchema", "true").saveAsTable(target)

        n = spark.table(target).count()
        print(f"  + {target:44} {n:7,} rows")
        ok += 1
    except Exception as e:                                    # noqa: BLE001
        print(f"  ! {table:44} FAILED: {str(e)[:160]}")
        failed += 1

print(f"\n{ok} table(s) written, {failed} failure(s).")
if failed:
    raise SystemExit(f"{failed} table(s) failed to load")
