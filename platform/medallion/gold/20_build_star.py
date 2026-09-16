# Fabric notebook — GOLD: emit the star the semantic model consumes
# ---------------------------------------------------------------------------
# RUNNABLE. Reads the curated SILVER entities and writes the full 11-table star
# into the gold lakehouse.
#
# Gold reads Silver, never Bronze. An earlier version built its dimensions
# straight from the bronze reference tables, which bypassed the curation layer:
# identity resolution and de-duplication then had to be repeated here, and the
# two implementations could drift. Silver now owns conforming and resolving;
# Gold owns the dimensional model, the sentinel members and referential
# integrity.
#
#   silver_usage_unified      -> fact_ai_usage
#   silver_identity_resolved  -> dim_identity
#   silver_org_hierarchy      -> dim_business_unit
#   silver_application_map    -> dim_application
#   silver_model_map          -> dim_model
#   silver_rate_card          -> dim_rate_card
#
# The column contract below MUST stay identical to the CSV headers in
# AIFinOps.SemanticModel/data/, otherwise the TMDL partitions break. Run
# platform/validate/validate_pbip.py after changing either side.
#
# Emits: fact_ai_usage + dim_date/platform/identity/model/cost_center/rate_card
#        + dim_business_unit/application/environment + extractable_data_catalog
#
# Parameters (WS_ID / BRONZE_ID / SILVER_ID / GOLD_ID) are injected by
# platform/deploy/fabric_deploy.py at import time.
#
# The original design scaffold is kept as 20_build_star.design.py.
# ---------------------------------------------------------------------------
from pyspark.sql import functions as F
from pyspark.sql.window import Window

ONELAKE = "abfss://{ws}@onelake.dfs.fabric.microsoft.com/{lh}/Tables/dbo/{tbl}"

BRONZE_REAL = globals().get("BRONZE_REAL_ID") or ""


def read(lh, tbl):
    return spark.read.format("delta").load(ONELAKE.format(ws=WS_ID, lh=lh, tbl=tbl))


def silver(tbl):
    return read(SILVER_ID, tbl)


def read_bronze(tbl):
    """Bronze fallback, used only for provenance inspection and the catalog.

    dim_platform[data_source] is derived by reading _data_class off the bronze
    feed a platform came from, which is deliberately a Bronze-level question:
    it asks "what kind of data was ingested", not "what did we curate".
    """
    dfs = []
    for lh in (BRONZE_ID, BRONZE_REAL):
        if not lh:
            continue
        try:
            dfs.append(read(lh, tbl))
        except Exception:                                         # noqa: BLE001
            pass
    if not dfs:
        raise ValueError(f"bronze table {tbl} not found in any bronze lakehouse")
    out = dfs[0]
    for d in dfs[1:]:
        out = out.unionByName(d, allowMissingColumns=True)
    return out


# Every gold table must expose exactly the columns the semantic model declares.
# A Direct Lake model refuses to frame with "Delta protocol violation: the column
# X is not found in delta table Y" if anything is missing, so assert it here
# where the error names the table and the column.
GOLD_CONTRACT = {
    "dim_application": ["application_key", "application_name", "application_type",
                        "owner_business_unit_key", "owner_upn",
                        "default_environment_key", "criticality", "is_mock"],
    "dim_business_unit": ["business_unit_key", "business_unit_name", "division",
                          "monthly_budget_usd", "executive_owner", "is_mock_budget"],
    "dim_cost_center": ["cost_center_key", "cost_center_name", "business_unit",
                        "owner_upn"],
    "dim_date": ["date_key", "year", "quarter", "month", "month_name", "day",
                 "day_name", "is_weekday", "year_month"],
    "dim_environment": ["environment_key", "environment_name", "is_production",
                        "sla_tier"],
    "dim_identity": ["identity_key", "display_name", "principal_type", "upn",
                     "github_login", "team", "business_unit", "cost_center_key",
                     "identity_class", "is_human", "home_business_unit_key"],
    "dim_model": ["model_key", "model_name", "model_version", "provider", "modality"],
    "dim_platform": ["platform_key", "platform_name", "billing_model", "native_unit",
                     "has_token_telemetry", "has_native_cost", "is_variable_cost",
                     "data_source", "enterprise_discount_pct", "addon_unit",
                     "addon_billing_model"],
    "dim_rate_card": ["rate_key", "platform", "unit_type", "model", "unit_price_usd",
                      "effective_from", "currency", "source", "note"],
    "extractable_data_catalog": ["platform", "signal_category", "signal", "source_api",
                                 "grain", "identity_granularity", "cost_fidelity",
                                 "retention", "availability", "notes"],
    "fact_ai_usage": ["usage_date", "platform_key", "identity_key", "model_key",
                      "cost_center_key", "unit_type", "quantity", "input_tokens",
                      "output_tokens", "cached_tokens", "requests", "cost_usd",
                      "list_cost_usd", "cost_is_estimated", "has_rate_card",
                      "is_error", "latency_ms", "application_key",
                      "environment_key", "business_unit_key"],
}

