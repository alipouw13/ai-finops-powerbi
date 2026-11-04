#!/usr/bin/env python3
"""Read-only Fabric probe: what can the signed-in user actually see and use?

Answers the "what is missing from my account?" question without creating,
modifying or deleting anything. Prints licence state, capacities, and the
workspaces the user can write to (with any lakehouses already in them).

    python platform/validate/probe_fabric.py [--workspace <name-or-guid>]

Stdlib only. Requires `az login`.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import urllib.error
import urllib.request

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

FABRIC = "https://api.fabric.microsoft.com/v1"
WRITE_ROLES = {"Admin", "Member", "Contributor"}


def az_exe() -> str:
    exe = shutil.which("az") or shutil.which("az.cmd")
    if not exe:
        sys.exit("Azure CLI (`az`) not found on PATH. Install it and run `az login`.")
    return exe


def az_token(resource: str) -> str:
    out = subprocess.run(
        [az_exe(), "account", "get-access-token", "--resource", resource,
         "--query", "accessToken", "-o", "tsv"],
        capture_output=True, text=True)
    if out.returncode != 0:
        sys.exit(f"az token failed for {resource}: {out.stderr.strip()}")
    return out.stdout.strip()


def get(path: str, token: str):
    req = urllib.request.Request(FABRIC + path, headers={"Authorization": "Bearer " + token})
    try:
        with urllib.request.urlopen(req) as r:
            body = r.read()
            return r.status, (json.loads(body) if body else {})
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")


def get_all(path: str, token: str):
    """GET a collection, following continuationUri. Fabric pages at 100."""
    items, url, pages = [], FABRIC + path, 0
    while url:
        req = urllib.request.Request(url, headers={"Authorization": "Bearer " + token})
        try:
            with urllib.request.urlopen(req) as r:
                body = r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode(errors="replace")
        payload = json.loads(body) if body else {}
        items.extend(payload.get("value", []))
        url = payload.get("continuationUri")
        pages += 1
        if pages > 100:
            break
    return 200, {"value": items}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workspace", help="inspect one workspace by display name or GUID")
    args = ap.parse_args()

    who = subprocess.run([az_exe(), "account", "show", "-o", "json"],
                         capture_output=True, text=True)
    acct = json.loads(who.stdout) if who.returncode == 0 else {}
    print(f"signed in as : {acct.get('user', {}).get('name', '?')}")
    print(f"tenant       : {acct.get('tenantId', '?')}")

    token = az_token("https://api.fabric.microsoft.com")

    status, payload = get_all("/workspaces", token)
    if status == 401:
        print("\nFAIL  HTTP 401 UserNotLicensed — the account has no Power BI/Fabric licence.")
        print("      Sign in once at https://app.fabric.microsoft.com to self-provision.")
        return 1
    if status != 200:
        print(f"\nFAIL  /workspaces returned {status}: {payload}")
        return 1
    workspaces = payload.get("value", [])
    print(f"licence      : OK — {len(workspaces)} workspace(s) visible")

    status, payload = get("/capacities", token)
    caps = payload.get("value", []) if status == 200 else []
    active = [c for c in caps if str(c.get("state", "")).lower() == "active"]
    fabric_caps = [c for c in active if not str(c.get("sku", "")).startswith("PP")]
    print(f"capacities   : {len(active)} active, {len(fabric_caps)} Fabric-capable "
          f"(non-PPU); Lakehouse needs an F-SKU or Trial (FT1), not PPU")

    if args.workspace:
        match = [w for w in workspaces
                 if args.workspace in (w.get("displayName"), w.get("id"))]
        if not match:
            # A GUID may resolve directly even when it is not in the list
            # (for example when it lives in a different tenant context).
            st, direct = get("/workspaces/%s" % args.workspace, token)
            if st == 200 and isinstance(direct, dict) and direct.get("id"):
                match = [direct]
            else:
                print(f"\nFAIL  workspace '{args.workspace}' is not accessible to this account.")
                print(f"      Direct lookup returned HTTP {st}.")
                print("      If it belongs to another tenant, sign in there first:")
                print("        az login --tenant <tenant-id>")
                return 1
        workspaces = match

    print()
    print(f"{'workspace':<44} {'role':<12} {'capacity':<38} lakehouses")
    print("-" * 120)
    writable = 0
    for ws in sorted(workspaces, key=lambda w: (w.get("displayName") or "").lower()):
        ws_id, name = ws["id"], ws.get("displayName", "?")
        st, roles = get(f"/workspaces/{ws_id}/roleAssignments", token)
        # Only an Admin can read roleAssignments; 401/403 means we are not Admin.
        role = "?" if st != 200 else "|".join(sorted(
            {r.get("role", "?") for r in roles.get("value", [])
             if r.get("principal", {}).get("id", "").lower()
             == (acct.get("user", {}).get("name", "") or "").lower()} or {"Admin"}))
        cap = ws.get("capacityId") or "(none - Pro/personal)"
        st, items = get(f"/workspaces/{ws_id}/items?type=Lakehouse", token)
        lakes = [i.get("displayName") for i in items.get("value", [])] if st == 200 else []
        if st == 200:
            writable += 1
        if args.workspace or lakes or st == 200:
            print(f"{name[:43]:<44} {role[:11]:<12} {cap[:37]:<38} "
                  f"{', '.join(lakes) if lakes else '-'}")

    print()
    print(f"{writable} workspace(s) allow item enumeration (Viewer or better).")
    print("To deploy you need Admin/Member/Contributor on the target workspace and a")
    print("Fabric capacity (F-SKU or Trial) assigned to it.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
