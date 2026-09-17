#!/usr/bin/env python3
"""
Generate MOCK RAW (Bronze) extracts for the AI FinOps accelerator.

Every table here is SOURCE-FAITHFUL: column names, casing, value types and null
behaviour match what the real extract actually hands over. Bronze never
reshapes; Silver is the only place that renames, casts, or joins.

  bronze_focus_cost               Cost Management export, FOCUS 1.0r2 (96 cols)
  bronze_apim_gateway_requests    APIM AI gateway -> Log Analytics (all strings)
  bronze_apim_client_ownership    Log Analytics ApimClientOwnership_CL
  bronze_m365_copilot_usage       Graph getMicrosoft365CopilotUsageUserDetail
  bronze_m365_copilot_seats       Graph subscribedSkus + users/assignedLicenses
  bronze_m365_copilot_credits     M365 / Power Platform Copilot Credits billing
  bronze_dataverse_msdyn_aievent  Dataverse msdyn_aievent (Copilot Studio)
  bronze_ghc_seats                GitHub /orgs/{org}/copilot/billing/seats
  bronze_ghc_premium_usage        GitHub /{org}/settings/billing/usage
  bronze_ref_*                    customer reference inputs (not API extracts)

INTERNAL CONSISTENCY IS THE POINT. Gateway requests are generated first, then
the FOCUS charge lines are priced FROM those token totals -- exactly as Cost
Management bills the tokens the gateway observed. Dataverse credits and the
Copilot Studio PAYG meter line up the same way. That lets Silver allocate real
billed dollars onto identities and reconcile to the cent.

EVERYTHING PRODUCED IS SYNTHETIC / MOCK. Rows carry lineage columns and a
_data_class='MOCK' marker so nothing is ever mistaken for real tenant data.

Output: platform/fabric/bronze_out/*.csv
"""
import csv
import json
import os
import random
import uuid
from collections import defaultdict
from datetime import date, timedelta

random.seed(42)
HERE = os.path.dirname(os.path.abspath(__file__))
DIMS = os.path.normpath(os.path.join(HERE, "..", "..", "AIFinOps.SemanticModel", "data"))
OUT = os.path.join(HERE, "bronze_out")
os.makedirs(OUT, exist_ok=True)

INGEST_TS = "2026-09-04T18:00:00Z"
BATCH = "batch-2026-09-04"
TENANT_ID = "24bb70f1-e5cd-4e0f-9495-274ba9146731"
SUBSCRIPTION_ID = "68837237-5a48-41a9-bed4-947f5c277684"
SUBSCRIPTION_NAME = "Contoso AI Platform"
BILLING_ACCOUNT_ID = "EA-7654321"
BILLING_ACCOUNT_NAME = "Contoso Enterprise Agreement"

# FOCUS 1.0 column order exactly as Microsoft Cost Management emits it.
FOCUS_COLUMNS = [
    "BilledCost", "BillingAccountId", "BillingAccountName", "BillingAccountType",
    "BillingCurrency", "BillingPeriodEnd", "BillingPeriodStart", "ChargeCategory",
    "ChargeClass", "ChargeDescription", "ChargeFrequency", "ChargePeriodEnd",
    "ChargePeriodStart", "CommitmentDiscountCategory", "CommitmentDiscountId",
    "CommitmentDiscountName", "CommitmentDiscountStatus", "CommitmentDiscountType",
    "ConsumedQuantity", "ConsumedUnit", "ContractedCost", "ContractedUnitPrice",
    "EffectiveCost", "InvoiceIssuerName", "ListCost", "ListUnitPrice",
    "PricingCategory", "PricingQuantity", "PricingUnit", "ProviderName",
    "PublisherName", "RegionId", "RegionName", "ResourceId", "ResourceName",
    "ResourceType", "ServiceCategory", "ServiceName", "SkuId", "SkuPriceId",
    "SubAccountId", "SubAccountName", "SubAccountType", "Tags", "x_AccountId",
    "x_AccountName", "x_AccountOwnerId", "x_BilledCostInUsd", "x_BilledUnitPrice",
    "x_BillingAccountId", "x_BillingAccountName", "x_BillingExchangeRate",
    "x_BillingExchangeRateDate", "x_BillingProfileId", "x_BillingProfileName",
    "x_ContractedCostInUsd", "x_CostAllocationRuleName", "x_CostCenter",
    "x_CustomerId", "x_CustomerName", "x_EffectiveCostInUsd", "x_EffectiveUnitPrice",
    "x_InvoiceId", "x_InvoiceIssuerId", "x_InvoiceSectionId", "x_InvoiceSectionName",
    "x_ListCostInUsd", "x_PartnerCreditApplied", "x_PartnerCreditRate",
    "x_PricingBlockSize", "x_PricingCurrency", "x_PricingSubcategory",
    "x_PricingUnitDescription", "x_PublisherCategory", "x_PublisherId",
    "x_ResellerId", "x_ResellerName", "x_ResourceGroupName", "x_ResourceType",
    "x_ServicePeriodEnd", "x_ServicePeriodStart", "x_SkuDescription", "x_SkuDetails",
    "x_SkuIsCreditEligible", "x_SkuMeterCategory", "x_SkuMeterId", "x_SkuMeterName",
    "x_SkuMeterSubcategory", "x_SkuOfferId", "x_SkuOrderId", "x_SkuOrderName",
    "x_SkuPartNumber", "x_SkuRegion", "x_SkuServiceFamily", "x_SkuTerm", "x_SkuTier",
]

