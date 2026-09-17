#!/usr/bin/env python3
"""
Run the AI FinOps medallion end to end into a portable SQLite database.

    RAW/BRONZE  platform/fabric/bronze_out/*.csv   (source-faithful extracts)
        |
    SILVER      typed, conformed, identity-resolved, cost-allocated
        |
    GOLD        fact_ai_usage + dim_* (the exact star the semantic model binds)

This is the Fabric-free twin of the notebooks in platform/medallion/. The
transformations, table names, and column contracts are identical; only the
engine differs (SQLite here, Spark there), so the logic can be reviewed and
verified on any machine with no cloud, no licence, and no dependencies.

The central design rule: THE PROVIDER'S BILL IS THE ONLY SOURCE OF DOLLARS.
  * FOCUS charge lines carry the authoritative cost, but no identity.
  * APIM gateway requests and Dataverse events carry identity, but no cost.
  * Silver allocates the billed cost onto identities by their measured share of
    the billed unit (tokens, credits), so Gold reconciles to the invoice.
Where a provider bills no dollars at all (M365 seats, GitHub seats) the rate
card is used and the row is flagged cost_is_estimated = TRUE. Modelled dollars
are never relabelled as billed.

Run:
  python3 platform/fabric/gen_bronze_data.py      # (re)generate the raw extracts
  python3 platform/data-store/build_store.py      # -> platform/data-store/finops.db
  python3 platform/data-store/build_store.py --query "SELECT ..."
"""
import argparse
import csv
import glob
import os
import sqlite3

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, ".."))
BRONZE = os.path.join(ROOT, "fabric", "bronze_out")
DB = os.path.join(HERE, "finops.db")

# The Gold fact column contract. It is identical to the header of
# AIFinOps.SemanticModel/data/fact_ai_usage.csv, because the TMDL partition
# casts are keyed to these exact names. Drift here breaks the semantic model,
# so the build asserts it instead of discovering it in Power BI.
FACT_CONTRACT = [
    "usage_date", "platform_key", "identity_key", "model_key", "cost_center_key",
    "unit_type", "quantity", "input_tokens", "output_tokens", "cached_tokens",
    "requests", "cost_usd", "cost_is_estimated", "is_error", "latency_ms",
    "application_key", "environment_key", "business_unit_key",
]

DIM_CONTRACT = {
    "dim_date": ["date_key", "year", "quarter", "month", "month_name", "day",
                 "day_name", "is_weekday", "year_month"],
    "dim_platform": ["platform_key", "platform_name", "billing_model", "native_unit",
                     "has_token_telemetry", "has_native_cost", "is_variable_cost",
                     "data_source", "enterprise_discount_pct"],
    "dim_identity": ["identity_key", "display_name", "principal_type", "upn",
                     "github_login", "team", "business_unit", "cost_center_key",
                     "identity_class", "is_human", "home_business_unit_key"],
    "dim_application": ["application_key", "application_name", "application_type",
                        "owner_business_unit_key", "owner_upn",
                        "default_environment_key", "criticality", "is_mock"],
    "dim_business_unit": ["business_unit_key", "business_unit_name", "division",
                          "monthly_budget_usd", "executive_owner", "is_mock_budget"],
    "dim_cost_center": ["cost_center_key", "cost_center_name", "business_unit", "owner_upn"],
    "dim_environment": ["environment_key", "environment_name", "is_production", "sla_tier"],
    "dim_model": ["model_key", "model_name", "model_version", "provider", "modality"],
    "dim_rate_card": ["rate_key", "platform", "unit_type", "model", "unit_price_usd",
                      "effective_from", "currency", "source", "note"],
}

