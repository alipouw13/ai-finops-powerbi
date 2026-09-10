# Real-Data Spec — wiring live sources into the AI FinOps accelerator

> **Status:** proposal. Grounded in a read-only probe of a Microsoft
> partner (MCAP) sandbox tenant, run 2026-09-08. Every availability claim below
> was verified against the live API, not assumed. Tenant, subscription and
> resource identifiers are redacted; re-run
> `platform/validate/probe_real_sources.py` against your own tenant to
> reproduce the table.

---

## 1. Recommendation: do **not** build a parallel stack

The ask was "another model, another report, three more lakehouses for real
data". That is the wrong shape, and the repo already has the right one.

**Provenance in this accelerator is a data column, not a code branch.** Every
bronze row carries `_data_class` (`REAL` / `MOCK`); gold derives
`dim_platform[data_source]` from it; every fact row carries
`cost_is_estimated`. A REAL Azure feed and a MOCK GitHub feed can sit in the
*same* fact table and the report already tells them apart — that is the
product's central claim.

Forking into a parallel stack costs you:

| | One pipeline | Parallel stack |
|---|---|---|
| Lakehouses | 3 (+1 landing) | 6 |
| Semantic models | 1 | 2 |
| Reports | 1 | 2 |
| Places a schema fix must be applied | 1 | 2 |
| Demo narrative | "here is real spend beside modelled spend, and the model says which is which" | two disconnected demos |

Every defect found while building this — duplicate dimension keys, orphaned
foreign keys, the TMDL triple-backtick trap, gold/CSV contract drift — would
need fixing twice, and the two would silently diverge.

### What to build instead

```
                     ┌─ MOCK collectors  → LH_tokenomics_bronze      (existing)
bronze landing ──────┤
                     └─ REAL collectors  → LH_tokenomics_bronze_real (NEW, 1 lakehouse)
                                    │
                                    ▼
              LH_tokenomics_silver  (unchanged — unions both, keeps _data_class)
                                    │
                                    ▼
              LH_tokenomics_gold    (unchanged — derives provenance)
                                    │
                                    ▼
              AIFinOps.DirectLake + AIFinOps.DirectLake.Report (unchanged)
```

**One new lakehouse, not three.** It exists purely as a *security boundary*:
real tenant cost and identity data is more sensitive than synthetic demo data
and deserves its own permission set. Silver, gold, the model and the report all
stay single.

---

## 2. What is actually available (verified)

| Source | Status | Evidence from the probe |
|---|---|---|
| **Azure Cost Management** | ✅ REAL | 1 subscription. **91 days, 61,340 line items, $5,809.81** |
| **Entra identities** | ✅ REAL | 694 principals (users + service principals), both readable |
| **M365 licence inventory** | ✅ REAL | 5 subscribed SKUs returned |
| **Azure AI resource inventory** | ✅ REAL | **16 Cognitive Services accounts** — 8 `AIServices`, 3 `OpenAI`, plus ContentSafety / FormRecognizer / Speech / Translation |
| **Fabric capacity cost** | ✅ REAL | 9 capacities; 2 Active (one F2, one PPU) |
| **Copilot Studio** | ⚠️ SPLIT | **8 real agents exist** across 2 environments. But `msdyn_aievents` returns **0 rows**: the agents have never been run, so there is no credit consumption to report |
| **Azure OpenAI token telemetry** | ❌ NONE | 6 resources emit telemetry (3,923 calls over 31 days) but **every token metric is zero** — they are Content Understanding / Doc Intelligence / Speech / Translation, which bill per *call*. The 3 `OpenAI`-kind resources have no traffic at all |
| **M365 Copilot** | ❌ NONE | No `Microsoft_365_Copilot` SKU — confirms your read |
| **GitHub Copilot** | ❌ NONE | No org access |

