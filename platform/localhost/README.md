# AI FinOps — Local Demo (no Fabric / no Power BI license)

A self-contained mock-up of the **deployed** Fabric + Power BI end result, runnable
entirely on `localhost`. Zero external dependencies (Python stdlib only). All data is
**MOCK**. The app reads from the portable data store `platform/data-store/finops.db`
(Bronze + Gold + catalog) when present, and falls back to `AIFinOps.SemanticModel/data/*.csv`
(the Gold star schema) otherwise — so the BI dashboards and the AI layer read from the
**same source** the Fabric push (`load_bronze.py`) uses.

## Run

```bash
# optional but recommended: (re)build the portable data store first
python3 platform/data-store/build_store.py

python3 platform/localhost/app.py
# then open http://localhost:8080
```

Set a different port with `PORT=9000 python3 platform/localhost/app.py`.

## What it shows

Five **persona dashboards** + a working **AI insight layer**:

| Tab | Answers |
|-----|---------|
| CFO / Finance | Total spend, spend by BU, budget variance, forecast, fixed vs variable |
| Governance | Spend by platform (with REAL/MOCK source honesty), model mix, human vs non-human identities, unallocated $ |
| Engineering | Tokens, requests, errors, latency, per-model performance |
| App Owner | Cost by application, app × model spend |
| License Optimization | Idle licensed seats + reclaimable $ |
| 🤖 Ask AI | Natural-language question → SQL → grounded answer over the Gold table |

## The AI layer

The **Ask AI** tab mocks Fabric Copilot / semantic-model Q&A — but runs 100% locally.
Type a question (or click a suggestion). It routes intent → parameterized SQL over
`fact_ai_usage` + dims, and returns the **answer**, the **SQL it ran** (auditable), and
the **evidence rows**. Handles: business-unit ranking, idle licenses, cost by model /
application / platform, fixed-vs-variable, and "where can we reduce spend".

This is the demo to run when Fabric licensing is blocked: it makes the AI story tangible
without a capacity. It keeps these five original persona tabs; the Power BI report has
since consolidated to 8 pages (Engineering folds into *Engineering Tokenomics*, License
Optimization into *Licence Seats, Waste & Utilisation*), but both are backed by the same
Gold model and measures.
