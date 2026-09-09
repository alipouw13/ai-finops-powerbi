# REAL M365 collector — Microsoft Graph

Two scripts turn the M365 slice from MOCK into a live connector, without ever
relabelling modelled dollars as billed ones.

| Script | Does |
|---|---|
| `extract_m365_graph.py` | pulls REAL SKU entitlement + per-user licences + Copilot activity from Microsoft Graph |
| `price_seats.py` | multiplies those REAL seat counts by PUBLISHED list price from `dim_rate_card` |

```bash
az login --tenant <tenant-id>

# 1. see what Graph exposes for this tenant — writes nothing
python platform/fabric/extract_m365_graph.py --probe

# 2. price the seats it found (or a hypothetical count)
python platform/fabric/price_seats.py --seats 250
python platform/fabric/price_seats.py --from-extract platform/fabric/bronze_real
```

## The one thing to be honest about

**Microsoft Graph never returns invoiced dollars.** There is no endpoint that
tells you what Microsoft billed you for M365 Copilot. What you get is:

| Signal | Source | Status |
|---|---|---|
| Seat **counts** (purchased + assigned) | `subscribedSkus`, `users?$select=assignedLicenses` | **REAL** |
| Seat **activity** (per-app last-activity dates) | `getMicrosoft365CopilotUsageUserDetail` | **REAL** (beta, needs `Reports.Read.All`) |
| Seat **dollars** | `dim_rate_card` list price × seat count | **MODELLED** — `cost_is_estimated=TRUE` |

That is why the M365 rows in the model always carry `cost_is_estimated=TRUE`.
Load the customer's EA/MCA price sheet into `dim_rate_card` and the same code
produces contract-accurate figures — the collector itself does not change.

## Proving the connector on a tenant with no Copilot licences

A tenant with **zero** Copilot seats still proves the whole path:

- `subscribedSkus` returns the real SKU inventory
- `users?$select=assignedLicenses` returns the real per-user licence graph
- the Copilot usage report legitimately returns nothing

That is a *truthful empty*, not a fabricated number. Pair it with
`price_seats.py --seats N` to demonstrate the dollar model at a published rate,
clearly labelled `HYPOTHETICAL`.

## Permissions

| Call | Delegated permission | Fails with |
|---|---|---|
| `/organization`, `/subscribedSkus` | `Organization.Read.All` | 403 |
| `/users?$select=assignedLicenses` | `User.Read.All` | 403 |
| `/beta/.../getMicrosoft365CopilotUsageUserDetail` | `Reports.Read.All` | 403 |

Seat entitlement and seat activity have **different permission floors**. A
non-admin often gets the first two and not the third — the collector reports
exactly which one failed rather than silently emitting zeros.

> The usage report is **Graph beta only**. The v1.0 path returns
> `400 Resource not found for the segment`.

## Output safety

REAL extracts are written to **`platform/fabric/bronze_real/`**, which is
gitignored — deliberately separate from the committed MOCK `bronze_out/`.
Writing also requires an explicit flag:

```bash
python platform/fabric/extract_m365_graph.py --i-understand-this-is-real-tenant-data
```

Without it the script probes and refuses to write, so a real tenant's licence
counts cannot be committed into the demo repo by accident.