> An earlier draft of this table claimed *0 Cognitive Services accounts*. That
> was a bug in the probe, not a fact about the tenant: it listed accounts
> per-subscription, which returned nothing, and swallowed the non-200 response.
> Azure Resource Graph returns all 16. **Always enumerate via Resource Graph.**

### Two API traps that fail silently

1. **`usageDetails` ignores `$filter` on dates.** A request filtered with
   `properties/usageStart ge '…'` returns HTTP 200 and *only the open billing
   period* — 8 days instead of the 91 requested. The dedicated `startDate` /
   `endDate` query parameters are honoured. Nothing errors, so the extract just
   quietly covers the wrong window; `extract_real_bronze.py` now prints the day
   count and warns when it looks truncated.
2. **Paging caps look like completion.** The `nextLink` walk stopped at a
   40-page guard and returned exactly 40,000 rows, which reads as a plausible
   total. The cap now warns loudly when more data was available.

Also: `usageDetails` echoes its `$filter` back inside `nextLink` **unencoded**,
so the continuation URL contains raw spaces and `http.client` rejects it —
continuation URLs must be re-quoted before use.

### The honest headline

The real data you can get is **identity, licence and infrastructure cost** —
*not AI usage telemetry*. That means:

- Pages **5 CFO**, **6 Governance**, **8 Application Owner** and **1 Spend
  Overview** carry genuinely real, invoiced spend.
- Pages **2 Foundry Tokenomics** and **7 Engineering** stay MOCK, because
  nothing in the tenant is currently emitting tokens.

If real tokenomics matters for the demo, see Phase 0 below — it is a small,
cheap unlock.

---

## 3. Phase 0 (optional, ~30 min) — unlock real tokenomics

Deploy one Azure OpenAI resource and push a little traffic through it. This is
the single highest-value addition, because it turns the *token* story real:

1. Create an Azure OpenAI (or AI Foundry) resource in your subscription.
2. Deploy a cheap model (`gpt-4o-mini`).
3. Enable **diagnostic settings → Log Analytics** on it.
4. Send a few hundred requests (a loop script is enough).

That yields real `processed_prompt_tokens` / `generated_tokens` /
`latency_ms` / per-request identity, which is exactly what
`bronze_azure_ai_metrics` expects. Cost is a few cents.

Without this, treat tokenomics as permanently MOCK and say so on the page.

---

## 4. Collector specs

Each collector writes ONE bronze table with the schema the existing silver layer
already consumes, plus the standard lineage columns
(`_ingested_at`, `_source_system`, `_source_api`, `_watermark`, `_batch_id`,
`_data_class='REAL'`). No silver or gold change is required for any of them.

### 4.1 `bronze_azure_cost` — REAL invoiced Azure spend ★ highest value

| | |
|---|---|
| Source | Cost Management **Exports** → storage → OneLake shortcut |
| Auth | `az login` (you are Owner/admin on the subscription) |
| Permission | Cost Management Reader on the target subscription |
| Cadence | Daily; exports are month-to-date and **replace**, so bronze must append + dedupe on `(usage_date, resource_id, meter_id)` |
| Columns | `usage_date, subscription_id, resource_group, resource_id, meter_id, meter_name, meter_category, quantity, unit_price, cost_usd, currency, tags_json` |

> **Do not use the Cost Management *Query* API for recurring ingestion.** The
> probe hit `HTTP 429` on every attempt across 7 retries with backoff — this
> tenant is aggressively throttled. Scheduled **Exports** to a storage account
> are Microsoft's supported pattern for FinOps ingestion and have no such limit.
> Query API is fine for ad-hoc exploration only.

This is the only feed that produces genuinely **billed** dollars
(`cost_is_estimated = FALSE`), which is what makes `Cost Confidence %`
meaningful rather than decorative.

### 4.2 `bronze_ref_identity_map` — REAL identity dimension ★ high value

