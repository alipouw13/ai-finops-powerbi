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


# The two bronze lakehouses in preference order. Foundry attribution is resolved
# PER LAKEHOUSE (see the foundry block), so this list is the single source of
# truth for "which lakehouses exist".
BRONZE_LAKEHOUSES = [lh for lh in (BRONZE_ID, BRONZE_REAL) if lh]


def has_bronze_in(lh, tbl):
    """Does THIS specific lakehouse carry the table? (not the union of both.)

    bronze()/has_bronze() union mock + real, which is right for a feed that both
    lakehouses may contribute to. It is WRONG for a prefer-one-feed decision: the
    mock lakehouse always emits bronze_foundry_gateway, so a union-level check
    would let the mock gateway suppress a REAL bronze_azure_ai_metrics landed in
    the other lakehouse. Resolving presence per lakehouse keeps each lakehouse's
    richest available Foundry source, and never silently drops the other's.
    """
    if not lh:
        return False
    try:
        _read(lh, tbl)
        return True
    except Exception:                                             # noqa: BLE001
        return False


def focus_col(df, *names, default=None):
    """First FOCUS column that exists, coalesced for row-level nulls.

    FOCUS 1.2-preview (the CURRENT Microsoft dataset version, not 1.0) promoted
    several vendor-prefixed 1.0 columns to standard names and drops the old
    spelling: x_InvoiceId -> InvoiceId, x_PricingCurrency -> PricingCurrency,
    x_SkuMeterName -> SkuMeter. A plain F.coalesce(F.col("x_SkuMeterName"),
    F.col("SkuMeter")) throws AnalysisException on whichever version is missing
    one of the two columns entirely, so pick by PRESENCE first (tolerates either
    schema) and still coalesce the survivors for per-row nulls. Order the names
    richest-first (the x_...InUsd USD-normalised variant before the raw measure)
    so the 1.0 preference is preserved where both exist.
    """
    present = [F.col(n) for n in names if n in df.columns]
    if not present:
        return F.lit(default)
    if default is not None:
        present.append(F.lit(default))
    return present[0] if len(present) == 1 else F.coalesce(*present)


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
    "bronze_foundry_gateway": ["prompt_tokens", "completion_tokens",
                               "cached_prompt_tokens", "total_tokens", "requests",
                               "total_latency_ms"],
    "bronze_studio_credits": ["credits_consumed", "cost_usd", "session_count"],
    "bronze_ghc_premium_usage": ["quantity", "net_amount", "gross_amount"],
    "bronze_m365_copilot_credits": ["credits_consumed", "cost_usd"],
    "bronze_m365_cowork_usage": ["total_tasks", "scheduled_tasks",
                                 "user_initiated_tasks", "credits_consumed"],
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
    "foundry_gateway": "delta",
    "m365_seats": "snapshot",
    "m365_activity": "delta",
    "m365_credits": "delta",
    "m365_cowork": "delta",
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
                  "application_key", "list_cost_usd", "has_rate_card", "_data_class"]

DEFAULTS = {
    "model_key": F.lit(""), "cost_center_key": F.lit(""),
    "input_tokens": F.lit(0), "output_tokens": F.lit(0),
    "cached_tokens": F.lit(0), "requests": F.lit(0),
    "is_error": F.lit("False"), "latency_ms": F.lit(0.0),
    "application_key": F.lit(""),
    # list_cost_usd left NULL by default so the unified step can fall it back to
    # cost_usd for rows a rate card never priced; has_rate_card then stays FALSE.
    # has_rate_card is a real BOOLEAN, not the string "FALSE": the Direct Lake
    # TMDL declares it boolean and [Rate Card Coverage %] compares = TRUE. Unlike
    # the import model (whose M partition casts to logical), Direct Lake applies
    # no transform, so a "TRUE"/"FALSE" string would never match and coverage
    # would read 0%. Matches cost_is_estimated, the model's other measured bool.
    "list_cost_usd": F.lit(None), "has_rate_card": F.lit(False),
    "_data_class": F.lit("MOCK"),
}
CASTS = {"usage_date": "date", "quantity": "double", "cost_usd": "double",
         "cost_is_estimated": "boolean", "input_tokens": "long",
         "output_tokens": "long", "cached_tokens": "long", "requests": "long",
         "latency_ms": "double", "is_error": "string", "model_key": "string",
         "cost_center_key": "string", "platform_key": "string",
         "identity_key": "string", "unit_type": "string",
         "application_key": "string", "list_cost_usd": "double",
         "has_rate_card": "boolean", "_data_class": "string"}


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


