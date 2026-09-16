#!/usr/bin/env python3
"""Deploy a TMDL semantic model to a Fabric workspace via the REST API.

Used for the Direct Lake model produced by
platform/validate/build_directlake.py. Creates the item if it does not exist,
otherwise replaces its definition (updateDefinition).

The Fabric definition payload is every TMDL part, base64-encoded, with paths
relative to the semantic model folder — e.g. `definition/model.tmdl`. Partial
payloads are rejected, so every part must be sent on every update.

    python platform/deploy/deploy_semantic_model.py \
        --workspace <workspace-guid> --model-dir AIFinOps.DirectLake.SemanticModel

Stdlib only. Requires `az login`.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

FABRIC = "https://api.fabric.microsoft.com/v1"
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
RETRIES = 4


def az_exe():
    exe = shutil.which("az") or shutil.which("az.cmd")
    if not exe:
        sys.exit("Azure CLI (`az`) not found on PATH. Install it and run `az login`.")
    return exe


def az_token(resource="https://api.fabric.microsoft.com"):
    out = subprocess.run(
        [az_exe(), "account", "get-access-token",
         "--resource", resource,
         "--query", "accessToken", "-o", "tsv"],
        capture_output=True, text=True)
    if out.returncode != 0:
        sys.exit("Could not get a token for %s. Run `az login --tenant <id>` first.\n%s"
                 % (resource, out.stderr.strip()))
    return out.stdout.strip()


def req(method, url, token, body=None):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Authorization": "Bearer " + token}
    if data:
        headers["Content-Type"] = "application/json"
    for attempt in range(RETRIES):
        r = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            resp = urllib.request.urlopen(r, timeout=300)
            payload = resp.read()
            loc = resp.headers.get("Location") or resp.headers.get("Operation-Location")
            return resp.status, (json.loads(payload) if payload else {}), loc
        except urllib.error.HTTPError as e:
            txt = e.read().decode(errors="replace")
            if e.code in (429, 500, 502, 503, 504) and attempt < RETRIES - 1:
                time.sleep(2 ** attempt)
                continue
            return e.code, txt, None
        except (urllib.error.URLError, ssl.SSLError, OSError) as e:
            if attempt < RETRIES - 1:
                print("    ! network error (%s), retrying" % type(e).__name__)
                time.sleep(2 ** attempt)
                continue
            sys.exit("network failed after %d attempts: %s" % (RETRIES, e))


def poll(loc, token, label, timeout=900):
    if not loc:
        return {}
    deadline = time.time() + timeout
    while time.time() < deadline:
        status, payload, _ = req("GET", loc, token)
        state = (payload or {}).get("status") if isinstance(payload, dict) else None
        if state in ("Succeeded", "Completed"):
            return payload
        if state in ("Failed", "Cancelled"):
            sys.exit("%s failed: %s" % (label, json.dumps(payload)[:600]))
        time.sleep(5)
    sys.exit("%s timed out after %ss" % (label, timeout))


def reframe(ws_id, ds_id, timeout=900):
    """Reframe the Direct Lake model so it re-reads the Delta schema.

    "Direct Lake needs no refresh" is true for *data* and false for *schema*.
    updateDefinition returns 200 and the model looks deployed, but a column the
    TMDL newly declares stays unmapped until the model reframes: the table still
    binds, the column is simply absent, and every measure referencing it fails at
    query time with "cannot be determined". Nothing errors at deploy time and the
    report just shows blank cards.

    This is how two measures (Rate Card Cost, Rate Card Coverage %) shipped
    broken after list_cost_usd / has_rate_card were added to the gold fact, and
    it is why this call is unconditional rather than a flag.

    Refresh lives on the Power BI REST surface, not the Fabric one, so it needs
    its own token audience.
    """
    token = az_token("https://analysis.windows.net/powerbi/api")
    url = ("https://api.powerbi.com/v1.0/myorg/groups/%s/datasets/%s/refreshes"
           % (ws_id, ds_id))
    status, resp, _ = req("POST", url, token, {"type": "full"})
    if status not in (200, 202):
        print("  ! reframe request rejected (%s): %s" % (status, str(resp)[:300]))
        print("    Refresh the dataset by hand, or newly declared columns will")
        print("    stay unmapped and their measures will return errors.")
        return
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(5)
        st, payload, _ = req("GET", url + "?$top=1", token)
        rows = (payload or {}).get("value") if isinstance(payload, dict) else None
        if not rows:
            continue
        state = rows[0].get("status")
        if state == "Completed":
            print("  ✓ reframed — Delta schema re-read")
            return
        if state in ("Failed", "Disabled"):
            sys.exit("reframe failed: %s" % json.dumps(rows[0])[:600])
    print("  ! reframe still running after %ss — check the dataset in the portal"
          % timeout)


def collect_parts(model_dir):
    """Every file under the model folder, as Fabric definition parts."""
    parts = []
    for base, _dirs, files in os.walk(model_dir):
        for f in sorted(files):
            full = os.path.join(base, f)
            rel = os.path.relpath(full, model_dir).replace(os.sep, "/")
            if rel == ".platform":
                continue  # Fabric supplies this itself
            with open(full, "rb") as fh:
                payload = base64.b64encode(fh.read()).decode()
            parts.append({"path": rel, "payload": payload,
                          "payloadType": "InlineBase64"})
    return parts


def find_item(token, ws_id, kind, name):
    status, payload, _ = req("GET", "%s/workspaces/%s/items?type=%s"
                             % (FABRIC, ws_id, kind), token)
    if status == 200 and isinstance(payload, dict):
        for it in payload.get("value", []):
            if it.get("displayName") == name:
                return it["id"]
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workspace", required=True, help="workspace GUID")
    ap.add_argument("--model-dir", required=True,
                    help="semantic model folder, e.g. AIFinOps.DirectLake.SemanticModel")
    ap.add_argument("--name", default=None,
                    help="display name in Fabric (default: folder name without suffix)")
    args = ap.parse_args()

    model_dir = args.model_dir
    if not os.path.isabs(model_dir):
        model_dir = os.path.join(ROOT, model_dir)
    if not os.path.isdir(model_dir):
        sys.exit("not a directory: %s" % model_dir)

    name = args.name or os.path.basename(model_dir).replace(".SemanticModel", "")
    token = az_token()

    parts = collect_parts(model_dir)
    print("semantic model : %s" % name)
    print("parts          : %d" % len(parts))
    for p in parts[:4]:
        print("                 %s" % p["path"])
    if len(parts) > 4:
        print("                 ... +%d more" % (len(parts) - 4))

    existing = find_item(token, args.workspace, "SemanticModel", name)
    definition = {"parts": parts}

    if existing:
        print("\nupdating existing model %s ..." % existing)
        status, resp, loc = req(
            "POST", "%s/workspaces/%s/semanticModels/%s/updateDefinition"
            % (FABRIC, args.workspace, existing), token, {"definition": definition})
        if status == 202:
            poll(loc, token, "updateDefinition")
        elif status not in (200, 201):
            sys.exit("updateDefinition failed (%s): %s" % (status, str(resp)[:900]))
        item_id = existing
        print("  ✓ definition replaced")
    else:
        print("\ncreating model ...")
        status, resp, loc = req(
            "POST", "%s/workspaces/%s/semanticModels" % (FABRIC, args.workspace), token,
            {"displayName": name, "definition": definition})
        if status == 202:
            poll(loc, token, "create semantic model")
        elif status not in (200, 201):
            sys.exit("create failed (%s): %s" % (status, str(resp)[:900]))
        item_id = find_item(token, args.workspace, "SemanticModel", name)
        print("  ✓ created id=%s" % item_id)

    print("\nreframing so the model re-reads the Delta schema ...")
    reframe(args.workspace, item_id)

    print("\nhttps://app.powerbi.com/groups/%s/datasets/%s"
          % (args.workspace, item_id))
    print("\nDirect Lake binds to Delta files in OneLake — no gateway, and no")
    print("refresh is needed for new *rows*. A schema change is different: newly")
    print("declared columns stay unmapped until the model reframes, which is why")
    print("this script now reframes on every deploy.")
    print("If the tables show no data, confirm the gold notebook has run and that")
    print("the lakehouse GUID in the DirectLake expression is correct.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