| | |
|---|---|
| Source | Graph `/users`, `/servicePrincipals` |
| Auth | delegated, already working |
| Permission | `User.Read.All`, `Directory.Read.All` (you have them) |
| Volume | 61 users + 633 service principals |
| Columns | `identity_key, display_name, principal_type, upn, entra_object_id, github_login, is_human, department, home_business_unit_key, cost_center_key` |

`department` from the Entra profile feeds the business-unit rollup, so
chargeback becomes real rather than fabricated. `github_login` and
`cost_center_key` have no Entra source — leave blank or supply a mapping file;
they stay MOCK and should be labelled as such.

**Privacy note:** this pulls real names and UPNs. It must land in the
`_real` lakehouse and must never be committed. `extract_m365_graph.py` already
enforces both (gitignored output + an explicit
`--i-understand-this-is-real-tenant-data` flag).

### 4.3 `bronze_m365_license_inventory` — REAL licence entitlement

Already built and proven: `platform/fabric/extract_m365_graph.py`. Point it at
this tenant and it emits the 5 real SKUs. Note the Copilot usage report will
return 404/403 here because there is no Copilot service plan — that is the
correct, truthful result.

### 4.4 `bronze_fabric_capacity_cost` — REAL Fabric spend

| | |
|---|---|
| Source | Cost Management (service = *Microsoft Fabric* / *Power BI Embedded*) + `/v1/capacities` for SKU metadata |
| Billable today | one F2 capacity (Active), plus a PPU reservation |
| Columns | `usage_date, capacity_id, sku, meter_name, quantity, cost_usd, tags_json` |

Pleasingly self-referential: the platform reports its own cost.

### 4.5 `bronze_ref_agent_inventory` — REAL agents ✅ / `bronze_studio_credits` — not yet

The Dataverse check has been run. The two halves land differently:

**Agent inventory — REAL and available now.**

| | |
|---|---|
| Source | Dataverse `bots` table, per environment |
| Auth | delegated; a Dataverse token per environment API URL |
| Found | **8 agents** across 2 Dataverse environments |
| Examples | line-of-business underwriting and orchestration agents |
| Columns | `agent_key, agent_name, platform, environment_id, owner_upn, owner_business_unit_key, purpose, created_on` |

This matters more than it looks: agent → owner → business unit is what makes
**agent chargeback** real, and non-human spend attribution is the accelerator's
core IP.

**Credit consumption — REAL source, currently empty.**

| | |
|---|---|
| Source | Dataverse `msdyn_aievents` |
| Result | **0 rows in both environments** |
| Meaning | The agents exist but have never been invoked, so no credits have been consumed |
| To unlock | Run a few conversations against one agent, then re-query |
| Note | `msdyn_creditconsumed` is already net of zero-rating — use it directly, never model credits from activity counts |

Wire the collector now and it will return a truthful zero; it starts producing
real numbers the moment someone uses an agent. Do **not** substitute mock rows
into a table labelled REAL.

### 4.6 `bronze_azure_ai_metrics` — REAL tokens (only after Phase 0)

| | |
|---|---|
| Source | Azure Monitor metrics on the AOAI resource, or Log Analytics if diagnostics are on |
| Blocker | No Cognitive Services account exists today |

---

## 5. Changes required outside the collectors

Deliberately small:

| Component | Change |
|---|---|
| `fabric_deploy.py` | add `--lakehouse-bronze-real`; upload real extracts there |
| `10_conform_usage.py` | read each bronze table from **both** bronze lakehouses and union; carry `_data_class` through to silver |
| `20_build_star.py` | `provenance()` already derives from `_data_class` — **no change** |
| Semantic model | **no change** |
| Report | **no change** — the Governance page's REAL-vs-MOCK register updates itself |

The one genuinely new piece of logic is the silver union, and it is a few lines,
because provenance was designed as data from the start.

---

## 6. Phasing

