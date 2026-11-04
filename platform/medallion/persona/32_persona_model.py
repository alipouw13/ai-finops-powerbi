# ---------------------------------------------------------------------------
# 32_persona_model: persona dimension + persona-to-tool affinity (Silver reference -> Gold star)
#
# Source of truth for personas and their tools is the "Key Stakeholders" diagram:
#   solid line  = primary tool, dotted line = secondary tool.
# The identity -> persona assignment is an ASSUMPTION (derived from dim_identity.team) until the
# customer provides an HR / Entra job-profile feed. It is labelled ASSUMED, never REAL.
#
#   Silver: silver_persona_map        team -> persona (assumed mapping, reviewable)
#   Gold:   dim_persona               5 personas + 'Workload (non-human)' + 'Unassigned' members
#           bridge_persona_platform   persona x platform, affinity = Primary | Secondary
#           dim_identity.persona_key  stamped on the existing identity dimension
#           dim_platform              adds CopilotCowork and AzureML rows (shown, not yet ingested)
# Runs AFTER Gold_Validate because Gold rewrites dim_identity. Idempotent.
# The default lakehouse must be the GOLD lakehouse.
# ---------------------------------------------------------------------------
import json
from pyspark.sql import functions as F

WS_ID = globals().get("WS_ID") or "0cb77528-27b4-4298-a48a-477caff105f3"
SILVER_ID = globals().get("SILVER_ID") or "597401b8-65f9-4fff-802b-8b5a584fd8cf"
endpoint = "onelake.dfs.fabric.microsoft.com"


def path(lakehouse, table):
    return f"abfss://{WS_ID}@{endpoint}/{lakehouse}/Tables/dbo/{table}"


PERSONAS = [
    ("PERS-KNOW", "Knowledge & Frontline Worker", 1, "Consumes AI inside the tools they already use"),
    ("PERS-MAKER", "Business-Led Developer / Maker", 2, "Builds low-code agents and flows"),
    ("PERS-DEV", "Software Developer", 3, "Writes and ships application code"),
    ("PERS-AIDS", "AI Engineer / Data Scientist", 4, "Builds and tunes models and AI pipelines"),
    ("PERS-PLAT", "Platform / Infrastructure Engineer", 5, "Runs the cloud and AI platform"),
    ("PERS-WORKLOAD", "Workload (non-human)", 6, "Service principals and managed identities"),
    ("PERS-UNASSIGNED", "Unassigned", 7, "Human identity with no persona mapping yet"),
]
# (persona, platform, affinity) read from the diagram
AFFINITY = [
    ("PERS-KNOW", "M365Copilot", "Primary"), ("PERS-KNOW", "CopilotStudio", "Secondary"),
    ("PERS-KNOW", "CopilotCowork", "Secondary"),
    ("PERS-MAKER", "CopilotStudio", "Primary"), ("PERS-MAKER", "M365Copilot", "Secondary"),
    ("PERS-MAKER", "CopilotCowork", "Secondary"), ("PERS-MAKER", "Foundry", "Secondary"),
    ("PERS-DEV", "GitHubCopilot", "Primary"), ("PERS-DEV", "M365Copilot", "Secondary"),
    ("PERS-DEV", "Foundry", "Secondary"),
    ("PERS-AIDS", "Foundry", "Primary"), ("PERS-AIDS", "GitHubCopilot", "Secondary"),
    ("PERS-AIDS", "M365Copilot", "Secondary"), ("PERS-AIDS", "AzureML", "Secondary"),
    ("PERS-PLAT", "GitHubCopilot", "Primary"), ("PERS-PLAT", "M365Copilot", "Secondary"),
    ("PERS-PLAT", "Foundry", "Secondary"), ("PERS-PLAT", "AzureML", "Secondary"),
]
TEAM_MAP = [
    # Demo / Retail / Insurance teams
    ("Underwriting", "PERS-KNOW"), ("Claims", "PERS-KNOW"),
    ("Checkout", "PERS-DEV"), ("Search", "PERS-DEV"), ("Mobile", "PERS-DEV"),
    ("Data", "PERS-AIDS"), ("DataScience", "PERS-AIDS"), ("Actuarial", "PERS-AIDS"),
    ("Platform", "PERS-PLAT"),
    # Financial Services (FSI) & Enterprise Entra department names
    ("Retail Banking", "PERS-KNOW"), ("Wealth Management", "PERS-KNOW"),
    ("Commercial Banking", "PERS-KNOW"), ("Investment Banking", "PERS-KNOW"),
    ("Capital Markets", "PERS-KNOW"), ("Risk & Compliance", "PERS-KNOW"),
    ("Risk", "PERS-KNOW"), ("Compliance", "PERS-KNOW"), ("Legal", "PERS-KNOW"),
    ("Internal Audit", "PERS-KNOW"), ("Operations", "PERS-KNOW"),
    ("Customer Service", "PERS-KNOW"), ("Treasury", "PERS-KNOW"),
    ("Finance & Strategy", "PERS-MAKER"), ("Finance", "PERS-MAKER"),
    ("Business Automation", "PERS-MAKER"), ("Low-Code", "PERS-MAKER"),
    ("Power Platform", "PERS-MAKER"), ("Operations Excellence", "PERS-MAKER"),
    ("Software Engineering", "PERS-DEV"), ("Engineering", "PERS-DEV"),
    ("Application Development", "PERS-DEV"), ("Core Banking", "PERS-DEV"),
    ("Digital Channels", "PERS-DEV"), ("Digital", "PERS-DEV"),
    ("Quantitative Research", "PERS-AIDS"), ("Quant", "PERS-AIDS"),
    ("Model Risk", "PERS-AIDS"), ("Model Risk Management", "PERS-AIDS"),
    ("AI & Machine Learning", "PERS-AIDS"), ("AI Engineering", "PERS-AIDS"),
    ("Data Science", "PERS-AIDS"), ("Analytics", "PERS-AIDS"),
    ("Platform Engineering", "PERS-PLAT"), ("Cloud Engineering", "PERS-PLAT"),
    ("Infrastructure", "PERS-PLAT"), ("Cybersecurity", "PERS-PLAT"),
    ("Information Security", "PERS-PLAT"), ("SecOps", "PERS-PLAT"),
    ("SRE", "PERS-PLAT"), ("DevOps", "PERS-PLAT"), ("IT", "PERS-PLAT"),
]

