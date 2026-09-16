#!/usr/bin/env python3
"""
Append 4 persona report pages to AIFinOps.Report/report.json (legacy PBIR).

Additive and idempotent: existing pages 1-4 (built by build_report.py) are
preserved untouched; persona pages are re-generated each run (matched by section
name PERSONA_* / DATA_SPECTRUM). Together they form the 8-page set in
CHANGE-SPEC.md:

    5 - CFO Finance
    6 - Governance
    7 - Application Owner
    8 - Extractable Data Spectrum

The old Engineering and License Optimization persona pages are removed; their
content folds into pages 2 (Engineering Tokenomics) and 3 (Licence Seats) built
by build_report.py. Every queryRef points at a measure/column that exists in the
semantic model, or a measure CHANGE-SPEC.md adds in parallel via WS-MODEL.

Run:  python3 build_personas.py
"""
import json, os, itertools

RP = os.path.join(os.path.dirname(__file__), "AIFinOps.Report", "report.json")

# entity -> short alias used inside prototypeQuery
ALIAS = {
    "fact_ai_usage": "fau", "dim_platform": "dp", "dim_date": "dd",
    "dim_model": "dm", "dim_identity": "di", "dim_business_unit": "dbu",
    "dim_application": "da", "dim_environment": "de", "dim_cost_center": "dcc",
    "dim_rate_card": "drc",
    "dim_data_source": "dds",
}
# which fields are measures (all live on fact_ai_usage)
_MEASURES = {
    "Total AI Cost", "Billed Cost", "Modelled Cost", "Cost Confidence %",
    "Fixed Cost", "Variable Cost", "Variable Cost %", "Total Tokens",
    "Input Tokens", "Output Tokens", "Cached Tokens", "Cache Hit Rate",
    "Cost per 1K Tokens", "Copilot Credits", "Premium Requests", "M365 Prompts",
    "Licensed Seats", "Total Requests", "Active Users", "Cost per Active User",
    "Idle Licensed Users", "Idle Seat Waste (monthly)", "Error Rate",
    "Avg Latency (ms)", "Cost PM", "MoM Cost Delta %", "Cost (30d run-rate)",
    "Discounted Cost", "Discount Savings", "Cost MTD", "Forecast Cost (EOM)",
    "Forecast Cost (next 30d, net)", "Attributable Cost", "Unallocated Cost",
    "Chargeback Coverage %", "Chargeback Cost", "Monthly Budget",
    "Budget Variance", "Budget Variance %",
    # New measures introduced by CHANGE-SPEC.md; added to the semantic model in
    # parallel by WS-MODEL, so bind by exact spec name here.
    "Cowork Add-on Cost", "Workload Attribution %",
}


_DS_MEASURES = {"Extractable Signals", "Signals Live (REAL)", "Signals Available"}


def _ref(entity, prop):
    """One Select entry + queryRef, dispatching measure vs column."""
    src = {"Expression": {"SourceRef": {"Source": ALIAS[entity]}}, "Property": prop}
    is_meas = (entity == "fact_ai_usage" and prop in _MEASURES) or \
              (entity == "dim_data_source" and prop in _DS_MEASURES)
    kind = "Measure" if is_meas else "Column"
    return {kind: src, "Name": f"{entity}.{prop}"}, f"{entity}.{prop}"


def _from(entities):
    return [{"Name": ALIAS[e], "Entity": e, "Type": 0} for e in entities]


def _title(text):
    return {"title": [{"properties": {
        "text": {"expr": {"Literal": {"Value": f"'{text}'"}}},
        "show": {"expr": {"Literal": {"Value": "true"}}}}}]}


def _vc(x, y, w, h, name, visual_type, projections, entities, selects, title):
    cfg = {
        "name": name,
        "layouts": [{"id": 0, "position": {"x": x, "y": y, "z": 0, "width": w, "height": h}}],
        "singleVisual": {
            "visualType": visual_type,
            "projections": projections,
            "prototypeQuery": {"Version": 2, "From": _from(entities), "Select": selects},
            "drillFilterOtherVisuals": True,
            "objects": {},
            "vcObjects": _title(title),
        },
    }
    return {"x": x, "y": y, "z": 0, "width": w, "height": h,
            "config": json.dumps(cfg), "filters": "[]"}