def has_rate(platform, unit_type, model=""):
    """True when the rate card genuinely prices this (platform, unit, model).

    Drives fact_ai_usage[has_rate_card]. A modelled list/rate-card cost is only
    honest when a real rate-card row backs it; when none exists the caller must
    leave list_cost_usd null so the unified step falls it back to cost_usd,
    rather than emitting a $0 list price that reads as "free".
    """
    return ((platform, unit_type, model) in rate_map
            or (platform, unit_type, "") in rate_map)


def priced(df, platform, unit_type, model=""):
    """Attach the rate-card list cost (quantity x unit price), honestly flagged.

    Sets list_cost_usd and has_rate_card only when the rate card actually prices
    the row. Otherwise both are left at their conform() defaults (null / FALSE)
    and the unified step falls list_cost_usd back to cost_usd — so the report
    never shows a modelled list price the row never had. Token-priced feeds
    (Foundry) don't use this: their unit is priced per input/output/cached token,
    not per 'token', so they compute list_cost_usd inline.
    """
    if not has_rate(platform, unit_type, model):
        return df
    return (df.withColumn("list_cost_usd",
                          F.col("quantity").cast("double") * F.lit(rate(platform, unit_type, model)))
              .withColumn("has_rate_card", F.lit(True)))


print("\nper-platform usage:")

# --------------------------------------------------------- silver_usage_m365
m365_parts = []

# Seats: entitlement, never dollars -> priced from the rate card. A daily seat
# snapshot is state, so it is deduped per user per day before being valued.
seats = dedupe(bronze("bronze_m365_copilot_seats"), "bronze_m365_copilot_seats")
seats = grain_guard(seats, "m365_seats", "snapshot_date",
                    ["user_principal_name"], "assigned_seats")
m365_parts.append(conform(priced(seats
    .withColumn("usage_date", F.col("snapshot_date"))
    .withColumn("platform_key", F.lit("M365Copilot"))
    .withColumn("identity_key", F.col("user_principal_name"))
    .withColumn("unit_type", F.lit("seat_day"))
    .withColumn("quantity", F.lit(1.0))
    .withColumn("cost_usd", F.lit(rate("M365Copilot", "seat_day")))
    .withColumn("cost_is_estimated", F.lit(True)), "M365Copilot", "seat_day")))

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
m365_parts.append(conform(priced(credits
    .withColumn("platform_key", F.lit("M365Copilot"))
    .withColumn("identity_key", F.col("consumer_id"))
    .withColumn("model_key", F.col("capability"))
    .withColumn("unit_type", F.lit("copilot_credit"))
    .withColumn("quantity", F.col("credits_consumed"))
    .withColumn("cost_is_estimated", F.lit(False)), "M365Copilot", "copilot_credit")))

silver_m365 = m365_parts[0]
for p in m365_parts[1:]:
    silver_m365 = silver_m365.unionByName(p)
write_silver(silver_m365, "silver_usage_m365")

# --------------------------------------------------- silver_usage_m365_cowork
# Microsoft 365 Copilot Cowork add-on consumption, kept in its own curated
# entity (like the other per-platform feeds) before it is unioned into
# silver_usage_unified. The stakeholder calls these "Cowork units", but there is
# no such billing unit: Cowork is billed in Copilot Credits at $0.01/credit, the
# SAME currency Copilot Studio already uses, so it conforms to
# unit_type = "copilot_credit" on platform_key = "M365Copilot" — a consumptive
# add-on ON TOP of the fixed seat, not a second seat SKU.
#
# The credit export is expressed in credits, never dollars, so cost_usd is
# MODELLED from the rate card (cost_is_estimated = TRUE); requests carry the
# task count so the report can show tasks alongside credits.
#
# Double-count: these same credits also surface on the Azure bill under the
# service "Microsoft Copilot Studio" (Cowork + Studio + Work IQ share one
# meter). Silver's Azure conform EXCLUDES that service so the dollars are counted
# exactly once — see the guard in the FOCUS branch below.
cowork = dedupe(bronze("bronze_m365_cowork_usage"), "bronze_m365_cowork_usage")
cowork = grain_guard(cowork, "m365_cowork", "report_date",
                     ["user_id"], "credits_consumed")
