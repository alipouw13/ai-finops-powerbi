#!/usr/bin/env python3
"""
Fabric deployment automation for the AI FinOps accelerator.

Dependency-free (stdlib urllib). Idempotent where the Fabric API allows it.
Auth uses the Azure CLI: you must be `az login`ed as a user that HAS a Power BI
/ Fabric license (see platform/deploy/README.md → "Unblock" for the 2 one-time
browser steps). The script fails fast with a clear message if you are not
licensed, so it will never silently do the wrong thing against your account.

What it does (each step is independently selectable via --steps):
  preflight  verify az login + Fabric token + that the user is licensed
  capacity   find a usable capacity (Trial or an F-SKU); print guidance if none
  workspace  create (or reuse) the target workspace, assign it to the capacity
  lakehouse  create (or reuse) the lakehouse
  upload     push all data/*.csv to Lakehouse Files/bronze via OneLake (ADLS)
  notebooks  import the 3 medallion notebooks (bronze/silver/gold)
  run        run bronze -> silver -> gold in order, polling to completion
  teardown   delete the workspace (honours --delete-after)

Publishing the semantic model + report: the robust path is Fabric Git
integration or "Publish" from Power BI Desktop — see README.md. This script
deliberately does NOT hand-craft PBIP definition payloads (brittle); it builds
the Lakehouse + gold tables that a Direct Lake / import model then consumes.

USAGE
  python3 platform/deploy/fabric_deploy.py --steps preflight
  python3 platform/deploy/fabric_deploy.py            # full deploy
  python3 platform/deploy/fabric_deploy.py --delete-after   # deploy then remove

  # target an existing workspace/lakehouse by GUID instead of creating new ones
  python3 platform/deploy/fabric_deploy.py \
      --workspace 11111111-2222-3333-4444-555555555555 \
      --lakehouse 66666666-7777-8888-9999-000000000000
"""
import argparse
import base64
import json
import os
import re
import shutil
import ssl
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

FABRIC = "https://api.fabric.microsoft.com/v1"
ONELAKE = "https://onelake.dfs.fabric.microsoft.com"
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
DATA_DIR = os.path.join(REPO_ROOT, "AIFinOps.SemanticModel", "data")
BRONZE_SRC = os.path.join(REPO_ROOT, "platform", "fabric", "bronze_out")
# Gitignored: produced by extract_real_bronze.py and holds live tenant data.
REAL_SRC = os.path.join(REPO_ROOT, "platform", "fabric", "bronze_real")
NB_DIR = os.path.join(REPO_ROOT, "platform", "medallion")

WORKSPACE_NAME = os.environ.get("FINOPS_WORKSPACE", "AI FinOps Accelerator")
LAKEHOUSE_NAME = os.environ.get("FINOPS_LAKEHOUSE", "finops_lakehouse")

GUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                     r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def is_guid(value):
    return bool(value) and bool(GUID_RE.match(value))

NOTEBOOKS = [
    # RUNNABLE medallion chain. Each notebook writes to its own lakehouse and
    # reads upstream ones by abfss path, so bronze/silver/gold stay separable.
    ("00_load_bronze_csv", os.path.join(NB_DIR, "bronze", "00_load_bronze_csv.py"), "bronze"),
    ("01_load_bronze_real_csv",
     os.path.join(NB_DIR, "bronze", "01_load_bronze_real_csv.py"), "bronze_real"),
    ("10_silver_conform", os.path.join(NB_DIR, "silver", "10_conform_usage.py"), "silver"),
    ("20_gold_star", os.path.join(NB_DIR, "gold", "20_build_star.py"), "gold"),
    # DESIGN scaffolds: placeholder paths, not runnable. Kept as *.design.py.
    ("01_bronze_ingest", os.path.join(NB_DIR, "bronze", "01_ingest_foundry_apim.py"), "bronze"),
    ("02_bronze_copilot", os.path.join(NB_DIR, "bronze", "02_ingest_copilot_platforms.py"), "bronze"),
]

