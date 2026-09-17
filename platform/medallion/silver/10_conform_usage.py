# Fabric notebook — SILVER: type, conform, resolve identity, allocate cost
# ---------------------------------------------------------------------------
# This is the layer that earns the platform its keep.
#
# THE CENTRAL RULE: THE PROVIDER'S BILL IS THE ONLY SOURCE OF DOLLARS.
#   * FOCUS charge lines carry authoritative cost, but no identity.
#   * APIM requests and Dataverse events carry identity, but no cost.
#   * Silver allocates the billed cost onto identities by each identity's share
#     of the billed unit (tokens per direction, credits), so Gold reconciles to
#     the invoice instead of re-deriving spend from a rate card.
# Where a provider bills no dollars at all (M365 seats, GitHub seats) the rate
# card is used and the row is flagged cost_is_estimated = TRUE. Modelled dollars
# are never relabelled as billed.
#
# Four platforms, four incompatible billing units, and only Foundry exposes
# tokens — so USD is the single conformed measure and unit_type stays a
# dimension with quantity keeping its native meaning.
#
# These statements are the Spark twin of platform/data-store/build_store.py.
# The table names, column names, and logic are identical so the pipeline can be
# reviewed and verified locally with no Fabric capacity.
# ---------------------------------------------------------------------------
spark.sql("CREATE DATABASE IF NOT EXISTS finops_silver")

# --------------------------------------------------------------- charges ----
# Type the FOCUS export and decode its two JSON columns (Tags, x_SkuDetails).
spark.sql("""
CREATE OR REPLACE TABLE finops_silver.cost_charge AS
SELECT
    substr(ChargePeriodStart, 1, 10)                                  AS charge_date,
    CASE ServiceName
        WHEN 'Azure OpenAI Service'     THEN 'Foundry'
        WHEN 'Microsoft Copilot Studio' THEN 'CopilotStudio'
        WHEN 'Microsoft Fabric'         THEN 'FabricCapacity'
        ELSE 'Unmapped'
    END                                                               AS platform_key,
    BillingAccountId                                                  AS billing_account_id,
    SubAccountId                                                      AS sub_account_id,
    ResourceId                                                        AS resource_id,
    ResourceName                                                      AS resource_name,
    x_ResourceGroupName                                               AS resource_group,
    ServiceName                                                       AS service_name,
    ServiceCategory                                                   AS service_category,
    ChargeCategory                                                    AS charge_category,
    x_SkuMeterCategory                                                AS meter_category,
    x_SkuMeterName                                                    AS meter_name,
    x_SkuMeterId                                                      AS meter_id,
    get_json_object(x_SkuDetails, '$.model')                          AS model_key,
    lower(coalesce(get_json_object(x_SkuDetails, '$.tokenType'), ''))  AS token_direction,
    CAST(ConsumedQuantity AS DOUBLE)                                  AS consumed_quantity,
    ConsumedUnit                                                      AS consumed_unit,
    CAST(PricingQuantity AS DOUBLE)                                   AS pricing_quantity,
    PricingUnit                                                       AS pricing_unit,
    CAST(BilledCost AS DOUBLE)                                        AS billed_cost_usd,
    CAST(EffectiveCost AS DOUBLE)                                     AS effective_cost_usd,
    CAST(ListCost AS DOUBLE)                                          AS list_cost_usd,
    CAST(ListCost AS DOUBLE) - CAST(BilledCost AS DOUBLE)             AS discount_usd,
    get_json_object(Tags, '$.app')                                    AS application_key,
    get_json_object(Tags, '$.bu')                                     AS business_unit_key,
    get_json_object(Tags, '$.env')                                    AS environment_key,
    x_CostCenter                                                      AS cost_center_key,
    RegionId                                                          AS region_id,
    BillingCurrency                                                   AS billing_currency,
    _source_api                                                       AS cost_contract
FROM finops_bronze.focus_cost
WHERE ChargeCategory = 'Usage'
""")

