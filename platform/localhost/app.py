#!/usr/bin/env python3
"""
AI FinOps — Local demo server (NO Fabric / NO Power BI license required).

Mocks up what the deployed Fabric + Power BI end result looks like:
  * 5 persona dashboards (CFO, Governance, Engineering, App Owner, License Optimization)
  * A working AI Insight layer: ask a question in plain English -> get the
    answer + the SQL it ran (grounded in the real Gold table).

Zero external dependencies. Uses Python stdlib only (http.server + sqlite3).
Data source of truth: AIFinOps.SemanticModel/data/*.csv  (all clearly MOCK).

Run:
    python3 platform/localhost/app.py
    open http://localhost:8080
"""
import csv
import json
import os
import re
import sqlite3
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# The startup banner uses →; the default Windows console codepage (cp1252)
# raises UnicodeEncodeError on it and the server dies before it binds.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.normpath(os.path.join(HERE, "..", "..", "AIFinOps.SemanticModel", "data"))
STORE_DB = os.path.normpath(os.path.join(HERE, "..", "data-store", "finops.db"))
PORT = int(os.environ.get("PORT", "8080"))

# Seat-based (license) platforms vs consumption platforms
SEAT_PLATFORMS = ("M365Copilot", "GitHubCopilot")
ACTIVITY_UNITS = ("prompt", "premium_request", "token", "copilot_credit", "message")


# --------------------------------------------------------------------------- #
#  Load the Gold star schema (CSV -> in-memory SQLite)                         #
# --------------------------------------------------------------------------- #
def _load_csv(con, table, path):
    with open(path, newline="", encoding="utf-8") as fh:
        rdr = csv.reader(fh)
        cols = next(rdr)
        rows = list(rdr)
    coldef = ", ".join(f'"{c}"' for c in cols)
    con.execute(f'CREATE TABLE "{table}" ({coldef})')
    ph = ", ".join("?" for _ in cols)
    con.executemany(f'INSERT INTO "{table}" VALUES ({ph})', rows)
    return len(rows)


