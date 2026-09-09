# Fabric deployment — cost, unblock steps, and one-command deploy

This folder deploys the AI FinOps accelerator into **your** Microsoft Fabric
tenant: a workspace, a lakehouse, the medallion notebooks (bronze→silver→gold),
and your `data/*.csv` uploaded to OneLake. It is dependency-free (Python stdlib)
and authenticates through the Azure CLI you already have (`az login`).

---

## 1. Check what your account can actually do

Before anything else, run the read-only probe. It creates and changes nothing —
it just reports whether your user is licensed, which capacities are active, and
which workspaces you can write to:

```bash
python platform/validate/probe_fabric.py
python platform/validate/probe_fabric.py --workspace <name-or-guid>   # one workspace
```

If it reports `licence : OK` and at least one active Fabric capacity, you are
ready to deploy and can skip section 2.

## 2. If you are blocked — 2 clicks, $0

A user with **no Power BI / Fabric license** gets `UserNotLicensed` on every
Fabric REST call, and a tenant with no capacity has nowhere to put a lakehouse.
Neither is a code problem — both are fixed in **2 one-time browser clicks**.

1. **Get a free Power BI license.** Open <https://app.fabric.microsoft.com> and
   sign in with your work account. This self-service-provisions a **free Power BI
   license** for your user (no admin needed; it's the standard sign-up flow).
2. **Start the Fabric Trial.** In the Fabric portal top-right **Account manager
   ▸ Start trial**. You get a **60-day Fabric trial capacity (~F64 power, $0)**.

That's it. Re-run the preflight and it will go green:

```bash
python platform/deploy/fabric_deploy.py --steps preflight
```

> If self-service trials are disabled by your tenant admin, ask an admin to
> either enable trials or assign you a Pro license + an F-SKU capacity.
> Note that a **Premium-Per-User (PPU) capacity cannot host a Lakehouse** — you
> need a Trial (FT1) or an F-SKU.

## 3. Cost — so you don't burn your account

Fabric bills on **capacity compute time**, not per query. Storage in OneLake is
trivial. Key numbers:

| Option | Compute cost | Notes |
|---|---|---|
| **Fabric Trial (recommended)** | **$0 for 60 days** | ~F64 power. Perfect for the demo. Auto-expires — no runaway bill. |
| **F2 (smallest paid)** | ~$0.36 / hr → **~$262/mo if left on 24×7** | Enough for this model. **Pause when idle** ⇒ $0. |
| **F2 paused** | **$0 compute** | Pausing an F-SKU stops all compute billing; state is preserved. |
| **OneLake storage** | ~$0.023 / GB / mo | This dataset is a few MB ⇒ effectively $0. |
| **Copilot in Fabric** | included on F2+ | consumes capacity units while running; negligible for demo use. |

**How to not burn the account:**
- Use the **Trial** for the demo → $0.
- If you go paid, buy **F2**, and **pause it** whenever you're not demoing.
- Run this deployer with **`--delete-after`** to tear the workspace down when done.
- The Trial hard-expires at 60 days, so a forgotten trial can't cost you money.

## 4. One-command deploy (after unblocking)

```bash
# dry check first
python platform/deploy/fabric_deploy.py --steps preflight

# full deploy into a NEW workspace: workspace + lakehouse + CSV upload + notebooks + run
python platform/deploy/fabric_deploy.py --capacity <capacity-guid>

# deploy into an EXISTING workspace/lakehouse you already own
python platform/deploy/fabric_deploy.py \
    --workspace <workspace-guid> \
    --lakehouse <lakehouse-guid>

# full medallion into three lakehouses (the verified path)
python platform/deploy/fabric_deploy.py \
    --steps preflight workspace lakehouse upload notebooks run \
    --workspace        <workspace-guid> \
    --lakehouse        <bronze-guid> \
    --lakehouse-silver <silver-guid> \
    --lakehouse-gold   <gold-guid>

# blended: mock bronze + REAL tenant bronze in a fourth lakehouse
# (run platform/fabric/extract_real_bronze.py first)
python platform/deploy/fabric_deploy.py \
    --steps preflight workspace lakehouse upload upload-real notebooks run \
    --workspace             <workspace-guid> \
    --lakehouse             <bronze-guid> \
    --lakehouse-bronze-real LH_tokenomics_bronze_real \
    --lakehouse-silver      <silver-guid> \
    --lakehouse-gold        <gold-guid>

# deploy, demo, then remove everything ($0 residual)
python platform/deploy/fabric_deploy.py --capacity <capacity-guid> --delete-after
```

Real tenant extracts land in their **own** lakehouse, never beside the mock
data. The separation is physical rather than a column filter, so "which of
these numbers is real" is answered by pointing at storage. Silver unions both
and carries `_data_class` per row; gold derives each platform's REAL/MOCK label
from it, so the Governance page updates itself and no label is hand-written.

Lakehouses are created with **schemas enabled**, matching what the Fabric UI
now does by default. Without it there is no `dbo` schema, every
`saveAsTable("dbo.x")` fails with `SCHEMA_NOT_FOUND`, and the `Tables/dbo/...`
abfss paths the medallion notebooks use do not resolve either.

`--workspace` and `--lakehouse` accept either a **GUID** or a **display name**. A
GUID is only ever resolved, never created, so passing one cannot accidentally
create a second workspace; a display name is created if it does not exist.

`--capacity` matters in a large tenant: many capacities can be visible to you that
you do not own, and without it the deployer picks the first Trial/F-SKU it finds.
Run `platform/validate/probe_fabric.py` to list them. `--delete-after` is refused
when `--workspace` is a GUID, so teardown can only ever remove a workspace this
tool created.

Run a subset of stages with `--steps` (e.g. `--steps workspace lakehouse upload`).
Names can also be defaulted with env vars `FINOPS_WORKSPACE` / `FINOPS_LAKEHOUSE`.

### What each step does
| step | action |
|---|---|
| `preflight` | verify `az` login + Fabric token + that you're licensed |
| `capacity` | resolve `--capacity`, else find an active capacity (prefers Trial, excludes PPU) |
| `workspace` | resolve/create the target workspace, assign to capacity |
| `lakehouse` | resolve/create the target lakehouse |
| `upload` | push all `data/*.csv` to `Files/bronze` via OneLake (ADLS Gen2, GUID-addressed) |
| `notebooks` | import the notebooks, attaching the lakehouse as their default |
| `run` | execute them, polling to completion |
| teardown | `--delete-after` deletes the workspace |

> OneLake is addressed as `<workspaceGUID>/<itemGUID>/...`. The friendly-name form
> (`<workspace>/<item>.Lakehouse/...`) is rejected with `FriendlyNameSupportDisabled`
> in tenants that disable it, and breaks on workspace names containing spaces.

### Verified end-to-end

Run against workspace `AI-tokenomics` with three lakehouses
(`LH_tokenomics_bronze` / `_silver` / `_gold`): Bronze CSVs uploaded to OneLake,
notebooks imported with their lakehouse attached, and bronze → silver executed
to completion. Four things that path taught us:

**1. Only the medallion chain is runnable.** `00_load_bronze_csv`,
`10_silver_conform` and `20_gold_star` run as-is. `01_ingest_foundry_apim` and
`02_ingest_copilot_platforms` are *design scaffolds* containing placeholders such
as `LAKE = "abfss://finops@<lake>.dfs.core.windows.net"`, and fail if executed.
`--steps run` therefore defaults to the runnable set; use `--only` to override.

**2. Schema-enabled lakehouses cannot use the Load Table REST API.** A lakehouse
with `properties.defaultSchema` set (`"dbo"` by default on new lakehouses) rejects
both `/tables/{name}/load` and `/tables` with:

```
errorCode: UnsupportedOperationForSchemasEnabledLakehouse
```

Spark has no such restriction, which is why the CSV→Delta step is a notebook.
`load_bronze.py` detects this, still uploads the files, and tells you to run the
notebook step rather than reporting a wall of 400s.

**3. Notebooks must have a lakehouse attached to write tables.** The import sets
`metadata.dependencies.lakehouse.default_lakehouse`; without it Spark has no
default catalog and every `saveAsTable()` fails at runtime. Re-importing an
existing notebook updates its definition rather than silently reusing a stale one.

**4. Fabric hides notebook tracebacks.** A failed run reports only *"System
cancelled the Spark session due to statement execution failures"*. The import
wrapper therefore persists the traceback to `Files/_errors/<script>.log` in that
layer's lakehouse, and `step_run` prints it automatically on failure.

> Also note: the Load Table API wants `format` **inside** `formatOptions`. At the
> top level the service rejects the request with a bare
> `"An invalid request has been received"` that names no field.

## 5. Publishing the semantic model + report

The deployer builds the **lakehouse + gold tables**. To publish the
`AIFinOps.SemanticModel` + `AIFinOps.Report` on top, use either:

- **(a) Fabric Git integration** — in the workspace, *Workspace settings ▸ Git
  integration*, connect this GitHub repo, and *Update from Git*. Fabric imports
  the PBIP items natively. Best for a repeatable, source-controlled customer
  accelerator.
- **(b) Power BI Desktop Publish** — open `AIFinOps.pbip`, *Publish* to the
  `AI FinOps Accelerator` workspace. Fastest for a one-off demo.

Hand-crafting PBIP definition REST payloads is intentionally avoided (brittle);
Git integration is the supported, durable path for the accelerator.

## 6. Demo storyline this enables

1. **Collect** — bronze notebooks show raw platform telemetry landing in OneLake.
2. **Land in Fabric** — lakehouse Files/Tables, one place for all AI platforms.
3. **Conform** — silver/gold build the unified FinOps star (identity/app/BU/cost).
4. **Persona dashboards** — the 10-page report (CFO → Governance → Engineering →
   App Owner → License Optimization → **Extractable Data Spectrum**).
5. **Optimize** — idle licenses, expensive-model usage, budget overruns, chargeback.

Real vs mock provenance never blurs — `dim_platform.data_source`,
`fact.cost_is_estimated`, and the catalog's `availability` column keep REAL,
AVAILABLE, MOCK, and ROADMAP signals clearly labeled.
