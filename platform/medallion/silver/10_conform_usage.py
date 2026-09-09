# Fabric notebook — SILVER: conform every platform onto one taxonomy
# ---------------------------------------------------------------------------
# RUNNABLE. Reads Bronze Delta tables from the bronze lakehouse and writes one
# conformed table to the silver lakehouse.
#
# Silver is where four incompatible billing units become one grain:
#   usage_date x platform x identity x model x unit_type  (+ cost in USD)
#
# There is NO common physical unit (only Foundry/Azure AI exposes tokens), so USD
# is the only conformed measure and unit_type is a dimension.
#
# COST PROVENANCE is preserved per row, never averaged away:
#   cost_is_estimated = False -> billed by the platform (GitHub net_amount,
#                                Studio/M365 credit meters carry real cost_usd)
#   cost_is_estimated = True  -> modelled from bronze_ref_rate_card
#
# Parameters (WS_ID / BRONZE_ID / SILVER_ID / GOLD_ID) are injected as a
# preceding cell by platform/deploy/fabric_deploy.py at import time.
#
# The original design scaffold is kept as 10_conform_usage.design.py.
# ---------------------------------------------------------------------------
from pyspark.sql import functions as F
from pyspark.sql.utils import AnalysisException

ONELAKE = "abfss://{ws}@onelake.dfs.fabric.microsoft.com/{lh}/Tables/dbo/{tbl}"

# BRONZE_REAL_ID is injected only once a real-bronze lakehouse exists. Guarding
# on globals() keeps the notebook runnable against a mock-only deployment.
BRONZE_REAL = globals().get("BRONZE_REAL_ID") or ""


def _read(lh, tbl):
    return spark.read.format("delta").load(ONELAKE.format(ws=WS_ID, lh=lh, tbl=tbl))


def bronze(tbl):
    """Read a bronze table from the mock lakehouse and the real one, unioned.

    This is the single point where REAL and MOCK spend become one dataset.
    They are deliberately NOT reconciled or averaged: every row keeps its own
    _data_class, silver carries that column through to gold, and gold derives
    the platform provenance label from it. Blending without that column would
    be exactly the relabelling this accelerator exists to prevent.

    allowMissingColumns because the real extract legitimately carries columns
    the mock generator never invented (consumed_service, cost_center, ...).
    """
    try:
        mock = _read(BRONZE_ID, tbl)
    except (AnalysisException, Exception):                        # noqa: BLE001
        mock = None
    real = None
    if BRONZE_REAL:
        try:
            real = _read(BRONZE_REAL, tbl)
        except (AnalysisException, Exception):                    # noqa: BLE001
            real = None
    if mock is not None and real is not None:
        return mock.unionByName(real, allowMissingColumns=True)
    if mock is not None:
        return mock
    if real is not None:
        return real
    raise AnalysisException(f"bronze table {tbl} exists in neither lakehouse")


def has_real(tbl):
    if not BRONZE_REAL:
        return False
    try:
        _read(BRONZE_REAL, tbl)
        return True
    except Exception:                                             # noqa: BLE001
        return False


def write_silver(df, tbl):
    path = ONELAKE.format(ws=WS_ID, lh=SILVER_ID, tbl=tbl)
    (df.write.format("delta").mode("overwrite")
       .option("overwriteSchema", "true").save(path))
    print(f"  + silver.{tbl:24} {df.count():7,} rows")


# --------------------------------------------------------------- rate lookup
rate_map = {
    (r["platform"], r["unit_type"], r["model"] or ""): float(r["unit_price_usd"])
    for r in bronze("bronze_ref_rate_card").collect()
}


def rate(platform, unit_type, model=""):
    return rate_map.get((platform, unit_type, model),
                        rate_map.get((platform, unit_type, ""), 0.0))