def build_db():
    con = sqlite3.connect(":memory:", check_same_thread=False)
    con.row_factory = sqlite3.Row
    loaded = {}
    source = None
    # Prefer the portable data store (Bronze + Gold + catalog) if it exists,
    # so the BI dashboards and AI layer read from the SAME source the Fabric
    # push (load_bronze.py) uses. Fall back to the raw Gold CSVs otherwise.
    if os.path.exists(STORE_DB):
        src = sqlite3.connect(STORE_DB)
        src.row_factory = sqlite3.Row
        tables = [r[0] for r in src.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        for t in tables:
            rows = src.execute(f'SELECT * FROM "{t}"').fetchall()
            cols = [d[0] for d in src.execute(f'SELECT * FROM "{t}" LIMIT 1').description]
            coldef = ", ".join(f'"{c}"' for c in cols)
            con.execute(f'CREATE TABLE "{t}" ({coldef})')
            ph = ", ".join("?" for _ in cols)
            con.executemany(f'INSERT INTO "{t}" VALUES ({ph})', [tuple(r) for r in rows])
            loaded[t] = len(rows)
        src.close()
        source = "finops.db (portable data store: Bronze + Gold)"
    else:
        for fn in os.listdir(DATA):
            if fn.endswith(".csv"):
                table = fn[:-4]
                loaded[table] = _load_csv(con, table, os.path.join(DATA, fn))
        source = "AIFinOps.SemanticModel/data/*.csv (Gold)"
    # numeric cost view for convenience
    con.execute("DROP VIEW IF EXISTS gold")
    con.execute(
        "CREATE VIEW gold AS SELECT usage_date, platform_key, identity_key, "
        "model_key, unit_type, CAST(cost_usd AS REAL) AS cost, "
        "CAST(quantity AS REAL) AS quantity, CAST(requests AS REAL) AS requests, "
        "CAST(input_tokens AS REAL) AS input_tokens, "
        "CAST(output_tokens AS REAL) AS output_tokens, "
        "CAST(latency_ms AS REAL) AS latency_ms, is_error, "
        "application_key, environment_key, business_unit_key "
        "FROM fact_ai_usage"
    )
    con.commit()
    loaded["__source__"] = source
    return con, loaded


CON, LOADED = build_db()


def q(sql, args=()):
    return [dict(r) for r in CON.execute(sql, args).fetchall()]


def scalar(sql, args=()):
    r = CON.execute(sql, args).fetchone()
    return r[0] if r and r[0] is not None else 0


# --------------------------------------------------------------------------- #
#  Latest month window (data spans 2026-06-30 .. 2026-08-28)                   #
# --------------------------------------------------------------------------- #
LATEST_MONTH = scalar("SELECT substr(MAX(usage_date),1,7) FROM fact_ai_usage")
MONTH_FILTER = "substr(usage_date,1,7) = ?"


# --------------------------------------------------------------------------- #
#  Persona payloads                                                            #
# --------------------------------------------------------------------------- #
def bu_name(key):
    r = CON.execute(
        "SELECT business_unit_name FROM dim_business_unit WHERE business_unit_key=?",
        (key,),
    ).fetchone()
    return r[0] if r else key


def payload_cfo():
    total = scalar("SELECT SUM(cost) FROM gold")
    month_total = scalar(f"SELECT SUM(cost) FROM gold WHERE {MONTH_FILTER}", (LATEST_MONTH,))
    by_bu = q(
        f"""SELECT b.business_unit_name AS label,
                   ROUND(SUM(g.cost),2) AS value,
                   MAX(CAST(b.monthly_budget_usd AS REAL)) AS budget
            FROM gold g LEFT JOIN dim_business_unit b
              ON g.business_unit_key=b.business_unit_key
            WHERE {MONTH_FILTER}
            GROUP BY b.business_unit_name ORDER BY value DESC""",
        (LATEST_MONTH,),
    )
    for r in by_bu:
        r["variance"] = round((r["budget"] or 0) - r["value"], 2)
        r["over"] = (r["value"] > (r["budget"] or 0)) and (r["budget"] or 0) > 0
    # fixed vs variable via dim_platform.is_variable_cost
    split = q(
        """SELECT CASE WHEN p.is_variable_cost='TRUE' THEN 'Variable (usage)'
                       ELSE 'Fixed (seats)' END AS label,
                  ROUND(SUM(g.cost),2) AS value
           FROM gold g JOIN dim_platform p ON g.platform_key=p.platform_key
           GROUP BY label ORDER BY value DESC"""
    )
    # simple run-rate forecast: latest-month daily avg * 30
    days = scalar(f"SELECT COUNT(DISTINCT usage_date) FROM gold WHERE {MONTH_FILTER}", (LATEST_MONTH,))
    forecast = round((month_total / days) * 30, 2) if days else 0
    return {
        "kpis": [
            {"label": "Total AI Spend (all)", "value": f"${total:,.0f}"},
            {"label": f"Spend — {LATEST_MONTH}", "value": f"${month_total:,.0f}"},
            {"label": "Forecast (run-rate/mo)", "value": f"${forecast:,.0f}"},
            {"label": "Business Units", "value": f"{len([r for r in by_bu if r['label']])}"},
        ],
        "bars": [{"label": r["label"] or "Unallocated", "value": r["value"]} for r in by_bu],
        "split": split,
        "budget_table": by_bu,
    }


def payload_governance():
    by_platform = q(
        """SELECT p.platform_name AS label, ROUND(SUM(g.cost),2) AS value,
                  p.data_source AS source
           FROM gold g JOIN dim_platform p ON g.platform_key=p.platform_key
           GROUP BY p.platform_name ORDER BY value DESC"""
    )
    by_model = q(
        """SELECT model_key AS label, ROUND(SUM(cost),2) AS value
           FROM gold WHERE model_key NOT IN ('','unknown')
           GROUP BY model_key ORDER BY value DESC"""
    )
    by_class = q(
        """SELECT i.identity_class AS label, COUNT(DISTINCT g.identity_key) AS value
           FROM gold g LEFT JOIN dim_identity i ON g.identity_key=i.identity_key
           WHERE g.identity_key NOT IN ('','unknown')
           GROUP BY i.identity_class ORDER BY value DESC"""
    )
    humans = scalar("SELECT COUNT(*) FROM dim_identity WHERE is_human='TRUE'")
    nonhuman = scalar("SELECT COUNT(*) FROM dim_identity WHERE is_human!='TRUE'")
    platforms = scalar("SELECT COUNT(*) FROM dim_platform")
    unalloc = scalar("SELECT ROUND(SUM(cost),2) FROM gold WHERE business_unit_key='BU-UNALLOC'")
    return {
        "kpis": [
            {"label": "AI Platforms", "value": str(platforms)},
            {"label": "Human Identities", "value": str(humans)},
            {"label": "Non-Human (SP/agent)", "value": str(nonhuman)},
            {"label": "Unallocated $ (risk)", "value": f"${unalloc:,.0f}"},
        ],
        "bars": [{"label": r["label"], "value": r["value"]} for r in by_platform],
        "models": by_model,
        "identity_mix": by_class,
        "platform_table": by_platform,
    }


def payload_engineering():
    tokens = scalar("SELECT SUM(input_tokens)+SUM(output_tokens) FROM gold")
    requests = scalar("SELECT SUM(requests) FROM gold")
    errors = scalar("SELECT SUM(CASE WHEN is_error IN ('True','TRUE','1') THEN 1 ELSE 0 END) FROM gold")
    avg_lat = scalar("SELECT AVG(latency_ms) FROM gold WHERE latency_ms>0")
    by_model = q(
        """SELECT model_key AS label,
                  ROUND(SUM(input_tokens)+SUM(output_tokens),0) AS value,
                  ROUND(AVG(CASE WHEN latency_ms>0 THEN latency_ms END),0) AS latency,
                  SUM(requests) AS reqs
           FROM gold WHERE model_key NOT IN ('','unknown')
           GROUP BY model_key ORDER BY value DESC"""
    )
    return {
        "kpis": [
            {"label": "Total Tokens", "value": f"{tokens:,.0f}"},
            {"label": "Requests", "value": f"{requests:,.0f}"},
            {"label": "Errors", "value": f"{errors:,.0f}"},
            {"label": "Avg Latency (ms)", "value": f"{avg_lat:,.0f}"},
        ],
        "bars": [{"label": r["label"], "value": r["value"]} for r in by_model],
        "model_table": by_model,
    }


def payload_appowner():
    by_app = q(
        """SELECT a.application_name AS label, ROUND(SUM(g.cost),2) AS value,
                  b.business_unit_name AS bu, a.criticality AS crit
           FROM gold g LEFT JOIN dim_application a ON g.application_key=a.application_key
                       LEFT JOIN dim_business_unit b ON a.owner_business_unit_key=b.business_unit_key
           WHERE g.application_key NOT IN ('','APP-UNKNOWN')
           GROUP BY a.application_name ORDER BY value DESC"""
    )
    app_model = q(
        """SELECT a.application_name AS app, g.model_key AS model, ROUND(SUM(g.cost),2) AS value
           FROM gold g JOIN dim_application a ON g.application_key=a.application_key
           WHERE g.model_key NOT IN ('','unknown')
           GROUP BY a.application_name, g.model_key ORDER BY value DESC LIMIT 12"""
    )
    return {
        "kpis": [
            {"label": "Applications", "value": str(len(by_app))},
            {"label": "Top App Spend", "value": f"${by_app[0]['value']:,.0f}" if by_app else "$0"},
            {"label": "Top App", "value": by_app[0]["label"] if by_app else "—"},
        ],
        "bars": [{"label": r["label"], "value": r["value"]} for r in by_app],
        "app_table": by_app,
        "app_model": app_model,
    }


def idle_licensed_rows():
    """Identities that incur seat_day cost on a seat platform but have zero
    activity (prompt/premium_request/etc.) in the latest month -> reclaimable."""
    seat_in = ",".join("?" for _ in SEAT_PLATFORMS)
    act_in = ",".join("?" for _ in ACTIVITY_UNITS)
    rows = q(
        f"""
        WITH seats AS (
          SELECT DISTINCT g.identity_key, g.platform_key
          FROM gold g WHERE g.unit_type='seat_day'
            AND g.platform_key IN ({seat_in})
        ),
        active AS (
          SELECT DISTINCT identity_key, platform_key
          FROM gold WHERE unit_type IN ({act_in})
        ),
        seatcost AS (
          SELECT identity_key, platform_key, ROUND(SUM(cost),2) AS seat_cost
          FROM gold WHERE unit_type='seat_day' AND platform_key IN ({seat_in})
          GROUP BY identity_key, platform_key
        )
        SELECT s.platform_key AS platform,
               COALESCE(i.display_name, s.identity_key) AS person,
               COALESCE(b.business_unit_name,'—') AS bu,
               sc.seat_cost AS wasted
        FROM seats s
        LEFT JOIN active a ON s.identity_key=a.identity_key AND s.platform_key=a.platform_key
        JOIN seatcost sc ON s.identity_key=sc.identity_key AND s.platform_key=sc.platform_key
        LEFT JOIN dim_identity i ON s.identity_key=i.identity_key
        LEFT JOIN dim_business_unit b ON i.home_business_unit_key=b.business_unit_key
        WHERE a.identity_key IS NULL
        ORDER BY wasted DESC
        """,
        (*SEAT_PLATFORMS, *ACTIVITY_UNITS, *SEAT_PLATFORMS),
    )
    return rows


def payload_license():
    idle = idle_licensed_rows()
    total_seat = scalar(
        f"SELECT ROUND(SUM(cost),2) FROM gold WHERE unit_type='seat_day' "
        f"AND platform_key IN ({','.join('?' for _ in SEAT_PLATFORMS)})",
        SEAT_PLATFORMS,
    )
    reclaim = round(sum(r["wasted"] for r in idle), 2)
    seats_total = scalar(
        f"SELECT COUNT(DISTINCT identity_key||platform_key) FROM gold WHERE unit_type='seat_day' "
        f"AND platform_key IN ({','.join('?' for _ in SEAT_PLATFORMS)})",
        SEAT_PLATFORMS,
    )
    util = round(100 * (1 - (len(idle) / seats_total)), 1) if seats_total else 0
    return {
        "kpis": [
            {"label": "Total Seat Spend", "value": f"${total_seat:,.0f}"},
            {"label": "Idle Licensed Seats", "value": str(len(idle))},
            {"label": "Reclaimable $", "value": f"${reclaim:,.0f}"},
            {"label": "Seat Utilization", "value": f"{util}%"},
        ],
        "bars": [{"label": f"{r['person']} ({r['platform']})", "value": r["wasted"]} for r in idle],
        "idle_table": idle,
    }


PERSONAS = {
    "cfo": ("CFO / Finance", payload_cfo),
    "governance": ("Governance", payload_governance),
    "engineering": ("Engineering", payload_engineering),
    "appowner": ("Application Owner", payload_appowner),
    "license": ("License Optimization", payload_license),
}


# --------------------------------------------------------------------------- #
#  AI Insight layer: natural language -> SQL -> grounded answer                #
# --------------------------------------------------------------------------- #
def ask(question):
    ql = question.lower().strip()

    def has(*words):
        return any(w in ql for w in words)

    # 1) idle / reclaim / unused licenses
    if has("idle", "reclaim", "unused", "wasted", "underutil", "under-util"):
        rows = idle_licensed_rows()
        total = sum(r["wasted"] for r in rows)
        ans = (f"{len(rows)} licensed seats are idle (no activity in {LATEST_MONTH}), "
               f"wasting ${total:,.0f}. Top: "
               + ", ".join(f"{r['person']} ({r['platform']}, ${r['wasted']:,.0f})" for r in rows[:3])
               + ". Reclaim these seats.")
        sql = ("-- seats with cost but zero activity this month\n"
               "SELECT identity, platform, seat_cost FROM seats\n"
               "LEFT JOIN activity USING(identity,platform)\n"
               "WHERE activity IS NULL ORDER BY seat_cost DESC;")
        return ans, sql, rows

    # 2) which business unit spent the most
    if has("business unit", "which bu", "by bu", "department", "division") or ("unit" in ql and has("most", "spend", "top", "highest")):
        rows = q(
            f"""SELECT b.business_unit_name AS business_unit, ROUND(SUM(g.cost),2) AS spend
                FROM gold g LEFT JOIN dim_business_unit b ON g.business_unit_key=b.business_unit_key
                WHERE {MONTH_FILTER} AND g.business_unit_key!='BU-UNALLOC'
                GROUP BY b.business_unit_name ORDER BY spend DESC""",
            (LATEST_MONTH,),
        )
        top = rows[0] if rows else {"business_unit": "—", "spend": 0}
        ans = (f"In {LATEST_MONTH}, **{top['business_unit']}** spent the most at "
               f"${top['spend']:,.0f}, followed by "
               + ", ".join(f"{r['business_unit']} (${r['spend']:,.0f})" for r in rows[1:3]) + ".")
        sql = (f"SELECT business_unit_name, SUM(cost_usd) AS spend\n"
               f"FROM fact_ai_usage JOIN dim_business_unit USING(business_unit_key)\n"
               f"WHERE month='{LATEST_MONTH}' GROUP BY 1 ORDER BY spend DESC;")
        return ans, sql, rows

    # 3) highest cost models
    if has("model") and has("cost", "expensive", "highest", "spend", "top"):
        rows = q(
            """SELECT model_key AS model, ROUND(SUM(cost),2) AS spend,
                      SUM(requests) AS requests
               FROM gold WHERE model_key NOT IN ('','unknown')
               GROUP BY model_key ORDER BY spend DESC"""
        )
        top = rows[0] if rows else {"model": "—", "spend": 0}
        ans = (f"Highest-cost model is **{top['model']}** at ${top['spend']:,.0f}. "
               + "Full ranking below.")
        sql = ("SELECT model_key, SUM(cost_usd) AS spend, SUM(requests)\n"
               "FROM fact_ai_usage GROUP BY 1 ORDER BY spend DESC;")
        return ans, sql, rows

    # 4) applications increased / by application
    if has("application", "app "):
        rows = q(
            """SELECT COALESCE(a.application_name,g.application_key) AS application,
                      ROUND(SUM(g.cost),2) AS spend
               FROM gold g LEFT JOIN dim_application a ON g.application_key=a.application_key
               WHERE g.application_key NOT IN ('','APP-UNKNOWN')
               GROUP BY application ORDER BY spend DESC"""
        )
        top = rows[0] if rows else {"application": "—", "spend": 0}
        ans = f"Top application by AI spend is **{top['application']}** (${top['spend']:,.0f})."
        sql = ("SELECT application_name, SUM(cost_usd) AS spend\n"
               "FROM fact_ai_usage JOIN dim_application USING(application_key)\n"
               "GROUP BY 1 ORDER BY spend DESC;")
        return ans, sql, rows

    # 5) by platform
    if has("platform", "foundry", "copilot", "github"):
        rows = q(
            """SELECT p.platform_name AS platform, ROUND(SUM(g.cost),2) AS spend
               FROM gold g JOIN dim_platform p ON g.platform_key=p.platform_key
               GROUP BY p.platform_name ORDER BY spend DESC"""
        )
        top = rows[0] if rows else {"platform": "—", "spend": 0}
        ans = f"Spend by platform — largest is **{top['platform']}** (${top['spend']:,.0f})."
        sql = ("SELECT platform_name, SUM(cost_usd) AS spend\n"
               "FROM fact_ai_usage JOIN dim_platform USING(platform_key)\n"
               "GROUP BY 1 ORDER BY spend DESC;")
        return ans, sql, rows

    # 6) fixed vs variable
    if has("fixed", "variable"):
        rows = q(
            """SELECT CASE WHEN p.is_variable_cost='TRUE' THEN 'Variable (usage)'
                           ELSE 'Fixed (seats)' END AS cost_type,
                      ROUND(SUM(g.cost),2) AS spend
               FROM gold g JOIN dim_platform p ON g.platform_key=p.platform_key
               GROUP BY cost_type ORDER BY spend DESC"""
        )
        tot = sum(r["spend"] for r in rows) or 1
        ans = "Cost split: " + ", ".join(
            f"{r['cost_type']} ${r['spend']:,.0f} ({100*r['spend']/tot:.0f}%)" for r in rows)
        sql = ("SELECT is_variable_cost, SUM(cost_usd)\n"
               "FROM fact_ai_usage JOIN dim_platform USING(platform_key) GROUP BY 1;")
        return ans, sql, rows

    # 7) reduce / optimize / save
    if has("reduce", "optimi", "save", "cut", "lower"):
        idle = idle_licensed_rows()
        idle_total = sum(r["wasted"] for r in idle)
        var = q(
            """SELECT p.platform_name AS platform, ROUND(SUM(g.cost),2) AS spend
               FROM gold g JOIN dim_platform p ON g.platform_key=p.platform_key
               WHERE p.is_variable_cost='TRUE'
               GROUP BY p.platform_name ORDER BY spend DESC LIMIT 3"""
        )
        ans = (f"Two levers: (1) reclaim {len(idle)} idle seats to save "
               f"${idle_total:,.0f}/mo; (2) tune top variable-spend platforms "
               + ", ".join(f"{r['platform']} (${r['spend']:,.0f})" for r in var) + ".")
        return ans, "-- idle seats + top variable platforms (see License tab)", idle + var

    # 8) total spend / fallback
    total = scalar("SELECT ROUND(SUM(cost),2) FROM gold")
    month = scalar(f"SELECT ROUND(SUM(cost),2) FROM gold WHERE {MONTH_FILTER}", (LATEST_MONTH,))
    rows = q(
        """SELECT p.platform_name AS platform, ROUND(SUM(g.cost),2) AS spend
           FROM gold g JOIN dim_platform p ON g.platform_key=p.platform_key
           GROUP BY p.platform_name ORDER BY spend DESC"""
    )
    ans = (f"Total AI spend is ${total:,.0f} (${month:,.0f} in {LATEST_MONTH}) "
           f"across {len(rows)} platforms. Ask me about business units, models, "
           f"applications, or idle licenses.")
    sql = "SELECT SUM(cost_usd) FROM fact_ai_usage;"
    return ans, sql, rows


SUGGESTED = [
    "Which business unit spent the most last month?",
    "Which licenses should we reclaim?",
    "What are the highest cost models?",
    "Which applications cost the most?",
    "What is the fixed vs variable split?",
    "Where can we reduce AI spend?",
]


# --------------------------------------------------------------------------- #
#  HTTP handler                                                               #
# --------------------------------------------------------------------------- #
class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        b = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if self.path == "/" or self.path.startswith("/index"):
            return self._send(200, PAGE, "text/html; charset=utf-8")
        if self.path == "/api/personas":
            data = {k: {"title": v[0], **v[1]()} for k, v in PERSONAS.items()}
            data["_meta"] = {"month": LATEST_MONTH, "suggested": SUGGESTED,
                             "tables": LOADED}
            return self._send(200, json.dumps(data))
        return self._send(404, json.dumps({"error": "not found"}))

    def do_POST(self):
        if self.path == "/api/ask":
            n = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(n) or b"{}")
            ans, sql, rows = ask(body.get("q", ""))
            return self._send(200, json.dumps({"answer": ans, "sql": sql, "rows": rows[:20]}))
        return self._send(404, json.dumps({"error": "not found"}))


