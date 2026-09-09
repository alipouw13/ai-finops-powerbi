"""Probe a tenant for REAL AI-cost data availability.

Answers "what can we actually wire up?" with evidence instead of assumption,
and is the source of the availability table in docs/real-data-spec.md. Read-only
throughout: it lists and counts, never writes.

Covers:
  * Azure subscriptions            -> Cost Management (real invoiced spend)
  * Cognitive Services / Azure ML  -> token telemetry feasibility
  * Fabric capacities              -> platform self-cost
  * Microsoft Graph                -> identities + licence inventory
  * Power Platform + Dataverse     -> Copilot Studio agents and credit events

    az login --tenant <tenant-id>
    python platform/validate/probe_real_sources.py

Stdlib only.
"""
import json, shutil, subprocess, sys, urllib.error, urllib.request

for s in (sys.stdout, sys.stderr):
    if hasattr(s, "reconfigure"):
        s.reconfigure(encoding="utf-8", errors="replace")

az = shutil.which("az") or shutil.which("az.cmd")


def run(args):
    p = subprocess.run([az] + args, capture_output=True, text=True)
    return p.returncode, p.stdout.strip(), p.stderr.strip()


def tok(resource):
    rc, out, _ = run(["account", "get-access-token", "--resource", resource,
                      "--query", "accessToken", "-o", "tsv"])
    return out if rc == 0 else None


def get(url, token, api=None):
    r = urllib.request.Request(url, headers={"Authorization": "Bearer " + token})
    try:
        with urllib.request.urlopen(r, timeout=120) as x:
            return x.status, json.loads(x.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")[:200]


print("=" * 74)
print("REAL-DATA AVAILABILITY PROBE")
print("=" * 74)

rc, out, _ = run(["account", "show", "--query", "{u:user.name,t:tenantId}", "-o", "json"])
print("signed in:", out)
print()

# ---------------------------------------------------------------- Azure ARM
arm = tok("https://management.azure.com")
print("--- Azure subscriptions (Cost Management source)")
if not arm:
    print("    no ARM token")
    subs = []
else:
    s, d = get("https://management.azure.com/subscriptions?api-version=2020-01-01", arm)
    subs = [x for x in d.get("value", [])] if s == 200 else []
    for x in subs:
        print(f"    {x['displayName'][:44]:46} {x['subscriptionId']}  {x['state']}")
    if not subs:
        print("    none visible", d if s != 200 else "")
print()

# ------------------------------------------------- Azure OpenAI / AI Foundry
print("--- Azure OpenAI / Cognitive Services accounts (token telemetry source)")
found_aoai = 0
for x in subs:
    sid = x["subscriptionId"]
    s, d = get(f"https://management.azure.com/subscriptions/{sid}/providers/"
               f"Microsoft.CognitiveServices/accounts?api-version=2023-05-01", arm)
    if s == 200:
        for a in d.get("value", []):
            kind = a.get("kind", "?")
            print(f"    {a['name'][:34]:36} kind={kind:16} {a['location']}")
            found_aoai += 1
    elif s == 403:
        print(f"    {x['displayName'][:30]}: 403 (no reader on this sub)")
if not found_aoai:
    print("    none found")
print()

# ------------------------------------------------------------- Azure ML / AI
print("--- Azure AI Foundry (Machine Learning) workspaces")
found_ml = 0
for x in subs:
    sid = x["subscriptionId"]
    s, d = get(f"https://management.azure.com/subscriptions/{sid}/providers/"
               f"Microsoft.MachineLearningServices/workspaces?api-version=2024-04-01", arm)
    if s == 200:
        for a in d.get("value", []):
            print(f"    {a['name'][:34]:36} {a.get('location','')}")
            found_ml += 1
if not found_ml:
    print("    none found")
print()

# ----------------------------------------------------------- Fabric capacity
print("--- Fabric capacities (capacity cost source)")
fab = tok("https://api.fabric.microsoft.com")
if fab:
    s, d = get("https://api.fabric.microsoft.com/v1/capacities", fab)
    if s == 200:
        for c in d.get("value", []):
            print(f"    {c.get('displayName','?')[:40]:42} sku={c.get('sku'):6} "
                  f"{c.get('state')}")
    else:
        print("   ", s, d)
print()

# ------------------------------------------------------------ Microsoft Graph
print("--- Microsoft Graph (identity + licence inventory)")
gr = tok("https://graph.microsoft.com")
if not gr:
    print("    no Graph token")
else:
    s, d = get("https://graph.microsoft.com/v1.0/organization", gr)
    if s == 200 and d.get("value"):
        o = d["value"][0]
        print(f"    tenant: {o.get('displayName')}")
    s, d = get("https://graph.microsoft.com/v1.0/subscribedSkus", gr)
    if s == 200:
        skus = d.get("value", [])
        print(f"    subscribedSkus: {len(skus)} SKU(s)")
        for k in skus:
            pre = k.get("prepaidUnits", {}).get("enabled", 0)
            print(f"      {k.get('skuPartNumber','')[:40]:42} enabled={pre:6} "
                  f"consumed={k.get('consumedUnits',0)}")
    else:
        print("    subscribedSkus:", s, str(d)[:120])
    s, d = get("https://graph.microsoft.com/v1.0/users?$select=id&$top=1&$count=true",
               gr)
    print("    users readable:", "yes" if s == 200 else f"no ({s})")
    s, d = get("https://graph.microsoft.com/v1.0/servicePrincipals?$select=id&$top=1", gr)
    print("    servicePrincipals readable:", "yes" if s == 200 else f"no ({s})")
print()

# --------------------------------------------------------- Power Platform
print("--- Power Platform environments (Copilot Studio source)")
pp = tok("https://service.powerapps.com/")
if not pp:
    print("    no Power Platform token (may need consent)")
else:
    s, d = get("https://api.bap.microsoft.com/providers/Microsoft.BusinessAppPlatform/"
               "scopes/admin/environments?api-version=2020-10-01", pp)
    envs = d.get("value", []) if s == 200 else []
    if s != 200:
        print("   ", s, str(d)[:160])
    print(f"    {len(envs)} environment(s)")
    for e in envs:
        props = e.get("properties", {})
        name = props.get("displayName", "?")
        url = (props.get("linkedEnvironmentMetadata") or {}).get("instanceApiUrl")
        print(f"      {name[:38]:40} {props.get('environmentSku','')}")
        if not url:
            print("        no Dataverse database -> no agent or credit telemetry")
            continue
        dv = tok(url.rstrip("/") + "/")
        if not dv:
            print("        could not acquire a Dataverse token")
            continue
        for tbl, label in [("bots", "agents"), ("msdyn_aievents", "credit events")]:
            st, dd = get(f"{url.rstrip('/')}/api/data/v9.2/{tbl}?$count=true&$top=3", dv)
            if st == 200:
                n = dd.get("@odata.count", len(dd.get("value", [])))
                names = [str(r.get("name") or r.get("msdyn_name") or "?")[:38]
                         for r in dd.get("value", [])[:3]]
                print(f"        {label:14} {n:5} row(s)"
                      + (f"  e.g. {', '.join(names)}" if names else ""))
            else:
                print(f"        {label:14} HTTP {st}")
