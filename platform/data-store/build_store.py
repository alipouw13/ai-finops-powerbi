#!/usr/bin/env python3
"""
Build a portable AI FinOps data source (SQLite) — the Fabric-free data store.

Loads:
  * all 16 MOCK Bronze tables from platform/fabric/bronze_out/*.csv
    (incl. bronze_m365_cowork_usage and bronze_foundry_gateway)
  * an `extractable_data_catalog` table (every extractable field per product,
    from docs/extractable-data-by-product.md) — queryable metadata

Output: platform/data-store/finops.db  (a real, portable, queryable database)

Why SQLite: zero dependencies, single-file, and the exact interchange a teammate
can import into a Fabric Lakehouse / Azure SQL later. Runs anywhere, today.

Run:
  python3 platform/fabric/gen_bronze_data.py     # (re)generate Bronze CSVs
  python3 platform/data-store/build_store.py
  python3 platform/data-store/build_store.py --query "SELECT ..."   # ad-hoc SQL
"""
import argparse
import csv
import glob
import os
import sqlite3

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, ".."))
BRONZE = os.path.join(ROOT, "fabric", "bronze_out")
GOLD = os.path.normpath(os.path.join(ROOT, "..", "AIFinOps.SemanticModel", "data"))
DB = os.path.join(HERE, "finops.db")