# The conformed column contract. Every branch below is forced onto exactly this.
# _data_class rides all the way through so gold can label each platform REAL or
# MOCK from the data itself rather than from a hardcoded string.
# application_key lets a source that genuinely knows its workload (real Azure
# cost knows the ARM resource) attribute itself, instead of gold having to guess
# from platform alone.
CONTRACT = ["usage_date", "platform_key", "identity_key", "model_key",
            "cost_center_key", "unit_type", "quantity", "input_tokens",
            "output_tokens", "cached_tokens", "requests", "cost_usd",
            "cost_is_estimated", "is_error", "latency_ms",
            "application_key", "_data_class"]

DEFAULTS = {
    "model_key": F.lit(""), "cost_center_key": F.lit(""),
    "input_tokens": F.lit(0), "output_tokens": F.lit(0),
    "cached_tokens": F.lit(0), "requests": F.lit(0),
    "is_error": F.lit("False"), "latency_ms": F.lit(0.0),
    "application_key": F.lit(""), "_data_class": F.lit("MOCK"),
}
CASTS = {"usage_date": "date", "quantity": "double", "cost_usd": "double",
         "cost_is_estimated": "boolean", "input_tokens": "long",
         "output_tokens": "long", "cached_tokens": "long", "requests": "long",
         "latency_ms": "double", "is_error": "string", "model_key": "string",
         "cost_center_key": "string", "platform_key": "string",
         "identity_key": "string", "unit_type": "string",
         "application_key": "string", "_data_class": "string"}


def conform(df):
    for col, default in DEFAULTS.items():
        if col not in df.columns:
            df = df.withColumn(col, default)
    for col, typ in CASTS.items():
        df = df.withColumn(col, F.col(col).cast(typ))
    return df.select(*CONTRACT)


parts = []

# ----------------------------------------------- 1. M365 Copilot seats (fixed)
# Graph gives entitlement, never dollars -> priced from the rate card.
parts.append(conform(bronze("bronze_m365_copilot_seats")
    .withColumn("usage_date", F.col("snapshot_date"))
    .withColumn("platform_key", F.lit("M365Copilot"))
    .withColumn("identity_key", F.col("user_principal_name"))
    .withColumn("unit_type", F.lit("seat_day"))
    .withColumn("quantity", F.lit(1.0))
    .withColumn("cost_usd", F.lit(rate("M365Copilot", "seat_day")))
    .withColumn("cost_is_estimated", F.lit(True))))

# --------------------------------------- 2. M365 Copilot activity (signal only)
# The Graph usage report returns per-app LAST ACTIVITY DATES, not prompt counts.
# One row per user per day they were genuinely active: quantity/requests = 1
# means "active that day", NOT "one prompt". Prompt counts do not exist in Graph.
usage = bronze("bronze_m365_copilot_usage")
act_cols = [c for c in usage.columns
            if c.endswith("_last_activity") or c == "last_activity_date"]
parts.append(conform(usage
    .withColumn("last_active", F.greatest(*[F.col(c).cast("date") for c in act_cols]))
    .filter(F.col("last_active").isNotNull()
            & (F.col("last_active") == F.col("report_date").cast("date")))
    .withColumn("usage_date", F.col("report_date"))
    .withColumn("platform_key", F.lit("M365Copilot"))
    .withColumn("identity_key", F.col("user_principal_name"))
    .withColumn("unit_type", F.lit("active_day"))
    .withColumn("quantity", F.lit(1.0))
    .withColumn("requests", F.lit(1))
    .withColumn("cost_usd", F.lit(0.0))          # activity is never billed
    .withColumn("cost_is_estimated", F.lit(False))))

# ------------------------------------------- 3. M365 Copilot credits (variable)
# Credit meters carry real cost_usd -> billed, not modelled.
parts.append(conform(bronze("bronze_m365_copilot_credits")
    .withColumn("platform_key", F.lit("M365Copilot"))
    .withColumn("identity_key", F.col("consumer_id"))
    .withColumn("model_key", F.col("capability"))
    .withColumn("unit_type", F.lit("copilot_credit"))
    .withColumn("quantity", F.col("credits_consumed"))
    .withColumn("cost_is_estimated", F.lit(False))))