# The one-side key of every relationship must be unique. Power BI rejects the
# whole relationship otherwise, and it only surfaces when a visual runs, as
# "contains a duplicate value and this is not allowed".
DIM_KEYS = {
    "dim_application": "application_key",
    "dim_business_unit": "business_unit_key",
    "dim_cost_center": "cost_center_key",
    "dim_date": "date_key",
    "dim_environment": "environment_key",
    "dim_identity": "identity_key",
    "dim_model": "model_key",
    "dim_platform": "platform_key",
}


def write_gold(df, tbl):
    want = GOLD_CONTRACT.get(tbl)
    if want:
        missing = [c for c in want if c not in df.columns]
        extra = [c for c in df.columns if c not in want]
        if missing:
            raise SystemExit(
                f"gold.{tbl} is missing column(s) {missing} that the semantic model "
                f"declares — Direct Lake would fail to frame. Got: {df.columns}")
        if extra:
            print(f"    . {tbl}: dropping unmapped column(s) {extra}")
        df = df.select(*want)
    key = DIM_KEYS.get(tbl)
    if key:
        n, distinct = df.count(), df.select(key).distinct().count()
        if n != distinct:
            dupes = (df.groupBy(key).count().filter(F.col("count") > 1)
                     .limit(5).collect())
            raise SystemExit(
                f"gold.{tbl}[{key}] is not unique ({n} rows, {distinct} distinct) — "
                f"Power BI rejects the relationship and every visual using this "
                f"dimension fails. Duplicates: {[r[0] for r in dupes]}")
    path = ONELAKE.format(ws=WS_ID, lh=GOLD_ID, tbl=tbl)
    (df.write.format("delta").mode("overwrite")
       .option("overwriteSchema", "true").save(path))
    print(f"  + gold.{tbl:26} {df.count():7,} rows")


def row_like(df, **values):
    """A 1-row DataFrame matching df's schema exactly.

    Building extra members with a literal tuple + column-name list lets Spark
    re-infer types, so a bigint budget column meets an int literal and the union
    fails. Casting to the parent schema keeps unions total.
    """
    cols = [F.lit(values.get(f.name)).cast(f.dataType).alias(f.name)
            for f in df.schema.fields]
    return spark.range(1).select(*cols)


fact = silver("silver_usage_unified").cache()

# ------------------------------------------------------------- dim_identity
# Silver already resolved the identity graph (github_login <-> UPN <-> service
# principal <-> agent) and folded agents in with their owning BU, so Gold only
# has to shape it and add the sentinel member.
dim_identity = silver("silver_identity_resolved").select(
    "identity_key", "display_name", "principal_type", "upn", "github_login",
    "team", F.col("home_business_unit_key").alias("business_unit"),
    "cost_center_key", "identity_class", "is_human", "home_business_unit_key")

# Any key silver could not resolve ('unknown', resource-level telemetry) gets an
# explicit Unattributed member rather than silently landing on Power BI's
# auto-generated blank row, where the spend would detach from every slicer.
orphans = (fact.select("identity_key").distinct()
           .join(dim_identity.select("identity_key"), "identity_key", "left_anti"))
unattributed = (orphans
    .withColumn("display_name",
                F.when(F.col("identity_key") == "unknown",
                       F.lit("Unattributed Identity"))
                 .otherwise(F.concat(F.lit("Agent "), F.col("identity_key"))))
    .withColumn("principal_type", F.when(F.col("identity_key") == "unknown",
                                         F.lit("Unknown")).otherwise(F.lit("Agent")))
    .withColumn("upn", F.lit("")).withColumn("github_login", F.lit(""))
    .withColumn("team", F.lit("")).withColumn("cost_center_key", F.lit(""))
    .withColumn("identity_class", F.when(F.col("identity_key") == "unknown",
                                         F.lit("Unknown")).otherwise(F.lit("Agent")))
    .withColumn("is_human", F.lit("FALSE"))
    .withColumn("home_business_unit_key", F.lit("BU-UNALLOC"))
    .withColumn("business_unit", F.col("home_business_unit_key")))