# (product, category, field, description, source, grain)
CATALOG = [
    ("Copilot Studio", "identity", "environment_id", "Power Platform environment", "PPAC / Dataverse", "env"),
    ("Copilot Studio", "identity", "agent_id/agent_name", "The bot/agent", "Dataverse bot", "agent"),
    ("Copilot Studio", "usage", "conversation_id", "Session identifier", "conversationtranscript", "conversation"),
    ("Copilot Studio", "usage", "activities_json", "Turn-by-turn transcript", "conversationtranscript", "conversation"),
    ("Copilot Studio", "usage", "action_type", "classic/generative/agent action", "Analytics + transcript", "message"),
    ("Copilot Studio", "cost", "credits_consumed", "Copilot Credits used", "PPAC billing / Cost Mgmt", "day/agent/action"),
    ("Copilot Studio", "usage", "session_count", "Conversations handled", "Studio Analytics", "day"),
    ("Copilot Studio", "usage", "message_count", "Messages per conversation", "transcript", "conversation"),
    ("Copilot Studio", "usage", "outcome", "Resolved/escalated/abandoned", "Analytics", "conversation"),
    ("Copilot Studio", "dimension", "channel", "Teams, web, etc.", "transcript", "conversation"),
    ("Copilot Studio", "usage", "created_on", "Timestamp", "Dataverse", "conversation"),
    ("Copilot Studio", "identity", "end_user_id", "Authenticated user (if any)", "transcript", "conversation"),
    ("GitHub Copilot", "identity", "assignee_login", "GitHub user", "/orgs/{org}/copilot/billing/seats", "seat"),
    ("GitHub Copilot", "identity", "assignee_id", "Numeric user id", "seats", "seat"),
    ("GitHub Copilot", "usage", "created_at", "Seat assigned date", "seats", "seat"),
    ("GitHub Copilot", "usage", "last_activity_at", "Last Copilot use", "seats (IDE telemetry)", "seat"),
    ("GitHub Copilot", "dimension", "last_activity_editor", "vscode/VS/JetBrains", "seats", "seat"),
    ("GitHub Copilot", "dimension", "plan_type", "business/enterprise", "seats", "seat"),
    ("GitHub Copilot", "usage", "pending_cancellation_date", "Scheduled removal", "seats", "seat"),
    ("GitHub Copilot", "usage", "premium_requests", "Metered requests", "/settings/billing/usage", "day/user"),
    ("GitHub Copilot", "dimension", "model", "Model used", "usage/metrics", "day/model"),
    ("GitHub Copilot", "cost", "model_multiplier", "Cost weight (code review 13x)", "usage", "request"),
    ("GitHub Copilot", "cost", "net_amount", "Overage $", "usage", "day"),
    ("GitHub Copilot", "dimension", "repository_name", "Repo context", "usage", "day/repo"),
    ("GitHub Copilot", "usage", "active_engaged_users", "Adoption counts", "/copilot/metrics (>=5 users)", "day"),
    ("GitHub Copilot", "usage", "suggestions_acceptances", "Code accept rate", "metrics", "day/lang/editor"),
    ("GitHub Copilot", "usage", "chat_counts", "Copilot Chat usage", "metrics", "day"),
    ("M365 Copilot", "identity", "user_principal_name", "The user", "Graph usage report", "user"),
    ("M365 Copilot", "identity", "display_name", "Name", "Graph", "user"),
    ("M365 Copilot", "usage", "last_activity_date", "Overall last use", "getMicrosoft365CopilotUsageUserDetail", "user/day"),
    ("M365 Copilot", "usage", "{app}_last_activity", "Teams/Word/Excel/Outlook/PPT/OneNote/Loop/Chat", "Graph usage report", "user/app"),
    ("M365 Copilot", "cost", "sku_id/sku_part_number", "License held", "subscribedSkus", "user"),
    ("M365 Copilot", "dimension", "capability_status", "Enabled/suspended", "assignedLicenses", "user"),
    ("M365 Copilot", "usage", "assigned_date", "License grant date", "Graph", "user"),
    ("M365 Copilot", "usage", "interaction_id", "Individual AI interaction", "aiInteraction API", "interaction"),
    ("M365 Copilot", "dimension", "app_class/interaction_type", "Where/how used", "aiInteraction", "interaction"),
    ("M365 Copilot", "usage", "from/body_preview", "Who + content", "aiInteraction", "interaction"),
    ("M365 Copilot", "usage", "session_id", "Conversation grouping", "aiInteraction", "interaction"),
    ("Foundry/AOAI", "identity", "resource_id", "AOAI/Foundry resource", "Cost Mgmt / Monitor", "resource"),
    ("Foundry/AOAI", "dimension", "deployment_name/model_name", "Deployed model", "Monitor metrics", "deployment"),
    ("Foundry/AOAI", "usage", "processed_prompt_tokens", "Input tokens", "Monitor metrics", "hour/deploy"),
    ("Foundry/AOAI", "usage", "generated_tokens", "Output tokens", "Monitor", "hour/deploy"),
    ("Foundry/AOAI", "usage", "total_tokens", "Sum", "Monitor", "hour/deploy"),
    ("Foundry/AOAI", "usage", "requests", "Call count", "Monitor", "hour"),
    ("Foundry/AOAI", "usage", "latency_ms", "Response time", "Monitor", "hour"),
    ("Foundry/AOAI", "usage", "throttled_count", "429s", "Monitor", "hour"),
    ("Foundry/AOAI", "cost", "meter_name/quantity/cost_usd", "Real $", "Cost Management usageDetails", "day/meter"),
    ("Foundry/AOAI", "dimension", "tags_json", "app/bu/env tags", "Cost Mgmt", "resource"),
    ("Foundry/AOAI", "identity", "caller/api_subscription_id", "Who called (via APIM)", "Log Analytics", "request"),
    ("Foundry/AOAI", "usage", "request_id/status_code", "Per-request detail", "Diagnostic logs", "request"),
    ("Copilot Cowork/Autopilot", "identity", "consumer_id", "User/agent consuming", "PPAC billing / Cost Mgmt", "day/consumer"),
    ("Copilot Cowork/Autopilot", "dimension", "capability", "Cowork vs Autopilot", "billing meter", "consumer"),
    ("Copilot Cowork/Autopilot", "cost", "credits_consumed", "Copilot Credits", "PPAC / Cost Mgmt", "day"),
    ("Copilot Cowork/Autopilot", "usage", "action_count", "Autonomous actions taken", "agent telemetry", "day"),
    ("Copilot Cowork/Autopilot", "cost", "cost_usd", "$ from credits", "Cost Mgmt", "day"),
    ("Azure ML", "identity", "workspace_id", "AML workspace", "Cost Mgmt / Monitor", "workspace"),
    ("Azure ML", "dimension", "compute_target", "Cluster/instance", "Monitor metrics", "compute"),
    ("Azure ML", "usage", "node_hours", "CPU/GPU hours", "Monitor / logs", "job"),
    ("Azure ML", "usage", "job_id/run", "Training run", "AML REST / AmlComputeJobEvent", "job"),
    ("Azure ML", "usage", "endpoint_id/deployment_id", "Online endpoint", "Monitor", "endpoint"),
    ("Azure ML", "usage", "request_count", "Inference calls", "AmlOnlineEndpointTrafficLog", "endpoint/hour"),
    ("Azure ML", "usage", "latency_ms", "Endpoint latency", "Monitor", "endpoint"),
    ("Azure ML", "usage", "gpu_utilization", "Hardware use", "Monitor", "compute"),
    ("Azure ML", "identity", "submitted_by", "User who ran job", "AML logs (Entra)", "job"),
    ("Azure ML", "cost", "meter_name/cost_usd", "Real compute $", "Cost Management", "day/resource"),
    ("Azure ML", "dimension", "tags_json", "app/bu/env", "Cost Mgmt", "resource"),
    ("Microsoft Fabric", "identity", "capacity_id/sku", "The F-capacity", "Capacity Metrics / Cost Mgmt", "capacity"),
    ("Microsoft Fabric", "dimension", "workspace_id/item_id", "Where consumed", "Metrics app (XMLA)", "workspace/item"),
    ("Microsoft Fabric", "dimension", "operation_type/workload", "Warehouse/Spark/Pipeline/Copilot/PBI", "Metrics app", "operation"),
    ("Microsoft Fabric", "usage", "cu_seconds", "Capacity Units consumed", "Metrics app", "operation/day"),
    ("Microsoft Fabric", "usage", "interactive_cu/background_cu", "Split", "Metrics app", "day"),
    ("Microsoft Fabric", "usage", "throttled/overload", "Capacity pressure", "Monitor metrics", "day"),
    ("Microsoft Fabric", "identity", "user_or_sp_id", "Who ran it", "activity events", "operation"),
    ("Microsoft Fabric", "cost", "meter_name/cost_usd", "Capacity $", "Cost Management", "day"),
]


