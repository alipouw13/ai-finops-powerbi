# AI FinOps Accelerator — Clone & Run Runbook

Everything here runs **Fabric-free** with the Python standard library only (no pip
installs, no Power BI license, no cloud). All data is **MOCK** and tagged as such.
A coworker can clone this repo and see the full story — per-persona BI dashboards +
the AI insight layer — in under a minute.

```bash
git clone https://github.com/natesanshreyas/ai-finops-powerbi.git
cd ai-finops-powerbi

# 1. Build the portable data store (Bronze + Gold + extractable-data catalog)
python3 platform/data-store/build_store.py        # -> platform/data-store/finops.db

# 2. Launch the persona dashboards + AI layer
python3 platform/localhost/app.py                 # -> http://localhost:8080
```

Open <http://localhost:8080> and click through the tabs.

## Deployed Fabric pipeline

The `AI-tokenomics` workspace also has a deployed flow, separate from this
Fabric-free demo. Its `Pipeline_1` runs these existing notebooks sequentially,
only advancing after success:

1. `00_load_bronze_csv` -> `LH_tokenomics_bronze` (MOCK source files).
2. `01_load_bronze_real_csv` -> `LH_tokenomics_bronze_real` (REAL source files).
3. `10_silver_conform` -> `LH_tokenomics_silver` (conformed real + mock rows).
4. `20_gold_star` -> `LH_tokenomics_gold` (report model and validation).

The workspace uses the `fabrictokenomicswus3` F8 capacity. Resume that capacity
before running the pipeline; leaving it active incurs capacity charges.

### Live source snapshots

The additive cells in [live_evidence_cells.py](platform/fabric/live_evidence_cells.py)
are deployed after the original Bronze-real, Silver, and Gold notebook cells.
They preserve the existing transformations and lakehouse bindings.

- Stage the verified baseline JSON under real Bronze
  `Files/live_source/baseline.json` and the complete Cost Management response
  (`url`, `status`, `data`) under `Files/live_source/cost_query.json`.
- Stage real seat assignments using the existing
  `Files/bronze_real/bronze_m365_copilot_seats.csv` contract. Do not invent
  assignment dates or negotiated seat prices.
- Bronze retains raw JSON evidence. Silver produces
  `silver_live_license_summary`, `silver_live_cost_query`, and
  `silver_live_source_status`. Gold_Validate reads them from Silver (they are not copied into Gold),
  verifies row counts and the real seat identities in `fact_ai_usage`,
  and writes `Files/live_source/validation.json` in the Gold lakehouse.
- Cost-query evidence is separate from existing spend facts to avoid counting
  the same Azure charges twice. Currency is retained. Fabric Copilot capacity
  charges must not be classified as M365 Cowork credits.
- Live seat counts can enter the existing modeled seat-cost calculation, but
  those dollars remain **estimated**, not invoiced. Blocked usage reports and
  unobserved M365 credit meters remain explicit source statuses, not zero usage.

**This pipeline processes staged snapshots; it does not refresh Graph or Azure
APIs.** Restage source extracts before a later measurement. A successful run
alone does not establish source freshness. Keep the snapshot date visible, and
check the final validation file rather than treating job acceptance as success.
The live evidence tables stay in Silver and are not part of the Power BI semantic model.

### Verified run — October 23, 2026

Run `4701cc36-a561-42f7-b806-a84d5bbc47b3` completed successfully at
18:58:56 UTC. The persisted Gold validation reported:

| Output | Rows |
|---|---:|
| `fact_ai_usage` (existing real + mock model) | 81,560 |
| Today's verified Copilot seat identities in the Gold fact | 2 |
| `gold_live_license_summary` | 1 |
| `gold_live_cost_query` | 1,910 |
| `gold_live_source_status` | 3 |

The snapshot contains 5 enabled and 2 assigned Copilot seats. Usage reporting
remains blocked by authorization, and no M365/Cowork meter was observed in the
selected billing scope. Neither status was replaced with fabricated usage.

### User-level cost allocation

Stage `User_Allocation` ([source](platform/medallion/allocation/33_user_allocation.py)) builds
`fact_user_cost_allocation`: the same dollars, tokens and requests as `fact_ai_usage`, re-keyed to a
person (`user_identity_key`) with the original caller kept as `source_identity_key`. Every row carries an
`allocation_basis`:

| Basis | Rule |
|---|---|
| `DIRECT` | The calling identity is already a human (Copilot seats, GitHub, Studio makers). |
| `OWNER` | The caller is a service principal or app; cost goes to the application's accountable owner (`dim_application.owner_upn` matched to `dim_identity.upn`). |
| `UNALLOCATED` | No owner resolves; the row stays on the `unknown` identity member. |

`OWNER` is accountability, not proof of who made the calls. Foundry token usage today arrives under
service principals, so for example the Mobile Shopping Assistant tokens are allocated to the app owner.
Direct per-person attribution needs the caller (Entra user object id) logged on the APIM or Foundry
request. The notebook asserts that cost, tokens and requests tie to `fact_ai_usage` and that no key is
orphaned, and writes `Files/live_source/user_allocation_validation.json`. Do not add the measures to
Total AI Cost. `rel_alloc_source` (source identity) is inactive; use `USERELATIONSHIP` to slice by caller.

