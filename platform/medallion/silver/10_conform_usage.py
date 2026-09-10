# Fabric notebook — SILVER: conformed entities
# ---------------------------------------------------------------------------
# RUNNABLE. Reads Bronze Delta tables (mock + real lakehouses) and writes the
# curated Silver entity tables that Gold consumes.
#
# WHY THIS IS NOT ONE TABLE
# -------------------------
# Silver's job is *curation*, not modelling. An earlier version of this notebook
# collapsed every source straight into a single fact-shaped `usage_conformed`
# table, which skipped the layer entirely: there was nowhere to resolve identity
# once, nowhere to hold a de-duplicated reference entity, and nowhere for a data
# quality gate to live. Gold then had to read Bronze directly for its
# dimensions, which is the medallion smell of a layer being bypassed.
#
# The table list below is the one already specified in docs/medallion-tables.md;
# it was documented but never implemented. This notebook implements it.
#
#   reference/master   silver_org_hierarchy, silver_identity_resolved,
#                      silver_application_map, silver_model_map,
#                      silver_rate_card
#   per-platform usage silver_usage_foundry, silver_usage_m365,
#                      silver_usage_ghc, silver_usage_studio,
#                      silver_usage_azure
#   integration        silver_usage_unified   (daily grain, the Gold input)
#   quality            silver_grain_audit, silver_cost_reconciliation
#
# THE FOUR SILVER RESPONSIBILITIES
# --------------------------------
#   1. Normalize attributes across sources  -> each silver_usage_* forces its
#      source onto USAGE_CONTRACT; silver_model_map canonicalises model names.
#   2. De-duplicate to a per-month grain    -> dedupe()/dedupe_key():
#      a re-published feed must not double a month.
#   3. Grain guard: cumulative vs delta     -> grain_guard(): converts declared
#      cumulative feeds to deltas, and flags a "delta" feed that actually looks
#      cumulative. Summing a cumulative series is the classic FinOps
#      double-count and it is completely silent.
#   4. Join telemetry to the BU / application map -> enrichment happens here,
#      once, so Gold consumes resolved business keys instead of re-deriving
#      them per dimension.
#
# Cost provenance is preserved per row and never averaged away:
#   cost_is_estimated = False -> billed by the platform
#   cost_is_estimated = True  -> modelled from the rate card
#
# Parameters (WS_ID / BRONZE_ID / BRONZE_REAL_ID / SILVER_ID / GOLD_ID) are
# injected as a preceding cell by platform/deploy/fabric_deploy.py.
#
# The original design scaffold is kept as 10_conform_usage.design.py.
# ---------------------------------------------------------------------------
from pyspark.sql import functions as F
from pyspark.sql.window import Window
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
    dfs = []
    for lh in (BRONZE_ID, BRONZE_REAL):
        if not lh:
            continue
        try:
            dfs.append(_read(lh, tbl))
        except Exception:                                         # noqa: BLE001
            pass
    if not dfs:
        raise AnalysisException(f"bronze table {tbl} exists in neither lakehouse")
    out = dfs[0]
    for d in dfs[1:]:
        out = out.unionByName(d, allowMissingColumns=True)
    return out


def has_bronze(tbl):
    try:
        bronze(tbl)
        return True
    except Exception:                                             # noqa: BLE001
        return False


WRITTEN = {}


def write_silver(df, tbl):
    path = ONELAKE.format(ws=WS_ID, lh=SILVER_ID, tbl=tbl)
    (df.write.format("delta").mode("overwrite")
       .option("overwriteSchema", "true").save(path))
    n = df.count()
    WRITTEN[tbl] = n
    print(f"  + silver.{tbl:28} {n:8,} rows")
    return df


# ------------------------------------------------------------ 2. de-duplicate
# Lineage stamps are re-written on every extract, so they can never take part in
# identifying a duplicate: including them would make every re-run look like new
# data, and excluding the wrong ones would silently merge distinct rows.
RUN_STAMPS = ["_ingested_at", "_batch_id", "_loaded_at", "_source_file",
              "_watermark", "_source_api", "_source_system"]