# (product, category, field, description, source, grain) — the machine-readable
# companion to docs/extractable-data-by-product.md, named after the RAW fields.
CATALOG = [
    ("Foundry/AOAI", "cost", "BilledCost/EffectiveCost", "Authoritative billed + amortized cost", "Cost Management FOCUS 1.0r2", "charge line"),
    ("Foundry/AOAI", "cost", "ListCost/ContractedUnitPrice", "List vs negotiated price (discount)", "Cost Management FOCUS 1.0r2", "charge line"),
    ("Foundry/AOAI", "usage", "ConsumedQuantity/ConsumedUnit", "Provider-measured tokens", "Cost Management FOCUS 1.0r2", "charge line"),
    ("Foundry/AOAI", "usage", "PricingQuantity/PricingUnit", "Billable units (1K token blocks)", "Cost Management FOCUS 1.0r2", "charge line"),
    ("Foundry/AOAI", "dimension", "ResourceId/x_ResourceGroupName", "Billed resource", "Cost Management FOCUS 1.0r2", "resource"),
    ("Foundry/AOAI", "dimension", "Tags", "app/bu/env attribution tags", "Cost Management FOCUS 1.0r2", "resource"),
    ("Foundry/AOAI", "dimension", "x_SkuMeterName/x_SkuDetails", "Meter, model, token direction", "Cost Management FOCUS 1.0r2", "charge line"),
    ("Foundry/AOAI", "identity", "ClientId/Oid", "Calling SP or user (the only identity path)", "APIM gateway -> Log Analytics", "request"),
    ("Foundry/AOAI", "identity", "BusinessUnitClaim/CostCenterClaim", "JWT chargeback claims", "APIM gateway -> Log Analytics", "request"),
    ("Foundry/AOAI", "usage", "PromptTokens/CompletionTokens/CachedPromptTokens", "Per-request token split", "APIM gateway -> Log Analytics", "request"),
    ("Foundry/AOAI", "usage", "TotalLatencyMs/StatusCode/IsError", "Performance and failures", "APIM gateway -> Log Analytics", "request"),
    ("Foundry/AOAI", "dimension", "ModelName/ModelVersion/DeploymentRegion", "What served the call", "APIM gateway -> Log Analytics", "request"),
    ("Foundry/AOAI", "usage", "IsStreaming/OperationName", "Call shape", "APIM gateway -> Log Analytics", "request"),
    ("Foundry/AOAI", "dimension", "AppName/Team/CostCenter", "Client registry ownership", "Log Analytics ApimClientOwnership_CL", "client"),
    ("Copilot Studio", "cost", "BilledCost", "PAYG credit charge (authoritative $)", "Cost Management FOCUS 1.0r2", "day"),
    ("Copilot Studio", "cost", "msdyn_creditconsumed", "Credits, already net of zero-rating", "Dataverse msdyn_aievent", "event"),
    ("Copilot Studio", "dimension", "msdyn_billingtype", "Billed vs zero-rated", "Dataverse msdyn_aievent", "event"),
    ("Copilot Studio", "dimension", "msdyn_eventtype", "Classic/generative answer, agent action", "Dataverse msdyn_aievent", "event"),
    ("Copilot Studio", "identity", "_msdyn_botid_value", "The agent consuming credits", "Dataverse msdyn_aievent", "agent"),
    ("Copilot Studio", "identity", "_msdyn_environmentid_value", "Power Platform environment", "Dataverse msdyn_aievent", "environment"),
    ("Copilot Studio", "usage", "msdyn_conversationid/msdyn_sessionid", "Conversation grouping", "Dataverse msdyn_aievent", "conversation"),
    ("Copilot Studio", "usage", "msdyn_outcome", "Resolved/escalated/abandoned", "Dataverse msdyn_aievent", "conversation"),
    ("Copilot Studio", "dimension", "msdyn_channel", "Teams, web chat, custom", "Dataverse msdyn_aievent", "conversation"),
    ("Copilot Studio", "usage", "msdyn_eventtimestamp", "When the credit was burned", "Dataverse msdyn_aievent", "event"),
    ("M365 Copilot", "identity", "User Principal Name", "The licensed user", "Graph usage report", "user/day"),
    ("M365 Copilot", "usage", "Last Activity Date", "Overall last use (blank = idle seat)", "Graph usage report", "user/day"),
    ("M365 Copilot", "usage", "<App> Copilot Last Activity Date", "Teams/Word/Excel/PowerPoint/Outlook/OneNote/Loop/Chat", "Graph usage report", "user/app"),
    ("M365 Copilot", "dimension", "Report Refresh Date/Report Period", "Report window", "Graph usage report", "report"),
    ("M365 Copilot", "cost", "skuId/skuPartNumber", "Licence held (seat = fixed cost)", "Graph subscribedSkus", "user"),
    ("M365 Copilot", "dimension", "capabilityStatus/provisioningStatus", "Seat state", "Graph assignedLicenses", "user"),
    ("M365 Copilot", "usage", "assignedDateTime", "Licence grant date", "Graph assignedLicenses", "user"),
    ("M365 Copilot", "cost", "consumedUnits/prepaidUnitsEnabled", "Seats used vs purchased", "Graph subscribedSkus", "tenant"),
    ("M365 Copilot", "cost", "creditsConsumed/costUsd", "Copilot Credits (PAYG)", "M365 billing", "day/consumer"),
    ("M365 Copilot", "dimension", "capability", "Cowork, Autopilot, agent action", "M365 billing", "day/consumer"),
    ("M365 Copilot", "identity", "consumerId/consumerType", "Who burned the credits", "M365 billing", "day/consumer"),
    ("GitHub Copilot", "identity", "assignee_login/assignee_id", "GitHub user (maps to UPN in Silver)", "/orgs/{org}/copilot/billing/seats", "seat"),
    ("GitHub Copilot", "usage", "last_activity_at", "Last Copilot use (blank = idle seat)", "/orgs/{org}/copilot/billing/seats", "seat"),
    ("GitHub Copilot", "dimension", "last_activity_editor", "vscode/VS/JetBrains", "/orgs/{org}/copilot/billing/seats", "seat"),
    ("GitHub Copilot", "dimension", "plan_type/assigning_team", "Business vs enterprise, team", "/orgs/{org}/copilot/billing/seats", "seat"),
    ("GitHub Copilot", "usage", "created_at/pending_cancellation_date", "Seat lifecycle", "/orgs/{org}/copilot/billing/seats", "seat"),
    ("GitHub Copilot", "cost", "netAmount/grossAmount/discountAmount", "Billed premium-request overage", "/settings/billing/usage", "day/user"),
    ("GitHub Copilot", "usage", "quantity/unitType", "Premium requests consumed", "/settings/billing/usage", "day/user"),
    ("GitHub Copilot", "dimension", "model/modelMultiplier", "Model weight (code review 13x)", "/settings/billing/usage", "request"),
    ("GitHub Copilot", "dimension", "repositoryName/organizationName", "Where it was used", "/settings/billing/usage", "day/repo"),
    ("Microsoft Fabric", "cost", "BilledCost", "Capacity $ (platform self-cost)", "Cost Management FOCUS 1.0r2", "day"),
    ("Microsoft Fabric", "usage", "ConsumedQuantity/ConsumedUnit", "Capacity hours", "Cost Management FOCUS 1.0r2", "day"),
    ("Microsoft Fabric", "dimension", "x_SkuMeterSubcategory", "F-SKU tier", "Cost Management FOCUS 1.0r2", "capacity"),
    ("Reference", "dimension", "identity_key/upn/github_login", "Cross-platform identity resolution", "Entra + manual map", "identity"),
    ("Reference", "dimension", "gateway_client_id/azure_resource_name", "Client + resource to application", "CMDB", "application"),
    ("Reference", "dimension", "bot_id/owner_business_unit_key", "Agent ownership", "Copilot Studio inventory", "agent"),
    ("Reference", "cost", "unit_price_usd", "Rate card (only where no $ is billed)", "EA price sheet / list price", "platform/unit"),
    ("Reference", "dimension", "monthly_budget_usd", "Budget for variance", "Finance master data", "business unit"),
]

