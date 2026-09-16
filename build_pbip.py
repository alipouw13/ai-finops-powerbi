#!/usr/bin/env python3
"""
Emit a Power BI Project (PBIP) — text-based, opens directly in Power BI Desktop.

  AIFinOps.pbip
  AIFinOps.SemanticModel/   definition.pbism + TMDL (model, tables, relationships)
  AIFinOps.Report/          definition.pbir + report.json (report.json via build_report.py)

TMDL is tab-indented. Generated rather than hand-written so indentation can't drift.

Every table partition reads its CSV through the single **DataFolder** parameter
(model.tmdl), never a baked absolute path, so the model stays portable. The
committed DataFolder value is a placeholder — point it at your clone once with
`python platform/validate/validate_pbip.py --fix-data-folder`.

Set the env var PBIP_OUT to write into a scratch directory (used to diff the
generator output against the committed model without touching the working tree).
"""
import json
import os
from pathlib import Path

ROOT = Path(__file__).parent
OUT = Path(os.environ.get("PBIP_OUT", ROOT))
SM = OUT / "AIFinOps.SemanticModel"
RP = OUT / "AIFinOps.Report"
(SM / "definition" / "tables").mkdir(parents=True, exist_ok=True)
RP.mkdir(parents=True, exist_ok=True)

T = "\t"


def w(p: Path, s: str):
    with open(p, "w", encoding="utf-8", newline="\n") as f:
        f.write(s)
    print(f"  {p.relative_to(OUT)}")


# ---------------------------------------------------------------- project files
w(OUT / "AIFinOps.pbip", json.dumps({
    "$schema": "https://developer.microsoft.com/json-schemas/fabric/pbip/pbipProperties/1.0.0/schema.json",
    "version": "1.0",
    "artifacts": [{"report": {"path": "AIFinOps.Report"}}],
    "settings": {"enableAutoRecovery": True},
}, indent=2))

w(SM / "definition.pbism", json.dumps({
    "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/semanticModel/definitionProperties/1.0.0/schema.json",
    "version": "4.2", "settings": {}
}, indent=2))

w(RP / "definition.pbir", json.dumps({
    "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definitionProperties/1.0.0/schema.json",
    "version": "4.0",
    "datasetReference": {"byPath": {"path": "../AIFinOps.SemanticModel"}},
}, indent=2))

for p, name in ((SM, "AIFinOps.SemanticModel"), (RP, "AIFinOps.Report")):
    w(p / ".platform", json.dumps({
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/gitIntegration/platformProperties/2.0.0/schema.json",
        "metadata": {"type": "SemanticModel" if p is SM else "Report", "displayName": name},
        "config": {"version": "2.0", "logicalId": "00000000-0000-0000-0000-00000000000" + ("1" if p is SM else "2")},
    }, indent=2))

w(SM / "definition" / "database.tmdl",
  "database\n" + T + "compatibilityLevel: 1567\n")


# ------------------------------------------------------------------- emit table
# A column is a dict: n(name) t(dataType) fmt hide key sort sc(summarizeBy) desc
# A measure is a dict: name  expr(str=inline / list=fenced block)  fmt  desc
def emit_table(t: dict) -> str:
    L: list[str] = []
    for d in t.get("table_desc") or []:
        L.append(f"/// {d}")
    L.append(f"table {t['name']}")
    if t.get("prop"):
        L.append(f"{T}{t['prop']}")
    L.append("")

    for c in t["columns"]:
        for d in c.get("desc") or []:
            L.append(f"{T}/// {d}")
        L.append(f"{T}column {c['n']}")
        L.append(f"{T}{T}dataType: {c['t']}")
        if c.get("fmt"):
            L.append(f"{T}{T}formatString: {c['fmt']}")
        if c.get("hide"):
            L.append(f"{T}{T}isHidden")
        if c.get("key"):
            L.append(f"{T}{T}isKey")
        if c.get("sort"):
            L.append(f"{T}{T}sortByColumn: {c['sort']}")
        L.append(f"{T}{T}summarizeBy: {c.get('sc', 'none')}")
        L.append(f"{T}{T}sourceColumn: {c['n']}")
        L.append("")

    if t.get("measures"):
        for _ in range(t.get("measures_gap", 1) - 1):
            L.append("")
        for m in t["measures"]:
            for d in m.get("desc") or []:
                L.append(f"{T}/// {d}")
            expr = m["expr"]
            if isinstance(expr, list):
                L.append(f"{T}measure '{m['name']}' = ```")
                for bl in expr:
                    L.append(f"{T}{T}{T}{bl}")
                L.append(f"{T}{T}{T}```")
            else:
                L.append(f"{T}measure '{m['name']}' = {expr}")
            if m.get("fmt") is not None:
                L.append(f"{T}{T}formatString: {m['fmt']}")
            L.append("")

    csv = t.get("csv", t["name"] + ".csv")
    casts = ", ".join('{"%s", %s}' % (c, ty) for c, ty in t["casts"])
    L.append(f"{T}partition {t['name']} = m")
    L.append(f"{T}{T}mode: import")
    L.append(f"{T}{T}source =")
    L.append(f"{T}{T}{T}let")
    L.append(f'{T}{T}{T}{T}Src = Csv.Document(File.Contents(DataFolder & "{csv}"),'
             f'[Delimiter=",", Encoding=65001, QuoteStyle=QuoteStyle.Csv]),')
    L.append(f"{T}{T}{T}{T}Hdr = Table.PromoteHeaders(Src, [PromoteAllScalars=true]),")
    L.append(f"{T}{T}{T}{T}Typed = Table.TransformColumnTypes(Hdr, {{{casts}}})")
    L.append(f"{T}{T}{T}in")
    L.append(f"{T}{T}{T}{T}Typed")
    return "\n".join(L) + "\n"