RUNNABLE = ["00_load_bronze_csv", "01_load_bronze_real_csv",
            "10_silver_conform", "20_gold_star"]


# --------------------------------------------------------------------------- auth
def az_exe():
    """On Windows the CLI is az.cmd, which CreateProcess will not resolve from
    the bare name 'az'. shutil.which honours PATHEXT and returns the full path."""
    return shutil.which("az") or shutil.which("az.cmd")


def az_token(resource):
    exe = az_exe()
    if not exe:
        die("Azure CLI (`az`) not found on PATH. Install it and run `az login`.")
    try:
        out = subprocess.check_output(
            [exe, "account", "get-access-token", "--resource", resource,
             "--query", "accessToken", "-o", "tsv"],
            stderr=subprocess.PIPE)
        return out.decode().strip()
    except subprocess.CalledProcessError as e:
        die("`az` could not get a token. Run `az login` first.\n" + e.stderr.decode())


def die(msg, code=1):
    print("\nERROR: " + msg, file=sys.stderr)
    sys.exit(code)


# --------------------------------------------------------------------------- http
# Transient TLS/connection resets are common on corporate networks and against
# OneLake under load. A single blip used to abort the whole deploy with an
# unhandled URLError mid-upload, so retry the network layer with backoff.
# HTTP error *responses* are returned to the caller, not retried here.
RETRIES = 4
RETRY_STATUS = (429, 500, 502, 503, 504)


def _req(method, url, token, body=None, ctype="application/json", raw=False):
    data = None
    headers = {"Authorization": "Bearer " + token}
    if body is not None:
        if raw:
            data = body if isinstance(body, bytes) else body.encode()
            if ctype:
                headers["Content-Type"] = ctype
        else:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"

    last_err = None
    for attempt in range(RETRIES):
        r = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            resp = urllib.request.urlopen(r, timeout=300)
            payload = resp.read()
            # Fabric long-running ops return 202 + Operation-Location header
            loc = resp.headers.get("Location") or resp.headers.get("Operation-Location")
            return resp.status, (json.loads(payload) if payload and not raw else payload), loc
        except urllib.error.HTTPError as e:
            body_txt = e.read().decode(errors="replace")
            if e.code in RETRY_STATUS and attempt < RETRIES - 1:
                wait = int(e.headers.get("Retry-After") or 0) or 2 ** attempt
                print("    ! HTTP %s, retrying in %ss" % (e.code, wait))
                time.sleep(wait)
                continue
            return e.code, body_txt, None
        except (urllib.error.URLError, ssl.SSLError, OSError) as e:
            last_err = e
            if attempt < RETRIES - 1:
                wait = 2 ** attempt
                print("    ! network error (%s), retrying in %ss"
                      % (type(e).__name__, wait))
                time.sleep(wait)
                continue
    raise SystemExit("network failed after %d attempts: %s" % (RETRIES, last_err))


def fab(method, path, token, body=None):
    return _req(method, FABRIC + path, token, body)


def fab_list(path, token):
    """GET a Fabric collection, following continuationUri.

    The list endpoints page at 100 items. Without this a tenant with more than
    100 workspaces silently fails to find the target and the deployer would
    create a duplicate instead of reusing it.
    """
    items, url, pages = [], FABRIC + path, 0
    while url:
        status, payload, _ = _req("GET", url, token)
        if status != 200:
            return status, payload
        if not isinstance(payload, dict):
            return status, payload
        items.extend(payload.get("value", []))
        url = payload.get("continuationUri")
        pages += 1
        if pages > 100:
            break
    return 200, {"value": items}


def poll_lro(loc, token, label, timeout=1800):
    """Poll a Fabric long-running operation URL until it succeeds/fails."""
    if not loc:
        return
    start = time.time()
    while time.time() - start < timeout:
        status, payload, _ = _req("GET", loc, token)
        state = (payload or {}).get("status") if isinstance(payload, dict) else None
        if state in ("Succeeded", "Completed"):
            return payload
        if state in ("Failed", "Cancelled"):
            die("%s failed: %s" % (label, json.dumps(payload)))
        time.sleep(5)
    die("%s timed out after %ss" % (label, timeout))