# ------------------------------------------------------ 4. GitHub Copilot seats
parts.append(conform(bronze("bronze_ghc_seats")
    .withColumn("usage_date", F.col("snapshot_date"))
    .withColumn("platform_key", F.lit("GitHubCopilot"))
    .withColumn("identity_key", F.col("assignee_login"))
    .withColumn("unit_type", F.lit("seat_day"))
    .withColumn("quantity", F.lit(1.0))
    .withColumn("cost_usd", F.lit(rate("GitHubCopilot", "seat_day")))
    .withColumn("cost_is_estimated", F.lit(True))))

# ------------------------------- 5. GitHub premium requests (REAL billed amount)
# net_amount is what GitHub actually charged -> the highest-fidelity cost we have.
parts.append(conform(bronze("bronze_ghc_premium_usage")
    .withColumn("platform_key", F.lit("GitHubCopilot"))
    .withColumn("identity_key", F.col("login"))
    .withColumn("model_key", F.coalesce(F.col("model"), F.lit("")))
    .withColumn("unit_type", F.lit("premium_request"))
    .withColumn("requests", F.col("quantity"))
    .withColumn("cost_usd", F.col("net_amount"))
    .withColumn("cost_is_estimated", F.lit(False))))

# ------------------------------------------- 6. Copilot Studio credits (billed)
# Dataverse reports CREDITS, not dollars. The mock feed carries a real cost_usd;
# a live msdyn_aievent extract cannot, so those rows are priced from the rate
# card and marked estimated. Deciding this per row rather than per source keeps
# the billed-vs-modelled split truthful whichever feed a row came from.
studio = bronze("bronze_studio_credits")
has_cost = F.col("cost_usd").isNotNull() & (F.col("cost_usd").cast("double") != 0)
parts.append(conform(studio
    .withColumn("platform_key", F.lit("CopilotStudio"))
    .withColumn("identity_key", F.col("agent_id"))
    .withColumn("model_key", F.col("action_type"))
    .withColumn("unit_type", F.lit("copilot_credit"))
    .withColumn("quantity", F.col("credits_consumed"))
    .withColumn("requests", F.col("session_count"))
    .withColumn("cost_is_estimated", ~has_cost)
    .withColumn("cost_usd",
                F.when(has_cost, F.col("cost_usd").cast("double"))
                 .otherwise(F.col("credits_consumed").cast("double")
                            * F.lit(rate("CopilotStudio", "copilot_credit"))))))

# ------------------------------------- 7. Azure AI / Foundry tokens (modelled)
# Resource-level telemetry carries no identity -> 'unknown', which is the honest
# answer and is exactly what dim_identity's Unattributed member exists for.
met = bronze("bronze_azure_ai_metrics")
models = [r[0] for r in met.select("model_name").distinct().collect() if r[0]]


def price_col(unit):
    expr = F.lit(0.0)
    for m in models:
        expr = F.when(F.col("model_name") == m, F.lit(rate("Foundry", unit, m))).otherwise(expr)
    return expr


parts.append(conform(met
    .withColumn("usage_date", F.col("metric_time").cast("date"))
    .withColumn("platform_key", F.lit("Foundry"))
    .withColumn("identity_key", F.lit("unknown"))
    .withColumn("model_key", F.col("model_name"))
    .withColumn("unit_type", F.lit("token"))
    .withColumn("input_tokens", F.col("processed_prompt_tokens"))
    .withColumn("output_tokens", F.col("generated_tokens"))
    .withColumn("quantity", F.col("total_tokens"))
    .withColumn("requests", F.col("requests"))
    .withColumn("latency_ms", F.col("latency_ms"))
    # Azure Monitor metrics carry no per-request error flag. throttled_count is
    # a capacity signal, not a failure count, so mapping it to is_error would
    # report a ~90% error rate that is simply wrong.
    .withColumn("is_error", F.lit("False"))
    .withColumn("cost_usd",
                F.col("processed_prompt_tokens").cast("double") * price_col("input_token")
                + F.col("generated_tokens").cast("double") * price_col("output_token"))
    .withColumn("cost_is_estimated", F.lit(True))))

