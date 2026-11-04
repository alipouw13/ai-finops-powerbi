#!/usr/bin/env python3
"""Price REAL seat counts with PUBLISHED list prices.

Bridges the honest gap in the M365 slice: Microsoft Graph gives you real seat
*entitlement* but never invoiced *dollars*. This turns one into the other and
labels the result correctly.

    seats (REAL, from Graph)  x  list price (PUBLISHED, from dim_rate_card)
        = cost  (MODELLED, cost_is_estimated=TRUE)

Two modes:

  --from-extract <dir>   price a real Graph extract produced by
                         extract_m365_graph.py (bronze_m365_license_inventory.csv)

  --seats N --sku X      price a hypothetical seat count, for a tenant that has
                         no Copilot licences yet but wants the $ model proven

Everything it emits carries cost_is_estimated=TRUE and source='LIST PRICE'.
Load the customer's EA/MCA price sheet into dim_rate_card and the same code
produces contract-accurate numbers with no changes.

USAGE
  python platform/fabric/price_seats.py --seats 250 --sku Microsoft_365_Copilot
  python platform/fabric/price_seats.py --from-extract platform/fabric/bronze_real
"""

from __future__ import annotations

import argparse
import csv
import os
import sys

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, "..", ".."))
RATE_CARD = os.path.join(ROOT, "AIFinOps.SemanticModel", "data", "dim_rate_card.csv")

# Which rate-card platform prices a given SKU family. Tenants name SKUs
# inconsistently, so match on substrings.
SKU_TO_PLATFORM = [
    ("MICROSOFT_365_COPILOT", "M365Copilot"),
    ("M365_COPILOT", "M365Copilot"),
    ("COPILOT_FOR_SALES", "M365Copilot"),
    ("COPILOT_FOR_SERVICE", "M365Copilot"),
    ("POWER_VIRTUAL_AGENT", "CopilotStudio"),
    ("GITHUB", "GitHubCopilot"),
]


def platform_for_sku(sku_part_number: str):
    up = (sku_part_number or "").upper()
    for marker, platform in SKU_TO_PLATFORM:
        if marker in up:
            return platform
    return None


def load_rate_card(path=RATE_CARD):
    if not os.path.exists(path):
        sys.exit(f"rate card not found: {path}")
    with open(path, newline="", encoding="utf-8-sig") as fh:
        return list(csv.DictReader(fh))


def seat_day_rate(rates, platform):
    for r in rates:
        if r["platform"] == platform and r["unit_type"] == "seat_day":
            return float(r["unit_price_usd"]), r.get("source", ""), r.get("note", "")
    return None, "", ""


def report(rows, rates):
    """rows: list of (sku_part_number, seat_count, basis_label)"""
    print("%-40s %10s %12s %14s %14s" %
          ("SKU", "SEATS", "$/seat/mo", "MONTHLY $", "ANNUAL $"))
    print("-" * 94)
    total_m = 0.0
    priced = 0
    for sku, seats, basis in rows:
        platform = platform_for_sku(sku)
        if not platform:
            print("%-40s %10s %12s %14s %14s" % (sku[:40], f"{seats:,}", "-", "no rate", "-"))
            continue
        rate, source, note = seat_day_rate(rates, platform)
        if rate is None:
            print("%-40s %10s %12s %14s %14s" % (sku[:40], f"{seats:,}", "-", "no rate", "-"))
            continue
        monthly_rate = rate * 30
        monthly = seats * monthly_rate
        total_m += monthly
        priced += 1
        print("%-40s %10s %12s %14s %14s" %
              (sku[:40], f"{seats:,}", f"${monthly_rate:,.2f}",
               f"${monthly:,.2f}", f"${monthly * 12:,.2f}"))
    print("-" * 94)
    print("%-40s %10s %12s %14s %14s" %
          (f"TOTAL ({priced} priced SKU(s))", "", "", f"${total_m:,.2f}", f"${total_m * 12:,.2f}"))
    return total_m


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--from-extract", metavar="DIR",
                     help="directory holding bronze_m365_license_inventory.csv")
    src.add_argument("--seats", type=int, metavar="N",
                     help="hypothetical seat count to price")
    ap.add_argument("--sku", default="Microsoft_365_Copilot",
                    help="SKU part number for --seats (default: Microsoft_365_Copilot)")
    ap.add_argument("--basis", choices=["consumed", "enabled"], default="consumed",
                    help="price assigned seats (consumed, default) or purchased (enabled)")
    args = ap.parse_args()

    rates = load_rate_card()

    if args.seats is not None:
        print("Basis: HYPOTHETICAL seat count (no tenant data)")
        print()
        rows = [(args.sku, args.seats, "hypothetical")]
    else:
        path = os.path.join(args.from_extract, "bronze_m365_license_inventory.csv")
        if not os.path.exists(path):
            sys.exit(f"not found: {path}\nRun extract_m365_graph.py first.")
        with open(path, newline="", encoding="utf-8-sig") as fh:
            inv = list(csv.DictReader(fh))
        ai = [r for r in inv if r.get("is_ai_sku") == "TRUE"]
        col = "consumed_units" if args.basis == "consumed" else "prepaid_enabled"
        print("Basis: REAL seat counts from Graph (%s), tenant %s"
              % (col, inv[0].get("_source_system", "?") if inv else "?"))
        print()
        rows = [(r["sku_part_number"], int(r[col] or 0), col) for r in ai]
        rows = [r for r in rows if r[1] > 0] or rows

    total = report(rows, rates)

    print()
    if args.seats is not None:
        print("Seat count: HYPOTHETICAL (supplied on the command line, not from a tenant).")
    else:
        print("Seat counts: REAL (Microsoft Graph entitlement).")
    print("Unit prices: PUBLISHED LIST from dim_rate_card — cost_is_estimated=TRUE.")
    print("Graph exposes no invoiced amount, so these dollars are modelled, not billed.")
    print("Load the customer's EA/MCA price sheet into dim_rate_card for contract rates.")
    return 0 if total >= 0 else 1


if __name__ == "__main__":
    sys.exit(main())
