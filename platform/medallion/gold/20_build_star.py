# Fabric notebook — GOLD: emit the star the semantic model consumes
# ---------------------------------------------------------------------------
# Gold materializes the 10-table star in AIFinOps.SemanticModel:
#   fact_ai_usage + dim_date/platform/identity/model/cost_center/rate_card
#                 + dim_business_unit/application/environment
#
# The COLUMN CONTRACT below must stay identical to the CSV headers the PBIP
# reads, because the TMDL partition casts are keyed to those exact names. Gold
# asserts the contract and the dimension joins, so schema drift fails loudly
# here instead of silently dropping spend inside Power BI.
#
# Spark twin of the GOLD section of platform/data-store/build_store.py.
# ---------------------------------------------------------------------------
from pyspark.sql import functions as F

spark.sql("CREATE DATABASE IF NOT EXISTS finops_gold")

FACT_CONTRACT = ["usage_date", "platform_key", "identity_key", "model_key",
                 "cost_center_key", "unit_type", "quantity", "input_tokens",
                 "output_tokens", "cached_tokens", "requests", "cost_usd",
                 "cost_is_estimated", "is_error", "latency_ms", "application_key",
                 "environment_key", "business_unit_key"]

# Fabric capacity is real FOCUS content but it is the platform's own running
# cost, not AI consumption, so it stays in Silver and is deliberately excluded
# from the usage star. Report it from finops_silver.cost_charge instead.
spark.sql(f"""
CREATE OR REPLACE TABLE finops_gold.fact_ai_usage AS
SELECT usage_date, platform_key, identity_key, model_key, cost_center_key,
       unit_type, quantity, input_tokens, output_tokens, cached_tokens, requests,
       round(cost_usd, 8) AS cost_usd, cost_is_estimated, is_error,
       round(latency_ms, 4) AS latency_ms,
       application_key, environment_key, business_unit_key
FROM finops_silver.usage_conformed
""")

# ------------------------------------------------------------------- dims ----
spark.sql("""
CREATE OR REPLACE TABLE finops_gold.dim_identity AS
SELECT identity_key, display_name, principal_type, upn, github_login, team,
       business_unit, cost_center_key,
       CASE WHEN is_human = 'TRUE' THEN 'Human'
            WHEN principal_type = 'ServicePrincipal' THEN 'ServicePrincipal'
            ELSE 'Application' END AS identity_class,
       is_human, home_business_unit_key
FROM finops_bronze.ref_identity_map
UNION ALL
-- Agents are first-class identities: they consume credits exactly as people
-- consume seats, so the same vocabulary has to cover them.
SELECT a.agent_key, a.agent_name, 'Agent', '', '', 'Copilot Studio',
       coalesce(b.business_unit_name, ''), a.cost_center_key,
       'Agent', 'FALSE', a.owner_business_unit_key
FROM finops_bronze.ref_agent_inventory a
LEFT JOIN finops_bronze.ref_business_hierarchy b
       ON b.business_unit_key = a.owner_business_unit_key
UNION ALL
-- Requests that arrived without a resolvable caller still cost money. Giving
-- them a real key keeps unallocated spend on the dashboard instead of dropping
-- it from the join.
SELECT 'unknown', 'Unattributed caller', 'Unknown', '', '', 'Unallocated',
       'Unallocated', '', 'Unattributed', 'FALSE', 'BU-UNALLOC'
""")

spark.sql("""
CREATE OR REPLACE TABLE finops_gold.dim_application AS
SELECT application_key, application_name, application_type,
       owner_business_unit_key, owner_upn, default_environment_key,
       criticality, is_mock
FROM finops_bronze.ref_app_inventory
""")

spark.sql("""
CREATE OR REPLACE TABLE finops_gold.dim_business_unit AS
SELECT business_unit_key, business_unit_name, division, monthly_budget_usd,
       executive_owner, is_mock_budget
FROM finops_bronze.ref_business_hierarchy
""")

spark.sql("""
CREATE OR REPLACE TABLE finops_gold.dim_cost_center AS
SELECT cost_center_key, MIN(team) AS cost_center_name,
       MIN(business_unit) AS business_unit, '' AS owner_upn
FROM finops_bronze.ref_identity_map
WHERE cost_center_key <> ''
GROUP BY cost_center_key
""")

spark.sql("""
CREATE OR REPLACE TABLE finops_gold.dim_environment AS
SELECT 'ENV-PROD' AS environment_key, 'Production' AS environment_name,
       'TRUE' AS is_production, 'Tier-1' AS sla_tier
UNION ALL SELECT 'ENV-TEST', 'Test', 'FALSE', 'Tier-3'
UNION ALL SELECT 'ENV-DEV', 'Development', 'FALSE', 'Tier-3'
UNION ALL SELECT 'ENV-UNK', 'Unknown', 'FALSE', 'Unknown'
""")