# --------------------------------------------------------------------------- #
#  SILVER                                                                      #
# --------------------------------------------------------------------------- #
SILVER_SQL = """
-- ---------------------------------------------------------------- charges --
-- Type the FOCUS export and decode the two JSON columns (Tags, x_SkuDetails).
-- Nothing is renamed for cosmetics: every alias below is a concept Gold needs.
DROP TABLE IF EXISTS silver_cost_charge;
CREATE TABLE silver_cost_charge AS
SELECT
    substr("ChargePeriodStart", 1, 10)                              AS charge_date,
    CASE "ServiceName"
        WHEN 'Azure OpenAI Service'     THEN 'Foundry'
        WHEN 'Microsoft Copilot Studio' THEN 'CopilotStudio'
        WHEN 'Microsoft Fabric'         THEN 'FabricCapacity'
        ELSE 'Unmapped'
    END                                                             AS platform_key,
    "BillingAccountId"                                              AS billing_account_id,
    "SubAccountId"                                                  AS sub_account_id,
    "ResourceId"                                                    AS resource_id,
    "ResourceName"                                                  AS resource_name,
    "x_ResourceGroupName"                                           AS resource_group,
    "ServiceName"                                                   AS service_name,
    "ServiceCategory"                                               AS service_category,
    "ChargeCategory"                                                AS charge_category,
    "x_SkuMeterCategory"                                            AS meter_category,
    "x_SkuMeterName"                                                AS meter_name,
    "x_SkuMeterId"                                                  AS meter_id,
    json_extract("x_SkuDetails", '$.model')                         AS model_key,
    lower(COALESCE(json_extract("x_SkuDetails", '$.tokenType'), '')) AS token_direction,
    CAST("ConsumedQuantity" AS REAL)                                AS consumed_quantity,
    "ConsumedUnit"                                                  AS consumed_unit,
    CAST("PricingQuantity" AS REAL)                                 AS pricing_quantity,
    "PricingUnit"                                                   AS pricing_unit,
    CAST("BilledCost" AS REAL)                                      AS billed_cost_usd,
    CAST("EffectiveCost" AS REAL)                                   AS effective_cost_usd,
    CAST("ListCost" AS REAL)                                        AS list_cost_usd,
    CAST("ListCost" AS REAL) - CAST("BilledCost" AS REAL)           AS discount_usd,
    json_extract("Tags", '$.app')                                   AS application_key,
    json_extract("Tags", '$.bu')                                    AS business_unit_key,
    json_extract("Tags", '$.env')                                   AS environment_key,
    "x_CostCenter"                                                  AS cost_center_key,
    "RegionId"                                                      AS region_id,
    "BillingCurrency"                                               AS billing_currency,
    "_source_api"                                                   AS cost_contract
FROM bronze_focus_cost
WHERE "ChargeCategory" = 'Usage';

-- --------------------------------------------------------------- requests --
-- Log Analytics hands everything over as text, with "" and the literal "None"
-- standing in for null, so every numeric needs an explicit cast. A missing
-- ClientId is a real condition (absent JWT claim), not dirty data: those
-- requests burned billed tokens and must stay visible as unallocated spend.
DROP TABLE IF EXISTS silver_gateway_request;
CREATE TABLE silver_gateway_request AS
SELECT
    substr(g."TimeGenerated", 1, 10)                       AS usage_date,
    g."TimeGenerated"                                      AS request_ts,
    g."RequestId"                                          AS request_id,
    COALESCE(NULLIF(g."ClientId", ''), 'unknown')          AS identity_key,
    CASE
        WHEN NULLIF(g."ClientId", '') IS NULL       THEN 'Unattributed'
        WHEN i.is_human = 'TRUE'                    THEN 'Human'
        WHEN i.principal_type = 'ServicePrincipal'  THEN 'ServicePrincipal'
        ELSE 'Application'
    END                                                    AS identity_class,
    COALESCE(a.application_key, 'APP-UNKNOWN')             AS application_key,
    coalesce(i.home_business_unit_key, bo.business_unit_key,
             CASE WHEN NULLIF(g."ClientId", '') IS NULL THEN 'BU-UNALLOC'
                  ELSE a.owner_business_unit_key END,
             'BU-UNALLOC')                                 AS business_unit_key,
    COALESCE(NULLIF(i.cost_center_key, ''), NULLIF(o."CostCenter", ''),
             NULLIF(g."CostCenterClaim", ''), '')          AS cost_center_key,
    COALESCE(a.default_environment_key, 'ENV-UNK')         AS environment_key,
    g."ModelName"                                          AS model_key,
    g."ModelVersion"                                       AS model_version,
    g."DeploymentRegion"                                   AS deployment_region,
    CAST(COALESCE(NULLIF(NULLIF(g."PromptTokens", ''), 'None'), '0') AS INTEGER)       AS input_tokens,
    CAST(COALESCE(NULLIF(NULLIF(g."CompletionTokens", ''), 'None'), '0') AS INTEGER)   AS output_tokens,
    CAST(COALESCE(NULLIF(NULLIF(g."CachedPromptTokens", ''), 'None'), '0') AS INTEGER) AS cached_tokens,
    CASE WHEN g."IsError" = 'True' THEN 'TRUE' ELSE 'FALSE' END AS is_error,
    g."StatusCode"                                         AS status_code,
    CAST(COALESCE(NULLIF(g."TotalLatencyMs", ''), '0') AS REAL) AS latency_ms
FROM bronze_apim_gateway_requests g
LEFT JOIN bronze_ref_identity_map i
       ON i.identity_key = g."ClientId"
LEFT JOIN bronze_ref_app_inventory a
       ON a.azure_resource_name = replace(g."BackendId", '.openai.azure.com', '')
      AND a.azure_resource_name <> ''
-- The client registry is the fallback owner when a caller is not in the
-- identity map. It names the business unit, so conform that name to a key.
LEFT JOIN bronze_apim_client_ownership o
       ON o."ClientId" = g."ClientId" AND o."ClientId" <> ''
LEFT JOIN bronze_ref_business_hierarchy bo
       ON bo.business_unit_name = o."BusinessUnit";

-- ----------------------------------------------------------- studio events --
-- msdyn_creditconsumed is already net of zero-rating, so it is used directly
-- and never recomputed from an action-type rate table.
DROP TABLE IF EXISTS silver_studio_event;
CREATE TABLE silver_studio_event AS
SELECT
    substr(e."msdyn_eventtimestamp", 1, 10)         AS usage_date,
    e."msdyn_aieventid"                             AS event_id,
    COALESCE(ag.agent_key, 'unknown')               AS identity_key,
    COALESCE(ag.agent_name, 'Unmapped agent')       AS agent_name,
    e."_msdyn_botid_value"                          AS bot_id,
    e."_msdyn_environmentid_value"                  AS environment_id,
    e."msdyn_eventtype"                             AS event_type,
    e."msdyn_billingtype"                           AS billing_type,
    e."msdyn_channel"                               AS channel,
    e."msdyn_outcome"                               AS outcome,
    e."msdyn_conversationid"                        AS conversation_id,
    CAST(e."msdyn_creditconsumed" AS REAL)          AS credits_consumed,
    COALESCE(ag.owner_business_unit_key, 'BU-UNALLOC') AS business_unit_key,
    COALESCE(ag.cost_center_key, '')                AS cost_center_key
FROM bronze_dataverse_msdyn_aievent e
LEFT JOIN bronze_ref_agent_inventory ag ON ag.bot_id = e."_msdyn_botid_value";

-- -------------------------------------------------- Foundry cost allocation --
-- The whole point of the platform. FOCUS knows the dollars per resource x meter
-- x day; the gateway knows which identity burned which tokens. Splitting the
-- billed cost by each identity's share of the billed tokens -- per direction,
-- because input, output and cached tokens are priced differently -- attributes
-- real invoiced money to people and services without inventing a rate.
DROP TABLE IF EXISTS silver_foundry_allocation;
CREATE TABLE silver_foundry_allocation AS
WITH charge AS (
    SELECT charge_date, application_key, model_key, token_direction,
           SUM(billed_cost_usd) AS billed_cost_usd
    FROM silver_cost_charge
    WHERE platform_key = 'Foundry'
    GROUP BY 1, 2, 3, 4
),
day_total AS (
    SELECT usage_date, application_key, model_key,
           SUM(input_tokens)  AS total_input,
           SUM(output_tokens) AS total_output,
           SUM(cached_tokens) AS total_cached
    FROM silver_gateway_request
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
    FROM silver_gateway_request
    GROUP BY 1, 2, 3, 4, 5, 6, 7, 8, 9
)
SELECT g.*,
       COALESCE(ci.billed_cost_usd, 0)
         * (CASE WHEN d.total_input  > 0 THEN g.input_tokens  * 1.0 / d.total_input  ELSE 0 END)
     + COALESCE(co.billed_cost_usd, 0)
         * (CASE WHEN d.total_output > 0 THEN g.output_tokens * 1.0 / d.total_output ELSE 0 END)
     + COALESCE(cc.billed_cost_usd, 0)
         * (CASE WHEN d.total_cached > 0 THEN g.cached_tokens * 1.0 / d.total_cached ELSE 0 END)
       AS cost_usd
FROM grouped g
JOIN day_total d
  ON d.usage_date = g.usage_date
 AND d.application_key = g.application_key
 AND d.model_key = g.model_key
LEFT JOIN charge ci ON ci.charge_date = g.usage_date AND ci.application_key = g.application_key
                   AND ci.model_key = g.model_key AND ci.token_direction = 'input'
LEFT JOIN charge co ON co.charge_date = g.usage_date AND co.application_key = g.application_key
                   AND co.model_key = g.model_key AND co.token_direction = 'output'
LEFT JOIN charge cc ON cc.charge_date = g.usage_date AND cc.application_key = g.application_key
                   AND cc.model_key = g.model_key AND cc.token_direction = 'cached';

-- ------------------------------------------- Copilot Studio cost allocation --
-- Same pattern, different billed unit: the PAYG meter bills credits for the
-- whole tenant per day, Dataverse says which agent burned them.
DROP TABLE IF EXISTS silver_studio_allocation;
CREATE TABLE silver_studio_allocation AS
WITH charge AS (
    SELECT charge_date, SUM(billed_cost_usd) AS billed_cost_usd
    FROM silver_cost_charge
    WHERE platform_key = 'CopilotStudio'
    GROUP BY 1
),
day_total AS (
    SELECT usage_date, SUM(credits_consumed) AS total_credits
    FROM silver_studio_event
    GROUP BY 1
),
grouped AS (
    SELECT usage_date, identity_key, business_unit_key, cost_center_key,
           SUM(credits_consumed) AS credits_consumed,
           COUNT(*)              AS events
    FROM silver_studio_event
    GROUP BY 1, 2, 3, 4
)
SELECT g.*,
       COALESCE(c.billed_cost_usd, 0)
         * (CASE WHEN d.total_credits > 0 THEN g.credits_consumed / d.total_credits ELSE 0 END)
       AS cost_usd
FROM grouped g
JOIN day_total d ON d.usage_date = g.usage_date
LEFT JOIN charge c ON c.charge_date = g.usage_date;

-- ------------------------------------------------------- conformed usage --
-- Four platforms, four incompatible billing units. USD is the only measure that
-- reconciles, so unit_type stays a dimension and quantity keeps its native
-- meaning. cost_is_estimated marks the rows the provider never billed in
-- dollars, so modelled spend can always be separated from invoiced spend.
DROP TABLE IF EXISTS silver_usage_conformed;
CREATE TABLE silver_usage_conformed AS
    -- Foundry: billed dollars, allocated by token share
    SELECT usage_date, 'Foundry' AS platform_key, identity_key, model_key,
           cost_center_key, 'token' AS unit_type,
           input_tokens + output_tokens + cached_tokens AS quantity,
           input_tokens, output_tokens, cached_tokens, requests,
           cost_usd, 'FALSE' AS cost_is_estimated, is_error, latency_ms,
           application_key, environment_key, business_unit_key,
           'focus_token_allocation' AS cost_method
    FROM silver_foundry_allocation

    UNION ALL
    -- Copilot Studio: billed dollars, allocated by credit share
    SELECT usage_date, 'CopilotStudio', identity_key, '',
           cost_center_key, 'copilot_credit',
           credits_consumed, 0, 0, 0, events,
           cost_usd, 'FALSE', 'FALSE', NULL,
           'APP-STUDIO', 'ENV-PROD', business_unit_key,
           'focus_credit_allocation'
    FROM silver_studio_allocation

    UNION ALL
    -- M365 Copilot seats: a fixed licence, never billed as usage -> rate card
    SELECT s."snapshotDate", 'M365Copilot', i.identity_key, '',
           i.cost_center_key, 'seat_day',
           1, 0, 0, 0, 0,
           (SELECT CAST(unit_price_usd AS REAL) FROM bronze_ref_rate_card
             WHERE platform = 'M365Copilot' AND unit_type = 'seat_day'),
           'TRUE', 'FALSE', NULL,
           'APP-M365', 'ENV-PROD', i.home_business_unit_key,
           'rate_card'
    FROM bronze_m365_copilot_seats s
    JOIN bronze_ref_identity_map i ON i.upn = s."userPrincipalName"
    WHERE s."capabilityStatus" = 'Enabled'

    UNION ALL
    -- M365 Copilot Credits: billed by the provider in dollars
    SELECT c."usageDate", 'M365Copilot', i.identity_key, '',
           i.cost_center_key, 'copilot_credit',
           CAST(c."creditsConsumed" AS REAL), 0, 0, 0, 0,
           CAST(c."costUsd" AS REAL), 'FALSE', 'FALSE', NULL,
           'APP-M365', 'ENV-PROD', i.home_business_unit_key,
           'provider_billed'
    FROM bronze_m365_copilot_credits c
    JOIN bronze_ref_identity_map i ON i.upn = c."consumerId"

    UNION ALL
    -- GitHub Copilot seats: fixed licence -> rate card
    SELECT g."snapshot_date", 'GitHubCopilot', i.identity_key, '',
           i.cost_center_key, 'seat_day',
           1, 0, 0, 0, 0,
           (SELECT CAST(unit_price_usd AS REAL) FROM bronze_ref_rate_card
             WHERE platform = 'GitHubCopilot' AND unit_type = 'seat_day'),
           'TRUE', 'FALSE', NULL,
           'APP-GHCP', 'ENV-PROD', i.home_business_unit_key,
           'rate_card'
    FROM bronze_ghc_seats g
    JOIN bronze_ref_identity_map i ON i.github_login = g."assignee_login"

    UNION ALL
    -- GitHub premium requests: netAmount is the billed overage, so use it
    SELECT u."date", 'GitHubCopilot', i.identity_key, u."model",
           i.cost_center_key, 'premium_request',
           CAST(u."quantity" AS REAL), 0, 0, 0, CAST(u."quantity" AS INTEGER),
           CAST(u."netAmount" AS REAL), 'FALSE', 'FALSE', NULL,
           'APP-GHCP', 'ENV-PROD', i.home_business_unit_key,
           'provider_billed'
    FROM bronze_ghc_premium_usage u
    JOIN bronze_ref_identity_map i ON i.github_login = u."username"

    UNION ALL
    -- M365 Copilot activity. The usage report returns last-activity DATES, not
    -- counts or tokens, so an active day is the only honest unit. Cost is zero
    -- because the seat is already paid for: this row exists to prove the seat
    -- is used, and its ABSENCE is what makes a seat reclaimable.
    SELECT u."Report Refresh Date", 'M365Copilot', i.identity_key, '',
           i.cost_center_key, 'active_day',
           1, 0, 0, 0, 0,
           0.0, 'FALSE', 'FALSE', NULL,
           'APP-M365', 'ENV-PROD', i.home_business_unit_key,
           'usage_signal'
    FROM bronze_m365_copilot_usage u
    JOIN bronze_ref_identity_map i ON i.upn = u."User Principal Name"
    WHERE u."Last Activity Date" <> ''

    UNION ALL
    -- GitHub Copilot activity, from the seat's last_activity_at. Requires IDE
    -- telemetry to be on, otherwise every seat looks idle.
    SELECT g."snapshot_date", 'GitHubCopilot', i.identity_key, '',
           i.cost_center_key, 'active_day',
           1, 0, 0, 0, 0,
           0.0, 'FALSE', 'FALSE', NULL,
           'APP-GHCP', 'ENV-PROD', i.home_business_unit_key,
           'usage_signal'
    FROM bronze_ghc_seats g
    JOIN bronze_ref_identity_map i ON i.github_login = g."assignee_login"
    WHERE g."last_activity_at" <> '';
"""
# --------------------------------------------------------------------------- #
#  GOLD                                                                        #
# --------------------------------------------------------------------------- #
GOLD_SQL = """
-- Fabric capacity is real FOCUS content but it is the platform's own running
-- cost, not AI consumption, so it stays in Silver and is deliberately excluded
-- from the usage star. It is reported from silver_cost_charge instead.
DROP TABLE IF EXISTS fact_ai_usage;
CREATE TABLE fact_ai_usage AS
SELECT usage_date, platform_key, identity_key, model_key, cost_center_key,
       unit_type, quantity, input_tokens, output_tokens, cached_tokens, requests,
       ROUND(cost_usd, 8) AS cost_usd, cost_is_estimated, is_error,
       ROUND(latency_ms, 4) AS latency_ms,
       application_key, environment_key, business_unit_key
FROM silver_usage_conformed;

-- --------------------------------------------------------------------- dims --
DROP TABLE IF EXISTS dim_identity;
CREATE TABLE dim_identity AS
SELECT identity_key, display_name, principal_type, upn, github_login, team,
       business_unit, cost_center_key,
       CASE WHEN is_human = 'TRUE' THEN 'Human'
            WHEN principal_type = 'ServicePrincipal' THEN 'ServicePrincipal'
            ELSE 'Application' END AS identity_class,
       is_human, home_business_unit_key
FROM bronze_ref_identity_map
UNION ALL
-- Agents are first-class identities: they consume credits exactly as people
-- consume seats, so the same vocabulary has to cover them.
SELECT a.agent_key, a.agent_name, 'Agent', '', '', 'Copilot Studio',
       COALESCE(b.business_unit_name, ''), a.cost_center_key,
       'Agent', 'FALSE', a.owner_business_unit_key
FROM bronze_ref_agent_inventory a
LEFT JOIN bronze_ref_business_hierarchy b
       ON b.business_unit_key = a.owner_business_unit_key
UNION ALL
-- Requests that arrived without a resolvable caller still cost money. Giving
-- them a real key keeps unallocated spend on the dashboard instead of dropping
-- it from the join.
SELECT 'unknown', 'Unattributed caller', 'Unknown', '', '', 'Unallocated',
       'Unallocated', '', 'Unattributed', 'FALSE', 'BU-UNALLOC';

DROP TABLE IF EXISTS dim_application;
CREATE TABLE dim_application AS
SELECT application_key, application_name, application_type,
       owner_business_unit_key, owner_upn, default_environment_key,
       criticality, is_mock
FROM bronze_ref_app_inventory;

DROP TABLE IF EXISTS dim_business_unit;
CREATE TABLE dim_business_unit AS
SELECT business_unit_key, business_unit_name, division, monthly_budget_usd,
       executive_owner, is_mock_budget
FROM bronze_ref_business_hierarchy;

DROP TABLE IF EXISTS dim_cost_center;
CREATE TABLE dim_cost_center AS
SELECT cost_center_key, MIN(team) AS cost_center_name,
       MIN(business_unit) AS business_unit, '' AS owner_upn
FROM bronze_ref_identity_map
WHERE cost_center_key <> ''
GROUP BY cost_center_key;

DROP TABLE IF EXISTS dim_environment;
CREATE TABLE dim_environment AS
SELECT * FROM (
    SELECT 'ENV-PROD' AS environment_key, 'Production' AS environment_name,
           'TRUE' AS is_production, 'Tier-1' AS sla_tier
    UNION ALL SELECT 'ENV-TEST', 'Test',  'FALSE', 'Tier-3'
    UNION ALL SELECT 'ENV-DEV',  'Development', 'FALSE', 'Tier-3'
    UNION ALL SELECT 'ENV-UNK',  'Unknown', 'FALSE', 'Unknown'
);

DROP TABLE IF EXISTS dim_model;
CREATE TABLE dim_model AS
SELECT DISTINCT
    model_key,
    model_key AS model_name,
    CASE WHEN model_key LIKE 'gpt-4.1%' THEN '2025-04-14' ELSE '' END AS model_version,
    CASE WHEN model_key LIKE 'gpt-%' OR model_key LIKE 'o3%'
         THEN 'Azure OpenAI' ELSE 'GitHub Copilot' END AS provider,
    'text' AS modality
FROM fact_ai_usage
WHERE model_key <> '';

DROP TABLE IF EXISTS dim_platform;
CREATE TABLE dim_platform AS
SELECT * FROM (
    SELECT 'Foundry' AS platform_key, 'Azure AI Foundry' AS platform_name,
           'Consumption (tokens)' AS billing_model, 'token' AS native_unit,
           'TRUE' AS has_token_telemetry, 'TRUE' AS has_native_cost,
           'TRUE' AS is_variable_cost,
           'MOCK - FOCUS 1.0r2 export + APIM gateway (Log Analytics)' AS data_source,
           '0.15' AS enterprise_discount_pct
    UNION ALL SELECT 'GitHubCopilot', 'GitHub Copilot Enterprise',
           'Seats + premium requests', 'premium_request', 'FALSE', 'TRUE', 'TRUE',
           'MOCK - GitHub seats + enhanced billing usage API', '0.05'
    UNION ALL SELECT 'CopilotStudio', 'Microsoft Copilot Studio',
           'Copilot Credits', 'copilot_credit', 'FALSE', 'TRUE', 'TRUE',
           'MOCK - FOCUS 1.0r2 export + Dataverse msdyn_aievent', '0.20'
    UNION ALL SELECT 'M365Copilot', 'Microsoft 365 Copilot',
           'Per-seat licence + credits', 'seat_day', 'FALSE', 'FALSE', 'FALSE',
           'MOCK - Graph reports + M365 Copilot Credits billing', '0.0'
);

DROP TABLE IF EXISTS dim_rate_card;
CREATE TABLE dim_rate_card AS
SELECT rate_key, platform, unit_type, model, unit_price_usd, effective_from,
       currency, source, note
FROM bronze_ref_rate_card;

-- A convenience numeric view so the dashboards and ad-hoc SQL never have to
-- remember which columns arrived as text.
DROP VIEW IF EXISTS gold;
CREATE VIEW gold AS
SELECT usage_date, platform_key, identity_key, model_key, unit_type,
       CAST(cost_usd AS REAL) AS cost, CAST(quantity AS REAL) AS quantity,
       CAST(requests AS REAL) AS requests,
       CAST(input_tokens AS REAL) AS input_tokens,
       CAST(output_tokens AS REAL) AS output_tokens,
       CAST(latency_ms AS REAL) AS latency_ms,
       is_error, application_key, environment_key, business_unit_key
FROM fact_ai_usage;
"""


