#!/usr/bin/env python3
"""Generate the 10-page themed report over the Direct Lake semantic model.

Page set matches the repo specification in README.md:

    1  Spend Overview            total, fixed vs variable, confidence, capability matrix
    2  Foundry Tokenomics        in/out/cached tokens, cache hit rate, $/1K
    3  Waste & Utilisation       idle seats and recoverable spend
    4  Rate Card                 the editable input + billed-vs-modelled by platform
    5  CFO - Finance             spend, discounts, forecast, budget variance, chargeback
    6  Governance                adoption by principal type, REAL-vs-MOCK register
    7  Engineering               token consumption, unit economics, latency
    8  Application Owner         spend by application, trend, MoM delta, criticality
    9  License Optimization      idle users, reclaimable spend, seat utilisation
    10 Extractable Data Spectrum catalogue of every AI cost signal per platform

Visual theme follows the supplied dashboard screenshot: deep-indigo canvas, a
gradient KPI strip (blue -> teal -> green), white rounded content cards, and a
right-hand filter rail.

Two hard rules, both enforced by platform/validate/check_report.py:
  1. No two visuals on a page may overlap, and nothing leaves the canvas.
  2. Every visual binds only to measures verified to return data. Measures that
     are legitimately blank on this dataset are excluded rather than shipped as
     empty cards -- pass them via --blank to the checker to keep it that way.

Usage:
    python platform/validate/build_report_directlake.py --dataset <dataset-guid>
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[2]
W, H = 1280, 720

# ------------------------------------------------------------------ palette
INK = "#FFFFFF"
CANVAS = "#221A5E"        # deep indigo page background
BAND = "#191147"          # header band
CARD = "#FFFFFF"          # white content card
CARD_INK = "#1B1464"      # text on white cards
RAIL = "#2A2072"          # right filter rail
MUTED = "#B9B2E8"

# KPI tiles, left-to-right: blue -> teal -> green (the theme's gradient).
# Darkened from the source image so white text clears WCAG AA (>=4.5:1); the
# original bright ramp measures 2.0-2.7:1, unreadable at KPI sizes.
KPI_COLORS = ["#1D4ED8", "#0F6BBF", "#0E7490", "#0F766E", "#15803D"]
SERIES = ["#4C8DFF", "#12C2A8", "#F5B93B", "#FF6E8A", "#9B7CFF", "#2ACE86"]

_uid = [0]


def uid(prefix="v"):
    _uid[0] += 1
    return f"{prefix}{_uid[0]:05d}"


def lit(value):
    return {"expr": {"Literal": {"Value": value}}}


def s_lit(text):
    return lit(f"'{text}'")


def num(n):
    return lit(f"{n}D")


def solid(color):
    return {"solid": {"color": s_lit(color)}}


# --------------------------------------------------------------- query build
def build_query(fields, order_by=None, top=None):
    """fields: [(entity, field, 'm'|'c')]. Returns (prototypeQuery, queryRefs)."""
    aliases, from_clause = {}, []
    for entity, _f, _k in fields:
        if entity not in aliases:
            aliases[entity] = "e%d" % len(aliases)
            from_clause.append({"Name": aliases[entity], "Entity": entity, "Type": 0})
    select, refs = [], []
    for entity, field, kind in fields:
        a = aliases[entity]
        key = "Measure" if kind == "m" else "Column"
        select.append({key: {"Expression": {"SourceRef": {"Source": a}},
                             "Property": field},
                       "Name": f"{entity}.{field}"})
        refs.append(f"{entity}.{field}")
    pq = {"Version": 2, "From": from_clause, "Select": select}
    if order_by:
        oe, of, ok, direction = order_by
        key = "Measure" if ok == "m" else "Column"
        pq["OrderBy"] = [{"Direction": direction, "Expression": {
            key: {"Expression": {"SourceRef": {"Source": aliases[oe]}},
                  "Property": of}}}]
    if top:
        pq["Top"] = top
    return pq, refs


# ------------------------------------------------------------------- chrome
def vc_common(title=None, bg=CARD, title_color=CARD_INK, radius=10, title_size=11):
    out = {
        "background": [{"properties": {"show": lit("true"), "color": solid(bg),
                                       "transparency": num(0)}}],
        "border": [{"properties": {"show": lit("true"), "color": solid(bg),
                                   "radius": num(radius)}}],
        "dropShadow": [{"properties": {"show": lit("false")}}],
    }
    if title:
        out["title"] = [{"properties": {
            "show": lit("true"), "text": s_lit(title),
            "fontColor": solid(title_color), "fontSize": num(title_size),
            "background": solid(bg), "alignment": s_lit("left"),
            "titleWrap": lit("true")}}]
    else:
        out["title"] = [{"properties": {"show": lit("false")}}]
    return out


def visual(kind, x, y, w, h, projections, pq, objects=None, vcobjects=None, z=0):
    cfg = {
        "name": uid(),
        "layouts": [{"id": 0, "position": {"x": x, "y": y, "z": z,
                                           "width": w, "height": h}}],
        "singleVisual": {
            "visualType": kind, "projections": projections,
            "prototypeQuery": pq, "drillFilterOtherVisuals": True,
            "objects": objects or {}, "vcObjects": vcobjects or {},
        },
    }
    return {"x": x, "y": y, "z": z, "width": w, "height": h,
            "config": json.dumps(cfg), "filters": "[]"}


def kpi(x, y, w, h, entity, measure, label, color):
    pq, refs = build_query([(entity, measure, "m")])
    objects = {
        "labels": [{"properties": {"color": solid(INK), "fontSize": num(21),
                                   "labelDisplayUnits": num(0),
                                   "fontFamily": s_lit("Segoe UI Semibold")}}],
        "categoryLabels": [{"properties": {"show": lit("false")}}],
        "wordWrap": [{"properties": {"show": lit("true")}}],
    }
    return visual("card", x, y, w, h, {"Values": [{"queryRef": refs[0]}]}, pq,
                  objects, vc_common(label, bg=color, title_color=INK, title_size=10))


def chart(kind, x, y, w, h, title, cat, values, *, legend=False,
          order_desc=True, top=None, colors=None):
    fields = [(cat[0], cat[1], "c")] + [(e, m, "m") for e, m in values]
    # A time axis must be ordered by time. Sorting a trend by its measure
    # produced "2026-06, 2026-09, 2026-07, 2026-08" — the series still rendered,
    # so nothing failed, it was just silently meaningless. Only visible once the
    # real data widened the range beyond two months.
    if cat[0] == "dim_date":
        order = (cat[0], cat[1], "c", 1)
    else:
        order = (values[0][0], values[0][1], "m", 2 if order_desc else 1)
    pq, refs = build_query(fields, order_by=order, top=top)
    if kind == "donutChart":
        projections = {"Category": [{"queryRef": refs[0]}],
                       "Y": [{"queryRef": refs[1]}]}
        objects = {
            "legend": [{"properties": {"show": lit("true"), "position": s_lit("Right"),
                                       "labelColor": solid(CARD_INK), "fontSize": num(9)}}],
            "labels": [{"properties": {"show": lit("true"), "color": solid(CARD_INK),
                                       "fontSize": num(9),
                                       "labelStyle": s_lit("Percent")}}],
            "slices": [{"properties": {"innerRadiusRatio": num(62)}}],
            "dataPoint": [{"properties": {"fill": solid((colors or SERIES)[0])}}],
        }
    else:
        projections = {"Category": [{"queryRef": refs[0]}],
                       "Y": [{"queryRef": r} for r in refs[1:]]}
        palette = colors or SERIES
        objects = {
            "legend": [{"properties": {
                "show": lit("true" if legend else "false"), "position": s_lit("Top"),
                "labelColor": solid(CARD_INK), "fontSize": num(9)}}],
            "categoryAxis": [{"properties": {
                "show": lit("true"), "labelColor": solid(CARD_INK),
                "fontSize": num(9), "gridlineShow": lit("false")}}],
            "valueAxis": [{"properties": {
                "show": lit("true"), "labelColor": solid(CARD_INK),
                "fontSize": num(9), "gridlineColor": solid("#E8E6F5")}}],
            "labels": [{"properties": {"show": lit("false")}}],
            "dataPoint": [
                {"properties": {"fill": solid(palette[i % len(palette)])},
                 "selector": {"metadata": refs[i + 1]}}
                for i in range(len(refs) - 1)],
        }
    return visual(kind, x, y, w, h, projections, pq, objects, vc_common(title))


def table(x, y, w, h, title, fields):
    pq, refs = build_query(fields)
    objects = {
        "grid": [{"properties": {"gridVertical": lit("false"),
                                 "gridHorizontal": lit("true"),
                                 "gridHorizontalColor": solid("#E8E6F5"),
                                 "outlineColor": solid("#E8E6F5")}}],
        "columnHeaders": [{"properties": {"fontColor": solid(CARD_INK),
                                          "backColor": solid("#F2F0FC"),
                                          "fontSize": num(9), "bold": lit("true")}}],
        "values": [{"properties": {"fontColor": solid(CARD_INK), "fontSize": num(9),
                                   "backColor": solid(CARD)}}],
    }
    return visual("tableEx", x, y, w, h,
                  {"Values": [{"queryRef": r} for r in refs]}, pq, objects,
                  vc_common(title))


def slicer(x, y, w, h, entity, column, title):
    pq, refs = build_query([(entity, column, "c")])
    objects = {
        "general": [{"properties": {"outlineWeight": num(0)}}],
        # Dropdown rather than the default vertical checkbox list. The list form
        # only ever showed the first two or three members before scrolling, which
        # hides most of the options — the Platform rail has six members and
        # Application has 135. Note this is `data.mode`, NOT `general.orientation`:
        # orientation only enumerates VerticalList/HorizontalList, so setting it
        # to a dropdown value silently does nothing.
        "data": [{"properties": {"mode": s_lit("Dropdown")}}],
        "selection": [{"properties": {"singleSelect": lit("false"),
                                      "selectAllCheckboxEnabled": lit("true")}}],
        "items": [{"properties": {"fontColor": solid(INK), "background": solid(RAIL),
                                  "fontSize": num(9)}}],
        "header": [{"properties": {"show": lit("false")}}],
    }
    v = visual("slicer", x, y, w, h, {"Values": [{"queryRef": refs[0]}]}, pq,
               objects, vc_common(title, bg=RAIL, title_color=INK, radius=10,
                                  title_size=10))
    # Direct Lake materialises a blank row on the one side of every
    # relationship, regardless of referential integrity. It holds no rows and no
    # spend, but slicers still list it as "(Blank)". Filter it out so the rail
    # only offers real, selectable values.
    v["filters"] = json.dumps([{
        "name": uid("f"),
        "expression": {"Column": {
            "Expression": {"SourceRef": {"Entity": entity}}, "Property": column}},
        "type": "Advanced",
        "filter": {
            "Version": 2,
            "From": [{"Name": "s", "Entity": entity, "Type": 0}],
            "Where": [{"Condition": {"Not": {"Expression": {"Comparison": {
                "ComparisonKind": 0,
                "Left": {"Column": {"Expression": {"SourceRef": {"Source": "s"}},
                                    "Property": column}},
                "Right": {"Literal": {"Value": "null"}}}}}}}],
        },
    }])
    return v


def banner(x, y, w, h, text, sub=None):
    runs = [{"textRuns": [{"value": text,
                           "textStyle": {"fontSize": "19pt", "fontWeight": "bold",
                                         "color": INK, "fontFamily": "Segoe UI"}}]}]
    if sub:
        runs.append({"textRuns": [{"value": sub,
                                   "textStyle": {"fontSize": "9pt", "color": MUTED,
                                                 "fontFamily": "Segoe UI"}}]})
    cfg = {
        "name": uid("t"),
        "layouts": [{"id": 0, "position": {"x": x, "y": y, "z": 0,
                                           "width": w, "height": h}}],
        "singleVisual": {
            "visualType": "textbox", "drillFilterOtherVisuals": True,
            "objects": {"general": [{"properties": {"paragraphs": runs}}]},
            "vcObjects": {
                "background": [{"properties": {"show": lit("true"),
                                               "color": solid(BAND),
                                               "transparency": num(0)}}],
                "border": [{"properties": {"show": lit("true"),
                                           "color": solid(BAND), "radius": num(10)}}],
                "title": [{"properties": {"show": lit("false")}}],
            },
        },
    }
    return {"x": x, "y": y, "z": 0, "width": w, "height": h,
            "config": json.dumps(cfg), "filters": "[]"}


# --------------------------------------------------------------------- grid
FACT = "fact_ai_usage"
M, G = 16, 12
HEAD_H = 52
KPI_Y = M + HEAD_H + G          # 80
KPI_H = 84
BODY_Y = KPI_Y + KPI_H + G      # 176
RAIL_W = 232
RAIL_X = W - M - RAIL_W         # 1032
BODY_W = RAIL_X - M - G         # 1004
ROW1_H = 246
ROW2_Y = BODY_Y + ROW1_H + G    # 434
ROW2_H = H - M - ROW2_Y         # 270
HALF = (BODY_W - G) // 2        # 496


def page(name, display, visuals):
    cfg = {"objects": {
        "background": [{"properties": {"color": solid(CANVAS), "transparency": num(0)}}],
        "outspace": [{"properties": {"color": solid(BAND), "transparency": num(0)}}],
        "displayArea": [{"properties": {"verticalAlignment": s_lit("Top")}}],
    }}
    return {"id": 0, "name": name, "displayName": display, "ordinal": 0,
            "width": W, "height": H, "displayOption": 1,
            "config": json.dumps(cfg), "filters": "[]",
            "visualContainers": visuals}


def kpi_row(specs):
    """Lay KPI tiles across the content width with equal gutters.

    Each spec is (measure, label) on fact_ai_usage, or (measure, label, entity)
    when the measure lives on another table -- the catalogue measures sit on
    dim_data_source, and binding them to the fact silently yields a blank card.

    The integer remainder is spread over the leading tiles so the last one ends
    exactly on the content edge; otherwise the gap to the filter rail is a pixel
    wider than every other gutter.
    """
    n = len(specs)
    base, rem = divmod(BODY_W - G * (n - 1), n)
    out, x = [], M
    for i, spec in enumerate(specs):
        measure, label = spec[0], spec[1]
        entity = spec[2] if len(spec) > 2 else FACT
        w = base + (1 if i < rem else 0)
        out.append(kpi(x, KPI_Y, w, KPI_H, entity, measure, label,
                       KPI_COLORS[i % 5]))
        x += w + G
    return out


def rail(slicers):
    # 76px: tall enough that the dropdown control is never clipped (it needs
    # >= 64) without leaving the dead space a list-sized panel now has.
    out, y = [], KPI_Y
    for entity, col, label in slicers:
        out.append(slicer(RAIL_X, y, RAIL_W, 76, entity, col, label))
        y += 76 + G
    return out


DATE_RAIL = ("dim_date", "year_month", "Month")
PLAT_RAIL = ("dim_platform", "platform_name", "Platform")
BU_RAIL = ("dim_business_unit", "business_unit_name", "Business unit")


def build_pages():
    p = []

    # ------------------------------------------------------ 1. Spend Overview
    v = [banner(M, M, W - 2 * M, HEAD_H, "Spend Overview",
                "Total AI spend across Foundry, M365 Copilot, Copilot Studio and GitHub Copilot")]
    v += kpi_row([("Total AI Cost", "Total AI Spend"),
                  ("Fixed Cost", "Fixed (licence)"),
                  ("Variable Cost", "Variable (usage)"),
                  ("Cost Confidence %", "Cost Confidence"),
                  ("Cost per Active User", "Cost / Active User")])
    v.append(chart("lineChart", M, BODY_Y, BODY_W, ROW1_H, "Daily AI spend",
                   ("dim_date", "date_key"), [(FACT, "Total AI Cost")],
                   order_desc=False, colors=["#4C8DFF"]))
    v.append(chart("donutChart", M, ROW2_Y, HALF, ROW2_H, "Spend by platform",
                   ("dim_platform", "platform_name"), [(FACT, "Total AI Cost")]))
    v.append(table(M + HALF + G, ROW2_Y, HALF, ROW2_H, "Platform capability matrix",
                   [("dim_platform", "platform_name", "c"),
                    ("dim_platform", "native_unit", "c"),
                    ("dim_platform", "has_token_telemetry", "c"),
                    ("dim_platform", "has_native_cost", "c"),
                    (FACT, "Total AI Cost", "m")]))
    v += rail([PLAT_RAIL, BU_RAIL, DATE_RAIL])
    p.append(page("PageSpend", "1 - Spend Overview", v))

    # ------------------------------------------------- 2. Foundry Tokenomics
    v = [banner(M, M, W - 2 * M, HEAD_H, "Foundry Tokenomics",
                "The only platform with true token telemetry - input, output and cached")]
    v += kpi_row([("Total Tokens", "Total Tokens"),
                  ("Input Tokens", "Input Tokens"),
                  ("Output Tokens", "Output Tokens"),
                  ("Cached Tokens", "Cached Tokens"),
                  ("Cost per 1K Tokens", "Cost / 1K Tokens")])
    v.append(chart("clusteredColumnChart", M, BODY_Y, BODY_W, ROW1_H,
                   "Input vs output tokens by model",
                   ("dim_model", "model_name"),
                   [(FACT, "Input Tokens"), (FACT, "Output Tokens")],
                   legend=True, colors=["#4C8DFF", "#12C2A8"]))
    v.append(chart("lineChart", M, ROW2_Y, HALF, ROW2_H, "Token volume over time",
                   ("dim_date", "date_key"), [(FACT, "Total Tokens")],
                   order_desc=False, colors=["#9B7CFF"]))
    v.append(table(M + HALF + G, ROW2_Y, HALF, ROW2_H, "Token economics by model",
                   [("dim_model", "model_name", "c"),
                    (FACT, "Total Tokens", "m"),
                    (FACT, "Total AI Cost", "m"),
                    (FACT, "Cost per 1K Tokens", "m")]))
    v += rail([("dim_model", "model_name", "Model"), PLAT_RAIL, DATE_RAIL])
    p.append(page("PageTokens", "2 - Foundry Tokenomics", v))

    # --------------------------------------------------- 3. Waste & Utilisation
    v = [banner(M, M, W - 2 * M, HEAD_H, "Waste & Utilisation",
                "Licensed seats with no activity in 28 days, and the spend they represent")]
    v += kpi_row([("Idle Licensed Users", "Idle Licensed Seats"),
                  ("Idle Seat Waste (monthly)", "Recoverable / month"),
                  ("Licensed Seats", "Licensed Seats"),
                  ("Active Users", "Active Users"),
                  ("Fixed Cost", "Fixed (licence) Cost")])
    v.append(chart("barChart", M, BODY_Y, BODY_W, ROW1_H,
                   "Requests by user - the short bars are shelfware",
                   ("dim_identity", "display_name"), [(FACT, "Total Requests")],
                   colors=["#F5B93B"]))
    v.append(chart("donutChart", M, ROW2_Y, HALF, ROW2_H,
                   "Fixed licence cost by platform",
                   ("dim_platform", "platform_name"), [(FACT, "Fixed Cost")]))
    v.append(table(M + HALF + G, ROW2_Y, HALF, ROW2_H, "Seat utilisation by user",
                   [("dim_identity", "display_name", "c"),
                    (FACT, "Licensed Seats", "m"),
                    (FACT, "Total Requests", "m"),
                    (FACT, "Total AI Cost", "m")]))
    v += rail([("dim_identity", "identity_class", "Identity class"),
               PLAT_RAIL, DATE_RAIL])
    p.append(page("PageWaste", "3 - Waste & Utilisation", v))

    # ------------------------------------------------------------ 4. Rate Card
    v = [banner(M, M, W - 2 * M, HEAD_H, "Rate Card",
                "The single customer-supplied input - swap for your EA/MCA price sheet")]
    v += kpi_row([("Billed Cost", "Billed (real)"),
                  ("Modelled Cost", "Modelled (rate card)"),
                  ("Cost Confidence %", "Cost Confidence"),
                  ("Discounted Cost", "After Discounts"),
                  ("Discount Savings", "Negotiated Savings")])
    v.append(chart("clusteredBarChart", M, BODY_Y, BODY_W, ROW1_H,
                   "Billed vs modelled by platform",
                   ("dim_platform", "platform_name"),
                   [(FACT, "Billed Cost"), (FACT, "Modelled Cost")],
                   legend=True, colors=["#2ACE86", "#F5B93B"]))
    v.append(table(M, ROW2_Y, BODY_W, ROW2_H, "Rate card - edit these values",
                   [("dim_rate_card", "platform", "c"),
                    ("dim_rate_card", "unit_type", "c"),
                    ("dim_rate_card", "model", "c"),
                    ("dim_rate_card", "unit_price_usd", "c"),
                    ("dim_rate_card", "source", "c")]))
    v += rail([("dim_rate_card", "platform", "Platform"),
               ("dim_rate_card", "unit_type", "Unit type"), DATE_RAIL])
    p.append(page("PageRates", "4 - Rate Card", v))

    # -------------------------------------------------------- 5. CFO - Finance
    v = [banner(M, M, W - 2 * M, HEAD_H, "CFO - Finance",
                "Spend, discounts, forecast, budget variance and chargeback by business unit")]
    v += kpi_row([("Cost MTD", "Spend MTD"),
                  ("Forecast Cost (EOM)", "Forecast (EOM)"),
                  ("Monthly Budget", "Monthly Budget"),
                  ("Budget Variance", "Budget Variance"),
                  ("MoM Cost Delta %", "MoM Change")])
    v.append(chart("clusteredColumnChart", M, BODY_Y, BODY_W, ROW1_H,
                   "Budget vs forecast by business unit",
                   ("dim_business_unit", "business_unit_name"),
                   [(FACT, "Monthly Budget"), (FACT, "Forecast Cost (EOM)")],
                   legend=True, colors=["#4C8DFF", "#F5B93B"]))
    v.append(chart("areaChart", M, ROW2_Y, HALF, ROW2_H, "Spend trend by month",
                   ("dim_date", "year_month"), [(FACT, "Total AI Cost")],
                   order_desc=False, colors=["#12C2A8"]))
    v.append(table(M + HALF + G, ROW2_Y, HALF, ROW2_H, "Chargeback by business unit",
                   [("dim_business_unit", "business_unit_name", "c"),
                    (FACT, "Chargeback Cost", "m"),
                    (FACT, "Monthly Budget", "m"),
                    (FACT, "Budget Variance %", "m")]))
    v += rail([BU_RAIL, DATE_RAIL, PLAT_RAIL])
    p.append(page("PageCFO", "5 - CFO Finance", v))

    # ----------------------------------------------------------- 6. Governance
    v = [banner(M, M, W - 2 * M, HEAD_H, "Governance",
                "Who is spending, on what, and how much of it is billed rather than modelled")]
    v += kpi_row([("Cost Confidence %", "Cost Confidence"),
                  ("Chargeback Coverage %", "Chargeback Coverage"),
                  ("Unallocated Cost", "Unallocated Spend"),
                  ("Active Users", "Active Identities"),
                  ("Total Requests", "Total Requests")])
    v.append(chart("donutChart", M, BODY_Y, HALF, ROW1_H, "Spend by identity class",
                   ("dim_identity", "identity_class"), [(FACT, "Total AI Cost")]))
    v.append(chart("clusteredBarChart", M + HALF + G, BODY_Y, HALF, ROW1_H,
                   "Adoption by principal type",
                   ("dim_identity", "principal_type"), [(FACT, "Total Requests")],
                   colors=["#9B7CFF"]))
    v.append(table(M, ROW2_Y, BODY_W, ROW2_H, "REAL vs MOCK risk register",
                   [("dim_platform", "platform_name", "c"),
                    ("dim_platform", "data_source", "c"),
                    ("dim_platform", "billing_model", "c"),
                    (FACT, "Total AI Cost", "m"),
                    (FACT, "Cost Confidence %", "m")]))
    v += rail([("dim_platform", "data_source", "Provenance"),
               ("dim_identity", "identity_class", "Identity class"), DATE_RAIL])
    p.append(page("PageGov", "6 - Governance", v))

    # ---------------------------------------------------------- 7. Engineering
    v = [banner(M, M, W - 2 * M, HEAD_H, "Engineering",
                "Token consumption, unit economics by model and gateway latency")]
    v += kpi_row([("Total Tokens", "Total Tokens"),
                  ("Total Requests", "Requests"),
                  ("Avg Latency (ms)", "Avg Latency (ms)"),
                  ("Premium Requests", "Premium Requests"),
                  ("Copilot Credits", "Copilot Credits")])
    v.append(chart("clusteredColumnChart", M, BODY_Y, BODY_W, ROW1_H,
                   "Tokens and cost by model", ("dim_model", "model_name"),
                   [(FACT, "Total Tokens"), (FACT, "Total AI Cost")],
                   legend=True, colors=["#4C8DFF", "#12C2A8"]))
    v.append(chart("lineChart", M, ROW2_Y, HALF, ROW2_H, "Requests over time",
                   ("dim_date", "date_key"), [(FACT, "Total Requests")],
                   order_desc=False, colors=["#FF6E8A"]))
    v.append(table(M + HALF + G, ROW2_Y, HALF, ROW2_H, "Model detail",
                   [("dim_model", "model_name", "c"),
                    ("dim_model", "provider", "c"),
                    (FACT, "Total Tokens", "m"),
                    (FACT, "Avg Latency (ms)", "m")]))
    v += rail([("dim_model", "provider", "Provider"), PLAT_RAIL, DATE_RAIL])
    p.append(page("PageEng", "7 - Engineering", v))

    # ----------------------------------------------------- 8. Application Owner
    v = [banner(M, M, W - 2 * M, HEAD_H, "Application Owner",
                "Spend by application, trend and month-over-month movement")]
    v += kpi_row([("Total AI Cost", "Total AI Spend"),
                  ("Cost PM", "Previous Month"),
                  ("MoM Cost Delta %", "MoM Change"),
                  ("Cost (30d run-rate)", "30-day Run-rate"),
                  ("Variable Cost %", "Variable Share")])
    v.append(chart("barChart", M, BODY_Y, BODY_W, ROW1_H, "Spend by application",
                   ("dim_application", "application_name"),
                   [(FACT, "Total AI Cost")], colors=["#4C8DFF"]))
    v.append(chart("areaChart", M, ROW2_Y, HALF, ROW2_H, "Application spend trend",
                   ("dim_date", "year_month"), [(FACT, "Total AI Cost")],
                   order_desc=False, colors=["#12C2A8"]))
    v.append(table(M + HALF + G, ROW2_Y, HALF, ROW2_H, "Application detail",
                   [("dim_application", "application_name", "c"),
                    ("dim_application", "criticality", "c"),
                    (FACT, "Total AI Cost", "m"),
                    (FACT, "MoM Cost Delta %", "m")]))
    v += rail([("dim_application", "application_name", "Application"),
               ("dim_application", "criticality", "Criticality"), DATE_RAIL])
    p.append(page("PageApp", "8 - Application Owner", v))

    # ------------------------------------------------- 9. License Optimization
    v = [banner(M, M, W - 2 * M, HEAD_H, "License Optimization",
                "Idle licensed users, reclaimable spend and seat utilisation")]
    v += kpi_row([("Idle Licensed Users", "Idle Licensed Users"),
                  ("Idle Seat Waste (monthly)", "Reclaimable / month"),
                  ("Licensed Seats", "Licensed Seats"),
                  ("Active Users", "Active Users"),
                  ("Cost per Active User", "Cost / Active User")])
    v.append(chart("clusteredBarChart", M, BODY_Y, BODY_W, ROW1_H,
                   "Seat cost vs activity by user",
                   ("dim_identity", "display_name"),
                   [(FACT, "Fixed Cost"), (FACT, "Total Requests")],
                   legend=True, colors=["#FF6E8A", "#2ACE86"]))
    v.append(chart("donutChart", M, ROW2_Y, HALF, ROW2_H,
                   "Licensed seat cost by platform",
                   ("dim_platform", "platform_name"), [(FACT, "Fixed Cost")]))
    v.append(table(M + HALF + G, ROW2_Y, HALF, ROW2_H, "Reclaim candidates",
                   [("dim_identity", "display_name", "c"),
                    ("dim_identity", "team", "c"),
                    (FACT, "Total Requests", "m"),
                    (FACT, "Fixed Cost", "m")]))
    v += rail([("dim_identity", "identity_class", "Identity class"),
               ("dim_identity", "team", "Team"), DATE_RAIL])
    p.append(page("PageLicense", "9 - License Optimization", v))

    # ------------------------------------------- 10. Extractable Data Spectrum
    v = [banner(M, M, W - 2 * M, HEAD_H, "Extractable Data Spectrum",
                "Every AI cost signal per platform - source API, identity grain, cost fidelity")]
    v += kpi_row([("Extractable Signals", "Signals Catalogued", "dim_data_source"),
                  ("Signals Live (REAL)", "Live (REAL)", "dim_data_source"),
                  ("Signals Available", "Available (not wired)", "dim_data_source"),
                  ("Cost Confidence %", "Cost Confidence"),
                  ("Total AI Cost", "Total AI Spend")])
    v.append(chart("clusteredBarChart", M, BODY_Y, HALF, ROW1_H,
                   "Signals by platform", ("dim_data_source", "platform"),
                   [("dim_data_source", "Extractable Signals")], colors=["#4C8DFF"]))
    v.append(chart("donutChart", M + HALF + G, BODY_Y, HALF, ROW1_H,
                   "Signals by availability", ("dim_data_source", "availability"),
                   [("dim_data_source", "Extractable Signals")]))
    v.append(table(M, ROW2_Y, BODY_W, ROW2_H, "Signal catalogue",
                   [("dim_data_source", "platform", "c"),
                    ("dim_data_source", "signal", "c"),
                    ("dim_data_source", "source_api", "c"),
                    ("dim_data_source", "identity_granularity", "c"),
                    ("dim_data_source", "cost_fidelity", "c"),
                    ("dim_data_source", "availability", "c")]))
    v += rail([("dim_data_source", "platform", "Platform"),
               ("dim_data_source", "availability", "Availability"),
               ("dim_data_source", "cost_fidelity", "Cost fidelity")])
    p.append(page("PageSpectrum", "10 - Extractable Data Spectrum", v))

    # Fabric drops `ordinal: 0` as a default, and getDefinition returns pages in
    # arbitrary order, so page 1 would be unanchored. Zero-padded section names
    # give an independent, stable sort key -- the same thing Desktop emits.
    for i, pg in enumerate(p):
        pg["id"] = i
        pg["ordinal"] = i
        pg["name"] = "ReportSection%03d" % i
    return p


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", required=True, help="Direct Lake dataset GUID")
    ap.add_argument("--out", default="AIFinOps.DirectLake.Report")
    args = ap.parse_args()

    out = ROOT / args.out
    out.mkdir(parents=True, exist_ok=True)
    pages = build_pages()

    (out / "report.json").write_text(json.dumps({
        "id": 0, "resourcePackages": [],
        "config": json.dumps({
            "version": "5.43",
            "themeCollection": {"baseTheme": {"name": "CY24SU06"}},
            "activeSectionIndex": 0, "defaultDrillFilterOtherVisuals": True,
            "settings": {"useStylableVisualContainerHeader": True},
        }),
        "layoutOptimization": 0, "sections": pages, "filters": "[]", "pods": [],
    }, indent=2), encoding="utf-8")

    (out / "definition.pbir").write_text(json.dumps({
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/"
                   "report/definitionProperties/1.0.0/schema.json",
        "version": "4.0",
        "datasetReference": {"byConnection": {
            "connectionString": None, "pbiServiceModelId": None,
            "pbiModelVirtualServerName": "sobe_wowvirtualserver",
            "pbiModelDatabaseName": args.dataset, "name": "EntityDataSource",
            "connectionType": "pbiServiceXmlaStyleLive"}},
    }, indent=2), encoding="utf-8")

    (out / ".platform").write_text(json.dumps({
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/"
                   "gitIntegration/platformProperties/2.0.0/schema.json",
        "metadata": {"type": "Report", "displayName": args.out},
        "config": {"version": "2.0",
                   "logicalId": "00000000-0000-0000-0000-000000000004"},
    }, indent=2), encoding="utf-8")

    total = sum(len(pg["visualContainers"]) for pg in pages)
    print(f"{len(pages)} pages, {total} visuals -> {out.relative_to(ROOT)}")
    for pg in pages:
        print(f"  {pg['displayName']:32} {len(pg['visualContainers'])} visuals")
    return 0


if __name__ == "__main__":
    sys.exit(main())
