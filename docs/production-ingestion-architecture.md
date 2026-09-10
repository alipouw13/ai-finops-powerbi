# Production ingestion architecture — how this runs on a schedule

> **Scope.** How the AI FinOps medallion gets its data in production: what calls
> each API, what identity it runs as, what permissions that identity needs, and
> how the layers are orchestrated.
>
> Statements marked **[verified]** were proven against a live tenant during
> build-out; everything else is a design recommendation and is labelled as such.

---

## 1. The short answer on Webhook activity

**Don't use it.** The Webhook activity is not a general-purpose "call an API"
activity — it calls an endpoint, hands it a **callback URL, and then blocks the
pipeline until that endpoint calls back**. It is also POST-only.

None of the sources here (Consumption, Resource Graph, Azure Monitor, Graph,
Dataverse, GitHub) accept a callback URL or invoke one. A Webhook activity
pointed at them would fire the request and then sit there until it times out.

The natural next thought is the **Web activity**, and that is also wrong for the
big feeds, for a concrete reason: **its output payload is capped at 4 MB** and it
has no pagination. Our real Azure cost extract pulled **61,340 line items over 62
pages** **[verified]** — that has to stream to a sink, not accumulate in a
pipeline variable.

| Activity | What it's actually for | Fit here |
|---|---|---|
| **Webhook** | Long-running external job that calls back when finished | ✗ nothing calls back |
| **Web** | Small control-plane calls: trigger a refresh, post a message | ~ only for tiny payloads |
| **Copy (REST source)** | Paginated data movement, streamed to a sink | ✓ the default choice |
| **Notebook** | Multi-step logic, fan-out, several token audiences | ✓ where Copy can't reach |

Web activity still has a job here — starting the Direct Lake refresh and posting
failure notifications — just not data movement.

---

## 2. For Azure cost specifically: don't call the API at all

The highest-value recommendation in this document.

Azure Cost Management has a **native scheduled Export**: it writes daily/monthly
actual-cost data as CSV/Parquet into a storage account on its own schedule. You
then put a **OneLake shortcut** over that container and Bronze reads it as if it
were local. No API calls, no pagination, no throttling, no 4 MB ceiling.

This matters because of something we hit directly: the **Cost Management Query
API is permanently HTTP 429 in the test tenant** — it never succeeded across 11+
retries with backoff. `Microsoft.Consumption/usageDetails` worked, but it is a
paginated read that is unpleasant at enterprise volume **[verified]**.

```
Cost Management Export ──► ADLS Gen2 container ──► OneLake shortcut ──► Bronze
        (native, scheduled)                          (no copy, no compute)
```

Two behaviours to design around, both **[verified]**:

- Exports **replace** the month-to-date file each run, so Bronze must append and
  de-duplicate rather than trust the file as a delta.
- Azure **restates** recent usage. Two rows can share resource, meter, day and
  price and still be different charges, differing only by resource tag. De-duplicate
  on the **whole natural key**; a hand-picked subset silently merged distinct
  charges and lost 824 rows / $166.99 in testing.

Keep `extract_real_bronze.py` as the fallback for tenants where you cannot
configure an Export, and for the non-cost sources.

---

## 3. Proposed architecture

```
                       ┌─────────────────────────── Fabric workspace ──────────────────────────┐
Cost Mgmt Export ─► ADLS ─shortcut─►│                                                          │
                                    │  PL_master (scheduled, daily 06:00)                      │
Resource Graph  ─┐                  │    ├─ PL_ingest        (metadata-driven, parallel)       │
Azure Monitor    ├─ Notebook ──────►│    │    ForEach source in control table                  │
Graph            │  (SPN token)     │    │      └─ Switch: Copy activity | Notebook            │
Dataverse       ─┘                  │    ├─ NB_bronze_load   (CSV/JSON ─► Delta, append+dedupe)│
                                    │    ├─ NB_silver        (14 curated entities)             │
GitHub REST ─── Copy activity ─────►│    ├─ NB_gold          (star + RI gates)                 │
                                    │    ├─ Semantic model refresh (Direct Lake reframe)       │
                                    │    └─ On failure ─► Teams/Activator alert                │
                                    └──────────────────────────────────────────────────────────┘
```

### Why metadata-driven