# Azure OpenAI list prices per 1,000 tokens, and the EA discount that turns the
# list price into the contracted/effective price on the invoice.
AOAI_METERS = {
    ("gpt-4.1-mini", "input"): ("9d25a9f8-1c8e-5e82-949f-a69580f18e8d", "gpt-4.1-mini Input Tokens", 0.0004),
    ("gpt-4.1-mini", "output"): ("ce8b7b6e-f4d1-5b56-beca-4ea97224f27b", "gpt-4.1-mini Output Tokens", 0.0016),
    ("gpt-4.1-mini", "cached"): ("a3f1d0b2-6d52-5c74-8f0a-19a2f1cbb6d4", "gpt-4.1-mini Cached Input Tokens", 0.0001),
    ("gpt-4.1", "input"): ("22c19c9a-bdbb-5481-969f-a54f3f93391a", "gpt-4.1 Input Tokens", 0.002),
    ("gpt-4.1", "output"): ("73fcd9cf-82df-5fb5-89d4-1448394f0f1e", "gpt-4.1 Output Tokens", 0.008),
    ("gpt-4.1", "cached"): ("5b0c47ae-91ad-5f39-a2bd-77d5c6a41f83", "gpt-4.1 Cached Input Tokens", 0.0005),
}
AOAI_DISCOUNT = 0.15        # matches dim_platform[enterprise_discount_pct] for Foundry
STUDIO_DISCOUNT = 0.20      # matches dim_platform[enterprise_discount_pct] for Copilot Studio
STUDIO_CREDIT_LIST = 0.01   # PAYG list price per Copilot Credit
M365_CREDIT_LIST = 0.01

# Which gateway client (service principal or user) calls which application, and
# which Azure OpenAI resource serves it. This is the customer's CMDB mapping and
# is published to Log Analytics as ApimClientOwnership_CL.
GATEWAY_APPS = [
    {
        "application_key": "APP-CHECKOUT", "gateway_app_name": "checkout-service",
        "client_id": "0999cfae-085e-464f-a49d-f8851e3e5195",
        "business_unit": "Retail", "business_unit_key": "BU-RETAIL",
        "cost_center": "CC-1000", "team": "Checkout",
        "resource_name": "aoai-checkout-prod", "environment_key": "ENV-PROD",
        "models": ["gpt-4.1", "gpt-4.1-mini"], "human_callers": [],
    },
    {
        "application_key": "APP-SEARCH", "gateway_app_name": "search-api",
        "client_id": "dd6d3503-88f1-4fc9-a4d0-1a79c3aaf364",
        "business_unit": "Discovery", "business_unit_key": "BU-DISCOVERY",
        "cost_center": "CC-2000", "team": "Search",
        "resource_name": "aoai-search-prod", "environment_key": "ENV-PROD",
        "models": ["gpt-4.1-mini"], "human_callers": [],
    },
    {
        "application_key": "APP-DSNB", "gateway_app_name": "ds-notebooks",
        "client_id": "62a2cc36-edb8-4520-b7fe-05433a160c32",
        "business_unit": "Platform", "business_unit_key": "BU-PLATFORM",
        "cost_center": "CC-3000", "team": "DataScience",
        "resource_name": "aoai-datascience-dev", "environment_key": "ENV-DEV",
        "models": ["gpt-4.1", "gpt-4.1-mini"],
        "human_callers": ["jenny.oyelaran", "dev.patel", "lee.novak"],
    },
    {
        "application_key": "APP-MOBILE", "gateway_app_name": "mobile-assistant",
        "client_id": "7bec6f27-dc94-4d6d-b89a-3969513bc71e",
        "business_unit": "Retail", "business_unit_key": "BU-RETAIL",
        "cost_center": "CC-4000", "team": "Mobile",
        "resource_name": "aoai-mobile-test", "environment_key": "ENV-TEST",
        "models": ["gpt-4.1-mini"], "human_callers": [],
    },
]

STUDIO_AGENTS = [
    ("hr-helpdesk-bot", "HR Helpdesk Bot", "BU-TECH"),
    ("claims-triage-agent", "Claims Triage Agent", "BU-INSURANCE"),
    ("store-ops-assistant", "Store Ops Assistant", "BU-RETAIL"),
]
STUDIO_ENV_ID = "3f2ad9c1-7b64-4f6a-9d8e-52c0b7a13e45"
STUDIO_ENV_NAME = "Contoso Production"

