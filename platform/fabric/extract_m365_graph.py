#!/usr/bin/env python3
"""REAL M365 Bronze extractor — Microsoft Graph, no mock data.

Drop-in replacement for the M365 slice of gen_bronze_data.py. Emits the exact
Bronze schemas documented in docs/bronze-layer-architecture.md, so Silver/Gold
consume it unchanged — only the lineage columns differ (_data_class=REAL).

  bronze_m365_license_inventory  tenant SKU entitlement   (Graph subscribedSkus)
  bronze_m365_copilot_seats      per-user seat assignment (Graph users)
  bronze_m365_copilot_usage      per-user activity dates  (Graph usage report)

WHAT IS AND IS NOT REAL
-----------------------
Graph exposes *entitlement and activity*, never invoiced dollars. There is no
Graph endpoint that returns what Microsoft billed you for M365 Copilot. So:

  seat COUNTS      -> REAL      (subscribedSkus.prepaidUnits / consumedUnits)
  seat ACTIVITY    -> REAL      (getMicrosoft365CopilotUsageUserDetail)
  seat DOLLARS     -> MODELLED  (count x published list price, from the rate card)

That is why every cost derived from this feed carries cost_is_estimated=TRUE.
Swap dim_rate_card for the customer's actual EA/MCA price sheet and the dollars
become contract-accurate; the connector itself does not change.

A tenant with zero Copilot licences still proves the connector: the SKU
inventory and the per-user licence graph come back real and populated, and the
Copilot tables come back legitimately empty rather than fabricated.

PERMISSIONS (delegated, via az CLI)
  Organization.Read.All   subscribedSkus
  User.Read.All           users + assignedLicenses
  Reports.Read.All        Copilot usage report

USAGE
  az login --tenant <tenant-id>
  python platform/fabric/extract_m365_graph.py --probe
  python platform/fabric/extract_m365_graph.py --out platform/fabric/bronze_out
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

GRAPH = "https://graph.microsoft.com/v1.0"
GRAPH_BETA = "https://graph.microsoft.com/beta"
HERE = os.path.dirname(os.path.abspath(__file__))
# REAL extracts land in a gitignored directory, separate from the committed MOCK
# bronze_out/. A production tenant's licence counts must never be committed.
DEFAULT_OUT = os.path.join(HERE, "bronze_real")

# The Copilot usage report is NOT in Graph v1.0 — v1.0 returns
# 400 "Resource not found for the segment". Microsoft is moving it to the
# /copilot report root, so try that first and fall back to /beta/reports.
USAGE_ENDPOINTS = [
    (GRAPH_BETA + "/copilot/reports/getMicrosoft365CopilotUsageUserDetail(period='D7')",
     "graph.beta.copilot.reports.getMicrosoft365CopilotUsageUserDetail"),
    (GRAPH_BETA + "/reports/getMicrosoft365CopilotUsageUserDetail(period='D7')",
     "graph.beta.reports.getMicrosoft365CopilotUsageUserDetail"),
]

# Any SKU whose part number contains one of these is an AI/Copilot SKU. Tenants
# name these inconsistently, so match on substrings rather than fixed GUIDs.
COPILOT_MARKERS = ("COPILOT", "M365_COPILOT", "POWER_VIRTUAL_AGENT", "CCIQ")


def az_exe():
    exe = shutil.which("az") or shutil.which("az.cmd")
    if not exe:
        sys.exit("Azure CLI (`az`) not found on PATH. Install it and run `az login`.")
    return exe


def az_token(tenant=None):
    cmd = [az_exe(), "account", "get-access-token",
           "--resource", "https://graph.microsoft.com",
           "--query", "accessToken", "-o", "tsv"]
    if tenant:
        cmd += ["--tenant", tenant]
    out = subprocess.run(cmd, capture_output=True, text=True)
    if out.returncode != 0:
        tenant_arg = "--tenant %s " % tenant if tenant else ""
        sys.exit(
            "Could not get a Microsoft Graph token for this tenant.\n\n"
            "Sign in, then re-run:\n"
            "  az login %s--scope https://graph.microsoft.com/.default\n\n"
            "On a machine with no browser, add --use-device-code:\n"
            "  az login %s--use-device-code --allow-no-subscriptions\n\n"
            "az said:\n%s" % (tenant_arg, tenant_arg, out.stderr.strip()))
    return out.stdout.strip()


def graph_get(path, token):
    """GET a Graph path, following @odata.nextLink. Returns (status, items|error)."""
    url = path if path.startswith("http") else GRAPH + path
    items, page = [], 0
    while url:
        req = urllib.request.Request(url, headers={
            "Authorization": "Bearer " + token,
            "ConsistencyLevel": "eventual",
        })
        try:
            with urllib.request.urlopen(req) as resp:
                body = resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode(errors="replace")
        if not body:
            return 200, items
        payload = json.loads(body)
        if "value" in payload:
            items.extend(payload["value"])
        else:
            return 200, payload
        url = payload.get("@odata.nextLink")
        page += 1
        if page > 50:  # safety valve on very large tenants
            break
    return 200, items


def graph_get_csv(url, token):
    """The reports endpoints answer 302 -> a reports.office.com blob holding CSV.
    urllib follows the redirect for us."""
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + token})
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, resp.read().decode("utf-8-sig", errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")


def lineage(source_api, watermark, tenant_domain):
    return {
        "_ingested_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "_source_system": "MicrosoftGraph/" + tenant_domain,
        "_source_api": source_api,
        "_watermark": watermark,
        "_batch_id": "graph-" + dt.date.today().isoformat(),
        "_data_class": "REAL",
    }


def write(out_dir, name, rows, columns=None):
    os.makedirs(out_dir, exist_ok=True)
    cols = columns or (list(rows[0].keys()) if rows else [])
    path = os.path.join(out_dir, name + ".csv")
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    print("  %-34s %5d rows -> %s" % (name, len(rows), os.path.basename(path)))
    return len(rows)


# --------------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tenant", help="tenant id (default: current az context)")
    ap.add_argument("--out", default=DEFAULT_OUT, help="Bronze output directory")
    ap.add_argument("--probe", action="store_true",
                    help="report what Graph exposes; write nothing")
    ap.add_argument("--i-understand-this-is-real-tenant-data", action="store_true",
                    dest="confirmed",
                    help="required to write CSVs: acknowledges the output contains real "
                         "licence data for the signed-in tenant and must not be committed")
    args = ap.parse_args()

    token = az_token(args.tenant)

    status, org = graph_get("/organization", token)
    if status != 200:
        sys.exit("Graph /organization failed (%s): %s\n\nIf this is 403, the signed-in "
                 "account lacks Organization.Read.All." % (status, org))
    org0 = org[0] if isinstance(org, list) and org else {}
    domains = [d["name"] for d in org0.get("verifiedDomains", []) if d.get("isInitial")]
    tenant_domain = domains[0] if domains else org0.get("id", "unknown")
    print("tenant      : %s" % org0.get("displayName", "?"))
    print("domain      : %s" % tenant_domain)
    print("tenant id   : %s" % org0.get("id", "?"))
    print()

    # ---------------------------------------------------- 1. SKU entitlement (REAL)
    status, skus = graph_get("/subscribedSkus", token)
    if status != 200:
        sys.exit("subscribedSkus failed (%s): %s" % (status, skus))
    today = dt.date.today().isoformat()

    inventory = []
    for s in skus:
        pre = s.get("prepaidUnits", {}) or {}
        part = s.get("skuPartNumber", "")
        inventory.append({
            "snapshot_date": today,
            "sku_id": s.get("skuId", ""),
            "sku_part_number": part,
            "capability_status": s.get("capabilityStatus", ""),
            "applies_to": s.get("appliesTo", ""),
            "prepaid_enabled": pre.get("enabled", 0),
            "prepaid_warning": pre.get("warning", 0),
            "prepaid_suspended": pre.get("suspended", 0),
            "consumed_units": s.get("consumedUnits", 0),
            "is_ai_sku": "TRUE" if any(m in part.upper() for m in COPILOT_MARKERS) else "FALSE",
            "service_plans_enabled": ";".join(
                sp.get("servicePlanName", "") for sp in s.get("servicePlans", [])
                if sp.get("provisioningStatus") == "Success"),
            **lineage("graph.subscribedSkus", today, tenant_domain),
        })

    ai_skus = [r for r in inventory if r["is_ai_sku"] == "TRUE"]
    print("SKU inventory (REAL entitlement from Graph)")
    print("  %d subscribed SKU(s), %d AI/Copilot SKU(s)" % (len(inventory), len(ai_skus)))
    if ai_skus:
        print("  AI/Copilot SKUs:")
        for r in sorted(ai_skus, key=lambda x: -int(x["consumed_units"])):
            print("    %-42s enabled=%-8s consumed=%-8s"
                  % (r["sku_part_number"][:42], r["prepaid_enabled"], r["consumed_units"]))
    else:
        print("  (no AI/Copilot SKU in this tenant — seat cost will be 0 until one is bought)")
    print()

    ai_sku_ids = {r["sku_id"] for r in ai_skus}

    # ------------------------------------------------ 2. per-user assignment (REAL)
    status, users = graph_get(
        "/users?$select=id,userPrincipalName,displayName,assignedLicenses,accountEnabled,"
        "department,jobTitle,officeLocation&$top=999", token)
    if status != 200:
        print("! users query failed (%s) — seat table will be empty: %s" % (status, users))
        users = []

    seats = []
    for u in users:
        for lic in u.get("assignedLicenses", []) or []:
            sku_id = lic.get("skuId", "")
            if ai_sku_ids and sku_id not in ai_sku_ids:
                continue
            match = next((r for r in inventory if r["sku_id"] == sku_id), {})
            seats.append({
                "snapshot_date": today,
                "user_principal_name": u.get("userPrincipalName", ""),
                "sku_id": sku_id,
                "sku_part_number": match.get("sku_part_number", ""),
                "capability_status": "Enabled" if u.get("accountEnabled") else "Suspended",
                "assigned_date": "",  # not exposed on assignedLicenses; needs audit log
                "service_plans_enabled": match.get("service_plans_enabled", ""),
                **lineage("graph.users.assignedLicenses", today, tenant_domain),
            })

    print("Per-user licence graph (REAL)")
    print("  %d user(s) enumerated, %d AI seat assignment(s)" % (len(users), len(seats)))
    print()

    # -------------------------------------------------- 3. Copilot usage (REAL/None)
    usage_rows = []
    print("Copilot usage report")
    status, csv_text, used_api = None, "", USAGE_ENDPOINTS[-1][1]
    for url, api_name in USAGE_ENDPOINTS:
        status, csv_text = graph_get_csv(url, token)
        used_api = api_name
        if status == 200:
            break
        print("  %s -> HTTP %s" % (url.split("/beta/")[-1][:52], status))

    if status == 200 and csv_text.strip():
        reader = csv.DictReader(csv_text.splitlines())

        def pick(row, *names):
            for n in names:
                if n in row and row[n]:
                    return row[n]
            return ""

        for row in reader:
            usage_rows.append({
                "report_date": pick(row, "Report Refresh Date"),
                "user_principal_name": pick(row, "User Principal Name"),
                "display_name": pick(row, "Display Name"),
                "last_activity_date": pick(row, "Last Activity Date"),
                "copilot_chat_last_activity": pick(row, "Copilot Chat Last Activity Date"),
                "teams_last_activity": pick(row, "Microsoft Teams Copilot Last Activity Date"),
                "word_last_activity": pick(row, "Word Copilot Last Activity Date"),
                "excel_last_activity": pick(row, "Excel Copilot Last Activity Date"),
                "powerpoint_last_activity": pick(row, "PowerPoint Copilot Last Activity Date"),
                "outlook_last_activity": pick(row, "Outlook Copilot Last Activity Date"),
                "onenote_last_activity": pick(row, "OneNote Copilot Last Activity Date"),
                "loop_last_activity": pick(row, "Loop Copilot Last Activity Date"),
                "report_period": pick(row, "Report Period") or "D7",
                **lineage(used_api, today, tenant_domain),
            })
        print("  ✓ %d row(s) returned via %s" % (len(usage_rows), used_api))
    elif status == 403:
        print("  403 Forbidden — the account lacks Reports.Read.All, or the tenant has")
        print("  report concealment on (Admin center > Settings > Org settings > Reports).")
    elif status in (400, 404):
        print("  No Copilot usage report in this tenant (HTTP %s)." % status)
        print("  Expected when the tenant holds 0 M365 Copilot seats — the report only")
        print("  exists once the service plan is provisioned. The SKU inventory and the")
        print("  per-user licence graph above are still REAL and prove the connector.")
    else:
        print("  HTTP %s: %s" % (status, str(csv_text)[:180]))
    print()

    if args.probe:
        print("--probe: no files written.")
        return 0

    if not args.confirmed:
        print("Refusing to write. This extract contains REAL licence data for")
        print("'%s'. Writing it into the repo would place tenant" % tenant_domain)
        print("information beside the committed MOCK demo data.")
        print()
        print("Re-run with --i-understand-this-is-real-tenant-data to write to")
        print("%s (gitignored)." % args.out)
        return 2

    print("Writing Bronze CSVs to %s" % args.out)
    write(args.out, "bronze_m365_license_inventory", inventory, list(inventory[0].keys()) if inventory else None)
    write(args.out, "bronze_m365_copilot_seats", seats, [
        "snapshot_date", "user_principal_name", "sku_id", "sku_part_number",
        "capability_status", "assigned_date", "service_plans_enabled",
        "_ingested_at", "_source_system", "_source_api", "_watermark",
        "_batch_id", "_data_class"])
    write(args.out, "bronze_m365_copilot_usage", usage_rows, [
        "report_date", "user_principal_name", "display_name", "last_activity_date",
        "copilot_chat_last_activity", "teams_last_activity", "word_last_activity",
        "excel_last_activity", "powerpoint_last_activity", "outlook_last_activity",
        "onenote_last_activity", "loop_last_activity", "report_period",
        "_ingested_at", "_source_system", "_source_api", "_watermark",
        "_batch_id", "_data_class"])

    print()
    print("Seat counts above are REAL. Dollars are applied downstream from")
    print("dim_rate_card (published list price) and stay cost_is_estimated=TRUE")
    print("until you load the customer's actual EA/MCA price sheet.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
