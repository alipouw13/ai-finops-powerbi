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

WS_ID = "<injected>"
SILVER_ID = "<injected>"
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
TEAM_MAP = [("Underwriting", "PERS-KNOW"), ("Claims", "PERS-KNOW"),
            ("Checkout", "PERS-DEV"), ("Search", "PERS-DEV"), ("Mobile", "PERS-DEV"),
            ("Data", "PERS-AIDS"), ("DataScience", "PERS-AIDS"), ("Actuarial", "PERS-AIDS"),
            ("Platform", "PERS-PLAT")]

# ---- SILVER: assumed team -> persona map -----------------------------------
s_map = spark.createDataFrame(TEAM_MAP, "team string, persona_key string").withColumn(
    "_data_class", F.lit("ASSUMED"))
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
stamped = (ident.join(s_map.select("team", "persona_key"), "team", "left")
           .withColumn("persona_key", F.when(F.col("is_human") == False, F.lit("PERS-WORKLOAD"))  # noqa: E712
                       .when(F.col("identity_key") == "unknown", F.lit("PERS-UNASSIGNED"))
                       .otherwise(F.coalesce("persona_key", F.lit("PERS-UNASSIGNED")))))
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