# --------------------------------------------------------------------------- steps
def step_preflight():
    print("• preflight: getting Fabric token via az ...")
    token = az_token("https://api.fabric.microsoft.com")
    status, payload, _ = fab("GET", "/workspaces", token)
    if status == 200:
        print("  ✓ licensed — Fabric REST reachable (%d workspaces visible)"
              % len(payload.get("value", [])))
        return token
    if isinstance(payload, str) and "UserNotLicensed" in payload:
        die("You are signed in but have NO Power BI/Fabric license, so the "
            "Fabric REST API rejects every call (UserNotLicensed).\n\n"
            "Fix it with 2 one-time browser steps (see platform/deploy/README.md):\n"
            "  1. Go to https://app.fabric.microsoft.com and sign in — this "
            "self-service-provisions a free Power BI license for your user.\n"
            "  2. In the Fabric portal: Account manager ▸ 'Start trial' (Fabric "
            "60-day trial, ~F64, $0), OR have an admin assign an F-SKU capacity.\n\n"
            "Then re-run this script. Nothing here charges your account until a "
            "paid capacity runs — the Trial is free.")
    die("Unexpected Fabric response (%s): %s" % (status, payload))


def step_capacity(token, wanted=None):
    print("• capacity: looking for a usable capacity ...")
    status, payload, _ = fab("GET", "/capacities", token)
    if status != 200:
        die("Could not list capacities: %s" % payload)
    caps = payload.get("value", [])
    active = [c for c in caps if c.get("state", "").lower() == "active"]
    if not active:
        die("No ACTIVE capacity found. Start the Fabric Trial (free) from the "
            "portal Account manager, or create/assign an F-SKU. Re-run after.")
    if wanted:
        chosen = next((c for c in active
                       if wanted in (c.get("id"), c.get("displayName"))), None)
        if not chosen:
            die("Capacity '%s' not found among %d active capacities. Run "
                "platform/validate/probe_fabric.py to list them." % (wanted, len(active)))
    else:
        for c in active:
            print("  found capacity: %s (sku=%s, id=%s)"
                  % (c.get("displayName"), c.get("sku", "?"), c.get("id")))
        # A PPU capacity (sku PP*) cannot host a Lakehouse - exclude it.
        usable = [c for c in active if not str(c.get("sku", "")).startswith("PP")]
        if not usable:
            die("Only Premium-Per-User capacity is available, which cannot host a "
                "Lakehouse. Start the Fabric Trial or assign an F-SKU.")
        if len(usable) > 1:
            print("  ! %d capacities are visible; picking the first Trial/F-SKU. Pass "
                  "--capacity <id> to choose deliberately." % len(usable))
        trial = [c for c in usable
                 if "trial" in (c.get("sku", "") + c.get("displayName", "")).lower()]
        chosen = (trial or usable)[0]
    print("  ✓ using capacity: %s (id=%s)" % (chosen.get("displayName"), chosen["id"]))
    return chosen["id"]


def _find_item(token, ws_id, kind, name):
    status, payload, _ = fab("GET", "/workspaces/%s/items?type=%s" % (ws_id, kind), token)
    if status == 200:
        for it in payload.get("value", []):
            if it.get("displayName") == name:
                return it["id"]
    return None