silver_cowork = conform(priced(cowork
    .withColumn("usage_date", F.col("report_date"))
    .withColumn("platform_key", F.lit("M365Copilot"))
    .withColumn("identity_key", F.col("user_id"))
    # model_key = "Cowork" is the discriminator that keeps this feed disjoint
    # from the generic M365 credit feed (silver_usage_m365), which lands on the
    # same platform + unit_type. [Cowork Credits] / [Cowork Add-on Cost] filter
    # on this exact spelling (coordinated with WS-MODEL); without it the two
    # feeds would sum together and double the reported Cowork consumption. It is
    # a real, non-empty model_key so it survives into dim_model rather than being
    # coerced to the MODEL-NA sentinel.
    .withColumn("model_key", F.lit("Cowork"))
    .withColumn("unit_type", F.lit("copilot_credit"))
    .withColumn("quantity", F.col("credits_consumed").cast("double"))
    .withColumn("requests", F.col("total_tasks"))
    # Credits are metered but the export never states dollars, so the cost is
    # modelled from the Copilot Credit list price and flagged estimated.
    .withColumn("cost_usd", F.col("credits_consumed").cast("double")
                * F.lit(rate("M365Copilot", "copilot_credit")))
    .withColumn("cost_is_estimated", F.lit(True)), "M365Copilot", "copilot_credit"))
write_silver(silver_cowork, "silver_usage_m365_cowork")

# ---------------------------------------------------------- silver_usage_ghc
ghc_parts = []
ghc_seats = dedupe(bronze("bronze_ghc_seats"), "bronze_ghc_seats")
ghc_parts.append(conform(priced(ghc_seats
    .withColumn("usage_date", F.col("snapshot_date"))
    .withColumn("platform_key", F.lit("GitHubCopilot"))
    .withColumn("identity_key", F.col("assignee_login"))
    .withColumn("unit_type", F.lit("seat_day"))
    .withColumn("quantity", F.lit(1.0))
    .withColumn("cost_usd", F.lit(rate("GitHubCopilot", "seat_day")))
    .withColumn("cost_is_estimated", F.lit(True)), "GitHubCopilot", "seat_day")))

# net_amount is what GitHub actually charged -> the highest-fidelity cost here.
prem = dedupe(bronze("bronze_ghc_premium_usage"), "bronze_ghc_premium_usage")
prem = grain_guard(prem, "ghc_premium", "usage_date", ["login"], "quantity")
ghc_parts.append(conform(priced(prem
    .withColumn("platform_key", F.lit("GitHubCopilot"))
    .withColumn("identity_key", F.col("login"))
    .withColumn("model_key", F.coalesce(F.col("model"), F.lit("")))
    .withColumn("unit_type", F.lit("premium_request"))
    .withColumn("quantity", F.col("quantity").cast("double"))
    .withColumn("requests", F.col("quantity"))
    .withColumn("cost_usd", F.col("net_amount"))
    .withColumn("cost_is_estimated", F.lit(False)), "GitHubCopilot", "premium_request")))

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
silver_studio = conform(priced(studio
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
                            * F.lit(rate("CopilotStudio", "copilot_credit")))),
    "CopilotStudio", "copilot_credit"))
write_silver(silver_studio, "silver_usage_studio")