# -------------------------------------------------------------- requests ----
# Log Analytics hands everything over as text, with "" and the literal "None"
# standing in for null, so every numeric needs an explicit cast.
spark.sql("""
CREATE OR REPLACE TABLE finops_silver.gateway_request AS
SELECT
    substr(g.TimeGenerated, 1, 10)                          AS usage_date,
    g.TimeGenerated                                         AS request_ts,
    g.RequestId                                             AS request_id,
    coalesce(nullif(g.ClientId, ''), 'unknown')             AS identity_key,
    CASE
        WHEN nullif(g.ClientId, '') IS NULL        THEN 'Unattributed'
        WHEN i.is_human = 'TRUE'                   THEN 'Human'
        WHEN i.principal_type = 'ServicePrincipal' THEN 'ServicePrincipal'
        ELSE 'Application'
    END                                                     AS identity_class,
    coalesce(a.application_key, 'APP-UNKNOWN')              AS application_key,
    coalesce(i.home_business_unit_key, bo.business_unit_key,
             CASE WHEN nullif(g.ClientId, '') IS NULL THEN 'BU-UNALLOC'
                  ELSE a.owner_business_unit_key END,
             'BU-UNALLOC')                                  AS business_unit_key,
    coalesce(nullif(i.cost_center_key, ''), nullif(o.CostCenter, ''),
             nullif(g.CostCenterClaim, ''), '')             AS cost_center_key,
    coalesce(a.default_environment_key, 'ENV-UNK')          AS environment_key,
    g.ModelName                                             AS model_key,
    g.ModelVersion                                          AS model_version,
    g.DeploymentRegion                                      AS deployment_region,
    CAST(coalesce(nullif(nullif(g.PromptTokens, ''), 'None'), '0') AS BIGINT)       AS input_tokens,
    CAST(coalesce(nullif(nullif(g.CompletionTokens, ''), 'None'), '0') AS BIGINT)   AS output_tokens,
    CAST(coalesce(nullif(nullif(g.CachedPromptTokens, ''), 'None'), '0') AS BIGINT) AS cached_tokens,
    CASE WHEN g.IsError = 'True' THEN 'TRUE' ELSE 'FALSE' END AS is_error,
    g.StatusCode                                            AS status_code,
    CAST(coalesce(nullif(g.TotalLatencyMs, ''), '0') AS DOUBLE) AS latency_ms
FROM finops_bronze.apim_gateway_requests g
LEFT JOIN finops_bronze.ref_identity_map i
       ON i.identity_key = g.ClientId
LEFT JOIN finops_bronze.ref_app_inventory a
       ON a.azure_resource_name = replace(g.BackendId, '.openai.azure.com', '')
      AND a.azure_resource_name <> ''
-- The client registry is the fallback owner when a caller is not in the
-- identity map. It names the business unit, so conform that name to a key.
LEFT JOIN finops_bronze.apim_client_ownership o
       ON o.ClientId = g.ClientId AND o.ClientId <> ''
LEFT JOIN finops_bronze.ref_business_hierarchy bo
       ON bo.business_unit_name = o.BusinessUnit
""")

# ---------------------------------------------------------- studio events ----
spark.sql("""
CREATE OR REPLACE TABLE finops_silver.studio_event AS
SELECT
    substr(e.msdyn_eventtimestamp, 1, 10)              AS usage_date,
    e.msdyn_aieventid                                  AS event_id,
    coalesce(ag.agent_key, 'unknown')                  AS identity_key,
    coalesce(ag.agent_name, 'Unmapped agent')          AS agent_name,
    e._msdyn_botid_value                               AS bot_id,
    e._msdyn_environmentid_value                       AS environment_id,
    e.msdyn_eventtype                                  AS event_type,
    e.msdyn_billingtype                                AS billing_type,
    e.msdyn_channel                                    AS channel,
    e.msdyn_outcome                                    AS outcome,
    e.msdyn_conversationid                             AS conversation_id,
    CAST(e.msdyn_creditconsumed AS DOUBLE)             AS credits_consumed,
    coalesce(ag.owner_business_unit_key, 'BU-UNALLOC') AS business_unit_key,
    coalesce(ag.cost_center_key, '')                   AS cost_center_key
FROM finops_bronze.dataverse_msdyn_aievent e
LEFT JOIN finops_bronze.ref_agent_inventory ag ON ag.bot_id = e._msdyn_botid_value
""")