def step_workspace(token, capacity_id, wanted=None):
    """Resolve the target workspace. `wanted` may be a GUID or a display name;
    a GUID is never created, so passing one can only ever target what exists."""
    target = wanted or WORKSPACE_NAME
    print("• workspace: ensuring '%s' ..." % target)
    # A GUID can be fetched directly; that also works when the workspace is
    # beyond the first page of /workspaces.
    if is_guid(target):
        status, payload, _ = fab("GET", "/workspaces/%s" % target, token)
        if status == 200 and isinstance(payload, dict) and payload.get("id"):
            print("  ✓ reusing workspace '%s' id=%s"
                  % (payload.get("displayName"), payload["id"]))
            ws_id = payload["id"]
        else:
            die("Workspace %s is not accessible to this account (HTTP %s).\n"
                "  If it lives in another tenant, sign in there first:\n"
                "    az login --tenant <tenant-id>\n"
                "  Otherwise ask for Admin/Member/Contributor on it."
                % (target, status))
    else:
        status, payload = fab_list("/workspaces", token)
        if status != 200:
            die("Could not list workspaces: %s" % payload)
        for ws in payload.get("value", []):
            if ws.get("displayName") == target:
                ws_id = ws["id"]
                print("  ✓ reusing workspace '%s' id=%s" % (target, ws_id))
                break
        else:
            status, payload, loc = fab("POST", "/workspaces", token,
                                       {"displayName": target})
            if status not in (200, 201):
                die("Create workspace failed: %s" % payload)
            ws_id = payload["id"]
            print("  ✓ created workspace id=%s" % ws_id)
    # assign to capacity (idempotent); skipped when the workspace already has one
    if capacity_id:
        status, payload, _ = fab("POST", "/workspaces/%s/assignToCapacity" % ws_id, token,
                                 {"capacityId": capacity_id})
        if status in (200, 202):
            print("  ✓ assigned to capacity")
        else:
            print("  ! assignToCapacity returned %s: %s" % (status, payload))
    return ws_id


def step_lakehouse(token, ws_id, wanted=None):
    """Resolve the target lakehouse. `wanted` may be a GUID or a display name."""
    target = wanted or LAKEHOUSE_NAME
    print("• lakehouse: ensuring '%s' ..." % target)
    status, payload = fab_list("/workspaces/%s/items?type=Lakehouse" % ws_id, token)
    if status == 200:
        for it in payload.get("value", []):
            if target in (it.get("displayName"), it.get("id")):
                print("  ✓ reusing lakehouse '%s' id=%s"
                      % (it.get("displayName"), it["id"]))
                return it["id"]
    if is_guid(target):
        die("Lakehouse %s not found in workspace %s. Pass a display name to create "
            "a new one." % (target, ws_id))
    # enableSchemas mirrors what the Fabric UI now does by default. Without it
    # the lakehouse has no `dbo` schema, every saveAsTable("dbo.x") fails with
    # SCHEMA_NOT_FOUND, and the abfss paths the medallion notebooks use
    # (Tables/dbo/<table>) do not resolve either.
    body = {"displayName": target, "creationPayload": {"enableSchemas": True}}
    for attempt in range(10):
        status, payload, loc = fab("POST", "/workspaces/%s/lakehouses" % ws_id,
                                   token, body)
        # Fabric holds a deleted item's display name for a few minutes. This is
        # explicitly retriable, and it is the normal path when a lakehouse is
        # dropped and immediately recreated.
        if (status not in (200, 201, 202) and isinstance(payload, dict)
                and payload.get("errorCode") == "ItemDisplayNameNotAvailableYet"
                and attempt < 9):
            print("  . name '%s' still reserved from a recent delete, "
                  "retrying in 30s (%d/9)" % (target, attempt + 1))
            time.sleep(30)
            continue
        break
    if status == 202:
        payload = poll_lro(loc, token, "create lakehouse")
    if status not in (200, 201, 202):
        die("Create lakehouse failed: %s" % payload)
    lh_id = _find_item(token, ws_id, "Lakehouse", target)
    print("  ✓ created lakehouse id=%s" % lh_id)
    return lh_id