# ------------------------------------- 8. REAL Azure invoiced spend (billed)
# Only present once a real-bronze lakehouse is attached. This is the highest-
# fidelity cost in the whole model: it is what Microsoft actually invoiced, per
# resource per day, so cost_is_estimated is False and nothing is modelled.
#
# Two platforms, deliberately separated rather than lumped into one "Azure"
# bucket:
#   AzureAI    - Cognitive Services / AI Foundry / ML services. AI spend proper.
#   AzureInfra - the storage, search, database and networking tier the AI
#                workloads run on. Real money caused by AI, but not AI meters.
# Merging them would overstate AI platform spend; dropping the second would
# understate the true cost of running these workloads. Both are wrong, so the
# model reports them side by side and lets the reader choose the denominator.
if has_real("bronze_azure_cost"):
    ai_svc = F.lower(F.coalesce(F.col("consumed_service"), F.lit("")))
    is_ai = (ai_svc.contains("cognitiveservices")
             | ai_svc.contains("machinelearningservices")
             | F.lower(F.coalesce(F.col("resource_id"), F.lit("")))
                .contains("/microsoft.cognitiveservices/"))
    parts.append(conform(bronze("bronze_azure_cost")
        .withColumn("platform_key", F.when(is_ai, F.lit("AzureAI"))
                                     .otherwise(F.lit("AzureInfra")))
        # Billing data is resource-scoped, never user-scoped. Claiming an
        # identity here would be an invention; 'unknown' routes to the
        # Unattributed member, which is the honest answer and is exactly the
        # attribution gap this accelerator is meant to make visible.
        .withColumn("identity_key", F.lit("unknown"))
        .withColumn("model_key", F.lit(""))
        .withColumn("cost_center_key", F.coalesce(F.col("cost_center"), F.lit("")))
        .withColumn("unit_type", F.lit("azure_meter"))
        .withColumn("quantity", F.col("quantity").cast("double"))
        .withColumn("cost_usd", F.col("cost_usd").cast("double"))
        .withColumn("cost_is_estimated", F.lit(False))))

# --------------------------------------------------------------- union + write
silver = parts[0]
for p in parts[1:]:
    silver = silver.unionByName(p)
silver = silver.filter(F.col("usage_date").isNotNull()).cache()

print("writing silver:")
write_silver(silver, "usage_conformed")

agg = silver.selectExpr("count(*) n", "sum(cost_usd) c").collect()[0]
billed = (silver.filter(~F.col("cost_is_estimated"))
          .selectExpr("sum(cost_usd) c").collect()[0][0]) or 0.0
total = agg["c"] or 0.0
print(f"\n{agg['n']:,} conformed rows, ${total:,.2f} total")
print(f"${billed:,.2f} billed / ${total - billed:,.2f} modelled "
      f"({billed / (total or 1) * 100:.1f}% cost confidence)")
print("\nby provenance:")
(silver.groupBy("_data_class")
       .agg(F.count("*").alias("rows"), F.round(F.sum("cost_usd"), 2).alias("cost_usd"))
       .orderBy("_data_class").show(10, False))
print("\nby platform x unit_type:")
(silver.groupBy("platform_key", "unit_type", "_data_class")
       .agg(F.count("*").alias("rows"), F.round(F.sum("cost_usd"), 2).alias("cost_usd"))
       .orderBy("platform_key", "unit_type").show(40, False))
