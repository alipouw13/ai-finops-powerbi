#!/usr/bin/env python3
"""
Generate MOCK Bronze-layer datasets for the AI FinOps accelerator.

Produces one CSV per MVP Bronze table defined in docs/bronze-layer-architecture.md,
grounded in the identities / apps / business units already in the repo dimensions so
the Bronze feeds conform cleanly in Silver.

EVERYTHING PRODUCED IS SYNTHETIC / MOCK. Rows carry lineage columns and a
_data_class='MOCK' marker so nothing is ever mistaken for real tenant data.

Output: platform/fabric/bronze_out/*.csv
"""
import csv
import os
import random
from datetime import date, timedelta

random.seed(42)
HERE = os.path.dirname(os.path.abspath(__file__))
DIMS = os.path.normpath(os.path.join(HERE, "..", "..", "AIFinOps.SemanticModel", "data"))
OUT = os.path.join(HERE, "bronze_out")
os.makedirs(OUT, exist_ok=True)

INGEST_TS = "2026-09-04T18:00:00Z"
BATCH = "batch-2026-09-04"


def read_dim(name):
    with open(os.path.join(DIMS, f"{name}.csv"), newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


identities = read_dim("dim_identity")
apps = read_dim("dim_application")
bus = read_dim("dim_business_unit")
ccs = read_dim("dim_cost_center")

humans = [i for i in identities if i["is_human"] == "TRUE"]
sps = [i for i in identities if i["principal_type"] == "ServicePrincipal"]

# Shelfware: users holding a paid seat who never actually use it. Without this
# every generated user is active on some day, [Idle Licensed Users] is always
# empty, and the Waste & Utilisation / License Optimization pages have nothing
# to show. build_data.py models the same two users as idle for the CSV demo.
IDLE_LOGINS = {"lee.novak", "kim.arroyo"}


def is_idle(identity):
    return identity["identity_key"] in IDLE_LOGINS


ACTIVE_HUMANS = [u for u in humans if not is_idle(u)]

# A hardcoded end date silently ages the demo out: every month-to-date and
# forecast measure reads BLANK once "today" moves past it, so the CFO page shows
# -100% budget variance for every business unit and looks broken rather than
# empty. Anchor on today, and let FINOPS_MOCK_END pin it for reproducible tests.
END = (date.fromisoformat(os.environ["FINOPS_MOCK_END"])
       if os.environ.get("FINOPS_MOCK_END") else date.today())
DAYS = [END - timedelta(days=d) for d in range(60)]
MONTH_DAYS = [d for d in DAYS if d.month == END.month and d.year == END.year]


def lineage(source_api, watermark):
    return {
        "_ingested_at": INGEST_TS, "_source_system": "AI-FinOps-MOCK",
        "_source_api": source_api, "_watermark": watermark,
        "_batch_id": BATCH, "_data_class": "MOCK",
    }


def write(name, rows):
    if not rows:
        print(f"  ! {name}: 0 rows"); return
    cols = list(rows[0].keys())
    with open(os.path.join(OUT, f"{name}.csv"), "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader(); w.writerows(rows)
    print(f"  ok {name}: {len(rows)} rows")


def gen_m365_usage():
    rows = []
    for d in DAYS:
        for u in humans:
            active = random.random() > 0.25 and not is_idle(u)
            la = d.isoformat() if active else ""
            rows.append({
                "report_date": d.isoformat(),
                "user_principal_name": u["upn"],
                "display_name": u["display_name"],
                "last_activity_date": la,
                "copilot_chat_last_activity": la if active and random.random() > .2 else "",
                "teams_last_activity": la if active and random.random() > .4 else "",
                "word_last_activity": la if active and random.random() > .5 else "",
                "excel_last_activity": la if active and random.random() > .6 else "",
                "outlook_last_activity": la if active and random.random() > .3 else "",
                "report_period": "D7",
                **lineage("graph.getMicrosoft365CopilotUsageUserDetail", d.isoformat()),
            })
    write("bronze_m365_copilot_usage", rows)


def gen_m365_seats():
    rows = []
    for d in DAYS:
        for u in humans:
            rows.append({
                "snapshot_date": d.isoformat(),
                "user_principal_name": u["upn"],
                "sku_id": "639dec6b-bb19-468b-871c-c5c441c4b0cb",
                "sku_part_number": "Microsoft_365_Copilot",
                "capability_status": "Enabled",
                "assigned_date": "2026-06-01",
                "service_plans_enabled": "M365_COPILOT;COPILOT_STUDIO_IN_COPILOT",
                **lineage("graph.subscribedSkus+users.assignedLicenses", d.isoformat()),
            })
    write("bronze_m365_copilot_seats", rows)


def gen_m365_credits():
    caps = [("Cowork", 2), ("Autopilot", 3), ("agent_action", 3), ("generative_answer", 2)]
    rows = []
    for d in MONTH_DAYS:
        for u in random.sample(ACTIVE_HUMANS, k=min(5, len(ACTIVE_HUMANS))):
            cap, rate = random.choice(caps)
            credits = random.randint(200, 3000)
            rows.append({
                "usage_date": d.isoformat(),
                "consumer_id": u["upn"],
                "consumer_type": "User",
                "meter_id": "m365-copilot-credit",
                "meter_name": "Microsoft 365 Copilot Credits",
                "capability": cap,
                "credit_rate": rate,
                "credits_consumed": credits,
                "unit_price_usd": 0.01,
                "cost_usd": round(credits * 0.01, 2),
                **lineage("powerplatform.billing / costmanagement", d.isoformat()),
            })
    write("bronze_m365_copilot_credits", rows)


def gen_studio_credits():
    agents = [("HR Helpdesk Bot", "BU-TECH", "ENV-PROD"),
              ("Claims Triage Agent", "BU-INSURANCE", "ENV-PROD"),
              ("Store Ops Assistant", "BU-RETAIL", "ENV-PROD")]
    actions = [("classic_answer", 1), ("generative_answer", 2), ("agent_action", 3)]
    rows = []
    for d in MONTH_DAYS:
        for aname, bu, env in agents:
            for act, rate in actions:
                credits = random.randint(500, 8000)
                rows.append({
                    "usage_date": d.isoformat(),
                    "environment_id": env,
                    "agent_id": aname.lower().replace(" ", "-"),
                    "agent_name": aname,
                    "action_type": act,
                    "credit_rate": rate,
                    "credits_consumed": credits,
                    "cost_usd": round(credits * 0.01, 2),
                    "session_count": random.randint(10, 200),
                    "owner_business_unit_key": bu,
                    **lineage("powerplatform.admin / dataverse.conversationtranscript", d.isoformat()),
                })
    write("bronze_studio_credits", rows)


def gen_ghc_seats():
    rows = []
    for d in DAYS:
        for u in humans:
            active = random.random() > 0.3 and not is_idle(u)
            rows.append({
                "snapshot_date": d.isoformat(),
                "assignee_login": u["github_login"],
                "assignee_id": abs(hash(u["github_login"])) % 10**8,
                "created_at": "2026-06-01T00:00:00Z",
                "last_activity_at": (d.isoformat() + "T14:00:00Z") if active else "",
                "last_activity_editor": random.choice(["vscode", "visualstudio", "jetbrains"]) if active else "",
                "plan_type": "enterprise",
                "pending_cancellation_date": "",
                **lineage("github.orgs.copilot.billing.seats", d.isoformat()),
            })
    write("bronze_ghc_seats", rows)


def gen_ghc_premium():
    models = [("gpt-4.1", 1), ("claude-sonnet-4.5", 1), ("code-review", 13), ("o3", 10)]
    rows = []
    for d in MONTH_DAYS:
        for u in random.sample(ACTIVE_HUMANS, k=min(4, len(ACTIVE_HUMANS))):
            model, mult = random.choice(models)
            qty = random.randint(1, 60)
            net = round(qty * 0.04 * mult, 2)
            rows.append({
                "usage_date": d.isoformat(),
                "login": u["github_login"],
                "sku": "copilot_premium_request",
                "unit_type": "premium_request",
                "quantity": qty,
                "model": model,
                "model_multiplier": mult,
                "gross_amount": net,
                "discount_amount": 0,
                "net_amount": net,
                "repository_name": random.choice(["contoso/checkout", "contoso/search", "contoso/platform"]),
                **lineage("github.settings.billing.usage", d.isoformat()),
            })
    write("bronze_ghc_premium_usage", rows)


def gen_azure_ai_cost():
    meters = [("gpt-4.1-mini Input Tokens", "Azure OpenAI"),
              ("gpt-4.1-mini Output Tokens", "Azure OpenAI"),
              ("gpt-4.1 Input Tokens", "Azure OpenAI"),
              ("gpt-4.1 Output Tokens", "Azure OpenAI")]
    rows = []
    for d in DAYS:
        for app in apps:
            if app["application_type"] not in ("api", "notebook"):
                continue
            for meter, cat in random.sample(meters, k=2):
                qty = random.randint(50_000, 2_000_000)
                cost = round(qty * random.uniform(0.4e-6, 1.6e-6), 4)
                rows.append({
                    "usage_date": d.isoformat(),
                    "subscription_id": "68837237-5a48-41a9-bed4-947f5c277684",
                    "resource_group": "rg-aoai-prod",
                    "resource_id": f"/subscriptions/.../aoai-{app['application_key'].lower()}",
                    "meter_id": abs(hash(meter)) % 10**6,
                    "meter_name": meter,
                    "meter_category": cat,
                    "quantity": qty,
                    "unit_price": 1e-6,
                    "cost_usd": cost,
                    "currency": "USD",
                    "tags_json": f'{{"app":"{app["application_key"]}","bu":"{app["owner_business_unit_key"]}","env":"{app["default_environment_key"]}"}}',
                    **lineage("costmanagement.usageDetails", d.isoformat()),
                })
    write("bronze_azure_ai_cost", rows)


def gen_azure_ai_metrics():
    rows = []
    for d in DAYS:
        for app in apps:
            if app["application_type"] not in ("api", "notebook"):
                continue
            model = random.choice(["gpt-4.1-mini", "gpt-4.1"])
            pt = random.randint(50_000, 1_500_000)
            gt = int(pt * random.uniform(0.3, 0.7))
            rows.append({
                "metric_time": d.isoformat() + "T00:00:00Z",
                "resource_id": f"/subscriptions/.../aoai-{app['application_key'].lower()}",
                "deployment_name": f"{model}-prod",
                "model_name": model,
                "processed_prompt_tokens": pt,
                "generated_tokens": gt,
                "total_tokens": pt + gt,
                "requests": random.randint(100, 5000),
                "latency_ms": round(random.uniform(200, 3500), 1),
                "throttled_count": random.randint(0, 20),
                **lineage("monitor.metrics", d.isoformat()),
            })
    write("bronze_azure_ai_metrics", rows)


def gen_fabric_cost():
    rows = []
    for d in DAYS:
        rows.append({
            "usage_date": d.isoformat(),
            "capacity_id": "davidshreyasalison",
            "sku": "F2",
            "meter_name": "Fabric Capacity Usage",
            "quantity": 48,
            "cost_usd": round(48 * 0.18, 2),
            "tags_json": '{"env":"demo","owner":"finops"}',
            **lineage("costmanagement.usageDetails", d.isoformat()),
        })
    write("bronze_fabric_capacity_cost", rows)


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
            "department": i["team"],
            "home_business_unit_key": i["home_business_unit_key"],
            "cost_center_key": i["cost_center_key"],
            **lineage("entra.users+servicePrincipals / manual-map", "2026-09-04"),
        })
    write("bronze_ref_identity_map", rows)