def step_upload(ws_id, lh_id):
    print("• upload: pushing Bronze CSVs to Files/bronze via OneLake ...")
    st = az_token("https://storage.azure.com")
    # Upload the BRONZE extracts, not the Gold star. Pushing
    # AIFinOps.SemanticModel/data/*.csv here would land dim_*/fact_* beside the
    # bronze_* tables and make the medallion layers meaningless.
    src = BRONZE_SRC if os.path.isdir(BRONZE_SRC) else DATA_DIR
    files = sorted(f for f in os.listdir(src) if f.endswith(".csv"))
    if not files:
        die("no CSVs in %s — run platform/fabric/gen_bronze_data.py first" % src)
    print("  source: %s" % src)
    uploads = [(f, os.path.join(src, f)) for f in files]
    # The extractable-data catalog is a reference table, not usage telemetry, so
    # it is not produced by gen_bronze_data.py. Gold still needs it to build
    # dim_data_source (the Extractable Data Spectrum page), so land it as a
    # bronze ref table alongside the rest.
    catalog = os.path.join(DATA_DIR, "extractable_data_catalog.csv")
    if os.path.exists(catalog) and src != DATA_DIR:
        uploads.append(("bronze_ref_extractable_catalog.csv", catalog))
    _push(st, ws_id, lh_id, "bronze", uploads)


def step_upload_real(ws_id, lh_id):
    """Push the REAL tenant extracts to the real-bronze lakehouse.

    Kept separate from step_upload so real and mock can never land in the same
    lakehouse: the whole provenance story depends on being able to point at a
    physical boundary between them.
    """
    print("• upload-real: pushing REAL Bronze CSVs to Files/bronze_real ...")
    if not os.path.isdir(REAL_SRC):
        die("no %s — run platform/fabric/extract_real_bronze.py "
            "--i-understand-this-is-real-tenant-data first" % REAL_SRC)
    files = sorted(f for f in os.listdir(REAL_SRC) if f.endswith(".csv"))
    if not files:
        die("no CSVs in %s" % REAL_SRC)
    print("  source: %s" % REAL_SRC)
    st = az_token("https://storage.azure.com")
    _push(st, ws_id, lh_id, "bronze_real",
          [(f, os.path.join(REAL_SRC, f)) for f in files])


def _push(st, ws_id, lh_id, folder, uploads):
    # OneLake accepts either <workspaceName>/<itemName>.<itemtype>/... or
    # <workspaceGUID>/<itemGUID>/... — with GUIDs the item-type suffix must be
    # omitted, and GUIDs avoid having to URL-encode names containing spaces.
    base = "%s/%s/%s/Files/%s" % (ONELAKE, ws_id, lh_id, folder)
    failures = 0
    for name, path in uploads:
        with open(path, "rb") as fh:
            content = fh.read()
        url = "%s/%s" % (base, name)
        # ADLS Gen2: create file, append bytes, flush.
        s1, _, _ = _req("PUT", url + "?resource=file", st, b"", None, raw=True)
        s2, _, _ = _req("PATCH", url + "?action=append&position=0", st,
                        content, "application/octet-stream", raw=True)
        s3, _, _ = _req("PATCH", "%s?action=flush&position=%d" % (url, len(content)),
                        st, b"", None, raw=True)
        ok = s1 in (200, 201) and s2 in (200, 202) and s3 in (200, 201)
        failures += 0 if ok else 1
        print("  %s %s (%d bytes) [%s/%s/%s]" %
              ("✓" if ok else "✗", name, len(content), s1, s2, s3))
    if failures:
        die("%d of %d file(s) failed to upload to OneLake. A 403 here usually means "
            "the account lacks write access to the lakehouse." % (failures, len(uploads)))


