#!/usr/bin/env python3
"""
Load the MOCK Bronze CSVs into a Fabric Lakehouse (Bronze layer).

Pipeline (pure REST, no external deps):
  1. Create/find Lakehouse 'bronze_finops' in the workspace.
  2. Upload each platform/fabric/bronze_out/*.csv to OneLake  Files/bronze/<name>.csv
     via the ADLS Gen2 (OneLake DFS) API.
  3. Promote each file to a managed Delta table via the Lakehouse 'Load Table' API.

PREREQUISITE (the current blocker): the signed-in user (or a service principal
added to the workspace) MUST hold a Power BI / Fabric license. Without it every
write returns HTTP 401 'UserNotLicensed'. The tenant already has POWER_BI_STANDARD
(Power BI Free) SKUs available — an admin just needs to assign one to the user.

Run (once licensed):
  az login
  python3 platform/fabric/load_bronze.py --workspace davidshreyasalison
"""
import argparse
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

# The status output uses ✓/✗/→; the default Windows console codepage (cp1252)
# raises UnicodeEncodeError on them.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
BRONZE_DIR = os.path.join(HERE, "bronze_out")
FABRIC_RES = "https://api.fabric.microsoft.com"
STORAGE_RES = "https://storage.azure.com"
ONELAKE = "https://onelake.dfs.fabric.microsoft.com"


def az_exe():
    """On Windows the CLI is az.cmd, which CreateProcess will not resolve from
    the bare name 'az'. shutil.which honours PATHEXT and returns the full path."""
    exe = shutil.which("az") or shutil.which("az.cmd")
    if not exe:
        sys.exit("Azure CLI (`az`) not found on PATH. Install it and run `az login`.")
    return exe


def token(resource):
    out = subprocess.run(
        [az_exe(), "account", "get-access-token", "--resource", resource,
         "--query", "accessToken", "-o", "tsv"],
        capture_output=True, text=True)
    if out.returncode != 0:
        sys.exit(f"token failed for {resource}: {out.stderr.strip()}")
    return out.stdout.strip()


def req(method, url, tok, data=None, ctype="application/json", raw=False):
    headers = {"Authorization": f"Bearer {tok}"}
    if ctype:
        headers["Content-Type"] = ctype
    r = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(r) as resp:
            body = resp.read()
            return resp.status, (body if raw else (json.loads(body) if body else {}))
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


GUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                     r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def is_guid(v):
    return bool(v) and bool(GUID_RE.match(v))


def list_all(tok, url):
    """GET a Fabric collection, following continuationUri (pages at 100)."""
    items, pages = [], 0
    while url:
        st, body = req("GET", url, tok)
        if st != 200 or not isinstance(body, dict):
            return st, body
        items.extend(body.get("value", []))
        url = body.get("continuationUri")
        pages += 1
        if pages > 100:
            break
    return 200, {"value": items}


def find_workspace(tok, name):
    # A GUID resolves directly, which also works past the first page.
    if is_guid(name):
        st, body = req("GET", f"{FABRIC_RES}/v1/workspaces/{name}", tok)
        if st == 200 and isinstance(body, dict) and body.get("id"):
            return body["id"], body.get("displayName", name)
        sys.exit(f"workspace {name} not accessible (HTTP {st}). If it is in another "
                 f"tenant, run: az login --tenant <tenant-id>")
    st, body = list_all(tok, f"{FABRIC_RES}/v1/workspaces")
    if st == 401:
        sys.exit("HTTP 401 UserNotLicensed — assign the user a Power BI license first "
                 "(tenant has POWER_BI_STANDARD available). See module docstring.")
    if st != 200:
        sys.exit(f"list workspaces failed: {st} {body}")
    for w in body["value"]:
        if name in (w.get("displayName"), w.get("id")):
            return w["id"], w["displayName"]
    sys.exit(f"workspace '{name}' not found")


def ensure_lakehouse(tok, ws_id, name):
    st, body = list_all(tok, f"{FABRIC_RES}/v1/workspaces/{ws_id}/lakehouses")
    if st == 200:
        for lh in body["value"]:
            if name in (lh.get("displayName"), lh.get("id")):
                print(f"  = lakehouse '{lh.get('displayName')}' exists ({lh['id']})")
                return lh["id"]
    if is_guid(name):
        sys.exit(f"lakehouse {name} not found in workspace {ws_id}")
    st, body = req("POST", f"{FABRIC_RES}/v1/workspaces/{ws_id}/lakehouses", tok,
                   data=json.dumps({"displayName": name,
                                    "description": "AI FinOps Bronze (MOCK)"}).encode())
    if st in (200, 201):
        print(f"  + created lakehouse '{name}' ({body['id']})")
        return body["id"]
    if st == 202:  # long-running
        loc = None
        time.sleep(5)
        st, body = req("GET", f"{FABRIC_RES}/v1/workspaces/{ws_id}/lakehouses", tok)
        for lh in body.get("value", []):
            if lh["displayName"] == name:
                return lh["id"]
    sys.exit(f"create lakehouse failed: {st} {body}")


