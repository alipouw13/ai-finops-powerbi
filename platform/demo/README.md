# Runnable medallion demo database

A **real, working SQLite database** that shows the *same reality* represented three
ways - Bronze → Silver → Gold - so you can show coworkers the concrete difference
between the layers. No Fabric, no Power BI, no dependencies needed.

## Run it

```bash
python3 platform/demo/build_demo_db.py
```

It builds `finops_demo.db` and prints every table (Bronze, Silver, Gold) plus the two
payoff queries (Total AI Cost by BU, Idle Licensed Users). Then poke around yourself:

```bash
sqlite3 platform/demo/finops_demo.db '.tables'
sqlite3 platform/demo/finops_demo.db 'SELECT * FROM fact_ai_usage'
```

## What it demonstrates

- **Bronze** - raw, one shape per source. GitHub is keyed by `github_login`, M365 by
  `UPN` (no common key); usage feeds carry **no cost**; the cost feed carries **no user**;
  idle seats show up as `NULL` last-activity. *Not BI-ready.*
- **Silver** - conformed. `pnair` (GitHub) + `priya.nair@contoso.com` (M365) collapse
  into **one `identity_key`**; humans/agents/service-principals classified; all four
  platforms land on **one daily grain with cost attached**.
- **Gold** - the star the Power BI model binds to: `fact_ai_usage` + conformed
  dimensions. From here the report computes Total AI Cost by BU and flags idle,
  reclaimable seats.

The columns and transformations mirror the production design in
`docs/medallion-tables.md` and `docs/medallion-examples.md`. Data is illustrative MOCK.