# ----------------------------------------------- Foundry cost allocation ----
# FOCUS knows the dollars per resource x meter x day; the gateway knows which
# identity burned which tokens. Split per direction, because input, output and
# cached tokens are priced differently.
spark.sql("""
CREATE OR REPLACE TABLE finops_silver.foundry_allocation AS
WITH charge AS (
    SELECT charge_date, application_key, model_key, token_direction,
           SUM(billed_cost_usd) AS billed_cost_usd
    FROM finops_silver.cost_charge
    WHERE platform_key = 'Foundry'
    GROUP BY 1, 2, 3, 4
),
day_total AS (
    SELECT usage_date, application_key, model_key,
           SUM(input_tokens)  AS total_input,
           SUM(output_tokens) AS total_output,
           SUM(cached_tokens) AS total_cached
    FROM finops_silver.gateway_request
    GROUP BY 1, 2, 3
),
grouped AS (
    SELECT usage_date, application_key, model_key, identity_key, identity_class,
           business_unit_key, cost_center_key, environment_key, is_error,
           SUM(input_tokens)  AS input_tokens,
           SUM(output_tokens) AS output_tokens,
           SUM(cached_tokens) AS cached_tokens,
           COUNT(*)           AS requests,
           AVG(latency_ms)    AS latency_ms
    FROM finops_silver.gateway_request
    GROUP BY 1, 2, 3, 4, 5, 6, 7, 8, 9
)
SELECT g.*,
       coalesce(ci.billed_cost_usd, 0)
         * (CASE WHEN d.total_input  > 0 THEN g.input_tokens  / d.total_input  ELSE 0 END)
     + coalesce(co.billed_cost_usd, 0)
         * (CASE WHEN d.total_output > 0 THEN g.output_tokens / d.total_output ELSE 0 END)
     + coalesce(cc.billed_cost_usd, 0)
         * (CASE WHEN d.total_cached > 0 THEN g.cached_tokens / d.total_cached ELSE 0 END)
       AS cost_usd
FROM grouped g
JOIN day_total d
  ON d.usage_date = g.usage_date AND d.application_key = g.application_key
 AND d.model_key = g.model_key
LEFT JOIN charge ci ON ci.charge_date = g.usage_date AND ci.application_key = g.application_key
                   AND ci.model_key = g.model_key AND ci.token_direction = 'input'
LEFT JOIN charge co ON co.charge_date = g.usage_date AND co.application_key = g.application_key
                   AND co.model_key = g.model_key AND co.token_direction = 'output'
LEFT JOIN charge cc ON cc.charge_date = g.usage_date AND cc.application_key = g.application_key
                   AND cc.model_key = g.model_key AND cc.token_direction = 'cached'
""")

# ---------------------------------------- Copilot Studio cost allocation ----
# Same pattern, different billed unit: the PAYG meter bills credits for the
# whole tenant per day, Dataverse says which agent burned them.
spark.sql("""
CREATE OR REPLACE TABLE finops_silver.studio_allocation AS
WITH charge AS (
    SELECT charge_date, SUM(billed_cost_usd) AS billed_cost_usd
    FROM finops_silver.cost_charge
    WHERE platform_key = 'CopilotStudio'
    GROUP BY 1
),
day_total AS (
    SELECT usage_date, SUM(credits_consumed) AS total_credits
    FROM finops_silver.studio_event
    GROUP BY 1
),
grouped AS (
    SELECT usage_date, identity_key, business_unit_key, cost_center_key,
           SUM(credits_consumed) AS credits_consumed,
           COUNT(*)              AS events
    FROM finops_silver.studio_event
    GROUP BY 1, 2, 3, 4
)
SELECT g.*,
       coalesce(c.billed_cost_usd, 0)
         * (CASE WHEN d.total_credits > 0 THEN g.credits_consumed / d.total_credits ELSE 0 END)
       AS cost_usd
FROM grouped g
JOIN day_total d ON d.usage_date = g.usage_date
LEFT JOIN charge c ON c.charge_date = g.usage_date
""")