def upload_onelake(stok, ws_id, lh_id, local_path, rel_path):
    """ADLS Gen2 3-step: create -> append -> flush.

    Addressed by GUID (<workspaceGUID>/<itemGUID>/...) rather than by name.
    The name form needs the '.Lakehouse' type suffix and breaks on workspace
    names containing spaces; the GUID form takes no suffix and always works.
    """
    base = f"{ONELAKE}/{ws_id}/{lh_id}/{rel_path}"
    with open(local_path, "rb") as fh:
        data = fh.read()
    # create (empty file)
    st, _ = req("PUT", base + "?resource=file", stok, data=b"", ctype=None, raw=True)
    if st not in (200, 201, 202):
        return st
    # append
    st, _ = req("PATCH", base + "?action=append&position=0", stok, data=data,
                ctype="application/octet-stream", raw=True)
    if st not in (200, 202):
        return st
    # flush
    st, _ = req("PATCH", base + f"?action=flush&position={len(data)}", stok,
                ctype=None, raw=True)
    return st


def lakehouse_props(tok, ws_id, lh_id):
    st, body = req("GET", f"{FABRIC_RES}/v1/workspaces/{ws_id}/lakehouses/{lh_id}", tok)
    return body.get("properties", {}) if st == 200 and isinstance(body, dict) else {}


def load_table(tok, ws_id, lh_id, table, rel_path):
    url = (f"{FABRIC_RES}/v1/workspaces/{ws_id}/lakehouses/{lh_id}"
           f"/tables/{table}/load")
    # 'format' belongs INSIDE formatOptions. At the top level the service
    # rejects the whole request with a bare "An invalid request has been received".
    body = {"relativePath": rel_path, "pathType": "File",
            "formatOptions": {"format": "Csv", "header": True, "delimiter": ","},
            "mode": "Overwrite", "recursive": False}
    st, resp = req("POST", url, tok, data=json.dumps(body).encode())
    return st, resp


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workspace", required=True,
                    help="target workspace GUID or display name")
    ap.add_argument("--lakehouse", default="bronze_finops",
                    help="target lakehouse display name (created if absent)")
    args = ap.parse_args()

    ftok = token(FABRIC_RES)
    stok = token(STORAGE_RES)

    ws_id, ws_name = find_workspace(ftok, args.workspace)
    print(f"Workspace: {ws_name} ({ws_id})")
    lh_id = ensure_lakehouse(ftok, ws_id, args.lakehouse)

    # The Load Table REST API refuses schema-enabled lakehouses outright
    # (errorCode UnsupportedOperationForSchemasEnabledLakehouse). Detect it up
    # front so the upload still happens and the user is told the real next step.
    props = lakehouse_props(ftok, ws_id, lh_id)
    schemas_enabled = bool(props.get("defaultSchema"))
    if schemas_enabled:
        print(f"  ! schema-enabled lakehouse (defaultSchema="
              f"{props['defaultSchema']!r}) — the Load Table API does not support")
        print("    these. Files will be uploaded; create the Delta tables with Spark:")
        print("      python platform/deploy/fabric_deploy.py --steps notebooks run \\")
        print(f"          --workspace {ws_id} --lakehouse {lh_id}")

    csvs = sorted(glob.glob(os.path.join(BRONZE_DIR, "*.csv")))
    if not csvs:
        sys.exit(f"no CSVs in {BRONZE_DIR}; run gen_bronze_data.py first")
    verb = "Uploading" if schemas_enabled else "Uploading + loading"
    print(f"\n{verb} {len(csvs)} Bronze files:")
    uploaded = loaded = failed = 0
    for path in csvs:
        table = os.path.splitext(os.path.basename(path))[0]
        rel = f"Files/bronze/{table}.csv"
        st = upload_onelake(stok, ws_id, lh_id, path, rel)
        if st not in (200, 201, 202):
            print(f"  ! {table}: upload HTTP {st}")
            failed += 1
            continue
        uploaded += 1
        if schemas_enabled:
            print(f"  + {table}: uploaded")
            continue
        lst, resp = load_table(ftok, ws_id, lh_id, table, rel)
        if lst in (200, 201, 202):
            loaded += 1
            print(f"  + {table}: uploaded + load ok")
        else:
            failed += 1
            print(f"  ! {table}: uploaded, load HTTP {lst} {str(resp)[:120]}")

    print(f"\n{uploaded} uploaded, {loaded} table(s) loaded, {failed} failure(s).")
    if schemas_enabled:
        print("Files are in OneLake under Files/bronze. Run the notebooks step above")
        print("to materialise them as Delta tables.")
    else:
        print("Open the Lakehouse in Fabric -> Tables to see the Bronze layer.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
