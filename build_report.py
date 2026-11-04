#!/usr/bin/env python3
"""
Emit AIFinOps.Report/report.json with fully-bound visuals.

Each visual needs a real `prototypeQuery` (From aliases + Select entries) whose
Select[].Name matches the projection queryRef. Without that the container
renders its title but stays empty.
"""
import json
from pathlib import Path

RP = Path(__file__).parent / "AIFinOps.Report"
RP.mkdir(parents=True, exist_ok=True)
W, H = 1280, 720
F = "fact_ai_usage"

# Everything defined as a measure on fact_ai_usage; anything else is a column.
MEASURES = {
    "Total AI Cost", "Billed Cost", "Modelled Cost", "Cost Confidence %",
    "Fixed Cost", "Variable Cost", "Variable Cost %", "Total Tokens",
    "Input Tokens", "Output Tokens", "Cached Tokens", "Cache Hit Rate",
    "Cost per 1K Tokens", "Copilot Credits", "Premium Requests", "M365 Prompts",
    "Licensed Seats", "Total Requests", "Active Users", "Cost per Active User",
    "Idle Licensed Users", "Idle Seat Waste (monthly)", "Error Rate",
    "Avg Latency (ms)", "Cost PM", "MoM Cost Delta %", "Cost (30d run-rate)",
    "Discounted Cost", "Discount Savings", "FOCUS List Cost", "FOCUS Contracted Cost",
    "FOCUS Effective Cost", "FOCUS Billed Cost", "Total FOCUS Savings",
    "Effective Savings Rate (ESR %)", "Negotiated Discount Savings",
    "Commitment Discount Savings", "PTU Commitment Cost", "PTU Unused Waste",
    "Commitment Utilization %", "Total Actionable Waste", "Gateway Routed Spend",
    "Bypassed Gateway Spend", "Gateway Governance %", "Supporting AI Infra Spend",
    "Supporting Infra % of TCO", "Agent Attributed Spend", "Cost MTD",
    "Forecast Cost (EOM)", "Forecast Cost (next 30d, net)", "Attributable Cost",
    "Unallocated Cost", "Chargeback Coverage %", "Chargeback Cost",
    "Monthly Budget", "Budget Variance", "Budget Variance %",
}

_alias = {}


def _a(entity):
    """Stable short alias per entity, e.g. fact_ai_usage -> f."""
    if entity not in _alias:
        base = "".join(w[0] for w in entity.split("_")) or "t"
        cand, i = base, 1
        while cand in _alias.values():
            i += 1
            cand = f"{base}{i}"
        _alias[entity] = cand
    return _alias[entity]


def build_query(refs):
    """refs = ['fact_ai_usage.Total AI Cost', 'dim_platform.platform_name', ...]"""
    froms, selects, seen = [], [], set()
    for ref in refs:
        entity, prop = ref.split(".", 1)
        al = _a(entity)
        if entity not in seen:
            seen.add(entity)
            froms.append({"Name": al, "Entity": entity, "Type": 0})
        src = {"Expression": {"SourceRef": {"Source": al}}, "Property": prop}
        kind = "Measure" if (entity == F and prop in MEASURES) else "Column"
        selects.append({kind: src, "Name": ref})
    return {"Version": 2, "From": froms, "Select": selects}


def visual(vtype, x, y, w, h, roles, title=None):
    """roles = {'Values': [refs]} / {'Category': [refs], 'Y': [refs]}"""
    all_refs = [r for lst in roles.values() for r in lst]
    cfg = {
        "name": f"v{abs(hash((vtype, x, y, title))) % 10**8:08d}",
        "layouts": [{"id": 0, "position": {"x": x, "y": y, "z": 0,
                                           "width": w, "height": h}}],
        "singleVisual": {
            "visualType": vtype,
            "projections": {role: [{"queryRef": r} for r in refs]
                            for role, refs in roles.items()},
            "prototypeQuery": build_query(all_refs),
            "drillFilterOtherVisuals": True,
            "objects": {},
        },
    }
    if title:
        cfg["singleVisual"]["vcObjects"] = {"title": [{"properties": {
            "text": {"expr": {"Literal": {"Value": "'" + title.replace("'", "''") + "'"}}},
            "show": {"expr": {"Literal": {"Value": "true"}}}}}]}
    return cfg