STR, INT, DEC, DT, BOOL = "string", "int64", "double", "dateTime", "boolean"
Ttext, Tint, Tnum, Tdate, Tlog = "type text", "Int64.Type", "type number", "type date", "type logical"
MONEY, MONEY4, MONEY6 = r"\$#,0.00", r"\$#,0.0000", r"\$#,0.000000"
PCT, NUM = "0.0%", "#,0"


def hk(n, t=STR):  # hidden key column
    return {"n": n, "t": t, "hide": True, "key": True}


# -------------------------------------------------------------- fact measures
FACT_MEASURES = [
    {"name": "Total AI Cost", "expr": "SUM(fact_ai_usage[cost_usd])", "fmt": MONEY},
    {"desc": ["Cost sourced from a real billing surface (Foundry gateway rates, GitHub netAmount)."],
     "name": "Billed Cost", "expr": "CALCULATE([Total AI Cost], fact_ai_usage[cost_is_estimated] = FALSE)", "fmt": MONEY},
    {"desc": ["Cost derived from dim_rate_card. Accuracy depends entirely on the customer's rate card."],
     "name": "Modelled Cost", "expr": "CALCULATE([Total AI Cost], fact_ai_usage[cost_is_estimated] = TRUE)", "fmt": MONEY},
    {"desc": ["Share of total spend that is billed rather than modelled. Put this on page 1."],
     "name": "Cost Confidence %", "expr": "DIVIDE([Billed Cost] + 0, [Total AI Cost])", "fmt": PCT},
    {"desc": ["Licence cost (seat_day only). Does not vary with usage. M365 Cowork (copilot_credit) is a",
              "consumptive add-on and is deliberately excluded here - it sits inside Variable Cost."],
     "name": "Fixed Cost",
     "expr": ['CALCULATE([Total AI Cost], fact_ai_usage[unit_type] IN { "seat_day" })'], "fmt": MONEY},
    {"desc": ["Consumption cost. The only half a FinOps programme can actually influence."],
     "name": "Variable Cost",
     "expr": ['CALCULATE([Total AI Cost], NOT ( fact_ai_usage[unit_type] IN { "seat_day" } ))'], "fmt": MONEY},
    {"name": "Variable Cost %", "expr": "DIVIDE([Variable Cost], [Total AI Cost])", "fmt": PCT},
    {"name": "Total Tokens",
     "expr": ['CALCULATE(SUM(fact_ai_usage[quantity]), fact_ai_usage[unit_type] = "token")'], "fmt": NUM},
    {"name": "Input Tokens", "expr": "SUM(fact_ai_usage[input_tokens])", "fmt": NUM},
    {"name": "Output Tokens", "expr": "SUM(fact_ai_usage[output_tokens])", "fmt": NUM},
    {"name": "Cached Tokens", "expr": "SUM(fact_ai_usage[cached_tokens])", "fmt": NUM},
    {"desc": ["Cached input bills at a lower rate. Direct, actionable saving."],
     "name": "Cache Hit Rate", "expr": "DIVIDE([Cached Tokens], [Input Tokens])", "fmt": PCT},
    {"name": "Cost per 1K Tokens",
     "expr": ['DIVIDE(CALCULATE([Total AI Cost], fact_ai_usage[unit_type] = "token"), DIVIDE([Total Tokens], 1000))'],
     "fmt": MONEY4},
    {"desc": ["Copilot Credits consumed. Spans both Copilot Studio and M365 Copilot Cowork - the same",
              "$0.01 usage-based currency across both surfaces."],
     "name": "Copilot Credits",
     "expr": ['CALCULATE(SUM(fact_ai_usage[quantity]), fact_ai_usage[unit_type] = "copilot_credit")'], "fmt": NUM},
    {"name": "Premium Requests",
     "expr": ['CALCULATE(SUM(fact_ai_usage[quantity]), fact_ai_usage[unit_type] = "premium_request")'], "fmt": NUM},
    {"desc": ["Usage signal only. M365 Copilot prompts are never billable."],
     "name": "M365 Prompts",
     "expr": ['CALCULATE(SUM(fact_ai_usage[quantity]), fact_ai_usage[unit_type] = "prompt")'], "fmt": NUM},
    {"name": "Licensed Seats",
     "expr": ['CALCULATE(DISTINCTCOUNT(fact_ai_usage[identity_key]), fact_ai_usage[unit_type] = "seat_day")'], "fmt": NUM},
    {"name": "Total Requests", "expr": "SUM(fact_ai_usage[requests])", "fmt": NUM},
    {"name": "Active Users", "expr": "DISTINCTCOUNT(fact_ai_usage[identity_key])", "fmt": NUM},
    {"name": "Cost per Active User", "expr": "DIVIDE([Total AI Cost], [Active Users])", "fmt": MONEY},
    {"desc": ["Users holding a paid seat with zero activity in 28 days. The recoverable number."],
     "name": "Idle Licensed Users",
     "expr": ["VAR Win = DATESINPERIOD(dim_date[date_key], MAX(dim_date[date_key]), -28, DAY)",
              "RETURN",
              "COUNTROWS(",
              "FILTER(",
              "VALUES(dim_identity[identity_key]),",
              "CALCULATE([Licensed Seats], Win) > 0",
              "&& CALCULATE([Total Requests], Win) = 0",
              ")",
              ")"], "fmt": NUM},
    {"desc": ["Recoverable monthly spend from idle paid seats, priced at the seat_day rate only.",
              "(Previously averaged the whole rate card incl. token micro-prices, understating waste ~1000x.)"],
     "name": "Idle Seat Waste (monthly)",
     "expr": ["[Idle Licensed Users] * 30",
              '* CALCULATE(AVERAGE(dim_rate_card[unit_price_usd]), dim_rate_card[unit_type] = "seat_day")'], "fmt": MONEY},
    {"name": "Error Rate",
     "expr": ['DIVIDE(CALCULATE([Total Requests], fact_ai_usage[is_error] = "True"), [Total Requests])'], "fmt": PCT},
    {"name": "Avg Latency (ms)",
     "expr": ["AVERAGEX(FILTER(fact_ai_usage, fact_ai_usage[latency_ms] > 0), fact_ai_usage[latency_ms])"], "fmt": NUM},
    {"name": "Cost PM", "expr": "CALCULATE([Total AI Cost], DATEADD(dim_date[date_key], -1, MONTH))", "fmt": MONEY},
    {"name": "MoM Cost Delta %", "expr": "DIVIDE([Total AI Cost] - [Cost PM], [Cost PM])", "fmt": "+0.0%;-0.0%;0.0%"},
    {"name": "Cost (30d run-rate)",
     "expr": ["VAR D = COUNTROWS(VALUES(dim_date[date_key]))",
              "RETURN DIVIDE([Total AI Cost], D) * 30"], "fmt": MONEY},
    {"desc": ["Actual cost after each platform's negotiated discount (dim_platform[enterprise_discount_pct])."],
     "name": "Discounted Cost",
     "expr": ["SUMX(",
              "VALUES(dim_platform[platform_key]),",
              "[Total AI Cost] * (1 - CALCULATE(SELECTEDVALUE(dim_platform[enterprise_discount_pct], 0)))",
              ")"], "fmt": MONEY},
    {"desc": ["Savings unlocked by negotiated rates vs list. = Total AI Cost - Discounted Cost."],
     "name": "Discount Savings", "expr": "[Total AI Cost] - [Discounted Cost]", "fmt": MONEY},
    {"name": "Cost MTD", "expr": "CALCULATE([Total AI Cost], DATESMTD(dim_date[date_key]))", "fmt": MONEY},
    {"desc": ["Straight-line projection of the current month's MTD spend to month end."],
     "name": "Forecast Cost (EOM)",
     "expr": ["VAR LastDay = MAX(dim_date[date_key])",
              "VAR DaysInMonth = DAY(EOMONTH(LastDay, 0))",
              "VAR DaysElapsed = DAY(LastDay)",
              "RETURN DIVIDE([Cost MTD], DaysElapsed) * DaysInMonth"], "fmt": MONEY},
    {"desc": ["30-day forward projection at the trailing run-rate. Discounted basis."],
     "name": "Forecast Cost (next 30d, net)",
     "expr": ["VAR D = COUNTROWS(VALUES(dim_date[date_key]))",
              "RETURN DIVIDE([Discounted Cost], D) * 30"], "fmt": MONEY},
    {"desc": ["Spend that maps to a real business unit (excludes BU-UNALLOC). The directly billable pool."],
     "name": "Attributable Cost",
     "expr": ['CALCULATE([Total AI Cost], dim_business_unit[business_unit_key] <> "BU-UNALLOC")'], "fmt": MONEY},
    {"name": "Unallocated Cost",
     "expr": ['CALCULATE([Total AI Cost], dim_business_unit[business_unit_key] = "BU-UNALLOC")'], "fmt": MONEY},
    {"desc": ["Share of spend attributable to a business unit. The chargeback readiness number."],
     "name": "Chargeback Coverage %", "expr": "DIVIDE([Attributable Cost], [Total AI Cost])", "fmt": PCT},
    {"desc": ["Full allocation: a BU's direct spend plus its pro-rata share of unallocated spend."],
     "name": "Chargeback Cost",
     "expr": ["VAR Direct = [Attributable Cost]",
              "VAR TotalAttrib = CALCULATE([Attributable Cost], ALL(dim_business_unit))",
              "VAR Pool = CALCULATE([Unallocated Cost], ALL(dim_business_unit))",
              "RETURN Direct + Pool * DIVIDE(Direct, TotalAttrib)"], "fmt": MONEY},
    {"name": "Monthly Budget", "expr": "SUM(dim_business_unit[monthly_budget_usd])", "fmt": r"\$#,0"},
    {"desc": ["Projected month-end spend vs budget. Positive = over budget."],
     "name": "Budget Variance", "expr": "[Forecast Cost (EOM)] - [Monthly Budget]", "fmt": r"+\$#,0;-\$#,0;\$0"},
    {"name": "Budget Variance %",
     "expr": "DIVIDE([Forecast Cost (EOM)] - [Monthly Budget], [Monthly Budget])", "fmt": "+0.0%;-0.0%;0.0%"},
    {"desc": ["M365 Copilot Cowork usage in Copilot Credits ($0.01 each). Consumptive add-on over the seat."],
     "name": "Cowork Credits",
     "expr": ['CALCULATE(SUM(fact_ai_usage[quantity]), fact_ai_usage[unit_type] = "copilot_credit", dim_platform[platform_key] = "M365Copilot", dim_model[model_name] = "Cowork")'],
     "fmt": NUM},
    {"desc": ["Cowork add-on spend. The credit count is metered, but the dollars are modelled from the",
              "$0.01 Copilot Credit list price (cost_is_estimated = TRUE). Extra spend on top of the M365 seat."],
     "name": "Cowork Add-on Cost",
     "expr": ['CALCULATE([Total AI Cost], fact_ai_usage[unit_type] = "copilot_credit", dim_platform[platform_key] = "M365Copilot", dim_model[model_name] = "Cowork")'],
     "fmt": MONEY},
    {"desc": ["Spend at list / rate-card price across every platform (fact_ai_usage[list_cost_usd])."],
     "name": "Rate Card Cost", "expr": "SUM(fact_ai_usage[list_cost_usd])", "fmt": MONEY},
    {"desc": ["Share of spend whose row carries a genuine rate-card price (has_rate_card = TRUE)."],
     "name": "Rate Card Coverage %",
     "expr": ["DIVIDE(CALCULATE([Total AI Cost], fact_ai_usage[has_rate_card] = TRUE), [Total AI Cost])"], "fmt": PCT},
    {"desc": ["Active share of licensed seats = (Licensed Seats - Idle Licensed Users) / Licensed Seats."],
     "name": "Seat Utilisation %",
     "expr": "DIVIDE([Licensed Seats] - [Idle Licensed Users], [Licensed Seats])", "fmt": PCT},
    {"desc": ["Licensed identities with 1-20 requests in the trailing 28 days. Idle (0) is counted separately,",
              "so Low-Use and Idle Licensed Users never double count.",
              "The trailing + 0 makes a genuine zero read as 0 on a KPI card instead of an empty tile,",
              "which is indistinguishable from a broken visual."],
     "name": "Low-Use Licensed Seats",
     "expr": ["VAR Win = DATESINPERIOD(dim_date[date_key], MAX(dim_date[date_key]), -28, DAY)",
              "RETURN",
              "COUNTROWS(",
              "FILTER(",
              "VALUES(dim_identity[identity_key]),",
              "CALCULATE([Licensed Seats], Win) > 0",
              "&& CALCULATE([Total Requests], Win) >= 1",
              "&& CALCULATE([Total Requests], Win) <= 20",
              ")",
              ") + 0"], "fmt": NUM},
    {"desc": ["Monthly spend carried by low-use seats, priced at the seat_day rate. Spend *at risk*, not a saving."],
     "name": "Downgrade Candidate Spend (monthly)",
     "expr": ["[Low-Use Licensed Seats] * 30",
              '* CALCULATE(AVERAGE(dim_rate_card[unit_price_usd]), dim_rate_card[unit_type] = "seat_day")'], "fmt": MONEY},
    {"desc": ["Per-identity recommendation from 28-day activity. Text measure - use on the seat action queue."],
     "name": "Seat Action",
     "expr": ["VAR Win = DATESINPERIOD(dim_date[date_key], MAX(dim_date[date_key]), -28, DAY)",
              "VAR Seats = CALCULATE([Licensed Seats], Win)",
              "VAR Act = CALCULATE([Total Requests], Win)",
              "RETURN",
              "SWITCH(TRUE(),",
              'Seats = 0, "No licensed seat",',
              'Act = 0, "Reclaim - no activity in 28d",',
              'Act <= 20, "Review - low use (<20 req/28d)",',
              '"Healthy")'], "fmt": None},
    {"desc": ['Requests from telemetry carrying no principal (dim_identity[identity_class] = "Unknown").',
              "+ 0 so a fully-attributed tenant reads 0 on the KPI card rather than rendering blank."],
     "name": "Unattributed Requests",
     "expr": ['CALCULATE([Total Requests], dim_identity[identity_class] = "Unknown") + 0'], "fmt": NUM},
    {"name": "Attributed Requests", "expr": "[Total Requests] - [Unattributed Requests]", "fmt": NUM},
    {"name": "Unattributed Request %", "expr": "DIVIDE([Unattributed Requests], [Total Requests])", "fmt": PCT},
    {"desc": ["Spend on APP-UNKNOWN whose source feed named no workload (derived in 20_build_star.py)."],
     "name": "Unattributed Workload Cost",
     "expr": ['CALCULATE([Total AI Cost], dim_application[application_key] = "APP-UNKNOWN")'], "fmt": MONEY},
    {"desc": ["Share of spend that resolves to a named workload = 1 - unattributed / total."],
     "name": "Workload Attribution %",
     "expr": "1 - DIVIDE([Unattributed Workload Cost], [Total AI Cost])", "fmt": PCT},
    {"desc": ["Business units with a monthly budget set (> 0)."],
     "name": "Budgeted Business Units",
     "expr": ["CALCULATE(DISTINCTCOUNT(dim_business_unit[business_unit_key]), dim_business_unit[monthly_budget_usd] > 0)"],
     "fmt": NUM},
    {"desc": ["Share of business units that have a budget set."],
     "name": "Budget Coverage %",
     "expr": ["DIVIDE([Budgeted Business Units], CALCULATE(DISTINCTCOUNT(dim_business_unit[business_unit_key]), ALL(dim_business_unit)))"],
     "fmt": PCT},
]

