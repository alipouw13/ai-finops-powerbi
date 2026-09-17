# Fabric notebook — BRONZE: Cost Management export (FOCUS 1.0r2)
# ---------------------------------------------------------------------------
# The authoritative dollars for every Azure-billed AI service: Azure OpenAI /
# Foundry, the Copilot Studio pay-as-you-go credit meter, and Fabric capacity.
#
# Contract: FOCUS 1.0r2 cost and usage details, 96 provider columns.
#   https://learn.microsoft.com/azure/cost-management-billing/dataset-schema/cost-usage-details-focus
# This is deliberately NOT the legacy EA UsageDetails shape (Date / Quantity /
# CostInBillingCurrency / MeterId). Do not mix the two contracts.
#
# Bronze rule: land the file as it arrives. Every column is preserved with the
# provider's own name and casing; the only additions are lineage columns.
#
# WHY APPEND-ONLY MATTERS HERE: a Cost Management export REPLACES the
# month-to-date file on every run. Overwriting Bronze would destroy the prior
# days, so each extract is appended and stamped, and Silver reads the latest
# extract per charge period.
# ---------------------------------------------------------------------------
from pyspark.sql import functions as F

LAKE = "abfss://finops@<lake>.dfs.core.windows.net"
RAW_PATH = f"{LAKE}/landing/costmanagement/focus/**/*.parquet"
BRONZE_TBL = "finops_bronze.focus_cost"
EXTRACT_ID = spark.conf.get("finops.extract_id", "manual")

# The 96 FOCUS source columns land untouched. Cost columns arrive as decimals and
# date columns as ISO-8601 strings with seconds (the 1.0r2 change), so no cast
# belongs here — Silver owns typing.
raw = (
    spark.read.parquet(RAW_PATH)
    .withColumn("_ingested_at", F.current_timestamp())
    .withColumn("_source_system", F.lit("costmanagement"))
    .withColumn("_source_api", F.lit("costmanagement.exports.focus-1.0r2"))
    .withColumn("_watermark", F.col("ChargePeriodStart").substr(1, 10))
    .withColumn("_batch_id", F.lit(EXTRACT_ID))
    .withColumn("_data_class", F.lit("REAL"))
)

expected = {"BilledCost", "EffectiveCost", "ConsumedQuantity", "PricingQuantity",
            "ChargePeriodStart", "ResourceId", "SubAccountId", "Tags",
            "x_SkuMeterName", "x_ResourceGroupName"}
missing = expected - set(raw.columns)
assert not missing, f"export is not FOCUS 1.0r2 — missing {sorted(missing)}"

(raw.write.format("delta").mode("append")
    .partitionBy("_watermark")
    .option("mergeSchema", "true")
    .saveAsTable(BRONZE_TBL))

print(f"bronze append: {raw.count()} charge lines -> {BRONZE_TBL}")
