#!/usr/bin/env python3
"""Generate the 8-page themed report over the Direct Lake semantic model.

Page set matches the repo specification (CHANGE-SPEC.md); the old 10-page set
collapsed to 8 by merging Foundry Tokenomics + Engineering into one Engineering
Tokenomics page, and Waste & Utilisation + License Optimization into one Licence
Seats page:

    1  Spend Overview                    total, fixed vs variable, confidence,
                                         capability matrix incl. Cowork add-on
    2  Engineering Tokenomics            (old 2 + 7) tokens, requests, latency and
                                         unit economics across Foundry and Azure
                                         OpenAI; Platform/Provider/Model slicers
    3  Licence Seats, Waste & Utilisation (old 3 + 9) idle/low-use seats, seat
                                         action queue, per-platform seat cards
    4  Rate Card                         editable input + billed-vs-rate-card cost
    5  CFO - Finance                     spend, discounts, forecast, budget, MOCK flag
    6  Governance                        principal type, REAL-vs-MOCK, Cowork add-on
    7  Application Owner                 spend by application, workload attribution
    8  Extractable Data Spectrum         catalogue of every AI cost signal per platform

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


def multi_row_card(x, y, w, h, title, fields):
    """A multiRowCard: one category column plus a set of measures, laid out as
    stacked rows. Used on page 3 in place of the old per-platform donut so the
    Fixed Cost / Licensed Seats / Idle Users figures read as numbers, not slices."""
    pq, refs = build_query(fields)
    objects = {
        "dataLabels": [{"properties": {"color": solid(CARD_INK), "fontSize": num(12)}}],
        "categoryLabels": [{"properties": {"color": solid(CARD_INK), "fontSize": num(9)}}],
        "cardTitle": [{"properties": {"color": solid(CARD_INK), "fontSize": num(10),
                                      "fontFamily": s_lit("Segoe UI Semibold")}}],
    }
    return visual("multiRowCard", x, y, w, h,
                  {"Values": [{"queryRef": r} for r in refs]}, pq, objects,
                  vc_common(title))


def textbox_note(x, y, w, h, heading, lines, bg=CARD, ink=CARD_INK):
    """A prose card (textbox). Textboxes are exempt from the binding checks, so
    every number stays in a measure — the prose only tells the reader how to act."""
    runs = [{"textRuns": [{"value": heading,
                           "textStyle": {"fontSize": "12pt", "fontWeight": "bold",
                                         "color": ink, "fontFamily": "Segoe UI"}}]}]
    for line in lines:
        runs.append({"textRuns": [{"value": line,
                                   "textStyle": {"fontSize": "9.5pt", "color": ink,
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
                                               "color": solid(bg),
                                               "transparency": num(0)}}],
                "border": [{"properties": {"show": lit("true"),
                                           "color": solid(bg), "radius": num(10)}}],
                "title": [{"properties": {"show": lit("false")}}],
            },
        },
    }
    return {"x": x, "y": y, "z": 0, "width": w, "height": h,
            "config": json.dumps(cfg), "filters": "[]"}


def header_tooltip(text):
    """Visual-header tooltip objects, verified against the published PBIR schema
    (visualContainer/1.4.0). Both VisualHeader and VisualHeaderTooltip are
    `additionalProperties: false`, so ONLY these property names may be emitted:
    visualHeader -> show / showTooltipButton / showVisualInformationButton, and
    visualHeaderTooltip -> type / text. Single quotes in the text are doubled,
    exactly as the title code escapes them."""
    esc = text.replace("'", "''")
    return {
        "visualHeader": [{"properties": {
            "show": lit("true"),
            "showTooltipButton": lit("true"),
            "showVisualInformationButton": lit("true")}}],
        "visualHeaderTooltip": [{"properties": {
            "type": s_lit("Default"),
            "text": s_lit(esc)}}],
    }


def with_tooltip(container, text):
    """Merge a header tooltip into an already-built visual container. The tooltip
    icon only renders because showTooltipButton is true; check_report.py fails the
    build if a tooltip is configured without it."""
    cfg = json.loads(container["config"])
    cfg["singleVisual"].setdefault("vcObjects", {}).update(header_tooltip(text))
    container["config"] = json.dumps(cfg)
    return container


def exclude_filter(entity, column, value):
    """An advanced filter keeping only rows where column <> value. Used to drop
    the Unattributed Identity bar (identity_class = 'Unknown') from page 3 so it
    stops swamping the per-user chart; the excluded volume is surfaced separately
    as the Unattributed Requests KPI."""
    return json.dumps([{
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
                "Right": {"Literal": {"Value": f"'{value}'"}}}}}}}],
        },
    }])


def include_filter(entity, column, value):
    """An advanced filter keeping only rows where column = value.

    Page 3 is about paid seats, so its per-user visuals scope to
    identity_class = 'Human'. Merely excluding 'Unknown' is not enough: once the
    APIM gateway feed attributes Foundry traffic, the backend service principals
    carry ~890K requests against ~12K for every human combined, so they swamp the
    chart exactly the way Unattributed Identity used to. Service principals hold
    no licence, so they do not belong on a seat page at all."""
    return json.dumps([{
        "name": uid("f"),
        "expression": {"Column": {
            "Expression": {"SourceRef": {"Entity": entity}}, "Property": column}},
        "type": "Advanced",
        "filter": {
            "Version": 2,
            "From": [{"Name": "s", "Entity": entity, "Type": 0}],
            "Where": [{"Condition": {"Comparison": {
                "ComparisonKind": 0,
                "Left": {"Column": {"Expression": {"SourceRef": {"Source": "s"}},
                                    "Property": column}},
                "Right": {"Literal": {"Value": f"'{value}'"}}}}}],
        },
    }])


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
    v.append(with_tooltip(
        table(M + HALF + G, ROW2_Y, HALF, ROW2_H, "Platform capability matrix",
              [("dim_platform", "platform_name", "c"),
               ("dim_platform", "native_unit", "c"),
               ("dim_platform", "addon_unit", "c"),
               (FACT, "Cowork Credits", "m"),
               (FACT, "Cowork Add-on Cost", "m"),
               (FACT, "Total AI Cost", "m")]),
        "Microsoft 365 Copilot bills a fixed seat_day licence plus consumptive "
        "Cowork usage. The Cowork Credits column is the metered credit COUNT from "
        "the usage feed; the Cowork Add-on Cost is those credits valued at the "
        "$0.01 Copilot Credit list price - a MODELLED figure "
        "(cost_is_estimated = TRUE), not an invoiced amount, which is why the "
        "Cost Confidence % KPI on this page (and on Governance) stays below 100%. "
        "Other platforms are blank because they have no add-on unit."))
    v += rail([PLAT_RAIL, BU_RAIL, DATE_RAIL])
    p.append(page("PageSpend", "1 - Spend Overview", v))

    # ---------------------------------------- 2. Engineering Tokenomics (old 2+7)
    v = [banner(M, M, W - 2 * M, HEAD_H, "Engineering Tokenomics",
                "Token, request and latency economics across Azure AI Foundry and "
                "Azure OpenAI - use the Platform and Provider filters to switch")]
    v += kpi_row([("Total Tokens", "Total Tokens"),
                  ("Input Tokens", "Input Tokens"),
                  ("Output Tokens", "Output Tokens"),
                  ("Cache Hit Rate", "Cache Hit Rate"),
                  ("Cost per 1K Tokens", "Cost / 1K Tokens")])
    v.append(with_tooltip(
        chart("clusteredColumnChart", M, BODY_Y, BODY_W, ROW1_H,
              "Input vs output tokens by model",
              ("dim_model", "model_name"),
              [(FACT, "Input Tokens"), (FACT, "Output Tokens")],
              legend=True, colors=["#4C8DFF", "#12C2A8"]),
        "Only gateway-fronted Azure AI Foundry traffic (APIM AI Gateway -> Log "
        "Analytics) carries true per-request token telemetry. Azure OpenAI seen "
        "through Azure Monitor is resource-grain, so its tokens are aggregate, not "
        "per user. Switch between the two with the Platform and Provider filters."))
    v.append(chart("lineChart", M, ROW2_Y, HALF, ROW2_H, "Token volume over time",
                   ("dim_date", "date_key"), [(FACT, "Total Tokens")],
                   order_desc=False, colors=["#9B7CFF"]))
    v.append(table(M + HALF + G, ROW2_Y, HALF, ROW2_H, "Token economics by model",
                   [("dim_model", "model_name", "c"),
                    ("dim_model", "provider", "c"),
                    (FACT, "Total Tokens", "m"),
                    (FACT, "Total Requests", "m"),
                    (FACT, "Avg Latency (ms)", "m"),
                    (FACT, "Cost per 1K Tokens", "m")]))
    v += rail([PLAT_RAIL, ("dim_model", "provider", "Provider"),
               ("dim_model", "model_name", "Model"), DATE_RAIL])
    p.append(page("PageTokens", "2 - Engineering Tokenomics", v))

    # ------------------------ 3. Licence Seats, Waste & Utilisation (old 3+9)
    v = [banner(M, M, W - 2 * M, HEAD_H, "Licence Seats, Waste & Utilisation",
                "Idle and low-use licensed seats, the spend they represent, and the "
                "action to take on each")]
    v += kpi_row([("Idle Licensed Users", "Idle Licensed Seats"),
                  ("Low-Use Licensed Seats", "Low-Use Seats (1-20/28d)"),
                  ("Seat Utilisation %", "Seat Utilisation"),
                  ("Downgrade Candidate Spend (monthly)", "Downgrade Spend / mo"),
                  ("Unattributed Requests", "Unattributed Requests")])
    bar = chart("barChart", M, BODY_Y, HALF, ROW1_H,
                "Requests by licensed user",
                ("dim_identity", "display_name"), [(FACT, "Total Requests")],
                colors=["#F5B93B"])
    bar["filters"] = include_filter("dim_identity", "identity_class", "Human")
    v.append(with_tooltip(bar,
        "Scoped to human identities, the only population that holds a paid seat. "
        "Service principals and agents are excluded because they carry no licence "
        "- the Foundry gateway backends alone run ~890K requests against ~12K for "
        "all humans combined, which would swamp the chart. Requests that carry no "
        "principal claim at all are counted in the Unattributed Requests KPI "
        "above; they come from resource-grain Azure Monitor metrics, and wiring "
        "the APIM gateway is what attributes them."))
    seat_queue = table(M + HALF + G, BODY_Y, HALF, ROW1_H, "Seat action queue",
                       [("dim_identity", "display_name", "c"),
                        ("dim_identity", "team", "c"),
                        (FACT, "Total Requests", "m"),
                        (FACT, "Fixed Cost", "m"),
                        (FACT, "Seat Action", "m")])
    seat_queue["filters"] = include_filter("dim_identity", "identity_class", "Human")
    v.append(seat_queue)
    v.append(multi_row_card(M, ROW2_Y, HALF, ROW2_H, "Fixed cost & seats by platform",
                            [("dim_platform", "platform_name", "c"),
                             (FACT, "Fixed Cost", "m"),
                             (FACT, "Licensed Seats", "m"),
                             (FACT, "Idle Licensed Users", "m")]))
    v.append(textbox_note(M + HALF + G, ROW2_Y, HALF, ROW2_H,
                          "How to act on this page", [
        "1. Reclaim - no activity in 28 days: pull the seat back into the pool.",
        "2. Review or downgrade - 1 to 20 requests in 28 days: move to a lower tier.",
        "3. Renegotiate the tier when cost per active user exceeds the seat price.",
        "The numbers behind each rule live in the Seat Action, Idle Licensed Users "
        "and Cost per Active User measures - this card is guidance only."]))
    v += rail([("dim_identity", "identity_class", "Identity class"),
               PLAT_RAIL, DATE_RAIL])
    p.append(page("PageWaste", "3 - Licence Seats, Waste & Utilisation", v))

    # ------------------------------------------------------------ 4. Rate Card
    v = [banner(M, M, W - 2 * M, HEAD_H, "Rate Card",
                "The single customer-supplied input - swap for your EA/MCA price sheet")]
    v += kpi_row([("Billed Cost", "Billed (real)"),
                  ("Rate Card Cost", "Modelled (rate card / list)"),
                  ("Rate Card Coverage %", "Rate Card Coverage"),
                  ("Discounted Cost", "After Discounts"),
                  ("Discount Savings", "Negotiated Savings")])
    v.append(chart("clusteredBarChart", M, BODY_Y, BODY_W, ROW1_H,
                   "Billed vs rate card by platform",
                   ("dim_platform", "platform_name"),
                   [(FACT, "Billed Cost"), (FACT, "Rate Card Cost")],
                   legend=True, colors=["#2ACE86", "#F5B93B"]))
    v.append(with_tooltip(
        table(M, ROW2_Y, BODY_W, ROW2_H, "Rate card - edit these values",
              [("dim_rate_card", "platform", "c"),
               ("dim_rate_card", "unit_type", "c"),
               ("dim_rate_card", "model", "c"),
               ("dim_rate_card", "unit_price_usd", "c"),
               ("dim_rate_card", "source", "c")]),
        "To input modelled cost, edit dim_rate_card: in the PoC "
        "AIFinOps.SemanticModel/data/dim_rate_card.csv, in Fabric "
        "platform/fabric/bronze_out/bronze_ref_rate_card.csv -> silver_rate_card "
        "-> dim_rate_card. One row per (platform, unit_type, model) with your "
        "EA/MCA effective price. Azure meters need no row because FOCUS carries "
        "ListCost."))
    v += rail([("dim_rate_card", "platform", "Platform"),
               ("dim_rate_card", "unit_type", "Unit type"), DATE_RAIL])
    p.append(page("PageRates", "4 - Rate Card", v))

    # -------------------------------------------------------- 5. CFO - Finance
    v = [banner(M, M, W - 2 * M, HEAD_H, "CFO - Finance",
                "Spend, discounts, forecast, budget variance and chargeback by business unit")]
    kpis = kpi_row([("Cost MTD", "Spend MTD"),
                    ("Forecast Cost (EOM)", "Forecast (EOM)"),
                    ("Monthly Budget", "Monthly Budget"),
                    ("Budget Variance", "Budget Variance"),
                    ("Budget Coverage %", "Budget Coverage")])
    with_tooltip(kpis[2],
        "Budgets live in dim_business_unit[monthly_budget_usd], one row per "
        "business unit. Set them in bronze_ref_business_hierarchy.csv (Fabric) or "
        "dim_business_unit.csv (PoC). Flip is_mock_budget to FALSE when real "
        "budgets are loaded so demo figures stop reading as approved plan.")
    v += kpis
    v.append(with_tooltip(
        chart("clusteredColumnChart", M, BODY_Y, BODY_W, ROW1_H,
              "Budget vs forecast by business unit",
              ("dim_business_unit", "business_unit_name"),
              [(FACT, "Monthly Budget"), (FACT, "Forecast Cost (EOM)")],
              legend=True, colors=["#4C8DFF", "#F5B93B"]),
        "Forecast is compared against dim_business_unit[monthly_budget_usd]. Demo "
        "budgets are flagged is_mock_budget = TRUE (see the chargeback table); edit "
        "bronze_ref_business_hierarchy.csv (Fabric) or dim_business_unit.csv (PoC) "
        "and set is_mock_budget FALSE once real budgets are loaded."))
    v.append(chart("areaChart", M, ROW2_Y, HALF, ROW2_H, "Spend trend by month",
                   ("dim_date", "year_month"), [(FACT, "Total AI Cost")],
                   order_desc=False, colors=["#12C2A8"]))
    v.append(table(M + HALF + G, ROW2_Y, HALF, ROW2_H, "Chargeback by business unit",
                   [("dim_business_unit", "business_unit_name", "c"),
                    ("dim_business_unit", "is_mock_budget", "c"),
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
                  ("Cowork Add-on Cost", "Cowork Add-on Cost")])
    v.append(chart("donutChart", M, BODY_Y, HALF, ROW1_H, "Spend by identity class",
                   ("dim_identity", "identity_class"), [(FACT, "Total AI Cost")]))
    v.append(with_tooltip(
        chart("clusteredBarChart", M + HALF + G, BODY_Y, HALF, ROW1_H,
              "Adoption by principal type",
              ("dim_identity", "principal_type"), [(FACT, "Total Requests")],
              colors=["#9B7CFF"]),
        "principal_type is the Entra directory object behind the request - User, "
        "ServicePrincipal, ManagedIdentity or Agent. Unknown is telemetry that "
        "carries NO principal claim at all (Azure Monitor metrics at resource "
        "grain, FOCUS invoice lines), not a privilege problem. Wiring the APIM "
        "gateway (Entra JWT) and Graph with Reports.Read.All / Directory.Read.All "
        "collapses Unknown to near zero; it can never reach zero while "
        "invoice-grain rows are in scope."))
    v.append(table(M, ROW2_Y, BODY_W, ROW2_H, "REAL vs MOCK risk register",
                   [("dim_platform", "platform_name", "c"),
                    ("dim_platform", "data_source", "c"),
                    ("dim_platform", "billing_model", "c"),
                    (FACT, "Total AI Cost", "m"),
                    (FACT, "Cowork Add-on Cost", "m"),
                    (FACT, "Cost Confidence %", "m")]))
    v += rail([("dim_platform", "data_source", "Provenance"),
               ("dim_identity", "identity_class", "Identity class"), DATE_RAIL])
    p.append(page("PageGov", "6 - Governance", v))

    # ----------------------------------------------------- 7. Application Owner
    v = [banner(M, M, W - 2 * M, HEAD_H, "Application Owner",
                "Spend by application, how much is attributed to a named workload, "
                "and month-over-month movement")]
    v += kpi_row([("Total AI Cost", "Total AI Spend"),
                  ("Cost PM", "Previous Month"),
                  ("Cost (30d run-rate)", "30-day Run-rate"),
                  ("Unattributed Workload Cost", "Unattributed Workload $"),
                  ("Workload Attribution %", "Workload Attribution")])
    v.append(with_tooltip(
        chart("barChart", M, BODY_Y, BODY_W, ROW1_H, "Spend by application",
              ("dim_application", "application_name"),
              [(FACT, "Total AI Cost")], colors=["#4C8DFF"]),
        "Unattributed Workload (APP-UNKNOWN) is spend whose source feed names no "
        "workload - an Azure meter on a resource absent from "
        "bronze_ref_app_inventory, or Foundry traffic that did not pass the "
        "gateway. It is derived in 20_build_star.py; fix it by tagging the "
        "resource or adding it to the app inventory, not by reassigning the "
        "dollars."))
    v.append(chart("areaChart", M, ROW2_Y, HALF, ROW2_H, "Application spend trend",
                   ("dim_date", "year_month"), [(FACT, "Total AI Cost")],
                   order_desc=False, colors=["#12C2A8"]))
    v.append(table(M + HALF + G, ROW2_Y, HALF, ROW2_H, "Application detail",
                   [("dim_application", "application_name", "c"),
                    ("dim_application", "application_type", "c"),
                    ("dim_application", "criticality", "c"),
                    (FACT, "Total AI Cost", "m"),
                    (FACT, "MoM Cost Delta %", "m")]))
    v += rail([("dim_application", "application_name", "Application"),
               ("dim_application", "criticality", "Criticality"), DATE_RAIL])
    p.append(page("PageApp", "7 - Application Owner", v))

    # -------------------------------------------- 8. Extractable Data Spectrum
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
    v.append(with_tooltip(
        table(M, ROW2_Y, BODY_W, ROW2_H, "Signal catalogue",
              [("dim_data_source", "platform", "c"),
               ("dim_data_source", "signal", "c"),
               ("dim_data_source", "source_api", "c"),
               ("dim_data_source", "identity_granularity", "c"),
               ("dim_data_source", "cost_fidelity", "c"),
               ("dim_data_source", "availability", "c")]),
        "The availability scale is honest by design: REAL = this repo reads it "
        "today; AVAILABLE = a documented surface (API or CSV export) exists but is "
        "not wired here; MOCK = demo data stands in for a real feed; ROADMAP = "
        "announced or planned, not yet extractable."))
    v += rail([("dim_data_source", "platform", "Platform"),
               ("dim_data_source", "availability", "Availability"),
               ("dim_data_source", "cost_fidelity", "Cost fidelity")])
    p.append(page("PageSpectrum", "8 - Extractable Data Spectrum", v))

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