def load_csv(con, table, path):
    with open(path, newline="", encoding="utf-8") as fh:
        reader = csv.reader(fh)
        cols = next(reader)
        rows = list(reader)
    quoted = ", ".join('"' + c.replace('"', '""') + '"' for c in cols)
    con.execute(f'DROP TABLE IF EXISTS "{table}"')
    con.execute(f'CREATE TABLE "{table}" ({quoted})')
    con.executemany(
        f'INSERT INTO "{table}" VALUES ({", ".join("?" for _ in cols)})', rows)
    return len(rows)


def build_dim_date(con):
    """dim_date is derived, not landed: it spans exactly the fact's date range."""
    lo, hi = con.execute(
        "SELECT MIN(usage_date), MAX(usage_date) FROM fact_ai_usage").fetchone()
    from datetime import date as _date, timedelta as _td
    start, end = _date.fromisoformat(lo), _date.fromisoformat(hi)
    months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
              "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    rows = []
    d = start
    while d <= end:
        rows.append((d.isoformat(), d.year, f"Q{(d.month - 1) // 3 + 1}", d.month,
                     f"{months[d.month - 1]} {d.year}", d.day, days[d.weekday()],
                     "TRUE" if d.weekday() < 5 else "FALSE", f"{d.year}-{d.month:02d}"))
        d += _td(days=1)
    con.execute("DROP TABLE IF EXISTS dim_date")
    con.execute('CREATE TABLE dim_date (' +
                ", ".join(f'"{c}"' for c in DIM_CONTRACT["dim_date"]) + ")")
    con.executemany(f"INSERT INTO dim_date VALUES ({', '.join('?' * 9)})", rows)
    return len(rows)