# Force the union side onto the parent's exact types.
unattributed = unattributed.select(
    *[F.col(f.name).cast(f.dataType).alias(f.name) for f in dim_identity.schema.fields])
dim_identity = dim_identity.unionByName(unattributed).cache()
write_gold(dim_identity, "dim_identity")

# -------------------------------------------------------- dim_business_unit
dim_bu = (silver("silver_org_hierarchy")
          .withColumn("is_mock_budget", F.lit("TRUE"))
          .select("business_unit_key", "business_unit_name", "division",
                  F.col("monthly_budget_usd").cast("long").alias("monthly_budget_usd"),
                  F.col("executive_owner"), "is_mock_budget"))
if dim_bu.filter(F.col("business_unit_key") == "BU-UNALLOC").count() == 0:
    dim_bu = dim_bu.unionByName(row_like(
        dim_bu, business_unit_key="BU-UNALLOC", business_unit_name="Unallocated",
        division="Unallocated", monthly_budget_usd=0, executive_owner="",
        is_mock_budget="TRUE"))
write_gold(dim_bu, "dim_business_unit")

# ---------------------------------------------------------- dim_application
dim_app = (silver("silver_application_map")
    .withColumn("is_mock", F.when(F.col("_data_class") == "REAL", F.lit("FALSE"))
                            .otherwise(F.lit("TRUE")))
    .select("application_key", "application_name", "application_type",
            F.col("owner_business_unit_key"), F.col("owner_upn"),
            "default_environment_key", "criticality", "is_mock"))
# Only add the catch-all if the source inventory does not already carry one.
# Appending unconditionally produced a duplicate application_key, which makes
# Power BI reject the whole relationship ("contains a duplicate value ... and
# this is not allowed for columns on the one side of a relationship").
if dim_app.filter(F.col("application_key") == "APP-UNKNOWN").count() == 0:
    dim_app = dim_app.unionByName(row_like(
        dim_app, application_key="APP-UNKNOWN", application_name="Unattributed Workload",
        application_type="unattributed", owner_business_unit_key="BU-UNALLOC", owner_upn="",
        default_environment_key="ENV-UNK", criticality="Unassigned", is_mock="FALSE"))
write_gold(dim_app, "dim_application")

# ---------------------------------------------------------- dim_environment
# Columns and keys must match AIFinOps.SemanticModel/data/dim_environment.csv:
# a Direct Lake model fails to frame with "Delta protocol violation: the column
# X is not found" if the Delta table is missing anything the TMDL declares.
dim_env = spark.createDataFrame(
    [("ENV-PROD", "Production", "TRUE", "Tier-1"),
     ("ENV-TEST", "Test", "FALSE", "Tier-3"),
     ("ENV-DEV", "Development", "FALSE", "Tier-3"),
     ("ENV-UNK", "Unknown", "FALSE", "")],
    ["environment_key", "environment_name", "is_production", "sla_tier"])
write_gold(dim_env, "dim_environment")

# ---------------------------------------------------------- dim_cost_center
# Same sentinel reasoning as dim_model: agent and resource-level rows carry no
# cost centre, and an empty-string key would be read as BLANK and never match.
NA_CC = "CC-NA"
dim_cc = (dim_identity
    .filter((F.col("cost_center_key").isNotNull()) & (F.col("cost_center_key") != ""))
    .groupBy("cost_center_key")
    .agg(F.first("team").alias("cost_center_name"),
         F.first("business_unit").alias("business_unit"),
         F.lit("").alias("owner_upn"))
    .select("cost_center_key", "cost_center_name", "business_unit", "owner_upn"))
# Real billing rows carry their own cost centre (Azure's costCenter tag), which
# no identity in the directory knows about. Without this the whole real spend
# would be forced onto CC-NA and the chargeback page would show nothing.
fact_ccs = (fact.select(F.col("cost_center_key")).distinct()
            .filter(F.col("cost_center_key").isNotNull()
                    & (F.col("cost_center_key") != ""))
            .join(dim_cc.select("cost_center_key"), "cost_center_key", "left_anti")
            .withColumn("cost_center_name", F.col("cost_center_key"))
            .withColumn("business_unit", F.lit("BU-UNALLOC"))
            .withColumn("owner_upn", F.lit(""))
            .select("cost_center_key", "cost_center_name", "business_unit", "owner_upn"))
