#!/usr/bin/env python3
"""
Emit a Power BI Project (PBIP) — text-based, opens directly in Power BI Desktop.

  AIFinOps.pbip
  AIFinOps.SemanticModel/   definition.pbism + TMDL (model, tables, relationships)
  AIFinOps.Report/          definition.pbir + report.json

TMDL is tab-indented. Generated rather than hand-written so indentation can't drift.
"""
import json
from pathlib import Path

ROOT = Path(__file__).parent
SM = ROOT / "AIFinOps.SemanticModel"
RP = ROOT / "AIFinOps.Report"
(SM / "definition" / "tables").mkdir(parents=True, exist_ok=True)
RP.mkdir(parents=True, exist_ok=True)

T = "\t"

# Absolute path baked into every partition - no parameter to configure.
# Derived from this clone so the generator never bakes in another user's home
# directory. Run platform/validate/validate_pbip.py --fix-data-folder to
# re-point an already-generated model at a different clone.
DATA_DIR = str(SM / "data") + "\\"


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
def m_partition(table: str, casts: dict) -> str:
    """M expression reading a CSV from the model-relative data folder."""
    types = ", ".join(f'{{"{c}", {t}}}' for c, t in casts.items())
    return (
        f"{T}partition {table} = m\n"
        f"{T}{T}mode: import\n"
        f"{T}{T}source =\n"
        f"{T}{T}{T}let\n"
        f'{T}{T}{T}{T}Src = Csv.Document(File.Contents("{DATA_DIR}{table}.csv"),'
        f"[Delimiter=\",\", Encoding=65001, QuoteStyle=QuoteStyle.Csv]),\n"
        f"{T}{T}{T}{T}Hdr = Table.PromoteHeaders(Src, [PromoteAllScalars=true]),\n"
        f"{T}{T}{T}{T}Typed = Table.TransformColumnTypes(Hdr, {{{types}}})\n"
        f"{T}{T}{T}in\n"
        f"{T}{T}{T}{T}Typed\n"
    )


def table(name: str, cols: list, casts: dict, extra: str = "",
          hidden=False, props: str = "") -> str:
    """props = table-level properties (must precede children).
       extra = child objects such as measures (must follow columns)."""
    s = f"table {name}\n"
    if hidden:
        s += f"{T}isHidden\n"
    s += props
    s += "\n"
    for c in cols:
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
    s += m_partition(name, casts)
    return s


STR, INT, DEC, DT, BOOL = "string", "int64", "double", "dateTime", "boolean"
Mstr, Mint, Mnum, Mdate, Mlog = "type text", "Int64.Type", "type number", "type date", "type logical"

# ------------------------------------------------------------- fact_ai_usage
MEASURES = f"""
{T}measure 'Total AI Cost' = SUM(fact_ai_usage[cost_usd])
{T}{T}formatString: \\$#,0.00

{T}/// Cost sourced from a real billing surface (Foundry gateway rates, GitHub netAmount).
{T}measure 'Billed Cost' = CALCULATE([Total AI Cost], fact_ai_usage[cost_is_estimated] = FALSE)
{T}{T}formatString: \\$#,0.00

{T}/// Cost derived from dim_rate_card. Accuracy depends entirely on the customer's rate card.
{T}measure 'Modelled Cost' = CALCULATE([Total AI Cost], fact_ai_usage[cost_is_estimated] = TRUE)
{T}{T}formatString: \\$#,0.00

{T}/// Share of total spend that is billed rather than modelled. Put this on page 1.
{T}measure 'Cost Confidence %' = DIVIDE([Billed Cost], [Total AI Cost])
{T}{T}formatString: 0.0%

{T}/// Licence cost. Does not vary with usage.
{T}measure 'Fixed Cost' =
{T}{T}CALCULATE([Total AI Cost], fact_ai_usage[unit_type] IN {{ "seat_day" }})
{T}{T}formatString: \\$#,0.00

{T}/// Consumption cost. The only half a FinOps programme can actually influence.
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

{T}/// Cached input bills at a lower rate. Direct, actionable saving.
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

{T}/// Usage signal only. M365 Copilot prompts are never billable.
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

{T}/// Users holding a paid seat with zero activity in 28 days. The recoverable number.
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

{T}measure 'Idle Seat Waste (monthly)' =
{T}{T}[Idle Licensed Users] * 30 * AVERAGE(dim_rate_card[unit_price_usd])
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

"""