| Phase | Deliverable | Effort | Unlocks |
|---|---|---|---|
| **1** | `bronze_ref_identity_map` from Graph | S | Real identities, real BU rollup → pages 6, 9 |
| **2** | `bronze_m365_license_inventory` | XS | Already built; just retarget |
| **3** | `bronze_ref_agent_inventory` from Dataverse | S | 8 real agents → real agent chargeback |
| **4** | Cost Management **Export** → `bronze_azure_cost` | M | Real billed $ → pages 1, 5; makes Cost Confidence real |
| **5** | `bronze_fabric_capacity_cost` | S | Platform self-cost |
| **0/6** | AOAI resource + traffic → real tokens | M | Pages 2, 7 |
| **7** | `bronze_studio_credits` | S | Truthful zero today; real once agents are used |

Phases 1–3 are worth doing regardless: quick, no prerequisites, and they make
the identity, licence and agent stories genuine.

### What "real" would look like after phases 1–5

| Page | Today | After |
|---|---|---|
| 1 Spend Overview | MOCK | **REAL** Azure + Fabric cost |
| 2 Foundry Tokenomics | MOCK | MOCK (unless Phase 0) |
| 3 Waste & Utilisation | MOCK | **REAL** seats, MOCK activity |
| 4 Rate Card | list prices | list prices (unchanged by design) |
| 5 CFO Finance | MOCK | **REAL** billed spend vs budget |
| 6 Governance | MOCK | **REAL** identities and provenance mix |
| 7 Engineering | MOCK | MOCK (unless Phase 0) |
| 8 Application Owner | MOCK | partly REAL via resource tags |
| 9 License Optimization | MOCK | **REAL** SKUs and assignments |
| 10 Extractable Spectrum | catalogue | catalogue, with REAL flags flipped |

---

## 7. What stays MOCK — and must keep saying so

- **GitHub Copilot** — no org access
- **M365 Copilot usage/credits** — no SKU in tenant
- **`github_login` ↔ UPN mapping** — no automated source
- **Budgets, criticality, SLA tiers** — customer inputs, never derivable
- **Rate card** — published list prices, not your contract

All of these already carry MOCK markers and `is_mock*` flags. The Governance
page will show a genuine mix of REAL and MOCK rows, which is a **better** demo
than either extreme: it demonstrates the model handles partial coverage
honestly.

---

## 8. Open questions

1. ~~Do any Copilot Studio agents exist?~~ **Answered:** yes — 8 agents across
   the two environments, but `msdyn_aievents` is empty, so credits are zero
   until someone actually runs one.
2. **Is Phase 0 (deploy AOAI + generate traffic) acceptable?** It is the only
   route to real tokenomics and costs pennies. Without it, pages 2 and 7 stay
   MOCK permanently and should be labelled that way on the page.
3. **Sensitivity:** real UPNs, display names and department values will be in
   the model. Fine for a personal admin tenant; confirm before any screen-share
   or before this workspace is shared more widely.
4. **Retention:** Cost Management exports replace month-to-date. Bronze must
   append and dedupe on `(usage_date, resource_id, meter_id)`, or you lose
   prior-day history. Confirm you want history kept from day one.
5. **Blended or separated?** Recommendation is blended — one fact table with
   `_data_class` distinguishing rows, because that *is* the demo. Say so if you
   would rather the report defaulted to a REAL-only filter.

---

## 8. What was actually built (implemented and verified)

Decisions taken: **keep all history**, **display real names**, **blended** —
one fact table carrying both provenances rather than a parallel stack.

### Topology

A fourth lakehouse, `LH_tokenomics_bronze_real`, holds every live extract. Real
and mock are separated by a **physical boundary**, not a column filter, so
"which of these numbers is real" is answerable by pointing at storage. Silver
unions the two and carries `_data_class` per row; gold derives each platform's
provenance label from it, so no label is ever hand-written.

```
LH_tokenomics_bronze       (MOCK)  ─┐
                                    ├─► LH_tokenomics_silver ─► LH_tokenomics_gold
LH_tokenomics_bronze_real  (REAL)  ─┘
```