dim_cc = dim_cc.unionByName(fact_ccs)
dim_cc = dim_cc.unionByName(row_like(
    dim_cc, cost_center_key=NA_CC, cost_center_name="(no cost centre)",
    business_unit="BU-UNALLOC", owner_upn=""))
write_gold(dim_cc, "dim_cost_center")

# --------------------------------------------------------------- dim_platform
# Provenance is DERIVED from the bronze lineage column, never hardcoded.
# gen_bronze_data.py stamps _data_class='MOCK' on synthetic rows;
# extract_m365_graph.py stamps 'REAL' on live Microsoft Graph extracts. Reading
# it here means the label always follows the data: swap a mock feed for a real
# collector and the Governance page flips to REAL on the next run, with no code
# change. Hardcoding "REAL - Azure Monitor metrics" here previously relabelled
# synthetic spend as real, which is precisely what this accelerator exists to
# avoid.
# Which Azure billing feed is actually in play. This mirrors the preference
# silver applies (FOCUS wins when present), so the Governance page can never
# name a source that did not produce the numbers on screen.
def _azure_feed():
    try:
        read_bronze("bronze_azure_cost_focus")
        return ("bronze_azure_cost_focus",
                "Cost Management FOCUS 1.2-preview export via OneLake shortcut")
    except Exception:                                             # noqa: BLE001
        return ("bronze_azure_cost", "Consumption usageDetails")

AZURE_TBL, AZURE_NOTE = _azure_feed()


# Foundry mirrors silver's prefer-the-gateway rule: when the APIM AI Gateway feed
# is present it is the source (per-user token attribution), otherwise Azure
# Monitor metrics are (resource-grain, no identity). The Governance page must
# never name a source that did not produce the numbers on screen.
def _foundry_feed():
    try:
        read_bronze("bronze_foundry_gateway")
        return ("bronze_foundry_gateway",
                "APIM AI Gateway / Log Analytics (per-user token attribution)")
    except Exception:                                             # noqa: BLE001
        return ("bronze_azure_ai_metrics",
                "Azure Monitor metrics (resource-grain, no identity)")

FOUNDRY_TBL, FOUNDRY_NOTE = _foundry_feed()

PLATFORM_SOURCE_TABLE = {
    "Foundry": FOUNDRY_TBL,
    "M365Copilot": "bronze_m365_copilot_seats",
    "GitHubCopilot": "bronze_ghc_seats",
    "CopilotStudio": "bronze_studio_credits",
    "AzureAI": AZURE_TBL,
    "AzureInfra": AZURE_TBL,
}
PLATFORM_NOTE = {
    "Foundry": FOUNDRY_NOTE,
    "M365Copilot": "Graph seats + Cowork Copilot Credits (add-on modelled from the rate card)",
    "GitHubCopilot": "GitHub billing API (needs classic PAT)",
    "CopilotStudio": "Dataverse msdyn_aievent - connected, tenant has zero consumption",
    "AzureAI": f"{AZURE_NOTE} - invoiced AI service spend",
    "AzureInfra": f"{AZURE_NOTE} - invoiced supporting infrastructure",
}


def provenance(platform_key):
    tbl = PLATFORM_SOURCE_TABLE[platform_key]
    note = PLATFORM_NOTE[platform_key]
    try:
        df = read_bronze(tbl)
        if "_data_class" in df.columns:
            classes = sorted({r[0] for r in df.select("_data_class").distinct().collect()
                              if r[0]})
            label = "/".join(classes) if classes else "UNKNOWN"
        else:
            label = "UNKNOWN"
    except Exception:                                             # noqa: BLE001
        label = "UNKNOWN"
    return f"{label} - {note}"