def gen_ref_app():
    rows = []
    for a in apps:
        rows.append({
            "application_key": a["application_key"],
            "application_name": a["application_name"],
            "application_type": a["application_type"],
            "owner_upn": a["owner_upn"],
            "owner_business_unit_key": a["owner_business_unit_key"],
            "environment": a["default_environment_key"],
            "criticality": a["criticality"],
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
            **lineage("finance-master-data / entra-department-rollup", "2026-09-04"),
        })
    write("bronze_ref_business_hierarchy", rows)


def gen_ref_agent():
    agents = [("hr-helpdesk-bot", "HR Helpdesk Bot", "CopilotStudio", "BU-TECH"),
              ("claims-triage-agent", "Claims Triage Agent", "CopilotStudio", "BU-INSURANCE"),
              ("store-ops-assistant", "Store Ops Assistant", "CopilotStudio", "BU-RETAIL")]
    rows = []
    for aid, an, plat, bu in agents:
        rows.append({
            "agent_key": aid, "agent_name": an, "platform": plat,
            "environment_id": "ENV-PROD", "owner_upn": "platform-team@contoso.com",
            "owner_business_unit_key": bu, "purpose": "customer/employee assistance",
            "created_on": "2026-06-15",
            **lineage("copilotstudio.env-inventory", "2026-09-04"),
        })
    write("bronze_ref_agent_inventory", rows)


def gen_ref_rate():
    rows = read_dim("dim_rate_card")
    out = []
    for r in rows:
        out.append({**r, **lineage("ea-price-sheet / list-price", "2026-09-04")})
    write("bronze_ref_rate_card", out)


if __name__ == "__main__":
    print(f"Generating MOCK Bronze datasets -> {OUT}")
    gen_m365_usage(); gen_m365_seats(); gen_m365_credits()
    gen_studio_credits(); gen_ghc_seats(); gen_ghc_premium()
    gen_azure_ai_cost(); gen_azure_ai_metrics(); gen_fabric_cost()
    gen_ref_identity(); gen_ref_app(); gen_ref_bu(); gen_ref_agent(); gen_ref_rate()
    print("Done.")
