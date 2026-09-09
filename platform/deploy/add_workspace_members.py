#!/usr/bin/env python3
"""
Add members to a Fabric workspace as Admins (idempotent).

PREREQUISITE (must be done by a tenant admin first):
  The target users must already exist in Entra ID — either as members or as
  invited B2B guests. A 'Global Reader' CANNOT invite them; that needs
  Guest Inviter / User Administrator / Global Administrator.

Usage:
  az login
  python3 platform/deploy/add_workspace_members.py \
      --workspace davidshreyasalison \
      --role Admin \
      alison.pouw@contoso.com david.rodriguez@contoso.com

What it does:
  1. Resolves each UPN/email -> Entra object id via Microsoft Graph.
  2. Assigns the requested workspace role via the Fabric REST API.
  3. Skips anyone already assigned (idempotent).

Requires: az CLI logged into the tenant that owns the workspace.
"""
import argparse
import json
import shutil
import subprocess
import sys
import urllib.parse
import urllib.request


def az_exe():
    """On Windows the CLI is az.cmd, which CreateProcess will not resolve from
    the bare name 'az'. shutil.which honours PATHEXT and returns the full path."""
    exe = shutil.which("az") or shutil.which("az.cmd")
    if not exe:
        sys.exit("Azure CLI (`az`) not found on PATH. Install it and run `az login`.")
    return exe


def az_token(resource):
    out = subprocess.run(
        [az_exe(), "account", "get-access-token", "--resource", resource,
         "--query", "accessToken", "-o", "tsv"],
        capture_output=True, text=True,
    )
    if out.returncode != 0:
        sys.exit(f"az token failed for {resource}: {out.stderr.strip()}")
    return out.stdout.strip()


def graph_lookup(upn):
    """Resolve a UPN/email to an Entra object id (handles guest #EXT# form)."""
    # az rest handles the corp proxy that blocks direct graph.microsoft.com curls
    filt = (f"userPrincipalName eq '{upn}' or mail eq '{upn}' "
            f"or otherMails/any(m:m eq '{upn}')")
    url = ("https://graph.microsoft.com/v1.0/users?"
           f"$filter={urllib.parse.quote(filt)}&$select=id,displayName,userPrincipalName")
    out = subprocess.run(
        [az_exe(), "rest", "--method", "GET", "--url", url,
         "--headers", "ConsistencyLevel=eventual", "--query", "value", "-o", "json"],
        capture_output=True, text=True,
    )
    if out.returncode != 0:
        print(f"  ! Graph lookup failed for {upn}: {out.stderr.strip()}")
        return None
    rows = json.loads(out.stdout or "[]")
    if not rows:
        print(f"  ! {upn} not found in directory — invite them as a guest first.")
        return None
    return rows[0]


def fabric_get(token, path):
    req = urllib.request.Request(
        f"https://api.fabric.microsoft.com/v1{path}",
        headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req) as r:
        return json.load(r)


def resolve_workspace(token, name_or_id):
    ws = fabric_get(token, "/workspaces")["value"]
    for w in ws:
        if w.get("id") == name_or_id or w.get("displayName") == name_or_id:
            return w["id"]
    sys.exit(f"Workspace '{name_or_id}' not found. Available: "
             + ", ".join(w.get("displayName", "?") for w in ws))


def assign_role(token, ws_id, principal_id, display, role):
    body = json.dumps({
        "principal": {"id": principal_id, "type": "User"},
        "role": role,
    }).encode()
    req = urllib.request.Request(
        f"https://api.fabric.microsoft.com/v1/workspaces/{ws_id}/roleAssignments",
        data=body, method="POST",
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req)
        print(f"  ✓ {display}: assigned {role}")
    except urllib.error.HTTPError as e:
        msg = e.read().decode()
        if e.code == 400 and "already" in msg.lower():
            print(f"  = {display}: already has a role (skipped)")
        else:
            print(f"  ! {display}: HTTP {e.code} {msg}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("upns", nargs="+", help="UPNs/emails to add")
    ap.add_argument("--workspace", required=True, help="workspace name or id")
    ap.add_argument("--role", default="Admin",
                    choices=["Admin", "Member", "Contributor", "Viewer"])
    args = ap.parse_args()

    fabric = az_token("https://api.fabric.microsoft.com")
    ws_id = resolve_workspace(fabric, args.workspace)
    print(f"Workspace: {args.workspace} ({ws_id})")
    print(f"Role: {args.role}\n")

    for upn in args.upns:
        print(f"- {upn}")
        u = graph_lookup(upn)
        if not u:
            continue
        assign_role(fabric, ws_id, u["id"], u.get("displayName", upn), args.role)


if __name__ == "__main__":
    main()