# addon_unit / addon_billing_model describe a consumptive add-on billed ON TOP of
# the platform's native unit. Only Microsoft 365 Copilot has one today: the
# Cowork add-on, billed in Copilot Credits (there is no "Cowork unit"). Every
# other platform carries empty strings, not null, so the Direct Lake column is
# never BLANK in a slicer.
platform_rows = [
    ("Foundry", "Azure AI Foundry", "Consumption (tokens)", "token", "TRUE", "TRUE",
     "TRUE", provenance("Foundry"), 0.15, "", ""),
    ("GitHubCopilot", "GitHub Copilot Enterprise", "Seats + premium requests",
     "premium_request", "FALSE", "TRUE", "TRUE", provenance("GitHubCopilot"), 0.05, "", ""),
    ("CopilotStudio", "Microsoft Copilot Studio", "Copilot Credits", "copilot_credit",
     "FALSE", "FALSE", "TRUE", provenance("CopilotStudio"), 0.20, "", ""),
    ("M365Copilot", "Microsoft 365 Copilot", "Per-seat licence", "seat_day",
     "FALSE", "FALSE", "FALSE", provenance("M365Copilot"), 0.0,
     "copilot_credit", "Usage-based (Copilot Credits) - Cowork"),
]
# Only declare the Azure billing platforms when real cost is actually present.
# Emitting them unconditionally would leave two empty members in every slicer.
if BRONZE_REAL:
    platform_rows += [
        ("AzureAI", "Azure AI Services (invoiced)", "Consumption (Azure meters)",
         "azure_meter", "FALSE", "TRUE", "TRUE", provenance("AzureAI"), 0.0, "", ""),
        ("AzureInfra", "Azure AI Supporting Infrastructure",
         "Consumption (Azure meters)", "azure_meter", "FALSE", "TRUE", "TRUE",
         provenance("AzureInfra"), 0.0, "", ""),
    ]

dim_platform = spark.createDataFrame(platform_rows, [
    "platform_key", "platform_name", "billing_model", "native_unit",
    "has_token_telemetry", "has_native_cost", "is_variable_cost", "data_source",
    "enterprise_discount_pct", "addon_unit", "addon_billing_model"])
write_gold(dim_platform, "dim_platform")

# ------------------------------------------------------------------ dim_model
# Silver canonicalised the model names and already applied the MODEL-NA
# sentinel, so Gold just binds it.
dim_model = silver("silver_model_map").select(
    "model_key", "model_name", "model_version", "provider", "modality")
write_gold(dim_model, "dim_model")

# -------------------------------------------------------------- dim_rate_card
# Cast to the types the semantic model declares, so a Direct Lake model binds
# without a type mismatch on the Delta column.
write_gold(silver("silver_rate_card")
           .select("rate_key", "platform", "unit_type", "model",
                   F.col("unit_price_usd").cast("double").alias("unit_price_usd"),
                   F.col("effective_from").cast("date").alias("effective_from"),
                   "currency", "source", "note"), "dim_rate_card")

# ------------------------------------------------------------------- dim_date
bounds = fact.selectExpr("min(usage_date) lo", "max(usage_date) hi").collect()[0]
if bounds["lo"] is None:
    raise SystemExit("fact has no usable usage_date — check the silver layer")
dim_date = (spark.sql(
        f"SELECT explode(sequence(to_date('{bounds['lo']}'), to_date('{bounds['hi']}'),"
        f" interval 1 day)) AS date_key")
    .withColumn("year", F.year("date_key").cast("long"))
    .withColumn("quarter", F.concat(F.lit("Q"), F.quarter("date_key")))
    .withColumn("month", F.month("date_key").cast("long"))
    .withColumn("month_name", F.date_format("date_key", "MMMM"))
    .withColumn("day", F.dayofmonth("date_key").cast("long"))
    .withColumn("day_name", F.date_format("date_key", "EEEE"))
    .withColumn("is_weekday", F.dayofweek("date_key").between(2, 6))
    .withColumn("year_month", F.date_format("date_key", "yyyy-MM")))
write_gold(dim_date, "dim_date")

# ------------------------------------------------------ fact_ai_usage (+ keys)
# Silver already resolved business_unit_key and cost_center_key. Gold's job here
# is only to fill the application/environment keys and enforce referential
# integrity against the dimensions it just built.
sp_app = {"CopilotStudio": "APP-STUDIO", "M365Copilot": "APP-M365",
          "GitHubCopilot": "APP-GHCP", "Foundry": "APP-UNKNOWN"}
app_expr = F.lit("APP-UNKNOWN")
for plat, app in sp_app.items():
    app_expr = F.when(F.col("platform_key") == plat, F.lit(app)).otherwise(app_expr)

# A source that genuinely knows its workload beats a per-platform guess. Real
# Azure billing rows name the exact ARM resource, and gateway-fronted Foundry
# rows carry the calling app's client id resolved to an application_key via the
# ownership map in silver — both arrive with application_key already set, so they
# resolve to a real workload instead of Foundry's default APP-UNKNOWN. This is
# what shrinks "Unattributed Workload": the feed genuinely knows the app, rather
# than the dollars being spread around. Everything else falls back to the map.
if "application_key" in fact.columns:
    app_expr = F.when(F.col("application_key").isNotNull()
                      & (F.col("application_key") != ""),
                      F.col("application_key")).otherwise(app_expr)

