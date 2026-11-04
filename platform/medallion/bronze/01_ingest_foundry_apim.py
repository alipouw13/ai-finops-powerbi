# Fabric notebook — BRONZE: FOCUS 1.2 Cost Export + APIM AI Gateway + Azure Monitor
# ---------------------------------------------------------------------------
# Architectural principle:
#   1. FOCUS 1.2-preview (from Microsoft Cost Management export / OneLake shortcut)
#      is the SOLE billing authority for all Azure AI, PTUs, Azure ML, Supporting
#      Infrastructure (AI Search, APIM, Cosmos, Redis, Fabric, Monitor), and
#      Copilot Studio / Cowork PAYG & P3 commitment plans.
#   2. APIM AI Gateway logs (Entra JWT claims -> DCR -> Log Analytics / Eventhouse)
#      are the per-request token & caller attribution driver (PromptTokens,
#      CompletionTokens, CachedPromptTokens, ReasoningTokens, Oid, ClientId, AgentId).
#   3. Azure Monitor metrics capture deployment-level PTU utilization % and 429 throttles.
#
# Bronze rule: land raw, append-only, source columns preserved, stamped with
# lineage metadata (_ingested_at, _source_system, _source_api, _watermark, _batch_id).
# ---------------------------------------------------------------------------
from pyspark.sql import functions as F

LAKE = "abfss://finops@onelake.dfs.fabric.microsoft.com/LH_tokenomics_bronze.Lakehouse/Files"
EXTRACT_ID = spark.conf.get("finops.extract_id", "manual")


def stamp_and_save(df, table_name: str, source_api: str, mode: str = "append"):
    out = (
        df.withColumn("_ingested_at", F.current_timestamp())
        .withColumn("_source_system", F.lit("azure_finops"))
        .withColumn("_source_api", F.lit(source_api))
        .withColumn("_extract_id", F.lit(EXTRACT_ID))
    )
    (
        out.write.format("delta")
        .mode(mode)
        .option("mergeSchema", "true")
        .saveAsTable(f"finops_bronze.{table_name}")
    )
    print(f"bronze {mode}: finops_bronze.{table_name} ({out.count()} rows)")


# 1. FOCUS 1.2-preview Cost & Usage Details (Parquet/CSV via OneLake shortcut or export)
focus_raw = (
    spark.read.option("header", "true")
    .option("multiLine", "true")
    .option("escape", '"')
    .csv(f"{LAKE}/bronze/bronze_focus_cost.csv")
)
stamp_and_save(focus_raw, "bronze_focus_cost", "costmanagement.exports.focus_1.2_preview", mode="append")

# 2. APIM AI Gateway Per-Request Token & Identity Logs
apim_raw = (
    spark.read.option("header", "true")
    .option("multiLine", "true")
    .option("escape", '"')
    .csv(f"{LAKE}/bronze/bronze_apim_gateway_logs.csv")
)
stamp_and_save(apim_raw, "bronze_apim_gateway_logs", "apim.ai_gateway.log_analytics", mode="append")

# 3. Azure Monitor Metrics (Tokens, Latency, 429 Throttles, PTU Utilization %)
metrics_raw = (
    spark.read.option("header", "true")
    .csv(f"{LAKE}/bronze/bronze_azure_ai_metrics.csv")
)
stamp_and_save(metrics_raw, "bronze_azure_ai_metrics", "microsoft.insights.metrics", mode="append")

# 4. Reference / Master Data Tables (Identity Map, App Inventory, Business Hierarchy, Rate Card)
for ref_name in [
    "bronze_ref_identity_map",
    "bronze_ref_app_inventory",
    "bronze_ref_business_hierarchy",
    "bronze_ref_agent_inventory",
    "bronze_ref_rate_card",
]:
    ref_df = (
        spark.read.option("header", "true")
        .option("multiLine", "true")
        .option("escape", '"')
        .csv(f"{LAKE}/bronze/{ref_name}.csv")
    )
    stamp_and_save(ref_df, ref_name, f"reference.{ref_name}", mode="overwrite")

