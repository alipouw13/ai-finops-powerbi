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
    # New measures introduced by CHANGE-SPEC.md; added to the semantic model in
    # parallel by WS-MODEL, so bind by exact spec name here.
    "Cowork Add-on Cost", "Rate Card Cost", "Rate Card Coverage %", "Seat Action",
    "Workload Attribution %",
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
    ("ReportSection1", "1 - Spend Overview", [
        card(16, 16, 240, 120, "Total AI Cost", "Total AI Spend"),
        card(272, 16, 240, 120, "Fixed Cost", "Fixed (licences)"),
        card(528, 16, 240, 120, "Variable Cost", "Variable (consumption)"),
        card(784, 16, 240, 120, "Cost Confidence %", "Billed vs Modelled"),
        card(1040, 16, 224, 120, "Cost (30d run-rate)", "30-day run-rate"),
        chart("clusteredColumnChart", 16, 152, 624, 280,
              "dim_platform.platform_name", [f"{F}.Total AI Cost"], "Spend by platform"),
        chart("lineChart", 656, 152, 608, 280,
              "dim_date.date_key", [f"{F}.Total AI Cost"], "Daily spend trend"),
        tbl(16, 448, 1248, 250, [
            "dim_platform.platform_name", "dim_platform.native_unit",
            "dim_platform.addon_unit", f"{F}.Total AI Cost", f"{F}.Cowork Add-on Cost"],
            "Platform capability matrix — Cowork add-on billed on top of the seat"),
    ]),
    ("ReportSection2", "2 - Engineering Tokenomics", [
        card(16, 16, 240, 120, "Total Tokens", "Total Tokens"),
        card(272, 16, 240, 120, "Input Tokens", "Input Tokens"),
        card(528, 16, 240, 120, "Output Tokens", "Output Tokens"),
        card(784, 16, 240, 120, "Cache Hit Rate", "Cache Hit Rate"),
        card(1040, 16, 224, 120, "Cost per 1K Tokens", "$ / 1K Tokens"),
        chart("clusteredColumnChart", 16, 152, 624, 280,
              "dim_model.model_name", [f"{F}.Input Tokens", f"{F}.Output Tokens"],
              "Input vs output tokens by model"),
        chart("lineChart", 656, 152, 608, 280,
              "dim_date.date_key", [f"{F}.Total Tokens"], "Token volume over time"),
        tbl(16, 448, 1248, 250, [
            "dim_model.model_name", "dim_model.provider", f"{F}.Total Tokens",
            f"{F}.Total Requests", f"{F}.Avg Latency (ms)", f"{F}.Cost per 1K Tokens"],
            "Token economics by model — Foundry vs Azure OpenAI"),
    ]),
    ("ReportSection3", "3 - Licence Seats, Waste & Utilisation", [
        card(16, 16, 240, 120, "Idle Licensed Users", "Idle Licensed Seats"),
        card(272, 16, 240, 120, "Idle Seat Waste (monthly)", "Recoverable / month"),
        card(528, 16, 240, 120, "Licensed Seats", "Licensed Seats"),
        card(784, 16, 240, 120, "Total Requests", "Total Requests"),
        card(1040, 16, 224, 120, "Fixed Cost", "Fixed (licence) Cost"),
        chart("barChart", 16, 152, 624, 280,
              "dim_identity.display_name", [f"{F}.Total Requests"],
              "Requests by licensed user"),
        tbl(656, 152, 608, 280, [
            "dim_identity.display_name", "dim_identity.team",
            f"{F}.Total Requests", f"{F}.Fixed Cost", f"{F}.Seat Action"],
            "Seat action queue — reclaim, review or keep"),
        chart("clusteredColumnChart", 16, 448, 1248, 250,
              "dim_platform.platform_name", [f"{F}.Fixed Cost", f"{F}.Licensed Seats"],
              "Fixed cost & seats by platform"),
    ]),
    ("ReportSection4", "4 - Rate Card", [
        tbl(16, 16, 1248, 420, [
            "dim_rate_card.platform", "dim_rate_card.unit_type", "dim_rate_card.model",
            "dim_rate_card.unit_price_usd", "dim_rate_card.source", "dim_rate_card.note"],
            "dim_rate_card — the single customer-supplied input"),
        chart("clusteredBarChart", 16, 452, 1248, 246,
              "dim_platform.platform_name",
              [f"{F}.Billed Cost", f"{F}.Rate Card Cost"],
              "Billed vs rate card by platform — every platform has both bars"),
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
print(f"  report.json — {len(pages)} pages, {n} visuals, all with bound queries")
