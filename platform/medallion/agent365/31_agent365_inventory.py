# Fabric notebook - AGENT 365: Bronze -> Silver -> Gold, RUNNABLE
# ---------------------------------------------------------------------------
# Source: Microsoft 365 admin center > Agents > Registry export (REAL, manual export) and the
# agent "active users" export. Both are point-in-time snapshots. Agent 365 Graph APIs
# (/copilot/admin/catalog/packages) replace this export once the tenant licence has propagated.
#
#   BRONZE  bronze_agent365_agents, bronze_agent365_active_users   as delivered (columns snake_cased)
#   SILVER  silver_agent_inventory   one row per agent: typed, flags, owner_class
#           silver_agent_activity    per-user agent activity (UPN is hashed by the export)
#   GOLD    dim_agent                one row per agent (agent_key = Title ID)
#           fact_agent_inventory     snapshot x agent, additive 0/1 flags; links to dim_date and
#                                    dim_identity (owner), so Governance can cross-reference cost
#
# Not carried past Bronze: agent Instructions, uploaded-file names and sharing lists (free text,
# potentially sensitive, and not needed for FinOps). Cost per agent is NOT in this export; cost
# still comes from FOCUS / Cost Management and is joined by owner or tag.
# The default lakehouse must be the GOLD lakehouse.
# ---------------------------------------------------------------------------
import re
from pyspark.sql import functions as F

WS_ID = globals().get("WS_ID") or "0cb77528-27b4-4298-a48a-477caff105f3"
BRONZE_ID = globals().get("BRONZE_ID") or "03c83faf-ae22-401f-bcc5-d7241ab05a28"
SILVER_ID = globals().get("SILVER_ID") or "597401b8-65f9-4fff-802b-8b5a584fd8cf"
BRONZE_REAL = globals().get("BRONZE_REAL_ID") or "0ac1d742-ef4b-49df-a5fc-4c5adf41370e"
SNAPSHOT_DATE = "2026-10-26"
endpoint = "onelake.dfs.fabric.microsoft.com"


def path(lakehouse, table):
    return f"abfss://{WS_ID}@{endpoint}/{lakehouse}/Tables/dbo/{table}"


def snake(name):
    return re.sub(r"_+", "_", re.sub(r"[^0-9a-z]+", "_", name.strip().lower())).strip("_")


def land(csv_name, table, source_file):
    target_lh = BRONZE_REAL or BRONZE_ID
    raw = None
    for lh, folder in [(BRONZE_REAL, "bronze_real"), (BRONZE_ID, "bronze")]:
        if not lh or lh.startswith("<"):
            continue
        try:
            raw = (spark.read.option("header", "true").option("multiLine", "true").option("escape", '"')
                   .csv(f"abfss://{WS_ID}@{endpoint}/{lh}/Files/{folder}/{csv_name}.csv"))
            target_lh = lh
            break
        except Exception:                                         # noqa: BLE001
            continue
    if raw is None:
        # Real-only fallback when the manual M365 Admin Center Agent 365 CSV has not
        # been uploaded yet: synthesize from Dataverse bronze_ref_agent_inventory if present.
        if csv_name == "bronze_agent365_agents" and BRONZE_REAL:
            dv = spark.read.format("delta").load(path(BRONZE_REAL, "bronze_ref_agent_inventory"))
            raw = dv.select(
                F.col("agent_key").alias("title_id"),
                F.col("agent_name").alias("name"),
                F.lit("Available").alias("status"),
                F.lit("Teams;Web").alias("channel"),
                F.lit("Internal").alias("publisher"),
                F.lit("Customer").alias("publisher_type"),
                F.lit("1.0").alias("version"),
                F.coalesce(F.col("owner_upn"), F.lit("")).alias("owner"),
                F.coalesce(F.col("platform"), F.lit("Copilot Studio")).alias("platform"),
                F.coalesce(F.col("environment_id"), F.lit("")).alias("environment_id"),
                F.col("agent_key").alias("bot_id"),
                F.lit("").alias("foundry_resource_id"),
                F.lit("").alias("creator_id"),
                F.lit("").alias("entra_agent_id"),
                F.lit("Production").alias("deployment"),
                F.coalesce(F.col("purpose"), F.lit("")).alias("description"),
                F.concat(F.coalesce(F.col("created_on"), F.lit(SNAPSHOT_DATE)), F.lit("T00:00:00Z")).alias("date_created"),
                F.concat(F.coalesce(F.col("created_on"), F.lit(SNAPSHOT_DATE)), F.lit("T00:00:00Z")).alias("last_modified"),
                F.concat(F.lit(SNAPSHOT_DATE), F.lit("T00:00:00Z")).alias("last_used"),
                F.lit("Yes").alias("custom_actions"),
                F.lit("No").alias("can_use_code_interpreter"),
                F.lit("Yes").alias("can_read_onedrive_and_sharepoint_items"),
                F.lit("Yes").alias("can_read_onedrive_files"),
                F.lit("Yes").alias("can_read_sharepoint_sites_and_files"),
                F.lit("No").alias("can_extend_to_graph_connector"),
            )
        else:
            raw = spark.createDataFrame(
                [],
                "user_principal_name string, total_agents_used string, total_sessions string, last_activity_date string",
            )
    raw = raw.toDF(*[snake(c) for c in raw.columns])
    raw = (raw.withColumn("_snapshot_date", F.lit(SNAPSHOT_DATE))
           .withColumn("_source_system", F.lit("Agent 365 / M365 admin center export"))
           .withColumn("_source_file", F.lit(source_file))
           .withColumn("_data_class", F.lit("REAL"))
           .withColumn("_loaded_at", F.current_timestamp()))
    raw.write.format("delta").mode("overwrite").option("overwriteSchema", "true").save(path(target_lh, table))
    return spark.read.format("delta").load(path(target_lh, table))


