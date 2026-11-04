#!/usr/bin/env python3
"""Wrap multi-line TMDL measure expressions in triple backticks.

TMDL cannot tell where a multi-line DAX expression ends if its continuation
lines sit at the same indent as the measure's properties:

    measure 'Total Tokens' =
        CALCULATE(SUM(fact_ai_usage[quantity]), fact_ai_usage[unit_type] = "token")
        formatString: #,0        <-- swallowed into the DAX

Power BI Desktop tolerates this; the Fabric TMDL parser does not. It silently
folds `formatString` into the expression, and the measure then fails at query
time with "Failed to resolve name 'SYNTAXERROR'". The measure still *exists*, so
nothing complains until a visual using it comes back blank.

The documented form is to delimit multi-line DAX:

    measure 'Total Tokens' = ```
            CALCULATE(SUM(fact_ai_usage[quantity]), fact_ai_usage[unit_type] = "token")
            ```
        formatString: #,0

Usage:
    python platform/validate/fix_tmdl_measures.py [--check] [<model dir> ...]

--check reports without writing (exit 1 if any measure needs wrapping).
Stdlib only.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# TMDL property keywords that may follow a measure expression. Anything else at
# expression indent is part of the DAX.
PROPS = ("formatString", "displayFolder", "isHidden", "lineageTag", "annotation",
         "description", "changedProperty", "formatStringDefinition",
         "detailRowsDefinition", "dataType", "dataCategory", "kpi",
         "isSimpleMeasure", "sourceColumn", "summarizeBy", "isKey", "sortByColumn")

MEASURE_RE = re.compile(r"^(\t+)measure\s+(.+?)\s*=\s*(.*)$")


def fix_text(text: str) -> tuple[str, list[str]]:
    lines = text.splitlines()
    out: list[str] = []
    fixed: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        m = MEASURE_RE.match(line)
        if not m or m.group(3).strip():
            # Not a measure, or a single-line measure (expression after '=').
            out.append(line)
            i += 1
            continue

        indent, name = m.group(1), m.group(2)
        # Collect the expression: subsequent lines indented deeper than the
        # measure that are not TMDL properties.
        body: list[str] = []
        j = i + 1
        while j < len(lines):
            nxt = lines[j]
            if not nxt.strip():
                break
            stripped = nxt.lstrip("\t ")
            depth = len(nxt) - len(nxt.lstrip("\t"))
            if depth <= len(indent):
                break
            if any(stripped.startswith(p) for p in PROPS):
                break
            body.append(nxt)
            j += 1

        if not body:
            out.append(line)
            i += 1
            continue

        expr_indent = indent + "\t\t"
        out.append(f"{indent}measure {name} = ```")
        for b in body:
            out.append(expr_indent + b.lstrip("\t"))
        out.append(expr_indent + "```")
        fixed.append(name)
        i = j
    return "\n".join(out) + "\n", fixed


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("dirs", nargs="*", default=None,
                    help="semantic model dirs (default: all *.SemanticModel in repo)")
    ap.add_argument("--check", action="store_true", help="report only, do not write")
    args = ap.parse_args()

    dirs = [Path(d) for d in args.dirs] if args.dirs else sorted(
        p for p in ROOT.glob("*.SemanticModel") if p.is_dir())

    total = 0
    for d in dirs:
        if not d.is_absolute():
            d = ROOT / d
        tables = d / "definition" / "tables"
        if not tables.is_dir():
            continue
        for tmdl in sorted(tables.glob("*.tmdl")):
            original = tmdl.read_text(encoding="utf-8")
            if "= ```" in original:
                continue  # already delimited
            new, fixed = fix_text(original)
            if not fixed:
                continue
            total += len(fixed)
            rel = tmdl.relative_to(ROOT)
            print(f"  {rel}: {len(fixed)} multi-line measure(s)")
            for n in fixed:
                print(f"      {n}")
            if not args.check:
                tmdl.write_text(new, encoding="utf-8")

    if args.check:
        if total:
            print(f"\n{total} measure(s) need triple-backtick delimiters "
                  f"(they will silently fail in Fabric). Run without --check to fix.")
            return 1
        print("all multi-line measures are properly delimited")
        return 0
    print(f"\n{total} measure(s) wrapped." if total else "nothing to fix.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
