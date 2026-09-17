# Fabric notebook — BRONZE: APIM AI gateway via Log Analytics
# ---------------------------------------------------------------------------
# Source of fidelity: the APIM AI gateway is the ONLY per-identity attribution
# path for Foundry. Azure Monitor token metrics carry no identity dimension, so
# they are not collected at all — they would only duplicate token counts that
# this feed already provides, without the one column that matters.
#
# Flow: Entra JWT claims -> APIM policy (emit-token-metric + log-to-eventhub)
#       -> Data Collection Rule -> Log Analytics custom tables:
#          ApimAiGateway_CL        one row per model request
#          ApimClientOwnership_CL  the customer's client registry
#
# Log Analytics returns EVERY field as a string, including numbers and booleans,
# and unset values arrive as "" or the literal "None". Bronze keeps them exactly
# that way; Silver casts. A missing ClientId is a real condition (absent JWT
# claim), not dirty data: those requests burned billed tokens and must stay
# visible so the cost surfaces as unallocated rather than disappearing.
# ---------------------------------------------------------------------------
from pyspark.sql import functions as F

LAKE = "abfss://finops@<lake>.dfs.core.windows.net"
EXTRACT_ID = spark.conf.get("finops.extract_id", "manual")


def land(df, table, source_api, watermark_col, mode="append"):
    out = (df
           .withColumn("_ingested_at", F.current_timestamp())
           .withColumn("_source_system", F.lit("loganalytics"))
           .withColumn("_source_api", F.lit(source_api))
           .withColumn("_watermark", F.col(watermark_col).substr(1, 10))
           .withColumn("_batch_id", F.lit(EXTRACT_ID))
           .withColumn("_data_class", F.lit("REAL")))
    (out.write.format("delta").mode(mode)
        .option("mergeSchema", "true")
        .saveAsTable(f"finops_bronze.{table}"))
    print(f"bronze {mode}: finops_bronze.{table} ({out.count()} rows)")


# --- per-request telemetry: append-only, this is the historical record -------
# ApiName, Appid, BackendId, BusinessUnitClaim, CachedPromptTokens, ClientId,
# CompletionTokens, CostCenterClaim, DeploymentRegion, IsError, IsStreaming,
# ModelName, ModelVersion, Oid, OperationName, PromptTokens, RequestId,
# StatusCode, TimeGenerated, TotalLatencyMs, TotalTokens, UpnOrAppName
requests = spark.read.option("multiLine", True).json(f"{LAKE}/landing/apim/gateway/*.json")
land(requests, "apim_gateway_requests", "loganalytics.ApimAiGateway_CL", "TimeGenerated")

# --- client registry: small current-state snapshot, overwrite is correct -----
ownership = spark.read.option("multiLine", True).json(f"{LAKE}/landing/apim/ownership/*.json")
land(ownership, "apim_client_ownership", "loganalytics.ApimClientOwnership_CL",
     "TimeGenerated", mode="overwrite")