agents = land("bronze_agent365_agents", "bronze_agent365_agents", "Agents_2026-10-26_16_54_42.csv")
active = land("bronze_agent365_active_users", "bronze_agent365_active_users", "active_users_2026-10-26_165447.csv")


def yes(col):
    return F.coalesce(F.lower(F.col(col)) == "yes", F.lit(False))


def blank_to_null(col):
    return F.expr(f"nullif(trim({col}), '')")


# ---- SILVER: agent inventory ------------------------------------------------
owner = F.lower(blank_to_null("owner"))
publisher_type = F.coalesce(blank_to_null("publisher_type"), F.lit("Unknown"))
s_agents = (agents
            .withColumn("agent_key", F.col("title_id"))
            .withColumn("agent_name", F.trim("name"))
            .withColumn("owner_upn", owner)
            .withColumn("publisher_type", publisher_type)
            .withColumn("platform", F.coalesce(blank_to_null("platform"), F.lit("Not specified")))
            .withColumn("owner_class",
                        F.when(owner.isNotNull(), "Owner assigned")
                         .when(publisher_type == "FirstParty", "Microsoft-managed")
                         .when(publisher_type == "ThirdParty", "Publisher-managed")
                         .otherwise("Unowned tenant agent"))
            .withColumn("entra_agent_id", blank_to_null("entra_agent_id"))
            .withColumn("created_ts", F.expr("try_to_timestamp(nullif(trim(regexp_replace(date_created, 'Z$', '')), ''))"))
            .withColumn("last_modified_ts", F.expr("try_to_timestamp(nullif(trim(regexp_replace(last_modified, 'Z$', '')), ''))"))
            .withColumn("last_used_ts", F.expr("try_to_timestamp(nullif(trim(regexp_replace(last_used, 'Z$', '')), ''))"))
            .withColumn("has_custom_actions", yes("custom_actions"))
            .withColumn("can_use_code_interpreter", yes("can_use_code_interpreter"))
            .withColumn("can_read_files_or_sites",
                        yes("can_read_onedrive_and_sharepoint_items") | yes("can_read_onedrive_files")
                        | yes("can_read_sharepoint_sites_and_files"))
            .withColumn("can_extend_to_graph_connector", yes("can_extend_to_graph_connector"))
            .withColumn("has_entra_identity", F.col("entra_agent_id").isNotNull())
            .withColumn("is_active_status", F.col("status") == "Available")
            .select("agent_key", "agent_name", "status", "channel", "publisher", "publisher_type", "version",
                    "owner_upn", "owner_class", "platform", "environment_id", "bot_id", "foundry_resource_id",
                    "creator_id", "entra_agent_id", "deployment", "description", "created_ts",
                    "last_modified_ts", "last_used_ts", "has_custom_actions", "can_use_code_interpreter",
                    "can_read_files_or_sites", "can_extend_to_graph_connector", "has_entra_identity",
                    "is_active_status", "_snapshot_date", "_source_system", "_data_class"))
