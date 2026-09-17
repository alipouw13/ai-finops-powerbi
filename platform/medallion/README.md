# Medallion pipeline — bronze → silver → gold

Reference implementation for landing real AI cost/usage extracts in Microsoft Fabric
and materializing the exact 10-table star the semantic model reads. These are Fabric
notebook scripts (PySpark); they are **not** wired into the PBIP, so they cannot
affect whether `AIFinOps.pbip` opens.

Every statement here has a **runnable twin** in
[`platform/data-store/build_store.py`](../data-store/build_store.py), which executes
the identical table names, column names, and logic in SQLite. That means the whole
pipeline can be reviewed, run, and verified with no Fabric capacity and no licence —
and the Spark version is the same code, not a re-interpretation.

```
RAW EXTRACT                          BRONZE (Delta)                 SILVER (conformed)            GOLD (star)
───────────                          ──────────────                 ──────────────────            ───────────
Cost Management FOCUS 1.0r2 ───────► focus_cost ──────────────┐
  (96 cols: the only $ authority)                             │
APIM AI gateway → Log Analytics ───► apim_gateway_requests ───┤
  (Entra JWT claims → DCR)           apim_client_ownership    ├─► cost_charge ─┐
Dataverse msdyn_aievent ───────────► dataverse_msdyn_aievent ─┤   gateway_request ├─► foundry_allocation ─┐
  (Copilot Studio credits)                                    │   studio_event  ─┘   studio_allocation  ─┤
M365 Graph + credit billing ───────► m365_copilot_usage ──────┤                                          ├─► usage_conformed ─► fact_ai_usage
  (seats, activity dates, credits)   m365_copilot_seats       │                                          │                      + dim_*
                                     m365_copilot_credits     │                                          │
GitHub REST ───────────────────────► ghc_seats ───────────────┤                                          │
                                     ghc_premium_usage        │                                          │
Customer master data ──────────────► ref_* (5 tables) ────────┘──────────────────────────────────────────┘
```

| Notebook | What it does |
|---|---|
| `bronze/01_ingest_focus_cost.py` | Lands the FOCUS 1.0r2 export — the dollars for Azure OpenAI, the Copilot Studio PAYG meter, and Fabric capacity, in **one** contract |
| `bronze/02_ingest_apim_gateway.py` | Lands the gateway requests and client registry — the only per-identity path for Foundry |
| `bronze/03_ingest_saas_platforms.py` | Lands Dataverse, M365 Graph/billing, GitHub, and the customer reference inputs |
| `silver/10_conform_usage.py` | Types, conforms, resolves identity, **allocates billed cost onto identities**, and asserts the allocation ties to the invoice |
| `gold/20_build_star.py` | Emits `fact_ai_usage` + 9 dimensions under a fixed column contract, asserting no orphan keys |

## The rule that shapes the whole design

**The provider's bill is the only source of dollars.**

* FOCUS charge lines carry authoritative cost, but **no identity**.
* APIM requests and Dataverse events carry identity, but **no cost**.
* Silver splits the billed amount by each identity's share of the billed unit —
  tokens per direction for Foundry, credits for Copilot Studio — so the star
  **reconciles to the invoice** instead of re-deriving spend from a rate card.

Where a provider bills no dollars at all (M365 and GitHub seats) the rate card is used
and the row is flagged `cost_is_estimated = TRUE`. Modelled dollars are never
relabelled as billed. The silver notebook asserts the reconciliation and fails rather
than publish a dashboard that does not tie out.

| Layer | Rule | Why |
|---|---|---|
| **Bronze** | Raw, append-only, source columns preserved with the provider's own names and casing | Historical system of record. A Cost Management export *replaces* the month-to-date file, so append protects prior days; Azure Monitor retention is 93 days, so Bronze is the durable copy. |
| **Silver** | Conform to one daily grain; USD is the only common measure; `unit_type` stays a dimension | Four platforms, four incompatible billing units, and only Foundry exposes tokens. USD is the sole thing that reconciles. Identity, application, and cost allocation live here. |
| **Gold** | Emit the star under a fixed **column contract** identical to the CSV headers | The semantic model's TMDL partition casts are keyed to those exact columns, so gold asserts the contract and the dimension joins — schema drift fails loudly, not silently. |

## What is deliberately NOT collected

* **Azure Monitor token metrics** — no identity dimension, so they would only duplicate
  the gateway's token counts without the column that makes attribution possible.
* **A cost collector per service** — Azure OpenAI, Copilot Studio PAYG, Fabric capacity
  and Azure ML all arrive on the one FOCUS export, separated by `ServiceName`.
* **Rate-card pricing for anything the provider already bills** — GitHub `netAmount`
  and M365 `costUsd` are used directly.

## Provenance never blurs

Real vs mock is `dim_platform.data_source` + `fact.cost_is_estimated` + Bronze's
`_data_class`, all **columns**, not code branches. To take a mock platform live you
swap one bronze reader; silver, gold, and the model are unchanged, and the dashboards
immediately show REAL.

## Going to DirectLake

The PoC reads CSVs via the `DataFolder` parameter so it opens with zero Fabric
dependency. In production, either (a) export gold to the same CSV layout, or
(b) repoint the semantic model to the gold Lakehouse and convert import partitions to
**DirectLake** for no-refresh, near-real-time cost.

| Option | Effort | Tradeoff |
|---|---|---|
| CSV export from gold | S | Zero model change; still an import refresh |
| DirectLake on gold | M | Live data, no refresh; requires Fabric capacity + partition rewrite |