A control table (`bronze_ctl_sources`) lists every feed with its ingestion type,
auth audience, endpoint, watermark column and enabled flag. `PL_ingest` does
`Lookup → ForEach → Switch`. Adding a source becomes a row, not a pipeline edit,
and one failing source doesn't take the others down.

| Column | Purpose |
|---|---|
| `source_key` | `azure_cost`, `entra_identities`, … |
| `ingest_type` | `copy` \| `notebook` \| `shortcut` |
| `auth_audience` | which token to mint (ARM / Graph / PowerPlatform / Dataverse / GitHub) |
| `endpoint` | base URL or notebook name |
| `watermark_column` / `watermark_value` | incremental high-water mark |
| `lookback_days` | trailing window to reprocess for late-arriving restatements |
| `enabled` | kill switch per source, no redeploy |

### Which tool per source

| Source | Tool | Why not Copy |
|---|---|---|
| Azure invoiced cost | **Shortcut** over Export | — (best case: no ingestion code at all) |
| Azure cost (fallback) | Notebook | `startDate`/`endDate` semantics + full-key dedupe **[verified]** |
| Resource inventory | Notebook | Resource Graph is a POST with a KQL body and its own `$skipToken` |
| Azure Monitor metrics | Notebook | fan-out: one call per resource × metric, then union |
| Entra identities / licences | **Copy** | plain paginated Graph GET, `@odata.nextLink` |
| M365 Copilot usage | **Copy** | Graph **beta** endpoint; v1.0 returns 400 **[verified]** |
| Copilot Studio credits | Notebook | environment discovery, then a per-environment API URL |
| GitHub Copilot | **Copy** | paginated REST, `Link` header |

---

## 4. Identity model

Everything in production runs **non-interactively**. Today the extractors run on
`az login` delegated tokens, which is fine for a laptop and unacceptable for a
schedule.

| Concern | Choice | Rationale |
|---|---|---|
| Ingestion identity | **One SPN per trust boundary**, not one global SPN | Azure-read, Graph-read, Dataverse and GitHub are different blast radii. One compromised secret shouldn't grant all four. |
| Secret storage | **Azure Key Vault**, referenced by a Fabric connection | Never a secret in a notebook, pipeline parameter or the repo. |
| Fabric → Key Vault | **Workspace identity** with `Key Vault Secrets User` | Removes the bootstrap secret problem — no credential needed to fetch credentials. |
| Fabric → OneLake | **Workspace identity** | Native, nothing to rotate. |
| Rotation | 90-day secrets, or **certificate credentials** | Certificates are preferable; they can be rotated without a co-ordinated redeploy. |
| Copilot Studio invoke | **Delegated** (exception) | App-only is rejected: `405 App-only S2S access is not enabled for this environment` **[verified]**. See §6. |

---

## 5. Permissions matrix

Least privilege per source. Every role below is a **read** role except where the
pipeline must write to Fabric.

### Azure data plane

| Source | API | Permission | Scope | Granted by |
|---|---|---|---|---|
| Invoiced cost | Cost Management Export | **Cost Management Contributor** (to create the export) then **Storage Blob Data Reader** for the reader | Subscription / MG / billing profile | Subscription Owner |
| Invoiced cost (API fallback) | `Microsoft.Consumption/usageDetails` | **Cost Management Reader** | Subscription, or **Billing account reader** at EA/MCA scope | Subscription Owner / Billing admin |
| Resource inventory | Azure Resource Graph | **Reader** | Every subscription in scope — Resource Graph honours RBAC and silently returns fewer rows without it | Subscription Owner |
| AI telemetry | Azure Monitor metrics | **Monitoring Reader** | Resource group holding the AI resources | RG Owner |

> **[verified]** Enumerate AI accounts via **Resource Graph**, not the
> per-subscription `Microsoft.CognitiveServices/accounts` listing: the latter
> returned **0** accounts where Resource Graph returned **16**.

### Microsoft Graph (application permissions + admin consent)

| Source | Permission | Notes |
|---|---|---|
| Users | `User.Read.All` | |
| Service principals | `Application.Read.All` | `Directory.Read.All` covers both but is broader |
| Licence inventory | `Organization.Read.All` | `subscribedSkus` |
| M365 Copilot usage | `Reports.Read.All` | **beta** endpoint only **[verified]** |