# --------------------------------------------------------------------------- #
#  Single-page UI (inline, offline, no CDN)                                    #
# --------------------------------------------------------------------------- #
PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AI FinOps — Demo (MOCK DATA)</title>
<style>
:root{--bg:#0b1020;--card:#141b2e;--card2:#1b2540;--ink:#e8edf7;--mut:#93a0bd;
--acc:#4f8cff;--good:#3ddc97;--warn:#ffb020;--bad:#ff5c72;--line:#243050}
*{box-sizing:border-box}body{margin:0;font:14px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;
background:var(--bg);color:var(--ink)}
header{padding:16px 24px;background:linear-gradient(90deg,#0d1428,#101932);
border-bottom:1px solid var(--line);display:flex;align-items:center;gap:16px;flex-wrap:wrap}
h1{font-size:18px;margin:0;font-weight:700}
.badge{background:var(--warn);color:#241a00;font-weight:700;font-size:11px;
padding:2px 8px;border-radius:999px;letter-spacing:.5px}
.sub{color:var(--mut);font-size:12px}
nav{display:flex;gap:6px;padding:12px 24px;flex-wrap:wrap;border-bottom:1px solid var(--line);
background:#0c1120;position:sticky;top:0;z-index:5;background:#0c1120}
nav button{background:var(--card);color:var(--ink);border:1px solid var(--line);
padding:8px 14px;border-radius:8px;cursor:pointer;font-weight:600;font-size:13px}
nav button.active{background:var(--acc);border-color:var(--acc);color:#fff}
nav button.ai{background:linear-gradient(90deg,#6a5cff,#4f8cff);border:none;color:#fff}
main{padding:24px;max-width:1180px;margin:0 auto}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:14px;margin-bottom:20px}
.kpi{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px}
.kpi .v{font-size:26px;font-weight:800;margin-top:6px}
.kpi .l{color:var(--mut);font-size:12px;text-transform:uppercase;letter-spacing:.4px}
.panel{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:18px;margin-bottom:18px}
.panel h3{margin:0 0 14px;font-size:14px}
.bar{display:flex;align-items:center;gap:10px;margin:7px 0}
.bar .name{width:210px;font-size:12px;color:var(--mut);text-align:right;flex:none;
overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.bar .track{flex:1;background:#0c1120;border-radius:6px;height:20px;overflow:hidden;background:#0c1120}
.bar .fill{height:100%;background:linear-gradient(90deg,#4f8cff,#6a5cff);border-radius:6px}
.bar .val{width:96px;font-size:12px;font-weight:700;flex:none}
table{width:100%;border-collapse:collapse;font-size:12.5px}
th,td{text-align:left;padding:7px 10px;border-bottom:1px solid var(--line)}
th{color:var(--mut);font-weight:600;text-transform:uppercase;font-size:11px;letter-spacing:.3px}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
.pill{padding:1px 7px;border-radius:999px;font-size:11px;font-weight:700}
.pill.red{background:#3a1622;color:var(--bad)}.pill.green{background:#12301f;color:var(--good)}
.pill.mock{background:#3a2f12;color:var(--warn)}.pill.real{background:#12253a;color:var(--acc)}
.cols{display:grid;grid-template-columns:1fr 1fr;gap:18px}
@media(max-width:820px){.cols{grid-template-columns:1fr}.bar .name{width:120px}}
/* AI tab */
.ask{display:flex;gap:10px;margin-bottom:14px}
.ask input{flex:1;background:#0c1120;border:1px solid var(--line);color:var(--ink);
padding:12px 14px;border-radius:10px;font-size:14px}
.ask button{background:var(--acc);border:none;color:#fff;padding:0 20px;border-radius:10px;
font-weight:700;cursor:pointer}
.chips{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:16px}
.chip{background:var(--card2);border:1px solid var(--line);color:var(--ink);padding:7px 12px;
border-radius:999px;font-size:12.5px;cursor:pointer}
.chip:hover{border-color:var(--acc)}
.answer{background:linear-gradient(180deg,#182038,#151f38);border:1px solid #2b3a63;
background:#151f38;border-radius:12px;padding:16px;margin-bottom:14px}
.answer .a{font-size:15px;line-height:1.6}
.answer .a b{color:var(--good)}
.sqlbox{background:#0a0f1e;border:1px solid var(--line);border-radius:10px;padding:12px;
font:12px/1.5 ui-monospace,Menlo,Consolas,monospace;color:#9fd0ff;white-space:pre-wrap;margin:10px 0}
.tag{font-size:11px;color:var(--mut);text-transform:uppercase;letter-spacing:.4px;margin-bottom:4px}
.foot{color:var(--mut);font-size:11px;padding:16px 24px;border-top:1px solid var(--line)}
.thinking{color:var(--mut);font-style:italic}
</style></head><body>
<header>
  <h1>⚡ AI FinOps Platform</h1>
  <span class="badge">MOCK DATA</span>
  <span class="sub" id="meta">local demo · no Fabric / no Power BI license required</span>
</header>
<nav id="nav"></nav>
<main id="main"></main>
<div class="foot">Grounded in <code>AIFinOps.SemanticModel/data/*.csv</code> (Gold star schema).
This is a local mock-up of the deployed Fabric + Power BI experience. All figures are synthetic.</div>
<script>
let DATA=null, CUR='cfo';
const TABS=[['cfo','CFO / Finance'],['governance','Governance'],['engineering','Engineering'],
['appowner','App Owner'],['license','License Optimization'],['ai','🤖 Ask AI']];
const $=s=>document.querySelector(s);
const money=n=>'$'+Number(n).toLocaleString(undefined,{maximumFractionDigits:0});

function bars(items,fmt){
  if(!items||!items.length) return '<div class="sub">No data.</div>';
  const max=Math.max(...items.map(i=>Math.abs(i.value)))||1;
  return items.map(i=>`<div class="bar"><div class="name" title="${i.label}">${i.label}</div>
    <div class="track"><div class="fill" style="width:${Math.max(2,100*Math.abs(i.value)/max)}%"></div></div>
    <div class="val">${fmt?fmt(i.value):money(i.value)}</div></div>`).join('');
}
function kpis(arr){return `<div class="grid">`+arr.map(k=>
  `<div class="kpi"><div class="l">${k.label}</div><div class="v">${k.value}</div></div>`).join('')+`</div>`;}

function renderNav(){
  $('#nav').innerHTML=TABS.map(([k,l])=>
    `<button class="${k===CUR?'active':''} ${k==='ai'?'ai':''}" onclick="go('${k}')">${l}</button>`).join('');
}
function go(k){CUR=k;renderNav();render();}

function table(cols,rows,fmts){
  if(!rows||!rows.length)return '<div class="sub">No rows.</div>';
  const head=cols.map(c=>`<th class="${c.num?'num':''}">${c.label}</th>`).join('');
  const body=rows.map(r=>'<tr>'+cols.map(c=>{
    let v=r[c.k]; if(c.fmt)v=c.fmt(v,r);
    return `<td class="${c.num?'num':''}">${v==null?'—':v}</td>`;}).join('')+'</tr>').join('');
  return `<table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
}

function render(){
  if(!DATA){$('#main').innerHTML='<div class="sub">Loading…</div>';return;}
  if(CUR==='ai')return renderAI();
  const d=DATA[CUR];let h=kpis(d.kpis);
  if(CUR==='cfo'){
    h+=`<div class="cols"><div class="panel"><h3>AI Spend by Business Unit · ${DATA._meta.month}</h3>${bars(d.bars)}</div>
    <div class="panel"><h3>Fixed vs Variable</h3>${bars(d.split)}</div></div>`;
    h+=`<div class="panel"><h3>Budget Variance</h3>`+table(
      [{k:'label',label:'Business Unit'},{k:'value',label:'Spend',num:1,fmt:money},
       {k:'budget',label:'Budget',num:1,fmt:money},
       {k:'variance',label:'Variance',num:1,fmt:(v,r)=>`<span class="pill ${r.over?'red':'green'}">${money(v)}</span>`}],
      d.budget_table.filter(r=>r.label))+`</div>`;
  }else if(CUR==='governance'){
    h+=`<div class="panel"><h3>Spend by Platform (with data-source honesty)</h3>${bars(d.bars)}</div>`;
    h+=`<div class="cols"><div class="panel"><h3>Model Distribution</h3>${bars(d.models)}</div>
    <div class="panel"><h3>Identity Mix (human vs non-human)</h3>${bars(d.identity_mix,v=>v)}</div></div>`;
    h+=`<div class="panel"><h3>Platform Governance</h3>`+table(
      [{k:'label',label:'Platform'},{k:'value',label:'Spend',num:1,fmt:money},
       {k:'source',label:'Data Source',fmt:v=>{const m=/^REAL/.test(v);
         return `<span class="pill ${m?'real':'mock'}">${m?'REAL':'MOCK'}</span> ${v.replace(/^(REAL|MOCK)\s*-\s*/,'')}`;}}],
      d.platform_table)+`</div>`;
  }else if(CUR==='engineering'){
    h+=`<div class="panel"><h3>Tokens by Model</h3>${bars(d.bars,v=>Number(v).toLocaleString())}</div>`;
    h+=`<div class="panel"><h3>Model Performance</h3>`+table(
      [{k:'label',label:'Model'},{k:'value',label:'Tokens',num:1,fmt:v=>Number(v).toLocaleString()},
       {k:'reqs',label:'Requests',num:1,fmt:v=>Number(v).toLocaleString()},
       {k:'latency',label:'Avg Latency (ms)',num:1,fmt:v=>Number(v).toLocaleString()}],
      d.model_table)+`</div>`;
  }else if(CUR==='appowner'){
    h+=`<div class="panel"><h3>Cost by Application</h3>${bars(d.bars)}</div>`;
    h+=`<div class="cols"><div class="panel"><h3>Applications</h3>`+table(
      [{k:'label',label:'Application'},{k:'bu',label:'BU'},{k:'crit',label:'Crit'},
       {k:'value',label:'Spend',num:1,fmt:money}],d.app_table)+`</div>`;
    h+=`<div class="panel"><h3>App × Model Spend</h3>`+table(
      [{k:'app',label:'Application'},{k:'model',label:'Model'},{k:'value',label:'Spend',num:1,fmt:money}],
      d.app_model)+`</div></div>`;
  }else if(CUR==='license'){
    h+=`<div class="panel"><h3>💸 Idle Licensed Seats — reclaim these</h3>${bars(d.bars)}</div>`;
    h+=`<div class="panel"><h3>Reclaimable Detail</h3>`+table(
      [{k:'person',label:'User / Identity'},{k:'platform',label:'Platform'},{k:'bu',label:'BU'},
       {k:'wasted',label:'Wasted $/mo',num:1,fmt:v=>`<span class="pill red">${money(v)}</span>`}],
      d.idle_table)+`</div>`;
  }
  $('#main').innerHTML=h;
}

function renderAI(){
  const s=DATA._meta.suggested;
  $('#main').innerHTML=`
  <div class="panel">
    <h3>🤖 AI Insight Layer — ask in plain English</h3>
    <div class="sub" style="margin-bottom:12px">Natural language → SQL → grounded answer over the Gold table.
    This mocks Fabric Copilot / semantic-model Q&A, running fully locally.</div>
    <div class="ask"><input id="q" placeholder="e.g. Which business unit spent the most last month?"
      onkeydown="if(event.key==='Enter')send()"><button onclick="send()">Ask</button></div>
    <div class="chips">${s.map(x=>`<span class="chip" onclick="chip(this)">${x}</span>`).join('')}</div>
    <div id="out"></div>
  </div>`;
}
function chip(el){$('#q').value=el.textContent;send();}
async function send(){
  const q=$('#q').value.trim();if(!q)return;
  $('#out').innerHTML='<div class="answer thinking">Thinking… translating to SQL…</div>';
  const r=await fetch('/api/ask',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({q})});
  const d=await r.json();
  const ans=d.answer.replace(/\*\*(.+?)\*\*/g,'<b>$1</b>');
  let rowsHtml='';
  if(d.rows&&d.rows.length){
    const cols=Object.keys(d.rows[0]);
    const head=cols.map(c=>`<th>${c}</th>`).join('');
    const body=d.rows.map(row=>'<tr>'+cols.map(c=>{
      let v=row[c]; if(typeof v==='number'&&(/cost|spend|wasted/i.test(c)))v=money(v);
      else if(typeof v==='number')v=v.toLocaleString();
      return `<td class="${typeof row[c]==='number'?'num':''}">${v==null?'—':v}</td>`;}).join('')+'</tr>').join('');
    rowsHtml=`<table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
  }
  $('#out').innerHTML=`<div class="answer"><div class="tag">Answer</div><div class="a">${ans}</div></div>
    <div class="tag">SQL it ran (auditable)</div><div class="sqlbox">${d.sql}</div>
    <div class="tag">Evidence</div>${rowsHtml}`;
}

fetch('/api/personas').then(r=>r.json()).then(d=>{DATA=d;
  const t=Object.values(d._meta.tables).reduce((a,b)=>a+b,0);
  $('#meta').textContent=`local demo · ${Object.keys(d._meta.tables).length} tables · ${t.toLocaleString()} rows · latest month ${d._meta.month}`;
  renderNav();render();});
renderNav();
</script></body></html>"""


if __name__ == "__main__":
    print(f"AI FinOps local demo  →  http://localhost:{PORT}")
    print(f"Loaded {len(LOADED)} tables from {DATA}")
    print(f"Latest month in data: {LATEST_MONTH}")
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