dupes = s_agents.groupBy("agent_key").count().filter("count > 1").count()
assert dupes == 0 and s_agents.filter("agent_key is null").count() == 0, "agent_key must be unique and non-null"
s_agents.write.format("delta").mode("overwrite").option("overwriteSchema", "true").save(
    path(SILVER_ID, "silver_agent_inventory"))

# ---- SILVER: agent activity -------------------------------------------------
s_activity = (active
              .withColumn("user_hash", F.regexp_replace("user_principal_name", "^'", ""))
              .withColumn("agents_used", F.col("total_agents_used").cast("int"))
              .withColumn("sessions", F.col("total_sessions").cast("int"))
              .withColumn("last_activity_date", F.to_date("last_activity_date"))
              .select("user_hash", "agents_used", "sessions", "last_activity_date",
                      "_snapshot_date", "_source_system", "_data_class"))
s_activity.write.format("delta").mode("overwrite").option("overwriteSchema", "true").save(
    path(SILVER_ID, "silver_agent_activity"))
s_agents = spark.read.format("delta").load(path(SILVER_ID, "silver_agent_inventory"))

# ---- GOLD: dim_agent --------------------------------------------------------
dim_agent = s_agents.select(
    "agent_key", "agent_name", "status", "publisher", "publisher_type", "platform", "version", "owner_upn",
    "owner_class", "channel", "deployment", "entra_agent_id", "environment_id",
    F.col("created_ts").cast("date").alias("created_date"),
    F.col("last_modified_ts").cast("date").alias("last_modified_date"),
    F.col("_data_class").alias("data_class"))
dim_agent.write.format("delta").mode("overwrite").option("overwriteSchema", "true").saveAsTable("dbo.dim_agent")

# ---- GOLD: fact_agent_inventory --------------------------------------------
ident = spark.table("dbo.dim_identity")
has_unknown = ident.filter(F.col("identity_key") == "unknown").count() > 0
by_upn = (ident.filter(F.col("upn").isNotNull())
          .select(F.lower("upn").alias("_upn"), F.col("identity_key").alias("_ik")).dropDuplicates(["_upn"]))
fact = (s_agents.join(by_upn, s_agents.owner_upn == by_upn._upn, "left")
        .select(F.to_date("_snapshot_date").alias("snapshot_date"), "agent_key",
                (F.coalesce("_ik", F.lit("unknown")) if has_unknown else F.col("_ik")).alias("owner_identity_key"),
                F.lit(1).alias("agent_count"),
                (F.col("owner_class") == "Unowned tenant agent").cast("int").alias("is_unowned_tenant_agent"),
                (F.col("owner_class") == "Owner assigned").cast("int").alias("has_owner"),
                F.col("has_entra_identity").cast("int").alias("has_entra_identity"),
                F.col("can_read_files_or_sites").cast("int").alias("can_read_files_or_sites"),
                F.col("has_custom_actions").cast("int").alias("has_custom_actions"),
                F.col("can_use_code_interpreter").cast("int").alias("can_use_code_interpreter"),
                F.col("is_active_status").cast("int").alias("is_available"),
                F.col("_data_class")))
fact.write.format("delta").mode("overwrite").option("overwriteSchema", "true").saveAsTable("dbo.fact_agent_inventory")
fact = spark.table("dbo.fact_agent_inventory")

# ---- GOLD: fact_agent_activity ----------------------------------------------
# Joins to the same dim_identity the FOCUS-derived facts use (identity_key = Entra user key / alias).
# The admin-center export masks UPNs with a salted hash by default; a hash cannot be reversed, so a
# masked user stays on the 'unknown' member and is_identity_resolved = 0 rather than being guessed.
by_key = ident.select(F.lower("identity_key").alias("_k"), F.col("identity_key").alias("_ik2"))
act = (s_activity
       .withColumn("_u", F.lower("user_hash"))
       .withColumn("_alias", F.regexp_extract("_u", r"^([^@]+)@", 1))
       .join(by_upn, F.col("_u") == by_upn._upn, "left")
       .join(by_key, F.col("_alias") == by_key._k, "left")
       .withColumn("_resolved", F.coalesce("_ik", "_ik2")))