def _py_to_ipynb(path, ws_id=None, lh_id=None, lh_name=None, params=None):
    """Wrap a .py medallion script as a Fabric notebook (ipynb).

    When a lakehouse is supplied it is attached as the notebook's default, which
    is what makes relative paths (Files/...) and saveAsTable() resolve. Without
    it Spark has no default catalog and every write fails at runtime.

    `params` becomes a leading cell defining WS_ID / BRONZE_ID / SILVER_ID /
    GOLD_ID, so the medallion scripts address lakehouses by id without any
    hard-coded GUIDs in the repo.
    """
    with open(path, encoding="utf-8") as fh:
        src = fh.read()
    meta = {
        "language_info": {"name": "python"},
        "kernelspec": {"name": "synapse_pyspark", "language": "Python",
                       "display_name": "Synapse PySpark"},
    }
    if ws_id and lh_id:
        meta["dependencies"] = {"lakehouse": {
            "default_lakehouse": lh_id,
            "default_lakehouse_name": lh_name or lh_id,
            "default_lakehouse_workspace_id": ws_id,
        }}
    cells = []
    if params:
        head = ["# Injected by fabric_deploy.py — lakehouse ids for this workspace.\n"]
        head += [f'{k} = "{v}"\n' for k, v in params.items() if v]
        cells.append({"cell_type": "code", "source": head,
                      "metadata": {"tags": ["parameters"]},
                      "outputs": [], "execution_count": None})
    # Fabric surfaces only "System cancelled the Spark session due to statement
    # execution failures" when a notebook raises — the traceback is not in the
    # job API. Persist it to OneLake so step_run can print the real cause.
    body = "".join(f"    {ln}" if ln.strip() else ln
                   for ln in src.splitlines(keepends=True))
    wrapped = (
        "import traceback\n"
        "try:\n"
        f"{body}\n"
        "except Exception:\n"
        "    _tb = traceback.format_exc()\n"
        "    print(_tb)\n"
        "    try:\n"
        "        mssparkutils.fs.put(\n"
        f"            'Files/_errors/{os.path.basename(path)}.log', _tb, True)\n"
        "    except Exception:\n"
        "        pass\n"
        "    raise\n"
    )
    cells.append({"cell_type": "code", "source": wrapped.splitlines(keepends=True),
                  "metadata": {}, "outputs": [], "execution_count": None})
    nb = {"cells": cells, "metadata": meta, "nbformat": 4, "nbformat_minor": 5}
    return json.dumps(nb).encode()


def step_notebooks(token, ws_id, lakehouses, only=None):
    """lakehouses: {'bronze': (id, name), 'silver': ..., 'gold': ...}"""
    print("• notebooks: importing medallion notebooks ...")
    params = {"WS_ID": ws_id,
              "BRONZE_ID": (lakehouses.get("bronze") or ("", ""))[0],
              "BRONZE_REAL_ID": (lakehouses.get("bronze_real") or ("", ""))[0],
              "SILVER_ID": (lakehouses.get("silver") or ("", ""))[0],
              "GOLD_ID": (lakehouses.get("gold") or ("", ""))[0]}
    for layer, pair in lakehouses.items():
        if pair:
            print("  %-7s -> %s" % (layer, pair[1] or pair[0]))
    ids = {}
    targets = [n for n in NOTEBOOKS if not only or n[0] in only]
    for name, path, layer in targets:
        lh_id, lh_name = lakehouses.get(layer) or (None, None)
        if not lh_id:
            print("  ! %s needs a %s lakehouse — skipped" % (name, layer))
            continue
        payload64 = base64.b64encode(
            _py_to_ipynb(path, ws_id, lh_id, lh_name, params)).decode()
        definition = {"format": "ipynb",
                      "parts": [{"path": "notebook-content.ipynb",
                                 "payload": payload64, "payloadType": "InlineBase64"}]}
        existing = _find_item(token, ws_id, "Notebook", name)
        if existing:
            # Replace the definition so a re-run picks up the lakehouse binding
            # and any edits, rather than silently reusing a stale notebook.
            status, resp, loc = fab(
                "POST", "/workspaces/%s/notebooks/%s/updateDefinition" % (ws_id, existing),
                token, {"definition": definition})
            if status == 202:
                poll_lro(loc, token, "update " + name)
            elif status not in (200, 201):
                die("Update notebook %s failed: %s" % (name, resp))
            ids[name] = existing
            print("  ✓ updated notebook %s (%s)" % (name, layer))
            continue
        status, resp, loc = fab("POST", "/workspaces/%s/notebooks" % ws_id, token,
                                {"displayName": name, "definition": definition})
        if status == 202:
            poll_lro(loc, token, "import " + name)
        elif status not in (200, 201):
            die("Import notebook %s failed: %s" % (name, resp))
        ids[name] = _find_item(token, ws_id, "Notebook", name)
        print("  ✓ imported notebook %s (%s)" % (name, layer))
    return ids