valid_apps = [r[0] for r in dim_app.select("application_key").collect()]
app_expr = F.when(app_expr.isin(valid_apps), app_expr).otherwise(F.lit("APP-UNKNOWN"))

fact = (fact
    .withColumn("business_unit_key",
                F.coalesce(F.col("business_unit_key"), F.lit("BU-UNALLOC")))
    .withColumn("application_key", app_expr)
    .withColumn("environment_key", F.lit("ENV-PROD")))

# Keep every foreign key inside its dimension, or the relationship silently
# creates a blank member and the spend vanishes from sliced visuals.
valid_bus = [r[0] for r in dim_bu.select("business_unit_key").collect()]
valid_ccs = [r[0] for r in dim_cc.select("cost_center_key").collect()]
fact = (fact
    .withColumn("business_unit_key", F.when(F.col("business_unit_key").isin(valid_bus),
                                            F.col("business_unit_key"))
                                      .otherwise(F.lit("BU-UNALLOC")))
    .withColumn("cost_center_key", F.when(F.col("cost_center_key").isin(valid_ccs),
                                          F.col("cost_center_key"))
                                    .otherwise(F.lit(NA_CC))))

# Column order matches AIFinOps.SemanticModel/data/fact_ai_usage.csv exactly, so
# a CSV export from gold is drop-in for the import model. Verified by
# platform/validate/check_notebooks.py. list_cost_usd sits beside cost_usd and
# has_rate_card beside cost_is_estimated, mirroring the CSV.
CONTRACT = ["usage_date", "platform_key", "identity_key", "model_key",
            "cost_center_key", "unit_type", "quantity", "input_tokens",
            "output_tokens", "cached_tokens", "requests", "cost_usd",
            "list_cost_usd", "cost_is_estimated", "has_rate_card", "is_error",
            "latency_ms", "application_key", "environment_key", "business_unit_key"]
missing = [c for c in CONTRACT if c not in fact.columns]
assert not missing, f"gold fact breaks the CSV column contract: {missing}"
write_gold(fact.select(*CONTRACT), "fact_ai_usage")

# ------------------------------------- dim_data_source (extractable catalog)
# Conformed by silver as silver_data_source_catalog. The semantic model calls it
# dim_data_source; the source CSV is extractable_data_catalog.
try:
    write_gold(silver("silver_data_source_catalog"), "extractable_data_catalog")
except Exception as e:                                            # noqa: BLE001
    print(f"  . extractable_data_catalog skipped — Extractable Data Spectrum page "
          f"will be empty ({str(e)[:70]})")

# ------------------------------------------------------------ referential QA
# Cover EVERY relationship in the model. An earlier version checked only five,
# which let 1,230 orphaned model_key rows and 432 orphaned cost_center_key rows
# through -- each one surfaces as a "(Blank)" member in the slicers and silently
# detaches that spend from the dimension.
print("\nreferential integrity:")
checks = [("dim_identity", dim_identity, "identity_key", "identity_key"),
          ("dim_business_unit", dim_bu, "business_unit_key", "business_unit_key"),
          ("dim_application", dim_app, "application_key", "application_key"),
          ("dim_environment", dim_env, "environment_key", "environment_key"),
          ("dim_platform", dim_platform, "platform_key", "platform_key"),
          ("dim_model", dim_model, "model_key", "model_key"),
          ("dim_cost_center", dim_cc, "cost_center_key", "cost_center_key"),
          ("dim_date", dim_date, "date_key", "usage_date")]
bad = 0
for name, dim, key, fk in checks:
    orph = (fact.select(F.col(fk).alias(key)).distinct()
            .join(dim.select(key), key, "left_anti").count())
    bad += orph
    print(f"  {'OK ' if orph == 0 else 'ORPHAN'} {name:20} {orph} unmatched {fk}")

agg = fact.selectExpr("count(*) n", "sum(cost_usd) c").collect()[0]
billed = (fact.filter(~F.col("cost_is_estimated"))
          .selectExpr("sum(cost_usd) c").collect()[0][0]) or 0.0
total = agg["c"] or 0.0
print(f"\nfact_ai_usage: {agg['n']:,} rows, ${total:,.2f}")
print(f"cost confidence: {billed / (total or 1) * 100:.1f}% billed "
      f"(${billed:,.2f} of ${total:,.2f})")
if bad:
    raise SystemExit(f"{bad} orphaned key(s) — fix before publishing the model")
print("\ngold star complete.")