# The additive columns per source. Everything else is descriptive and therefore
# part of the natural key.
MEASURES = {
    "bronze_azure_cost": ["quantity", "cost_usd", "line_item_count"],
    "bronze_azure_ai_metrics": ["processed_prompt_tokens", "generated_tokens",
                                "total_tokens", "requests", "latency_ms",
                                "throttled_count"],
    "bronze_studio_credits": ["credits_consumed", "cost_usd", "session_count"],
    "bronze_ghc_premium_usage": ["quantity", "net_amount", "gross_amount"],
    "bronze_m365_copilot_credits": ["credits_consumed", "cost_usd"],
}


def dedupe(df, source, order_col=None):
    """Collapse exact re-publications of the same observation.

    The natural key is *every descriptive column* — deliberately not a
    hand-picked subset. Picking a subset is how this went wrong the first time:
    keying Azure cost on (date, resource, meter, price) merged two genuinely
    different charges that differed only by resource tag, silently dropping 824
    rows and $166.99 of real spend. An observation is identified by all of its
    attributes; anything narrower is a guess that loses money.

    Bronze already de-duplicates the real feed on the same principle, so this is
    normally a no-op. It stays because silver must not depend on an upstream
    layer's guarantee.
    """
    measures = MEASURES.get(source, [])
    keys = [c for c in df.columns if c not in measures and c not in RUN_STAMPS]
    if not keys:
        return df
    order = order_col or ("_ingested_at" if "_ingested_at" in df.columns else None)
    if order is None:
        return df.dropDuplicates(keys)
    w = Window.partitionBy(*keys).orderBy(F.col(order).desc())
    return (df.withColumn("_rn", F.row_number().over(w))
              .filter(F.col("_rn") == 1).drop("_rn"))


def dedupe_key(df, keys, order_col=None):
    """One row per business key — for REFERENCE entities only.

    A dimension must be unique on its key, so collapsing to the latest
    observation per key is the whole point here. This is deliberately NOT used
    on usage feeds: there, a narrow key merges distinct observations and loses
    money (see dedupe()).
    """
    keys = [k for k in keys if k in df.columns]
    if not keys:
        return df
    order = order_col or ("_watermark" if "_watermark" in df.columns else None)
    if order is None:
        order = "_ingested_at" if "_ingested_at" in df.columns else None
    if order is None:
        return df.dropDuplicates(keys)
    w = Window.partitionBy(*keys).orderBy(F.col(order).desc())
    return (df.withColumn("_rn", F.row_number().over(w))
              .filter(F.col("_rn") == 1).drop("_rn"))


def dedupe_month(df, date_col, keys, order_col=None):
    """Collapse a monthly-snapshot feed to one row per key per month.

    A licence/SKU inventory describes *state*, not events. Ingesting it daily
    and then summing produces a month's worth of duplicate entitlement, so the
    latest snapshot in each month is taken as authoritative. Applied only to
    feeds declared `snapshot` in ACCUMULATION.
    """
    keys = [k for k in keys if k in df.columns]
    d = df.withColumn("_month", F.date_format(F.col(date_col).cast("date"), "yyyy-MM"))
    order = order_col or ("_ingested_at" if "_ingested_at" in d.columns else None)
    part = keys + ["_month"]
    if order is None:
        return d.dropDuplicates(part).drop("_month")
    w = Window.partitionBy(*part).orderBy(F.col(order).desc())
    return (d.withColumn("_rn", F.row_number().over(w))
             .filter(F.col("_rn") == 1).drop("_rn", "_month"))


# ------------------------------- 3. grain guard (cumulative vs delta)
# Every usage feed is declared here. Getting this wrong is the single most
# expensive silent error in FinOps: summing a month-to-date cumulative series
# across 30 days overstates spend by roughly an order of magnitude, and nothing
# raises an error.
#
#   delta      - each row is that period's increment. Safe to sum.
#   cumulative - each row is running month-to-date. Must be differenced first.
#   snapshot   - each row is state at a point in time, not an increment.
ACCUMULATION = {
    "foundry": "delta",
    "m365_seats": "snapshot",
    "m365_activity": "delta",
    "m365_credits": "delta",
    "ghc_seats": "snapshot",
    "ghc_premium": "delta",
    "studio": "delta",
    "azure_cost": "delta",
}

GRAIN_AUDIT = []