spark.sql("""
CREATE OR REPLACE TABLE finops_gold.dim_model AS
SELECT DISTINCT model_key, model_key AS model_name,
       CASE WHEN model_key LIKE 'gpt-4.1%' THEN '2025-04-14' ELSE '' END AS model_version,
       CASE WHEN model_key LIKE 'gpt-%' OR model_key LIKE 'o3%'
            THEN 'Azure OpenAI' ELSE 'GitHub Copilot' END AS provider,
       'text' AS modality
FROM finops_gold.fact_ai_usage
WHERE model_key <> ''
""")

# data_source keeps provenance a COLUMN: swapping a mock feed for the live API
# changes this value and nothing else in the pipeline.
spark.sql("""
CREATE OR REPLACE TABLE finops_gold.dim_platform AS
SELECT 'Foundry' AS platform_key, 'Azure AI Foundry' AS platform_name,
       'Consumption (tokens)' AS billing_model, 'token' AS native_unit,
       'TRUE' AS has_token_telemetry, 'TRUE' AS has_native_cost,
       'TRUE' AS is_variable_cost,
       'REAL - FOCUS 1.0r2 export + APIM gateway (Log Analytics)' AS data_source,
       '0.15' AS enterprise_discount_pct
UNION ALL SELECT 'GitHubCopilot', 'GitHub Copilot Enterprise',
       'Seats + premium requests', 'premium_request', 'FALSE', 'TRUE', 'TRUE',
       'REAL - GitHub seats + enhanced billing usage API', '0.05'
UNION ALL SELECT 'CopilotStudio', 'Microsoft Copilot Studio',
       'Copilot Credits', 'copilot_credit', 'FALSE', 'TRUE', 'TRUE',
       'REAL - FOCUS 1.0r2 export + Dataverse msdyn_aievent', '0.20'
UNION ALL SELECT 'M365Copilot', 'Microsoft 365 Copilot',
       'Per-seat licence + credits', 'seat_day', 'FALSE', 'FALSE', 'FALSE',
       'REAL - Graph reports + M365 Copilot Credits billing', '0.0'
""")

spark.sql("""
CREATE OR REPLACE TABLE finops_gold.dim_rate_card AS
SELECT rate_key, platform, unit_type, model, unit_price_usd, effective_from,
       currency, source, note
FROM finops_bronze.ref_rate_card
""")

# dim_date is derived, not landed: it spans exactly the fact's date range.
bounds = spark.sql("SELECT MIN(usage_date) lo, MAX(usage_date) hi "
                   "FROM finops_gold.fact_ai_usage").collect()[0]
spark.sql(f"""
CREATE OR REPLACE TABLE finops_gold.dim_date AS
SELECT CAST(d AS STRING)                              AS date_key,
       year(d)                                        AS year,
       concat('Q', quarter(d))                        AS quarter,
       month(d)                                       AS month,
       date_format(d, 'MMM yyyy')                     AS month_name,
       day(d)                                         AS day,
       date_format(d, 'EEE')                          AS day_name,
       CASE WHEN dayofweek(d) BETWEEN 2 AND 6 THEN 'TRUE' ELSE 'FALSE' END AS is_weekday,
       date_format(d, 'yyyy-MM')                      AS year_month
FROM (SELECT explode(sequence(to_date('{bounds.lo}'), to_date('{bounds.hi}'),
                              interval 1 day)) AS d)
""")

# ------------------------------------------------------- contract asserts ----
fact = spark.table("finops_gold.fact_ai_usage")
assert fact.columns == FACT_CONTRACT, f"gold fact breaks the CSV contract: {fact.columns}"

for fact_col, dim, dim_col in [
    ("identity_key", "dim_identity", "identity_key"),
    ("application_key", "dim_application", "application_key"),
    ("business_unit_key", "dim_business_unit", "business_unit_key"),
    ("environment_key", "dim_environment", "environment_key"),
    ("platform_key", "dim_platform", "platform_key"),
    ("usage_date", "dim_date", "date_key"),
]:
    orphans = spark.sql(f"""
        SELECT COUNT(*) FROM finops_gold.fact_ai_usage f
        WHERE f.{fact_col} <> '' AND NOT EXISTS (
            SELECT 1 FROM finops_gold.{dim} d WHERE d.{dim_col} = f.{fact_col})
    """).collect()[0][0]
    assert orphans == 0, f"{orphans} fact rows have no {dim} match on {fact_col}"

print(f"gold.fact_ai_usage rows: {fact.count()} — contract and dimension joins verified")

# Optional: export to the same CSV layout the PBIP reads, so the PoC model can
# be refreshed straight from a Fabric run without DirectLake.
# fact.toPandas().to_csv("/lakehouse/default/Files/gold/fact_ai_usage.csv", index=False)