def card(x, y, w, h, measure, title):
    return visual("card", x, y, w, h, {"Values": [f"{F}.{measure}"]}, title)


def chart(vtype, x, y, w, h, cat, vals, title):
    return visual(vtype, x, y, w, h, {"Category": [cat], "Y": vals}, title)


def tbl(x, y, w, h, refs, title):
    return visual("tableEx", x, y, w, h, {"Values": refs}, title)


pages = [
    ("ReportSection1", "1 · FOCUS & FinOps Executive Overview", [
        card(16, 16, 200, 120, "Total AI Cost", "FOCUS Effective Cost"),
        card(224, 16, 200, 120, "FOCUS List Cost", "FOCUS Retail List Cost"),
        card(432, 16, 200, 120, "Total FOCUS Savings", "Total FOCUS Savings"),
        card(640, 16, 200, 120, "Effective Savings Rate (ESR %)", "FinOps ESR %"),
        card(848, 16, 200, 120, "Cost Confidence %", "Billed vs Modelled %"),
        card(1056, 16, 208, 120, "Total Actionable Waste", "Actionable Waste"),
        chart("clusteredColumnChart", 16, 152, 624, 280,
              "dim_platform.platform_name",
              [f"{F}.FOCUS List Cost", f"{F}.FOCUS Contracted Cost", f"{F}.Total AI Cost"],
              "FOCUS Price Waterfall by Platform: List -> Contracted -> Effective"),
        chart("lineChart", 656, 152, 608, 280,
              "dim_date.date_key",
              [f"{F}.Total AI Cost", f"{F}.Billed Cost", f"{F}.Modelled Cost"],
              "Daily AI Spend Trend: FOCUS Billed vs Rate-Card Modelled"),
        tbl(16, 448, 1248, 250, [
            "dim_platform.platform_name", "dim_platform.billing_model",
            "dim_platform.data_source", f"{F}.FOCUS List Cost", f"{F}.Total AI Cost",
            f"{F}.Total FOCUS Savings", f"{F}.Effective Savings Rate (ESR %)",
            f"{F}.Cost Confidence %"],
            "Platform FinOps & FOCUS Capability Matrix — REAL vs FOCUS vs Rate-Card Provenance"),
    ]),
    ("ReportSection2", "2 · Foundry, APIM & Azure AI Tokenomics", [
        card(16, 16, 200, 120, "Total Tokens", "Total LLM Tokens"),
        card(224, 16, 200, 120, "Cache Hit Rate", "Prompt Cache Hit %"),
        card(432, 16, 200, 120, "Cost per 1K Tokens", "Effective $ / 1K Tokens"),
        card(640, 16, 200, 120, "PTU Commitment Cost", "PTU Reservation Cost"),
        card(848, 16, 200, 120, "Commitment Utilization %", "PTU & P3 Utilization %"),
        card(1056, 16, 208, 120, "Gateway Governance %", "APIM Gateway Routed %"),
        chart("clusteredBarChart", 16, 152, 624, 280,
              "dim_model.model_name", [f"{F}.Total AI Cost"],
              "Spend by Model & AI Engine (OpenAI, MaaS, DocIntel, Speech, AML, AI Search)"),
        chart("clusteredBarChart", 656, 152, 608, 280,
              f"{F}.service_name", [f"{F}.Total AI Cost"],
              "All Azure AI Services & Supporting Infrastructure TCO (FOCUS ServiceName)"),
        tbl(16, 448, 1248, 250, [
            "dim_model.model_name", "dim_model.provider", "dim_model.modality",
            f"{F}.Input Tokens", f"{F}.Output Tokens", f"{F}.Cached Tokens",
            f"{F}.Cache Hit Rate", f"{F}.Cost per 1K Tokens", f"{F}.Total AI Cost"],
            "Per-Model & Azure AI Service Unit Economics"),
    ]),
    ("ReportSection3", "3 · Waste, PTU & Seat Optimization", [
        card(16, 16, 200, 120, "Total Actionable Waste", "Total Actionable Waste"),
        card(224, 16, 200, 120, "Idle Seat Waste (monthly)", "Idle Seat Waste / mo"),
        card(432, 16, 200, 120, "PTU Unused Waste", "Unused PTU Capacity $"),
        card(640, 16, 200, 120, "Bypassed Gateway Spend", "Bypassed APIM Gateway $"),
        card(848, 16, 200, 120, "Idle Licensed Users", "Idle Copilot Seats (28d)"),
        card(1056, 16, 208, 120, "Cost per Active User", "Cost / Active Principal"),
        chart("clusteredColumnChart", 16, 152, 624, 260,
              f"{F}.pricing_category", [f"{F}.Total AI Cost"],
              "AI Spend by FOCUS Pricing Category (Standard, Committed PTU/P3, Seat, CreditPack)"),
        chart("clusteredBarChart", 656, 152, 608, 260,
              f"{F}.commitment_discount_status", [f"{F}.Total AI Cost"],
              "Commitment Discount Utilization (Used vs Unused PTU & P3 Reservations)"),
        tbl(16, 428, 1248, 270, [
            "dim_identity.display_name", "dim_identity.team", "dim_identity.business_unit",
            f"{F}.Licensed Seats", f"{F}.M365 Prompts", f"{F}.Premium Requests",
            f"{F}.Total Requests", f"{F}.Total AI Cost"],
            "Per-Principal Seat & Consumption Audit — Paid Seat with Zero Activity = Reclaim Candidate"),
    ]),
    ("ReportSection4", "4 · Rate Card & FOCUS Discounts", [
        card(16, 16, 300, 110, "FOCUS List Cost", "FOCUS Retail List Cost"),
        card(332, 16, 300, 110, "FOCUS Contracted Cost", "FOCUS Contracted Cost"),
        card(648, 16, 300, 110, "Negotiated Discount Savings", "EA/MCA Negotiated Savings"),
        card(964, 16, 300, 110, "Commitment Discount Savings", "PTU & P3 Reservation Savings"),
        tbl(16, 142, 1248, 310, [
            "dim_rate_card.platform", "dim_rate_card.unit_type", "dim_rate_card.model",
            "dim_rate_card.unit_price_usd", "dim_rate_card.source", "dim_rate_card.note"],
            "dim_rate_card — Customer Rate Card for Seats, Credit Packs & Effective Token Rates"),
        chart("clusteredBarChart", 16, 468, 1248, 230,
              "dim_platform.platform_name",
              [f"{F}.Billed Cost", f"{F}.Modelled Cost"],
              "FOCUS Billed Cost vs Rate-Card Modelled Cost by Platform"),
    ]),
]