_ids = itertools.count(1)


def card(x, y, entity, prop, title, w=200, h=110):
    sel, qref = _ref(entity, prop)
    return _vc(x, y, w, h, f"vc{next(_ids)}", "card",
               {"Values": [{"queryRef": qref}]}, [entity], [sel], title)


def _catval(x, y, w, h, vtype, cat_e, cat_c, val_e, val_c, title):
    s1, q1 = _ref(cat_e, cat_c)
    s2, q2 = _ref(val_e, val_c)
    ents = [cat_e] if cat_e == val_e else [cat_e, val_e]
    return _vc(x, y, w, h, f"vc{next(_ids)}", vtype,
               {"Category": [{"queryRef": q1}], "Y": [{"queryRef": q2}]},
               ents, [s1, s2], title)


def column(x, y, w, h, cat_e, cat_c, val_c, title):
    return _catval(x, y, w, h, "clusteredColumnChart", cat_e, cat_c, "fact_ai_usage", val_c, title)


def bar(x, y, w, h, cat_e, cat_c, val_c, title):
    return _catval(x, y, w, h, "clusteredBarChart", cat_e, cat_c, "fact_ai_usage", val_c, title)


def line(x, y, w, h, val_c, title, cat_e="dim_date", cat_c="date_key"):
    return _catval(x, y, w, h, "lineChart", cat_e, cat_c, "fact_ai_usage", val_c, title)


def table(x, y, w, h, cols, title):
    selects, vals, ents = [], [], []
    for e, c in cols:
        s, q = _ref(e, c)
        selects.append(s); vals.append({"queryRef": q})
        if e not in ents: ents.append(e)
    return _vc(x, y, w, h, f"vc{next(_ids)}", "tableEx",
               {"Values": vals}, ents, selects, title)


def cards_row(specs, y=16):
    xs = 16
    out = []
    for entity, prop, title in specs:
        out.append(card(xs, y, entity, prop, title))
        xs += 208
    return out


def section(sid, name, display, visuals):
    return {"id": sid, "name": name, "displayName": display, "ordinal": sid,
            "width": 1280, "height": 720, "displayOption": 1,
            "config": "{}", "filters": "[]", "visualContainers": visuals}


# --------------------------------------------------------------------- personas
def page_cfo():
    v = cards_row([
        ("fact_ai_usage", "Total AI Cost", "Total AI Spend"),
        ("fact_ai_usage", "Discounted Cost", "Net of Discounts"),
        ("fact_ai_usage", "Forecast Cost (EOM)", "Forecast (EOM)"),
        ("fact_ai_usage", "Monthly Budget", "Monthly Budget"),
        ("fact_ai_usage", "Budget Variance %", "Budget Variance %"),
        ("fact_ai_usage", "Chargeback Coverage %", "Chargeback Coverage"),
    ])
    v += [column(16, 150, 624, 300, "dim_business_unit", "business_unit_name",
                 "Chargeback Cost", "Chargeback by business unit"),
          line(656, 150, 608, 300, "Total AI Cost", "Daily spend trend"),
          table(16, 466, 1248, 236, [
              ("dim_business_unit", "business_unit_name"),
              ("dim_business_unit", "division"),
              ("dim_business_unit", "is_mock_budget"),
              ("fact_ai_usage", "Total AI Cost"),
              ("fact_ai_usage", "Chargeback Cost"),
              ("fact_ai_usage", "Monthly Budget"),
              ("fact_ai_usage", "Budget Variance"),
              ("fact_ai_usage", "Budget Variance %")],
              "Business-unit allocation vs budget — is_mock_budget flags demo budgets")]
    return section(4, "PERSONA_CFO", "5 - CFO Finance", v)


def page_governance():
    v = cards_row([
        ("fact_ai_usage", "Active Users", "Active Principals"),
        ("fact_ai_usage", "Licensed Seats", "Licensed Seats"),
        ("fact_ai_usage", "Cost Confidence %", "Cost Confidence"),
        ("fact_ai_usage", "Cowork Add-on Cost", "Cowork Add-on Cost"),
    ])
    v += [column(16, 150, 624, 300, "dim_platform", "platform_name",
                 "Total AI Cost", "Platform adoption (spend)"),
          bar(656, 150, 608, 300, "dim_identity", "principal_type",
              "Total Requests", "Adoption by principal type"),
          table(16, 466, 1248, 236, [
              ("dim_platform", "platform_name"),
              ("dim_platform", "data_source"),
              ("dim_platform", "billing_model"),
              ("fact_ai_usage", "Total AI Cost"),
              ("fact_ai_usage", "Cowork Add-on Cost"),
              ("fact_ai_usage", "Cost Confidence %")],
              "Platform policy & risk register — REAL vs MOCK provenance, Cowork add-on")]
    return section(5, "PERSONA_GOV", "6 - Governance", v)