def grain_guard(df, source, date_col, key_cols, value_col):
    """Enforce the declared accumulation semantics, and challenge them.

    For a declared `cumulative` feed the value is differenced within
    (key, month) so it can be summed like every other source.

    For a declared `delta` feed the series is *tested*: if the value is
    non-decreasing across essentially every consecutive period within a month,
    it is almost certainly cumulative and mislabelled. That is reported to
    silver_grain_audit rather than silently summed.
    """
    declared = ACCUMULATION.get(source, "delta")
    if value_col not in df.columns or date_col not in df.columns:
        GRAIN_AUDIT.append((source, declared, "skipped", 0, 0,
                            f"no {value_col}/{date_col} column to check"))
        return df

    if declared == "snapshot":
        GRAIN_AUDIT.append((source, declared, "n/a", 0, 0,
                            "state feed; not summed as an increment"))
        return df

    keys = [k for k in key_cols if k in df.columns]
    d = df.withColumn("_month", F.date_format(F.col(date_col).cast("date"), "yyyy-MM"))
    w = Window.partitionBy(*(keys + ["_month"])).orderBy(F.col(date_col))
    d = d.withColumn("_prev", F.lag(F.col(value_col).cast("double")).over(w))

    if declared == "cumulative":
        # First period of the month keeps its value; later periods take the
        # increment. A negative increment means the source reset mid-month, so
        # fall back to the raw value rather than emitting negative spend.
        delta = F.when(F.col("_prev").isNull(), F.col(value_col).cast("double")) \
                 .otherwise(F.col(value_col).cast("double") - F.col("_prev"))
        d = d.withColumn(value_col,
                         F.when(delta < 0, F.col(value_col).cast("double"))
                          .otherwise(delta))
        GRAIN_AUDIT.append((source, declared, "converted", 0, 0,
                            "cumulative series differenced to per-period deltas"))
        return d.drop("_prev", "_month")

    # declared == "delta": challenge it.
    steps = d.filter(F.col("_prev").isNotNull())
    total = steps.count()
    rising = (steps.filter(F.col(value_col).cast("double") >= F.col("_prev")).count()
              if total else 0)
    ratio = (rising / total) if total else 0.0
    suspicious = total >= 5 and ratio >= 0.95
    note = (f"LOOKS CUMULATIVE - non-decreasing in {ratio:.0%} of {total} "
            f"consecutive periods; summing this would double-count"
            if suspicious else
            f"non-decreasing in {ratio:.0%} of {total} steps")
    GRAIN_AUDIT.append((source, declared, "suspect" if suspicious else "ok",
                        total, rising, note))
    return d.drop("_prev", "_month")


