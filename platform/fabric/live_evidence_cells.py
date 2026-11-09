"""Additive notebook cells for verified live source evidence.

The deployment process supplies WS_ID, BRONZE_REAL_ID, SILVER_ID and GOLD_ID.
Evidence is separate from modeled spend to prevent double-counting Azure billing.
"""

BRONZE = '''
import notebookutils
from pyspark.sql import functions as F
try:
    payload = notebookutils.fs.head("Files/live_source/baseline.json", 1048576)
except Exception:
    payload = None
if not payload:
    print("Skipping demo live_source baseline ingestion (no Files/live_source/baseline.json present).")
else:
    raw = spark.createDataFrame([(payload,)], "payload string")
    raw = raw.withColumn("_loaded_at", F.current_timestamp())
    raw.write.format("delta").mode("overwrite").saveAsTable("dbo.bronze_live_source_baseline")
    costs = spark.read.option("multiline", True).json("Files/live_source/cost_query.json")
    assert costs.count() == 1, "Expected one cost-query response"
    costs.select(F.to_json(F.struct("*")).alias("payload")).write.format("delta").mode("overwrite").saveAsTable("dbo.bronze_live_cost_query")
'''

SILVER = '''
import json
import notebookutils
from pyspark.sql import functions as F
endpoint = notebookutils.conf.get("trident.onelake.endpoint").replace("https://", "").rstrip("/")
def live_path(lakehouse, table):
    return f"abfss://{WS_ID}@{endpoint}/{lakehouse}/Tables/dbo/{table}"
try:
    raw = spark.read.format("delta").load(live_path(BRONZE_REAL_ID, "bronze_live_source_baseline"))
    has_baseline = raw.count() > 0
except Exception:
    has_baseline = False
if not has_baseline:
    print("Skipping demo live_source Silver reconciliation (no bronze_live_source_baseline table present).")
else:
    baseline = json.loads(raw.select("payload").first()[0])
    lic = baseline["licensing"]
    assert lic["assigned_seats"] <= lic["enabled_seats"]
    assert lic["assigned_seats"] == len(lic["assigned_users"])
    license_summary = spark.createDataFrame([(
        baseline["test_date"], baseline["tenant_id"], lic["sku_id"],
        lic["enabled_seats"], lic["assigned_seats"], lic["unassigned_seats"], "REAL"
    )], "snapshot_date string, tenant_id string, sku_id string, enabled_seats long, assigned_seats long, unassigned_seats long, _data_class string")
    license_summary.write.format("delta").mode("overwrite").saveAsTable("dbo.silver_live_license_summary")
    cost_raw = spark.read.format("delta").load(live_path(BRONZE_REAL_ID, "bronze_live_cost_query"))
    cost_query = json.loads(cost_raw.select("payload").first()[0])
    props = cost_query["data"]["properties"]
    assert not props.get("nextLink"), "Incomplete cost-query pagination"
    columns = [c["name"] for c in props["columns"]]
    cost_rows = [dict(zip(columns, r)) for r in props["rows"]]
    cost_df = spark.createDataFrame([
        (str(r["UsageDate"]), r["ServiceName"], r["Meter"], float(r["Cost"]), r["Currency"],
         baseline["subscription_id"], "REAL")
        for r in cost_rows
    ], "usage_date string, service_name string, meter string, actual_cost double, currency string, subscription_id string, _data_class string")
    assert cost_df.count() == baseline["non_seat_billing"]["total_returned_rows"]
    cost_df.write.format("delta").mode("overwrite").saveAsTable("dbo.silver_live_cost_query")
    status = spark.createDataFrame([
        ("licensing", "verified", 200, baseline["test_date"]),
        ("copilot_usage", baseline["copilot_usage"]["status"], baseline["copilot_usage"]["http_status"], baseline["test_date"]),
        ("m365_non_seat_billing", baseline["non_seat_billing"]["status"], baseline["non_seat_billing"]["http_status"], baseline["test_date"])
    ], "source string, status string, http_status long, snapshot_date string")
    status.write.format("delta").mode("overwrite").saveAsTable("dbo.silver_live_source_status")
'''

GOLD = '''
import json
import notebookutils
from pyspark.sql import functions as F
endpoint = notebookutils.conf.get("trident.onelake.endpoint").replace("https://", "").rstrip("/")
def evidence_path(lakehouse, table):
    return f"abfss://{WS_ID}@{endpoint}/{lakehouse}/Tables/dbo/{table}"
for legacy in ["gold_live_license_summary", "gold_live_cost_query", "gold_live_source_status"]:
    spark.sql("DROP TABLE IF EXISTS dbo." + legacy)
try:
    evidence_df = spark.read.format("delta").load(evidence_path(BRONZE_REAL_ID, "bronze_live_source_baseline"))
    has_evidence = evidence_df.count() > 0
except Exception:
    has_evidence = False
if not has_evidence:
    print("Skipping demo live_source Gold baseline assertion (no bronze_live_source_baseline table present).")
else:
    evidence = json.loads(evidence_df.select("payload").first()[0])
    license_row = spark.read.format("delta").load(
        evidence_path(SILVER_ID, "silver_live_license_summary")).first()
    assert license_row.assigned_seats == evidence["licensing"]["assigned_seats"]
    fact = spark.table("dbo.fact_ai_usage")
    assert fact.count() > 0, "Gold fact table is empty"
    id_tbl = spark.table("dbo.dim_identity")
    expected_lower = [u.lower() for u in evidence["licensing"]["assigned_users"]]
    valid_keys = [
        r[0] for r in id_tbl.filter(
            F.lower(F.col("identity_key")).isin(expected_lower)
            | F.lower(F.coalesce(F.col("upn"), F.lit(""))).isin(expected_lower)
        ).select("identity_key").collect()
    ] + evidence["licensing"]["assigned_users"]
    real_seats = fact.filter((F.col("platform_key") == "M365Copilot") &
        (F.col("unit_type") == "seat_day") &
        F.col("identity_key").isin(valid_keys) &
        (F.col("usage_date").cast("string") == evidence["test_date"]))
    assert real_seats.count() >= evidence["licensing"]["assigned_seats"], "Real seat snapshot missing from Gold fact"
    counts = {table.name: spark.table("dbo." + table.name).count()
              for table in spark.catalog.listTables("dbo") if not table.isTemporary}
    result = {"status": "verified", "snapshot_date": evidence["test_date"],
              "real_seat_rows": real_seats.count(), "gold_table_counts": counts,
              "usage_report_status": evidence["copilot_usage"]["status"],
              "m365_non_seat_status": evidence["non_seat_billing"]["status"]}
    notebookutils.fs.put("Files/live_source/validation.json", json.dumps(result), True)
    print(json.dumps(result, indent=2))
'''