# ------------------------------------------------------ silver_usage_foundry
# Foundry token telemetry has two possible sources, and they must NEVER be read
# together or every token is counted twice. This is the SAME prefer-the-richer-
# source-and-fall-back rule the Azure cost branch below applies to FOCUS over
# usageDetails: exactly one feed is ever read.
#
#   bronze_foundry_gateway   - APIM AI Gateway -> Log Analytics. Per request,
#                              per IDENTITY (Entra oid / app client id) with the
#                              cc:/bu: claims the caller forwards. The ONLY
#                              per-user Foundry attribution path, so it wins.
#   bronze_azure_ai_metrics  - Azure Monitor metrics. Resource-grain, carries NO
#                              principal, so every row lands on 'unknown'. Used
#                              only as the fallback when the gateway feed is
#                              absent.
#
# Summing both would double-count tokens and leave the resource-grain 'unknown'
# bar swamping every per-user visual — which is exactly the problem the gateway
# feed exists to fix. The choice is therefore made PER LAKEHOUSE (see the loop
# below): a lakehouse's own gateway supersedes its own metrics, but one
# lakehouse's mock gateway never suppresses another lakehouse's REAL metrics.
def foundry_list_cost(df, in_col, out_col, cached_col):
    """Rate-card list cost + has_rate_card for a Foundry TOKEN feed.

    Foundry is priced PER TOKEN CLASS (input_token / output_token / cached_token
    per model in dim_rate_card), not per a generic 'token' unit, so priced()
    (a single unit_type x quantity) cannot express it. This is the one place a
    multi-unit price is needed; it still reuses the same rate() / has_rate()
    source of truth as priced(), so there is no second rate mechanism:

        list_cost_usd = in*rate(input_token) + out*rate(output_token)
                        + cached*rate(cached_token)          (per model)
        has_rate_card = TRUE for a model that genuinely has a Foundry rate-card
                        row, else FALSE — in which case list_cost_usd falls back
                        to cost_usd in the unified step, exactly like every other
                        feed. Foundry cost IS this same rate-card figure (never
                        invoice-billed here), so list_cost_usd == cost_usd.

    Only ever called inside the Foundry branches below, so the per-token-class
    pricing can never leak onto seat_day / copilot_credit / premium_request rows.
    """
    model_names = [r[0] for r in df.select("model_name").distinct().collect() if r[0]]

    def price(unit):
        expr = F.lit(0.0)
        for m in model_names:
            expr = F.when(F.col("model_name") == m,
                          F.lit(rate("Foundry", unit, m))).otherwise(expr)
        return expr

    cost = (in_col.cast("double") * price("input_token")
            + out_col.cast("double") * price("output_token")
            + cached_col.cast("double") * price("cached_token"))
    priced_models = [m for m in model_names if has_rate("Foundry", "input_token", m)]
    has_rate_flag = (F.when(F.col("model_name").isin(priced_models), F.lit(True))
                      .otherwise(F.lit(False)) if priced_models else F.lit(False))
    return cost, has_rate_flag


# The mock equivalent of ApimClientOwnership_CL: which application each calling
# client owns. Keyed on the app-name the gateway records (upn_or_app_name for a
# service-principal call), so no client-id GUIDs are hardcoded here. This is
# what lets a gateway row resolve a real application_key instead of APP-UNKNOWN.
GATEWAY_APP_OWNERSHIP = {
    "checkout-service": "APP-CHECKOUT",
    "search-api": "APP-SEARCH",
    "ds-notebooks": "APP-DSNB",
    "mobile-assistant": "APP-MOBILE",
}


