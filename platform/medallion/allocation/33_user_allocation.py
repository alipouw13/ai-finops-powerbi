# ---------------------------------------------------------------------------
# 33_user_allocation: user-level cost and token allocation (Gold star)
#
# Answers "which person is behind this spend, on which application/agent?" with an explicit
# allocation basis on every row, never a silent guess:
#   DIRECT       the calling identity is already a human (Copilot seats, GitHub, Studio makers)
#   OWNER        the caller is a service principal / managed identity / app; the cost is allocated to
#                the accountable owner of the application (dim_application.owner_upn -> dim_identity)
#   UNALLOCATED  no human resolved; stays on the 'unknown' identity member
# OWNER is accountability, not proof of who typed the prompts. Direct per-person token attribution
# for gateway traffic needs the caller (Entra user object id) logged on the APIM/Foundry request.
#
# fact_user_cost_allocation re-keys the SAME dollars and tokens as fact_ai_usage; it ties to it
# exactly (asserted) and must not be added to it.
# Runs AFTER Persona_Model. The default lakehouse must be the GOLD lakehouse.
# ---------------------------------------------------------------------------
import json
from pyspark.sql import functions as F

fact = spark.table("dbo.fact_ai_usage")
ident = spark.table("dbo.dim_identity")
app = spark.table("dbo.dim_application")

assert ident.filter(F.col("identity_key") == "unknown").count() == 1, "dim_identity needs the 'unknown' member"

owner_map = (ident.filter(F.col("upn").isNotNull() & (F.col("is_human") == True))  # noqa: E712
             .select(F.lower("upn").alias("_owner_upn"), F.col("identity_key").alias("_owner_key"))
             .dropDuplicates(["_owner_upn"]))
app_owner = (app.select("application_key", F.lower("owner_upn").alias("_owner_upn"))
             .join(owner_map, "_owner_upn", "left").select("application_key", "_owner_key"))
who = ident.select(F.col("identity_key").alias("_src"), F.col("is_human").alias("_is_human"))

f = (fact.join(who, fact.identity_key == who._src, "left")
     .join(app_owner, "application_key", "left")
     .withColumn("allocation_basis",
                 F.when(F.col("_is_human") == True, "DIRECT")  # noqa: E712
                 .when(F.col("_owner_key").isNotNull(), "OWNER")
                 .otherwise("UNALLOCATED"))
     .withColumn("user_identity_key",
                 F.when(F.col("allocation_basis") == "DIRECT", F.col("identity_key"))
                 .when(F.col("allocation_basis") == "OWNER", F.col("_owner_key"))
                 .otherwise(F.lit("unknown"))))

keys = ["usage_date", "platform_key", "application_key", "environment_key", "business_unit_key",
        "cost_center_key", "user_identity_key", "allocation_basis", "cost_is_estimated"]
agg = (f.withColumn("source_identity_key", F.col("identity_key"))
       .groupBy(*keys, "source_identity_key")
       .agg(F.sum("input_tokens").alias("input_tokens"), F.sum("output_tokens").alias("output_tokens"),
            F.sum("requests").alias("requests"), F.sum("cost_usd").alias("cost_usd")))
out = (agg.withColumn("total_tokens", F.when(F.col("input_tokens").isNull() & F.col("output_tokens").isNull(), F.lit(None))
                      .otherwise(F.coalesce("input_tokens", F.lit(0)) + F.coalesce("output_tokens", F.lit(0))))
       .select("usage_date", "platform_key", "application_key", "environment_key", "business_unit_key",
               "cost_center_key", "user_identity_key", "source_identity_key", "allocation_basis",
               "cost_is_estimated", "input_tokens", "output_tokens", "total_tokens", "requests", "cost_usd"))
out.write.format("delta").mode("overwrite").option("overwriteSchema", "true").saveAsTable("dbo.fact_user_cost_allocation")
out = spark.table("dbo.fact_user_cost_allocation")


# ---- validation: ties to fact_ai_usage, no orphans ---------------------------
def tot(df, c):
    return float(df.agg(F.sum(c)).collect()[0][0] or 0.0)


diffs = {"cost_usd": tot(out, "cost_usd") - tot(fact, "cost_usd"),
         "input_tokens": tot(out, "input_tokens") - tot(fact, "input_tokens"),
         "output_tokens": tot(out, "output_tokens") - tot(fact, "output_tokens"),
         "requests": tot(out, "requests") - tot(fact, "requests")}
assert abs(diffs["cost_usd"]) < 0.01 and all(abs(v) < 0.5 for v in diffs.values()), diffs


def orphans(col, dim, dcol):
    return out.select(F.col(col).alias("k")).distinct().join(
        spark.table("dbo." + dim).select(F.col(dcol).alias("k")), "k", "left_anti").count()


orph = {"user_identity_key": orphans("user_identity_key", "dim_identity", "identity_key"),
        "source_identity_key": orphans("source_identity_key", "dim_identity", "identity_key"),
        "application_key": orphans("application_key", "dim_application", "application_key"),
        "platform_key": orphans("platform_key", "dim_platform", "platform_key"),
        "usage_date": orphans("usage_date", "dim_date", "date_key"),
        "business_unit_key": orphans("business_unit_key", "dim_business_unit", "business_unit_key"),
        "cost_center_key": orphans("cost_center_key", "dim_cost_center", "cost_center_key"),
        "environment_key": orphans("environment_key", "dim_environment", "environment_key")}
assert all(v == 0 for v in orph.values()), orph

by_basis = {r[0]: [round(r[1], 2), r[2]] for r in out.groupBy("allocation_basis").agg(
    F.sum("cost_usd"), F.sum("total_tokens")).collect()}
mobile = (out.join(app.filter(F.col("application_name") == "Mobile Shopping Assistant").select("application_key"),
                   "application_key")
          .join(ident.select("identity_key", "display_name"), out.user_identity_key == ident.identity_key)
          .groupBy("display_name", "allocation_basis").agg(F.sum("total_tokens").alias("tokens"),
                                                          F.sum("cost_usd").alias("cost")).collect())
top_apps = (out.join(app.select("application_key", "application_name"), "application_key", "left")
            .groupBy("application_key", "application_name", "allocation_basis")
            .agg(F.sum("total_tokens").alias("tokens"), F.sum("cost_usd").alias("cost"))
            .orderBy(F.col("cost").desc()).limit(10).collect())
summary = {"rows": out.count(), "tie_out_diffs": diffs, "orphans": orph,
           "cost_and_tokens_by_basis": by_basis,
           "top_applications_by_cost": [[r[0], r[1], r[2], r[3], round(r[4] or 0, 2)] for r in top_apps],
           "mobile_shopping_assistant": [[r[0], r[1], r[2], round(r[3] or 0, 2)] for r in mobile]}
mssparkutils.fs.put("Files/live_source/user_allocation_validation.json", json.dumps(summary, default=str), True)
mssparkutils.fs.put("Files/export/fact_user_cost_allocation.csv", out.toPandas().to_csv(index=False), True)
print(json.dumps(summary, default=str))
