# Fabric notebook — BRONZE: SaaS AI platforms (Dataverse, M365, GitHub)
# ---------------------------------------------------------------------------
# The three platforms whose usage never appears in an Azure resource. Each one
# lands with its own API's field names, untouched:
#
#   Copilot Studio -> Dataverse msdyn_aievent (per-environment, OData)
#                     msdyn_creditconsumed is ALREADY net of zero-rating, so it
#                     is used directly and never recomputed from action rates.
#                     Lookups arrive as _msdyn_botid_value with a separate
#                     @OData...FormattedValue column — both are preserved.
#   M365 Copilot   -> Graph getMicrosoft365CopilotUsageUserDetail (last-activity
#                     DATES, never counts or tokens), subscribedSkus +
#                     assignedLicenses for seats, and the Copilot Credits meter.
#   GitHub Copilot -> /orgs/{org}/copilot/billing/seats and the enhanced billing
#                     usage API, whose netAmount is the billed overage in USD.
#
# Going-live requirements (see README "Going live"):
#   Copilot Studio -> Dataverse app registration + environment URL
#   M365 Copilot   -> Reports.Read.All (application) + admin consent, and
#                     "Display concealed user names" DISABLED or UPNs arrive
#                     hashed and per-user attribution is impossible
#   GitHub Copilot -> manage_billing:copilot; premium-request USD needs
#                     Enterprise Cloud + a classic PAT with admin:enterprise
#
# Provenance stays a COLUMN (_data_class, dim_platform.data_source), never a
# code branch, so swapping a mock reader for the live API changes nothing
# downstream.
# ---------------------------------------------------------------------------
from pyspark.sql import functions as F

LAKE = "abfss://finops@<lake>.dfs.core.windows.net"
EXTRACT_ID = spark.conf.get("finops.extract_id", "manual")


def land(df, table, source_system, source_api, watermark_col, mode="append"):
    out = (df
           .withColumn("_ingested_at", F.current_timestamp())
           .withColumn("_source_system", F.lit(source_system))
           .withColumn("_source_api", F.lit(source_api))
           .withColumn("_watermark", F.col(watermark_col).substr(1, 10))
           .withColumn("_batch_id", F.lit(EXTRACT_ID))
           .withColumn("_data_class", F.lit("REAL")))
    (out.write.format("delta").mode(mode)
        .option("mergeSchema", "true")
        .saveAsTable(f"finops_bronze.{table}"))
    print(f"bronze {mode}: finops_bronze.{table} ({out.count()} rows)")


# --- Copilot Studio ---------------------------------------------------------
# Query per environment; msdyn_aievent is NOT tenant-wide. Exclude
# bring-your-own-model rows so Foundry spend is not counted twice.
aievent = spark.read.option("multiLine", True).json(f"{LAKE}/landing/dataverse/msdyn_aievent/*.json")
land(aievent, "dataverse_msdyn_aievent", "dataverse", "dataverse.msdyn_aievents",
     "msdyn_eventtimestamp")

# --- Microsoft 365 Copilot --------------------------------------------------
usage = spark.read.option("header", True).csv(f"{LAKE}/landing/m365/copilot_usage/*.csv")
land(usage, "m365_copilot_usage", "graph",
     "graph.reports.getMicrosoft365CopilotUsageUserDetail", "Report Refresh Date")

seats = spark.read.option("multiLine", True).json(f"{LAKE}/landing/m365/seats/*.json")
land(seats, "m365_copilot_seats", "graph",
     "graph.subscribedSkus+users.assignedLicenses", "snapshotDate")

credits = spark.read.option("multiLine", True).json(f"{LAKE}/landing/m365/credits/*.json")
land(credits, "m365_copilot_credits", "m365billing", "m365.billing.copilotCredits", "usageDate")

# --- GitHub Copilot ---------------------------------------------------------
gh_seats = spark.read.option("multiLine", True).json(f"{LAKE}/landing/github/seats/*.json")
land(gh_seats, "ghc_seats", "github", "github.orgs.copilot.billing.seats", "snapshot_date")

gh_usage = spark.read.option("multiLine", True).json(f"{LAKE}/landing/github/usage/*.json")
land(gh_usage, "ghc_premium_usage", "github", "github.settings.billing.usage", "date")

# --- Customer reference inputs ---------------------------------------------
# Master data, not API extracts: identity map, application registry (including
# the gateway client id and Azure resource that represent each app), business
# hierarchy, agent registry, and the rate card. Current-state snapshots, so
# overwrite is correct.
for name, path in [
    ("ref_identity_map", "reference/identity_map/*.json"),
    ("ref_app_inventory", "reference/app_inventory/*.json"),
    ("ref_business_hierarchy", "reference/business_hierarchy/*.json"),
    ("ref_agent_inventory", "reference/agent_inventory/*.json"),
    ("ref_rate_card", "reference/rate_card/*.json"),
]:
    df = spark.read.option("multiLine", True).json(f"{LAKE}/landing/{path}")
    (df.withColumn("_ingested_at", F.current_timestamp())
       .withColumn("_source_system", F.lit("customer-reference"))
       .withColumn("_data_class", F.lit("REFERENCE"))
       .write.format("delta").mode("overwrite").option("overwriteSchema", "true")
       .saveAsTable(f"finops_bronze.{name}"))
    print(f"bronze snapshot: finops_bronze.{name}")