w(SM / "definition" / "tables" / "fact_ai_usage.tmdl", table(
    "fact_ai_usage",
    [{"n": "usage_date", "t": DT, "fmt": "yyyy-mm-dd", "hide": True},
     {"n": "platform_key", "t": STR, "hide": True},
     {"n": "identity_key", "t": STR, "hide": True},
     {"n": "model_key", "t": STR, "hide": True},
     {"n": "cost_center_key", "t": STR, "hide": True},
     {"n": "unit_type", "t": STR},
     {"n": "quantity", "t": DEC, "fmt": "#,0", "sc": "sum"},
     {"n": "input_tokens", "t": INT, "fmt": "#,0", "sc": "sum", "hide": True},
     {"n": "output_tokens", "t": INT, "fmt": "#,0", "sc": "sum", "hide": True},
     {"n": "cached_tokens", "t": INT, "fmt": "#,0", "sc": "sum", "hide": True},
     {"n": "requests", "t": INT, "fmt": "#,0", "sc": "sum", "hide": True},
     {"n": "cost_usd", "t": DEC, "fmt": "\\$#,0.0000", "sc": "sum", "hide": True},
     {"n": "cost_is_estimated", "t": BOOL},
     {"n": "is_error", "t": STR, "hide": True},
     {"n": "latency_ms", "t": DEC, "hide": True}],
    {"usage_date": Mdate, "platform_key": Mstr, "identity_key": Mstr, "model_key": Mstr,
     "cost_center_key": Mstr, "unit_type": Mstr, "quantity": Mnum, "input_tokens": Mint,
     "output_tokens": Mint, "cached_tokens": Mint, "requests": Mint, "cost_usd": Mnum,
     "cost_is_estimated": Mlog, "is_error": Mstr, "latency_ms": Mnum},
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
     {"n": "data_source", "t": STR}],
    {"platform_key": Mstr, "platform_name": Mstr, "billing_model": Mstr, "native_unit": Mstr,
     "has_token_telemetry": Mlog, "has_native_cost": Mlog, "is_variable_cost": Mlog,
     "data_source": Mstr}))

w(SM / "definition" / "tables" / "dim_identity.tmdl", table(
    "dim_identity",
    [{"n": "identity_key", "t": STR, "key": True, "hide": True},
     {"n": "display_name", "t": STR}, {"n": "principal_type", "t": STR},
     {"n": "upn", "t": STR}, {"n": "github_login", "t": STR},
     {"n": "team", "t": STR}, {"n": "business_unit", "t": STR},
     {"n": "cost_center_key", "t": STR, "hide": True}],
    {c: Mstr for c in ["identity_key", "display_name", "principal_type", "upn",
                       "github_login", "team", "business_unit", "cost_center_key"]}))

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

# ---------------------------------------------------------------- model.tmdl
w(SM / "definition" / "model.tmdl", f"""model Model
{T}culture: en-US
{T}defaultPowerBIDataSourceVersion: powerBI_V3
{T}discourageImplicitMeasures
{T}sourceQueryCulture: en-US
{T}dataAccessOptions
{T}{T}legacyRedirects
{T}{T}returnErrorValuesAsNull

ref table fact_ai_usage
ref table dim_date
ref table dim_platform
ref table dim_identity
ref table dim_model
ref table dim_cost_center
ref table dim_rate_card

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
""")


# --- guard: Power BI names are case-insensitive; a measure may not share a name
# with a column in the same table. TMDL deserializers do not catch this.
def _check_collisions():
    import glob, re as _re
    bad = []
    for f in glob.glob(str(SM / "definition" / "tables" / "*.tmdl")):
        txt = Path(f).read_text()
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