# ---- SILVER: assumed team -> persona map (externalizable via bronze_ref_persona_map)
BRONZE_REAL = globals().get("BRONZE_REAL_ID") or ""
BRONZE_MOCK = globals().get("BRONZE_ID") or ""
custom_map = None
for _lh in (BRONZE_REAL, BRONZE_MOCK):
    if not _lh or _lh.startswith("<"):
        continue
    try:
        custom_map = (spark.read.format("delta")
                      .load(path(_lh, "bronze_ref_persona_map"))
                      .select("team", "persona_key")
                      .withColumn("_data_class", F.lit("REAL")))
        break
    except Exception:                                             # noqa: BLE001
        continue

s_map = spark.createDataFrame(TEAM_MAP, "team string, persona_key string").withColumn(
    "_data_class", F.lit("ASSUMED"))
if custom_map is not None:
    s_map = custom_map.unionByName(
        s_map.join(custom_map.select("team"), "team", "left_anti"))
s_map.write.format("delta").mode("overwrite").option("overwriteSchema", "true").save(
    path(SILVER_ID, "silver_persona_map"))
s_map = spark.read.format("delta").load(path(SILVER_ID, "silver_persona_map"))

# ---- GOLD: dim_platform (tools in the diagram that are not ingested yet) -----
plat = spark.table("dbo.dim_platform")
cols = plat.columns
extra = {
    "CopilotCowork": {"platform_name": "Copilot Cowork / Agents", "billing_model": "Metered (not yet confirmed)",
                      "native_unit": "unmeasured", "data_source": "NOT INGESTED - no meter seen in Cost Management"},
    "AzureML": {"platform_name": "Azure Machine Learning", "billing_model": "Compute (Azure FOCUS)",
                "native_unit": "compute_hour", "data_source": "NOT INGESTED - available via Azure FOCUS export"},
}
rows = []
for k, v in extra.items():
    if plat.filter(F.col("platform_key") == k).count() == 0:
        r = {c: v.get(c, "") for c in cols}
        r["platform_key"] = k
        rows.append(tuple(str(r[c]) for c in cols))
if rows:
    new = spark.createDataFrame(rows, ", ".join(c + " string" for c in cols))
    new = new.select(*[F.col(f.name).cast(f.dataType) for f in plat.schema.fields])
    out = plat.unionByName(new)
    out.cache().count()
    out.write.format("delta").mode("overwrite").option("overwriteSchema", "true").saveAsTable("dbo.dim_platform")

