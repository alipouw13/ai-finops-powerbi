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

### Can Cost Management export straight to OneLake?

**No.** The export Destination tab offers a **Storage type** (Azure blob storage
or ADLS Gen2) and asks for a storage subscription, resource group, storage
account, container and directory. There is no OneLake or Fabric destination.

That is not a gap you have to work around, though — **ADLS + shortcut is
Microsoft's own published FinOps-on-Fabric pattern**
([Create a Fabric workspace for FinOps](https://learn.microsoft.com/cloud-computing/finops/fabric/create-fabric-workspace-finops)),
so this is the sanctioned path rather than a workaround:

```
Cost Management Export ──► ADLS Gen2 ──► OneLake shortcut ──► Bronze
     (native, scheduled)   (container)   (no copy, no compute)
```

Because the shortcut is a *reference*, not a copy, the data is not duplicated and
no Fabric compute is spent moving it. The only real cost is storage.

This matters because of something we hit directly: the **Cost Management Query
API is permanently HTTP 429 in the test tenant** — it never succeeded across 11+
retries with backoff. `Microsoft.Consumption/usageDetails` worked, but it is a
paginated read that is unpleasant at enterprise volume **[verified]**.

### Export it in FOCUS format

Use **FOCUS** (FinOps Open Cost and Usage Specification) rather than the legacy
actual/amortised schema. It is provider-agnostic, so the same Silver conforming
logic extends to AWS/GCP cost later without reshaping the model. Microsoft's
FinOps guidance defaults to it.

> One scope caveat: the **management group scope is not supported** for FOCUS
> cost-and-usage exports. Export per subscription (or at billing-profile scope)
> and let Silver union them.

### Three export behaviours to design around

All three are documented behaviour, and two are **[verified]** here:

1. **Exports partition large files automatically**, and Microsoft explicitly warns
   *"avoid hardcoding or guessing partition names, as file naming conventions may
   change"*. Read the directory with a wildcard and let Spark handle multi-file
   ingestion — never enumerate expected filenames.
2. **Exports replace the month-to-date file** each run, so Bronze must append and
   de-duplicate rather than treat the file as a delta **[verified]**.
3. **Azure restates recent usage.** Two rows can share resource, meter, day and
   price and still be different charges, differing only by resource tag.
   De-duplicate on the **whole natural key**; a hand-picked subset silently merged
   distinct charges and lost 824 rows / $166.99 in testing **[verified]**.

Keep `extract_real_bronze.py` as the fallback for tenants where you cannot
configure an Export, and for the non-cost sources.

---

## 2b. Securing the storage account: trusted workspace access

The shortcut should not require the storage account to be open to the internet.
**Trusted workspace access** lets a Fabric workspace reach a *firewall-enabled*
ADLS Gen2 account, scoped to that specific workspace via a resource instance rule.

### Workspace identity is not a preference here — it is the only option

Worth stating plainly, because it removes a decision: for connections that use
trusted workspace access, **workspace identity is the only supported
authentication method.** The documentation is explicit that *"test connection
fails if organizational account or service principal authentication methods are
used."*

A workspace identity **is** a service principal — a Fabric-managed one, created
and rotated by Fabric rather than registered by hand. So it satisfies the
"no interactive credentials in production" requirement while removing the secret
entirely: there is nothing to store in Key Vault and nothing to rotate.

### Requirements

| Requirement | Detail |
|---|---|
| **Capacity** | A **purchased F SKU**. Trusted workspace access is **not supported on Trial capacities** — this is the requirement most likely to bite. *(The workspace used here is on **F4**, so it qualifies **[verified]**.)* |
| Workspace identity | Created on the workspace, and granted **Contributor** on the workspace itself via *Manage access* |
| Storage firewall | A **resource instance rule** naming the specific Fabric workspace |
| Storage RBAC | The identity needs **Storage Blob Data Reader** at storage-account scope (Contributor/Owner also work; Reader is sufficient and is the least privilege for an ingestion-only path) |
| Tenancy | **Not compatible with cross-tenant requests** — storage and Fabric must be in the same tenant |

### Two limitations to design around

- **Pipelines cannot write to OneLake table shortcuts** on storage accounts with
  trusted workspace access (documented as temporary). Harmless here: the shortcut
  is read-only ingest, and Bronze/Silver/Gold are written to native lakehouse
  tables.
- **Don't reuse the connection elsewhere.** A trusted-workspace-access connection
  reused in items other than shortcuts, pipelines and semantic models — or in
  another workspace — may silently stop working. Create a dedicated connection
  for this shortcut.

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
| Fabric → cost storage account | **Workspace identity** (mandatory) | Trusted workspace access accepts *no other* authentication method — see §2b. It also removes the secret entirely: Fabric creates and rotates this identity, so there is nothing to store or expire. |
| Rotation | 90-day secrets, or **certificate credentials** | Certificates are preferable; they can be rotated without a co-ordinated redeploy. Workspace identity needs neither. |
| Copilot Studio invoke | **Delegated** (exception) | App-only is rejected: `405 App-only S2S access is not enabled for this environment` **[verified]**. See §6. |

> **Terminology worth being precise about:** a *workspace identity* **is** a
> service principal — one that Fabric creates and manages, rather than an app
> registration you create. Prefer it wherever it is accepted, because it is the
> only option in this design with no credential to leak, store or rotate.

---

## 5. Permissions matrix

Least privilege per source. Every role below is a **read** role except where the
pipeline must write to Fabric.

### Azure data plane

| Source | API | Permission | Scope | Granted by |
|---|---|---|---|---|
| Invoiced cost | Cost Management Export | **Cost Management Contributor** (to create the export) | Subscription / billing profile | Subscription Owner |
| Cost storage (write) | Export → ADLS | Export writes via the Cost Management service; grant it **Storage Blob Data Contributor** on the account when prompted | Storage account | Storage Owner |
| Cost storage (read) | OneLake shortcut | **Storage Blob Data Reader** for the **Fabric workspace identity**, plus a **resource instance rule** naming the workspace | Storage account | Storage Owner |
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
application user — that part is ordinary. **Caveat:** Microsoft does **not**
document `msdyn_aievent` as a billing/consumption source; the documented Copilot
Credit capacity surface is Power Platform admin center → Licensing → Products →
Copilot Studio. Reading `msdyn_aievent` is community/unsupported practice — it works,
but it is not Microsoft guidance. **Invoking** an agent is not ordinary:

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
6. **Configure the Cost Management Export** to an ADLS Gen2 container — daily,
   **FOCUS** format, Parquet, file overwrite enabled. Export per subscription;
   management group scope is not supported for FOCUS.
7. **Lock down the storage account and wire trusted workspace access**:
   enable the storage firewall, create the **workspace identity** on the Fabric
   workspace, give it **Contributor** on the workspace, add a **resource instance
   rule** naming that workspace, and grant it **Storage Blob Data Reader**.
   Confirm the capacity is a **purchased F SKU** first — this step cannot work on
   a Trial capacity.
8. **Create the Key Vault**, store every remaining secret, grant the Fabric
   **workspace identity** `Key Vault Secrets User`.
9. **Enable the two tenant settings**: service principals can use Fabric APIs and
   Power BI APIs. Scope them to a security group containing only these SPNs.
10. **Create the workspace and four lakehouses** — bronze, bronze_real, silver,
    gold. Create them **with schemas enabled**: without it there is no `dbo`
    schema, every `saveAsTable` fails `SCHEMA_NOT_FOUND`, and the Load Table REST
    API rejects the lakehouse outright **[verified]**.
11. **Create the OneLake shortcut** over the Export container, using a
    **dedicated** connection authenticated with the workspace identity. Don't
    reuse that connection for anything else.
12. **Deploy notebooks and pipelines** (`fabric_deploy.py` already does the
    notebook half).
13. **Seed the control table**, with everything `enabled = 0`.
14. **Enable one source at a time**, verifying row counts and `_data_class`
    before enabling the next.
15. **Schedule `PL_master`** and confirm two consecutive runs produce identical
    totals — the idempotency check.

---

## 9. What to decide before building

1. **Export vs API for cost.** Export is strictly better at scale; the API path
   only wins if you cannot get Cost Management Contributor to create one.
2. **Capacity SKU.** Trusted workspace access needs a **purchased F SKU**. If the
   production workspace lands on a Trial capacity, the firewalled-storage design
   silently isn't available and you fall back to a public endpoint — decide this
   before someone provisions the workspace.
3. **Concealed user names.** Per-user Copilot attribution is impossible while
   that setting is on. It is a privacy decision with a works-council dimension in
   some geographies, not an ops toggle.
4. **Management group vs per-subscription RBAC.** MG scope is far less
   maintenance; some orgs won't permit it. Note FOCUS exports don't support MG
   scope regardless, so you will be exporting per subscription either way.
5. **GitHub App vs PAT.** A PAT tied to an individual will break when they move
   teams.
6. **Do you need Copilot Studio invocation at all?** Almost certainly not in
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
| M365 Copilot usage APIs moved under the `/copilot` segment (`copilotReportRoot`); the older `reportRoot` beta path is superseded | Call `getMicrosoft365CopilotUsageUserDetail` / `...UserCountSummary` / `...UserCountTrend` under `/copilot`; still last-activity dates only, no prompt counts |
| Copilot Studio app-only S2S rejected with 405 | Invocation is delegated-only |
| Azure CLI cannot request the Copilot Studio scope (`AADSTS65002`) | Tenant-owned app registration required |
| Schema-enabled lakehouses reject the Load Table REST API | Load via Spark |
| Fabric hides notebook tracebacks | Persist them to `Files/_errors/` |
| Direct Lake tables aren't queryable until framed | Explicit refresh step after Gold |
| Pagination caps look like successful completion | Warn loudly when a page cap is hit |
| Cost Management **cannot** export to OneLake; destination is a storage account only | ADLS + shortcut, which is Microsoft's own published FinOps-on-Fabric pattern |
| Trusted workspace access is **not supported on Trial capacities** | Confirm a purchased F SKU before designing around a firewalled storage account |
| Trusted-workspace-access connections accept **workspace identity only** | Not a preference — organizational account and SPN auth fail the connection test |