# -------------------------------------------------------------------- tables
TABLES = [
    {"name": "fact_ai_usage", "measures_gap": 2, "measures": FACT_MEASURES,
     "columns": [
        {"n": "usage_date", "t": DT, "fmt": "yyyy-mm-dd", "hide": True},
        {"n": "platform_key", "t": STR, "hide": True},
        {"n": "identity_key", "t": STR, "hide": True},
        {"n": "model_key", "t": STR, "hide": True},
        {"n": "cost_center_key", "t": STR, "hide": True},
        {"n": "application_key", "t": STR, "hide": True},
        {"n": "environment_key", "t": STR, "hide": True},
        {"n": "business_unit_key", "t": STR, "hide": True},
        {"n": "unit_type", "t": STR},
        {"n": "quantity", "t": DEC, "fmt": NUM, "sc": "sum"},
        {"n": "input_tokens", "t": INT, "fmt": NUM, "hide": True, "sc": "sum"},
        {"n": "output_tokens", "t": INT, "fmt": NUM, "hide": True, "sc": "sum"},
        {"n": "cached_tokens", "t": INT, "fmt": NUM, "hide": True, "sc": "sum"},
        {"n": "requests", "t": INT, "fmt": NUM, "hide": True, "sc": "sum"},
        {"n": "cost_usd", "t": DEC, "fmt": MONEY4, "hide": True, "sc": "sum"},
        {"n": "list_cost_usd", "t": DEC, "fmt": MONEY4, "hide": True, "sc": "sum",
         "desc": ["What this row costs at list / rate-card price: quantity x dim_rate_card[unit_price_usd],",
                  "or for token rows the per-component input/output/cached model rates. When no rate-card row",
                  "exists (has_rate_card = FALSE) it falls back to cost_usd, so [Rate Card Cost] is never zero."]},
        {"n": "cost_is_estimated", "t": BOOL},
        {"n": "has_rate_card", "t": BOOL,
         "desc": ["TRUE when a genuine list/rate-card price was found for this (platform, unit_type, model);",
                  "token rows are priced from their per-component input/output/cached model rates.",
                  "FALSE (a model or unit with no rate-card row) means list_cost_usd is the cost_usd fallback."]},
        {"n": "is_error", "t": STR, "hide": True},
        {"n": "latency_ms", "t": DEC, "hide": True}],
     "casts": [("usage_date", Tdate), ("platform_key", Ttext), ("identity_key", Ttext),
               ("model_key", Ttext), ("cost_center_key", Ttext), ("application_key", Ttext),
               ("environment_key", Ttext), ("business_unit_key", Ttext), ("unit_type", Ttext),
               ("quantity", Tnum), ("input_tokens", Tint), ("output_tokens", Tint),
               ("cached_tokens", Tint), ("requests", Tint), ("cost_usd", Tnum),
               ("list_cost_usd", Tnum), ("cost_is_estimated", Tlog), ("has_rate_card", Tlog),
               ("is_error", Ttext), ("latency_ms", Tnum)]},

    {"name": "dim_date", "prop": "dataCategory: Time",
     "columns": [
        {"n": "date_key", "t": DT, "fmt": "yyyy-mm-dd", "key": True},
        {"n": "year", "t": INT},
        {"n": "quarter", "t": STR},
        {"n": "month", "t": INT, "hide": True},
        {"n": "month_name", "t": STR, "sort": "month"},
        {"n": "day", "t": INT},
        {"n": "day_name", "t": STR},
        {"n": "is_weekday", "t": BOOL},
        {"n": "year_month", "t": STR}],
     "casts": [("date_key", Tdate), ("year", Tint), ("quarter", Ttext), ("month", Tint),
               ("month_name", Ttext), ("day", Tint), ("day_name", Ttext),
               ("is_weekday", Tlog), ("year_month", Ttext)]},

    {"name": "dim_platform",
     "columns": [
        hk("platform_key"),
        {"n": "platform_name", "t": STR},
        {"n": "billing_model", "t": STR},
        {"n": "native_unit", "t": STR},
        {"n": "addon_unit", "t": STR,
         "desc": ["Consumptive add-on unit billed *on top of* the native unit. M365 Copilot Cowork",
                  "draws down Copilot Credits ($0.01 each) per task; empty where a platform has no add-on."]},
        {"n": "addon_billing_model", "t": STR,
         "desc": ["How the add-on is billed. Cowork is usage-based Copilot Credits stacked on the",
                  "fixed seat licence, so it is a consumptive charge over and above the native unit."]},
        {"n": "has_token_telemetry", "t": BOOL},
        {"n": "has_native_cost", "t": BOOL},
        {"n": "is_variable_cost", "t": BOOL},
        {"n": "data_source", "t": STR},
        {"n": "enterprise_discount_pct", "t": DEC, "fmt": PCT,
         "desc": ["MOCK negotiated discount off list (EA / volume tier / credit pack). Overwrite per contract."]}],
     "casts": [("platform_key", Ttext), ("platform_name", Ttext), ("billing_model", Ttext),
               ("native_unit", Ttext), ("addon_unit", Ttext), ("addon_billing_model", Ttext),
               ("has_token_telemetry", Tlog), ("has_native_cost", Tlog), ("is_variable_cost", Tlog),
               ("data_source", Ttext), ("enterprise_discount_pct", Tnum)]},

    {"name": "dim_identity",
     "columns": [
        hk("identity_key"),
        {"n": "display_name", "t": STR},
        {"n": "principal_type", "t": STR},
        {"n": "upn", "t": STR},
        {"n": "github_login", "t": STR},
        {"n": "team", "t": STR},
        {"n": "business_unit", "t": STR},
        {"n": "cost_center_key", "t": STR, "hide": True},
        {"n": "identity_class", "t": STR,
         "desc": ["Universal identity class: Human \u00b7 ServicePrincipal \u00b7 ManagedIdentity \u00b7 Agent \u00b7 Application.",
                  "Not every AI request maps to a person; this normalizes principals across all platforms."]},
        {"n": "is_human", "t": BOOL},
        {"n": "home_business_unit_key", "t": STR, "hide": True}],
     "casts": [("identity_key", Ttext), ("display_name", Ttext), ("principal_type", Ttext),
               ("upn", Ttext), ("github_login", Ttext), ("team", Ttext), ("business_unit", Ttext),
               ("cost_center_key", Ttext), ("identity_class", Ttext), ("is_human", Tlog),
               ("home_business_unit_key", Ttext)]},

    {"name": "dim_model",
     "columns": [
        hk("model_key"),
        {"n": "model_name", "t": STR},
        {"n": "model_version", "t": STR},
        {"n": "provider", "t": STR},
        {"n": "modality", "t": STR}],
     "casts": [("model_key", Ttext), ("model_name", Ttext), ("model_version", Ttext),
               ("provider", Ttext), ("modality", Ttext)]},

    {"name": "dim_cost_center",
     "columns": [
        hk("cost_center_key"),
        {"n": "cost_center_name", "t": STR},
        {"n": "business_unit", "t": STR},
        {"n": "owner_upn", "t": STR}],
     "casts": [("cost_center_key", Ttext), ("cost_center_name", Ttext),
               ("business_unit", Ttext), ("owner_upn", Ttext)]},

    {"name": "dim_rate_card",
     "columns": [
        hk("rate_key"),
        {"n": "platform", "t": STR},
        {"n": "unit_type", "t": STR},
        {"n": "model", "t": STR},
        {"n": "unit_price_usd", "t": DEC, "fmt": MONEY6},
        {"n": "effective_from", "t": DT, "fmt": "yyyy-mm-dd"},
        {"n": "currency", "t": STR},
        {"n": "source", "t": STR},
        {"n": "note", "t": STR}],
     "casts": [("rate_key", Ttext), ("platform", Ttext), ("unit_type", Ttext), ("model", Ttext),
               ("unit_price_usd", Tnum), ("effective_from", Tdate), ("currency", Ttext),
               ("source", Ttext), ("note", Ttext)]},

    {"name": "dim_business_unit",
     "columns": [
        hk("business_unit_key"),
        {"n": "business_unit_name", "t": STR},
        {"n": "division", "t": STR},
        {"n": "monthly_budget_usd", "t": INT, "fmt": r"\$#,0", "sc": "sum",
         "desc": ["MOCK monthly budget for variance/forecast demo. Overwrite with the customer's real budget."]},
        {"n": "executive_owner", "t": STR},
        {"n": "is_mock_budget", "t": BOOL}],
     "casts": [("business_unit_key", Ttext), ("business_unit_name", Ttext), ("division", Ttext),
               ("monthly_budget_usd", Tint), ("executive_owner", Ttext), ("is_mock_budget", Tlog)]},

    {"name": "dim_application",
     "columns": [
        hk("application_key"),
        {"n": "application_name", "t": STR},
        {"n": "application_type", "t": STR,
         "desc": ["api \u00b7 agent \u00b7 copilot \u00b7 notebook \u00b7 unattributed"]},
        {"n": "owner_business_unit_key", "t": STR, "hide": True},
        {"n": "owner_upn", "t": STR},
        {"n": "default_environment_key", "t": STR, "hide": True},
        {"n": "criticality", "t": STR},
        {"n": "is_mock", "t": BOOL,
         "desc": ["TRUE where the application's telemetry is mocked (see dim_platform[data_source])."]}],
     "casts": [("application_key", Ttext), ("application_name", Ttext), ("application_type", Ttext),
               ("owner_business_unit_key", Ttext), ("owner_upn", Ttext),
               ("default_environment_key", Ttext), ("criticality", Ttext), ("is_mock", Tlog)]},

    {"name": "dim_environment",
     "columns": [
        hk("environment_key"),
        {"n": "environment_name", "t": STR},
        {"n": "is_production", "t": BOOL},
        {"n": "sla_tier", "t": STR}],
     "casts": [("environment_key", Ttext), ("environment_name", Ttext),
               ("is_production", Tlog), ("sla_tier", Ttext)]},

    {"name": "dim_data_source", "csv": "extractable_data_catalog.csv",
     "table_desc": ["Catalogue of every extractable AI telemetry signal per platform: what can be",
                    "pulled, from which API, at what identity grain and cost fidelity, and whether it",
                    "is REAL / AVAILABLE / MOCK / ROADMAP today. Disconnected reference (no relationship)."],
     "columns": [
        {"n": "platform", "t": STR},
        {"n": "signal_category", "t": STR},
        {"n": "signal", "t": STR},
        {"n": "source_api", "t": STR},
        {"n": "grain", "t": STR},
        {"n": "identity_granularity", "t": STR},
        {"n": "cost_fidelity", "t": STR},
        {"n": "retention", "t": STR},
        {"n": "availability", "t": STR,
         "desc": ["REAL (live now) \u00b7 AVAILABLE (API exists, not yet wired) \u00b7 MOCK (needs SKU/policy) \u00b7 ROADMAP"]},
        {"n": "notes", "t": STR}],
     "measures": [
        {"name": "Extractable Signals", "expr": "COUNTROWS(dim_data_source)", "fmt": NUM},
        {"desc": ["Signals live in the model today (REAL data source)."],
         "name": "Signals Live (REAL)",
         "expr": ['CALCULATE(COUNTROWS(dim_data_source), dim_data_source[availability] = "REAL")'], "fmt": NUM},
        {"desc": ["Signals whose API exists and could be wired without new licensing."],
         "name": "Signals Available",
         "expr": ['CALCULATE(COUNTROWS(dim_data_source), dim_data_source[availability] = "AVAILABLE")'], "fmt": NUM}],
     "casts": [("platform", Ttext), ("signal_category", Ttext), ("signal", Ttext), ("source_api", Ttext),
               ("grain", Ttext), ("identity_granularity", Ttext), ("cost_fidelity", Ttext),
               ("retention", Ttext), ("availability", Ttext), ("notes", Ttext)]},
]