# ---- GOLD: dim_persona + bridge --------------------------------------------
dim_persona = spark.createDataFrame(PERSONAS, "persona_key string, persona_name string, persona_order int, description string")
dim_persona.write.format("delta").mode("overwrite").option("overwriteSchema", "true").saveAsTable("dbo.dim_persona")

bridge = spark.createDataFrame(AFFINITY, "persona_key string, platform_key string, affinity string").withColumn(
    "is_primary", (F.col("affinity") == "Primary").cast("int")).withColumn(
    "bridge_key", F.concat_ws("|", "persona_key", "platform_key"))
bridge.write.format("delta").mode("overwrite").option("overwriteSchema", "true").saveAsTable("dbo.bridge_persona_platform")

# ---- GOLD: stamp persona_key on dim_identity -------------------------------
ident = spark.table("dbo.dim_identity")
if "persona_key" in ident.columns:
    ident = ident.drop("persona_key")
s_map_norm = s_map.select(
    F.lower(F.trim("team")).alias("_team_norm"),
    F.col("persona_key").alias("_mapped_persona"),
).dropDuplicates(["_team_norm"])
t_lower = F.lower(F.coalesce(F.col("team"), F.lit("")))
heuristic_persona = (
    F.when(t_lower.rlike("quant|data|actuar|ai |ml |model risk|analytics"), F.lit("PERS-AIDS"))
     .when(t_lower.rlike("platform|infra|cloud|secops|cyber|sre|devops|it "), F.lit("PERS-PLAT"))
     .when(t_lower.rlike("eng|software|dev|checkout|search|mobile|core banking|digital"), F.lit("PERS-DEV"))
     .when(t_lower.rlike("finance|strategy|automation|maker|power platform|low-code"), F.lit("PERS-MAKER"))
     .when(t_lower.rlike("underwrit|claim|risk|compliance|legal|audit|wealth|retail|commercial|bank|ops|operation|treasury"), F.lit("PERS-KNOW"))
     .otherwise(F.lit("PERS-UNASSIGNED"))
)
stamped = (ident.withColumn("_team_norm", F.lower(F.trim(F.coalesce("team", F.lit("")))))
           .join(s_map_norm, "_team_norm", "left")
           .withColumn("persona_key",
                       F.when(F.col("is_human") == False, F.lit("PERS-WORKLOAD"))  # noqa: E712
                        .when(F.col("identity_key") == "unknown", F.lit("PERS-UNASSIGNED"))
                        .otherwise(F.coalesce("_mapped_persona", heuristic_persona)))
           .drop("_team_norm", "_mapped_persona"))
stamped = stamped.select(*ident.columns, "persona_key")
stamped.cache().count()
stamped.write.format("delta").mode("overwrite").option("overwriteSchema", "true").saveAsTable("dbo.dim_identity")

# ---- validation -------------------------------------------------------------
di = spark.table("dbo.dim_identity")
orph_ident = di.join(spark.table("dbo.dim_persona"), "persona_key", "left_anti").count()
orph_bridge_p = bridge.join(spark.table("dbo.dim_persona"), "persona_key", "left_anti").count()
orph_bridge_t = bridge.join(spark.table("dbo.dim_platform").select("platform_key"), "platform_key", "left_anti").count()
assert orph_ident == orph_bridge_p == orph_bridge_t == 0, (orph_ident, orph_bridge_p, orph_bridge_t)
summary = {
    "identities_by_persona": {r[0]: r[1] for r in di.groupBy("persona_key").count().collect()},
    "personas_without_identities": [r[0] for r in spark.table("dbo.dim_persona").join(
        di.select("persona_key").distinct(), "persona_key", "left_anti").select("persona_key").collect()],
    "bridge_rows": bridge.count(),
    "orphans": {"identity": orph_ident, "bridge_persona": orph_bridge_p, "bridge_platform": orph_bridge_t},
    "assignment_basis": "ASSUMED - derived from dim_identity.team",
}
mssparkutils.fs.put("Files/live_source/persona_validation.json", json.dumps(summary), True)
for t in ["dim_persona", "bridge_persona_platform", "dim_identity", "dim_platform"]:
    mssparkutils.fs.put(f"Files/export/{t}.csv", spark.table("dbo." + t).toPandas().to_csv(index=False), True)
print(json.dumps(summary))