def _conform_gateway(df):
    df = dedupe(df, "bronze_foundry_gateway")
    df = grain_guard(df, "foundry_gateway", "time_generated",
                     ["client_id", "oid", "model_name"], "total_tokens")
    # Identity comes from the JWT: the user's Entra oid for interactive calls,
    # else the calling app's client id. Both resolve to a real identity_key via
    # silver's alias map, so the spend lands on a person or an app, not 'unknown'.
    identity = (F.when(F.coalesce(F.col("oid"), F.lit("")) != "", F.col("oid"))
                 .when(F.coalesce(F.col("client_id"), F.lit("")) != "", F.col("client_id"))
                 .otherwise(F.lit("unknown")))
    app = F.lit("")
    for caller, appkey in GATEWAY_APP_OWNERSHIP.items():
        app = F.when(F.col("upn_or_app_name") == caller, F.lit(appkey)).otherwise(app)
    # Foundry token rows ARE rate-card priced (there is no invoice line), so the
    # gateway's real input/output/cached token counts give a genuine list cost
    # and has_rate_card = TRUE for every model that has a rate-card row.
    cost, has_rate_flag = foundry_list_cost(
        df, F.col("prompt_tokens"), F.col("completion_tokens"), F.col("cached_prompt_tokens"))
    return conform(df
        .withColumn("usage_date", F.col("time_generated").cast("date"))
        .withColumn("platform_key", F.lit("Foundry"))
        .withColumn("identity_key", identity)
        .withColumn("model_key", F.col("model_name"))
        .withColumn("cost_center_key", F.coalesce(F.col("cost_center_claim"), F.lit("")))
        .withColumn("unit_type", F.lit("token"))
        .withColumn("input_tokens", F.col("prompt_tokens"))
        .withColumn("output_tokens", F.col("completion_tokens"))
        .withColumn("cached_tokens", F.col("cached_prompt_tokens"))
        .withColumn("quantity", F.col("total_tokens"))
        .withColumn("requests", F.col("requests"))
        .withColumn("latency_ms", F.col("total_latency_ms"))
        # The gateway DOES carry a real per-request status, unlike Azure Monitor.
        # Normalise rather than cast. The bronze loader runs inferSchema, so this
        # column arrives as BOOLEAN from a CSV that writes True/False and as
        # STRING from one that does not -- coalescing it against a string literal
        # fails the whole notebook with DATATYPE_MISMATCH. Casting a boolean to
        # string would "work" and be worse: Spark emits lowercase 'true'/'false',
        # which never matches [Error Rate]'s `is_error = "True"`, so the measure
        # would silently read 0% forever.
        .withColumn("is_error",
                    F.when(F.coalesce(F.col("is_error").cast("boolean"), F.lit(False)),
                           F.lit("True")).otherwise(F.lit("False")))
        .withColumn("cost_usd", cost)
        .withColumn("cost_is_estimated", F.lit(True))
        .withColumn("list_cost_usd", cost)
        .withColumn("has_rate_card", has_rate_flag)
        .withColumn("application_key", app))


def _conform_metrics(df):
    df = dedupe(df, "bronze_azure_ai_metrics")
    df = grain_guard(df, "foundry", "metric_time", ["resource_id"], "total_tokens")
    # Azure Monitor carries no cached-token dimension, so cached contributes 0;
    # the same rate card still prices input/output, so has_rate_card is TRUE for
    # a priced model rather than falsely claiming Foundry is unpriced.
    cost, has_rate_flag = foundry_list_cost(
        df, F.col("processed_prompt_tokens"), F.col("generated_tokens"), F.lit(0.0))
    return conform(df
        .withColumn("usage_date", F.col("metric_time").cast("date"))
        .withColumn("platform_key", F.lit("Foundry"))
        # Resource-level telemetry carries no identity -> 'unknown', the honest
        # answer and exactly what the Unattributed member exists for.
        .withColumn("identity_key", F.lit("unknown"))
        .withColumn("model_key", F.col("model_name"))
        .withColumn("unit_type", F.lit("token"))
        .withColumn("input_tokens", F.col("processed_prompt_tokens"))
        .withColumn("output_tokens", F.col("generated_tokens"))
        .withColumn("quantity", F.col("total_tokens"))
        .withColumn("requests", F.col("requests"))
        .withColumn("latency_ms", F.col("latency_ms"))
        # Azure Monitor metrics carry no per-request error flag. throttled_count
        # is a capacity signal, not a failure count, so mapping it to is_error
        # would report a ~90% error rate that is simply wrong.
        .withColumn("is_error", F.lit("False"))
        .withColumn("cost_usd", cost)
        .withColumn("cost_is_estimated", F.lit(True))
        .withColumn("list_cost_usd", cost)
        .withColumn("has_rate_card", has_rate_flag))