def load_csv(con, table, path):
    with open(path, newline="", encoding="utf-8") as fh:
        rdr = csv.reader(fh)
        cols = next(rdr)
        rows = list(rdr)
    con.execute(f'DROP TABLE IF EXISTS "{table}"')
    con.execute(f'CREATE TABLE "{table}" ({", ".join(chr(34)+c+chr(34) for c in cols)})')
    con.executemany(f'INSERT INTO "{table}" VALUES ({", ".join("?" for _ in cols)})', rows)
    return len(rows)


def build():
    con = sqlite3.connect(DB)
    total = 0
    print(f"Building portable data store -> {DB}\n")
    print("Bronze tables:")
    for path in sorted(glob.glob(os.path.join(BRONZE, "*.csv"))):
        t = os.path.splitext(os.path.basename(path))[0]
        n = load_csv(con, t, path)
        total += n
        print(f"  + {t:<34} {n:>5} rows")
    print("\nGold star schema (fact + dims):")
    gold_files = sorted(glob.glob(os.path.join(GOLD, "fact_ai_usage.csv"))
                        + glob.glob(os.path.join(GOLD, "dim_*.csv")))
    gold_n = 0
    for path in gold_files:
        t = os.path.splitext(os.path.basename(path))[0]
        n = load_csv(con, t, path)
        total += n
        gold_n += 1
        print(f"  + {t:<34} {n:>5} rows")
    # convenience numeric view over the Gold fact
    con.execute("DROP VIEW IF EXISTS gold")
    con.execute(
        "CREATE VIEW gold AS SELECT usage_date, platform_key, identity_key, model_key, "
        "unit_type, CAST(cost_usd AS REAL) cost, CAST(quantity AS REAL) quantity, "
        "CAST(requests AS REAL) requests, CAST(input_tokens AS REAL) input_tokens, "
        "CAST(output_tokens AS REAL) output_tokens, CAST(latency_ms AS REAL) latency_ms, "
        "is_error, application_key, environment_key, business_unit_key FROM fact_ai_usage")
    con.execute('DROP TABLE IF EXISTS extractable_data_catalog')
    con.execute("CREATE TABLE extractable_data_catalog "
                "(product TEXT, category TEXT, field TEXT, description TEXT, source TEXT, grain TEXT)")
    con.executemany("INSERT INTO extractable_data_catalog VALUES (?,?,?,?,?,?)", CATALOG)
    print(f"\nMetadata:\n  + extractable_data_catalog          {len(CATALOG):>5} rows "
          f"({len(set(c[0] for c in CATALOG))} products)")
    con.commit()
    print(f"\nTotal: {total + len(CATALOG)} rows across "
          f"{len(glob.glob(os.path.join(BRONZE,'*.csv'))) + 1} tables.")
    print("\nExtractable fields per product:")
    for prod, cnt in con.execute(
        "SELECT product, COUNT(*) FROM extractable_data_catalog GROUP BY product ORDER BY 2 DESC"):
        print(f"  {prod:<28} {cnt} fields")
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
    a = ap.parse_args()
    if a.query:
        query(a.query)
    else:
        build()
