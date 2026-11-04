#!/usr/bin/env python3
"""
Emit a Power BI Project (PBIP) — text-based, opens directly in Power BI Desktop.

  AIFinOps.pbip
  AIFinOps.SemanticModel/   definition.pbism + TMDL (16 tables, 25 relationships, 58+ measures)
  AIFinOps.Report/          definition.pbir + report.json

TMDL is tab-indented. Generated deterministically so indentation and column
contracts never drift from `AIFinOps.SemanticModel/data/*.csv`.
"""
import glob
import json
import re as _re
from pathlib import Path

ROOT = Path(__file__).parent
SM = ROOT / "AIFinOps.SemanticModel"
RP = ROOT / "AIFinOps.Report"
(SM / "definition" / "tables").mkdir(parents=True, exist_ok=True)
RP.mkdir(parents=True, exist_ok=True)

T = "\t"

# Dynamic repo-local path for the DataFolder parameter (customers can also override in Power BI Desktop)
DEFAULT_DATA_DIR = str((SM / "data").resolve()) + "\\"


def w(p: Path, s: str):
    p.write_text(s, encoding="utf-8")
    print(f"  {p.relative_to(ROOT)}")


# ---------------------------------------------------------------- project files
w(ROOT / "AIFinOps.pbip", json.dumps({
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


# ------------------------------------------------------------------ table gen
def m_partition(table_name: str, casts: dict, csv_filename: str = None) -> str:
    """M expression reading a CSV from the DataFolder parameter."""
    fn = csv_filename or f"{table_name}.csv"
    types = ", ".join(f'{{"{c}", {t}}}' for c, t in casts.items())
    return (
        f"{T}partition {table_name} = m\n"
        f"{T}{T}mode: import\n"
        f"{T}{T}source =\n"
        f"{T}{T}{T}let\n"
        f'{T}{T}{T}{T}Src = Csv.Document(File.Contents(DataFolder & "{fn}"),'
        f"[Delimiter=\",\", Encoding=65001, QuoteStyle=QuoteStyle.Csv]),\n"
        f"{T}{T}{T}{T}Hdr = Table.PromoteHeaders(Src, [PromoteAllScalars=true]),\n"
        f"{T}{T}{T}{T}Typed = Table.TransformColumnTypes(Hdr, {{{types}}})\n"
        f"{T}{T}{T}in\n"
        f"{T}{T}{T}{T}Typed\n"
    )


def table(name: str, cols: list, casts: dict, extra: str = "",
          hidden=False, props: str = "", csv_filename: str = None) -> str:
    """props = table-level properties (must precede children).
       extra = child objects such as measures."""
    s = f"table {name}\n"
    if hidden:
        s += f"{T}isHidden\n"
    s += props
    s += "\n"
    for c in cols:
        if c.get("desc"):
            for line in c["desc"].splitlines():
                s += f"{T}/// {line}\n"
        s += f"{T}column {c['n']}\n"
        s += f"{T}{T}dataType: {c['t']}\n"
        if c.get("fmt"):
            s += f"{T}{T}formatString: {c['fmt']}\n"
        if c.get("hide"):
            s += f"{T}{T}isHidden\n"
        if c.get("key"):
            s += f"{T}{T}isKey\n"
        if c.get("sort"):
            s += f"{T}{T}sortByColumn: {c['sort']}\n"
        if c.get("sc"):
            s += f"{T}{T}summarizeBy: {c['sc']}\n"
        else:
            s += f"{T}{T}summarizeBy: none\n"
        s += f"{T}{T}sourceColumn: {c['n']}\n\n"
    s += extra
    s += m_partition(name, casts, csv_filename=csv_filename)
    return s


STR, INT, DEC, DT, BOOL = "string", "int64", "double", "dateTime", "boolean"
Mstr, Mint, Mnum, Mdate, Mlog = "type text", "Int64.Type", "type number", "type date", "type logical"

# ------------------------------------------------------------- fact_ai_usage
MEASURES = f"""
{T}/// Primary AI FinOps cost measure (= FOCUS EffectiveCost for Azure/FOCUS sources, rate-card cost for seat/pack models).
{T}measure 'Total AI Cost' = SUM(fact_ai_usage[cost_usd])
{T}{T}formatString: \\$#,0.00

{T}/// Cost sourced from a real billing surface (FOCUS 1.2 BilledCost/EffectiveCost, GitHub netAmount).
{T}measure 'Billed Cost' = CALCULATE([Total AI Cost], fact_ai_usage[cost_is_estimated] = FALSE)
{T}{T}formatString: \\$#,0.00

{T}/// Cost derived from dim_rate_card (M365/GitHub flat seats, prepaid credit packs).
{T}measure 'Modelled Cost' = CALCULATE([Total AI Cost], fact_ai_usage[cost_is_estimated] = TRUE)
{T}{T}formatString: \\$#,0.00

{T}/// Share of total spend that is invoice/FOCUS-backed rather than rate-card modelled.
{T}measure 'Cost Confidence %' = DIVIDE([Billed Cost], [Total AI Cost])
{T}{T}formatString: 0.0%

{T}/// Licence cost (M365 Copilot & GitHub Copilot seats). Does not vary with usage.
{T}measure 'Fixed Cost' =
{T}{T}CALCULATE([Total AI Cost], fact_ai_usage[unit_type] IN {{ "seat_day" }})
{T}{T}formatString: \\$#,0.00

{T}/// Consumption & reservation cost (tokens, PTUs, credits, premium requests, Azure AI meters).
{T}measure 'Variable Cost' =
{T}{T}CALCULATE([Total AI Cost], NOT ( fact_ai_usage[unit_type] IN {{ "seat_day" }} ))
{T}{T}formatString: \\$#,0.00

{T}measure 'Variable Cost %' = DIVIDE([Variable Cost], [Total AI Cost])
{T}{T}formatString: 0.0%

{T}measure 'Total Tokens' =
{T}{T}CALCULATE(SUM(fact_ai_usage[quantity]), fact_ai_usage[unit_type] = "token")
{T}{T}formatString: #,0

{T}measure 'Input Tokens' = SUM(fact_ai_usage[input_tokens])
{T}{T}formatString: #,0

{T}measure 'Output Tokens' = SUM(fact_ai_usage[output_tokens])
{T}{T}formatString: #,0

{T}measure 'Cached Tokens' = SUM(fact_ai_usage[cached_tokens])
{T}{T}formatString: #,0

{T}/// Cached prompt tokens billed at 75% discount. Direct, actionable tokenomics lever.
{T}measure 'Cache Hit Rate' = DIVIDE([Cached Tokens], [Input Tokens])
{T}{T}formatString: 0.0%

{T}measure 'Cost per 1K Tokens' =
{T}{T}DIVIDE(CALCULATE([Total AI Cost], fact_ai_usage[unit_type] = "token"), DIVIDE([Total Tokens], 1000))
{T}{T}formatString: \\$#,0.0000

{T}measure 'Copilot Credits' =
{T}{T}CALCULATE(SUM(fact_ai_usage[quantity]), fact_ai_usage[unit_type] = "copilot_credit")
{T}{T}formatString: #,0

{T}measure 'Premium Requests' =
{T}{T}CALCULATE(SUM(fact_ai_usage[quantity]), fact_ai_usage[unit_type] = "premium_request")
{T}{T}formatString: #,0

{T}/// Usage signal only. Standard M365 Copilot prompts are included in the seat licence.
{T}measure 'M365 Prompts' =
{T}{T}CALCULATE(SUM(fact_ai_usage[quantity]), fact_ai_usage[unit_type] = "prompt")
{T}{T}formatString: #,0

{T}measure 'Licensed Seats' =
{T}{T}CALCULATE(DISTINCTCOUNT(fact_ai_usage[identity_key]), fact_ai_usage[unit_type] = "seat_day")
{T}{T}formatString: #,0

{T}measure 'Total Requests' = SUM(fact_ai_usage[requests])
{T}{T}formatString: #,0

{T}measure 'Active Users' = DISTINCTCOUNT(fact_ai_usage[identity_key])
{T}{T}formatString: #,0

{T}measure 'Cost per Active User' = DIVIDE([Total AI Cost], [Active Users])
{T}{T}formatString: \\$#,0.00

{T}/// Users holding a paid seat with zero activity in 28 days. The recoverable seat count.
{T}measure 'Idle Licensed Users' =
{T}{T}VAR Win = DATESINPERIOD(dim_date[date_key], MAX(dim_date[date_key]), -28, DAY)
{T}{T}RETURN
{T}{T}COUNTROWS(
{T}{T}{T}FILTER(
{T}{T}{T}{T}VALUES(dim_identity[identity_key]),
{T}{T}{T}{T}CALCULATE([Licensed Seats], Win) > 0
{T}{T}{T}{T}{T}&& CALCULATE([M365 Prompts] + [Premium Requests] + [Total Requests], Win) = 0
{T}{T}{T})
{T}{T})
{T}{T}formatString: #,0

{T}/// Recoverable monthly spend from idle paid seats, priced at the seat_day rate only.
{T}measure 'Idle Seat Waste (monthly)' =
{T}{T}[Idle Licensed Users] * 30
{T}{T}{T}* CALCULATE(AVERAGE(dim_rate_card[unit_price_usd]), dim_rate_card[unit_type] = "seat_day")
{T}{T}formatString: \\$#,0.00

{T}measure 'Error Rate' =
{T}{T}DIVIDE(CALCULATE([Total Requests], fact_ai_usage[is_error] = "True"), [Total Requests])
{T}{T}formatString: 0.0%

{T}measure 'Avg Latency (ms)' =
{T}{T}AVERAGEX(FILTER(fact_ai_usage, fact_ai_usage[latency_ms] > 0), fact_ai_usage[latency_ms])
{T}{T}formatString: #,0

{T}measure 'Cost PM' = CALCULATE([Total AI Cost], DATEADD(dim_date[date_key], -1, MONTH))
{T}{T}formatString: \\$#,0.00

{T}measure 'MoM Cost Delta %' = DIVIDE([Total AI Cost] - [Cost PM], [Cost PM])
{T}{T}formatString: +0.0%;-0.0%;0.0%

{T}measure 'Cost (30d run-rate)' =
{T}{T}VAR D = COUNTROWS(VALUES(dim_date[date_key]))
{T}{T}RETURN DIVIDE([Total AI Cost], D) * 30
{T}{T}formatString: \\$#,0.00

{T}/// Actual cost after each platform's negotiated discount (dim_platform[enterprise_discount_pct]).
{T}measure 'Discounted Cost' =
{T}{T}SUMX(
{T}{T}{T}VALUES(dim_platform[platform_key]),
{T}{T}{T}[Total AI Cost] * (1 - CALCULATE(SELECTEDVALUE(dim_platform[enterprise_discount_pct], 0)))
{T}{T})
{T}{T}formatString: \\$#,0.00

{T}/// Savings unlocked by negotiated rates vs list. = Total AI Cost - Discounted Cost.
{T}measure 'Discount Savings' = [Total AI Cost] - [Discounted Cost]
{T}{T}formatString: \\$#,0.00

{T}/// Spend at list / rate-card price across every platform (fact_ai_usage[list_cost_usd]).
{T}measure 'Rate Card Cost' = SUM(fact_ai_usage[list_cost_usd])
{T}{T}formatString: \\$#,0.00

{T}/// Share of spend whose row carries a genuine rate-card price (has_rate_card = TRUE).
{T}measure 'Rate Card Coverage %' = DIVIDE(CALCULATE([Total AI Cost], fact_ai_usage[has_rate_card] = TRUE), [Total AI Cost])
{T}{T}formatString: 0.0%

{T}/// FOCUS ListCost / Rate-Card List Cost before negotiated EA/MCA or commitment discounts.
{T}measure 'FOCUS List Cost' = SUM(fact_ai_usage[list_cost_usd])
{T}{T}formatString: \\$#,0.00

{T}/// Total FinOps savings from FOCUS ListCost to EffectiveCost (negotiated EA/MCA + commitment discounts).
{T}measure 'Total FOCUS Savings' = [Rate Card Cost] - [Total AI Cost]
{T}{T}formatString: \\$#,0.00

{T}/// FinOps Foundation KPI: Effective Savings Rate = (ListCost - EffectiveCost) / ListCost.
{T}measure 'Effective Savings Rate (ESR %)' = DIVIDE([Rate Card Cost] - [Total AI Cost], [Rate Card Cost])
{T}{T}formatString: 0.0%

{T}/// M365 Copilot Cowork usage in Copilot Credits ($0.01 each). Consumptive add-on over the seat.
{T}measure 'Cowork Credits' = CALCULATE(SUM(fact_ai_usage[quantity]), fact_ai_usage[unit_type] = "copilot_credit", dim_platform[platform_key] = "M365Copilot", dim_model[model_name] = "Cowork")
{T}{T}formatString: #,0

{T}/// Cowork add-on spend. The credit count is metered, but the dollars are modelled from the
{T}/// $0.01 Copilot Credit list price (cost_is_estimated = TRUE). Extra spend on top of the M365 seat.
{T}measure 'Cowork Add-on Cost' = CALCULATE([Total AI Cost], fact_ai_usage[unit_type] = "copilot_credit", dim_platform[platform_key] = "M365Copilot", dim_model[model_name] = "Cowork")
{T}{T}formatString: \\$#,0.00

{T}/// Active share of licensed seats = (Licensed Seats - Idle Licensed Users) / Licensed Seats.
{T}measure 'Seat Utilisation %' = DIVIDE([Licensed Seats] - [Idle Licensed Users], [Licensed Seats])
{T}{T}formatString: 0.0%

{T}/// Licensed identities with 1-20 requests in the trailing 28 days. Idle (0) is counted separately.
{T}measure 'Low-Use Licensed Seats' =
{T}{T}VAR Win = DATESINPERIOD(dim_date[date_key], MAX(dim_date[date_key]), -28, DAY)
{T}{T}RETURN
{T}{T}COUNTROWS(
{T}{T}{T}FILTER(
{T}{T}{T}{T}VALUES(dim_identity[identity_key]),
{T}{T}{T}{T}CALCULATE([Licensed Seats], Win) > 0
{T}{T}{T}{T}{T}&& CALCULATE([Total Requests], Win) >= 1
{T}{T}{T}{T}{T}&& CALCULATE([Total Requests], Win) <= 20
{T}{T}{T})
{T}{T}) + 0
{T}{T}formatString: #,0

{T}/// Monthly spend carried by low-use seats, priced at the seat_day rate.
{T}measure 'Downgrade Candidate Spend (monthly)' =
{T}{T}[Low-Use Licensed Seats] * 30
{T}{T}{T}* CALCULATE(AVERAGE(dim_rate_card[unit_price_usd]), dim_rate_card[unit_type] = "seat_day")
{T}{T}formatString: \\$#,0.00

{T}/// Per-identity recommendation from 28-day activity. Text measure - use on the seat action queue.
{T}measure 'Seat Action' =
{T}{T}VAR Win = DATESINPERIOD(dim_date[date_key], MAX(dim_date[date_key]), -28, DAY)
{T}{T}VAR Seats = CALCULATE([Licensed Seats], Win)
{T}{T}VAR Act = CALCULATE([Total Requests], Win)
{T}{T}RETURN
{T}{T}SWITCH(TRUE(),
{T}{T}{T}Seats = 0, "No licensed seat",
{T}{T}{T}Act = 0, "Reclaim - no activity in 28d",
{T}{T}{T}Act <= 20, "Review - low use (<20 req/28d)",
{T}{T}{T}"Healthy")
{T}{T}formatString: @

{T}/// Requests from telemetry carrying no principal (dim_identity[identity_class] = "Unknown").
{T}measure 'Unattributed Requests' = CALCULATE([Total Requests], dim_identity[identity_class] = "Unknown") + 0
{T}{T}formatString: #,0

{T}measure 'Attributed Requests' = [Total Requests] - [Unattributed Requests]
{T}{T}formatString: #,0

{T}measure 'Unattributed Request %' = DIVIDE([Unattributed Requests], [Total Requests])
{T}{T}formatString: 0.0%

{T}/// Spend on APP-UNKNOWN whose source feed named no workload (derived in 20_build_star.py).
{T}measure 'Unattributed Workload Cost' = CALCULATE([Total AI Cost], dim_application[application_key] = "APP-UNKNOWN")
{T}{T}formatString: \\$#,0.00

{T}/// Share of spend that resolves to a named workload = 1 - unattributed / total.
{T}measure 'Workload Attribution %' = 1 - DIVIDE([Unattributed Workload Cost], [Total AI Cost])
{T}{T}formatString: 0.0%

{T}/// Business units with a monthly budget set (> 0).
{T}measure 'Budgeted Business Units' = CALCULATE(DISTINCTCOUNT(dim_business_unit[business_unit_key]), dim_business_unit[monthly_budget_usd] > 0)
{T}{T}formatString: #,0

{T}/// Share of business units that have a budget set.
{T}measure 'Budget Coverage %' = DIVIDE([Budgeted Business Units], CALCULATE(DISTINCTCOUNT(dim_business_unit[business_unit_key]), ALL(dim_business_unit)))
{T}{T}formatString: 0.0%

{T}/// Spend on Azure AI Services (invoiced via FOCUS: Document Intelligence, Speech, Content Safety, etc.).
{T}measure 'Azure AI Service Spend' = CALCULATE([Total AI Cost], dim_platform[platform_key] = "AzureAI")
{T}{T}formatString: \\$#,0.00

{T}/// Spend on Azure AI Supporting Infrastructure (AI Search, APIM, Cosmos DB, Redis Semantic Cache, Fabric, Monitor).
{T}measure 'Supporting AI Infra Spend' = CALCULATE([Total AI Cost], dim_platform[platform_key] = "AzureInfra")
{T}{T}formatString: \\$#,0.00

{T}/// Supporting infrastructure share of total AI TCO.
{T}measure 'Supporting Infra % of TCO' = DIVIDE([Supporting AI Infra Spend], [Total AI Cost])
{T}{T}formatString: 0.0%

{T}measure 'Cost MTD' = CALCULATE([Total AI Cost], DATESMTD(dim_date[date_key]))
{T}{T}formatString: \\$#,0.00

{T}/// Straight-line projection of the current month's MTD spend to month end.
{T}measure 'Forecast Cost (EOM)' =
{T}{T}VAR LastDay = MAX(dim_date[date_key])
{T}{T}VAR DaysInMonth = DAY(EOMONTH(LastDay, 0))
{T}{T}VAR DaysElapsed = DAY(LastDay)
{T}{T}RETURN DIVIDE([Cost MTD], DaysElapsed) * DaysInMonth
{T}{T}formatString: \\$#,0.00

{T}/// 30-day forward projection at the trailing run-rate. Discounted basis.
{T}measure 'Forecast Cost (next 30d, net)' =
{T}{T}VAR D = COUNTROWS(VALUES(dim_date[date_key]))
{T}{T}RETURN DIVIDE([Discounted Cost], D) * 30
{T}{T}formatString: \\$#,0.00

{T}/// Spend that maps to a real business unit (excludes BU-UNALLOC). The directly billable pool.
{T}measure 'Attributable Cost' =
{T}{T}CALCULATE([Total AI Cost], dim_business_unit[business_unit_key] <> "BU-UNALLOC")
{T}{T}formatString: \\$#,0.00

{T}measure 'Unallocated Cost' =
{T}{T}CALCULATE([Total AI Cost], dim_business_unit[business_unit_key] = "BU-UNALLOC")
{T}{T}formatString: \\$#,0.00

{T}/// Share of spend attributable to a business unit. The chargeback readiness number.
{T}measure 'Chargeback Coverage %' = DIVIDE([Attributable Cost], [Total AI Cost])
{T}{T}formatString: 0.0%

{T}/// Full allocation: a BU's direct spend plus its pro-rata share of unallocated spend.
{T}measure 'Chargeback Cost' =
{T}{T}VAR Direct = [Attributable Cost]
{T}{T}VAR TotalAttrib = CALCULATE([Attributable Cost], ALL(dim_business_unit))
{T}{T}VAR Pool = CALCULATE([Unallocated Cost], ALL(dim_business_unit))
{T}{T}RETURN Direct + Pool * DIVIDE(Direct, TotalAttrib)
{T}{T}formatString: \\$#,0.00

{T}measure 'Monthly Budget' = SUM(dim_business_unit[monthly_budget_usd])
{T}{T}formatString: \\$#,0

{T}/// Projected month-end spend vs budget. Positive = over budget.
{T}measure 'Budget Variance' = [Forecast Cost (EOM)] - [Monthly Budget]
{T}{T}formatString: +\\$#,0;-\\$#,0;\\$0

{T}measure 'Budget Variance %' = DIVIDE([Forecast Cost (EOM)] - [Monthly Budget], [Monthly Budget])
{T}{T}formatString: +0.0%;-0.0%;0.0%

"""

w(SM / "definition" / "tables" / "fact_ai_usage.tmdl", table(
    "fact_ai_usage",
    [{"n": "usage_date", "t": DT, "fmt": "yyyy-mm-dd", "hide": True},
     {"n": "platform_key", "t": STR, "hide": True},
     {"n": "identity_key", "t": STR, "hide": True},
     {"n": "model_key", "t": STR, "hide": True},
     {"n": "cost_center_key", "t": STR, "hide": True},
     {"n": "application_key", "t": STR, "hide": True},
     {"n": "environment_key", "t": STR, "hide": True},
     {"n": "business_unit_key", "t": STR, "hide": True},
     {"n": "unit_type", "t": STR},
     {"n": "quantity", "t": DEC, "fmt": "#,0", "sc": "sum"},
     {"n": "input_tokens", "t": INT, "fmt": "#,0", "sc": "sum", "hide": True},
     {"n": "output_tokens", "t": INT, "fmt": "#,0", "sc": "sum", "hide": True},
     {"n": "cached_tokens", "t": INT, "fmt": "#,0", "sc": "sum", "hide": True},
     {"n": "requests", "t": INT, "fmt": "#,0", "sc": "sum", "hide": True},
     {"n": "cost_usd", "t": DEC, "fmt": "\\$#,0.0000", "sc": "sum", "hide": True},
     {"n": "list_cost_usd", "t": DEC, "fmt": "\\$#,0.0000", "sc": "sum", "hide": True,
      "desc": "FOCUS ListCost / Rate-Card list price."},
     {"n": "cost_is_estimated", "t": BOOL},
     {"n": "has_rate_card", "t": BOOL, "desc": "TRUE when a genuine list/rate-card price was found."},
     {"n": "is_error", "t": STR, "hide": True},
     {"n": "latency_ms", "t": DEC, "hide": True}],
    {"usage_date": Mdate, "platform_key": Mstr, "identity_key": Mstr, "model_key": Mstr,
     "cost_center_key": Mstr, "unit_type": Mstr, "quantity": Mnum, "input_tokens": Mint,
     "output_tokens": Mint, "cached_tokens": Mint, "requests": Mint, "cost_usd": Mnum,
     "list_cost_usd": Mnum, "cost_is_estimated": Mlog, "has_rate_card": Mlog,
     "is_error": Mstr, "latency_ms": Mnum,
     "application_key": Mstr, "environment_key": Mstr, "business_unit_key": Mstr},
    extra=MEASURES))

# ------------------------------------------------------------------ dimensions
w(SM / "definition" / "tables" / "dim_date.tmdl", table(
    "dim_date",
    [{"n": "date_key", "t": DT, "fmt": "yyyy-mm-dd", "key": True},
     {"n": "year", "t": INT}, {"n": "quarter", "t": STR}, {"n": "month", "t": INT, "hide": True},
     {"n": "month_name", "t": STR, "sort": "month"}, {"n": "day", "t": INT},
     {"n": "day_name", "t": STR}, {"n": "is_weekday", "t": BOOL}, {"n": "year_month", "t": STR}],
    {"date_key": Mdate, "year": Mint, "quarter": Mstr, "month": Mint, "month_name": Mstr,
     "day": Mint, "day_name": Mstr, "is_weekday": Mlog, "year_month": Mstr},
    props=f"{T}dataCategory: Time\n"))

w(SM / "definition" / "tables" / "dim_platform.tmdl", table(
    "dim_platform",
    [{"n": "platform_key", "t": STR, "key": True, "hide": True},
     {"n": "platform_name", "t": STR}, {"n": "billing_model", "t": STR},
     {"n": "native_unit", "t": STR}, {"n": "has_token_telemetry", "t": BOOL},
     {"n": "has_native_cost", "t": BOOL}, {"n": "is_variable_cost", "t": BOOL},
     {"n": "data_source", "t": STR},
     {"n": "enterprise_discount_pct", "t": DEC, "fmt": "0.0%",
      "desc": "Negotiated discount off list (EA / MCA / volume tier / credit pack). Overwrite per contract."},
     {"n": "addon_unit", "t": STR},
     {"n": "addon_billing_model", "t": STR}],
    {"platform_key": Mstr, "platform_name": Mstr, "billing_model": Mstr, "native_unit": Mstr,
     "has_token_telemetry": Mlog, "has_native_cost": Mlog, "is_variable_cost": Mlog,
     "data_source": Mstr, "enterprise_discount_pct": Mnum, "addon_unit": Mstr, "addon_billing_model": Mstr}))

w(SM / "definition" / "tables" / "dim_identity.tmdl", table(
    "dim_identity",
    [{"n": "identity_key", "t": STR, "key": True, "hide": True},
     {"n": "display_name", "t": STR}, {"n": "principal_type", "t": STR},
     {"n": "upn", "t": STR}, {"n": "github_login", "t": STR},
     {"n": "team", "t": STR}, {"n": "business_unit", "t": STR},
     {"n": "cost_center_key", "t": STR, "hide": True},
     {"n": "identity_class", "t": STR,
      "desc": "Universal identity class: Human · ServicePrincipal · ManagedIdentity · Agent · Application."},
     {"n": "is_human", "t": BOOL},
     {"n": "home_business_unit_key", "t": STR, "hide": True},
     {"n": "persona_key", "t": STR, "hide": True,
      "desc": "ASSUMED persona (derived from team until an HR/Entra job-profile feed exists)."}],
    {"identity_key": Mstr, "display_name": Mstr, "principal_type": Mstr, "upn": Mstr,
     "github_login": Mstr, "team": Mstr, "business_unit": Mstr, "cost_center_key": Mstr,
     "identity_class": Mstr, "is_human": Mlog, "home_business_unit_key": Mstr, "persona_key": Mstr}))

w(SM / "definition" / "tables" / "dim_model.tmdl", table(
    "dim_model",
    [{"n": "model_key", "t": STR, "key": True, "hide": True},
     {"n": "model_name", "t": STR}, {"n": "model_version", "t": STR},
     {"n": "provider", "t": STR}, {"n": "modality", "t": STR}],
    {c: Mstr for c in ["model_key", "model_name", "model_version", "provider", "modality"]}))

w(SM / "definition" / "tables" / "dim_cost_center.tmdl", table(
    "dim_cost_center",
    [{"n": "cost_center_key", "t": STR, "key": True, "hide": True},
     {"n": "cost_center_name", "t": STR}, {"n": "business_unit", "t": STR},
     {"n": "owner_upn", "t": STR}],
    {c: Mstr for c in ["cost_center_key", "cost_center_name", "business_unit", "owner_upn"]}))

w(SM / "definition" / "tables" / "dim_business_unit.tmdl", table(
    "dim_business_unit",
    [{"n": "business_unit_key", "t": STR, "key": True, "hide": True},
     {"n": "business_unit_name", "t": STR},
     {"n": "division", "t": STR},
     {"n": "monthly_budget_usd", "t": INT, "fmt": "\\$#,0", "sc": "sum",
      "desc": "MOCK monthly budget for variance/forecast demo. Overwrite with the customer's real budget."},
     {"n": "executive_owner", "t": STR},
     {"n": "is_mock_budget", "t": BOOL}],
    {"business_unit_key": Mstr, "business_unit_name": Mstr, "division": Mstr,
     "monthly_budget_usd": Mint, "executive_owner": Mstr, "is_mock_budget": Mlog}))

w(SM / "definition" / "tables" / "dim_application.tmdl", table(
    "dim_application",
    [{"n": "application_key", "t": STR, "key": True, "hide": True},
     {"n": "application_name", "t": STR},
     {"n": "application_type", "t": STR, "desc": "api · agent · copilot · notebook · unknown"},
     {"n": "owner_business_unit_key", "t": STR, "hide": True},
     {"n": "owner_upn", "t": STR},
     {"n": "default_environment_key", "t": STR, "hide": True},
     {"n": "criticality", "t": STR},
     {"n": "is_mock", "t": BOOL, "desc": "TRUE where the application's telemetry is mocked."}],
    {"application_key": Mstr, "application_name": Mstr, "application_type": Mstr,
     "owner_business_unit_key": Mstr, "owner_upn": Mstr, "default_environment_key": Mstr,
     "criticality": Mstr, "is_mock": Mlog}))

w(SM / "definition" / "tables" / "dim_environment.tmdl", table(
    "dim_environment",
    [{"n": "environment_key", "t": STR, "key": True, "hide": True},
     {"n": "environment_name", "t": STR},
     {"n": "is_production", "t": BOOL},
     {"n": "sla_tier", "t": STR}],
    {"environment_key": Mstr, "environment_name": Mstr, "is_production": Mlog, "sla_tier": Mstr}))

w(SM / "definition" / "tables" / "dim_rate_card.tmdl", table(
    "dim_rate_card",
    [{"n": "rate_key", "t": STR, "key": True, "hide": True},
     {"n": "platform", "t": STR}, {"n": "unit_type", "t": STR}, {"n": "model", "t": STR},
     {"n": "unit_price_usd", "t": DEC, "fmt": "\\$#,0.000000"},
     {"n": "effective_from", "t": DT, "fmt": "yyyy-mm-dd"},
     {"n": "currency", "t": STR}, {"n": "source", "t": STR}, {"n": "note", "t": STR}],
    {"rate_key": Mstr, "platform": Mstr, "unit_type": Mstr, "model": Mstr,
     "unit_price_usd": Mnum, "effective_from": Mdate, "currency": Mstr,
     "source": Mstr, "note": Mstr}))

DS_MEASURES = f"""
{T}measure 'Extractable Signals' = COUNTROWS(dim_data_source)
{T}{T}formatString: #,0

{T}/// Signals live in the model today (REAL data source).
{T}measure 'Signals Live (REAL)' =
{T}{T}CALCULATE(COUNTROWS(dim_data_source), dim_data_source[availability] = "REAL")
{T}{T}formatString: #,0

{T}/// Signals whose API exists and could be wired without new licensing.
{T}measure 'Signals Available' =
{T}{T}CALCULATE(COUNTROWS(dim_data_source), dim_data_source[availability] = "AVAILABLE")
{T}{T}formatString: #,0

"""

w(SM / "definition" / "tables" / "dim_data_source.tmdl", table(
    "dim_data_source",
    [{"n": "platform", "t": STR}, {"n": "signal_category", "t": STR},
     {"n": "signal", "t": STR}, {"n": "source_api", "t": STR},
     {"n": "grain", "t": STR}, {"n": "identity_granularity", "t": STR},
     {"n": "cost_fidelity", "t": STR}, {"n": "retention", "t": STR},
     {"n": "availability", "t": STR, "desc": "REAL (live now) · AVAILABLE (API exists) · MOCK (needs SKU/policy) · ROADMAP"},
     {"n": "notes", "t": STR}],
    {c: Mstr for c in ["platform", "signal_category", "signal", "source_api", "grain",
                       "identity_granularity", "cost_fidelity", "retention", "availability", "notes"]},
    extra=DS_MEASURES,
    csv_filename="extractable_data_catalog.csv"))

w(SM / "definition" / "tables" / "dim_agent.tmdl", table(
    "dim_agent",
    [{"n": "agent_key", "t": STR, "hide": True, "key": True},
     {"n": "agent_name", "t": STR}, {"n": "status", "t": STR},
     {"n": "publisher", "t": STR}, {"n": "publisher_type", "t": STR},
     {"n": "platform", "t": STR}, {"n": "version", "t": STR},
     {"n": "owner_upn", "t": STR}, {"n": "owner_class", "t": STR},
     {"n": "channel", "t": STR}, {"n": "deployment", "t": STR},
     {"n": "entra_agent_id", "t": STR}, {"n": "environment_id", "t": STR},
     {"n": "created_date", "t": DT, "fmt": "yyyy-mm-dd"},
     {"n": "last_modified_date", "t": DT, "fmt": "yyyy-mm-dd"},
     {"n": "data_class", "t": STR}],
    {"agent_key": Mstr, "agent_name": Mstr, "status": Mstr, "publisher": Mstr,
     "publisher_type": Mstr, "platform": Mstr, "version": Mstr, "owner_upn": Mstr,
     "owner_class": Mstr, "channel": Mstr, "deployment": Mstr, "entra_agent_id": Mstr,
     "environment_id": Mstr, "created_date": Mdate, "last_modified_date": Mdate, "data_class": Mstr}))

w(SM / "definition" / "tables" / "dim_persona.tmdl", table(
    "dim_persona",
    [{"n": "persona_key", "t": STR, "hide": True, "key": True},
     {"n": "persona_name", "t": STR, "sort": "persona_order"},
     {"n": "persona_order", "t": INT},
     {"n": "description", "t": STR}],
    {"persona_key": Mstr, "persona_name": Mstr, "persona_order": Mint, "description": Mstr}))

BRIDGE_MEASURES = f"""
{T}/// Share of the selected persona AI spend that lands on the primary tool of that persona.
{T}measure 'Spend on Primary Tool %' = DIVIDE(CALCULATE([Total AI Cost], TREATAS(CALCULATETABLE(VALUES(bridge_persona_platform[platform_key]), bridge_persona_platform[is_primary] = 1), dim_platform[platform_key])), [Total AI Cost])
{T}{T}formatString: 0.0%

"""

w(SM / "definition" / "tables" / "bridge_persona_platform.tmdl", table(
    "bridge_persona_platform",
    [{"n": "persona_key", "t": STR, "hide": True},
     {"n": "platform_key", "t": STR, "hide": True},
     {"n": "affinity", "t": STR},
     {"n": "is_primary", "t": INT},
     {"n": "bridge_key", "t": STR, "hide": True}],
    {"persona_key": Mstr, "platform_key": Mstr, "affinity": Mstr, "is_primary": Mint, "bridge_key": Mstr},
    extra=BRIDGE_MEASURES))

AGENT_INV_MEASURES = f"""
{T}/// Point-in-time counts use the latest snapshot in the filter context so repeated exports never double count.
{T}measure 'Agents' = CALCULATE(SUM(fact_agent_inventory[agent_count]), fact_agent_inventory[snapshot_date] = MAX(fact_agent_inventory[snapshot_date]))
{T}{T}formatString: #,0

{T}measure 'Unowned Tenant Agents' = CALCULATE(SUM(fact_agent_inventory[is_unowned_tenant_agent]), fact_agent_inventory[snapshot_date] = MAX(fact_agent_inventory[snapshot_date]))
{T}{T}formatString: #,0

{T}measure 'Agents With Owner' = CALCULATE(SUM(fact_agent_inventory[has_owner]), fact_agent_inventory[snapshot_date] = MAX(fact_agent_inventory[snapshot_date]))
{T}{T}formatString: #,0

{T}measure 'Agents With Entra Identity' = CALCULATE(SUM(fact_agent_inventory[has_entra_identity]), fact_agent_inventory[snapshot_date] = MAX(fact_agent_inventory[snapshot_date]))
{T}{T}formatString: #,0

{T}measure 'Agents With Entra Identity %' = DIVIDE([Agents With Entra Identity], [Agents])
{T}{T}formatString: 0.0%

{T}measure 'Agents With File or Site Access' = CALCULATE(SUM(fact_agent_inventory[can_read_files_or_sites]), fact_agent_inventory[snapshot_date] = MAX(fact_agent_inventory[snapshot_date]))
{T}{T}formatString: #,0

{T}measure 'Agents With Custom Actions' = CALCULATE(SUM(fact_agent_inventory[has_custom_actions]), fact_agent_inventory[snapshot_date] = MAX(fact_agent_inventory[snapshot_date]))
{T}{T}formatString: #,0

{T}measure 'Agents With Code Interpreter' = CALCULATE(SUM(fact_agent_inventory[can_use_code_interpreter]), fact_agent_inventory[snapshot_date] = MAX(fact_agent_inventory[snapshot_date]))
{T}{T}formatString: #,0

{T}measure 'Available Agents' = CALCULATE(SUM(fact_agent_inventory[is_available]), fact_agent_inventory[snapshot_date] = MAX(fact_agent_inventory[snapshot_date]))
{T}{T}formatString: #,0

"""

w(SM / "definition" / "tables" / "fact_agent_inventory.tmdl", table(
    "fact_agent_inventory",
    [{"n": "snapshot_date", "t": DT, "fmt": "yyyy-mm-dd"},
     {"n": "agent_key", "t": STR, "hide": True},
     {"n": "owner_identity_key", "t": STR, "hide": True},
     {"n": "agent_count", "t": INT},
     {"n": "is_unowned_tenant_agent", "t": INT},
     {"n": "has_owner", "t": INT},
     {"n": "has_entra_identity", "t": INT},
     {"n": "can_read_files_or_sites", "t": INT},
     {"n": "has_custom_actions", "t": INT},
     {"n": "can_use_code_interpreter", "t": INT},
     {"n": "is_available", "t": INT},
     {"n": "_data_class", "t": STR, "hide": True}],
    {"snapshot_date": Mdate, "agent_key": Mstr, "owner_identity_key": Mstr,
     "agent_count": Mint, "is_unowned_tenant_agent": Mint, "has_owner": Mint,
     "has_entra_identity": Mint, "can_read_files_or_sites": Mint, "has_custom_actions": Mint,
     "can_use_code_interpreter": Mint, "is_available": Mint, "_data_class": Mstr},
    extra=AGENT_INV_MEASURES))

AGENT_ACT_MEASURES = f"""
{T}measure 'Agent Active Users' = DISTINCTCOUNT(fact_agent_activity[identity_key])
{T}{T}formatString: #,0

{T}measure 'Agent Sessions' = SUM(fact_agent_activity[sessions])
{T}{T}formatString: #,0

{T}measure 'Agent Users Resolved %' = DIVIDE(SUM(fact_agent_activity[is_identity_resolved]), COUNTROWS(fact_agent_activity))
{T}{T}formatString: 0.0%

"""

w(SM / "definition" / "tables" / "fact_agent_activity.tmdl", table(
    "fact_agent_activity",
    [{"n": "activity_date", "t": DT, "fmt": "yyyy-mm-dd"},
     {"n": "identity_key", "t": STR, "hide": True},
     {"n": "agents_used", "t": INT},
     {"n": "sessions", "t": INT},
     {"n": "is_identity_resolved", "t": INT},
     {"n": "_data_class", "t": STR, "hide": True}],
    {"activity_date": Mdate, "identity_key": Mstr, "agents_used": Mint,
     "sessions": Mint, "is_identity_resolved": Mint, "_data_class": Mstr},
    extra=AGENT_ACT_MEASURES))

ALLOC_MEASURES = f"""
{T}/// Same dollars as Total AI Cost re-keyed to a person. Never add it to Total AI Cost.
{T}measure 'User Allocated Cost' = SUM(fact_user_cost_allocation[cost_usd])
{T}{T}formatString: \\$#,0.00

{T}measure 'User Allocated Tokens' = SUM(fact_user_cost_allocation[total_tokens])
{T}{T}formatString: #,0

{T}measure 'User Allocated Requests' = SUM(fact_user_cost_allocation[requests])
{T}{T}formatString: #,0

{T}measure 'Directly Attributed Cost' = CALCULATE([User Allocated Cost], fact_user_cost_allocation[allocation_basis] = "DIRECT")
{T}{T}formatString: \\$#,0.00

{T}/// OWNER means a service principal or app cost allocated to the application owner. Accountability, not proof of who typed the prompts.
{T}measure 'Owner Allocated Cost' = CALCULATE([User Allocated Cost], fact_user_cost_allocation[allocation_basis] = "OWNER")
{T}{T}formatString: \\$#,0.00

{T}measure 'Unallocated User Cost' = CALCULATE([User Allocated Cost], fact_user_cost_allocation[allocation_basis] = "UNALLOCATED")
{T}{T}formatString: \\$#,0.00

{T}measure 'Directly Attributed Cost %' = DIVIDE([Directly Attributed Cost], [User Allocated Cost])
{T}{T}formatString: 0.0%

{T}measure 'Owner Allocated Tokens' = CALCULATE([User Allocated Tokens], fact_user_cost_allocation[allocation_basis] = "OWNER")
{T}{T}formatString: #,0

{T}measure 'Allocated Users' = CALCULATE(DISTINCTCOUNT(fact_user_cost_allocation[user_identity_key]), fact_user_cost_allocation[allocation_basis] <> "UNALLOCATED")
{T}{T}formatString: #,0

"""

w(SM / "definition" / "tables" / "fact_user_cost_allocation.tmdl", table(
    "fact_user_cost_allocation",
    [{"n": "usage_date", "t": DT, "fmt": "yyyy-mm-dd"},
     {"n": "platform_key", "t": STR, "hide": True},
     {"n": "application_key", "t": STR, "hide": True},
     {"n": "environment_key", "t": STR, "hide": True},
     {"n": "business_unit_key", "t": STR, "hide": True},
     {"n": "cost_center_key", "t": STR, "hide": True},
     {"n": "user_identity_key", "t": STR, "hide": True},
     {"n": "source_identity_key", "t": STR, "hide": True},
     {"n": "allocation_basis", "t": STR},
     {"n": "cost_is_estimated", "t": STR},
     {"n": "input_tokens", "t": DEC, "hide": True},
     {"n": "output_tokens", "t": DEC, "hide": True},
     {"n": "total_tokens", "t": DEC, "hide": True},
     {"n": "requests", "t": DEC, "hide": True},
     {"n": "cost_usd", "t": DEC, "hide": True}],
    {"usage_date": Mdate, "platform_key": Mstr, "application_key": Mstr, "environment_key": Mstr,
     "business_unit_key": Mstr, "cost_center_key": Mstr, "user_identity_key": Mstr,
     "source_identity_key": Mstr, "allocation_basis": Mstr, "cost_is_estimated": Mstr,
     "input_tokens": Mnum, "output_tokens": Mnum, "total_tokens": Mnum, "requests": Mnum, "cost_usd": Mnum},
    extra=ALLOC_MEASURES))

# ---------------------------------------------------------------- model.tmdl
w(SM / "definition" / "model.tmdl", f"""model Model
{T}culture: en-US
{T}defaultPowerBIDataSourceVersion: powerBI_V3
{T}discourageImplicitMeasures
{T}sourceQueryCulture: en-US
{T}dataAccessOptions
{T}{T}legacyRedirects
{T}{T}returnErrorValuesAsNull

/// Absolute path to AIFinOps.SemanticModel/data/ (include trailing separator).
/// Every table partition reads its CSV from this single parameter so the model is
/// portable: set it once via Transform data -> Manage Parameters -> DataFolder.
expression DataFolder = "{DEFAULT_DATA_DIR}" meta [IsParameterQuery=true, Type="Text", IsParameterQueryRequired=true]
{T}lineageTag: 9c1b7a10-1111-4111-8111-000000000001

{T}annotation PBI_ResultType = Text

ref table fact_ai_usage
ref table dim_date
ref table dim_platform
ref table dim_identity
ref table dim_model
ref table dim_cost_center
ref table dim_rate_card
ref table dim_business_unit
ref table dim_application
ref table dim_environment
ref table dim_data_source
ref table fact_user_cost_allocation
ref table dim_agent
ref table fact_agent_inventory
ref table fact_agent_activity
ref table dim_persona
ref table bridge_persona_platform

relationship rel_alloc_date
{T}fromColumn: fact_user_cost_allocation.usage_date
{T}toColumn: dim_date.date_key

relationship rel_alloc_platform
{T}fromColumn: fact_user_cost_allocation.platform_key
{T}toColumn: dim_platform.platform_key

relationship rel_alloc_application
{T}fromColumn: fact_user_cost_allocation.application_key
{T}toColumn: dim_application.application_key

relationship rel_alloc_businessunit
{T}fromColumn: fact_user_cost_allocation.business_unit_key
{T}toColumn: dim_business_unit.business_unit_key

relationship rel_alloc_costcenter
{T}fromColumn: fact_user_cost_allocation.cost_center_key
{T}toColumn: dim_cost_center.cost_center_key

relationship rel_alloc_environment
{T}fromColumn: fact_user_cost_allocation.environment_key
{T}toColumn: dim_environment.environment_key

relationship rel_alloc_user
{T}fromColumn: fact_user_cost_allocation.user_identity_key
{T}toColumn: dim_identity.identity_key

relationship rel_alloc_source
{T}isActive: false
{T}fromColumn: fact_user_cost_allocation.source_identity_key
{T}toColumn: dim_identity.identity_key

relationship rel_agentinv_date
{T}fromColumn: fact_agent_inventory.snapshot_date
{T}toColumn: dim_date.date_key

relationship rel_agentinv_agent
{T}fromColumn: fact_agent_inventory.agent_key
{T}toColumn: dim_agent.agent_key

relationship rel_agentinv_owner
{T}fromColumn: fact_agent_inventory.owner_identity_key
{T}toColumn: dim_identity.identity_key

relationship rel_agentact_date
{T}fromColumn: fact_agent_activity.activity_date
{T}toColumn: dim_date.date_key

relationship rel_agentact_identity
{T}fromColumn: fact_agent_activity.identity_key
{T}toColumn: dim_identity.identity_key

relationship rel_identity_persona
{T}fromColumn: dim_identity.persona_key
{T}toColumn: dim_persona.persona_key

relationship rel_bridge_persona
{T}fromColumn: bridge_persona_platform.persona_key
{T}toColumn: dim_persona.persona_key

relationship rel_bridge_platform
{T}fromColumn: bridge_persona_platform.platform_key
{T}toColumn: dim_platform.platform_key

relationship rel_usage_date
{T}fromColumn: fact_ai_usage.usage_date
{T}toColumn: dim_date.date_key

relationship rel_usage_platform
{T}fromColumn: fact_ai_usage.platform_key
{T}toColumn: dim_platform.platform_key

relationship rel_usage_identity
{T}fromColumn: fact_ai_usage.identity_key
{T}toColumn: dim_identity.identity_key

relationship rel_usage_model
{T}fromColumn: fact_ai_usage.model_key
{T}toColumn: dim_model.model_key

relationship rel_usage_costcenter
{T}fromColumn: fact_ai_usage.cost_center_key
{T}toColumn: dim_cost_center.cost_center_key

relationship rel_usage_businessunit
{T}fromColumn: fact_ai_usage.business_unit_key
{T}toColumn: dim_business_unit.business_unit_key

relationship rel_usage_application
{T}fromColumn: fact_ai_usage.application_key
{T}toColumn: dim_application.application_key

relationship rel_usage_environment
{T}fromColumn: fact_ai_usage.environment_key
{T}toColumn: dim_environment.environment_key
""")


# --- guard: Power BI names are case-insensitive; a measure may not share a name
# with a column in the same table. TMDL deserializers do not catch this.
def _check_collisions():
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

