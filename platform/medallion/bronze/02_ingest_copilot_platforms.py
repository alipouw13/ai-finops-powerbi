# Fabric notebook — BRONZE: Copilot Platforms (M365, Copilot Studio, GitHub Copilot)
# ---------------------------------------------------------------------------
# Lands all Copilot platform feeds into Bronze Delta tables:
#   - bronze_m365_copilot_seats     (Graph /subscribedSkus + /users?$select=assignedLicenses)
#   - bronze_m365_copilot_usage     (Graph /reports/getMicrosoft365CopilotUsageUserDetail)
#   - bronze_m365_copilot_credits   (M365 Admin Cost Mgmt: Cowork, Autopilot, Work IQ API)
#   - bronze_studio_credits         (Dataverse msdyn_aievents + PPAC capacity)
#   - bronze_ghc_seats              (GitHub REST /orgs/{org}/copilot/billing/seats)
#   - bronze_ghc_premium_usage      (GitHub REST /enterprises/{ent}/settings/billing/usage)
#   - bronze_ghc_metrics            (GitHub REST /orgs/{org}/copilot/metrics)
# ---------------------------------------------------------------------------
from pyspark.sql import functions as F

LAKE = "abfss://finops@onelake.dfs.fabric.microsoft.com/LH_tokenomics_bronze.Lakehouse/Files/bronze"


def land_csv(table_name: str, source_api: str, mode="append"):
    df = (
        spark.read.option("header", "true")
        .option("multiLine", "true")
        .option("escape", '"')
        .csv(f"{LAKE}/{table_name}.csv")
        .withColumn("_ingest_ts", F.current_timestamp())
        .withColumn("_source_api", F.lit(source_api))
    )
    (
        df.write.format("delta")
        .mode(mode)
        .option("mergeSchema", "true")
        .saveAsTable(f"finops_bronze.{table_name}")
    )
    print(f"bronze {mode}: finops_bronze.{table_name} ({df.count()} rows)")


land_csv("bronze_m365_copilot_seats",   "graph.subscribedSkus+users.assignedLicenses")
land_csv("bronze_m365_copilot_usage",   "graph.getMicrosoft365CopilotUsageUserDetail")
land_csv("bronze_m365_copilot_credits", "m365.admin.costmanagement")
land_csv("bronze_studio_credits",       "dataverse.msdyn_aievents+ppac.capacity")
land_csv("bronze_ghc_seats",            "github.orgs.copilot.billing.seats")
land_csv("bronze_ghc_premium_usage",    "github.enterprises.settings.billing.usage")
land_csv("bronze_ghc_metrics",          "github.orgs.copilot.metrics")

