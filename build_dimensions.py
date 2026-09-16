#!/usr/bin/env python3
"""
Additive dimension builder for the AI FinOps semantic model.

This is NOT a model regenerator. It reads the committed CSVs as the source of
truth and *augments* them:

  * creates 3 conformed dimensions  -> dim_business_unit, dim_application,
    dim_environment  (each joins to fact on a single key = clean star)
  * appends 3 foreign keys to fact_ai_usage.csv
       business_unit_key, application_key, environment_key
  * adds universal-identity columns to dim_identity.csv
       identity_class, is_human, home_business_unit_key

All synthetic (non-derivable) attributes are clearly MOCK: budgets, criticality,
SLA tiers, divisions, executive owners. Real vs mock provenance already lives on
dim_platform[data_source] and fact[cost_is_estimated]; nothing here relabels
mock spend as real.

Run:  python3 build_dimensions.py   (idempotent; safe to re-run)
"""
import csv, os

D = os.path.join(os.path.dirname(__file__), "AIFinOps.SemanticModel", "data")


def read(name):
    with open(os.path.join(D, name), newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def write(name, fieldnames, rows):
    with open(os.path.join(D, name), "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)


# ------------------------------------------------------------ PLATFORM DISCOUNTS
# dim_platform.csv is emitted by build_data.py, which knows nothing about
# negotiated rates, so re-running it used to silently drop this column and break
# [Discounted Cost] / [Discount Savings] / [Forecast Cost (next 30d, net)].
# Owning it here makes the column survive any regeneration order.
# MOCK: replace with the customer's actual EA/MCA discounts.
DISCOUNT_PCT = {
    "Foundry": "0.15",
    "GitHubCopilot": "0.05",
    "CopilotStudio": "0.20",
    "M365Copilot": "0.0",
}
platform = read("dim_platform.csv")
for r in platform:
    r.setdefault("addon_unit", "")
    r.setdefault("addon_billing_model", "")
    r.setdefault("enterprise_discount_pct", "")
    if not r["enterprise_discount_pct"]:
        r["enterprise_discount_pct"] = DISCOUNT_PCT.get(r["platform_key"], "0.0")
write("dim_platform.csv",
      ["platform_key", "platform_name", "billing_model", "native_unit",
       "addon_unit", "addon_billing_model",
       "has_token_telemetry", "has_native_cost", "is_variable_cost", "data_source",
       "enterprise_discount_pct"],
      platform)

# ----------------------------------------------------------------- BUSINESS UNIT
# Conform on dim_identity[business_unit] (the authoritative "home BU" of a
# person or service principal). dim_cost_center carries a *different* BU label
# for the same cost centre -- that inconsistency is exactly why we conform here.
BU = {
    # name        key           division              budget_usd  exec_owner (MOCK)
    "Retail":     ("BU-RETAIL",     "Commercial",         28000, "cfo-retail@contoso.com"),
    "Discovery":  ("BU-DISCOVERY",  "Commercial",         12000, "cfo-discovery@contoso.com"),
    "Platform":   ("BU-PLATFORM",   "Engineering",        18000, "cto@contoso.com"),
    "Technology": ("BU-TECH",       "Engineering",        45000, "cio@contoso.com"),
    "Insurance":  ("BU-INSURANCE",  "Financial Services", 22000, "cfo-insurance@contoso.com"),
    "Unallocated":("BU-UNALLOC",    "Unallocated",            0, ""),
}
write("dim_business_unit.csv",
      ["business_unit_key", "business_unit_name", "division",
       "monthly_budget_usd", "executive_owner", "is_mock_budget"],
      [{"business_unit_key": k, "business_unit_name": name, "division": div,
        "monthly_budget_usd": bud, "executive_owner": owner,
        "is_mock_budget": "TRUE"} for name, (k, div, bud, owner) in BU.items()])

# --------------------------------------------------------------------- ENVIRONMENT
write("dim_environment.csv",
      ["environment_key", "environment_name", "is_production", "sla_tier"],
      [{"environment_key": "ENV-PROD", "environment_name": "Production",
        "is_production": "TRUE", "sla_tier": "Tier-1"},
       {"environment_key": "ENV-TEST", "environment_name": "Test",
        "is_production": "FALSE", "sla_tier": "Tier-3"},
       {"environment_key": "ENV-DEV", "environment_name": "Development",
        "is_production": "FALSE", "sla_tier": "Tier-3"},
       {"environment_key": "ENV-UNK", "environment_name": "Unknown",
        "is_production": "FALSE", "sla_tier": ""}])

# --------------------------------------------------------------------- APPLICATION
# app_key: (name, type, owner_bu_key, owner_upn, default_env, criticality[MOCK], is_mock)
APPS = {
    "APP-CHECKOUT": ("Checkout Assistant",        "api",       "BU-RETAIL",    "robin.hale@contoso.com",  "ENV-PROD", "High",   "FALSE"),
    "APP-SEARCH":   ("Product Search API",        "api",       "BU-DISCOVERY", "sam.chen@contoso.com",    "ENV-PROD", "High",   "FALSE"),
    "APP-DSNB":     ("Data Science Notebooks",    "notebook",  "BU-PLATFORM",  "jenny.oyelaran@contoso.com","ENV-DEV", "Low",    "FALSE"),
    "APP-MOBILE":   ("Mobile Shopping Assistant", "agent",     "BU-RETAIL",    "robin.hale@contoso.com",  "ENV-TEST", "Medium", "FALSE"),
    "APP-M365":     ("Microsoft 365 Copilot",     "copilot",   "BU-TECH",      "cio@contoso.com",         "ENV-PROD", "Medium", "TRUE"),
    "APP-GHCP":     ("GitHub Copilot",            "copilot",   "BU-TECH",      "cio@contoso.com",         "ENV-PROD", "Medium", "TRUE"),
    "APP-STUDIO":   ("Employee Support Agent",    "agent",     "BU-TECH",      "cio@contoso.com",         "ENV-PROD", "Medium", "TRUE"),
    "APP-UNKNOWN":  ("Unattributed Workload",     "unattributed","BU-UNALLOC",   "",                        "ENV-UNK",  "Unassigned","FALSE"),
}
write("dim_application.csv",
      ["application_key", "application_name", "application_type",
       "owner_business_unit_key", "owner_upn", "default_environment_key",
       "criticality", "is_mock"],
      [{"application_key": k, "application_name": n, "application_type": t,
        "owner_business_unit_key": bu, "owner_upn": o,
        "default_environment_key": e, "criticality": c, "is_mock": m}
       for k, (n, t, bu, o, e, c, m) in APPS.items()])

# ----------------------------------------------------------------- UNIVERSAL IDENTITY
identity = read("dim_identity.csv")
# The Foundry gateway does not always record a ClientId/Oid, so build_data.py
# emits identity_key="unknown" for those rows. Without a matching dimension row
# Power BI silently creates a blank member on rel_usage_identity and the spend
# disappears from every identity-sliced visual. Give it an explicit member, the
# same way unattributed spend gets BU-UNALLOC and APP-UNKNOWN.
UNKNOWN_IDENTITY = {
    "identity_key": "unknown", "display_name": "Unattributed Identity",
    "principal_type": "Unknown", "upn": "", "github_login": "",
    "team": "", "business_unit": "Unallocated", "cost_center_key": "",
}
if not any(r["identity_key"] == "unknown" for r in identity):
    identity.append(dict(UNKNOWN_IDENTITY))
# principal_type domain today is {User, ServicePrincipal}; the identity_class
# vocabulary is the universal set so future ManagedIdentity/Agent rows conform.
CLASS = {"User": "Human", "ServicePrincipal": "ServicePrincipal", "Unknown": "Unknown"}
id_home_bu = {}
for r in identity:
    r["identity_class"] = CLASS.get(r["principal_type"], "Application")
    r["is_human"] = "TRUE" if r["principal_type"] == "User" else "FALSE"
    r["home_business_unit_key"] = BU.get(r["business_unit"], BU["Unallocated"])[0]
    id_home_bu[r["identity_key"]] = r["home_business_unit_key"]
write("dim_identity.csv",
      ["identity_key", "display_name", "principal_type", "upn", "github_login",
       "team", "business_unit", "cost_center_key",
       "identity_class", "is_human", "home_business_unit_key"],
      identity)

# --------------------------------------------------------------- AUGMENT FACT KEYS
# Service-principal identity -> its owning application.
SP_APP = {
    "0999cfae-085e-464f-a49d-f8851e3e5195": "APP-CHECKOUT",
    "dd6d3503-88f1-4fc9-a4d0-1a79c3aaf364": "APP-SEARCH",
    "62a2cc36-edb8-4520-b7fe-05433a160c32": "APP-DSNB",
    "7bec6f27-dc94-4d6d-b89a-3969513bc71e": "APP-MOBILE",
}
PLATFORM_APP = {"M365Copilot": "APP-M365", "GitHubCopilot": "APP-GHCP",
                "CopilotStudio": "APP-STUDIO", "Foundry": "APP-UNKNOWN"}


def app_for(row):
    if row["identity_key"] in SP_APP:
        return SP_APP[row["identity_key"]]
    return PLATFORM_APP.get(row["platform_key"], "APP-UNKNOWN")


fact = read("fact_ai_usage.csv")
for r in fact:
    app = app_for(r)
    r["application_key"] = app
    r["environment_key"] = APPS[app][4]
    # usage BU = the identity's home BU (chargeable owner); fall back to app owner
    r["business_unit_key"] = id_home_bu.get(r["identity_key"], APPS[app][2])

fact_cols = list(fact[0].keys())
for c in ("business_unit_key", "application_key", "environment_key"):
    if c not in fact_cols:
        fact_cols.append(c)
write("fact_ai_usage.csv", fact_cols, fact)

print("dimensions built:")
print("  dim_platform     : +enterprise_discount_pct on", len(platform), "rows")
print("  dim_business_unit:", len(BU), "rows")
print("  dim_application  :", len(APPS), "rows")
print("  dim_environment  : 4 rows")
print("  dim_identity     : +3 columns")
print("  fact_ai_usage    : +3 keys on", len(fact), "rows")