# --------------------------------------------------------------- 1. normalize
# The conformed column contract. Every silver_usage_* is forced onto exactly
# this, which is what makes the union in silver_usage_unified total.
USAGE_CONTRACT = ["usage_date", "platform_key", "identity_key", "model_key",
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
    return df.select(*USAGE_CONTRACT)


# --------------------------------------------------------------- reference data
print("reference entities:")

# --- silver_org_hierarchy
org = dedupe_key(bronze("bronze_ref_business_hierarchy"), ["business_unit_key"])
silver_org = write_silver(org.select(
    "business_unit_key", "business_unit_name", "division",
    F.col("monthly_budget_usd").cast("long").alias("monthly_budget_usd"),
    "executive_owner",
    F.coalesce(F.col("_data_class"), F.lit("MOCK")).alias("_data_class"),
), "silver_org_hierarchy")

# --- silver_identity_resolved
# The identity graph is the core IP: github_login <-> UPN <-> service principal
# <-> agent unified onto one identity_key. Agents are folded in here rather than
# left to Gold, because an agent's owning BU is what makes agent spend
# chargeable at all.
idm = dedupe_key(bronze("bronze_ref_identity_map"), ["identity_key"])
ident = (idm
    .withColumn("identity_class",
        F.when(F.col("principal_type") == "User", "Human")
         .when(F.col("principal_type") == "ServicePrincipal", "ServicePrincipal")
         .when(F.col("principal_type") == "ManagedIdentity", "ManagedIdentity")
         .when(F.col("principal_type") == "Agent", "Agent")
         .otherwise("Application"))
    .select("identity_key", "display_name", "principal_type", "upn",
            "github_login", F.col("department").alias("team"),
            F.coalesce(F.col("home_business_unit_key"), F.lit(""))
             .alias("home_business_unit_key"),
            F.coalesce(F.col("cost_center_key"), F.lit("")).alias("cost_center_key"),
            "identity_class",
            F.upper(F.col("is_human").cast("string")).alias("is_human"),
            F.coalesce(F.col("_data_class"), F.lit("MOCK")).alias("_data_class")))

if has_bronze("bronze_ref_agent_inventory"):
    agents = dedupe_key(bronze("bronze_ref_agent_inventory"), ["agent_key"])
    agent_ident = agents.select(
        F.col("agent_key").alias("identity_key"),
        F.col("agent_name").alias("display_name"),
        F.lit("Agent").alias("principal_type"),
        F.lit("").alias("upn"), F.lit("").alias("github_login"),
        F.lit("").alias("team"),
        F.coalesce(F.col("owner_business_unit_key"), F.lit(""))
         .alias("home_business_unit_key"),
        F.lit("").alias("cost_center_key"),
        F.lit("Agent").alias("identity_class"),
        F.lit("FALSE").alias("is_human"),
        F.coalesce(F.col("_data_class"), F.lit("MOCK")).alias("_data_class"))
    ident = ident.unionByName(
        agent_ident.join(ident.select("identity_key"), "identity_key", "left_anti"))

silver_identity = write_silver(dedupe_key(ident, ["identity_key"]),
                               "silver_identity_resolved")

# Alias -> canonical. Usage feeds arrive keyed by UPN, github login or agent id;
# resolving them once here is what stops Gold guessing per dimension.
# NB: do not name the column "alias" — DataFrame.alias is a method, so
# resolve.alias would return the bound method and the join silently breaks.
resolve = (silver_identity
    .select(F.col("identity_key").alias("canon"), "upn", "github_login")
    .withColumn("alias_key", F.explode(F.array(
        F.col("canon"), F.col("upn"), F.col("github_login"))))
    .filter(F.col("alias_key").isNotNull() & (F.col("alias_key") != ""))
    .select("alias_key", "canon").distinct())

# --- silver_application_map
apps = dedupe_key(bronze("bronze_ref_app_inventory"), ["application_key"])
silver_apps = write_silver(apps.select(
    "application_key", "application_name", "application_type",
    F.coalesce(F.col("owner_business_unit_key"), F.lit(""))
     .alias("owner_business_unit_key"),
    "owner_upn",
    F.concat(F.lit("ENV-"), F.upper(F.col("environment")))
     .alias("default_environment_key"),
    "criticality",
    F.coalesce(F.col("_data_class"), F.lit("MOCK")).alias("_data_class"),
), "silver_application_map")

# --- silver_data_source_catalog
# A hand-maintained reference catalog rather than telemetry, but it still passes
# through silver: Gold reading Bronze directly is the layer bypass this
# restructure exists to remove, and a single exception would reintroduce it.
if has_bronze("bronze_ref_extractable_catalog"):
    write_silver(bronze("bronze_ref_extractable_catalog").dropDuplicates(),
                 "silver_data_source_catalog")

# --- silver_rate_card
rc = dedupe_key(bronze("bronze_ref_rate_card"),
                 ["platform", "unit_type", "model"])
silver_rates = write_silver(rc.select(
    "rate_key", "platform", "unit_type",
    F.coalesce(F.col("model"), F.lit("")).alias("model"),
    F.col("unit_price_usd").cast("double").alias("unit_price_usd"),
    F.col("effective_from").cast("date").alias("effective_from"),
    "currency", "source", "note",
), "silver_rate_card")

rate_map = {(r["platform"], r["unit_type"], r["model"] or ""): float(r["unit_price_usd"])
            for r in silver_rates.collect()}


def rate(platform, unit_type, model=""):
    return rate_map.get((platform, unit_type, model),
                        rate_map.get((platform, unit_type, ""), 0.0))


print("\nper-platform usage:")

# --------------------------------------------------------- silver_usage_m365
m365_parts = []

# Seats: entitlement, never dollars -> priced from the rate card. A daily seat
# snapshot is state, so it is deduped per user per day before being valued.
seats = dedupe(bronze("bronze_m365_copilot_seats"), "bronze_m365_copilot_seats")
seats = grain_guard(seats, "m365_seats", "snapshot_date",
                    ["user_principal_name"], "assigned_seats")
m365_parts.append(conform(seats
    .withColumn("usage_date", F.col("snapshot_date"))
    .withColumn("platform_key", F.lit("M365Copilot"))
    .withColumn("identity_key", F.col("user_principal_name"))
    .withColumn("unit_type", F.lit("seat_day"))
    .withColumn("quantity", F.lit(1.0))
    .withColumn("cost_usd", F.lit(rate("M365Copilot", "seat_day")))
    .withColumn("cost_is_estimated", F.lit(True))))

# Activity: the Graph usage report returns per-app LAST ACTIVITY DATES, not
# prompt counts. One row per user per day they were genuinely active:
# quantity/requests = 1 means "active that day", NOT "one prompt".
usage = bronze("bronze_m365_copilot_usage")
act_cols = [c for c in usage.columns
            if c.endswith("_last_activity") or c == "last_activity_date"]
usage = dedupe(usage, "bronze_m365_copilot_usage")
m365_parts.append(conform(usage
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

# Credits: credit meters carry real cost_usd -> billed, not modelled.
credits = dedupe(bronze("bronze_m365_copilot_credits"), "bronze_m365_copilot_credits")
credits = grain_guard(credits, "m365_credits", "usage_date",
                      ["consumer_id"], "credits_consumed")
m365_parts.append(conform(credits
    .withColumn("platform_key", F.lit("M365Copilot"))
    .withColumn("identity_key", F.col("consumer_id"))
    .withColumn("model_key", F.col("capability"))
    .withColumn("unit_type", F.lit("copilot_credit"))
    .withColumn("quantity", F.col("credits_consumed"))
    .withColumn("cost_is_estimated", F.lit(False))))

silver_m365 = m365_parts[0]
for p in m365_parts[1:]:
    silver_m365 = silver_m365.unionByName(p)
write_silver(silver_m365, "silver_usage_m365")

# ---------------------------------------------------------- silver_usage_ghc
ghc_parts = []
ghc_seats = dedupe(bronze("bronze_ghc_seats"), "bronze_ghc_seats")
ghc_parts.append(conform(ghc_seats
    .withColumn("usage_date", F.col("snapshot_date"))
    .withColumn("platform_key", F.lit("GitHubCopilot"))
    .withColumn("identity_key", F.col("assignee_login"))
    .withColumn("unit_type", F.lit("seat_day"))
    .withColumn("quantity", F.lit(1.0))
    .withColumn("cost_usd", F.lit(rate("GitHubCopilot", "seat_day")))
    .withColumn("cost_is_estimated", F.lit(True))))

# net_amount is what GitHub actually charged -> the highest-fidelity cost here.
prem = dedupe(bronze("bronze_ghc_premium_usage"), "bronze_ghc_premium_usage")
prem = grain_guard(prem, "ghc_premium", "usage_date", ["login"], "quantity")
ghc_parts.append(conform(prem
    .withColumn("platform_key", F.lit("GitHubCopilot"))
    .withColumn("identity_key", F.col("login"))
    .withColumn("model_key", F.coalesce(F.col("model"), F.lit("")))
    .withColumn("unit_type", F.lit("premium_request"))
    .withColumn("requests", F.col("quantity"))
    .withColumn("cost_usd", F.col("net_amount"))
    .withColumn("cost_is_estimated", F.lit(False))))

silver_ghc = ghc_parts[0]
for p in ghc_parts[1:]:
    silver_ghc = silver_ghc.unionByName(p)
write_silver(silver_ghc, "silver_usage_ghc")

# ------------------------------------------------------- silver_usage_studio
# Dataverse reports CREDITS, not dollars. The mock feed carries a real cost_usd;
# a live msdyn_aievent extract cannot, so those rows are priced from the rate
# card and marked estimated. Deciding this per row rather than per source keeps
# the billed-vs-modelled split truthful whichever feed a row came from.
studio = dedupe(bronze("bronze_studio_credits"), "bronze_studio_credits")
studio = grain_guard(studio, "studio", "usage_date", ["agent_id"],
                     "credits_consumed")
has_cost = F.col("cost_usd").isNotNull() & (F.col("cost_usd").cast("double") != 0)
silver_studio = conform(studio
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
                            * F.lit(rate("CopilotStudio", "copilot_credit")))))
write_silver(silver_studio, "silver_usage_studio")

# ------------------------------------------------------ silver_usage_foundry
# Resource-level telemetry carries no identity -> 'unknown', which is the honest
# answer and is exactly what the Unattributed member exists for.
met = dedupe(bronze("bronze_azure_ai_metrics"), "bronze_azure_ai_metrics")
met = grain_guard(met, "foundry", "metric_time", ["resource_id"], "total_tokens")
models = [r[0] for r in met.select("model_name").distinct().collect() if r[0]]


def price_col(unit):
    expr = F.lit(0.0)
    for m in models:
        expr = F.when(F.col("model_name") == m,
                      F.lit(rate("Foundry", unit, m))).otherwise(expr)
    return expr


silver_foundry = conform(met
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
    .withColumn("cost_is_estimated", F.lit(True)))
write_silver(silver_foundry, "silver_usage_foundry")

# -------------------------------------------------------- silver_usage_azure
# REAL invoiced spend. The highest-fidelity cost in the model: what Microsoft
# actually charged, per resource per day, so nothing is modelled.
#
# Two platforms, deliberately separated rather than lumped into one "Azure"
# bucket:
#   AzureAI    - Cognitive Services / AI Foundry / ML services. AI spend proper.
#   AzureInfra - the storage, search, database and networking tier the AI
#                workloads run on. Real money caused by AI, but not AI meters.
# Merging them would overstate AI platform spend; dropping the second would
# understate the true cost of running these workloads.
silver_azure = None
if has_bronze("bronze_azure_cost"):
    az = bronze("bronze_azure_cost")
    az = dedupe(az, "bronze_azure_cost")
    az = grain_guard(az, "azure_cost", "usage_date", ["resource_id"], "cost_usd")
    ai_svc = F.lower(F.coalesce(F.col("consumed_service"), F.lit("")))
    is_ai = (ai_svc.contains("cognitiveservices")
             | ai_svc.contains("machinelearningservices")
             | F.lower(F.coalesce(F.col("resource_id"), F.lit("")))
                .contains("/microsoft.cognitiveservices/"))
    silver_azure = conform(az
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
        .withColumn("cost_is_estimated", F.lit(False)))
    write_silver(silver_azure, "silver_usage_azure")

# ----------------------------------------------------------- silver_model_map
# Canonical model identity. MODEL-NA is a real sentinel, not an empty string:
# the engine coerces "" to BLANK and a blank foreign key never matches a
# dimension row, so those rows would silently detach in every model slicer.
NA_MODEL = "MODEL-NA"
usage_parts = [silver_m365, silver_ghc, silver_studio, silver_foundry]
if silver_azure is not None:
    usage_parts.append(silver_azure)

all_usage = usage_parts[0]
for p in usage_parts[1:]:
    all_usage = all_usage.unionByName(p)
all_usage = (all_usage.filter(F.col("usage_date").isNotNull())
             .withColumn("model_key",
                         F.when((F.col("model_key").isNull())
                                | (F.col("model_key") == ""), F.lit(NA_MODEL))
                          .otherwise(F.col("model_key"))).cache())

write_silver(all_usage.select("model_key").distinct()
    .withColumn("model_name", F.when(F.col("model_key") == NA_MODEL,
                                     F.lit("(not model-specific)"))
                               .otherwise(F.col("model_key")))
    .withColumn("model_version", F.lit(""))
    .withColumn("provider", F.when(F.col("model_key") == NA_MODEL, F.lit("n/a"))
                             .when(F.col("model_key").startswith("gpt"), F.lit("OpenAI"))
                             .when(F.col("model_key").startswith("claude"),
                                   F.lit("Anthropic"))
                             .otherwise(F.lit("Microsoft")))
    .withColumn("modality", F.when(F.col("model_key") == NA_MODEL, F.lit("n/a"))
                             .otherwise(F.lit("text")))
    .select("model_key", "model_name", "model_version", "provider", "modality"),
    "silver_model_map")

# -------------------------------------- 4. join telemetry to BU / application
# Enrichment happens once, here, so Gold consumes resolved business keys rather
# than re-deriving them per dimension.
print("\nintegration:")

unified = (all_usage
    .join(resolve, all_usage["identity_key"] == resolve["alias_key"], "left")
    .withColumn("identity_key", F.coalesce(F.col("canon"), F.col("identity_key")))
    .drop("alias_key", "canon"))

# Business unit: the identity's home BU first, then the owning BU of the
# application the spend landed on, then the explicit Unallocated member. A blank
# would land on Power BI's auto-generated blank row and detach the spend.
unified = (unified
    .join(silver_identity.select(
        "identity_key",
        F.col("home_business_unit_key").alias("_bu_from_identity"),
        F.col("cost_center_key").alias("_cc_from_identity")),
        "identity_key", "left")
    .join(silver_apps.select(
        "application_key",
        F.col("owner_business_unit_key").alias("_bu_from_app")),
        "application_key", "left")
    .withColumn("business_unit_key", F.coalesce(
        F.when(F.col("_bu_from_identity") != "", F.col("_bu_from_identity")),
        F.when(F.col("_bu_from_app") != "", F.col("_bu_from_app")),
        F.lit("BU-UNALLOC")))
    .withColumn("cost_center_key", F.coalesce(
        F.when(F.col("cost_center_key") != "", F.col("cost_center_key")),
        F.when(F.col("_cc_from_identity") != "", F.col("_cc_from_identity")),
        F.lit("")))
    .drop("_bu_from_identity", "_cc_from_identity", "_bu_from_app"))

UNIFIED_CONTRACT = USAGE_CONTRACT + ["business_unit_key"]
silver_unified = write_silver(unified.select(*UNIFIED_CONTRACT),
                              "silver_usage_unified").cache()

# ------------------------------------------------- silver_cost_reconciliation
# Billed vs modelled per platform per month. This is what drives
# [Cost Confidence %]; materialising it makes the number auditable rather than
# something that exists only inside a DAX expression.
recon = (silver_unified
    .withColumn("year_month", F.date_format("usage_date", "yyyy-MM"))
    .groupBy("platform_key", "year_month", "_data_class")
    .agg(F.round(F.sum(F.when(~F.col("cost_is_estimated"), F.col("cost_usd"))
                        .otherwise(0.0)), 4).alias("billed_usd"),
         F.round(F.sum(F.when(F.col("cost_is_estimated"), F.col("cost_usd"))
                        .otherwise(0.0)), 4).alias("modelled_usd"),
         F.round(F.sum("cost_usd"), 4).alias("total_usd"),
         F.count(F.lit(1)).alias("row_count"))
    .withColumn("cost_confidence_pct",
                F.round(F.col("billed_usd")
                        / F.when(F.col("total_usd") == 0, None)
                           .otherwise(F.col("total_usd")) * 100, 2)))
write_silver(recon, "silver_cost_reconciliation")

# -------------------------------------------------------- silver_grain_audit
audit = spark.createDataFrame(
    GRAIN_AUDIT,
    "source string, declared string, verdict string, steps_checked long, "
    "steps_non_decreasing long, note string")
write_silver(audit, "silver_grain_audit")

# --------------------------------------------------------------------- report
print("\ngrain guard:")
for src, declared, verdict, steps, rising, note in GRAIN_AUDIT:
    flag = "!!" if verdict == "suspect" else "ok"
    print(f"  {flag} {src:14} declared={declared:11} {note}")

if [a for a in GRAIN_AUDIT if a[2] == "suspect"]:
    print("\n  ! A feed declared 'delta' behaves like a cumulative series.")
    print("    Summing it across a month double-counts. Set it to 'cumulative'")
    print("    in ACCUMULATION and re-run.")

agg = silver_unified.selectExpr("count(*) n", "sum(cost_usd) c").collect()[0]
billed = (silver_unified.filter(~F.col("cost_is_estimated"))
          .selectExpr("sum(cost_usd) c").collect()[0][0]) or 0.0
total = agg["c"] or 0.0
print(f"\nsilver_usage_unified: {agg['n']:,} rows, ${total:,.2f}")
print(f"${billed:,.2f} billed / ${total - billed:,.2f} modelled "
      f"({billed / (total or 1) * 100:.1f}% cost confidence)")

print("\nby provenance:")
(silver_unified.groupBy("_data_class")
    .agg(F.count("*").alias("rows"), F.round(F.sum("cost_usd"), 2).alias("cost_usd"))
    .orderBy("_data_class").show(10, False))

print("by platform x unit_type:")
(silver_unified.groupBy("platform_key", "unit_type", "_data_class")
    .agg(F.count("*").alias("rows"), F.round(F.sum("cost_usd"), 2).alias("cost_usd"))
    .orderBy("platform_key", "unit_type").show(40, False))

print("business-unit attribution (resolved in silver, not gold):")
(silver_unified.groupBy("business_unit_key")
    .agg(F.count("*").alias("rows"), F.round(F.sum("cost_usd"), 2).alias("cost_usd"))
    .orderBy(F.col("cost_usd").desc()).show(20, False))

print(f"\n{len(WRITTEN)} silver table(s) written:")
for t, n in WRITTEN.items():
    print(f"  {t:32} {n:8,}")