layout = {
    "id": 0,
    "resourcePackages": [],
    "config": json.dumps({
        "version": "5.43",
        "themeCollection": {"baseTheme": {"name": "CY24SU06"}},
        "activeSectionIndex": 0,
        "settings": {"useStylableVisualContainerHeader": True},
    }),
    "layoutOptimization": 0,
    "sections": [{
        "id": i, "name": nm, "displayName": dn, "ordinal": i,
        "width": W, "height": H, "displayOption": 1,
        "config": json.dumps({}), "filters": "[]",
        "visualContainers": [{
            "x": v["layouts"][0]["position"]["x"],
            "y": v["layouts"][0]["position"]["y"],
            "z": 0,
            "width": v["layouts"][0]["position"]["width"],
            "height": v["layouts"][0]["position"]["height"],
            "config": json.dumps(v),
            "filters": "[]",
        } for v in vis],
    } for i, (nm, dn, vis) in enumerate(pages)],
    "filters": "[]",
    "pods": [],
}

(RP / "report.json").write_text(json.dumps(layout, indent=2), encoding="utf-8")
n = sum(len(v) for _, _, v in pages)
print(f"  report.json base — {len(pages)} pages, {n} visuals, all with bound queries")

# Always append/refresh the persona, Agent 365, User Allocation & Spectrum pages
import build_personas
build_personas.main()