def page_appowner():
    v = cards_row([
        ("fact_ai_usage", "Total AI Cost", "Application Spend"),
        ("fact_ai_usage", "Workload Attribution %", "Workload Attribution"),
        ("fact_ai_usage", "Total Tokens", "Total Tokens"),
        ("fact_ai_usage", "MoM Cost Delta %", "MoM Cost Delta"),
    ])
    v += [column(16, 150, 624, 300, "dim_application", "application_name",
                 "Total AI Cost", "Spend by application"),
          line(656, 150, 608, 300, "Total AI Cost", "Application spend trend"),
          table(16, 466, 1248, 236, [
              ("dim_application", "application_name"),
              ("dim_application", "application_type"),
              ("dim_application", "criticality"),
              ("fact_ai_usage", "Total AI Cost"),
              ("fact_ai_usage", "Total Tokens"),
              ("fact_ai_usage", "MoM Cost Delta %")],
              "Application cost, type & trend — Unattributed Workload = APP-UNKNOWN")]
    return section(6, "PERSONA_APP", "7 - Application Owner", v)


def _ds_chart(x, y, w, h, vtype, cat_c, meas, title):
    s1, q1 = _ref("dim_data_source", cat_c)
    s2, q2 = _ref("dim_data_source", meas)
    return _vc(x, y, w, h, f"vc{next(_ids)}", vtype,
               {"Category": [{"queryRef": q1}], "Y": [{"queryRef": q2}]},
               ["dim_data_source"], [s1, s2], title)


def page_datasources():
    v = cards_row([
        ("dim_data_source", "Extractable Signals", "Extractable Signals"),
        ("dim_data_source", "Signals Live (REAL)", "Live (REAL) Now"),
        ("dim_data_source", "Signals Available", "Available (API exists)"),
    ])
    v += [_ds_chart(16, 150, 624, 300, "clusteredColumnChart", "platform",
                    "Extractable Signals", "Signals by platform"),
          _ds_chart(656, 150, 608, 300, "clusteredBarChart", "availability",
                    "Extractable Signals", "Signals by availability (REAL/AVAILABLE/MOCK/ROADMAP)"),
          table(16, 466, 1248, 236, [
              ("dim_data_source", "platform"),
              ("dim_data_source", "signal_category"),
              ("dim_data_source", "signal"),
              ("dim_data_source", "source_api"),
              ("dim_data_source", "identity_granularity"),
              ("dim_data_source", "cost_fidelity"),
              ("dim_data_source", "availability")],
              "Full extractable-data spectrum — every AI cost signal, its source & fidelity")]
    return section(7, "DATA_SPECTRUM", "8 - Extractable Data Spectrum", v)


def main():
    r = json.load(open(RP))
    persona_names = {"PERSONA_CFO", "PERSONA_GOV", "PERSONA_ENG", "PERSONA_APP",
                     "PERSONA_LIC", "DATA_SPECTRUM"}
    r["sections"] = [s for s in r["sections"] if s["name"] not in persona_names]
    # Old PERSONA_ENG (Engineering) and PERSONA_LIC (License Optimization) are
    # deleted; their content folds into pages 2 and 3 respectively, matching the
    # 8-page set in CHANGE-SPEC.md.
    r["sections"] += [page_cfo(), page_governance(),
                      page_appowner(), page_datasources()]
    for i, s in enumerate(r["sections"]):
        s["ordinal"] = i
    with open(RP, "w") as f:
        json.dump(r, f, indent=2)
    print(f"report now has {len(r['sections'])} pages:")
    for s in r["sections"]:
        print(f"  {s['name']:14} {s['displayName']!r:40} {len(s['visualContainers'])} visuals")


if __name__ == "__main__":
    main()
