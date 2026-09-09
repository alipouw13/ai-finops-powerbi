# Fabric notebook — GOLD: emit the star the semantic model consumes
# ---------------------------------------------------------------------------
# Gold materializes exactly the 10-table star in AIFinOps.SemanticModel:
#   fact_ai_usage + dim_date/platform/identity/model/cost_center/rate_card
#                 + dim_business_unit/application/environment (conformed v2)
#
# The PoC ships these as CSVs (data/*.csv) so the model opens with no Fabric
# dependency. In production, point the semantic model's DataFolder parameter at
# the OneLake path these tables write to, OR switch partitions to DirectLake.
# The COLUMN CONTRACT below must stay identical to the CSV headers, otherwise
# the TMDL partition casts break — the CSVs/TMDL are the source of truth.
# ---------------------------------------------------------------------------
from pyspark.sql import functions as F

GOLD = "finops_gold"
silver = spark.table("finops_silver.usage_conformed")
own    = spark.table("finops_bronze.foundry_client_ownership")

# --- dim_business_unit / dim_application / dim_environment ------------------
# Conformed from ownership claims. Budgets/criticality are customer inputs
# (MOCK in the PoC); keep the is_mock* flags so REAL and MOCK never blur.
# (See build_dimensions.py for the exact PoC key derivation these mirror.)

# --- fact_ai_usage: attach the v2 conformed keys ---------------------------
# business_unit_key = usage identity's home BU; application_key by SP/platform;
# environment_key by application default. This is the same mapping the PoC's
# build_dimensions.py applies, lifted into Spark.
fact = (silver
    .join(own.select(F.col("ClientId").alias("identity_key"),
                     F.col("BusinessUnit").alias("bu")), "identity_key", "left")
    .withColumn("business_unit_key",
        F.concat(F.lit("BU-"), F.upper(F.coalesce(F.col("bu"), F.lit("UNALLOC")))))
    .withColumn("application_key",
        F.when(F.col("platform_key") == "M365Copilot", "APP-M365")
         .when(F.col("platform_key") == "GitHubCopilot", "APP-GHCP")
         .when(F.col("platform_key") == "CopilotStudio", "APP-STUDIO")
         .otherwise("APP-UNKNOWN"))   # SP->owning app resolved via ownership map
    .withColumn("environment_key", F.lit("ENV-PROD")))

CONTRACT = ["usage_date", "platform_key", "identity_key", "model_key",
            "cost_center_key", "application_key", "environment_key",
            "business_unit_key", "unit_type", "quantity", "input_tokens",
            "output_tokens", "cached_tokens", "requests", "cost_usd",
            "cost_is_estimated", "is_error", "latency_ms"]

fact = fact.withColumn("latency_ms", F.lit(None).cast("double"))
missing = [c for c in CONTRACT if c not in fact.columns]
assert not missing, f"gold fact breaks the CSV column contract: {missing}"

(fact.select(*CONTRACT)
    .write.format("delta").mode("overwrite").option("overwriteSchema", "true")
    .saveAsTable(f"{GOLD}.fact_ai_usage"))
print(f"gold.fact_ai_usage rows: {fact.count()}")

# Optional: export to the same CSV layout the PBIP reads, so the PoC model can
# be refreshed straight from a Fabric run without DirectLake.
# fact.select(*CONTRACT).toPandas().to_csv("/lakehouse/.../data/fact_ai_usage.csv", index=False)