def assert_contracts(con):
    """Fail loudly on schema drift instead of silently breaking Power BI."""
    def columns(table):
        return [r[1] for r in con.execute(f'PRAGMA table_info("{table}")')]

    problems = []
    if columns("fact_ai_usage") != FACT_CONTRACT:
        problems.append(f"fact_ai_usage columns: {columns('fact_ai_usage')}")
    for table, contract in DIM_CONTRACT.items():
        if columns(table) != contract:
            problems.append(f"{table} columns: {columns(table)}")

    # Every fact key must resolve, or the star silently drops spend.
    orphan_checks = [
        ("identity_key", "dim_identity", "identity_key"),
        ("application_key", "dim_application", "application_key"),
        ("business_unit_key", "dim_business_unit", "business_unit_key"),
        ("environment_key", "dim_environment", "environment_key"),
        ("platform_key", "dim_platform", "platform_key"),
        ("usage_date", "dim_date", "date_key"),
    ]
    for fact_col, dim, dim_col in orphan_checks:
        n = con.execute(
            f"SELECT COUNT(*) FROM fact_ai_usage f "
            f'WHERE f."{fact_col}" <> "" AND NOT EXISTS '
            f'(SELECT 1 FROM "{dim}" d WHERE d."{dim_col}" = f."{fact_col}")').fetchone()[0]
        if n:
            problems.append(f"{n} fact rows have no {dim} match on {fact_col}")

    if problems:
        raise SystemExit("GOLD CONTRACT VIOLATION:\n  - " + "\n  - ".join(problems))