# Resolve the gateway-vs-metrics preference PER LAKEHOUSE. gen_bronze_data.py
# always writes bronze_foundry_gateway to the MOCK lakehouse, so a union-level
# `if has_bronze("bronze_foundry_gateway")` would be true in every deployment and
# would silently drop EVERY bronze_azure_ai_metrics row — including the REAL rows
# that 01_load_bronze_real_csv.py explicitly declares as REAL bronze. Resolving
# per lakehouse keeps each lakehouse's richest Foundry source: a lakehouse's own
# gateway supersedes its own metrics (same tokens, never summed), but one
# lakehouse's gateway never suppresses another's metrics. Any within-lakehouse
# drop is announced loudly with its row/token count.
foundry_parts = []
for _lh in BRONZE_LAKEHOUSES:
    if has_bronze_in(_lh, "bronze_foundry_gateway"):
        foundry_parts.append(_conform_gateway(_read(_lh, "bronze_foundry_gateway")))
        if has_bronze_in(_lh, "bronze_azure_ai_metrics"):
            _drop = _read(_lh, "bronze_azure_ai_metrics")
            _n = _drop.count()
            _toks = (_drop.agg(F.sum(F.col("total_tokens").cast("double")))
                     .collect()[0][0]) or 0.0
            print(f"  !! foundry source: lakehouse '{_lh}' has BOTH the APIM gateway "
                  f"and Azure Monitor metrics; preferring the per-user gateway and "
                  f"dropping {_n:,} metric row(s) / {_toks:,.0f} token(s) so the same "
                  f"Foundry traffic is not counted twice.")
    elif has_bronze_in(_lh, "bronze_azure_ai_metrics"):
        foundry_parts.append(_conform_metrics(_read(_lh, "bronze_azure_ai_metrics")))

if not foundry_parts:
    raise AnalysisException(
        "no Foundry source: neither bronze_foundry_gateway nor "
        "bronze_azure_ai_metrics exists in any lakehouse")
