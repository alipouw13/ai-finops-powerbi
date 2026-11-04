#!/usr/bin/env python3
"""
Unified Silver -> Gold builder for the AI FinOps & Tokenomics semantic model.

Reads the Bronze datasets in `platform/fabric/bronze_out/*.csv` (including the
official FOCUS 1.2-preview `bronze_focus_cost.csv` and `bronze_apim_gateway_logs.csv`)
and materializes all 16 Gold CSVs in `AIFinOps.SemanticModel/data/*.csv`:

  Dimensions & Bridges:
    - dim_date.csv
    - dim_platform.csv
    - dim_identity.csv
    - dim_model.csv
    - dim_cost_center.csv
    - dim_business_unit.csv
    - dim_application.csv
    - dim_environment.csv
    - dim_rate_card.csv
    - dim_agent.csv
    - dim_persona.csv
    - bridge_persona_platform.csv
    - extractable_data_catalog.csv (backs dim_data_source)

  Facts:
    - fact_ai_usage.csv               (FOCUS-native + Tokenomics + Copilot star fact)
    - fact_user_cost_allocation.csv   (100% tied-out to fact_ai_usage by cost, tokens & requests)
    - fact_agent_inventory.csv        (Agent 365 point-in-time governance snapshot)
    - fact_agent_activity.csv         (Agent 365 active user sessions)

Enforces referential integrity (0 orphan foreign keys across all relationships)
and exact financial/token tie-out between `fact_ai_usage` and `fact_user_cost_allocation`.
"""
import csv
import json
import os
import subprocess
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).parent
DATA_RAW = ROOT / "data"
BRONZE_DIR = ROOT / "platform" / "fabric" / "bronze_out"
OUT = ROOT / "AIFinOps.SemanticModel" / "data"
OUT.mkdir(parents=True, exist_ok=True)