### Two new platforms, deliberately not one

| Platform | What it is | 91-day spend |
|---|---|---|
| `AzureAI` | Cognitive Services / AI Foundry / ML meters — AI spend proper | **$500.62** |
| `AzureInfra` | Storage, search, database, networking the AI workloads run on | **$5,309.18** |

Merging them would overstate AI platform spend; dropping `AzureInfra` would
understate what these workloads actually cost. Both are wrong, so the model
reports them side by side and lets the reader pick the denominator.

### Grain and idempotency

Azure emits several `usageDetails` line items per resource-meter-day (one per
deployment, benefit, reservation) that are identical across every projected
column. They are **not** duplicates, so they cannot be dropped — but they are
not separately meaningful either. The extractor sums them onto the declared
grain and records `line_item_count`, which makes re-extraction idempotent:
61,340 line items → 17,775 rows, $5,809.81 preserved exactly.

The loader then appends and de-duplicates on *the grain minus the measures*,
keeping the newest extract. De-duplicating on every column instead looks safer
and is wrong: Azure **restates** recent usage, so a restated row differs in
`cost_usd`, fails to match the row it supersedes, and both survive. That was
observed live — 17,956 rows / $5,838.74 against a true 17,775 / $5,809.81, a
silent $28.93 double-count.

### Attribution

`bronze_ref_app_inventory` is built from every resource that either is an AI
account or incurred cost — 144 real applications — so real spend lands on a real
workload name instead of `APP-UNKNOWN`. Verified at 100% key coverage.

Business-unit attribution is a different story: Azure resources here carry no
owning-BU tag, so real spend routes to `BU-UNALLOC` and chargeback coverage
falls to ~46%. That is not a defect to paper over — an unattributable majority
of real AI spend is precisely the finding this accelerator exists to surface.

### Defects found and fixed along the way

| Symptom | Cause |
|---|---|
| Trend axis read `2026-06, 2026-09, 2026-07, 2026-08` | Charts sorted by measure; a time axis must sort by time. Invisible while the data spanned only two months |
| CFO page showed −100% variance for every BU | `gen_bronze_data.py` pinned `END = date(2026, 8, 28)`, so every MTD/forecast measure read BLANK once today passed it. Now anchored on `date.today()` |
| Foundry cost confidence rendered blank | `DIVIDE(BLANK(), x)` is BLANK. A platform with no billed cost has 0% confidence, not unknown |
| New lakehouse rejected every write | Created without `enableSchemas`, so `dbo` did not exist |
| Notebook failure reported only "System cancelled the Spark session" | The loader raised `SystemExit`, a `BaseException`, which slipped past the deploy wrapper's `except Exception` and left no traceback |

---

## 9. Copilot Studio: why there is no spend, and what unlocks it

### Findings

| Check | Result |
|---|---|
| Agents in tenant | **8** across 2 environments |
| Published | **1 of 8** (`Test`, `Contoso` env, published 2026-06-02). The 7 interesting ones have `publishedon: null` |
| `msdyn_aievent` readable | ✅ HTTP 200 in both environments — the collector works |
| Credit consumption | **0 events, 0 credits** |
| Licensing | `Power_Virtual_Agents` (1 seat, **0 assigned**) + `CCIBOTS_PRIVPREV_VIRAL` (2 assigned) |
| Authentication mode | `authenticationmode=2` on every agent — Entra auth required to converse |

The collector is now wired (`collect_studio_credits`) and writes
`bronze_studio_credits` with an explicit schema **even at zero rows**, so the
moment an agent is run the data flows with no code change. A zero-row REAL feed
is a meaningful statement, not a failure.

### Why the spend is zero

Not an API limitation. **The agents have never been run.** No API can extract
spend that was never incurred, so the only route to real Copilot Studio cost is
to generate genuine usage.

### Why automating the conversation is blocked