silver_foundry = foundry_parts[0]
for p in foundry_parts[1:]:
    silver_foundry = silver_foundry.unionByName(p)
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
# FOCUS 1.0 is preferred over the legacy per-meter extract when present. It is
# the same invoice, but richer: billed / effective / list / contracted cost as
# separate measures, commitment-discount attribution, region, sub-account and
# tags — and it is a vendor-neutral spec, so an AWS/GCP export would conform
# through this same branch. bronze_azure_cost_focus is already de-duplicated by
# bronze/03_ingest_azure_costmgmt_focus.py (period-level, newest run wins), so
# the generic dedupe()/grain_guard() pair is deliberately NOT applied here:
# their natural key would collapse legitimately distinct FOCUS charge lines.
if has_bronze("bronze_azure_cost_focus"):
    fo = bronze("bronze_azure_cost_focus")

    # FOCUS states the workload class directly, so the service taxonomy comes
    # from the spec rather than from string-matching ARM resource ids.
    svc_cat = F.coalesce(F.col("ServiceCategory"), F.lit(""))
    svc_name = F.lower(F.coalesce(F.col("ServiceName"), F.lit("")))
    is_ai = ((svc_cat == "AI and Machine Learning")
             | svc_name.contains("openai")
             | svc_name.contains("cognitive")
             | svc_name.contains("machine learning"))

    # Tags arrive as a JSON string; an application tag is the only honest way to
    # attribute a billing row to an app. No tag -> empty -> APP-UNKNOWN in gold.
    tags = F.coalesce(F.col("Tags"), F.lit(""))
    app_from_tag = F.coalesce(
        F.get_json_object(tags, "$.application"),
        F.get_json_object(tags, "$.Application"),
        F.get_json_object(tags, "$.app"),
        F.get_json_object(tags, "$.App"),
        F.lit(""))

    # focus_col tolerates the 1.0 -> 1.2-preview renames (see its docstring): the
    # x_...InUsd variant when present (1.0), else the standard measure (1.2). No
    # default literal here, so a genuinely absent value stays NULL (see below).
    billed_usd = focus_col(fo, "x_BilledCostInUsd", "BilledCost").cast("double")
    list_usd = focus_col(fo, "x_ListCostInUsd", "ListCost").cast("double")

    # ----- double-count guard: Copilot Credits bill as ONE Azure service --------
    # Microsoft bills Microsoft 365 Copilot Cowork, Copilot Studio AND Work IQ
    # consumption through a SINGLE Azure service labelled "Microsoft Copilot
    # Studio" — all three draw down the same Copilot Credits meter. Those credits
    # are ALREADY in the model from the M365 Copilot credit export
    # (silver_usage_m365_cowork / silver_usage_m365) and the Studio credit export
    # (silver_usage_studio). Summing the Azure line for that service on top counts
    # the very same dollars twice. This is the credit-feed twin of the "never sum
    # a cumulative series" trap the grain guard defends against, and just as
    # silent — so it is called out loudly and the service is EXCLUDED here rather
    # than left to inflate spend unnoticed. (Prepaid capacity-pack draw-down is
    # additionally invisible in Azure Cost Management, so the M365 credit feed is
    # the authoritative source for this consumption, not the Azure line.)
    copilot_credit_svc = svc_name.contains("copilot studio")
    overlap = (fo.filter(copilot_credit_svc).agg(F.sum(billed_usd)).collect()[0][0]) or 0.0
    if overlap:
        print(f"  !! double-count guard: excluding ${overlap:,.2f} of 'Microsoft "
              f"Copilot Studio' Azure spend from silver_usage_azure — those Copilot "
              f"Credits are already counted via the M365 Cowork and Copilot Studio "
              f"credit feeds. Counting the Azure line too double-counts the same "
              f"dollars.")
    fo = fo.filter(~copilot_credit_svc)

    silver_azure = conform(fo
        .withColumn("usage_date", F.to_date("ChargePeriodStart"))
        .withColumn("platform_key", F.when(is_ai, F.lit("AzureAI"))
                                     .otherwise(F.lit("AzureInfra")))
        # Billing data is resource-scoped, never user-scoped. Claiming an
        # identity here would be an invention; 'unknown' routes to the
        # Unattributed member, which is the honest answer and is exactly the
        # attribution gap this accelerator is meant to make visible.
        .withColumn("identity_key", F.lit("unknown"))
        # Meter names are not models; using them here would pollute dim_model
        # with hundreds of billing SKUs.
        .withColumn("model_key", F.lit(""))
        .withColumn("cost_center_key", focus_col(fo, "x_CostCenter", default=""))
        .withColumn("unit_type", F.lit("azure_meter"))
        .withColumn("quantity", F.col("ConsumedQuantity").cast("double"))
        .withColumn("cost_usd", billed_usd)
        .withColumn("cost_is_estimated", F.lit(False))
        # FOCUS carries the invoice ListCost, so an Azure row has a genuine rate-
        # card / list price without any modelled rate-card row of its own — but
        # ONLY when ListCost is actually populated. Purchases, taxes and some
        # commitment lines carry a null ListCost; those must read has_rate_card =
        # FALSE with a null list_cost_usd so the unified coalesce falls it back to
        # cost_usd. Hardcoding TRUE + a 0.0 default would leave list_cost_usd = 0
        # (not null, so the fallback never fires): page 4's Rate Card Cost bar
        # would be short by exactly those rows while coverage still read 100%.
        .withColumn("list_cost_usd", list_usd)
        .withColumn("has_rate_card", list_usd.isNotNull())
        .withColumn("application_key", app_from_tag)
        .withColumn("_data_class", F.lit("REAL")))
    write_silver(silver_azure, "silver_usage_azure")

    # The unified fact can only carry one cost column. FOCUS's other three cost
    # measures are the whole point of the spec (list vs contracted vs effective
    # is the discount story), so they get their own curated detail table instead
    # of being thrown away at the conform() boundary.
    write_silver(fo
        .withColumn("usage_date", F.to_date("ChargePeriodStart"))
        .withColumn("platform_key", F.when(is_ai, F.lit("AzureAI"))
                                     .otherwise(F.lit("AzureInfra")))
        .select(
            "usage_date", "platform_key",
            F.coalesce(F.col("SubAccountName"), F.lit("")).alias("subscription_name"),
            focus_col(fo, "x_ResourceGroupName", "ResourceGroupName", default="").alias("resource_group"),
            F.coalesce(F.col("ResourceName"), F.lit("")).alias("resource_name"),
            F.coalesce(F.col("ResourceType"), F.lit("")).alias("resource_type"),
            F.coalesce(F.col("ServiceName"), F.lit("")).alias("service_name"),
            F.coalesce(F.col("ServiceCategory"), F.lit("")).alias("service_category"),
            F.coalesce(F.col("RegionName"), F.lit("")).alias("region_name"),
            # x_SkuMeterName (FOCUS 1.0) was renamed SkuMeter in 1.2-preview.
            focus_col(fo, "x_SkuMeterName", "SkuMeter", default="").alias("meter_name"),
            F.coalesce(F.col("PricingCategory"), F.lit("")).alias("pricing_category"),
            F.coalesce(F.col("CommitmentDiscountType"), F.lit("")).alias("commitment_type"),
            F.col("ConsumedQuantity").cast("double").alias("consumed_quantity"),
            F.coalesce(F.col("ConsumedUnit"), F.lit("")).alias("consumed_unit"),
            billed_usd.alias("billed_usd"),
            focus_col(fo, "x_EffectiveCostInUsd", "EffectiveCost").cast("double").alias("effective_usd"),
            list_usd.alias("list_usd"),
            focus_col(fo, "x_ContractedCostInUsd", "ContractedCost").cast("double").alias("contracted_usd"),
            app_from_tag.alias("application_key"),
            F.lit("REAL").alias("_data_class"),
        ), "silver_azure_cost_detail")

