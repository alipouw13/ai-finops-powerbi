#!/usr/bin/env python3
"""Extract REAL AI FinOps telemetry from an Azure/Entra/Power Platform tenant.

Companion to gen_bronze_data.py: same Bronze table names and schemas, but every
row carries `_data_class='REAL'` instead of `'MOCK'`. Silver unions the two, and
gold derives `dim_platform[data_source]` from that column -- so provenance
follows the data and nothing has to be relabelled by hand.

Tables produced (all into a gitignored directory):

    bronze_azure_cost              real invoiced Azure spend, per resource per day
    bronze_azure_ai_metrics        real call/token telemetry per AI resource per day
    bronze_ref_app_inventory       real Azure AI resources as "applications"
    bronze_ref_identity_map        real Entra users + service principals
    bronze_ref_agent_inventory     real Copilot Studio agents from Dataverse
    bronze_m365_license_inventory  real subscribed SKUs

WHAT IS AND IS NOT AVAILABLE (verified against a live tenant)
------------------------------------------------------------
* Cost      -> Microsoft.Consumption/usageDetails. The Cost Management *Query*
               API is aggressively throttled (HTTP 429 on every attempt across
               many retries); usageDetails is not, and returns the per-resource
               per-day grain Bronze wants. For production, scheduled Cost
               Management Exports are the supported pattern.
* Tokens    -> Azure Monitor exposes InputTokens/OutputTokens/TotalTokens, but
               they are only populated for token-billed model traffic. Content
               Understanding, Document Intelligence, Speech and Translation
               emit TotalCalls only. Both are collected; whichever is populated
               is what you get, and zero is reported as zero.
* Identity  -> Graph /users and /servicePrincipals.
* Agents    -> Dataverse `bots` per environment. Credit consumption lives in
               `msdyn_aievents`, which is empty until an agent is actually run.

USAGE
    az login --tenant <tenant-id>
    python platform/fabric/extract_real_bronze.py --probe
    python platform/fabric/extract_real_bronze.py --i-understand-this-is-real-tenant-data

Stdlib only.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_OUT = os.path.join(HERE, "bronze_real")
ARM = "https://management.azure.com"
GRAPH = "https://graph.microsoft.com"
RETRIES = 5

# Azure resource kind -> the accelerator's platform taxonomy.
KIND_TO_PLATFORM = {
    "OpenAI": "Foundry",
    "AIServices": "Foundry",
    "CognitiveServices": "Foundry",
}
TOKEN_METRICS = ["InputTokens", "OutputTokens", "TotalTokens", "ModelRequests",
                 "ProcessedPromptTokens", "GeneratedTokens", "TotalCalls"]


def az_exe():
    exe = shutil.which("az") or shutil.which("az.cmd")
    if not exe:
        sys.exit("Azure CLI (`az`) not found on PATH. Install it and run `az login`.")
    return exe


def token(resource, tenant=None):
    cmd = [az_exe(), "account", "get-access-token", "--resource", resource,
           "--query", "accessToken", "-o", "tsv"]
    if tenant:
        cmd += ["--tenant", tenant]
    out = subprocess.run(cmd, capture_output=True, text=True)
    return out.stdout.strip() if out.returncode == 0 else None


def safe_url(url):
    """Percent-encode a URL's query string.

    Consumption `usageDetails` echoes the `$filter` back inside `nextLink`
    *unencoded*, so the continuation URL contains raw spaces and http.client
    rejects it with InvalidURL. Re-quote the query while leaving OData
    punctuation intact.
    """
    parts = urllib.parse.urlsplit(url)
    if not parts.query:
        return url
    query = urllib.parse.quote(parts.query, safe="=&$/':,+*()!~-._%")
    return urllib.parse.urlunsplit(parts._replace(query=query))


def http(method, url, tok, body=None, extra_headers=None):
    """GET/POST with retry on throttling and transient network faults."""
    url = safe_url(url)
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Authorization": "Bearer " + tok}
    if data:
        headers["Content-Type"] = "application/json"
    if extra_headers:
        headers.update(extra_headers)
    for attempt in range(RETRIES):
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                raw = resp.read()
                return resp.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as e:
            txt = e.read().decode(errors="replace")
            if e.code in (429, 500, 502, 503, 504) and attempt < RETRIES - 1:
                wait = int(e.headers.get("Retry-After") or 0) or min(2 ** attempt * 5, 60)
                print(f"      HTTP {e.code}, retry in {wait}s")
                time.sleep(wait)
                continue
            return e.code, txt
        except (urllib.error.URLError, ssl.SSLError, OSError) as e:
            if attempt < RETRIES - 1:
                time.sleep(2 ** attempt)
                continue
            return 0, str(e)
    return 0, "exhausted retries"


def paged(url, tok, limit_pages=400):
    """Follow nextLink / @odata.nextLink and concatenate `value`.

    The page cap is a runaway guard, not a row budget: hitting it means the
    extract is incomplete, so it warns rather than truncating in silence.
    """
    items, pages = [], 0
    while url and pages < limit_pages:
        status, payload = http("GET", url, tok)
        if status != 200 or not isinstance(payload, dict):
            if pages == 0:
                print(f"      HTTP {status}: {str(payload)[:140]}")
            break
        items.extend(payload.get("value", []))
        url = payload.get("nextLink") or payload.get("@odata.nextLink")
        pages += 1
    if url and pages >= limit_pages:
        print(f"      ! stopped at the {limit_pages}-page cap with more data "
              f"available — this extract is INCOMPLETE")
    return items


NOW = dt.datetime.now(dt.timezone.utc)


def lineage(source_api, watermark, tenant_domain):
    return {
        "_ingested_at": NOW.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "_source_system": "AzureTenant/" + tenant_domain,
        "_source_api": source_api,
        "_watermark": watermark,
        "_batch_id": "real-" + NOW.strftime("%Y-%m-%d"),
        "_data_class": "REAL",
    }


def write(out_dir, name, rows, columns=None):
    os.makedirs(out_dir, exist_ok=True)
    cols = columns or (list(rows[0].keys()) if rows else [])
    if not cols:
        # A header-less CSV loads as a zero-column Delta table and every
        # downstream union fails on a missing column. An empty extract is a
        # legitimate state (nothing has happened yet), so it must still carry
        # its schema.
        raise ValueError(f"{name}: no rows and no explicit columns — an empty "
                         f"table still has to declare its schema")
    path = os.path.join(out_dir, name + ".csv")
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    print(f"  {name:34} {len(rows):7,} rows")
    return len(rows)


# --------------------------------------------------------------------- Azure
def resource_graph(arm_tok, query):
    status, payload = http(
        "POST", f"{ARM}/providers/Microsoft.ResourceGraph/resources?api-version=2021-03-01",
        arm_tok, {"query": query})
    return payload.get("data", []) if status == 200 else []


def collect_ai_resources(arm_tok):
    """Every Cognitive Services / AI account the signed-in user can see.

    Resource Graph rather than per-subscription listing: the subscription
    endpoint returned 0 accounts in testing while Graph returned 16, because it
    resolves across every scope the principal can read.
    """
    return resource_graph(arm_tok,
                          "resources | where type =~ "
                          "'microsoft.cognitiveservices/accounts' "
                          "| project name, kind, location, resourceGroup, "
                          "subscriptionId, id, tags")


def app_key(name):
    """Stable application key from a resource name.

    Silver must be able to reproduce this exactly to join cost to the app
    inventory, so keep it trivially portable: uppercase, non-alphanumerics to
    dashes, prefix APP-.
    """
    return "APP-" + re.sub(r"[^A-Z0-9-]", "-", (name or "").upper())


def collect_cost(arm_tok, subs, days, tenant_domain):
    """Real invoiced spend, per resource per day, from Consumption usageDetails."""
    start = (NOW.date() - dt.timedelta(days=days)).isoformat()
    end = NOW.date().isoformat()
    rows = []
    for sid in subs:
        # startDate/endDate, NOT $filter. usageDetails accepts an OData
        # $filter on properties/usageStart without complaining and then
        # ignores it, returning only the open billing period — 8 days here
        # instead of the 90 that were asked for. The dedicated date params are
        # honoured, and the difference is silent, so it has to be asserted:
        # see the day-count check in main().
        url = (f"{ARM}/subscriptions/{sid}/providers/Microsoft.Consumption/"
               f"usageDetails?api-version=2023-05-01"
               f"&startDate={start}&endDate={end}&$top=1000")
        items = paged(url, arm_tok)
        print(f"      subscription {sid[:8]}...: {len(items)} usage row(s)")
        for it in items:
            p = it.get("properties", {})
            md = p.get("meterDetails") or {}
            # `kind` is "modern" in this tenant: the ARM id is `instanceName`
            # and meter fields are top level. "legacy" records use resourceId /
            # meterDetails.*, so read both shapes.
            rid = p.get("resourceId") or p.get("instanceId") or p.get("instanceName") or ""
            rname = rid.rsplit("/", 1)[-1] if rid else ""
            usage_date = (p.get("date") or p.get("usageStart") or "")[:10]
            cost = p.get("costInUSD")
            if cost is None:
                cost = p.get("cost", p.get("costInBillingCurrency", 0))
            rows.append({
                "usage_date": usage_date,
                "subscription_id": p.get("subscriptionGuid") or sid,
                "subscription_name": p.get("subscriptionName", ""),
                "resource_group": p.get("resourceGroup") or "",
                "resource_id": rid,
                "resource_name": rid.rsplit("/", 1)[-1] if rid else "",
                "application_key": app_key(rname) if rname else "",
                "consumed_service": p.get("consumedService", ""),
                "service_family": p.get("serviceFamily", ""),
                "resource_location": p.get("resourceLocation", ""),
                "meter_id": p.get("meterId", md.get("meterId", "")),
                "meter_name": p.get("meterName", md.get("meterName", "")),
                "meter_category": p.get("meterCategory", md.get("meterCategory", "")),
                "meter_subcategory": p.get("meterSubCategory",
                                           md.get("meterSubCategory", "")),
                "product": p.get("product", ""),
                "charge_type": p.get("chargeType", ""),
                "unit_of_measure": p.get("unitOfMeasure", md.get("unitOfMeasure", "")),
                "cost_center": p.get("costCenter", ""),
                "quantity": p.get("quantity", 0),
                "unit_price": p.get("unitPrice", 0),
                "cost_usd": round(float(cost or 0), 6),
                "currency": p.get("billingCurrencyCode", "USD"),
                "tags_json": json.dumps(it.get("tags") or {}),
                **lineage("consumption.usageDetails", usage_date, tenant_domain),
            })
    return aggregate_cost(rows)


def aggregate_cost(rows):
    """Collapse usageDetails line items onto the declared bronze grain.

    Azure emits several line items for one resource-meter-day — one per
    deployment, benefit, reservation and so on — that are identical across every
    column this extractor projects. They are not duplicates, so the loader must
    not discard them, but they are also not separately meaningful at the grain
    this model reports. Summing them here makes the grain explicit and, crucially,
    makes re-extraction idempotent: the append-and-dedupe loader can then key on
    the whole row without ever dropping real money.

    line_item_count keeps the collapse visible rather than hiding it.
    """
    MEASURES = ("quantity", "cost_usd")
    grouped = {}
    for r in rows:
        key = tuple(v for k, v in r.items() if k not in MEASURES)
        acc = grouped.get(key)
        if acc is None:
            acc = dict(r)
            acc["quantity"] = 0.0
            acc["cost_usd"] = 0.0
            acc["line_item_count"] = 0
            grouped[key] = acc
        acc["quantity"] += float(r.get("quantity") or 0)
        acc["cost_usd"] += float(r.get("cost_usd") or 0)
        acc["line_item_count"] += 1
    out = list(grouped.values())
    for r in out:
        r["cost_usd"] = round(r["cost_usd"], 6)
    return out


def collect_ai_metrics(arm_tok, resources, days, tenant_domain):
    """Per-resource, per-day AI telemetry from Azure Monitor.

    Token metrics only populate for token-billed model traffic; Content
    Understanding / Document Intelligence / Speech / Translation emit TotalCalls
    only. Collect both and report honestly whichever exists.
    """
    start = NOW - dt.timedelta(days=days)
    ts = f"{start.strftime('%Y-%m-%dT%H:%M:%SZ')}/{NOW.strftime('%Y-%m-%dT%H:%M:%SZ')}"
    rows = []
    for res in resources:
        rid, name = res["id"], res["name"]
        url = (f"{ARM}{rid}/providers/microsoft.insights/metrics?api-version=2018-01-01"
               f"&metricnames={','.join(TOKEN_METRICS)}&aggregation=Total"
               f"&interval=P1D&timespan={ts}")
        status, payload = http("GET", url, arm_tok)
        if status != 200:
            # Not every kind supports every metric; a 400 here is expected for
            # Speech/Translation/FormRecognizer and is not an error worth noise.
            continue
        per_day: dict[str, dict[str, float]] = {}
        for metric in payload.get("value", []):
            mname = metric["name"]["value"]
            for series in metric.get("timeseries", []):
                for point in series.get("data", []):
                    total = point.get("total")
                    if not total:
                        continue
                    day = point["timeStamp"][:10]
                    per_day.setdefault(day, {})[mname] = \
                        per_day.setdefault(day, {}).get(mname, 0) + total
        for day, vals in sorted(per_day.items()):
            inp = int(vals.get("InputTokens") or vals.get("ProcessedPromptTokens") or 0)
            out = int(vals.get("OutputTokens") or vals.get("GeneratedTokens") or 0)
            total_tok = int(vals.get("TotalTokens") or (inp + out))
            reqs = int(vals.get("ModelRequests") or vals.get("TotalCalls") or 0)
            rows.append({
                "metric_time": day,
                "resource_id": rid,
                "deployment_name": "",
                "model_name": "",
                "processed_prompt_tokens": inp,
                "generated_tokens": out,
                "total_tokens": total_tok,
                "requests": reqs,
                "latency_ms": 0,
                "throttled_count": 0,
                **lineage("azuremonitor.metrics", day, tenant_domain),
            })
        if per_day:
            print(f"      {name[:34]:36} {len(per_day)} day(s) with telemetry")
    return rows


# --------------------------------------------------------------------- Graph
def collect_identities(graph_tok, tenant_domain):
    users = paged(f"{GRAPH}/v1.0/users?$select=id,userPrincipalName,displayName,"
                  f"department,jobTitle,accountEnabled&$top=999", graph_tok)
    sps = paged(f"{GRAPH}/v1.0/servicePrincipals?$select=id,appId,displayName,"
                f"servicePrincipalType,accountEnabled&$top=999", graph_tok)
    rows = []
    for u in users:
        dept = u.get("department") or ""
        rows.append({
            "identity_key": u.get("userPrincipalName", u.get("id", "")),
            "display_name": u.get("displayName", ""),
            "principal_type": "User",
            "upn": u.get("userPrincipalName", ""),
            "entra_object_id": u.get("id", ""),
            "github_login": "",
            "is_human": "TRUE",
            "department": dept,
            # No Entra source for BU/cost-centre; leave blank so gold routes
            # them to the Unallocated members rather than inventing a mapping.
            "home_business_unit_key": "",
            "cost_center_key": "",
            **lineage("graph.users", NOW.date().isoformat(), tenant_domain),
        })
    for sp in sps:
        rows.append({
            "identity_key": sp.get("appId", sp.get("id", "")),
            "display_name": sp.get("displayName", ""),
            "principal_type": "ServicePrincipal",
            "upn": "",
            "entra_object_id": sp.get("id", ""),
            "github_login": "",
            "is_human": "FALSE",
            "department": "",
            "home_business_unit_key": "",
            "cost_center_key": "",
            **lineage("graph.servicePrincipals", NOW.date().isoformat(), tenant_domain),
        })
    return rows


def collect_licenses(graph_tok, tenant_domain):
    skus = paged(f"{GRAPH}/v1.0/subscribedSkus", graph_tok)
    today = NOW.date().isoformat()
    rows = []
    for s in skus:
        pre = s.get("prepaidUnits", {}) or {}
        rows.append({
            "snapshot_date": today,
            "sku_id": s.get("skuId", ""),
            "sku_part_number": s.get("skuPartNumber", ""),
            "capability_status": s.get("capabilityStatus", ""),
            "applies_to": s.get("appliesTo", ""),
            "prepaid_enabled": pre.get("enabled", 0),
            "prepaid_warning": pre.get("warning", 0),
            "prepaid_suspended": pre.get("suspended", 0),
            "consumed_units": s.get("consumedUnits", 0),
            "service_plans_enabled": ";".join(
                sp.get("servicePlanName", "") for sp in s.get("servicePlans", [])
                if sp.get("provisioningStatus") == "Success"),
            **lineage("graph.subscribedSkus", today, tenant_domain),
        })
    return rows


# ----------------------------------------------------------- Power Platform
def collect_agents(tenant_domain):
    pp = token("https://service.powerapps.com/")
    if not pp:
        print("      no Power Platform token")
        return []
    status, payload = http(
        "GET", "https://api.bap.microsoft.com/providers/Microsoft.BusinessAppPlatform/"
        "scopes/admin/environments?api-version=2020-10-01", pp)
    if status != 200:
        print(f"      environments: HTTP {status}")
        return []
    rows = []
    for env in payload.get("value", []):
        props = env.get("properties", {})
        env_name = props.get("displayName", "?")
        api = (props.get("linkedEnvironmentMetadata") or {}).get("instanceApiUrl")
        if not api:
            continue
        dv = token(api.rstrip("/") + "/")
        if not dv:
            continue
        bots = paged(f"{api.rstrip('/')}/api/data/v9.2/bots"
                     f"?$select=botid,name,createdon,_ownerid_value", dv)
        for b in bots:
            rows.append({
                "agent_key": b.get("botid", ""),
                "agent_name": b.get("name", ""),
                "platform": "CopilotStudio",
                "environment_id": env.get("name", ""),
                "owner_upn": "",
                "owner_business_unit_key": "",
                "purpose": env_name,
                "created_on": (b.get("createdon") or "")[:10],
                **lineage("dataverse.bots", NOW.date().isoformat(), tenant_domain),
            })
        print(f"      {env_name[:34]:36} {len(bots)} agent(s)")
    return rows


STUDIO_CREDIT_COLUMNS = [
    "usage_date", "environment_id", "agent_id", "agent_name", "action_type",
    "credit_rate", "credits_consumed", "cost_usd", "session_count",
    "owner_business_unit_key", "_ingested_at", "_source_system", "_source_api",
    "_watermark", "_batch_id", "_data_class",
]


def collect_studio_credits(tenant_domain, agents):
    """Real Copilot Studio credit consumption from Dataverse `msdyn_aievent`.

    This is the table the Power Platform admin centre bills from, and
    msdyn_creditconsumed is already net of zero-rated events — so it is used
    directly and never modelled up from session counts.

    cost_usd is deliberately left at 0: Dataverse reports *credits*, not
    dollars. Silver converts credits to USD from the rate card and flags those
    rows cost_is_estimated=True, which keeps the billed-vs-modelled split
    honest. Writing a dollar figure here would launder a modelled number into
    something that looks invoiced.

    Returns [] until an agent is actually run. That is the true state of the
    tenant, not a collector failure.
    """
    by_env = {}
    for a in agents:
        by_env.setdefault(a["environment_id"], {})[a["agent_key"]] = a["agent_name"]

    pp = token("https://service.powerapps.com/")
    if not pp:
        return []
    status, payload = http(
        "GET", "https://api.bap.microsoft.com/providers/Microsoft.BusinessAppPlatform/"
        "scopes/admin/environments?api-version=2020-10-01", pp)
    if status != 200:
        return []

    rows = []
    for env in payload.get("value", []):
        props = env.get("properties", {})
        api = (props.get("linkedEnvironmentMetadata") or {}).get("instanceApiUrl")
        if not api:
            continue
        dv = token(api.rstrip("/") + "/")
        if not dv:
            continue
        env_id = env.get("name", "")
        names = by_env.get(env_id, {})
        events = paged(
            f"{api.rstrip('/')}/api/data/v9.2/msdyn_aievents"
            f"?$select=msdyn_aieventid,msdyn_name,msdyn_creditconsumed,"
            f"msdyn_consumptionsource,msdyn_datatype,msdyn_processingdate,createdon", dv)
        # Collapse to one row per day x agent x action — the bronze grain the
        # mock feed already declares, so the two union without reshaping.
        grouped = {}
        for e in events:
            day = (e.get("msdyn_processingdate") or e.get("createdon") or "")[:10]
            agent = (e.get("_msdyn_aimodelid_value") or e.get("msdyn_name")
                     or "unknown")
            action = str(e.get("msdyn_datatype")
                         or e.get("msdyn_consumptionsource") or "copilot_event")
            acc = grouped.setdefault((day, agent, action),
                                     {"credits": 0.0, "sessions": 0})
            acc["credits"] += float(e.get("msdyn_creditconsumed") or 0)
            acc["sessions"] += 1
        for (day, agent, action), acc in grouped.items():
            rows.append({
                "usage_date": day,
                "environment_id": env_id,
                "agent_id": agent,
                "agent_name": names.get(agent, agent),
                "action_type": action,
                "credit_rate": 1,
                "credits_consumed": acc["credits"],
                "cost_usd": 0,
                "session_count": acc["sessions"],
                "owner_business_unit_key": "",
                **lineage("dataverse.msdyn_aievent",
                          day or NOW.date().isoformat(), tenant_domain),
            })
        print(f"      {props.get('displayName', '?')[:34]:36} "
              f"{len(events)} AI event(s)")
    return rows


# ---------------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tenant", help="tenant id (default: current az context)")
    ap.add_argument("--out", default=DEFAULT_OUT, help="output directory")
    ap.add_argument("--days", type=int, default=90,
                    help="lookback window (Azure Monitor retains 93 days)")
    ap.add_argument("--probe", action="store_true",
                    help="report what is available; write nothing")
    ap.add_argument("--i-understand-this-is-real-tenant-data", action="store_true",
                    dest="confirmed",
                    help="required to write: acknowledges the output contains real "
                         "tenant cost and identity data and must not be committed")
    args = ap.parse_args()

    arm_tok = token(ARM, args.tenant)
    if not arm_tok:
        sys.exit("Could not get an Azure token. Run:\n"
                 "  az login --tenant <tenant-id>")
    graph_tok = token(GRAPH, args.tenant)

    who = subprocess.run([az_exe(), "account", "show", "-o", "json"],
                         capture_output=True, text=True)
    acct = json.loads(who.stdout) if who.returncode == 0 else {}
    tenant_domain = acct.get("user", {}).get("name", "unknown").split("@")[-1]
    print(f"tenant : {acct.get('tenantId', '?')}")
    print(f"user   : {acct.get('user', {}).get('name', '?')}")
    print(f"window : last {args.days} days")
    print()

    status, payload = http("GET", f"{ARM}/subscriptions?api-version=2022-12-01", arm_tok)
    subs = [s["subscriptionId"] for s in payload.get("value", [])] if status == 200 else []
    print(f"subscriptions: {len(subs)}")

    print("\nAzure AI resources ...")
    resources = collect_ai_resources(arm_tok)
    kinds: dict[str, int] = {}
    for r in resources:
        kinds[r.get("kind", "?")] = kinds.get(r.get("kind", "?"), 0) + 1
    print(f"  {len(resources)} account(s): {kinds}")

    print("\nAzure Monitor telemetry ...")
    metrics = collect_ai_metrics(arm_tok, resources, min(args.days, 92), tenant_domain)
    tok_total = sum(r["total_tokens"] for r in metrics)
    req_total = sum(r["requests"] for r in metrics)
    print(f"  {len(metrics)} resource-day row(s); "
          f"{tok_total:,} tokens, {req_total:,} requests")
    if metrics and tok_total == 0:
        print("  ! no token metrics populated -- these workloads bill by call, not "
              "token.\n    Tokenomics stays MOCK until token-billed traffic exists.")

    print("\nAzure cost (Consumption usageDetails) ...")
    cost = collect_cost(arm_tok, subs, min(args.days, 90), tenant_domain)
    print(f"  {len(cost)} row(s), ${sum(r['cost_usd'] for r in cost):,.2f} total")
    cost_days = sorted({r["usage_date"] for r in cost if r["usage_date"]})
    if cost_days:
        print(f"  {len(cost_days)} day(s): {cost_days[0]} -> {cost_days[-1]}")
        # A silently-ignored date scope collapses the window to the open billing
        # period, which looks like a successful extract. Say so out loud.
        if len(cost_days) < min(args.days, 28):
            print(f"  ! only {len(cost_days)} day(s) returned for a "
                  f"{args.days}-day request — the subscription may have no older "
                  f"usage, or the date scope was ignored.")
    ai_cost = sum(r["cost_usd"] for r in cost
                  if "cognitiveservices" in r["resource_id"].lower()
                  or "machinelearning" in r["consumed_service"].lower()
                  or "cognitiveservices" in r["consumed_service"].lower())
    print(f"  of which AI/ML: ${ai_cost:,.2f}")

    identities = licences = agents = []
    if graph_tok:
        print("\nEntra identities ...")
        identities = collect_identities(graph_tok, tenant_domain)
        print(f"  {len(identities)} identity row(s)")
        print("\nM365 licence inventory ...")
        licences = collect_licenses(graph_tok, tenant_domain)
        print(f"  {len(licences)} SKU(s)")
    else:
        print("\n! no Graph token -- skipping identities and licences")

    print("\nCopilot Studio agents ...")
    agents = collect_agents(tenant_domain)
    print(f"  {len(agents)} agent(s)")

    print("\nCopilot Studio credit consumption (msdyn_aievent) ...")
    studio = collect_studio_credits(tenant_domain, agents)
    credits_total = sum(r["credits_consumed"] for r in studio)
    print(f"  {len(studio)} row(s), {credits_total:,.0f} credit(s)")
    if not studio:
        print("  ! zero consumption: the agents exist but have never been run.\n"
              "    This is the tenant's true state, not a collector failure — no\n"
              "    API can extract spend that was never incurred. Publish an agent\n"
              "    and hold a conversation with it to generate real credits.")

    # Application inventory = every resource that either is an AI account or
    # actually incurred cost. Deriving it from the cost rows as well as from
    # Resource Graph is what lets real spend attribute to a real workload
    # instead of collapsing into APP-UNKNOWN.
    apps_by_key = {}

    def add_app(name, kind, source_api):
        key = app_key(name)
        if key in apps_by_key:
            return
        apps_by_key[key] = {
            "application_key": key,
            "application_name": name,
            "application_type": kind or "azure-resource",
            "owner_upn": "",
            "owner_business_unit_key": "",
            "environment": "dev" if "-dev" in name.lower() else "prod",
            "criticality": "Unknown",
            **lineage(source_api, NOW.date().isoformat(), tenant_domain),
        }

    for r in resources:
        add_app(r["name"], r.get("kind", ""), "resourcegraph.cognitiveservices")
    for c in cost:
        if c["resource_name"]:
            add_app(c["resource_name"], c["consumed_service"],
                    "consumption.usageDetails")
    apps = list(apps_by_key.values())
    # A duplicate application_key makes Power BI reject the whole
    # dim_application relationship, so fail here rather than three layers later.
    collisions = {}
    for a in apps:
        collisions.setdefault(a["application_key"], set()).add(a["application_name"])
    dupes = {k: v for k, v in collisions.items() if len(v) > 1}
    if dupes:
        sys.exit(f"application_key collision(s): {dupes}")

    if args.probe:
        print("\n--probe: nothing written.")
        return 0

    if not args.confirmed:
        print(f"\nRefusing to write. This extract contains REAL cost and identity data")
        print(f"for '{tenant_domain}'. Re-run with")
        print("  --i-understand-this-is-real-tenant-data")
        print(f"to write to {args.out} (gitignored).")
        return 2

    print(f"\nWriting REAL bronze to {args.out}")
    write(args.out, "bronze_azure_cost", cost)
    write(args.out, "bronze_azure_ai_metrics", metrics)
    write(args.out, "bronze_ref_app_inventory", apps)
    if identities:
        write(args.out, "bronze_ref_identity_map", identities)
    if licences:
        write(args.out, "bronze_m365_license_inventory", licences)
    if agents:
        write(args.out, "bronze_ref_agent_inventory", agents)
    # Written even when empty, with an explicit schema: a zero-row REAL feed is
    # a meaningful statement ("nothing has run yet"), and the table has to exist
    # for silver to union it the moment it fills.
    write(args.out, "bronze_studio_credits", studio, STUDIO_CREDIT_COLUMNS)

    print("\nEvery row is tagged _data_class='REAL'. Gold derives")
    print("dim_platform[data_source] from it, so the Governance page updates itself.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