fact_act = act.select(
    F.col("last_activity_date").alias("activity_date"),
    (F.coalesce("_resolved", F.lit("unknown")) if has_unknown else F.col("_resolved")).alias("identity_key"),
    F.col("agents_used").alias("agents_used"), F.col("sessions").alias("sessions"),
    F.col("_resolved").isNotNull().cast("int").alias("is_identity_resolved"),
    F.col("_data_class"))
fact_act.write.format("delta").mode("overwrite").option("overwriteSchema", "true").saveAsTable("dbo.fact_agent_activity")
fact_act = spark.table("dbo.fact_agent_activity")

# dim_date is built from fact_ai_usage bounds, so extend it for the snapshot/activity dates if needed.
existing = spark.table("dbo.dim_date")
missing = (fact.select(F.col("snapshot_date").alias("date_key"))
           .union(fact_act.select(F.col("activity_date").alias("date_key"))).distinct()
           .filter(F.col("date_key").isNotNull())
           .join(existing.select("date_key"), "date_key", "left_anti"))
if missing.count():
    add = (missing.withColumn("year", F.year("date_key"))
           .withColumn("quarter", F.concat(F.lit("Q"), F.quarter("date_key")))
           .withColumn("month", F.month("date_key"))
           .withColumn("month_name", F.date_format("date_key", "MMM yyyy"))
           .withColumn("day", F.dayofmonth("date_key"))
           .withColumn("day_name", F.date_format("date_key", "EEE"))
           .withColumn("is_weekday", F.dayofweek("date_key").between(2, 6))
           .withColumn("year_month", F.date_format("date_key", "yyyy-MM")))
    add.select(*[F.col(f.name).cast(f.dataType) for f in existing.schema.fields]).write.format(
        "delta").mode("append").saveAsTable("dbo.dim_date")

# ---- GOLD: referential integrity + validation -------------------------------
orphans = {
    "dim_agent": fact.select("agent_key").distinct().join(
        spark.table("dbo.dim_agent").select("agent_key"), "agent_key", "left_anti").count(),
    "dim_date": fact.select(F.col("snapshot_date").alias("date_key")).distinct().join(
        spark.table("dbo.dim_date").select("date_key"), "date_key", "left_anti").count(),
    "activity_dim_date": fact_act.select(F.col("activity_date").alias("date_key")).filter("date_key is not null").distinct().join(
        spark.table("dbo.dim_date").select("date_key"), "date_key", "left_anti").count()}
assert all(v == 0 for v in orphans.values()), f"orphan keys: {orphans}"
assert fact.count() == spark.table("dbo.dim_agent").count() == agents.count()

import json
summary = {
    "snapshot_date": SNAPSHOT_DATE,
    "agents": fact.count(),
    "by_owner_class": {r[0]: r[1] for r in s_agents.groupBy("owner_class").count().collect()},
    "by_publisher_type": {r[0]: r[1] for r in s_agents.groupBy("publisher_type").count().collect()},
    "by_status": {r[0]: r[1] for r in s_agents.groupBy("status").count().collect()},
    "with_entra_identity": s_agents.filter("has_entra_identity").count(),
    "owners_resolved_to_dim_identity": fact.filter(F.col("owner_identity_key").isNotNull()
                                                   & (F.col("owner_identity_key") != "unknown")).count(),
    "activity_rows": s_activity.count(),
    "activity_identity_resolved": fact_act.filter("is_identity_resolved = 1").count(),
    "orphan_keys": orphans,
}
mssparkutils.fs.put("Files/live_source/agent365_validation.json", json.dumps(summary, default=str), True)
for t in ["dim_agent", "fact_agent_inventory", "fact_agent_activity", "dim_date"]:
    mssparkutils.fs.put(f"Files/export/{t}.csv", spark.table("dbo." + t).toPandas().to_csv(index=False), True)
print(json.dumps(summary, default=str))
