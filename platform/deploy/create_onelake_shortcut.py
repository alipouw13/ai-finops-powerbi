#!/usr/bin/env python3
"""
Create a Fabric OneLake shortcut over Azure Cost Management FOCUS exports.

This is the Fabric-side companion to platform/deploy/create_cost_export.py. The
Cost Management export writes Parquet files into an ADLS Gen2 storage account;
this script creates a shareable Fabric cloud connection and a Lakehouse shortcut
so the files appear in OneLake with zero copy.

Flow:
  1. Get Fabric, ARM, and OneLake/Storage tokens from the Azure CLI.
  2. If --connection-id was not supplied, get the storage account key through
     ARM management-plane listKeys. This intentionally avoids the storage data
     plane, which may be unreachable when public network access is disabled.
  3. Resolve the Fabric workspace and lakehouse by display name or id.
  4. Discover the Fabric AzureDataLakeStorage connector contract at runtime and
     validate that the AzureDataLakeStorage creation method requires server and
     path, and supports Key credentials.
  5. Reuse an existing cloud connection by displayName, or create it with the
     verified ShareableCloud AzureDataLakeStorage body.
  6. Create or overwrite the Lakehouse shortcut pointing at the ADLS Gen2 dfs
     endpoint and container subpath.
  7. Verify the shortcut exists, then list files through the OneLake DFS API and
     print file count plus total bytes. A zero-file warning usually means the
     Cost Management export has not completed yet.

Security note:
  The storage account key is held in memory only long enough to create the
  Fabric connection. It is never printed and never written to disk.

What this enables:
  Once the shortcut exists, platform/medallion/bronze/
  03_ingest_azure_costmgmt_focus.py reads
  Files/azure_costmgmt_focus/focus/*/*/*/part_*.parquet into
  finops_bronze.azure_costmgmt_focus_raw.

Examples:
  python platform/deploy/create_onelake_shortcut.py --help
  python platform/deploy/create_onelake_shortcut.py --steps connection
  python platform/deploy/create_onelake_shortcut.py --connection-id <guid> --steps shortcut verify
  python platform/deploy/create_onelake_shortcut.py --dry-run
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

ARM = "https://management.azure.com"
FABRIC = "https://api.fabric.microsoft.com/v1"
FABRIC_RESOURCE = "https://api.fabric.microsoft.com"
STORAGE_RESOURCE = "https://storage.azure.com"
ONELAKE = "https://onelake.dfs.fabric.microsoft.com"

DEFAULT_TENANT = "840a80c0-e4a5-47be-8a1d-7ecfa61e839c"
DEFAULT_WORKSPACE = "AI-tokenomics"
DEFAULT_WORKSPACE_ID = "0cb77528-27b4-4298-a48a-477caff105f3"
DEFAULT_LAKEHOUSE = "LH_tokenomics_bronze_real"
DEFAULT_LAKEHOUSE_ID = "0ac1d742-ef4b-49df-a5fc-4c5adf41370e"
DEFAULT_SUBSCRIPTION = "a699796c-ab5c-48bf-8bd7-adb31e225f11"
DEFAULT_RESOURCE_GROUP = "rg-finops-costexport"
DEFAULT_STORAGE_ACCOUNT = "stfinopscost848055"
DEFAULT_CONTAINER = "costexports"
DEFAULT_CONNECTION_NAME = "finops-costexports-adls"
DEFAULT_SHORTCUT_NAME = "azure_costmgmt_focus"

ALL_STEPS = ["connection", "shortcut", "verify"]
CONNECTOR_TYPE = "AzureDataLakeStorage"


def env(name, default):
    return os.environ.get(name, default)


def die(msg, code=1):
    print("! " + msg, file=sys.stderr)
    sys.exit(code)


def is_guidish(value):
    parts = value.split("-")
    return len(parts) == 5 and all(parts)


def quote(value):
    safe = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_.~"
    out = []
    for ch in value:
        if ch in safe:
            out.append(ch)
        else:
            for b in ch.encode():
                out.append("%%%02X" % b)
    return "".join(out)


def redact(obj):
    if isinstance(obj, dict):
        redacted = {}
        for key, value in obj.items():
            if key.lower() == "key":
                redacted[key] = "***"
            else:
                redacted[key] = redact(value)
        return redacted
    if isinstance(obj, list):
        return [redact(value) for value in obj]
    return obj


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


def az_token(resource, tenant):
    cmd = [
        _az_exe(),
        "account",
        "get-access-token",
        "--resource",
        resource,
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
        die("token failed for %s; run `az login` first:\n%s" % (resource, out.stderr.strip()))
    return out.stdout.strip()


def parse_payload(payload):
    if not payload:
        return {}
    text = payload.decode(errors="replace")
    try:
        return json.loads(text)
    except ValueError:
        return text


def req(method, url, token, body=None, dry_run=False):
    if dry_run:
        print("  = dry-run %s %s" % (method, url))
        if body is not None:
            print("    " + json.dumps(redact(body), sort_keys=True))
        return 200, {}, {}

    data = None
    headers = {"Authorization": "Bearer " + token}
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    r = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(r) as resp:
            payload = resp.read()
            return resp.status, parse_payload(payload), dict(resp.headers)
    except urllib.error.HTTPError as e:
        body_txt = e.read().decode(errors="replace")
        print("! HTTP %s %s %s" % (e.code, method, url), file=sys.stderr)
        print(body_txt, file=sys.stderr)
        return e.code, body_txt, {}
    except urllib.error.URLError as e:
        print("! URL error %s %s: %s" % (method, url, e), file=sys.stderr)
        return 0, str(e), {}


def require_ok(status, body, label, ok=(200, 201, 202, 204)):
    if status not in ok:
        die("%s failed: HTTP %s %s" % (label, status, body))


def created_marker(status):
    return "+" if status in (201, 202) else "="


def fabric_url(path):
    return FABRIC + path


def append_continuation(url, token):
    sep = "&" if "?" in url else "?"
    return url + sep + "continuationToken=" + quote(token)


def list_paged(url, token, dry_run, label):
    values = []
    next_url = url
    while next_url:
        st, body, _ = req("GET", next_url, token, dry_run=dry_run)
        require_ok(st, body, label, ok=(200,))
        if dry_run:
            return []
        if not isinstance(body, dict):
            die("%s returned a non-JSON response: %s" % (label, body))
        values.extend(body.get("value", []))
        continuation = body.get("continuationToken")
        continuation_uri = body.get("continuationUri")
        if continuation_uri:
            next_url = continuation_uri
        elif continuation:
            next_url = append_continuation(url, continuation)
        else:
            next_url = None
    return values


def storage_server(args):
    return "https://%s.dfs.core.windows.net" % args.storage_account


def list_keys(args, arm_token):
    print("+ keys: fetching storage account key via ARM listKeys")
    if args.connection_id:
        print("  = skipped because --connection-id was supplied")
        return None
    if args.dry_run:
        print("  = dry-run key retrieval skipped; placeholder key stays in memory only")
        return "dry-run-key"
    url = (
        "%s/subscriptions/%s/resourceGroups/%s/providers/Microsoft.Storage/"
        "storageAccounts/%s/listKeys?api-version=2023-05-01"
        % (ARM, args.subscription, args.resource_group, args.storage_account)
    )
    st, body, _ = req("POST", url, arm_token)
    require_ok(st, body, "list storage keys", ok=(200,))
    keys = body.get("keys", []) if isinstance(body, dict) else []
    if not keys or not keys[0].get("value"):
        die("ARM listKeys returned no keys for storage account %s" % args.storage_account)
    print("  + storage key acquired in memory only")
    return keys[0]["value"]


def resolve_workspace(args, fabric_token):
    if args.dry_run:
        ws_id = args.workspace if is_guidish(args.workspace) else DEFAULT_WORKSPACE_ID
        print("  = dry-run workspace resolved to %s" % ws_id)
        return ws_id
    workspaces = list_paged(fabric_url("/workspaces"), fabric_token, False, "list workspaces")
    for ws in workspaces:
        if args.workspace in (ws.get("id"), ws.get("displayName")):
            print("  = workspace %s (%s)" % (ws.get("displayName"), ws.get("id")))
            return ws["id"]
    die("workspace '%s' not found by displayName or id" % args.workspace)


def resolve_lakehouse(args, fabric_token, workspace_id):
    if args.dry_run:
        lh_id = args.lakehouse if is_guidish(args.lakehouse) else DEFAULT_LAKEHOUSE_ID
        print("  = dry-run lakehouse resolved to %s" % lh_id)
        return lh_id
    url = fabric_url("/workspaces/%s/items?type=Lakehouse" % workspace_id)
    lakehouses = list_paged(url, fabric_token, False, "list lakehouses")
    for item in lakehouses:
        if args.lakehouse in (item.get("id"), item.get("displayName")):
            print("  = lakehouse %s (%s)" % (item.get("displayName"), item.get("id")))
            return item["id"]
    die("lakehouse '%s' not found by displayName or id" % args.lakehouse)


def resolve_targets(args, fabric_token):
    print("+ resolve: workspace and lakehouse")
    workspace_id = resolve_workspace(args, fabric_token)
    lakehouse_id = resolve_lakehouse(args, fabric_token, workspace_id)
    return workspace_id, lakehouse_id


def values_named(items):
    names = []
    for item in items or []:
        if isinstance(item, str):
            names.append(item)
        elif isinstance(item, dict):
            names.append(item.get("name") or item.get("type") or item.get("credentialType"))
    return [name for name in names if name]


def required_parameter_names(method):
    params = method.get("parameters", [])
    names = []
    for param in params:
        if param.get("required") is False or param.get("isRequired") is False:
            continue
        names.append(param.get("name"))
    return sorted([name for name in names if name])


def discover_adls_contract(args, fabric_token):
    print("+ connector: validating AzureDataLakeStorage contract")
    if args.dry_run:
        print("  = dry-run connector discovery skipped")
        return
    url = fabric_url("/connections/supportedConnectionTypes?showAllCreationMethods=true")
    entries = list_paged(url, fabric_token, False, "list supported connection types")
    adls = None
    for entry in entries:
        if entry.get("type") == CONNECTOR_TYPE:
            adls = entry
            break
    if not adls:
        die("Fabric connector type %s was not returned; connector contract may have changed" %
            CONNECTOR_TYPE)

    creation_methods = adls.get("creationMethods", [])
    method = None
    for candidate in creation_methods:
        if candidate.get("name") == CONNECTOR_TYPE:
            method = candidate
            break
    if not method:
        die("Fabric %s creation method %s was not returned; contract changed: %s" %
            (CONNECTOR_TYPE, CONNECTOR_TYPE, json.dumps(adls)))

    required = required_parameter_names(method)
    if required != ["path", "server"]:
        die("Fabric %s required parameters changed; expected server,path got %s" %
            (CONNECTOR_TYPE, ",".join(required)))

    credential_types = values_named(adls.get("credentialTypes") or method.get("credentialTypes"))
    expected = ["Key", "OAuth2", "ServicePrincipal", "WorkspaceIdentity", "SharedAccessSignature"]
    missing = [name for name in expected if name not in credential_types]
    if missing:
        die("Fabric %s credential types missing %s; got %s" %
            (CONNECTOR_TYPE, ",".join(missing), ",".join(credential_types)))
    print("  = connector contract OK: server,path + Key credentials supported")


def list_connections(args, fabric_token):
    if args.dry_run:
        return []
    return list_paged(fabric_url("/connections"), fabric_token, False, "list connections")


def find_connection(args, fabric_token):
    for conn in list_connections(args, fabric_token):
        if conn.get("displayName") == args.connection_name:
            return conn.get("id")
    return None


def connection_body(args, storage_key):
    return {
        "connectivityType": "ShareableCloud",
        "displayName": args.connection_name,
        "privacyLevel": "Organizational",
        "connectionDetails": {
            "type": "AzureDataLakeStorage",
            "creationMethod": "AzureDataLakeStorage",
            "parameters": [
                {"dataType": "Text", "name": "server", "value": storage_server(args)},
                {"dataType": "Text", "name": "path", "value": args.container},
            ],
        },
        "credentialDetails": {
            "singleSignOnType": "None",
            "connectionEncryption": "NotEncrypted",
            "skipTestConnection": False,
            "credentials": {"credentialType": "Key", "key": storage_key},
        },
    }


def ensure_connection(args, fabric_token, storage_key):
    if args.connection_id:
        print("= connection: using supplied connection id %s" % args.connection_id)
        return args.connection_id
    discover_adls_contract(args, fabric_token)
    existing = find_connection(args, fabric_token)
    if existing:
        print("  = connection %s exists (%s)" % (args.connection_name, existing))
        return existing
    print("+ connection: creating %s" % args.connection_name)
    st, body, _ = req(
        "POST",
        fabric_url("/connections"),
        fabric_token,
        connection_body(args, storage_key),
        args.dry_run,
    )
    require_ok(st, body, "create connection", ok=(200, 201, 202))
    conn_id = body.get("id") if isinstance(body, dict) else None
    if args.dry_run:
        conn_id = "dry-run-connection-id"
    if not conn_id:
        die("create connection response did not contain an id: %s" % body)
    print("  %s connection %s (%s)" % (created_marker(st), args.connection_name, conn_id))
    return conn_id


def existing_or_supplied_connection(args, fabric_token):
    if args.connection_id:
        return args.connection_id
    existing = find_connection(args, fabric_token)
    if existing:
        print("  = connection %s exists (%s)" % (args.connection_name, existing))
        return existing
    die("connection '%s' not found; run --steps connection first or pass --connection-id" %
        args.connection_name)


def create_shortcut(args, fabric_token, workspace_id, lakehouse_id, connection_id):
    print("+ shortcut: creating or overwriting %s" % args.shortcut_name)
    url = fabric_url(
        "/workspaces/%s/items/%s/shortcuts?shortcutConflictPolicy=CreateOrOverwrite"
        % (workspace_id, lakehouse_id)
    )
    body = {
        "path": "Files",
        "name": args.shortcut_name,
        "target": {
            "adlsGen2": {
                "location": storage_server(args),
                "subpath": "/" + args.container,
                "connectionId": connection_id,
            }
        },
    }
    st, resp, _ = req("POST", url, fabric_token, body, args.dry_run)
    require_ok(st, resp, "create shortcut", ok=(200, 201, 202))
    print("  %s shortcut Files/%s" % (created_marker(st), args.shortcut_name))


def verify_shortcut(args, fabric_token, storage_token, workspace_id, lakehouse_id):
    print("+ verify: reading shortcut metadata")
    url = fabric_url("/workspaces/%s/items/%s/shortcuts" % (workspace_id, lakehouse_id))
    st, body, _ = req("GET", url, fabric_token, dry_run=args.dry_run)
    require_ok(st, body, "list shortcuts", ok=(200,))
    if args.dry_run:
        print("  = dry-run shortcut metadata verification skipped")
    else:
        found = False
        for shortcut in body.get("value", []):
            if shortcut.get("name") == args.shortcut_name:
                found = True
                print("  = shortcut %s/%s target=%s" %
                      (shortcut.get("path"), shortcut.get("name"),
                       json.dumps(shortcut.get("target", {}), sort_keys=True)))
        if not found:
            die("shortcut '%s' was not returned by Fabric" % args.shortcut_name)
    verify_onelake_files(args, storage_token, workspace_id, lakehouse_id)


def verify_onelake_files(args, storage_token, workspace_id, lakehouse_id):
    print("+ verify: listing files through OneLake DFS")
    base = (
        "%s/%s?resource=filesystem&recursive=true&directory=%s/Files/%s"
        % (ONELAKE, workspace_id, lakehouse_id, args.shortcut_name)
    )
    next_url = base
    file_count = 0
    total_bytes = 0
    while next_url:
        st, body, headers = req("GET", next_url, storage_token, dry_run=args.dry_run)
        require_ok(st, body, "list OneLake shortcut files", ok=(200,))
        if args.dry_run:
            print("  = dry-run OneLake listing skipped")
            return
        if not isinstance(body, dict):
            die("OneLake DFS returned a non-JSON response: %s" % body)
        for path in body.get("paths", []):
            if path.get("isDirectory") is True:
                continue
            file_count += 1
            try:
                total_bytes += int(path.get("contentLength") or 0)
            except ValueError:
                pass
        continuation = headers.get("x-ms-continuation")
        next_url = append_continuation(base, continuation) if continuation else None
    if file_count == 0:
        print("  ! zero files found through shortcut; the Cost Management export may not have finished yet")
    else:
        print("  + OneLake readable: %d files, %d bytes" % (file_count, total_bytes))


def parse_args():
    ap = argparse.ArgumentParser(
        description="Create a Fabric OneLake shortcut for Azure Cost Management FOCUS exports."
    )
    ap.add_argument("--tenant", default=env("AZURE_TENANT_ID", DEFAULT_TENANT))
    ap.add_argument("--workspace", default=env("FINOPS_FABRIC_WORKSPACE", DEFAULT_WORKSPACE),
                    help="Fabric workspace display name or id")
    ap.add_argument("--lakehouse", default=env("FINOPS_FABRIC_LAKEHOUSE", DEFAULT_LAKEHOUSE),
                    help="Fabric lakehouse display name or id")
    ap.add_argument("--subscription", default=env("AZURE_SUBSCRIPTION_ID", DEFAULT_SUBSCRIPTION))
    ap.add_argument("--resource-group", default=env("FINOPS_COST_RG", DEFAULT_RESOURCE_GROUP))
    ap.add_argument("--storage-account", default=env("FINOPS_COST_STORAGE", DEFAULT_STORAGE_ACCOUNT))
    ap.add_argument("--container", default=env("FINOPS_COST_CONTAINER", DEFAULT_CONTAINER))
    ap.add_argument("--connection-name", default=env("FINOPS_CONNECTION_NAME", DEFAULT_CONNECTION_NAME))
    ap.add_argument("--connection-id", default=env("FINOPS_CONNECTION_ID", ""))
    ap.add_argument("--shortcut-name", default=env("FINOPS_SHORTCUT_NAME", DEFAULT_SHORTCUT_NAME))
    ap.add_argument("--steps", nargs="+", default=ALL_STEPS,
                    help="subset of: %s (default: all)" % " ".join(ALL_STEPS))
    ap.add_argument("--dry-run", action="store_true",
                    help="print REST calls without sending them")
    args = ap.parse_args()
    unknown = [step for step in args.steps if step not in ALL_STEPS]
    if unknown:
        die("unknown --steps value(s): %s; choose from %s" %
            (", ".join(unknown), " ".join(ALL_STEPS)))
    return args


def main():
    args = parse_args()
    print("Fabric OneLake shortcut setup")
    print("= workspace %s" % args.workspace)
    print("= lakehouse  %s" % args.lakehouse)
    print("= storage    %s/%s" % (storage_server(args), args.container))

    fabric_token = "dry-run-fabric-token" if args.dry_run else az_token(FABRIC_RESOURCE, args.tenant)
    arm_token = "dry-run-arm-token" if args.dry_run else az_token(ARM, args.tenant)
    storage_token = "dry-run-storage-token" if args.dry_run else az_token(STORAGE_RESOURCE, args.tenant)

    workspace_id, lakehouse_id = resolve_targets(args, fabric_token)
    connection_id = args.connection_id

    if "connection" in args.steps:
        storage_key = list_keys(args, arm_token)
        connection_id = ensure_connection(args, fabric_token, storage_key)
    elif "shortcut" in args.steps:
        connection_id = existing_or_supplied_connection(args, fabric_token)

    if "shortcut" in args.steps:
        create_shortcut(args, fabric_token, workspace_id, lakehouse_id, connection_id)
    if "verify" in args.steps:
        verify_shortcut(args, fabric_token, storage_token, workspace_id, lakehouse_id)

    print("+ complete")


if __name__ == "__main__":
    main()