The correct endpoint is
`https://{env}.environment.api.powerplatform.com/copilotstudio/dataverse-backed/authenticated/bots/{schema}/conversations`.
It accepted an Azure CLI token and rejected it with
`403 InsufficientDelegatedPermissions` — the route is right, the scope is
missing.

Requesting that scope directly fails harder:

```
AADSTS65002: Consent between first party application '04b07795-…' (Azure CLI)
and first party resource '8578e004-…' (Power Platform API) must be configured
via preauthorization — applications owned and operated by Microsoft must get
approval from the API owner before requesting tokens for that API.
```

This is decisive: **no amount of tenant admin consent can fix it.** Azure CLI is
a first-party app and only Microsoft, as the API owner, can preauthorise it.
`CopilotStudio.Copilots.Invoke` requires a **custom app registration** plus admin
consent plus one interactive sign-in. The Power Platform API service principal
does exist in the tenant, so that path is available.

### Resolved: the custom app registration works

`platform/fabric/generate_studio_traffic.py --setup` builds exactly that path and
it is now **verified end to end** — 48 real messages sent to a published agent,
48 reply activities received, HTTP 200 throughout.

Two findings from getting there:

1. **App-only (client credentials) is a dead end.** Entra happily issues a token
   carrying the `CopilotStudio.Copilots.Invoke` *app role*, and the service then
   rejects it with `405 App-only S2S access is not enabled for this environment`.
   There is no corresponding toggle in the Dataverse `organization` settings, so
   the delegated flow is the supported route. It also keeps the generated events
   attributable to a real user, which is what the report wants anyway.
2. **The request body must wrap the activity.** A bare
   `{"type":"message","text":"..."}` is a silent `400` with an empty body;
   `{"activity": {...}}` returns `200` with the bot's reply activities.

The script only drives **published** agents, because an unpublished agent cannot
be invoked over the API at all — and Copilot Studio only bills published ones.

### Still outstanding: the telemetry lag

`msdyn_aievent` and `conversationtranscript` were both still **0 rows**
immediately after the traffic run. That is expected — `msdyn_aievent` is a
*billing* surface rather than a live trace and can lag by up to ~24h. The
collector is already wired, so re-running `extract_real_bronze.py` after the lag
window picks the credits up with no code change.

### Recommended unlock (2 minutes, beats the automation)

Open any agent in Copilot Studio → **Publish** → chat with it in the test pane.
That produces genuine sessions and credits. Then re-run
`extract_real_bronze.py`; `dim_platform[data_source]` flips itself to
`MOCK/REAL` with no code change.

Or drive it from the CLI, which does the same thing repeatably:

```bash
# one-time: app registration + admin consent + device-code sign-in
python platform/fabric/generate_studio_traffic.py --setup

python platform/fabric/generate_studio_traffic.py --list
python platform/fabric/generate_studio_traffic.py --conversations 6 --turns 8
```

Two caveats worth setting expectations on:

- Copilot Studio credit telemetry **lags** — `msdyn_aievent` is a billing
  surface, not a live trace, so allow up to ~24h before the rows appear.
- Credits are real but **dollars are not**: Dataverse reports credits, never
  currency. Silver prices them from the rate card and marks those rows
  `cost_is_estimated=True`, so they never masquerade as invoiced spend.

### On the corporate-tenant "Personal Usage V2" report

Declined, for two independent reasons:

1. **Governance** — it lives in Microsoft's corporate tenant
   corporate tenant. Copying corporate telemetry into an MCAP sandbox moves
   Microsoft-internal data across a tenant boundary into an unmanaged
   environment, and into a report intended for screen-sharing.
2. **Accuracy** — it is scoped to a *single user's own* usage. An admin pulls
   `getMicrosoft365CopilotUsageUserDetail` (Graph beta), the Viva Copilot
   Dashboard, or the GitHub org metrics API, all tenant-scoped. Presenting
   personal-scope figures as admin-grade would be exactly the relabelling this
   accelerator exists to prevent.