> **Gotcha:** M365 admin centre → *Settings → Org settings → Reports* has
> **"Display concealed user, group, and site names"**. While enabled, usage
> reports return pseudonymised UPNs and per-user attribution is impossible.
> Turning it off is a privacy decision, not just a config change — involve your
> privacy/works-council stakeholders before flipping it.

### Power Platform / Dataverse

| Need | Permission |
|---|---|
| List environments | SPN registered with Power Platform + **Power Platform Administrator** |
| Read `msdyn_aievent`, `bot` | An **application user** created *in each Dataverse environment*, bound to a custom security role with **Read** on those tables |
| Invoke agents | `CopilotStudio.Copilots.Invoke` — **delegated**, see §6 |

> Create a **custom security role** rather than granting System Administrator.
> The application user only needs Read on two tables.

### GitHub

| Need | Credential | Scope |
|---|---|---|
| Copilot seats + billing | **GitHub App** (preferred) or org-owned fine-grained PAT | `Copilot` read, `Administration: read`, `manage_billing:copilot` |

> Prefer a GitHub App: it is org-owned, has short-lived installation tokens, and
> survives the departure of whoever created it. A personal PAT is a person-shaped
> single point of failure.

### Fabric / Power BI

| Need | Permission | Tenant setting that must be on |
|---|---|---|
| Write Bronze/Silver/Gold | Workspace **Contributor** (workspace identity or SPN) | — |
| Run pipelines/notebooks via API | Workspace **Contributor** | *Service principals can use Fabric APIs* |
| Refresh the Direct Lake model | Workspace **Member** | *Service principals can use Power BI APIs* |
| Read secrets | **Key Vault Secrets User** on the vault | — |
| Shortcut to ADLS | **Storage Blob Data Reader** on the container | — |

---

## 6. The Copilot Studio exception

Worth calling out because it will surprise whoever implements this.

Copilot Studio credit consumption (`msdyn_aievent`) reads fine with an
application user — that part is ordinary. **Invoking** an agent is not:

- Requesting `CopilotStudio.Copilots.Invoke` with the **Azure CLI's** client id
  fails with `AADSTS65002 … must be configured via preauthorization`. Only
  Microsoft, as the API owner, can preauthorise a first-party client, so **no
  amount of tenant admin consent fixes it** **[verified]**.
- A **tenant-owned app registration** has no such restriction and works.
- **App-only** tokens are issued by Entra carrying the app role, then rejected by
  the service: `405 App-only S2S access is not enabled for this environment`
  **[verified]**.

So agent invocation is **delegated-only** and therefore not schedulable without a
long-lived refresh token — which is a credential-hygiene problem, not a solution.

**Recommendation:** don't schedule invocation. Ingest Copilot Studio *consumption*
on the schedule like everything else, and let real users generate the usage. The
traffic generator (`generate_studio_traffic.py`) exists for demo seeding, not for
production.

Also budget for latency: `msdyn_aievent` is a **billing** surface, not a live
trace, and lagged **>1 h** in testing (documented as up to ~24 h) **[verified]**.
Don't alert on "no credits today".

---

## 7. Orchestration and scheduling

**One master pipeline with real dependencies — not four independent schedules.**
Time-offset scheduling ("gold runs 30 min after silver") eventually races and
Gold reads a half-written Silver.

```
PL_master
  ├─ PL_ingest            (ForEach, parallel, continue-on-error per source)
  ├─ NB_bronze_load       (on success of ingest)
  ├─ NB_silver            (on success)
  ├─ NB_gold              (on success)
  ├─ Web: refresh model   (on success)   ← Direct Lake needs a framing refresh [verified]
  └─ Teams alert          (on failure of any)
```

| Concern | Recommendation |
|---|---|
| Cadence | Daily. Cost data restates for days; hourly buys nothing and multiplies throttling. |
| Late-arriving data | Reprocess a **trailing 7-day window** each run and let the dedupe collapse it. |
| Throttling | Copy activity **Request interval** ≥ 100 ms; exponential backoff on 429 in notebooks. Cost Management throttles aggressively **[verified]**. |
| Idempotency | Bronze appends and de-duplicates on the full natural key, so a re-run is a no-op — proven by running the pipeline twice to identical totals **[verified]**. |
| Failure isolation | `continue-on-error` per source; a Graph outage must not block Azure cost. |
| Observability | Persist notebook tracebacks to `Files/_errors/` — Fabric surfaces only "System cancelled the Spark session" **[verified]**. |
| Model refresh | Direct Lake tables are not queryable until framed; make the refresh an explicit pipeline step, not an afterthought. |