def reconcile(con):
    """Allocated cost must equal the invoice. This is the honesty check."""
    rows = []
    for platform, label in [("Foundry", "Azure OpenAI (token allocation)"),
                            ("CopilotStudio", "Copilot Studio (credit allocation)")]:
        billed = con.execute(
            "SELECT COALESCE(SUM(billed_cost_usd), 0) FROM silver_cost_charge "
            "WHERE platform_key = ?", (platform,)).fetchone()[0]
        allocated = con.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) FROM fact_ai_usage "
            "WHERE platform_key = ? AND cost_is_estimated = 'FALSE'",
            (platform,)).fetchone()[0]
        rows.append((label, billed, allocated, abs(billed - allocated)))
    return rows


def build():
    con = sqlite3.connect(DB)
    print(f"Building the medallion -> {DB}\n")

    print("BRONZE (raw, source-faithful extracts):")
    bronze_files = sorted(glob.glob(os.path.join(BRONZE, "*.csv")))
    if not bronze_files:
        raise SystemExit(f"no raw extracts in {BRONZE}; run gen_bronze_data.py first")
    total = 0
    for path in bronze_files:
        table = os.path.splitext(os.path.basename(path))[0]
        n = load_csv(con, table, path)
        total += n
        print(f"  + {table:<32} {n:>6} rows")

    print("\nSILVER (typed, conformed, identity-resolved, cost-allocated):")
    con.executescript(SILVER_SQL)
    for table in ("silver_cost_charge", "silver_gateway_request", "silver_studio_event",
                  "silver_foundry_allocation", "silver_studio_allocation",
                  "silver_usage_conformed"):
        n = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        total += n
        print(f"  + {table:<32} {n:>6} rows")

    print("\nGOLD (the star the semantic model binds to):")
    con.executescript(GOLD_SQL)
    n_date = build_dim_date(con)
    for table in ["fact_ai_usage"] + sorted(DIM_CONTRACT):
        n = n_date if table == "dim_date" else con.execute(
            f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        total += n
        print(f"  + {table:<32} {n:>6} rows")

    con.execute("DROP TABLE IF EXISTS extractable_data_catalog")
    con.execute("CREATE TABLE extractable_data_catalog "
                "(product TEXT, category TEXT, field TEXT, description TEXT, "
                "source TEXT, grain TEXT)")
    con.executemany("INSERT INTO extractable_data_catalog VALUES (?,?,?,?,?,?)", CATALOG)
    total += len(CATALOG)
    print(f"\nMETADATA:\n  + extractable_data_catalog       {len(CATALOG):>6} rows "
          f"({len(set(c[0] for c in CATALOG))} products)")

    con.commit()
    assert_contracts(con)
    print("\nContract check: fact + 9 dimensions match the semantic model, no orphan keys.")

    print("\nCost reconciliation (billed invoice vs allocated to identities):")
    for label, billed, allocated, delta in reconcile(con):
        print(f"  {label:<36} billed ${billed:>10.4f}   allocated ${allocated:>10.4f}   delta ${delta:.6f}")
        if delta > 0.01:
            raise SystemExit(f"allocation does not reconcile for {label}")

    billed_total = con.execute(
        "SELECT SUM(cost_usd) FROM fact_ai_usage WHERE cost_is_estimated='FALSE'").fetchone()[0]
    modelled_total = con.execute(
        "SELECT SUM(cost_usd) FROM fact_ai_usage WHERE cost_is_estimated='TRUE'").fetchone()[0]
    platform_self = con.execute(
        "SELECT SUM(billed_cost_usd) FROM silver_cost_charge "
        "WHERE platform_key='FabricCapacity'").fetchone()[0]
    unallocated = con.execute(
        "SELECT SUM(cost_usd) FROM fact_ai_usage WHERE business_unit_key='BU-UNALLOC'").fetchone()[0]
    print(f"\n  billed (provider invoiced)   ${billed_total:>10.2f}")
    print(f"  modelled (rate card)         ${modelled_total:>10.2f}")
    print(f"  of which unallocated to a BU ${unallocated:>10.2f}")
    print(f"  Fabric capacity self-cost    ${platform_self:>10.2f}  (silver only, not AI usage)")
    print(f"\nTotal: {total} rows.")
    con.close()


def query(sql):
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    rows = [dict(r) for r in con.execute(sql).fetchall()]
    if rows:
        cols = list(rows[0].keys())
        w = {c: max(len(c), *(len(str(r[c])) for r in rows)) for c in cols}
        print(" | ".join(c.ljust(w[c]) for c in cols))
        print("-+-".join("-" * w[c] for c in cols))
        for r in rows:
            print(" | ".join(str(r[c]).ljust(w[c]) for c in cols))
    print(f"\n{len(rows)} row(s)")
    con.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--query", help="run ad-hoc SQL against finops.db")
    args = ap.parse_args()
    if args.query:
        query(args.query)
    else:
        build()