# Copilot Studio billed event types and their credit rate (classic answer = 1,
# generative answer = 2, agent action = 3). msdyn_creditconsumed is already net
# of zero-rating, so it is used directly and never recomputed from rates.
STUDIO_EVENT_TYPES = [("Classic answer", 1), ("Generative answer", 2), ("Agent action", 3)]


def read_dim(name):
    with open(os.path.join(DIMS, f"{name}.csv"), newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


identities = read_dim("dim_identity")
apps = read_dim("dim_application")
bus = read_dim("dim_business_unit")

humans = [i for i in identities if i["is_human"] == "TRUE"]
by_identity_key = {i["identity_key"]: i for i in identities}

# A couple of licensed users never adopt the tool. That is the single most
# common real finding in an AI FinOps review, so the mock has to contain it:
# they hold a paid seat and produce no activity signal at all.
M365_IDLE_UPNS = {humans[5]["upn"], humans[7]["upn"]}
GHC_IDLE_LOGINS = {humans[1]["github_login"], humans[6]["github_login"]}

END = date(2026, 8, 28)
DAYS = sorted(END - timedelta(days=d) for d in range(60))
MONTH_DAYS = [d for d in DAYS if d.month == 8]


def lineage(source_api, watermark):
    return {
        "_ingested_at": INGEST_TS, "_source_system": "AI-FinOps-MOCK",
        "_source_api": source_api, "_watermark": watermark,
        "_batch_id": BATCH, "_data_class": "MOCK",
    }


def write(name, rows, columns=None):
    if not rows:
        print(f"  ! {name}: 0 rows")
        return
    cols = columns or list(rows[0].keys())
    with open(os.path.join(OUT, f"{name}.csv"), "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
    print(f"  ok {name}: {len(rows)} rows x {len(cols)} cols")


def month_bounds(d):
    start = d.replace(day=1)
    end = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
    return start, end


# --------------------------------------------------------------------------- #
#  APIM AI gateway -> Log Analytics                                            #
#                                                                              #
#  The ONLY per-identity attribution path for Foundry. Azure Monitor token      #
#  metrics carry no identity dimension, so they are deliberately not collected. #
#  Log Analytics hands every field over as a STRING, including numbers and      #
#  booleans, and unset values arrive as "" or the literal "None".               #
# --------------------------------------------------------------------------- #
def gen_apim_gateway_requests():
    rows = []
    tokens = defaultdict(int)   # (date, application_key, model, direction) -> tokens
    for d in DAYS:
        for app in GATEWAY_APPS:
            for _ in range(random.randint(12, 28)):
                model = random.choice(app["models"])
                # RAG-shaped traffic: large grounded prompts, modest completions.
                prompt = random.randint(1500, 28000)
                completion = random.randint(200, 3500)
                is_error = random.random() < 0.04
                status = "429" if is_error and random.random() < 0.5 else ("500" if is_error else "200")
                # A slice of traffic reaches the gateway without a resolvable client
                # id (missing JWT claim). It still consumes billed tokens, so it
                # must stay in the extract and surface as unallocated spend.
                anonymous = random.random() < 0.06
                # Those rows report CachedPromptTokens as the literal "None", so
                # they genuinely carry no cached tokens to bill or attribute.
                cached = 0 if anonymous else random.choice([0, 0, 0, random.randint(500, 6000)])
                caller = ""
                if not anonymous:
                    caller = (random.choice(app["human_callers"])
                              if app["human_callers"] and random.random() < 0.7
                              else app["client_id"])
                identity = by_identity_key.get(caller, {})
                ts = (f"{d.isoformat()}T{random.randint(0, 23):02d}:"
                      f"{random.randint(0, 59):02d}:{random.randint(0, 59):02d}."
                      f"{random.randint(1000000, 9999999)}Z")
                rows.append({
                    "ApiName": "AI Gateway",
                    "Appid": caller,
                    "BackendId": f"{app['resource_name']}.openai.azure.com",
                    "BusinessUnitClaim": app["business_unit"] if caller else "",
                    "CachedPromptTokens": str(cached) if not anonymous else "None",
                    "ClientId": caller,
                    "CompletionTokens": str(completion),
                    "CostCenterClaim": (identity.get("cost_center_key") or app["cost_center"]) if caller else "",
                    "DeploymentRegion": "eastus2" if caller else "",
                    "IsError": str(is_error) if caller else "None",
                    "IsStreaming": str(random.random() < 0.3) if caller else "None",
                    "ModelName": model,
                    "ModelVersion": "2025-04-14",
                    "Oid": identity.get("identity_key", "") if identity.get("is_human") == "TRUE" else "",
                    "OperationName": "Chat Completions",
                    "PromptTokens": str(prompt),
                    "RequestId": str(uuid.UUID(int=random.getrandbits(128))),
                    "StatusCode": status,
                    "TableName": "PrimaryResult",
                    "TimeGenerated": ts,
                    "TotalLatencyMs": str(round(random.uniform(180, 6500), 4)),
                    "TotalTokens": str(prompt + completion + cached),
                    "UpnOrAppName": identity.get("upn") or caller,
                    **lineage("loganalytics.ApimAiGateway_CL", d.isoformat()),
                })
                key = (d.isoformat(), app["application_key"], model)
                tokens[key + ("input",)] += prompt
                tokens[key + ("output",)] += completion
                tokens[key + ("cached",)] += cached
    write("bronze_apim_gateway_requests", rows)
    return tokens


def gen_apim_client_ownership():
    """ApimClientOwnership_CL — the customer-maintained client registry in LA."""
    rows = []
    for app in GATEWAY_APPS:
        rows.append({
            "AppName": app["gateway_app_name"],
            "BusinessUnit": app["business_unit"],
            "ClientId": app["client_id"],
            "CostCenter": app["cost_center"],
            "TableName": "PrimaryResult",
            "Team": app["team"],
            "TenantId": TENANT_ID,
            "TimeGenerated": "2026-09-04T03:48:36.725424Z",
            "Type": "ApimClientOwnership_CL",
            "_ResourceId": "",
            **lineage("loganalytics.ApimClientOwnership_CL", "2026-09-04"),
        })
    write("bronze_apim_client_ownership", rows)


# --------------------------------------------------------------------------- #
#  Cost Management export — FOCUS 1.0r2                                        #
#                                                                              #
#  The authoritative dollars. Priced from the gateway's own token totals and    #
#  the Dataverse credit totals, because that is what the provider actually      #
#  invoices. Empty strings are intentional: many FOCUS columns are conditional. #
# --------------------------------------------------------------------------- #
def focus_row(*, charge_date, charge_description, service_name, service_category,
              focus_resource_type, resource_name, resource_group, resource_provider,
              arm_resource_type, meter_id, meter_name, meter_category, meter_subcategory,
              sku_id, sku_part_number, sku_service_family, sku_details,
              consumed_quantity, consumed_unit, pricing_unit, pricing_block_size,
              list_unit_price, discount_pct, tags, cost_center, region_id, region_name,
              source_api):
    billing_start, billing_end = month_bounds(charge_date)
    charge_start = f"{charge_date.isoformat()}T00:00:00Z"
    charge_end = f"{(charge_date + timedelta(days=1)).isoformat()}T00:00:00Z"
    pricing_quantity = round(consumed_quantity / pricing_block_size, 6)
    contracted_unit_price = round(list_unit_price * (1 - discount_pct), 10)
    list_cost = round(pricing_quantity * list_unit_price, 6)
    contracted_cost = round(pricing_quantity * contracted_unit_price, 6)
    resource_id = (f"/subscriptions/{SUBSCRIPTION_ID}/resourceGroups/{resource_group}"
                   f"/providers/{resource_provider}/{resource_name}")

    row = {column: "" for column in FOCUS_COLUMNS}
    row.update({
        "BilledCost": contracted_cost,
        "BillingAccountId": BILLING_ACCOUNT_ID,
        "BillingAccountName": BILLING_ACCOUNT_NAME,
        "BillingAccountType": "EA",
        "BillingCurrency": "USD",
        "BillingPeriodEnd": f"{billing_end.isoformat()}T00:00:00Z",
        "BillingPeriodStart": f"{billing_start.isoformat()}T00:00:00Z",
        "ChargeCategory": "Usage",
        "ChargeDescription": charge_description,
        "ChargeFrequency": "Usage-Based",
        "ChargePeriodEnd": charge_end,
        "ChargePeriodStart": charge_start,
        "ConsumedQuantity": consumed_quantity,
        "ConsumedUnit": consumed_unit,
        "ContractedCost": contracted_cost,
        "ContractedUnitPrice": contracted_unit_price,
        "EffectiveCost": contracted_cost,
        "InvoiceIssuerName": "Microsoft",
        "ListCost": list_cost,
        "ListUnitPrice": list_unit_price,
        "PricingCategory": "Standard",
        "PricingQuantity": pricing_quantity,
        "PricingUnit": pricing_unit,
        "ProviderName": "Microsoft",
        "PublisherName": "Microsoft",
        "RegionId": region_id,
        "RegionName": region_name,
        "ResourceId": resource_id,
        "ResourceName": resource_name,
        "ResourceType": focus_resource_type,
        "ServiceCategory": service_category,
        "ServiceName": service_name,
        "SkuId": sku_id,
        "SkuPriceId": meter_id,
        "SubAccountId": SUBSCRIPTION_ID,
        "SubAccountName": SUBSCRIPTION_NAME,
        "SubAccountType": "Subscription",
        "Tags": json.dumps(tags, separators=(",", ":")),
        "x_AccountId": "EA-ACCOUNT-1001",
        "x_AccountName": "AI Platform Account",
        "x_AccountOwnerId": "finops-owner@contoso.example",
        "x_BilledCostInUsd": contracted_cost,
        "x_BilledUnitPrice": contracted_unit_price,
        "x_BillingAccountId": BILLING_ACCOUNT_ID,
        "x_BillingAccountName": BILLING_ACCOUNT_NAME,
        "x_BillingExchangeRate": 1,
        "x_BillingExchangeRateDate": billing_start.isoformat(),
        "x_BillingProfileId": BILLING_ACCOUNT_ID,
        "x_BillingProfileName": BILLING_ACCOUNT_NAME,
        "x_ContractedCostInUsd": contracted_cost,
        "x_CostCenter": cost_center,
        "x_EffectiveCostInUsd": contracted_cost,
        "x_EffectiveUnitPrice": contracted_unit_price,
        "x_InvoiceSectionId": "EA-DEPT-042",
        "x_InvoiceSectionName": "AI and Data",
        "x_ListCostInUsd": list_cost,
        "x_PricingBlockSize": pricing_block_size,
        "x_PricingCurrency": "USD",
        "x_PricingSubcategory": "On Demand",
        "x_PricingUnitDescription": pricing_unit,
        "x_PublisherCategory": "Cloud Provider",
        "x_PublisherId": "Microsoft",
        "x_ResourceGroupName": resource_group,
        "x_ResourceType": arm_resource_type,
        "x_ServicePeriodEnd": charge_end,
        "x_ServicePeriodStart": charge_start,
        "x_SkuDescription": meter_name,
        "x_SkuDetails": json.dumps(sku_details, separators=(",", ":")),
        "x_SkuIsCreditEligible": "True",
        "x_SkuMeterCategory": meter_category,
        "x_SkuMeterId": meter_id,
        "x_SkuMeterName": meter_name,
        "x_SkuMeterSubcategory": meter_subcategory,
        "x_SkuOfferId": "MS-AZR-0017P",
        "x_SkuPartNumber": sku_part_number,
        "x_SkuRegion": region_name,
        "x_SkuServiceFamily": sku_service_family,
        **lineage(source_api, charge_date.isoformat()),
    })
    return row


def gen_focus_cost(gateway_tokens, studio_credits):
    """One FOCUS export covering every Azure-billed AI charge line."""
    app_by_key = {a["application_key"]: a for a in GATEWAY_APPS}
    rows = []

    # --- Azure OpenAI usage: one charge line per resource x meter x day -------
    for (day, application_key, model, direction), token_count in sorted(gateway_tokens.items()):
        if token_count <= 0:
            continue
        app = app_by_key[application_key]
        meter_id, meter_name, list_price = AOAI_METERS[(model, direction)]
        rows.append(focus_row(
            charge_date=date.fromisoformat(day),
            charge_description=meter_name,
            service_name="Azure OpenAI Service",
            service_category="AI and Machine Learning",
            focus_resource_type="AI and Machine Learning",
            resource_name=app["resource_name"],
            resource_group="rg-aoai-prod",
            resource_provider="Microsoft.CognitiveServices/accounts",
            arm_resource_type="microsoft.cognitiveservices/accounts",
            meter_id=meter_id, meter_name=meter_name,
            meter_category="Azure OpenAI", meter_subcategory=model,
            sku_id=f"{model}-{direction}",
            sku_part_number=f"AOAI-{model.upper()}-{direction.upper()}",
            sku_service_family="AI + Machine Learning",
            sku_details={"model": model, "tokenType": direction.capitalize()},
            consumed_quantity=token_count, consumed_unit="Tokens",
            pricing_unit="1K Tokens", pricing_block_size=1000,
            list_unit_price=list_price, discount_pct=AOAI_DISCOUNT,
            tags={"app": application_key, "bu": app["business_unit_key"],
                  "env": app["environment_key"]},
            cost_center=app["cost_center"],
            region_id="eastus2", region_name="East US 2",
            source_api="costmanagement.exports.focus-1.0r2",
        ))

    # --- Copilot Studio pay-as-you-go credits: billed at the tenant meter -----
    for day, credits in sorted(studio_credits.items()):
        rows.append(focus_row(
            charge_date=date.fromisoformat(day),
            charge_description="Copilot Studio Copilot Credits",
            service_name="Microsoft Copilot Studio",
            service_category="AI and Machine Learning",
            focus_resource_type="AI and Machine Learning",
            resource_name="pp-contoso-production",
            resource_group="rg-powerplatform",
            resource_provider="Microsoft.PowerPlatform/accounts",
            arm_resource_type="microsoft.powerplatform/accounts",
            meter_id="b2d4f6a8-3c19-5d7e-90ab-1c2d3e4f5a6b",
            meter_name="Copilot Studio Copilot Credits",
            meter_category="Power Platform", meter_subcategory="Copilot Studio",
            sku_id="copilot-studio-payg",
            sku_part_number="PP-COPILOT-CREDIT",
            sku_service_family="Power Platform",
            sku_details={"capability": "Copilot Studio", "billingType": "PayAsYouGo"},
            consumed_quantity=credits, consumed_unit="Credits",
            pricing_unit="1 Credit", pricing_block_size=1,
            list_unit_price=STUDIO_CREDIT_LIST, discount_pct=STUDIO_DISCOUNT,
            tags={"app": "APP-STUDIO", "bu": "BU-TECH", "env": "ENV-PROD"},
            cost_center="CC-3000",
            region_id="global", region_name="Global",
            source_api="costmanagement.exports.focus-1.0r2",
        ))

    # --- Microsoft Fabric capacity: the platform's own running cost ----------
    # Real FOCUS content, but platform self-cost rather than AI consumption, so
    # Silver keeps it and Gold deliberately excludes it from fact_ai_usage.
    for day in DAYS:
        rows.append(focus_row(
            charge_date=day,
            charge_description="F2 Capacity Usage",
            service_name="Microsoft Fabric",
            service_category="Analytics",
            focus_resource_type="Analytics",
            resource_name="fab-finops-cap01",
            resource_group="rg-fabric-prod",
            resource_provider="Microsoft.Fabric/capacities",
            arm_resource_type="microsoft.fabric/capacities",
            meter_id="d7e8f9a0-1b2c-5d3e-8f4a-5b6c7d8e9f01",
            meter_name="F2 Capacity Usage",
            meter_category="Microsoft Fabric", meter_subcategory="F2",
            sku_id="Fabric-F2", sku_part_number="FABRIC-F2-CAP",
            sku_service_family="Analytics",
            sku_details={"sku": "F2", "capacityUnits": 2},
            consumed_quantity=24, consumed_unit="Hours",
            pricing_unit="1 Hour", pricing_block_size=1,
            list_unit_price=0.36, discount_pct=0.0,
            tags={"app": "APP-PLATFORM", "bu": "BU-PLATFORM", "env": "ENV-PROD"},
            cost_center="CC-3000",
            region_id="eastus2", region_name="East US 2",
            source_api="costmanagement.exports.focus-1.0r2",
        ))

    write("bronze_focus_cost", rows, columns=FOCUS_COLUMNS + list(lineage("", "").keys()))


# --------------------------------------------------------------------------- #
#  Dataverse msdyn_aievent — Copilot Studio                                    #
#                                                                              #
#  OData field names, including the _value suffix on lookups and the formatted #
#  value annotation Dataverse returns alongside them.                          #
# --------------------------------------------------------------------------- #
BOT_ID = {name: str(uuid.UUID(int=random.getrandbits(128))) for _, name, _ in STUDIO_AGENTS}


def gen_dataverse_aievent():
    rows = []
    credits_by_day = defaultdict(int)
    for d in MONTH_DAYS:
        for agent_key, agent_name, _bu in STUDIO_AGENTS:
            for event_type, rate in STUDIO_EVENT_TYPES:
                for _ in range(random.randint(2, 5)):
                    billed = random.random() > 0.15   # licensed users are zero-rated
                    units = random.randint(5, 60)
                    credits = units * rate if billed else 0
                    ts = (f"{d.isoformat()}T{random.randint(8, 19):02d}:"
                          f"{random.randint(0, 59):02d}:{random.randint(0, 59):02d}Z")
                    rows.append({
                        "@odata.etag": f'W/"{random.randint(1000000, 9999999)}"',
                        "msdyn_aieventid": str(uuid.UUID(int=random.getrandbits(128))),
                        "createdon": ts,
                        "msdyn_eventtimestamp": ts,
                        "msdyn_eventtype": event_type,
                        "msdyn_billingtype": "Billed" if billed else "Zero-rated",
                        "msdyn_creditconsumed": credits,
                        "msdyn_ismeteredevent": str(billed).lower(),
                        "msdyn_conversationid": str(uuid.UUID(int=random.getrandbits(128))),
                        "msdyn_sessionid": str(uuid.UUID(int=random.getrandbits(128))),
                        "_msdyn_botid_value": BOT_ID[agent_name],
                        "_msdyn_botid_value@OData.Community.Display.V1.FormattedValue": agent_name,
                        "_msdyn_environmentid_value": STUDIO_ENV_ID,
                        "_msdyn_environmentid_value@OData.Community.Display.V1.FormattedValue": STUDIO_ENV_NAME,
                        "msdyn_channel": random.choice(["Microsoft Teams", "Web chat", "Custom website"]),
                        "msdyn_outcome": random.choice(["Resolved", "Escalated", "Abandoned"]),
                        "statecode": 0,
                        "statuscode": 1,
                        "versionnumber": random.randint(10_000_000, 99_999_999),
                        **lineage("dataverse.msdyn_aievents", d.isoformat()),
                    })
                    credits_by_day[d.isoformat()] += credits
    write("bronze_dataverse_msdyn_aievent", rows)
    return credits_by_day


# --------------------------------------------------------------------------- #
#  Microsoft 365 Copilot — Graph reports, licensing, and credit billing        #
# --------------------------------------------------------------------------- #
def gen_m365_usage():
    """Graph getMicrosoft365CopilotUsageUserDetail — exact report CSV headers.

    The report returns last-activity DATES, never counts or tokens, and a blank
    date is the signal for an idle (reclaimable) seat.
    """
    rows = []
    for d in DAYS:
        for u in humans:
            active = u["upn"] not in M365_IDLE_UPNS and random.random() > 0.25
            last = d.isoformat() if active else ""

            def app_date(threshold):
                return last if active and random.random() > threshold else ""

            rows.append({
                "Report Refresh Date": d.isoformat(),
                "User Principal Name": u["upn"],
                "Display Name": u["display_name"],
                "Last Activity Date": last,
                "Copilot Chat Last Activity Date": app_date(0.2),
                "Microsoft Teams Copilot Last Activity Date": app_date(0.4),
                "Word Copilot Last Activity Date": app_date(0.5),
                "Excel Copilot Last Activity Date": app_date(0.6),
                "PowerPoint Copilot Last Activity Date": app_date(0.7),
                "Outlook Copilot Last Activity Date": app_date(0.3),
                "OneNote Copilot Last Activity Date": app_date(0.8),
                "Loop Copilot Last Activity Date": app_date(0.85),
                "Report Period": "D7",
                **lineage("graph.reports.getMicrosoft365CopilotUsageUserDetail", d.isoformat()),
            })
    write("bronze_m365_copilot_usage", rows)


def gen_m365_seats():
    """Graph /subscribedSkus joined to /users?$select=assignedLicenses."""
    rows = []
    for d in DAYS:
        for u in humans:
            rows.append({
                "snapshotDate": d.isoformat(),
                "userId": u["identity_key"],
                "userPrincipalName": u["upn"],
                "displayName": u["display_name"],
                "skuId": "639dec6b-bb19-468b-871c-c5c441c4b0cb",
                "skuPartNumber": "Microsoft_365_Copilot",
                "servicePlanId": "a62f8878-de10-42f3-b68f-6149a25ceb97",
                "servicePlanName": "M365_COPILOT",
                "provisioningStatus": "Success",
                "appliesTo": "User",
                "capabilityStatus": "Enabled",
                "assignedDateTime": "2026-06-01T00:00:00Z",
                "prepaidUnitsEnabled": 12,
                "consumedUnits": len(humans),
                **lineage("graph.subscribedSkus+users.assignedLicenses", d.isoformat()),
            })
    write("bronze_m365_copilot_seats", rows)


def gen_m365_credits():
    """Copilot Credits consumption billed outside the per-seat licence."""
    caps = ["Cowork", "Autopilot", "Agent action", "Generative answer"]
    # A user with no Copilot activity at all cannot burn Copilot Credits.
    consumers = [u for u in humans if u["upn"] not in M365_IDLE_UPNS]
    rows = []
    for d in MONTH_DAYS:
        for u in random.sample(consumers, k=min(5, len(consumers))):
            credits = random.randint(20, 400)
            rows.append({
                "usageDate": d.isoformat(),
                "tenantId": TENANT_ID,
                "consumerId": u["upn"],
                "consumerType": "User",
                "capability": random.choice(caps),
                "meterId": "m365-copilot-credit",
                "meterName": "Microsoft 365 Copilot Credits",
                "billingType": "PayAsYouGo",
                "creditsConsumed": credits,
                "unitPriceUsd": M365_CREDIT_LIST,
                "costUsd": round(credits * M365_CREDIT_LIST, 4),
                "currency": "USD",
                **lineage("m365.billing.copilotCredits", d.isoformat()),
            })
    write("bronze_m365_copilot_credits", rows)


# --------------------------------------------------------------------------- #
#  GitHub Copilot — REST API field names, flattened one level                  #
# --------------------------------------------------------------------------- #
def gen_ghc_seats():
    rows = []
    for d in DAYS:
        for u in humans:
            active = u["github_login"] not in GHC_IDLE_LOGINS and random.random() > 0.3
            rows.append({
                "snapshot_date": d.isoformat(),
                "assignee_login": u["github_login"],
                "assignee_id": abs(hash(u["github_login"])) % 10**8,
                "assignee_type": "User",
                "assigning_team": "contoso/engineering",
                "created_at": "2026-06-01T00:00:00Z",
                "updated_at": f"{d.isoformat()}T02:00:00Z",
                "last_activity_at": f"{d.isoformat()}T14:00:00Z" if active else "",
                "last_activity_editor": random.choice(["vscode", "visualstudio", "jetbrains"]) if active else "",
                "pending_cancellation_date": "",
                "plan_type": "enterprise",
                **lineage("github.orgs.copilot.billing.seats", d.isoformat()),
            })
    write("bronze_ghc_seats", rows)


def gen_ghc_premium():
    """Enhanced billing usage API — netAmount is the billed overage in USD."""
    models = [("gpt-4.1", 1), ("claude-sonnet-4.5", 1), ("code-review", 13), ("o3", 10)]
    # A seat with no activity cannot generate premium requests.
    billable = [u for u in humans if u["github_login"] not in GHC_IDLE_LOGINS]
    rows = []
    for d in MONTH_DAYS:
        for u in random.sample(billable, k=min(4, len(billable))):
            model, multiplier = random.choice(models)
            quantity = random.randint(1, 60)
            gross = round(quantity * 0.04 * multiplier, 4)
            discount = round(gross * 0.05, 4)
            rows.append({
                "date": d.isoformat(),
                "product": "copilot",
                "sku": "copilot_premium_request",
                "model": model,
                "modelMultiplier": multiplier,
                "quantity": quantity,
                "unitType": "premium_request",
                "pricePerUnit": 0.04,
                "grossAmount": gross,
                "discountAmount": discount,
                "netAmount": round(gross - discount, 4),
                "organizationName": "contoso",
                "repositoryName": random.choice(["contoso/checkout", "contoso/search", "contoso/platform"]),
                "username": u["github_login"],
                **lineage("github.settings.billing.usage", d.isoformat()),
            })
    write("bronze_ghc_premium_usage", rows)


# --------------------------------------------------------------------------- #
#  Reference inputs — customer master data, not API extracts                   #
# --------------------------------------------------------------------------- #
def gen_ref_identity():
    rows = []
    for i in identities:
        rows.append({
            "identity_key": i["identity_key"],
            "display_name": i["display_name"],
            "principal_type": i["principal_type"],
            "upn": i["upn"],
            "entra_object_id": i["identity_key"] if i["is_human"] == "TRUE" else "",
            "github_login": i["github_login"],
            "is_human": i["is_human"],
            "team": i["team"],
            "business_unit": i["business_unit"],
            "home_business_unit_key": i["home_business_unit_key"],
            "cost_center_key": i["cost_center_key"],
            **lineage("entra.users+servicePrincipals / manual-map", "2026-09-04"),
        })
    write("bronze_ref_identity_map", rows)


def gen_ref_app():
    """Application registry, including the gateway client that represents it."""
    gateway_by_app = {a["application_key"]: a for a in GATEWAY_APPS}
    rows = []
    for a in apps:
        gw = gateway_by_app.get(a["application_key"], {})
        rows.append({
            "application_key": a["application_key"],
            "application_name": a["application_name"],
            "application_type": a["application_type"],
            "owner_upn": a["owner_upn"],
            "owner_business_unit_key": a["owner_business_unit_key"],
            "default_environment_key": a["default_environment_key"],
            "criticality": a["criticality"],
            "gateway_app_name": gw.get("gateway_app_name", ""),
            "gateway_client_id": gw.get("client_id", ""),
            "azure_resource_name": gw.get("resource_name", ""),
            "is_mock": a["is_mock"],
            **lineage("cmdb / azure-resource-tags", "2026-09-04"),
        })
    write("bronze_ref_app_inventory", rows)


def gen_ref_bu():
    rows = []
    for b in bus:
        rows.append({
            "business_unit_key": b["business_unit_key"],
            "business_unit_name": b["business_unit_name"],
            "division": b["division"],
            "monthly_budget_usd": b["monthly_budget_usd"],
            "executive_owner": b["executive_owner"],
            "is_mock_budget": b["is_mock_budget"],
            **lineage("finance-master-data / entra-department-rollup", "2026-09-04"),
        })
    write("bronze_ref_business_hierarchy", rows)


def gen_ref_agent():
    """Agent registry: the Dataverse bot id -> owning business unit mapping."""
    rows = []
    for agent_key, agent_name, bu in STUDIO_AGENTS:
        rows.append({
            "agent_key": agent_key,
            "agent_name": agent_name,
            "bot_id": BOT_ID[agent_name],
            "platform": "CopilotStudio",
            "environment_id": STUDIO_ENV_ID,
            "environment_name": STUDIO_ENV_NAME,
            "owner_upn": "platform-team@contoso.com",
            "owner_business_unit_key": bu,
            "cost_center_key": "CC-3000",
            "purpose": "customer/employee assistance",
            "created_on": "2026-06-15",
            **lineage("copilotstudio.env-inventory", "2026-09-04"),
        })
    write("bronze_ref_agent_inventory", rows)


def gen_ref_rate():
    """Customer rate card — only used where the provider bills no dollars."""
    rows = [{**r, **lineage("ea-price-sheet / list-price", "2026-09-04")}
            for r in read_dim("dim_rate_card")]
    write("bronze_ref_rate_card", rows)


if __name__ == "__main__":
    print(f"Generating MOCK raw extracts -> {OUT}\n")
    print("Azure (FOCUS + APIM):")
    gateway_tokens = gen_apim_gateway_requests()
    gen_apim_client_ownership()
    print("\nCopilot Studio (Dataverse):")
    studio_credits = gen_dataverse_aievent()
    print("\nCost Management (FOCUS 1.0r2, priced from the extracts above):")
    gen_focus_cost(gateway_tokens, studio_credits)
    print("\nMicrosoft 365 Copilot (Graph + billing):")
    gen_m365_usage(); gen_m365_seats(); gen_m365_credits()
    print("\nGitHub Copilot (REST):")
    gen_ghc_seats(); gen_ghc_premium()
    print("\nReference inputs:")
    gen_ref_identity(); gen_ref_app(); gen_ref_bu(); gen_ref_agent(); gen_ref_rate()
    print("\nDone.")
