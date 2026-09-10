"""Drive real conversations against Copilot Studio agents.

Copilot Studio bills by *consumed credit*, and `msdyn_aievent` — the table the
Power Platform admin centre bills from — stays empty until an agent is actually
run. No API can extract spend that was never incurred, so the only honest way to
get real Copilot Studio cost into the model is to generate real usage.

This drives published agents over the Copilot Studio conversations API and then
lets platform/fabric/extract_real_bronze.py pick up the resulting events.

WHY A CUSTOM APP REGISTRATION IS REQUIRED
-----------------------------------------
The conversations API needs the `CopilotStudio.Copilots.Invoke` scope on the
Power Platform API (8578e004-a5c6-46e7-913e-12f58912df43). Asking for it with
the Azure CLI's client id fails, and not for a reason any tenant admin can fix:

    AADSTS65002: Consent between first party application '04b07795-...'
    (Azure CLI) and first party resource '8578e004-...' (Power Platform API)
    must be configured via preauthorization -- applications owned and operated
    by Microsoft must get approval from the API owner before requesting tokens
    for that API.

Only Microsoft, as the API owner, can preauthorize a first-party client. A
tenant-owned app registration has no such restriction, so `--setup` creates one,
grants admin consent, and signs in with the device-code flow.

App-only (client-credentials) tokens are accepted by Entra but rejected by the
service with `405 App-only S2S access is not enabled for this environment`, so
this uses a delegated token. That also keeps the generated events attributable
to a real user, which is what the report is trying to demonstrate.

Usage
-----
    # one-time: create the app registration, consent, and sign in
    python platform/fabric/generate_studio_traffic.py --setup

    # drive traffic (default: every published agent it can find)
    python platform/fabric/generate_studio_traffic.py --turns 12

    # then re-extract; the credits flow through bronze -> silver -> gold
    python platform/fabric/extract_real_bronze.py \
        --i-understand-this-is-real-tenant-data

Stdlib only, per repo convention.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")

GRAPH = "https://graph.microsoft.com"
PP_API = "https://api.powerplatform.com"
PP_APP_ID = "8578e004-a5c6-46e7-913e-12f58912df43"
INVOKE_SCOPE = "CopilotStudio.Copilots.Invoke"
APP_NAME = "finops-copilot-studio-probe"
API_VERSION = "2022-03-01-preview"

# Token cache lives outside the repo. It is a real user token for a real
# tenant, so it must never be committed.
CACHE = os.path.join(os.path.expanduser("~"), ".finops_studio_token.json")

PROMPTS = [
    "hello",
    "what can you help me with?",
    "summarise the latest underwriting brief",
    "what is our current policy on renewals?",
    "show me the open claims for this week",
    "thanks, that is all",
]


def az_exe():
    exe = shutil.which("az") or shutil.which("az.cmd")
    if not exe:
        sys.exit("Azure CLI (`az`) not found on PATH.")
    return exe


def sh(args):
    p = subprocess.run([az_exe()] + args, capture_output=True, text=True)
    return p.returncode, p.stdout.strip(), p.stderr.strip()


def az_token(resource):
    rc, out, _ = sh(["account", "get-access-token", "--resource", resource,
                     "--query", "accessToken", "-o", "tsv"])
    return out if rc == 0 else None


def http(method, url, token=None, body=None, form=None, timeout=120):
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        ctype = "application/x-www-form-urlencoded"
    elif body is not None:
        data = json.dumps(body).encode()
        ctype = "application/json"
    else:
        data, ctype = None, None
    headers = {}
    if token:
        headers["Authorization"] = "Bearer " + token
    if ctype:
        headers["Content-Type"] = ctype
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="replace")
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, raw
    except Exception as e:                                        # noqa: BLE001
        return 0, str(e)


def tenant_id():
    rc, out, _ = sh(["account", "show", "--query", "tenantId", "-o", "tsv"])
    return out if rc == 0 else None


# ------------------------------------------------------------------- setup
def setup():
    """Create the app registration, grant admin consent, sign in."""
    g = az_token(GRAPH)
    if not g:
        sys.exit("Could not get a Graph token. Run `az login --tenant <id>`.")

    st, sps = http("GET", f"{GRAPH}/v1.0/servicePrincipals"
                          f"?$filter=appId eq '{PP_APP_ID}'", g)
    if st != 200 or not sps.get("value"):
        sys.exit(f"Power Platform API service principal not found ({st}). "
                 f"It must exist in the tenant before consent can be granted.")
    pp_sp = sps["value"][0]
    role = next((r for r in pp_sp["appRoles"] if r["value"] == INVOKE_SCOPE), None)
    scope = next((s for s in pp_sp["oauth2PermissionScopes"]
                  if s["value"] == INVOKE_SCOPE), None)
    if not scope:
        sys.exit(f"{INVOKE_SCOPE} is not exposed as a delegated scope here.")

    access = [{"id": scope["id"], "type": "Scope"}]
    if role:
        access.append({"id": role["id"], "type": "Role"})
    desired = {
        "displayName": APP_NAME,
        "signInAudience": "AzureADMyOrg",
        "isFallbackPublicClient": True,
        "publicClient": {"redirectUris": [
            "https://login.microsoftonline.com/common/oauth2/nativeclient"]},
        "requiredResourceAccess": [{"resourceAppId": PP_APP_ID,
                                    "resourceAccess": access}],
    }

    st, ex = http("GET", f"{GRAPH}/v1.0/applications"
                         f"?$filter=displayName eq '{APP_NAME}'", g)
    if st == 200 and ex.get("value"):
        app = ex["value"][0]
        http("PATCH", f"{GRAPH}/v1.0/applications/{app['id']}", g, desired)
        print(f"  reusing app registration {app['appId']}")
    else:
        st, app = http("POST", f"{GRAPH}/v1.0/applications", g, desired)
        if st not in (200, 201):
            sys.exit(f"could not create the app registration: {app}")
        print(f"  created app registration {app['appId']}")

    st, mine = http("GET", f"{GRAPH}/v1.0/servicePrincipals"
                           f"?$filter=appId eq '{app['appId']}'", g)
    if st == 200 and mine.get("value"):
        my_sp = mine["value"][0]
    else:
        st, my_sp = http("POST", f"{GRAPH}/v1.0/servicePrincipals", g,
                         {"appId": app["appId"]})
        if st not in (200, 201):
            sys.exit(f"could not create the service principal: {my_sp}")
    print(f"  service principal {my_sp['id']}")

    st, grants = http("GET", f"{GRAPH}/v1.0/oauth2PermissionGrants"
                             f"?$filter=clientId eq '{my_sp['id']}'", g)
    if st == 200 and not grants.get("value"):
        st, r = http("POST", f"{GRAPH}/v1.0/oauth2PermissionGrants", g, {
            "clientId": my_sp["id"], "consentType": "AllPrincipals",
            "resourceId": pp_sp["id"], "scope": INVOKE_SCOPE})
        print(f"  admin consent granted ({st})")
    else:
        print("  admin consent already present")

    device_login(app["appId"])


def device_login(client_id):
    tid = tenant_id()
    st, r = http("POST",
                 f"https://login.microsoftonline.com/{tid}/oauth2/v2.0/devicecode",
                 form={"client_id": client_id,
                       "scope": f"{PP_API}/{INVOKE_SCOPE} offline_access"})
    if st != 200:
        sys.exit(f"device code request failed: {r}")
    print("\n  " + "=" * 58)
    print(f"   Go to : {r['verification_uri']}")
    print(f"   Code  : {r['user_code']}")
    print("  " + "=" * 58 + "\n")

    deadline = time.time() + r.get("expires_in", 900)
    while time.time() < deadline:
        time.sleep(r.get("interval", 5))
        st, t = http("POST",
                     f"https://login.microsoftonline.com/{tid}/oauth2/v2.0/token",
                     form={"grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                           "client_id": client_id, "device_code": r["device_code"]})
        if st == 200:
            save_token(client_id, tid, t)
            print("  signed in; token cached outside the repo at "
                  f"{CACHE}")
            return t["access_token"]
        if isinstance(t, dict) and t.get("error") not in (
                "authorization_pending", "slow_down"):
            sys.exit(f"sign-in failed: {t.get('error_description', t)}")
    sys.exit("device code expired")


def save_token(client_id, tid, t):
    blob = {"client_id": client_id, "tenant": tid,
            "access_token": t["access_token"],
            "refresh_token": t.get("refresh_token", ""),
            "expires_at": time.time() + int(t.get("expires_in", 3600)) - 120}
    with open(CACHE, "w", encoding="utf-8") as fh:
        json.dump(blob, fh)
    try:
        os.chmod(CACHE, 0o600)
    except OSError:
        pass


def load_token():
    """Cached delegated token, refreshed silently when it has expired."""
    if not os.path.exists(CACHE):
        sys.exit("No cached token. Run with --setup first.")
    with open(CACHE, encoding="utf-8") as fh:
        blob = json.load(fh)
    if time.time() < blob.get("expires_at", 0):
        return blob["access_token"]
    if blob.get("refresh_token"):
        st, t = http("POST", f"https://login.microsoftonline.com/"
                             f"{blob['tenant']}/oauth2/v2.0/token",
                     form={"grant_type": "refresh_token",
                           "client_id": blob["client_id"],
                           "refresh_token": blob["refresh_token"],
                           "scope": f"{PP_API}/{INVOKE_SCOPE} offline_access"})
        if st == 200:
            save_token(blob["client_id"], blob["tenant"], t)
            return t["access_token"]
    sys.exit("Cached token expired and could not be refreshed. Re-run --setup.")


# ------------------------------------------------------------------ agents
def env_host(env_id):
    """Copilot Studio addresses an environment by a split, dash-less id."""
    e = env_id.replace("-", "").lower()
    return f"{e[:-2]}.{e[-2:]}.environment.api.powerplatform.com"


def discover_agents():
    """Published agents, per environment, from Dataverse."""
    pp = az_token("https://service.powerapps.com/")
    if not pp:
        sys.exit("Could not get a Power Platform token.")
    st, payload = http("GET", "https://api.bap.microsoft.com/providers/"
                              "Microsoft.BusinessAppPlatform/scopes/admin/"
                              "environments?api-version=2020-10-01", pp)
    if st != 200:
        sys.exit(f"could not list environments: {payload}")
    out = []
    for env in payload.get("value", []):
        props = env.get("properties", {})
        api = (props.get("linkedEnvironmentMetadata") or {}).get("instanceApiUrl")
        if not api:
            continue
        dv = az_token(api.rstrip("/") + "/")
        if not dv:
            continue
        st, bots = http("GET", f"{api.rstrip('/')}/api/data/v9.2/bots"
                               f"?$select=botid,name,schemaname,publishedon", dv)
        for b in bots.get("value", []) if st == 200 else []:
            out.append({"env_id": env.get("name", ""),
                        "env_name": props.get("displayName", "?"),
                        "name": b.get("name", ""),
                        "schema": b.get("schemaname", ""),
                        "published": bool(b.get("publishedon"))})
    return out


def converse(token, env_id, schema, turns, delay):
    """One conversation; returns (turns_sent, replies_received)."""
    base = (f"https://{env_host(env_id)}/copilotstudio/dataverse-backed/"
            f"authenticated/bots/{schema}/conversations")
    st, conv = http("POST", f"{base}?api-version={API_VERSION}", token, {})
    if st != 200 or not isinstance(conv, dict) or "conversationId" not in conv:
        return 0, 0, f"start failed ({st}): {str(conv)[:160]}"
    cid = conv["conversationId"]
    url = f"{base}/{cid}?api-version={API_VERSION}"
    sent = replies = 0
    for i in range(turns):
        text = PROMPTS[i % len(PROMPTS)]
        # The service expects the activity wrapped; a bare activity is a 400.
        st, resp = http("POST", url, token, {"activity": {
            "type": "message", "text": text,
            "from": {"id": "finops-traffic", "role": "user"}}})
        if st != 200:
            return sent, replies, f"turn {i + 1} failed ({st}): {str(resp)[:160]}"
        sent += 1
        replies += len(resp.get("activities", []) if isinstance(resp, dict) else [])
        time.sleep(delay)
    return sent, replies, None


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--setup", action="store_true",
                    help="create the app registration, consent, and sign in")
    ap.add_argument("--conversations", type=int, default=3,
                    help="conversations per agent (default 3)")
    ap.add_argument("--turns", type=int, default=6,
                    help="messages per conversation (default 6)")
    ap.add_argument("--delay", type=float, default=1.0,
                    help="seconds between turns, to stay polite (default 1)")
    ap.add_argument("--agent", action="append", default=None,
                    help="restrict to these agent schema names (repeatable)")
    ap.add_argument("--list", action="store_true",
                    help="list discoverable agents and exit")
    args = ap.parse_args()

    if args.setup:
        setup()
        return 0

    agents = discover_agents()
    published = [a for a in agents if a["published"]]
    if args.agent:
        published = [a for a in published if a["schema"] in args.agent]

    print(f"{len(agents)} agent(s) found, {len(published)} published")
    for a in agents:
        flag = "published" if a["published"] else "UNPUBLISHED - skipped"
        print(f"  {a['name'][:44]:46} {flag}")
    if args.list:
        return 0
    if not published:
        print("\nNothing to drive. Copilot Studio only bills published agents, and\n"
              "an unpublished agent cannot be invoked over the API at all — publish\n"
              "one in the maker portal, then re-run.")
        return 1

    token = load_token()
    total_sent = total_replies = 0
    print()
    for a in published:
        for c in range(args.conversations):
            sent, replies, err = converse(token, a["env_id"], a["schema"],
                                          args.turns, args.delay)
            total_sent += sent
            total_replies += replies
            status = err or f"{sent} turn(s), {replies} reply activity(ies)"
            print(f"  {a['name'][:32]:34} conv {c + 1}/{args.conversations}: {status}")
            if err:
                break

    print(f"\n{total_sent} message(s) sent, {total_replies} activity(ies) received.")
    print("\nCopilot Studio credit telemetry is a BILLING surface, not a live trace:\n"
          "msdyn_aievent can lag by up to ~24h. Re-run extract_real_bronze.py\n"
          "afterwards and dim_platform[data_source] flips to REAL on its own.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
