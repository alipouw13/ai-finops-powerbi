#!/usr/bin/env python3
"""
Append 5 persona report pages to AIFinOps.Report/report.json (legacy PBIR).

Additive and idempotent: existing pages 1-4 are preserved untouched; persona
pages are re-generated each run (matched by section name PERSONA_*). Every
queryRef points at a measure/column that exists in the semantic model.

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
    "fact_user_cost_allocation": "fuc",
    "dim_agent": "dag", "fact_agent_inventory": "fai", "fact_agent_activity": "faa",
    "dim_persona": "dpe", "bridge_persona_platform": "bpp",
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
}


_DS_MEASURES = {"Extractable Signals", "Signals Live (REAL)", "Signals Available"}
_ALLOC_MEASURES = {"User Allocated Cost", "User Allocated Tokens", "User Allocated Requests", "Directly Attributed Cost",
                  "Owner Allocated Cost", "Unallocated User Cost", "Directly Attributed Cost %",
                  "Owner Allocated Tokens", "Allocated Users"}
_AGENT_MEASURES = {"Agents", "Unowned Tenant Agents", "Agents With Owner", "Agents With Entra Identity",
                   "Agents With Entra Identity %", "Agents With File or Site Access",
                   "Agents With Custom Actions", "Agents With Code Interpreter", "Available Agents"}
_ACT_MEASURES = {"Agent Active Users", "Agent Sessions", "Agent Users Resolved %"}
_BRIDGE_MEASURES = {"Spend on Primary Tool %"}
_ENTITY_MEASURES = {"fact_user_cost_allocation": _ALLOC_MEASURES, "fact_agent_inventory": _AGENT_MEASURES,
                    "fact_agent_activity": _ACT_MEASURES, "bridge_persona_platform": _BRIDGE_MEASURES}


def _ref(entity, prop):
    """One Select entry + queryRef, dispatching measure vs column."""
    src = {"Expression": {"SourceRef": {"Source": ALIAS[entity]}}, "Property": prop}
    is_meas = (entity == "fact_ai_usage" and prop in _MEASURES) or \
              (entity == "dim_data_source" and prop in _DS_MEASURES) or \
              prop in _ENTITY_MEASURES.get(entity, ())
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
              ("fact_ai_usage", "Total AI Cost"),
              ("fact_ai_usage", "Chargeback Cost"),
              ("fact_ai_usage", "Monthly Budget"),
              ("fact_ai_usage", "Budget Variance"),
              ("fact_ai_usage", "Budget Variance %")],
              "Business-unit allocation vs budget")]
    return section(4, "PERSONA_CFO", "5 · CFO — Finance", v)


def page_governance():
    v = cards_row([
        ("fact_ai_usage", "Active Users", "Active Principals"),
        ("fact_ai_usage", "Licensed Seats", "Licensed Seats"),
        ("fact_ai_usage", "Total Requests", "Total Requests"),
        ("fact_ai_usage", "Cost Confidence %", "Cost Confidence"),
    ])
    v += [column(16, 150, 624, 300, "dim_platform", "platform_name",
                 "Total AI Cost", "Platform adoption (spend)"),
          bar(656, 150, 608, 300, "dim_identity", "identity_class",
              "Active Users", "Adoption by principal type"),
          table(16, 466, 1248, 236, [
              ("dim_platform", "platform_name"),
              ("dim_platform", "data_source"),
              ("dim_platform", "billing_model"),
              ("dim_platform", "is_variable_cost"),
              ("fact_ai_usage", "Total AI Cost"),
              ("fact_ai_usage", "Cost Confidence %")],
              "Platform policy & risk register — REAL vs MOCK provenance")]
    return section(5, "PERSONA_GOV", "6 · Governance", v)


def page_engineering():
    v = cards_row([
        ("fact_ai_usage", "Total Tokens", "Total Tokens"),
        ("fact_ai_usage", "Input Tokens", "Input Tokens"),
        ("fact_ai_usage", "Output Tokens", "Output Tokens"),
        ("fact_ai_usage", "Cache Hit Rate", "Cache Hit Rate"),
        ("fact_ai_usage", "Avg Latency (ms)", "Avg Latency (ms)"),
        ("fact_ai_usage", "Error Rate", "Error Rate"),
    ])
    v += [line(16, 150, 624, 300, "Total Tokens", "Token consumption trend"),
          column(656, 150, 608, 300, "dim_model", "model_name",
                 "Cost per 1K Tokens", "Unit economics by model"),
          table(16, 466, 1248, 236, [
              ("dim_model", "model_name"),
              ("dim_model", "provider"),
              ("fact_ai_usage", "Total Tokens"),
              ("fact_ai_usage", "Cached Tokens"),
              ("fact_ai_usage", "Cache Hit Rate"),
              ("fact_ai_usage", "Avg Latency (ms)"),
              ("fact_ai_usage", "Error Rate")],
              "Model performance & APIM telemetry (Foundry = REAL)")]
    return section(6, "PERSONA_ENG", "7 · Engineering", v)


def page_appowner():
    v = cards_row([
        ("fact_ai_usage", "Total AI Cost", "Application Spend"),
        ("fact_ai_usage", "Variable Cost", "Variable Cost"),
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
              "Application cost, model usage & trend")]
    return section(7, "PERSONA_APP", "8 · Application Owner", v)


def page_license():
    v = cards_row([
        ("fact_ai_usage", "Idle Licensed Users", "Idle Licensed Users"),
        ("fact_ai_usage", "Idle Seat Waste (monthly)", "Reclaimable / mo"),
        ("fact_ai_usage", "Licensed Seats", "Licensed Seats"),
        ("fact_ai_usage", "Cost per Active User", "Cost / Active User"),
    ])
    v += [column(16, 150, 624, 300, "dim_business_unit", "business_unit_name",
                 "Idle Seat Waste (monthly)", "Reclaimable spend by business unit"),
          bar(656, 150, 608, 300, "dim_business_unit", "business_unit_name",
              "Active Users", "Active users by business unit"),
          table(16, 466, 1248, 236, [
              ("dim_identity", "display_name"),
              ("dim_identity", "team"),
              ("dim_identity", "business_unit"),
              ("fact_ai_usage", "Licensed Seats"),
              ("fact_ai_usage", "Total Requests"),
              ("fact_ai_usage", "M365 Prompts")],
              "Seat utilisation — zero activity = reclaim candidate")]
    return section(8, "PERSONA_LIC", "9 · License Optimization", v)


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
    return section(9, "DATA_SPECTRUM", "10 · Extractable Data Spectrum", v)


def page_user_allocation():
    f, u, a = "fact_user_cost_allocation", "dim_identity", "dim_application"
    v = cards_row([
        (f, "User Allocated Cost", "Cost allocated to a user key"),
        (f, "Directly Attributed Cost %", "Directly attributed %"),
        (f, "Owner Allocated Cost", "Owner-allocated cost"),
        (f, "Unallocated User Cost", "Unallocated cost"),
        (f, "User Allocated Tokens", "Tokens"),
        (f, "Allocated Users", "Users with allocated spend"),
    ])
    v += [_catval(16, 150, 624, 300, "clusteredBarChart", u, "display_name", f, "User Allocated Cost",
                  "Cost by user (DIRECT = caller is human; OWNER = app owner; Unallocated = no owner)"),
          _catval(656, 150, 608, 300, "clusteredBarChart", a, "application_name", f, "User Allocated Tokens",
                  "Tokens by application / agent"),
          table(16, 466, 1248, 236, [
              (u, "display_name"), (a, "application_name"), (f, "allocation_basis"),
              (f, "User Allocated Tokens"), (f, "User Allocated Requests"), (f, "User Allocated Cost")],
              "User x application. Same dollars as Total AI Cost, re-keyed to a person; OWNER is accountability, not proof of who made the calls")]
    return section(10, "USER_ALLOC", "11 · User Cost Allocation", v)

def page_agents():
    i, a, act = "fact_agent_inventory", "dim_agent", "fact_agent_activity"
    v = cards_row([
        (i, "Agents", "Agents in registry"),
        (i, "Unowned Tenant Agents", "Unowned tenant agents"),
        (i, "Agents With Entra Identity %", "With Entra agent ID"),
        (i, "Agents With File or Site Access", "Can read files / sites"),
        (act, "Agent Active Users", "Agent users (export)"),
        (act, "Agent Users Resolved %", "Users matched to identity"),
    ])
    v += [_catval(16, 150, 408, 300, "clusteredBarChart", a, "owner_class", i, "Agents",
                  "Agents by ownership (Unowned = governance gap)"),
          _catval(440, 150, 408, 300, "clusteredBarChart", a, "platform", i, "Agents",
                  "Agents by build platform (blank = store / unclassified)"),
          _catval(864, 150, 400, 300, "clusteredColumnChart", a, "status", i, "Agents", "Agents by status"),
          table(16, 466, 1248, 236, [
              (a, "agent_name"), (a, "publisher"), (a, "publisher_type"), (a, "owner_class"),
              (a, "owner_upn"), (a, "platform"), (a, "status"), (i, "Agents With Entra Identity")],
              "Agent 365 registry (REAL export). No cost per agent is exposed by Agent 365; cost joins by owner or tag")]
    return section(11, "AGENT365", "12 · Agent 365 Governance", v)


def page_personas():
    p, b = "dim_persona", "bridge_persona_platform"
    v = cards_row([
        ("fact_ai_usage", "Total AI Cost", "AI cost (all personas)"),
        ("fact_ai_usage", "Active Users", "Active principals"),
        (b, "Spend on Primary Tool %", "Spend on primary tool"),
    ])
    v += [_catval(16, 150, 624, 300, "clusteredBarChart", p, "persona_name", "fact_ai_usage", "Total AI Cost",
                  "AI cost by persona (ASSUMED persona mapping)"),
          _catval(656, 150, 608, 300, "clusteredBarChart", p, "persona_name", b, "Spend on Primary Tool %",
                  "Share of persona spend on its primary tool"),
          table(16, 466, 1248, 236, [
              (p, "persona_name"), ("dim_platform", "platform_name"), (b, "affinity"),
              ("fact_ai_usage", "Total AI Cost")],
              "Persona to tool affinity (Primary / Secondary) from the stakeholder map; Cowork and Azure ML are not ingested yet")]
    return section(12, "PERSONAS", "13 · Persona Coverage", v)


def main():
    r = json.load(open(RP))
    persona_names = {"PERSONA_CFO", "PERSONA_GOV", "PERSONA_ENG", "PERSONA_APP",
                     "PERSONA_LIC", "DATA_SPECTRUM", "DATABRICKS_TAGS", "USER_ALLOC", "AGENT365", "PERSONAS"}
    r["sections"] = [s for s in r["sections"] if s["name"] not in persona_names]
    r["sections"] += [page_cfo(), page_governance(), page_engineering(),
                      page_appowner(), page_license(), page_datasources(), page_user_allocation(),
                      page_agents(), page_personas()]
    for i, s in enumerate(r["sections"]):
        s["ordinal"] = i
    with open(RP, "w") as f:
        json.dump(r, f, indent=2)
    print(f"report now has {len(r['sections'])} pages:")
    for s in r["sections"]:
        print(f"  {s['name']:14} {s['displayName']!r:40} {len(s['visualContainers'])} visuals")


if __name__ == "__main__":
    main()