for t in TABLES:
    w(SM / "definition" / "tables" / f"{t['name']}.tmdl", emit_table(t))

# ---------------------------------------------------------------- model.tmdl
DATA_FOLDER_PLACEHOLDER = r"C:\path\to\ai-finops-powerbi\AIFinOps.SemanticModel\data" + "\\"
REF_ORDER = [t["name"] for t in TABLES]
RELATIONSHIPS = [
    ("rel_usage_date", "fact_ai_usage.usage_date", "dim_date.date_key"),
    ("rel_usage_platform", "fact_ai_usage.platform_key", "dim_platform.platform_key"),
    ("rel_usage_identity", "fact_ai_usage.identity_key", "dim_identity.identity_key"),
    ("rel_usage_model", "fact_ai_usage.model_key", "dim_model.model_key"),
    ("rel_usage_costcenter", "fact_ai_usage.cost_center_key", "dim_cost_center.cost_center_key"),
    ("rel_usage_businessunit", "fact_ai_usage.business_unit_key", "dim_business_unit.business_unit_key"),
    ("rel_usage_application", "fact_ai_usage.application_key", "dim_application.application_key"),
    ("rel_usage_environment", "fact_ai_usage.environment_key", "dim_environment.environment_key"),
]

ml: list[str] = [
    "model Model",
    f"{T}culture: en-US",
    f"{T}defaultPowerBIDataSourceVersion: powerBI_V3",
    f"{T}discourageImplicitMeasures",
    f"{T}sourceQueryCulture: en-US",
    f"{T}dataAccessOptions",
    f"{T}{T}legacyRedirects",
    f"{T}{T}returnErrorValuesAsNull",
    "",
    "/// Absolute path to AIFinOps.SemanticModel/data/ (include trailing separator).",
    "/// Every table partition reads its CSV from this single parameter so the model is",
    "/// portable. PBIP has no relative-path primitive, so point it at your own clone:",
    "///   python platform/validate/validate_pbip.py --fix-data-folder",
    "/// or set it manually via Transform data -> Manage Parameters -> DataFolder.",
    f'expression DataFolder = "{DATA_FOLDER_PLACEHOLDER}" meta [IsParameterQuery=true, Type="Text", IsParameterQueryRequired=true]',
    f"{T}lineageTag: 9c1b7a10-1111-4111-8111-000000000001",
    "",
    f"{T}annotation PBI_ResultType = Text",
    "",
]
ml += [f"ref table {n}" for n in REF_ORDER]
for name, frm, to in RELATIONSHIPS:
    ml += ["", f"relationship {name}", f"{T}fromColumn: {frm}", f"{T}toColumn: {to}"]
w(SM / "definition" / "model.tmdl", "\n".join(ml) + "\n")


# --- guard: Power BI names are case-insensitive; a measure may not share a name
# with a column in the same table. TMDL deserializers do not catch this.
def _check_collisions():
    import glob
    import re as _re
    bad = []
    for f in glob.glob(str(SM / "definition" / "tables" / "*.tmdl")):
        txt = Path(f).read_text(encoding="utf-8")
        cols = {c.lower() for c in _re.findall(r"^\tcolumn (\S+)", txt, _re.M)}
        meas = _re.findall(r"^\tmeasure '([^']+)'", txt, _re.M)
        for m in meas:
            if m.lower() in cols:
                bad.append(f"{Path(f).name}: measure '{m}' collides with a column")
    if bad:
        raise SystemExit("NAME COLLISIONS:\n  " + "\n  ".join(bad))
    print("  no measure/column name collisions")


_check_collisions()
print("\nPBIP written.")