elif has_bronze("bronze_azure_cost"):
    # Legacy fallback: the pre-FOCUS per-meter extract.
    az = bronze("bronze_azure_cost")
    az = dedupe(az, "bronze_azure_cost")
    az = grain_guard(az, "azure_cost", "usage_date", ["resource_id"], "cost_usd")
    ai_svc = F.lower(F.coalesce(F.col("consumed_service"), F.lit("")))
    is_ai = (ai_svc.contains("cognitiveservices")
             | ai_svc.contains("machinelearningservices")
             | F.lower(F.coalesce(F.col("resource_id"), F.lit("")))
                .contains("/microsoft.cognitiveservices/"))
    # Same double-count guard as the FOCUS branch: Cowork + Copilot Studio +
    # Work IQ credits bill under the single Azure service "Microsoft Copilot
    # Studio", which the M365 Cowork and Studio credit feeds already count. The
    # guard is MANDATORY on every Azure path, not just FOCUS — a pre-FOCUS tenant
    # would otherwise double-count exactly the same dollars, silently.
    copilot_credit_svc = ai_svc.contains("copilot studio")
    overlap = (az.filter(copilot_credit_svc)
                 .agg(F.sum(F.col("cost_usd").cast("double"))).collect()[0][0]) or 0.0
    if overlap:
        print(f"  !! double-count guard: excluding ${overlap:,.2f} of 'Microsoft "
              f"Copilot Studio' Azure spend from silver_usage_azure — those Copilot "
              f"Credits are already counted via the M365 Cowork and Copilot Studio "
              f"credit feeds. Counting the Azure line too double-counts the same "
              f"dollars.")
    az = az.filter(~copilot_credit_svc)
    silver_azure = conform(az
        .withColumn("platform_key", F.when(is_ai, F.lit("AzureAI"))
                                     .otherwise(F.lit("AzureInfra")))
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
usage_parts = [silver_m365, silver_cowork, silver_ghc, silver_studio, silver_foundry]
if silver_azure is not None:
    usage_parts.append(silver_azure)

all_usage = usage_parts[0]
for p in usage_parts[1:]:
    all_usage = all_usage.unionByName(p)
all_usage = (all_usage.filter(F.col("usage_date").isNotNull())
             .withColumn("model_key",
                         F.when((F.col("model_key").isNull())
                                | (F.col("model_key") == ""), F.lit(NA_MODEL))
                          .otherwise(F.col("model_key")))
             # A row a rate card never priced has list_cost_usd = null and
             # has_rate_card = FALSE; fall the list cost back to the billed cost
             # so the column is never null and the report never shows the two as
             # independent figures for such a row.
             .withColumn("list_cost_usd",
                         F.coalesce(F.col("list_cost_usd"), F.col("cost_usd")))
             .withColumn("has_rate_card",
                         F.coalesce(F.col("has_rate_card"), F.lit(False)))
             .cache())

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
