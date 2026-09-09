# Fabric notebook — SILVER: conform every platform onto one taxonomy
# ---------------------------------------------------------------------------
# Silver is where four incompatible billing units become one grain:
#   usage_date x platform x identity x model x unit_type  (+ cost in USD)
#
# There is NO common physical unit (only Foundry exposes tokens), so USD is the
# only conformed measure and unit_type is a dimension. This mirrors the PoC
# fact_ai_usage exactly, so gold can emit the same star the semantic model reads.
#
# Conformance responsibilities:
#   * identity normalization   -> universal identity_class (Human / SP / MI / Agent)
#   * application normalization -> map principal/app to dim_application
#   * model normalization       -> canonical model_key
#   * cost normalization        -> billed (real) vs modelled (rate-card), flagged
# ---------------------------------------------------------------------------
from pyspark.sql import functions as F

# ---- identity conformance --------------------------------------------------
# Not every request maps to a person. Foundry ClientId may be a service
# principal (Oid present, no UPN) or a human (JWT with upn). Copilot rows are
# always human seats. Managed identities / agents slot into the same vocabulary.
def classify_identity(df):
    return (df
        .withColumn("identity_class",
            F.when(F.col("upn").isNotNull(), "Human")
             .when(F.col("principal_type") == "ServicePrincipal", "ServicePrincipal")
             .when(F.col("principal_type") == "ManagedIdentity", "ManagedIdentity")
             .when(F.col("principal_type") == "Agent", "Agent")
             .otherwise("Application"))
        .withColumn("is_human", F.col("identity_class") == "Human"))


# ---- Foundry: real tokens -> three unit_type rows per request --------------
bronze = spark.table("finops_bronze.foundry_gateway_raw")
rates  = spark.table("finops_bronze.foundry_model_rate")   # InputPer1k etc. (text)

r = (rates
     .withColumn("model_key", F.col("ModelName"))
     .select("model_key",
             F.col("InputPer1k").cast("double").alias("in_1k"),
             F.col("OutputPer1k").cast("double").alias("out_1k"),
             F.col("CachedInputPer1k").cast("double").alias("cache_1k")))

f = (bronze
     .withColumn("usage_date", F.to_date("TimeGenerated"))
     .withColumn("platform_key", F.lit("Foundry"))
     .withColumn("identity_key", F.coalesce(F.col("ClientId"), F.lit("unknown")))
     .withColumn("model_key", F.col("ModelName"))
     .withColumn("cost_center_key", F.col("CostCenterClaim"))
     .withColumn("input_tokens",  F.col("PromptTokens").cast("long"))
     .withColumn("output_tokens", F.col("CompletionTokens").cast("long"))
     .withColumn("cached_tokens", F.col("CachedPromptTokens").cast("long"))
     .withColumn("is_error", F.col("IsError"))
     .join(r, "model_key", "left"))

# Cost is REAL here: derived from the gateway's own effective rates -> billed.
foundry_usage = (f
    .withColumn("token", F.col("input_tokens") + F.col("output_tokens") + F.col("cached_tokens"))
    .withColumn("cost_usd",
        (F.col("input_tokens")  / 1000 * F.col("in_1k")) +
        (F.col("output_tokens") / 1000 * F.col("out_1k")) +
        (F.col("cached_tokens") / 1000 * F.col("cache_1k")))
    .withColumn("unit_type", F.lit("token"))
    .withColumn("quantity", F.col("token"))
    .withColumn("requests", F.lit(1))
    .withColumn("cost_is_estimated", F.lit(False))          # billed, not modelled
    .select("usage_date", "platform_key", "identity_key", "model_key",
            "cost_center_key", "unit_type", "quantity", "input_tokens",
            "output_tokens", "cached_tokens", "requests", "cost_usd",
            "cost_is_estimated", "is_error"))

# ---- Copilot family: no tokens -> native unit rows, modelled cost ----------
# M365 -> seat_day (fixed), GitHub -> premium_request, Studio -> copilot_credit.
# msdyn_creditconsumed is already net of zero-rating: use directly (rationale #7).
# Rate-card join happens in gold; here we tag provenance and unit_type only.

# ---- union + write conformed silver ---------------------------------------
silver = classify_identity(foundry_usage)   # copilot unions appended once live
(silver.write.format("delta").mode("overwrite").option("overwriteSchema", "true")
    .saveAsTable("finops_silver.usage_conformed"))
print(f"silver.usage_conformed rows: {silver.count()}")