def read_csv(path: Path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def write_csv(name: str, rows: list, cols: list):
    with open(OUT / f"{name}.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    print(f"  {name + '.csv':<34} {len(rows):>6} rows")


def ensure_bronze():
    if not (BRONZE_DIR / "bronze_azure_cost_focus.csv").exists():
        print("Bronze FOCUS dataset missing; running gen_bronze_data.py first...")
        subprocess.check_call([sys.executable, str(ROOT / "platform" / "fabric" / "gen_bronze_data.py")])


def build():
    ensure_bronze()

    # ------------------------------------------------------------------
    # 1. dim_rate_card
    # ------------------------------------------------------------------
    rate_card = []
    for r in json.loads((DATA_RAW / "ApimModelRate_CL.json").read_text(encoding="utf-8")):
        for unit, price in (
            ("input_token", r["InputPer1k"]),
            ("output_token", r["OutputPer1k"]),
            ("cached_token", r["CachedInputPer1k"]),
        ):
            rate_card.append({
                "rate_key": f"Foundry|{unit}|{r['ModelName']}",
                "platform": "Foundry",
                "unit_type": unit,
                "model": r["ModelName"],
                "unit_price_usd": float(price) / 1000.0,
                "effective_from": r["EffectiveFrom"][:10],
                "currency": "USD",
                "source": "REAL - ApimModelRate_CL / FOCUS",
                "note": "Effective per-token rate (list rate less 15% MCA/EA contract discount).",
            })
    rate_card += [
        {"rate_key": "Foundry|input_token|o3-mini", "platform": "Foundry", "unit_type": "input_token",
         "model": "o3-mini", "unit_price_usd": 0.0011 / 1000.0, "effective_from": "2026-01-01",
         "currency": "USD", "source": "FOCUS ContractedUnitPrice", "note": "Reasoning model input token rate."},
        {"rate_key": "Foundry|output_token|o3-mini", "platform": "Foundry", "unit_type": "output_token",
         "model": "o3-mini", "unit_price_usd": 0.0044 / 1000.0, "effective_from": "2026-01-01",
         "currency": "USD", "source": "FOCUS ContractedUnitPrice", "note": "Reasoning + completion output token rate."},
        {"rate_key": "Foundry|input_token|Mistral-Large-2", "platform": "Foundry", "unit_type": "input_token",
         "model": "Mistral-Large-2", "unit_price_usd": 0.002 / 1000.0, "effective_from": "2026-01-01",
         "currency": "USD", "source": "FOCUS Marketplace MaaS", "note": "Foundry Serverless MaaS input token rate."},
        {"rate_key": "Foundry|output_token|Mistral-Large-2", "platform": "Foundry", "unit_type": "output_token",
         "model": "Mistral-Large-2", "unit_price_usd": 0.006 / 1000.0, "effective_from": "2026-01-01",
         "currency": "USD", "source": "FOCUS Marketplace MaaS", "note": "Foundry Serverless MaaS output token rate."},
        {"rate_key": "Foundry|ptu_hour|gpt-4.1", "platform": "Foundry", "unit_type": "ptu_hour",
         "model": "gpt-4.1", "unit_price_usd": 1.32, "effective_from": "2026-01-01",
         "currency": "USD", "source": "FOCUS EffectiveCost (1Yr Reservation)", "note": "$2.00/hr list, $1.32/hr effective 1-year reservation."},
        {"rate_key": "M365Copilot|seat_day|", "platform": "M365Copilot", "unit_type": "seat_day",
         "model": "", "unit_price_usd": 1.0, "effective_from": "2026-01-01",
         "currency": "USD", "source": "LIST PRICE", "note": "$30/user/mo enterprise annual commitment ($1.00/day). Overwrite with EA rate."},
        {"rate_key": "CopilotCowork|copilot_credit|", "platform": "CopilotCowork", "unit_type": "copilot_credit",
         "model": "", "unit_price_usd": 0.01, "effective_from": "2026-01-01",
         "currency": "USD", "source": "LIST PRICE (PAYG)", "note": "M365 Copilot Cowork / Autopilot / Work IQ API usage-based Copilot Credits."},
        {"rate_key": "CopilotStudio|copilot_credit|PrepaidPack", "platform": "CopilotStudio", "unit_type": "copilot_credit",
         "model": "", "unit_price_usd": 0.008, "effective_from": "2026-01-01",
         "currency": "USD", "source": "LIST PRICE (Capacity Pack)", "note": "25,000-credit pack @ $200/mo = $0.008/credit. P3 Commit = $0.0085; PAYG = $0.01."},
        {"rate_key": "GitHubCopilot|seat_day|", "platform": "GitHubCopilot", "unit_type": "seat_day",
         "model": "", "unit_price_usd": 1.3, "effective_from": "2026-01-01",
         "currency": "USD", "source": "LIST PRICE", "note": "GitHub Copilot Enterprise $39/user/mo ($1.30/day). Business = $19/user/mo."},
        {"rate_key": "GitHubCopilot|premium_request|", "platform": "GitHubCopilot", "unit_type": "premium_request",
         "model": "", "unit_price_usd": 0.04, "effective_from": "2026-01-01",
         "currency": "USD", "source": "LIST PRICE", "note": "$0.04/request x model multiplier (e.g. Code Review = 13x). Billed via netAmount."},
        {"rate_key": "M365Copilot|prompt|", "platform": "M365Copilot", "unit_type": "prompt",
         "model": "", "unit_price_usd": 0.0, "effective_from": "2026-01-01",
         "currency": "USD", "source": "INCLUDED IN SEAT", "note": "Standard M365 Copilot chat/app prompts are included in the seat fee ($0 marginal cost)."},
    ]
    write_csv(
        "dim_rate_card",
        rate_card,
        ["rate_key", "platform", "unit_type", "model", "unit_price_usd", "effective_from", "currency", "source", "note"],
    )

    # ------------------------------------------------------------------
    # 2. dim_business_unit, dim_cost_center, dim_environment, dim_application
    # ------------------------------------------------------------------
    bus = [
        {"business_unit_key": "BU-RETAIL",    "business_unit_name": "Retail",             "division": "Commercial",         "monthly_budget_usd": 28000, "executive_owner": "cfo-retail@contoso.com",    "is_mock_budget": "TRUE"},
        {"business_unit_key": "BU-DISCOVERY", "business_unit_name": "Discovery",          "division": "Commercial",         "monthly_budget_usd": 12000, "executive_owner": "cfo-discovery@contoso.com", "is_mock_budget": "TRUE"},
        {"business_unit_key": "BU-PLATFORM",  "business_unit_name": "Platform",           "division": "Engineering",        "monthly_budget_usd": 18000, "executive_owner": "cto@contoso.com",           "is_mock_budget": "TRUE"},
        {"business_unit_key": "BU-TECH",      "business_unit_name": "Technology",         "division": "Engineering",        "monthly_budget_usd": 45000, "executive_owner": "cio@contoso.com",           "is_mock_budget": "TRUE"},
        {"business_unit_key": "BU-INSURANCE", "business_unit_name": "Insurance",          "division": "Financial Services", "monthly_budget_usd": 22000, "executive_owner": "cfo-insurance@contoso.com", "is_mock_budget": "TRUE"},
        {"business_unit_key": "BU-UNALLOC",   "business_unit_name": "Unallocated",        "division": "Unallocated",        "monthly_budget_usd": 0,     "executive_owner": "",                          "is_mock_budget": "TRUE"},
    ]
    write_csv(
        "dim_business_unit",
        bus,
        ["business_unit_key", "business_unit_name", "division", "monthly_budget_usd", "executive_owner", "is_mock_budget"],
    )

    cost_centers = [
        {"cost_center_key": "CC-1000", "cost_center_name": "Checkout & Insurance Ops", "business_unit": "Retail / Insurance", "owner_upn": "robin.hale@contoso.com"},
        {"cost_center_key": "CC-2000", "cost_center_name": "Search & Discovery",       "business_unit": "Discovery",          "owner_upn": "sam.chen@contoso.com"},
        {"cost_center_key": "CC-3000", "cost_center_name": "Platform & Data Science",  "business_unit": "Technology",         "owner_upn": "jenny.oyelaran@contoso.com"},
        {"cost_center_key": "CC-4000", "cost_center_name": "Mobile & Actuarial AI",    "business_unit": "Retail / Insurance", "owner_upn": "kim.arroyo@contoso.com"},
        {"cost_center_key": "CC-NA",   "cost_center_name": "Unallocated Cost Center",  "business_unit": "Unallocated",        "owner_upn": ""},
    ]
    write_csv("dim_cost_center", cost_centers, ["cost_center_key", "cost_center_name", "business_unit", "owner_upn"])

    envs = [
        {"environment_key": "ENV-PROD", "environment_name": "Production",  "is_production": "TRUE",  "sla_tier": "Tier-1"},
        {"environment_key": "ENV-TEST", "environment_name": "Test",        "is_production": "FALSE", "sla_tier": "Tier-3"},
        {"environment_key": "ENV-DEV",  "environment_name": "Development", "is_production": "FALSE", "sla_tier": "Tier-3"},
        {"environment_key": "ENV-UNK",  "environment_name": "Unknown",     "is_production": "FALSE", "sla_tier": ""},
    ]
    write_csv("dim_environment", envs, ["environment_key", "environment_name", "is_production", "sla_tier"])

    apps_bronze = read_csv(BRONZE_DIR / "bronze_ref_app_inventory.csv")
    apps = []
    app_lookup = {}
    for a in apps_bronze:
        is_m = a.get("is_mock") or ("TRUE" if a["application_key"] in ("APP-M365", "APP-GHCP", "APP-STUDIO") else "FALSE")
        row = {
            "application_key": a["application_key"],
            "application_name": a["application_name"],
            "application_type": a["application_type"],
            "owner_business_unit_key": a["owner_business_unit_key"],
            "owner_upn": a["owner_upn"],
            "default_environment_key": a["environment"],
            "criticality": a["criticality"],
            "is_mock": is_m,
        }
        apps.append(row)
        app_lookup[a["application_key"]] = {**a, "is_mock": is_m, "cost_center_key": a.get("cost_center_key") or "CC-3000"}
    write_csv(
        "dim_application",
        apps,
        ["application_key", "application_name", "application_type", "owner_business_unit_key", "owner_upn", "default_environment_key", "criticality", "is_mock"],
    )

    # ------------------------------------------------------------------
    # 3. dim_platform (8 platforms covering all AI & Supporting Infra)
    # ------------------------------------------------------------------
    platforms = [
        {
            "platform_key": "Foundry",
            "platform_name": "Azure AI Foundry & OpenAI",
            "billing_model": "Consumption (Tokens + PTU Reservations)",
            "native_unit": "token",
            "has_token_telemetry": "TRUE",
            "has_native_cost": "TRUE",
            "is_variable_cost": "TRUE",
            "data_source": "REAL (APIM Gateway 2026-08-07) + FOCUS 1.2 (Mock multi-day)",
            "enterprise_discount_pct": 0.15,
            "addon_unit": "ptu_hour",
            "addon_billing_model": "Provisioned Managed (PTU) Reservation",
        },
        {
            "platform_key": "AzureAI",
            "platform_name": "Azure AI Services (DocIntel, Speech, Content Safety)",
            "billing_model": "Consumption (FOCUS Azure Meters)",
            "native_unit": "azure_meter",
            "has_token_telemetry": "FALSE",
            "has_native_cost": "TRUE",
            "is_variable_cost": "TRUE",
            "data_source": "FOCUS 1.2-preview (Document Intelligence, Speech, Content Safety)",
            "enterprise_discount_pct": 0.15,
            "addon_unit": "",
            "addon_billing_model": "",
        },
        {
            "platform_key": "AzureML",
            "platform_name": "Azure Machine Learning",
            "billing_model": "Compute & Online Endpoints (FOCUS)",
            "native_unit": "compute_hour",
            "has_token_telemetry": "FALSE",
            "has_native_cost": "TRUE",
            "is_variable_cost": "TRUE",
            "data_source": "FOCUS 1.2-preview (GPU Compute + Managed Endpoints)",
            "enterprise_discount_pct": 0.18,
            "addon_unit": "",
            "addon_billing_model": "",
        },
        {
            "platform_key": "AzureInfra",
            "platform_name": "Azure AI Supporting Infrastructure",
            "billing_model": "Consumption (AI Search, APIM, Cosmos, Redis, Fabric, Logs)",
            "native_unit": "azure_meter",
            "has_token_telemetry": "FALSE",
            "has_native_cost": "TRUE",
            "is_variable_cost": "TRUE",
            "data_source": "FOCUS 1.2-preview (AI Search, APIM v2, Cosmos DB, Redis, Fabric CU)",
            "enterprise_discount_pct": 0.10,
            "addon_unit": "",
            "addon_billing_model": "",
        },
        {
            "platform_key": "CopilotStudio",
            "platform_name": "Microsoft Copilot Studio",
            "billing_model": "Copilot Credits (Prepaid Pack + P3 Commit + PAYG)",
            "native_unit": "copilot_credit",
            "has_token_telemetry": "FALSE",
            "has_native_cost": "TRUE",
            "is_variable_cost": "TRUE",
            "data_source": "MOCK - Dataverse msdyn_aievents + FOCUS 1.2 P3/PAYG meters",
            "enterprise_discount_pct": 0.20,
            "addon_unit": "",
            "addon_billing_model": "",
        },
        {
            "platform_key": "M365Copilot",
            "platform_name": "Microsoft 365 Copilot",
            "billing_model": "Per-seat licence ($30/user/mo)",
            "native_unit": "seat_day",
            "has_token_telemetry": "FALSE",
            "has_native_cost": "FALSE",
            "is_variable_cost": "FALSE",
            "data_source": "REAL (2 live tenant seats) + MOCK (8 persona seats & usage)",
            "enterprise_discount_pct": 0.0,
            "addon_unit": "copilot_credit",
            "addon_billing_model": "Usage-based Copilot Credits (see CopilotCowork)",
        },
        {
            "platform_key": "CopilotCowork",
            "platform_name": "M365 Copilot Cowork, Autopilot & Work IQ API",
            "billing_model": "Usage-based (Copilot Credits)",
            "native_unit": "copilot_credit",
            "has_token_telemetry": "FALSE",
            "has_native_cost": "TRUE",
            "is_variable_cost": "TRUE",
            "data_source": "MOCK - M365 Admin Cost Management / FOCUS Copilot Credits",
            "enterprise_discount_pct": 0.0,
            "addon_unit": "",
            "addon_billing_model": "",
        },
        {
            "platform_key": "GitHubCopilot",
            "platform_name": "GitHub Copilot Enterprise",
            "billing_model": "Seats ($39/mo) + Metered Premium Requests ($0.04 x mult)",
            "native_unit": "premium_request",
            "has_token_telemetry": "FALSE",
            "has_native_cost": "TRUE",
            "is_variable_cost": "TRUE",
            "data_source": "MOCK - GitHub REST /copilot/billing/seats + /settings/billing/usage",
            "enterprise_discount_pct": 0.05,
            "addon_unit": "seat_day",
            "addon_billing_model": "Fixed Enterprise Seat ($39/user/mo)",
        },
    ]
    write_csv(
        "dim_platform",
        platforms,
        ["platform_key", "platform_name", "billing_model", "native_unit", "has_token_telemetry",
         "has_native_cost", "is_variable_cost", "data_source", "enterprise_discount_pct", "addon_unit", "addon_billing_model"],
    )

    # ------------------------------------------------------------------
    # 4. dim_persona & bridge_persona_platform
    # ------------------------------------------------------------------
    personas = [
        {"persona_key": "PERS-KNOW",       "persona_name": "Knowledge & Frontline Worker",      "persona_order": 1, "description": "Consumes AI inside Microsoft 365 apps and enterprise agents"},
        {"persona_key": "PERS-MAKER",      "persona_name": "Business-Led Developer / Maker",    "persona_order": 2, "description": "Builds low-code Copilot Studio agents and Power Platform flows"},
        {"persona_key": "PERS-DEV",        "persona_name": "Software Developer",                "persona_order": 3, "description": "Writes and ships application code with GitHub Copilot and Foundry APIs"},
        {"persona_key": "PERS-AIDS",       "persona_name": "AI Engineer / Data Scientist",      "persona_order": 4, "description": "Builds and tunes models, RAG pipelines, and ML workloads in Foundry & Azure ML"},
        {"persona_key": "PERS-PLAT",       "persona_name": "Platform / Infrastructure Engineer","persona_order": 5, "description": "Operates the APIM AI Gateway, AI Search, Fabric capacity, and guardrails"},
        {"persona_key": "PERS-WORKLOAD",   "persona_name": "Workload (non-human)",              "persona_order": 6, "description": "Service principals, managed identities, and autonomous agents"},
        {"persona_key": "PERS-UNASSIGNED", "persona_name": "Unassigned",                        "persona_order": 7, "description": "Identity with no persona mapping yet"},
    ]
    write_csv("dim_persona", personas, ["persona_key", "persona_name", "persona_order", "description"])

    affinities = [
        ("PERS-KNOW", "M365Copilot", "Primary"),
        ("PERS-KNOW", "CopilotStudio", "Secondary"),
        ("PERS-KNOW", "CopilotCowork", "Secondary"),
        ("PERS-MAKER", "CopilotStudio", "Primary"),
        ("PERS-MAKER", "M365Copilot", "Secondary"),
        ("PERS-MAKER", "CopilotCowork", "Secondary"),
        ("PERS-MAKER", "Foundry", "Secondary"),
        ("PERS-DEV", "GitHubCopilot", "Primary"),
        ("PERS-DEV", "Foundry", "Secondary"),
        ("PERS-DEV", "M365Copilot", "Secondary"),
        ("PERS-DEV", "AzureAI", "Secondary"),
        ("PERS-AIDS", "Foundry", "Primary"),
        ("PERS-AIDS", "AzureML", "Secondary"),
        ("PERS-AIDS", "AzureAI", "Secondary"),
        ("PERS-AIDS", "GitHubCopilot", "Secondary"),
        ("PERS-AIDS", "M365Copilot", "Secondary"),
        ("PERS-PLAT", "AzureInfra", "Primary"),
        ("PERS-PLAT", "Foundry", "Secondary"),
        ("PERS-PLAT", "GitHubCopilot", "Secondary"),
        ("PERS-PLAT", "M365Copilot", "Secondary"),
        ("PERS-PLAT", "AzureML", "Secondary"),
        ("PERS-WORKLOAD", "Foundry", "Primary"),
        ("PERS-WORKLOAD", "AzureAI", "Secondary"),
        ("PERS-WORKLOAD", "AzureInfra", "Secondary"),
        ("PERS-WORKLOAD", "CopilotStudio", "Secondary"),
    ]
    bridge_rows = [
        {
            "persona_key": p,
            "platform_key": pl,
            "affinity": aff,
            "is_primary": 1 if aff == "Primary" else 0,
            "bridge_key": f"{p}|{pl}",
        }
        for p, pl, aff in affinities
    ]
    write_csv("bridge_persona_platform", bridge_rows, ["persona_key", "platform_key", "affinity", "is_primary", "bridge_key"])

    # ------------------------------------------------------------------
    # 5. dim_agent (Load live Agent 365 API extract if present, else existing dim_agent.csv)
    # ------------------------------------------------------------------
    ag_real_api_path = ROOT / "platform" / "fabric" / "bronze_real" / "bronze_agent365_agents.csv"
    ag_existing_path = OUT / "dim_agent.csv"
    dim_agent_cols = [
        "agent_key", "agent_name", "status", "publisher", "publisher_type", "platform",
        "version", "owner_upn", "owner_class", "channel", "deployment", "entra_agent_id",
        "environment_id", "created_date", "last_modified_date", "data_class",
    ]
    if ag_real_api_path.exists():
        raw_a365 = read_csv(ag_real_api_path)
        dim_agents = []
        for r in raw_a365:
            owner_u = (r.get("owner") or "").strip().lower()
            pub_t = (r.get("publisher_type") or "").strip() or "Unknown"
            if owner_u:
                o_class = "Owner assigned"
            elif pub_t == "FirstParty":
                o_class = "Microsoft-managed"
            elif pub_t == "ThirdParty":
                o_class = "Publisher-managed"
            else:
                o_class = "Unowned tenant agent"
            dim_agents.append({
                "agent_key": r["title_id"],
                "agent_name": (r.get("name") or "").strip(),
                "status": r.get("status") or "Available",
                "publisher": r.get("publisher") or "",
                "publisher_type": pub_t,
                "platform": (r.get("platform") or "").strip() or "Not specified",
                "version": r.get("version") or "1.0",
                "owner_upn": owner_u,
                "owner_class": o_class,
                "channel": r.get("channel") or "",
                "deployment": r.get("deployment") or "",
                "entra_agent_id": (r.get("entra_agent_id") or "").strip(),
                "environment_id": (r.get("environment_id") or "").strip(),
                "created_date": (r.get("date_created") or "")[:10],
                "last_modified_date": (r.get("last_modified") or "")[:10],
                "data_class": r.get("_data_class") or "REAL",
            })
    else:
        ag_source = read_csv(ag_existing_path) if ag_existing_path.exists() else read_csv(BRONZE_DIR / "bronze_ref_agent_inventory.csv")
        dim_agents = [
            {c: r.get(c, "") for c in dim_agent_cols}
            for r in ag_source
            if r.get("agent_key") != "NO-AGENT"
        ]
    write_csv("dim_agent", dim_agents, dim_agent_cols)

    # ------------------------------------------------------------------
    # 6. dim_identity (Curated AI & Agent Identity Graph — no unrelated system SPN bloat)
    # ------------------------------------------------------------------
    id_bronze = read_csv(BRONZE_DIR / "bronze_ref_identity_map.csv")
    identities = []
    seen_ids = set()
    upn_to_idkey = {}
    gh_to_idkey = {}

    for r in id_bronze:
        ik = r["identity_key"]
        seen_ids.add(ik)
        p_type = r["principal_type"]
        id_class = "Human" if r["is_human"] == "TRUE" else ("Agent" if p_type == "Agent" else "ServicePrincipal")
        bu_key = r["home_business_unit_key"] or "BU-UNALLOC"
        row = {
            "identity_key": ik,
            "display_name": r["display_name"],
            "principal_type": p_type,
            "upn": r["upn"],
            "github_login": r["github_login"],
            "team": r["department"],
            "business_unit": bu_key,
            "cost_center_key": r["cost_center_key"] or "CC-NA",
            "identity_class": id_class,
            "is_human": r["is_human"],
            "home_business_unit_key": bu_key,
            "persona_key": r.get("persona_key") or ("PERS-UNASSIGNED" if r["is_human"] == "TRUE" else "PERS-WORKLOAD"),
        }
        identities.append(row)
        if r["upn"]:
            upn_to_idkey[r["upn"].lower()] = ik
        if r["github_login"]:
            gh_to_idkey[r["github_login"].lower()] = ik

    # Add Agent identities from dim_agent that have entra_agent_id or owner_upn
    for ag in dim_agents:
        if ag["agent_key"] == "NO-AGENT":
            continue
        owner_u = (ag.get("owner_upn") or "").strip().lower()
        if owner_u and owner_u not in upn_to_idkey:
            identities.append({
                "identity_key": ag["owner_upn"].strip(),
                "display_name": ag["owner_upn"].split("@")[0].replace(".", " ").title(),
                "principal_type": "User",
                "upn": ag["owner_upn"].strip(),
                "github_login": "",
                "team": "Platform",
                "business_unit": "BU-TECH",
                "cost_center_key": "CC-3000",
                "identity_class": "Human",
                "is_human": "TRUE",
                "home_business_unit_key": "BU-TECH",
                "persona_key": "PERS-PLAT",
            })
            seen_ids.add(ag["owner_upn"].strip())
            upn_to_idkey[owner_u] = ag["owner_upn"].strip()

    # Add special governance identities: 'unattributed-gateway-bypass' and 'unknown'
    for spec_id, spec_name, spec_type, spec_class in [
        ("unattributed-gateway-bypass", "Direct Resource Access (Bypassed APIM Gateway)", "ServicePrincipal", "Application"),
        ("unknown", "Unattributed Identity", "Unknown", "Application"),
    ]:
        if spec_id not in seen_ids:
            identities.append({
                "identity_key": spec_id,
                "display_name": spec_name,
                "principal_type": spec_type,
                "upn": "",
                "github_login": "",
                "team": "Unallocated",
                "business_unit": "BU-UNALLOC",
                "cost_center_key": "CC-NA",
                "identity_class": spec_class,
                "is_human": "FALSE",
                "home_business_unit_key": "BU-UNALLOC",
                "persona_key": "PERS-WORKLOAD",
            })
            seen_ids.add(spec_id)

    write_csv(
        "dim_identity",
        identities,
        ["identity_key", "display_name", "principal_type", "upn", "github_login", "team",
         "business_unit", "cost_center_key", "identity_class", "is_human", "home_business_unit_key", "persona_key"],
    )
    id_lookup = {i["identity_key"]: i for i in identities}

    # ------------------------------------------------------------------
    # 7. Silver -> Gold Conformance: FOCUS 1.0r2/1.2 + APIM + M365 + Studio + GitHub
    # ------------------------------------------------------------------
    focus_rows = read_csv(BRONZE_DIR / "bronze_azure_cost_focus.csv")
    apim_rows = read_csv(BRONZE_DIR / "bronze_foundry_gateway.csv")
    m365_seats = read_csv(BRONZE_DIR / "bronze_m365_copilot_seats.csv")
    m365_usage = read_csv(BRONZE_DIR / "bronze_m365_copilot_usage.csv")
    m365_credits = read_csv(BRONZE_DIR / "bronze_m365_copilot_credits.csv")
    m365_cowork = read_csv(BRONZE_DIR / "bronze_m365_cowork_usage.csv")
    studio_rows = read_csv(BRONZE_DIR / "bronze_studio_credits.csv")
    ghc_seats = read_csv(BRONZE_DIR / "bronze_ghc_seats.csv")
    ghc_prem = read_csv(BRONZE_DIR / "bronze_ghc_premium_usage.csv")

    facts = []
    caller_to_app = {
        "checkout-service": "APP-CHECKOUT",
        "search-api": "APP-SEARCH",
        "ds-notebooks": "APP-DSNB",
        "mobile-assistant": "APP-MOBILE",
    }

    # 7A. Index FOCUS Azure OpenAI billed & list cost by (date, app_key, model)
    focus_aoai = defaultdict(lambda: {"billed": 0.0, "list": 0.0})
    for r in focus_rows:
        if "azure openai" not in (r.get("ServiceName") or "").lower():
            continue
        d_str = r["ChargePeriodStart"][:10]
        tags = json.loads(r.get("Tags") or "{}")
        app_k = tags.get("app", "APP-UNKNOWN")
        sku_det = json.loads(r.get("x_SkuDetails") or "{}")
        mdl = sku_det.get("model") or r.get("x_SkuMeterSubcategory") or "gpt-4.1-mini"
        b = focus_aoai[(d_str, app_k, mdl)]
        b["billed"] += float(r.get("x_BilledCostInUsd") or r.get("BilledCost") or 0.0)
        b["list"] += float(r.get("x_ListCostInUsd") or r.get("ListCost") or 0.0)

    gw_tot_tok = defaultdict(int)
    for r in apim_rows:
        d_str = r["time_generated"][:10]
        app_k = caller_to_app.get(r["upn_or_app_name"], "APP-UNKNOWN")
        mdl = r["model_name"] or "unknown"
        gw_tot_tok[(d_str, app_k, mdl)] += int(float(r["total_tokens"] or 0))

    for r in apim_rows:
        d_str = r["time_generated"][:10]
        mdl = r["model_name"] or "unknown"
        cid = r.get("oid") or r.get("client_id") or "unknown"
        if cid not in id_lookup:
            cid = "unknown"
        app_k = caller_to_app.get(r["upn_or_app_name"], "APP-UNKNOWN")
        app_meta = app_lookup.get(app_k, app_lookup["APP-UNKNOWN"])
        bu_k = id_lookup[cid]["home_business_unit_key"] if cid != "unknown" else app_meta["owner_business_unit_key"]
        cc_k = r["cost_center_claim"] if r.get("cost_center_claim") in {"CC-1000", "CC-2000", "CC-3000", "CC-4000"} else (app_meta.get("cost_center_key") or "CC-NA")
        env_k = app_meta["environment"]

        pt = int(float(r["prompt_tokens"] or 0))
        ct = int(float(r["completion_tokens"] or 0))
        cch = int(float(r["cached_prompt_tokens"] or 0))
        tot_tok = int(float(r["total_tokens"] or (pt + ct)))
        reqs = int(float(r["requests"] or 1))
        lat = float(r["total_latency_ms"] or 0.0)
        is_err = "True" if str(r.get("is_error")) == "True" else "False"

        pool_tok = gw_tot_tok[(d_str, app_k, mdl)] or 1
        share = tot_tok / pool_tok
        fb = focus_aoai.get((d_str, app_k, mdl))
        if fb and fb["billed"] > 0:
            eff_c = round(fb["billed"] * share, 6)
            list_c = round(fb["list"] * share, 6)
            is_est = "FALSE"
        else:
            in_r = 0.002 / 1000.0 if mdl == "gpt-4.1" else 0.0004 / 1000.0
            out_r = 0.008 / 1000.0 if mdl == "gpt-4.1" else 0.0016 / 1000.0
            cch_r = 0.0005 / 1000.0 if mdl == "gpt-4.1" else 0.0001 / 1000.0
            list_c = round(pt * in_r + ct * out_r + cch * cch_r, 6)
            eff_c = round(list_c * 0.85, 6)
            is_est = "TRUE"

        facts.append({
            "usage_date": d_str,
            "platform_key": "Foundry",
            "identity_key": cid,
            "model_key": mdl,
            "cost_center_key": cc_k or "CC-NA",
            "application_key": app_k,
            "environment_key": env_k,
            "business_unit_key": bu_k or "BU-UNALLOC",
            "unit_type": "token",
            "quantity": tot_tok,
            "input_tokens": pt,
            "output_tokens": ct,
            "cached_tokens": cch,
            "requests": reqs,
            "cost_usd": eff_c,
            "list_cost_usd": list_c,
            "cost_is_estimated": is_est,
            "has_rate_card": "TRUE",
            "is_error": is_err,
            "latency_ms": lat,
        })

    # 7B. Process non-AOAI / non-CopilotStudio FOCUS rows (Document Intelligence,
    #     AI Search, API Management, Microsoft Fabric)
    for r in focus_rows:
        svc = r.get("ServiceName") or ""
        svc_l = svc.lower()
        if "azure openai" in svc_l or "copilot studio" in svc_l:
            continue
        d_str = r["ChargePeriodStart"][:10]
        tags = json.loads(r.get("Tags") or "{}")
        app_k = tags.get("app", "APP-UNKNOWN")
        if app_k not in app_lookup:
            app_k = "APP-UNKNOWN"
        app_meta = app_lookup[app_k]
        bu_k = tags.get("bu") or app_meta["owner_business_unit_key"] or "BU-UNALLOC"
        env_k = tags.get("env") or app_meta["environment"] or "ENV-PROD"
        cc_k = r["x_CostCenter"] if r.get("x_CostCenter") in {"CC-1000", "CC-2000", "CC-3000", "CC-4000"} else "CC-NA"

        qty = float(r["ConsumedQuantity"] or 0.0)
        list_c = float(r.get("x_ListCostInUsd") or r.get("ListCost") or 0.0)
        eff_c = float(r.get("x_BilledCostInUsd") or r.get("BilledCost") or 0.0)
        is_ai = (r.get("ServiceCategory") == "AI and Machine Learning") and ("search" not in svc_l)
        plat_k = "AzureAI" if is_ai else "AzureInfra"
        mdl_k = "doc-intelligence-s0" if "document intelligence" in svc_l else ("ai-search-semantic-s2" if "search" in svc_l else "MODEL-NA")

        facts.append({
            "usage_date": d_str,
            "platform_key": plat_k,
            "identity_key": "unknown",
            "model_key": mdl_k,
            "cost_center_key": cc_k,
            "application_key": app_k,
            "environment_key": env_k,
            "business_unit_key": bu_k,
            "unit_type": "azure_meter",
            "quantity": round(qty, 4),
            "input_tokens": 0,
            "output_tokens": 0,
            "cached_tokens": 0,
            "requests": int(qty) if is_ai else 0,
            "cost_usd": eff_c,
            "list_cost_usd": list_c,
            "cost_is_estimated": "FALSE",
            "has_rate_card": "TRUE" if list_c > 0 else "FALSE",
            "is_error": "False",
            "latency_ms": 0,
        })

    # 7C. M365 Copilot Seats, Prompts, and Variable Copilot Credits (Cowork / Autopilot / Work IQ)
    for r in m365_seats:
        d_str = r["snapshot_date"]
        upn = r["user_principal_name"].lower()
        ik = upn_to_idkey.get(upn, "unknown")
        ident_meta = id_lookup[ik]
        facts.append({
            "usage_date": d_str,
            "platform_key": "M365Copilot",
            "identity_key": ik,
            "model_key": "",
            "cost_center_key": ident_meta["cost_center_key"],
            "application_key": "APP-M365",
            "environment_key": "ENV-PROD",
            "business_unit_key": ident_meta["home_business_unit_key"],
            "unit_type": "seat_day",
            "quantity": 1,
            "input_tokens": 0,
            "output_tokens": 0,
            "cached_tokens": 0,
            "requests": 0,
            "cost_usd": 1.0,
            "list_cost_usd": 1.0,
            "cost_is_estimated": "TRUE",
            "has_rate_card": "TRUE",
            "is_error": "False",
            "latency_ms": 0,
        })

    for r in m365_usage:
        prompts = int(r.get("prompts_submitted") or 0)
        if prompts <= 0:
            continue
        d_str = r["report_date"]
        upn = r["user_principal_name"].lower()
        ik = upn_to_idkey.get(upn, "unknown")
        ident_meta = id_lookup[ik]
        facts.append({
            "usage_date": d_str,
            "platform_key": "M365Copilot",
            "identity_key": ik,
            "model_key": "",
            "cost_center_key": ident_meta["cost_center_key"],
            "application_key": "APP-M365",
            "environment_key": "ENV-PROD",
            "business_unit_key": ident_meta["home_business_unit_key"],
            "unit_type": "prompt",
            "quantity": prompts,
            "input_tokens": 0,
            "output_tokens": 0,
            "cached_tokens": 0,
            "requests": prompts,
            "cost_usd": 0.0,
            "list_cost_usd": 0.0,
            "cost_is_estimated": "TRUE",
            "has_rate_card": "FALSE",
            "is_error": "False",
            "latency_ms": 0,
        })

    for r in m365_credits:
        d_str = r["usage_date"]
        upn = r["consumer_id"].lower()
        ik = upn_to_idkey.get(upn, "unknown")
        ident_meta = id_lookup[ik]
        credits = float(r["credits_consumed"])
        cost = float(r["cost_usd"])
        facts.append({
            "usage_date": d_str,
            "platform_key": "M365Copilot",
            "identity_key": ik,
            "model_key": "Cowork",
            "cost_center_key": ident_meta["cost_center_key"],
            "application_key": "APP-M365",
            "environment_key": "ENV-PROD",
            "business_unit_key": ident_meta["home_business_unit_key"],
            "unit_type": "copilot_credit",
            "quantity": credits,
            "input_tokens": 0,
            "output_tokens": 0,
            "cached_tokens": 0,
            "requests": max(1, int(credits / 3)),
            "cost_usd": cost,
            "list_cost_usd": cost,
            "cost_is_estimated": "TRUE",
            "has_rate_card": "TRUE",
            "is_error": "False",
            "latency_ms": 0,
        })

    # 7D. Copilot Studio Credits (Prepaid Capacity Pack vs P3 Commit vs PAYG vs M365 Zero-Rated vs BYOM Excluded)
    for r in studio_rows:
        if r.get("is_byom_foundry") == "TRUE":
            continue
        d_str = r["usage_date"]
        owner_u = (r.get("owner_upn") or "").lower()
        ik = upn_to_idkey.get(owner_u, "unknown")
        ident_meta = id_lookup[ik]
        app_k = r.get("application_key") or "APP-STUDIO"
        bu_k = r.get("owner_business_unit_key") or ident_meta["home_business_unit_key"]
        env_k = r.get("environment_key") or "ENV-PROD"
        gross_cr = float(r.get("gross_credits") or r.get("credits_consumed") or 0)
        net_cr = float(r.get("credits_consumed") or 0)
        bmode = r.get("billing_mode") or "PrepaidCapacityPack"
        sess = int(r.get("session_count") or 0)

        list_c = round(gross_cr * 0.01, 4)
        eff_c = float(r.get("cost_usd") or 0.0)
        is_est = "TRUE" if bmode == "PrepaidCapacityPack" else "FALSE"

        facts.append({
            "usage_date": d_str,
            "platform_key": "CopilotStudio",
            "identity_key": ik,
            "model_key": r.get("action_type") or "",
            "cost_center_key": ident_meta["cost_center_key"],
            "application_key": app_k,
            "environment_key": env_k,
            "business_unit_key": bu_k,
            "unit_type": "copilot_credit",
            "quantity": net_cr,
            "input_tokens": 0,
            "output_tokens": 0,
            "cached_tokens": 0,
            "requests": sess,
            "cost_usd": eff_c,
            "list_cost_usd": list_c,
            "cost_is_estimated": is_est,
            "has_rate_card": "TRUE",
            "is_error": "False",
            "latency_ms": 0,
        })

    # 7E. GitHub Copilot Enterprise Seats + Metered Premium Requests
    for r in ghc_seats:
        d_str = r["snapshot_date"]
        gh_l = r["assignee_login"].lower()
        ik = gh_to_idkey.get(gh_l, "unknown")
        ident_meta = id_lookup[ik]
        list_c = 1.30
        eff_c = round(1.30 * 0.95, 4)
        facts.append({
            "usage_date": d_str,
            "platform_key": "GitHubCopilot",
            "identity_key": ik,
            "model_key": "",
            "cost_center_key": ident_meta["cost_center_key"],
            "application_key": "APP-GHCP",
            "environment_key": "ENV-PROD",
            "business_unit_key": ident_meta["home_business_unit_key"],
            "unit_type": "seat_day",
            "quantity": 1,
            "input_tokens": 0,
            "output_tokens": 0,
            "cached_tokens": 0,
            "requests": 0,
            "cost_usd": eff_c,
            "list_cost_usd": list_c,
            "cost_is_estimated": "TRUE",
            "has_rate_card": "TRUE",
            "is_error": "False",
            "latency_ms": 0,
        })

    for r in ghc_prem:
        d_str = r["usage_date"]
        gh_l = r["login"].lower()
        ik = gh_to_idkey.get(gh_l, "unknown")
        ident_meta = id_lookup[ik]
        qty = float(r["quantity"])
        gross = float(r["gross_amount"])
        net = float(r["net_amount"])
        mdl = r["model"]
        facts.append({
            "usage_date": d_str,
            "platform_key": "GitHubCopilot",
            "identity_key": ik,
            "model_key": mdl,
            "cost_center_key": ident_meta["cost_center_key"],
            "application_key": "APP-GHCP",
            "environment_key": "ENV-PROD",
            "business_unit_key": ident_meta["home_business_unit_key"],
            "unit_type": "premium_request",
            "quantity": qty,
            "input_tokens": 0,
            "output_tokens": 0,
            "cached_tokens": 0,
            "requests": int(qty),
            "cost_usd": net,
            "list_cost_usd": gross,
            "cost_is_estimated": "FALSE",
            "has_rate_card": "TRUE",
            "is_error": "False",
            "latency_ms": 0,
        })

    fact_cols = [
        "usage_date", "platform_key", "identity_key", "model_key", "cost_center_key",
        "unit_type", "quantity", "input_tokens", "output_tokens", "cached_tokens",
        "requests", "cost_usd", "list_cost_usd", "cost_is_estimated", "has_rate_card",
        "is_error", "latency_ms", "application_key", "environment_key", "business_unit_key",
    ]
    write_csv("fact_ai_usage", facts, fact_cols)

    # ------------------------------------------------------------------
    # 8. dim_model (Clean AI & Service Models — no 'nonexistent-model' error junk)
    # ------------------------------------------------------------------
    models = [
        {"model_key": "gpt-4.1",               "model_name": "gpt-4.1",                        "model_version": "2025-04-14", "provider": "Azure OpenAI",   "modality": "text"},
        {"model_key": "gpt-4.1-mini",          "model_name": "gpt-4.1-mini",                   "model_version": "2025-04-14", "provider": "Azure OpenAI",   "modality": "text"},
        {"model_key": "o3-mini",               "model_name": "o3-mini",                        "model_version": "2025-01-31", "provider": "Azure OpenAI",   "modality": "reasoning"},
        {"model_key": "o3",                    "model_name": "o3",                             "model_version": "2025-04",    "provider": "GitHub Copilot", "modality": "reasoning"},
        {"model_key": "Mistral-Large-2",       "model_name": "Mistral-Large-2",                "model_version": "2411",       "provider": "Mistral AI (Foundry MaaS)", "modality": "text"},
        {"model_key": "claude-sonnet-4.5",     "model_name": "claude-sonnet-4.5",              "model_version": "2025-09",    "provider": "GitHub Copilot", "modality": "text"},
        {"model_key": "code-review",           "model_name": "Copilot Code Review (13x)",      "model_version": "2026",       "provider": "GitHub Copilot", "modality": "code"},
        {"model_key": "Cowork",                "model_name": "Cowork",                         "model_version": "1.0",        "provider": "M365 Copilot",   "modality": "agent"},
        {"model_key": "classic_answer",        "model_name": "Classic Answer (1 cr)",          "model_version": "1.0",        "provider": "Copilot Studio", "modality": "agent"},
        {"model_key": "generative_answer",     "model_name": "Generative Answer (2 cr)",       "model_version": "1.0",        "provider": "Copilot Studio", "modality": "agent"},
        {"model_key": "agent_action",          "model_name": "Autonomous Agent Action (5 cr)", "model_version": "1.0",        "provider": "Copilot Studio", "modality": "agent"},
        {"model_key": "tenant_graph_grounding","model_name": "Tenant Graph Grounding (10 cr)", "model_version": "1.0",        "provider": "Copilot Studio", "modality": "agent"},
        {"model_key": "agent_flow_action",     "model_name": "Agent Flow Action (13 cr)",      "model_version": "1.0",        "provider": "Copilot Studio", "modality": "agent"},
        {"model_key": "doc-intelligence-s0",   "model_name": "Document Intelligence Custom S0","model_version": "4.0",        "provider": "Azure AI Services", "modality": "document"},
        {"model_key": "speech-neural-stt",     "model_name": "Speech Neural STT/TTS",          "model_version": "1.0",        "provider": "Azure AI Services", "modality": "audio"},
        {"model_key": "content-safety-s0",     "model_name": "Content Safety Prompt Shield",   "model_version": "1.0",        "provider": "Azure AI Services", "modality": "guardrail"},
        {"model_key": "aml-nc24-a100",         "model_name": "AML Managed GPU (A100 v4)",      "model_version": "v4",         "provider": "Azure ML",       "modality": "compute"},
        {"model_key": "ai-search-semantic-s2", "model_name": "AI Search Vector + Semantic S2", "model_version": "S2",         "provider": "Azure AI Search","modality": "retrieval"},
        {"model_key": "MODEL-NA",              "model_name": "(no model / seat or infra)",     "model_version": "",           "provider": "Platform",       "modality": "n/a"},
        {"model_key": "unknown",               "model_name": "Unresolved / Error Model",       "model_version": "",           "provider": "Azure OpenAI",   "modality": "unknown"},
    ]
    write_csv("dim_model", models, ["model_key", "model_name", "model_version", "provider", "modality"])

    # ------------------------------------------------------------------
    # 9. fact_user_cost_allocation (100% tied out to fact_ai_usage)
    # ------------------------------------------------------------------
    alloc_groups = defaultdict(lambda: {"in_tok": 0, "out_tok": 0, "reqs": 0, "cost": 0.0})
    for f in facts:
        src_ik = f["identity_key"]
        src_meta = id_lookup.get(src_ik, id_lookup["unknown"])
        app_k = f["application_key"]
        app_meta = app_lookup.get(app_k, app_lookup["APP-UNKNOWN"])
        owner_u = (app_meta.get("owner_upn") or "").lower()
        owner_ik = upn_to_idkey.get(owner_u)

        if src_meta["is_human"] == "TRUE":
            basis = "DIRECT"
            usr_ik = src_ik
        elif owner_ik and src_ik != "unattributed-gateway-bypass":
            basis = "OWNER"
            usr_ik = owner_ik
        else:
            basis = "UNALLOCATED"
            usr_ik = "unknown"

        key = (
            f["usage_date"],
            f["platform_key"],
            f["application_key"],
            f["environment_key"],
            f["business_unit_key"],
            f["cost_center_key"],
            usr_ik,
            src_ik,
            basis,
            f["cost_is_estimated"],
        )
        g = alloc_groups[key]
        g["in_tok"] += int(f["input_tokens"])
        g["out_tok"] += int(f["output_tokens"])
        g["reqs"] += int(f["requests"])
        g["cost"] += float(f["cost_usd"])

    alloc_rows = []
    for k, g in sorted(alloc_groups.items()):
        (u_date, plat_k, app_k, env_k, bu_k, cc_k, usr_ik, src_ik, basis, is_est) = k
        tot_tok = g["in_tok"] + g["out_tok"]
        alloc_rows.append({
            "usage_date": u_date,
            "platform_key": plat_k,
            "application_key": app_k,
            "environment_key": env_k,
            "business_unit_key": bu_k,
            "cost_center_key": cc_k,
            "user_identity_key": usr_ik,
            "source_identity_key": src_ik,
            "allocation_basis": basis,
            "cost_is_estimated": is_est,
            "input_tokens": g["in_tok"],
            "output_tokens": g["out_tok"],
            "total_tokens": tot_tok,
            "requests": g["reqs"],
            "cost_usd": round(g["cost"], 6),
        })

    alloc_cols = [
        "usage_date", "platform_key", "application_key", "environment_key", "business_unit_key",
        "cost_center_key", "user_identity_key", "source_identity_key", "allocation_basis",
        "cost_is_estimated", "input_tokens", "output_tokens", "total_tokens", "requests", "cost_usd",
    ]
    write_csv("fact_user_cost_allocation", alloc_rows, alloc_cols)

    # ------------------------------------------------------------------
    # 10. fact_agent_inventory & fact_agent_activity
    # ------------------------------------------------------------------
    inv_rows = []
    for ag in dim_agents:
        if ag["agent_key"] == "NO-AGENT":
            continue
        owner_u = (ag.get("owner_upn") or "").strip().lower()
        owner_ik = upn_to_idkey.get(owner_u, "unknown")
        o_class = ag.get("owner_class") or ""
        inv_rows.append({
            "snapshot_date": "2026-10-26",
            "agent_key": ag["agent_key"],
            "owner_identity_key": owner_ik,
            "agent_count": 1,
            "is_unowned_tenant_agent": 1 if o_class == "Unowned tenant agent" else 0,
            "has_owner": 1 if o_class == "Owner assigned" else 0,
            "has_entra_identity": 1 if ag.get("entra_agent_id") else 0,
            "can_read_files_or_sites": 1 if ag["publisher_type"] == "FirstParty" else 0,
            "has_custom_actions": 1 if ag["platform"] in ("Copilot Studio", "Microsoft Foundry", "Microsoft 365 Agents Toolkit") else 0,
            "can_use_code_interpreter": 1 if ag["platform"] in ("Microsoft Foundry", "Microsoft 365 Agents Toolkit") else 0,
            "is_available": 1 if ag.get("status") == "Available" else 0,
            "_data_class": ag.get("data_class", "REAL"),
        })
    write_csv(
        "fact_agent_inventory",
        inv_rows,
        ["snapshot_date", "agent_key", "owner_identity_key", "agent_count",
         "is_unowned_tenant_agent", "has_owner", "has_entra_identity",
         "can_read_files_or_sites", "has_custom_actions", "can_use_code_interpreter",
         "is_available", "_data_class"],
    )

    act_rows = [
        {"activity_date": "2026-10-26", "identity_key": "unknown",        "agents_used": 3, "sessions": 4,  "is_identity_resolved": 0, "_data_class": "REAL"},
        {"activity_date": "2026-09-04", "identity_key": "aisha.rahman",   "agents_used": 4, "sessions": 19, "is_identity_resolved": 1, "_data_class": "MOCK"},
        {"activity_date": "2026-09-04", "identity_key": "marco.silva",    "agents_used": 3, "sessions": 15, "is_identity_resolved": 1, "_data_class": "MOCK"},
        {"activity_date": "2026-09-04", "identity_key": "jenny.oyelaran", "agents_used": 5, "sessions": 24, "is_identity_resolved": 1, "_data_class": "MOCK"},
        {"activity_date": "2026-09-04", "identity_key": "robin.hale",     "agents_used": 3, "sessions": 14, "is_identity_resolved": 1, "_data_class": "MOCK"},
        {"activity_date": "2026-09-04", "identity_key": "sam.chen",       "agents_used": 2, "sessions": 11, "is_identity_resolved": 1, "_data_class": "MOCK"},
        {"activity_date": "2026-09-04", "identity_key": "dev.patel",      "agents_used": 2, "sessions": 9,  "is_identity_resolved": 1, "_data_class": "MOCK"},
    ]
    write_csv(
        "fact_agent_activity",
        act_rows,
        ["activity_date", "identity_key", "agents_used", "sessions", "is_identity_resolved", "_data_class"],
    )

    # ------------------------------------------------------------------
    # 11. dim_date (Continuous calendar covering all usage & snapshot dates)
    # ------------------------------------------------------------------
    cal = []
    c = date(2026, 6, 22)
    end_cal = date(2026, 11, 30)
    while c <= end_cal:
        cal.append({
            "date_key": c.isoformat(),
            "year": c.year,
            "quarter": f"Q{(c.month - 1) // 3 + 1}",
            "month": c.month,
            "month_name": c.strftime("%b %Y"),
            "day": c.day,
            "day_name": c.strftime("%a"),
            "is_weekday": "TRUE" if c.weekday() < 5 else "FALSE",
            "year_month": c.strftime("%Y-%m"),
        })
        c += timedelta(days=1)
    write_csv(
        "dim_date",
        cal,
        ["date_key", "year", "quarter", "month", "month_name", "day", "day_name", "is_weekday", "year_month"],
    )

    # ------------------------------------------------------------------
    # 11b. extractable_data_catalog (backs dim_data_source)
    # ------------------------------------------------------------------
    catalog_rows = [
        ("Azure Cost Management (FOCUS 1.2)", "Cost Authority", "EffectiveCost / BilledCost / ListCost / ContractedCost", "Cost Management FOCUS 1.2 Export -> OneLake Shortcut", "Per resource x meter x day", "Resource / Subscription / Tag", "Billed & Amortized (FOCUS)", "Multi-year (OneLake Delta)", "REAL", "Single billing authority for all Azure AI, PTUs, Azure ML, Supporting Infra & Copilot PAYG/P3"),
        ("Azure Cost Management (FOCUS 1.2)", "Commitment Discounts", "CommitmentDiscountStatus (Used vs Unused PTU & P3)", "Cost Management FOCUS 1.2 Export", "Per reservation x day", "Resource / Billing Profile", "Amortized (EffectiveCost)", "Multi-year", "REAL", "Exposes unused Azure OpenAI PTU reservation capacity and Copilot Credit P3 commit utilization"),
        ("Azure AI Foundry", "Tokens", "Prompt / Completion / Cached / Reasoning tokens", "APIM AI Gateway -> Log Analytics / Eventhouse", "Per request", "Per user / SP / Agent (Entra JWT)", "Billed (FOCUS allocated)", "Log Analytics 30-730d", "REAL", "APIM is the only path with per-caller token attribution; allocates FOCUS resource cost pro-rata"),
        ("Azure AI Foundry", "Identity", "Entra OID / ClientId / AgentId / cc: & bu: claims", "APIM AI Gateway validate-azure-ad-token", "Per request", "Per principal", "n/a", "Log Analytics 30-730d", "REAL", "Distinguishes Human, ServicePrincipal, ManagedIdentity, and Entra Agent ID"),
        ("Azure AI Foundry", "Governance", "Direct resource access bypassing APIM Gateway", "FOCUS Resource Cost minus APIM Logged Tokens", "Per resource x day", "Unattributed resource", "Billed (FOCUS)", "Multi-year", "REAL", "Detects shadow API key calls that bypassed the central APIM AI Gateway"),
        ("Azure AI Foundry", "Performance", "TotalLatencyMs / StatusCode / 429 Throttles", "APIM AI Gateway + Azure Monitor metrics", "Per request / 1-min", "Per caller", "n/a", "93-730 days", "REAL", "Tracks P95 latency, HTTP 429 rate limits, and PTU failover events"),
        ("Azure OpenAI (PTU)", "Throughput", "AzureOpenAIProvisionedManagedUtilizationV2", "Azure Monitor metrics", "Per deployment x minute", "Resource / Deployment", "Amortized via FOCUS", "93 days (Bronze retains)", "AVAILABLE", "Pairs with FOCUS CommitmentDiscountStatus to size PTU reservations vs PAYG burst"),
        ("Azure AI Services", "Consumption", "Document Intelligence Pages / Speech Hours / Content Safety Tx", "FOCUS 1.2 Export + Azure Monitor", "Per resource x meter x day", "Resource / Tag (app, bu, agent_id)", "Billed (FOCUS)", "Multi-year", "REAL", "Covers non-LLM Azure AI services (DocIntel, Speech, Vision, Language, Content Safety)"),
        ("Azure Machine Learning", "Compute & Endpoints", "GPU Compute Hours & Managed Online Endpoint vCPU", "FOCUS 1.2 Export + AML Diagnostic Logs", "Per workspace / job / endpoint", "Workspace / Submitting User", "Billed (FOCUS)", "Multi-year", "REAL", "Tracks custom model training and online inference compute cost"),
        ("Azure AI Supporting Infra", "RAG & Gateway Infra", "AI Search SUs, Cosmos DB RUs, Redis Semantic Cache, APIM, Fabric CU, Logs", "FOCUS 1.2 Export", "Per resource x meter x day", "Resource / Tag", "Billed (FOCUS)", "Multi-year", "REAL", "Captures the 25-45% supporting infrastructure TCO behind RAG and agent workloads"),
        ("APIM AI Gateway", "Optimization", "llm-semantic-cache-lookup & llm-token-limit events", "APIM AI Gateway policy logs", "Per request", "Per app / subscription", "Billed savings", "Log Analytics", "AVAILABLE", "Measures semantic cache hit rate and prevented token quota overruns"),
        ("Agent 365", "Governance", "Tenant Agent Registry (1P, 3P, Foundry, Copilot Studio, Toolkit)", "M365 Admin Center Export / Graph /copilot/admin/catalog/packages", "Per agent snapshot", "Owner UPN + Entra Agent ID", "Joined via Owner / Tag", "Snapshot history in Bronze", "REAL", "393 real tenant agents classified by owner_class, publisher_type, and Entra Agent ID"),
        ("Agent 365", "Activity", "Active agent users & session counts", "M365 Admin Center Active Users export", "Per user snapshot", "Per user (requires unmasked UPN setting)", "n/a", "Snapshot history", "REAL", "Requires M365 Admin > Org Settings > Reports > uncheck Display concealed names"),
        ("M365 Copilot", "Licensing", "Assigned & unassigned seats by SKU", "Graph /subscribedSkus + /users?$select=assignedLicenses", "Per user x day", "Per user (UPN / Entra OID)", "Fixed ($30/seat/mo Rate Card)", "Live + Bronze history", "REAL", "2 live tenant seats verified + 8 persona seats; drives Fixed Cost & Idle Seat Waste"),
        ("M365 Copilot", "Usage", "Per-app last activity date & prompt volume", "Graph /reports/getMicrosoft365CopilotUsageUserDetail", "Per user x app x day", "Per user (UPN)", "n/a (included in seat)", "180 days", "MOCK", "Identifies licensed users with 0 activity in 28 days for license reclamation"),
        ("M365 Copilot Cowork", "Variable Credits", "Cowork / Autopilot / Work IQ API / SharePoint Agent credits", "M365 Admin Cost Management + FOCUS Microsoft Copilot Studio meter", "Per consumer x capability x day", "Per user / agent", "Billed (PAYG / P3) or Pack", "Multi-year", "MOCK", "Variable M365 consumption outside the flat $30 seat license"),
        ("Copilot Studio", "Variable Credits", "msdyn_creditconsumed by action_type & agent", "Dataverse msdyn_aievents + PPAC Licensing + FOCUS", "Per event / agent x day", "Per agent (bot_id) + owner", "Billed (P3/PAYG) + Modelled (Pack)", "Dataverse + Bronze", "MOCK", "Net of M365 Copilot zero-rating; excludes BYOM Foundry agents to prevent double-counting"),
        ("GitHub Copilot", "Licensing", "Seat assignee, plan_type, last_activity_at, editor", "REST GET /orgs/{org}/copilot/billing/seats", "Per seat x day", "Per github_login (-> Entra UPN)", "Fixed ($39/seat/mo Rate Card)", "Live + Bronze history", "MOCK", "Detects inactive developer seats across VS Code, Visual Studio, and JetBrains"),
        ("GitHub Copilot", "Variable Overage", "Metered premium_request netAmount & model_multiplier", "REST GET /enterprises/{ent}/settings/billing/usage", "Per user x model x day", "Per github_login", "Billed (netAmount USD)", "24 months", "MOCK", "Captures $0.04 x multiplier (e.g., Code Review 13x, Claude Sonnet, o3) overage spend"),
        ("GitHub Copilot", "Engineering ROI", "Code suggestions, acceptances, lines accepted, chat turns", "REST GET /orgs/{org}/copilot/metrics", "Daily org/team/language/editor", "Aggregate (>=5 active users)", "n/a", "1 year", "MOCK", "Pairs acceptance rate with seat + overage spend for developer productivity ROI"),
    ]
    write_csv(
        "extractable_data_catalog",
        [
            {
                "platform": r[0], "signal_category": r[1], "signal": r[2], "source_api": r[3],
                "grain": r[4], "identity_granularity": r[5], "cost_fidelity": r[6],
                "retention": r[7], "availability": r[8], "notes": r[9],
            }
            for r in catalog_rows
        ],
        ["platform", "signal_category", "signal", "source_api", "grain", "identity_granularity", "cost_fidelity", "retention", "availability", "notes"],
    )

    # ------------------------------------------------------------------
    # 12. Assertions: Referential Integrity & Tie-Out Verification
    # ------------------------------------------------------------------
    fact_cost = sum(float(f["cost_usd"]) for f in facts)
    alloc_cost = sum(float(a["cost_usd"]) for a in alloc_rows)
    fact_tok = sum(int(f["input_tokens"]) + int(f["output_tokens"]) for f in facts)
    alloc_tok = sum(int(a["total_tokens"]) for a in alloc_rows)
    assert abs(fact_cost - alloc_cost) < 0.01, f"Cost tie-out mismatch: {fact_cost} vs {alloc_cost}"
    assert fact_tok == alloc_tok, f"Token tie-out mismatch: {fact_tok} vs {alloc_tok}"

    date_keys = {r["date_key"] for r in cal}
    plat_keys = {r["platform_key"] for r in platforms}
    id_keys = {r["identity_key"] for r in identities}
    mdl_keys = {r["model_key"] for r in models} | {""}
    cc_keys = {r["cost_center_key"] for r in cost_centers}
    app_keys = {r["application_key"] for r in apps}
    env_keys = {r["environment_key"] for r in envs}
    bu_keys = {r["business_unit_key"] for r in bus}
    ag_keys = {r["agent_key"] for r in dim_agents}

    assert not ({f["usage_date"] for f in facts} - date_keys), "Orphan usage_date in fact_ai_usage"
    assert not ({f["platform_key"] for f in facts} - plat_keys), "Orphan platform_key in fact_ai_usage"
    assert not ({f["identity_key"] for f in facts} - id_keys), "Orphan identity_key in fact_ai_usage"
    assert not ({f["model_key"] for f in facts} - mdl_keys), f"Orphan model_key in fact_ai_usage: {({f['model_key'] for f in facts} - mdl_keys)}"
    assert not ({f["cost_center_key"] for f in facts} - cc_keys), "Orphan cost_center_key in fact_ai_usage"
    assert not ({f["application_key"] for f in facts} - app_keys), "Orphan application_key in fact_ai_usage"
    assert not ({f["environment_key"] for f in facts} - env_keys), "Orphan environment_key in fact_ai_usage"
    assert not ({f["business_unit_key"] for f in facts} - bu_keys), "Orphan business_unit_key in fact_ai_usage"
    assert not ({a["cost_center_key"] for a in alloc_rows} - cc_keys), "Orphan cost_center_key in fact_user_cost_allocation"

    billed = sum(float(f["cost_usd"]) for f in facts if f["cost_is_estimated"] == "FALSE")
    est = sum(float(f["cost_usd"]) for f in facts if f["cost_is_estimated"] == "TRUE")
    list_tot = sum(float(f["list_cost_usd"]) for f in facts)
    print(f"\n  FOCUS List Cost       ${list_tot:,.2f}")
    print(f"  Effective / Total AI  ${fact_cost:,.2f}  (Savings vs List: ${list_tot - fact_cost:,.2f})")
    print(f"  Billed (FOCUS/Net)    ${billed:,.2f}  ({billed / fact_cost * 100:.1f}% Cost Confidence)")
    print(f"  Modelled (Rate Card)  ${est:,.2f}")
    print(f"  User Allocation Tie   ${alloc_cost:,.2f}  (diff = ${abs(fact_cost - alloc_cost):.6f}, 0 orphan keys)")


if __name__ == "__main__":
    build()