---

## 8. Setup runbook

Ordered so each step's prerequisite already exists.

1. **Create the app registrations** (one per trust boundary):
   `finops-azure-read`, `finops-graph-read`, `finops-dataverse-read`.
   Prefer certificate credentials.
2. **Grant Azure RBAC** — Cost Management Reader + Reader + Monitoring Reader per
   §5. Do it at the management group if you have more than a couple of
   subscriptions.
3. **Grant Graph application permissions** and **admin consent** them.
4. **Register the Dataverse application user** in *each* environment and bind a
   custom read-only security role.
5. **Create the GitHub App**, install it on the org, store the private key in
   Key Vault.
6. **Configure the Cost Management Export** to an ADLS Gen2 container (daily,
   actual cost, Parquet).
7. **Create the Key Vault**, store every secret, grant the Fabric **workspace
   identity** `Key Vault Secrets User`.
8. **Enable the two tenant settings**: service principals can use Fabric APIs and
   Power BI APIs. Scope them to a security group containing only these SPNs.
9. **Create the workspace and four lakehouses** — bronze, bronze_real, silver,
   gold. Create them **with schemas enabled**: without it there is no `dbo`
   schema, every `saveAsTable` fails `SCHEMA_NOT_FOUND`, and the Load Table REST
   API rejects the lakehouse outright **[verified]**.
10. **Create the OneLake shortcut** over the Export container.
11. **Deploy notebooks and pipelines** (`fabric_deploy.py` already does the
    notebook half).
12. **Seed the control table**, with everything `enabled = 0`.
13. **Enable one source at a time**, verifying row counts and `_data_class`
    before enabling the next.
14. **Schedule `PL_master`** and confirm two consecutive runs produce identical
    totals — the idempotency check.

---

## 9. What to decide before building

1. **Export vs API for cost.** Export is strictly better at scale; the API path
   only wins if you cannot get Cost Management Contributor to create one.
2. **Concealed user names.** Per-user Copilot attribution is impossible while
   that setting is on. It is a privacy decision with a works-council dimension in
   some geographies, not an ops toggle.
3. **Management group vs per-subscription RBAC.** MG scope is far less
   maintenance; some orgs won't permit it.
4. **GitHub App vs PAT.** A PAT tied to an individual will break when they move
   teams.
5. **Do you need Copilot Studio invocation at all?** Almost certainly not in
   production — real users are the traffic.

---

## 10. Evidence log

Behaviours proven during build-out that this design accounts for. Every one of
these returned HTTP 200 or otherwise looked healthy while being wrong, which is
why they are worth writing down.

| Finding | Consequence |
|---|---|
| `usageDetails` **ignores** an OData `$filter` on dates and silently returns only the open billing period — 8 days instead of 91 | Use `startDate`/`endDate`; assert the returned day count |
| Azure **restates** cost; two charges can differ only by resource tag | De-duplicate on the whole natural key, never a subset |
| Cost Management **Query API** permanently 429 in the tenant | Prefer Export; treat the API as a fallback |
| Per-subscription AI account listing returned 0; Resource Graph returned 16 | Always enumerate via Resource Graph |
| `getMicrosoft365CopilotUsageUserDetail` is **beta**-only; v1.0 returns 400 | Pin the beta endpoint and expect churn |
| Copilot Studio app-only S2S rejected with 405 | Invocation is delegated-only |
| Azure CLI cannot request the Copilot Studio scope (`AADSTS65002`) | Tenant-owned app registration required |
| Schema-enabled lakehouses reject the Load Table REST API | Load via Spark |
| Fabric hides notebook tracebacks | Persist them to `Files/_errors/` |
| Direct Lake tables aren't queryable until framed | Explicit refresh step after Gold |
| Pagination caps look like successful completion | Warn loudly when a page cap is hit |