Databricks was removed from the model. Genie and other third-party agents are inventoried through
Agent 365 (registry only; Agent 365 exposes no cost per agent).
### Layering rule (Silver cleans, Gold is a star)

Gold contains only `dim_*`, `fact_*` and `bridge_*` tables (plus `extractable_data_catalog`,
which backs `dim_data_source` and has not been renamed). Everything diagnostic or
evidence-only stays in Silver: `silver_audit_*` and `silver_live_*`. The Gold_Validate stage
reads `silver_live_*` from Silver and no longer copies it into `gold_live_*`.

### Agent 365 inventory (REAL export, 2026-10-26)

Stage `Agent365_Inventory`
([source](platform/medallion/agent365/31_agent365_inventory.py)) lands the two admin-center
exports as `Files/bronze/bronze_agent365_agents.csv` and `bronze_agent365_active_users.csv`
(tenant data, not committed). Silver: `silver_agent_inventory` (`owner_class` separates
Owner assigned / Microsoft-managed / Publisher-managed / Unowned tenant agent) and
`silver_agent_activity`. Gold: `dim_agent`, `fact_agent_inventory` (snapshot x agent, additive
0/1 flags; owner joins to `dim_identity` by UPN) and `fact_agent_activity` (joins to the same
`dim_identity` keys as the FOCUS-derived facts).

- The export has **no sponsor, spending policy, project or cost per agent**. Cost still joins
  through the owner identity or tags.
- The active-users export masks the UPN with a salted hash by default, which cannot be
  reversed. Rows stay on the `unknown` member with `is_identity_resolved = 0` until the
  tenant shows real user names in reports (M365 admin center > Settings > Org settings >
  Reports > clear "Display concealed user, group, and site names"). The same notebook then
  resolves them with no change.
- The Graph catalog API (`CopilotPackages.Read.All`) still returned 403 after licences were
  assigned, so the exports are the source until that clears.

### Personas (ASSUMED mapping)

Stage `Persona_Model` ([source](platform/medallion/persona/32_persona_model.py)) adds
`dim_persona` (the five stakeholder personas plus `Workload (non-human)` and `Unassigned`),
`bridge_persona_platform` (Primary / Secondary tool per the stakeholder map) and stamps
`persona_key` on `dim_identity`, so every identity-keyed fact slices by persona. It also adds
`CopilotCowork` and `AzureML` to `dim_platform` as visible, not-yet-ingested tools. The
team-to-persona mapping (`silver_persona_map`) is **ASSUMED** until an HR or Entra job-profile
feed is available; `fact_user_cost_allocation` slices by persona through the allocated user.

---

## What you get

### 5 persona BI dashboards (KPIs + charts)
| Tab | Answers |
|-----|---------|
| **CFO / Finance** | Total spend, spend by business unit, budget variance, forecast, fixed vs variable |
| **Governance** | Spend by platform (REAL vs MOCK honesty), model mix, human vs non-human identities, unallocated $ |
| **Engineering** | Tokens, requests, errors, latency, per-model performance |
| **App Owner** | Cost by application, app × model spend, trends |
| **License Optimization** | Idle licensed seats + reclaimable $ |

### AI insight layer (🤖 Ask AI tab)
Natural-language question → intent routing → parameterized SQL over the Gold model →
grounded answer + **the SQL it ran** (auditable) + evidence rows. This mocks Fabric
Copilot / semantic-model Q&A but runs 100% locally. Try:
- "Which business unit spent the most last month?"
- "Which licenses should be reclaimed?"
- "What are the highest cost models?"
- "Where can we reduce AI spend?"

---

## The 5-step pipeline (ends at the Fabric push)

| Step | Layer | Artifact |
|------|-------|----------|
| 1. Extract | Source telemetry | `docs/extractable-data-by-product.md`, `extractable_data_catalog` |
| 2. Bronze  | Raw per-platform tables | `platform/fabric/gen_bronze_data.py` → `bronze_out/*.csv` |
| 3. Store   | Portable data source | `platform/data-store/build_store.py` → `finops.db` |
| 4. Gold + BI + AI | Semantic model + dashboards + Q&A | `AIFinOps.SemanticModel/`, `platform/localhost/app.py` |
| 5. **Fabric push** | Land Bronze in a Lakehouse | `platform/fabric/load_bronze.py` (run once a Power BI license is assigned) |

Steps 1–4 run today with no license. Step 5 is the handoff to whoever owns the
Fabric workspace — see `docs/bronze-layer-architecture.md`.

## Also runnable
- **Power BI Desktop:** open `AIFinOps.pbip` (10 persona report pages, no Fabric needed).
- **Ad-hoc SQL:** `python3 platform/data-store/build_store.py --query "SELECT product, COUNT(*) FROM extractable_data_catalog GROUP BY product"`

> All figures are **MOCK** demo data. Nothing here is relabeled as real customer spend.
