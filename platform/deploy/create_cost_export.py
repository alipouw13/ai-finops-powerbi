#!/usr/bin/env python3
"""
Create real Azure Cost Management FOCUS exports for the AI FinOps accelerator.

This script replaces the accelerator's MOCK cost data path with Azure Cost
Management exports written to ADLS Gen2. It is intentionally dependency-free and
uses the same convention as the Fabric deployment helpers in this repo: tokens
come from the Azure CLI, HTTP calls use urllib.request, and each step is
idempotent so the script can be re-run safely.

Flow:
  1. Get an ARM token from `az account get-access-token`.
  2. Ensure the resource group exists.
  3. Ensure a StorageV2 account exists with hierarchical namespace enabled.
  4. Optionally create a resource-group-scoped policy exemption, then patch the
     storage account to enable public network access and shared-key auth.
  5. Create the export container through the ARM management plane.
  6. Create a daily MonthToDate FOCUS 1.0 parquet export.
  7. Create inactive one-month Custom FOCUS 1.0 backfill exports.
  8. Trigger each export and poll run history to terminal state.

Governance blocker:
  In the default tenant, the MCAPSGovDeployPolicies assignment at management
  group scope has a Modify effect that silently rewrites storage accounts with
  publicNetworkAccess=Disabled and allowSharedKeyAccess=false. Azure Cost
  Management exports currently require key-based access to the destination
  storage account and fail with HTTP 400:
    "Key-based authentication is currently disabled on this storage account."
  The workaround is a Waiver policy exemption scoped to the resource group,
  followed by a storage PATCH that re-enables shared-key access. Because policy
  propagation can lag, this script reads the storage account back and retries
  before failing with an actionable tenant-admin message.

FOCUS dataset:
  Exports use definition.type=FocusCost with dataVersion=1.0, daily granularity,
  Parquet format, partitionData=true, and OverwritePreviousReport. This matches
  the FinOps Open Cost and Usage Specification shape expected by the accelerator
  while avoiding CSV parsing drift.

Examples:
  python platform/deploy/create_cost_export.py --help
  python platform/deploy/create_cost_export.py --steps resource-group storage
  python platform/deploy/create_cost_export.py --dry-run
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

ARM = "https://management.azure.com"
DEFAULT_SUBSCRIPTION = "a699796c-ab5c-48bf-8bd7-adb31e225f11"
DEFAULT_TENANT = "840a80c0-e4a5-47be-8a1d-7ecfa61e839c"
DEFAULT_RESOURCE_GROUP = "rg-finops-costexport"
DEFAULT_LOCATION = "eastus2"
DEFAULT_STORAGE_ACCOUNT = "stfinopscost848055"
DEFAULT_CONTAINER = "costexports"
DEFAULT_ROOT_FOLDER = "focus"
DEFAULT_EXPORT_NAME = "finops-focus-daily"
DEFAULT_POLICY_ASSIGNMENT_ID = (
    "/providers/Microsoft.Management/managementGroups/"
    "840a80c0-e4a5-47be-8a1d-7ecfa61e839c/providers/"
    "Microsoft.Authorization/policyAssignments/MCAPSGovDeployPolicies"
)
DEFAULT_EXEMPTION_NAME = "finops-costexport-sharedkey-waiver"

ALL_STEPS = [
    "resource-group",
    "storage",
    "exemption",
    "storage-access",
    "container",
    "export",
    "backfill",
    "run",
    "history",
]
TERMINAL_RUN_STATES = ("Completed", "Failed", "Canceled", "Cancelled")


def env(name, default):
    return os.environ.get(name, default)


def die(msg, code=1):
    print("! " + msg, file=sys.stderr)
    sys.exit(code)


def arm_id(args):
    return (
        f"/subscriptions/{args.subscription}/resourceGroups/{args.resource_group}"
        f"/providers/Microsoft.Storage/storageAccounts/{args.storage_account}"
    )


def url_for(path, api_version):
    sep = "&" if "?" in path else "?"
    return ARM + path + sep + "api-version=" + api_version


def _az_exe():
    """Resolve the Azure CLI entry point.

    On Windows `az` is a .cmd shim, which subprocess will not find without a
    PATHEXT-aware lookup, so probe both names rather than assuming POSIX.
    """
    for name in ("az", "az.cmd"):
        found = shutil.which(name)
        if found:
            return found
    die("Azure CLI (`az`) not found. Install it, then run `az login`.")


def az_token(tenant):
    cmd = [
        _az_exe(),
        "account",
        "get-access-token",
        "--resource",
        ARM,
        "--tenant",
        tenant,
        "--query",
        "accessToken",
        "-o",
        "tsv",
    ]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError:
        die("Azure CLI (`az`) not found. Install it, then run `az login`.")
    if out.returncode != 0:
        die("token failed for ARM; run `az login` first:\n" + out.stderr.strip())
    return out.stdout.strip()


def req(method, url, token, body=None, dry_run=False):
    if dry_run:
        print(f"  = dry-run {method} {url}")
        if body is not None:
            print("    " + json.dumps(body, sort_keys=True))
        return 200, {}

    data = None
    headers = {"Authorization": "Bearer " + token}
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    r = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(r) as resp:
            payload = resp.read()
            if not payload:
                return resp.status, {}
            return resp.status, json.loads(payload)
    except urllib.error.HTTPError as e:
        body_txt = e.read().decode(errors="replace")
        print(f"! HTTP {e.code} {method} {url}", file=sys.stderr)
        print(body_txt, file=sys.stderr)
        return e.code, body_txt
    except urllib.error.URLError as e:
        print(f"! URL error {method} {url}: {e}", file=sys.stderr)
        return 0, str(e)


def require_ok(status, body, label, ok=(200, 201, 202, 204)):
    if status not in ok:
        die(f"{label} failed: HTTP {status} {body}")


def created_marker(status):
    return "+" if status in (201, 202) else "="


def ensure_resource_group(args, token):
    print(f"+ resource-group: ensuring {args.resource_group} in {args.location}")
    path = f"/subscriptions/{args.subscription}/resourcegroups/{args.resource_group}"
    body = {"location": args.location}
    st, resp = req("PUT", url_for(path, "2021-04-01"), token, body, args.dry_run)
    require_ok(st, resp, "resource group")
    print(f"  {created_marker(st)} resource group {args.resource_group}")


def get_storage(args, token):
    path = (
        f"/subscriptions/{args.subscription}/resourceGroups/{args.resource_group}"
        f"/providers/Microsoft.Storage/storageAccounts/{args.storage_account}"
    )
    return req("GET", url_for(path, "2023-05-01"), token, dry_run=args.dry_run)


def ensure_storage(args, token):
    print(f"+ storage: ensuring ADLS Gen2 account {args.storage_account}")
    path = (
        f"/subscriptions/{args.subscription}/resourceGroups/{args.resource_group}"
        f"/providers/Microsoft.Storage/storageAccounts/{args.storage_account}"
    )
    body = {
        "location": args.location,
        "kind": "StorageV2",
        "sku": {"name": "Standard_LRS"},
        "properties": {
            "isHnsEnabled": True,
            "minimumTlsVersion": "TLS1_2",
        },
    }
    st, resp = req("PUT", url_for(path, "2023-05-01"), token, body, args.dry_run)
    require_ok(st, resp, "storage account")
    print(f"  {created_marker(st)} storage account PUT accepted")
    poll_storage_succeeded(args, token)


def poll_storage_succeeded(args, token):
    if args.dry_run:
        print("  = dry-run storage provisioning poll skipped")
        return
    deadline = time.time() + args.storage_timeout_seconds
    while time.time() < deadline:
        st, body = get_storage(args, token)
        require_ok(st, body, "read storage account", ok=(200,))
        state = body.get("properties", {}).get("provisioningState")
        print(f"  = storage provisioningState={state}")
        if state == "Succeeded":
            return
        if state in ("Failed", "Canceled", "Cancelled"):
            die(f"storage provisioning ended in {state}: {body}")
        time.sleep(args.poll_seconds)
    die(f"storage account did not reach Succeeded within {args.storage_timeout_seconds}s")


def ensure_policy_exemption(args, token):
    if args.skip_exemption:
        print("= exemption: skipped by --skip-exemption")
        return
    print(f"+ exemption: ensuring policy waiver {args.exemption_name}")
    path = (
        f"/subscriptions/{args.subscription}/resourceGroups/{args.resource_group}"
        f"/providers/Microsoft.Authorization/policyExemptions/{args.exemption_name}"
    )
    body = {
        "properties": {
            "policyAssignmentId": args.policy_assignment_id,
            "exemptionCategory": "Waiver",
        }
    }
    st, resp = req("PUT", url_for(path, "2022-07-01-preview"), token, body, args.dry_run)
    require_ok(st, resp, "policy exemption")
    print(f"  {created_marker(st)} policy exemption {args.exemption_name}")


def patch_storage_access(args, token):
    print("+ storage-access: enabling public network + shared key")
    path = (
        f"/subscriptions/{args.subscription}/resourceGroups/{args.resource_group}"
        f"/providers/Microsoft.Storage/storageAccounts/{args.storage_account}"
    )
    body = {
        "properties": {
            "publicNetworkAccess": "Enabled",
            "allowSharedKeyAccess": True,
            "networkAcls": {"defaultAction": "Allow", "bypass": "AzureServices"},
        }
    }
    url = url_for(path, "2023-05-01")
    for attempt in range(1, args.policy_retries + 1):
        st, resp = req("PATCH", url, token, body, args.dry_run)
        require_ok(st, resp, "storage access patch")
        if args.dry_run:
            print("  = dry-run storage access verification skipped")
            return
        time.sleep(args.policy_retry_seconds)
        gst, storage = get_storage(args, token)
        require_ok(gst, storage, "verify storage access", ok=(200,))
        props = storage.get("properties", {})
        shared_key = props.get("allowSharedKeyAccess")
        public_network = props.get("publicNetworkAccess")
        default_action = props.get("networkAcls", {}).get("defaultAction")
        print(
            "  = verify attempt %d/%d: publicNetworkAccess=%s "
            "allowSharedKeyAccess=%s defaultAction=%s"
            % (attempt, args.policy_retries, public_network, shared_key, default_action)
        )
        if shared_key is True and public_network == "Enabled":
            print("  + storage account accepts Cost Management export requirements")
            return
    die(
        "allowSharedKeyAccess is still false after retries. A management-group "
        "policy is likely modifying the storage account. Ask a tenant admin for "
        "a policy exemption/waiver on the resource group, then rerun this step."
    )


def ensure_container(args, token):
    print(f"+ container: ensuring {args.container}")
    path = (
        f"/subscriptions/{args.subscription}/resourceGroups/{args.resource_group}"
        f"/providers/Microsoft.Storage/storageAccounts/{args.storage_account}"
        f"/blobServices/default/containers/{args.container}"
    )
    # Use the ARM management plane instead of the blob data plane. If governance
    # disables public network access, data-plane calls can be unreachable while
    # ARM control-plane calls still work, making this ordering robust.
    st, resp = req("PUT", url_for(path, "2023-05-01"), token, {}, args.dry_run)
    require_ok(st, resp, "container")
    print(f"  {created_marker(st)} container {args.container}")


def iso(dt):
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def daily_export_body(args):
    start = datetime.now(timezone.utc) + timedelta(days=args.start_offset_days)
    end = start + timedelta(days=365)
    return export_body(
        args,
        status="Active",
        timeframe="MonthToDate",
        recurrence_from=iso(start),
        recurrence_to=iso(end),
    )


def export_body(args, status, timeframe, recurrence_from, recurrence_to, time_period=None):
    definition = {
        "type": "FocusCost",
        "timeframe": timeframe,
        "dataSet": {
            "granularity": "Daily",
            "configuration": {"dataVersion": "1.0"},
        },
    }
    if time_period:
        definition["timePeriod"] = time_period
    return {
        "properties": {
            "schedule": {
                "status": status,
                "recurrence": "Daily",
                "recurrencePeriod": {"from": recurrence_from, "to": recurrence_to},
            },
            "format": "Parquet",
            "partitionData": True,
            "dataOverwriteBehavior": "OverwritePreviousReport",
            "compressionMode": "None",
            "deliveryInfo": {
                "destination": {
                    "resourceId": arm_id(args),
                    "container": args.container,
                    "rootFolderPath": args.root_folder,
                }
            },
            "definition": definition,
        }
    }


def export_url(args, name):
    path = f"/subscriptions/{args.subscription}/providers/Microsoft.CostManagement/exports/{name}"
    return url_for(path, "2023-07-01-preview")


def ensure_daily_export(args, token):
    print(f"+ export: ensuring daily export {args.export_name}")
    st, resp = req("PUT", export_url(args, args.export_name), token, daily_export_body(args), args.dry_run)
    require_ok(st, resp, "daily Cost Management export")
    print(f"  {created_marker(st)} export {args.export_name}")


def add_months(year, month, delta):
    idx = year * 12 + (month - 1) + delta
    return idx // 12, idx % 12 + 1


def month_window(year, month):
    next_year, next_month = add_months(year, month, 1)
    start = datetime(year, month, 1, tzinfo=timezone.utc)
    end = datetime(next_year, next_month, 1, tzinfo=timezone.utc) - timedelta(seconds=1)
    return start, end


def backfill_specs(args):
    now = datetime.now(timezone.utc)
    specs = []
    for i in range(args.backfill_months):
        year, month = add_months(now.year, now.month, -(i + 1))
        start, end = month_window(year, month)
        name = "finops-focus-bf-%04d%02d" % (year, month)
        body = export_body(
            args,
            status="Inactive",
            timeframe="Custom",
            recurrence_from=iso(datetime.now(timezone.utc) + timedelta(days=args.start_offset_days)),
            recurrence_to=iso(datetime.now(timezone.utc) + timedelta(days=365)),
            time_period={"from": iso(start), "to": iso(end)},
        )
        specs.append((name, body))
    return specs


def ensure_backfill_exports(args, token):
    print(f"+ backfill: ensuring {args.backfill_months} monthly exports")
    for name, body in backfill_specs(args):
        st, resp = req("PUT", export_url(args, name), token, body, args.dry_run)
        require_ok(st, resp, f"backfill export {name}")
        period = body["properties"]["definition"]["timePeriod"]
        print(f"  {created_marker(st)} {name} {period['from']}..{period['to']}")


def export_names(args):
    names = [args.export_name]
    names.extend(name for name, _ in backfill_specs(args))
    return names


def trigger_runs(args, token):
    print("+ run: triggering exports")
    for name in export_names(args):
        url = export_url(args, name).replace("?api-version=", "/run?api-version=")
        st, resp = req("POST", url, token, {}, args.dry_run)
        require_ok(st, resp, f"run export {name}", ok=(200, 201, 202))
        print(f"  + triggered {name}")


def latest_run_status(args, token, name):
    url = export_url(args, name).replace("?api-version=", "/runHistory?api-version=")
    st, body = req("GET", url, token, dry_run=args.dry_run)
    require_ok(st, body, f"run history {name}", ok=(200,))
    if args.dry_run:
        return "Completed", None, None
    runs = body.get("value", [])
    if not runs:
        return "NoRuns", None, None
    run = runs[0]
    props = run.get("properties", {})
    return (
        props.get("status", "Unknown"),
        props.get("processingStartTime"),
        props.get("processingEndTime"),
    )


def poll_run_history(args, token):
    print("+ history: polling export run history")
    pending = set(export_names(args))
    deadline = time.time() + args.run_timeout_seconds
    while pending and time.time() < deadline:
        for name in list(pending):
            status, start, end = latest_run_status(args, token, name)
            print(f"  = {name}: status={status} start={start} end={end}")
            if status in TERMINAL_RUN_STATES:
                if status != "Completed":
                    print(f"  ! {name} ended with status={status}")
                pending.remove(name)
        if pending:
            time.sleep(args.poll_seconds)
    if pending:
        die("run history timed out waiting for: " + ", ".join(sorted(pending)))


def parse_args():
    ap = argparse.ArgumentParser(
        description="Create Azure Cost Management FOCUS exports for AI FinOps."
    )
    ap.add_argument("--subscription", default=env("AZURE_SUBSCRIPTION_ID", DEFAULT_SUBSCRIPTION))
    ap.add_argument("--tenant", default=env("AZURE_TENANT_ID", DEFAULT_TENANT))
    ap.add_argument("--resource-group", default=env("FINOPS_COST_RG", DEFAULT_RESOURCE_GROUP))
    ap.add_argument("--location", default=env("FINOPS_COST_LOCATION", DEFAULT_LOCATION))
    ap.add_argument("--storage-account", default=env("FINOPS_COST_STORAGE", DEFAULT_STORAGE_ACCOUNT))
    ap.add_argument("--container", default=env("FINOPS_COST_CONTAINER", DEFAULT_CONTAINER))
    ap.add_argument("--root-folder", default=env("FINOPS_COST_ROOT_FOLDER", DEFAULT_ROOT_FOLDER))
    ap.add_argument("--export-name", default=env("FINOPS_COST_EXPORT_NAME", DEFAULT_EXPORT_NAME))
    ap.add_argument("--backfill-months", type=int, default=int(env("FINOPS_BACKFILL_MONTHS", "3")))
    ap.add_argument("--skip-exemption", action="store_true",
                    default=env("FINOPS_SKIP_EXEMPTION", "").lower() in ("1", "true", "yes"))
    ap.add_argument("--policy-assignment-id",
                    default=env("FINOPS_POLICY_ASSIGNMENT_ID", DEFAULT_POLICY_ASSIGNMENT_ID))
    ap.add_argument("--exemption-name", default=env("FINOPS_EXEMPTION_NAME", DEFAULT_EXEMPTION_NAME))
    ap.add_argument("--steps", nargs="+", default=ALL_STEPS,
                    help="subset of: %s (default: all)" % " ".join(ALL_STEPS))
    ap.add_argument("--dry-run", action="store_true",
                    help="print ARM calls without sending them")
    ap.add_argument("--policy-retries", type=int, default=int(env("FINOPS_POLICY_RETRIES", "10")))
    ap.add_argument("--policy-retry-seconds", type=int,
                    default=int(env("FINOPS_POLICY_RETRY_SECONDS", "15")))
    ap.add_argument("--poll-seconds", type=int, default=int(env("FINOPS_POLL_SECONDS", "30")))
    ap.add_argument("--storage-timeout-seconds", type=int,
                    default=int(env("FINOPS_STORAGE_TIMEOUT_SECONDS", "1800")))
    ap.add_argument("--run-timeout-seconds", type=int,
                    default=int(env("FINOPS_RUN_TIMEOUT_SECONDS", "3600")))
    ap.add_argument("--start-offset-days", type=int,
                    default=int(env("FINOPS_START_OFFSET_DAYS", "1")),
                    help="days from now for future recurrencePeriod.from")
    args = ap.parse_args()

    unknown = [s for s in args.steps if s not in ALL_STEPS]
    if unknown:
        die("unknown --steps value(s): %s; choose from %s"
            % (", ".join(unknown), " ".join(ALL_STEPS)))
    if args.backfill_months < 0:
        die("--backfill-months must be >= 0")
    return args


def main():
    args = parse_args()
    print("Azure Cost Management export setup")
    print(f"= subscription {args.subscription}")
    print(f"= tenant       {args.tenant}")
    print(f"= storage      {arm_id(args)}")
    token = "dry-run-token" if args.dry_run else az_token(args.tenant)

    if "resource-group" in args.steps:
        ensure_resource_group(args, token)
    if "storage" in args.steps:
        ensure_storage(args, token)
    if "exemption" in args.steps:
        ensure_policy_exemption(args, token)
    if "storage-access" in args.steps:
        patch_storage_access(args, token)
    if "container" in args.steps:
        ensure_container(args, token)
    if "export" in args.steps:
        ensure_daily_export(args, token)
    if "backfill" in args.steps:
        ensure_backfill_exports(args, token)
    if "run" in args.steps:
        trigger_runs(args, token)
    if "history" in args.steps:
        poll_run_history(args, token)

    print("+ complete")


if __name__ == "__main__":
    main()