def fetch_error_log(ws_id, lh_id, script_name):
    """Read the traceback the notebook wrapper persisted to OneLake, if any."""
    if not lh_id:
        return None
    st = az_token("https://storage.azure.com")
    url = "%s/%s/%s/Files/_errors/%s.log" % (ONELAKE, ws_id, lh_id, script_name)
    status, body, _ = _req("GET", url, st, raw=True)
    if status == 200 and body:
        return body.decode(errors="replace") if isinstance(body, bytes) else str(body)
    return None


def step_run(token, ws_id, nb_ids, only=None, lakehouses=None):
    print("• run: executing notebooks in order ...")
    lakehouses = lakehouses or {}
    targets = [(n, p, layer) for n, p, layer in NOTEBOOKS
               if n in nb_ids and (not only or n in only)]
    for name, path, layer in targets:
        nb_id = nb_ids.get(name)
        if not nb_id:
            die("notebook %s not found; run 'notebooks' step first" % name)
        url = "/workspaces/%s/items/%s/jobs/instances?jobType=RunNotebook" % (ws_id, nb_id)
        status, resp, loc = fab("POST", url, token, {})
        if status not in (200, 201, 202):
            die("Run %s failed: %s" % (name, resp))
        print("  ▶ %s submitted" % name)
        try:
            poll_lro(loc, token, "run " + name)
        except SystemExit:
            lh_id = (lakehouses.get(layer) or (None, None))[0]
            tb = fetch_error_log(ws_id, lh_id, os.path.basename(path))
            if tb:
                print("\n--- traceback from %s ---" % name)
                print(tb.strip()[-2500:])
                print("--- end traceback ---\n")
            raise
        print("  ✓ %s completed" % name)


def step_teardown(token, ws_id):
    print("• teardown: deleting workspace %s ..." % ws_id)
    status, resp, _ = fab("DELETE", "/workspaces/%s" % ws_id, token)
    if status in (200, 202, 204):
        print("  ✓ workspace deleted — $0 residual compute")
    else:
        print("  ✗ delete returned %s: %s" % (status, resp))


# --------------------------------------------------------------------------- main
ALL_STEPS = ["preflight", "capacity", "workspace", "lakehouse",
             "upload", "upload-real", "notebooks", "run"]