# ------------------------------------------------------- conformed usage ----
spark.sql("""
CREATE OR REPLACE TABLE finops_silver.usage_conformed AS
    -- Foundry: billed dollars, allocated by token share
    SELECT usage_date, 'Foundry' AS platform_key, identity_key, model_key,
           cost_center_key, 'token' AS unit_type,
           input_tokens + output_tokens + cached_tokens AS quantity,
           input_tokens, output_tokens, cached_tokens, requests,
           cost_usd, 'FALSE' AS cost_is_estimated, is_error, latency_ms,
           application_key, environment_key, business_unit_key,
           'focus_token_allocation' AS cost_method
    FROM finops_silver.foundry_allocation

    UNION ALL
    -- Copilot Studio: billed dollars, allocated by credit share
    SELECT usage_date, 'CopilotStudio', identity_key, '',
           cost_center_key, 'copilot_credit',
           credits_consumed, 0, 0, 0, events,
           cost_usd, 'FALSE', 'FALSE', NULL,
           'APP-STUDIO', 'ENV-PROD', business_unit_key,
           'focus_credit_allocation'
    FROM finops_silver.studio_allocation

    UNION ALL
    -- M365 Copilot seats: a fixed licence, never billed as usage -> rate card
    SELECT s.snapshotDate, 'M365Copilot', i.identity_key, '',
           i.cost_center_key, 'seat_day',
           1, 0, 0, 0, 0,
           (SELECT CAST(unit_price_usd AS DOUBLE) FROM finops_bronze.ref_rate_card
             WHERE platform = 'M365Copilot' AND unit_type = 'seat_day'),
           'TRUE', 'FALSE', NULL,
           'APP-M365', 'ENV-PROD', i.home_business_unit_key,
           'rate_card'
    FROM finops_bronze.m365_copilot_seats s
    JOIN finops_bronze.ref_identity_map i ON i.upn = s.userPrincipalName
    WHERE s.capabilityStatus = 'Enabled'

    UNION ALL
    -- M365 Copilot Credits: billed by the provider in dollars
    SELECT c.usageDate, 'M365Copilot', i.identity_key, '',
           i.cost_center_key, 'copilot_credit',
           CAST(c.creditsConsumed AS DOUBLE), 0, 0, 0, 0,
           CAST(c.costUsd AS DOUBLE), 'FALSE', 'FALSE', NULL,
           'APP-M365', 'ENV-PROD', i.home_business_unit_key,
           'provider_billed'
    FROM finops_bronze.m365_copilot_credits c
    JOIN finops_bronze.ref_identity_map i ON i.upn = c.consumerId

    UNION ALL
    -- GitHub Copilot seats: fixed licence -> rate card
    SELECT g.snapshot_date, 'GitHubCopilot', i.identity_key, '',
           i.cost_center_key, 'seat_day',
           1, 0, 0, 0, 0,
           (SELECT CAST(unit_price_usd AS DOUBLE) FROM finops_bronze.ref_rate_card
             WHERE platform = 'GitHubCopilot' AND unit_type = 'seat_day'),
           'TRUE', 'FALSE', NULL,
           'APP-GHCP', 'ENV-PROD', i.home_business_unit_key,
           'rate_card'
    FROM finops_bronze.ghc_seats g
    JOIN finops_bronze.ref_identity_map i ON i.github_login = g.assignee_login

    UNION ALL
    -- GitHub premium requests: netAmount is the billed overage, so use it
    SELECT u.date, 'GitHubCopilot', i.identity_key, u.model,
           i.cost_center_key, 'premium_request',
           CAST(u.quantity AS DOUBLE), 0, 0, 0, CAST(u.quantity AS BIGINT),
           CAST(u.netAmount AS DOUBLE), 'FALSE', 'FALSE', NULL,
           'APP-GHCP', 'ENV-PROD', i.home_business_unit_key,
           'provider_billed'
    FROM finops_bronze.ghc_premium_usage u
    JOIN finops_bronze.ref_identity_map i ON i.github_login = u.username

    UNION ALL
    -- M365 Copilot activity. The usage report returns last-activity DATES, not
    -- counts or tokens, so an active day is the only honest unit. Cost is zero
    -- because the seat is already paid for: this row exists to prove the seat
    -- is used, and its ABSENCE is what makes a seat reclaimable.
    SELECT u.`Report Refresh Date`, 'M365Copilot', i.identity_key, '',
           i.cost_center_key, 'active_day',
           1, 0, 0, 0, 0,
           0.0, 'FALSE', 'FALSE', NULL,
           'APP-M365', 'ENV-PROD', i.home_business_unit_key,
           'usage_signal'
    FROM finops_bronze.m365_copilot_usage u
    JOIN finops_bronze.ref_identity_map i ON i.upn = u.`User Principal Name`
    WHERE u.`Last Activity Date` <> ''

    UNION ALL
    -- GitHub Copilot activity, from the seat's last_activity_at. Requires IDE
    -- telemetry to be on, otherwise every seat looks idle.
    SELECT g.snapshot_date, 'GitHubCopilot', i.identity_key, '',
           i.cost_center_key, 'active_day',
           1, 0, 0, 0, 0,
           0.0, 'FALSE', 'FALSE', NULL,
           'APP-GHCP', 'ENV-PROD', i.home_business_unit_key,
           'usage_signal'
    FROM finops_bronze.ghc_seats g
    JOIN finops_bronze.ref_identity_map i ON i.github_login = g.assignee_login
    WHERE g.last_activity_at <> ''
""")

# --------------------------------------------------------- reconciliation ----
# Allocated cost must equal the invoice. If it does not, the attribution is
# wrong and the pipeline should fail here rather than publish a plausible
# looking dashboard that does not tie to the bill.
for platform in ["Foundry", "CopilotStudio"]:
    billed = spark.sql(f"""
        SELECT coalesce(SUM(billed_cost_usd), 0) FROM finops_silver.cost_charge
        WHERE platform_key = '{platform}'""").collect()[0][0]
    allocated = spark.sql(f"""
        SELECT coalesce(SUM(cost_usd), 0) FROM finops_silver.usage_conformed
        WHERE platform_key = '{platform}' AND cost_is_estimated = 'FALSE'""").collect()[0][0]
    print(f"{platform}: billed {billed:.4f} vs allocated {allocated:.4f}")
    assert abs(billed - allocated) < 0.01, f"{platform} allocation does not reconcile"

print(f"silver.usage_conformed rows: {spark.table('finops_silver.usage_conformed').count()}")