def main():
    ap = argparse.ArgumentParser(description="Deploy AI FinOps accelerator to Fabric")
    ap.add_argument("--steps", nargs="+", default=ALL_STEPS,
                    help="subset of: %s (default: all)" % " ".join(ALL_STEPS))
    ap.add_argument("--workspace", default=None,
                    help="target workspace GUID or display name (default: $FINOPS_WORKSPACE "
                         "or %r). A GUID must already exist; it is never created." % WORKSPACE_NAME)
    ap.add_argument("--lakehouse", default=None,
                    help="BRONZE lakehouse GUID or display name (default: $FINOPS_LAKEHOUSE "
                         "or %r)" % LAKEHOUSE_NAME)
    ap.add_argument("--lakehouse-silver", default=None,
                    help="SILVER lakehouse GUID or display name (created if a name)")
    ap.add_argument("--lakehouse-gold", default=None,
                    help="GOLD lakehouse GUID or display name (created if a name)")
    ap.add_argument("--lakehouse-bronze-real", default=None,
                    help="REAL-BRONZE lakehouse GUID or display name (created if a "
                         "name). Live tenant extracts land here, physically apart "
                         "from the mock bronze lakehouse.")
    ap.add_argument("--capacity", default=None,
                    help="capacity GUID or display name to assign the workspace to. "
                         "Omit only if you are happy with an arbitrary Trial/F-SKU.")
    ap.add_argument("--delete-after", action="store_true",
                    help="delete the workspace after a successful run ($0 residual)")
    ap.add_argument("--only", nargs="+", metavar="NB", default=None,
                    help="restrict notebooks/run to these notebook names. Defaults to "
                         "the runnable set (%s); the others are design scaffolds."
                         % " ".join(RUNNABLE))
    args = ap.parse_args()
    only = args.only or RUNNABLE

    if args.delete_after and is_guid(args.workspace or ""):
        die("Refusing --delete-after against an existing workspace GUID (%s). "
            "Teardown is only for workspaces this script created." % args.workspace)

    token = step_preflight()
    if args.steps == ["preflight"]:
        print("\npreflight OK — you are licensed and ready to deploy.")
        return

    capacity_id = step_capacity(token, args.capacity) if "capacity" in args.steps else None
    ws_id = step_workspace(token, capacity_id, args.workspace) if "workspace" in args.steps else None
    if ws_id is None and any(s in args.steps for s in
                             ("lakehouse", "upload", "upload-real", "notebooks", "run")):
        die("Steps after 'workspace' need a workspace id. Include 'workspace' in --steps.")
    lh_id = step_lakehouse(token, ws_id, args.lakehouse) if "lakehouse" in args.steps else None
    if "upload" in args.steps:
        if lh_id is None:
            die("The 'upload' step needs a lakehouse id. Include 'lakehouse' in --steps.")
        step_upload(ws_id, lh_id)

    def resolve(layer_arg, label):
        if not layer_arg:
            return None
        lid = step_lakehouse(token, ws_id, layer_arg)
        st, meta, _ = fab("GET", "/workspaces/%s/lakehouses/%s" % (ws_id, lid), token)
        return (lid, meta.get("displayName") if st == 200 and isinstance(meta, dict) else lid)

    lakehouses = {}
    if lh_id:
        st, meta, _ = fab("GET", "/workspaces/%s/lakehouses/%s" % (ws_id, lh_id), token)
        lakehouses["bronze"] = (
            lh_id, meta.get("displayName") if st == 200 and isinstance(meta, dict) else lh_id)
    # Resolved before upload-real so the real extracts have somewhere to land.
    if args.lakehouse_bronze_real:
        lakehouses["bronze_real"] = resolve(args.lakehouse_bronze_real, "bronze_real")
    if "upload-real" in args.steps:
        if not lakehouses.get("bronze_real"):
            die("The 'upload-real' step needs --lakehouse-bronze-real.")
        step_upload_real(ws_id, lakehouses["bronze_real"][0])
    if "notebooks" in args.steps or "run" in args.steps:
        lakehouses["silver"] = resolve(args.lakehouse_silver, "silver")
        lakehouses["gold"] = resolve(args.lakehouse_gold, "gold")

    nb_ids = (step_notebooks(token, ws_id, lakehouses, only)
              if "notebooks" in args.steps else {})
    if "run" in args.steps:
        if not nb_ids:
            nb_ids = {n: _find_item(token, ws_id, "Notebook", n) for n in only}
            nb_ids = {k: v for k, v in nb_ids.items() if v}
        step_run(token, ws_id, nb_ids, only, lakehouses)

    print("\n✓ Fabric deploy complete.")
    print("  Next: publish the semantic model + report — either")
    print("   (a) connect this repo via Fabric Git integration, or")
    print("   (b) open AIFinOps.pbip in Power BI Desktop and Publish to '%s'."
          % (args.workspace or WORKSPACE_NAME))
    if args.delete_after:
        step_teardown(token, ws_id)


if __name__ == "__main__":
    main()
