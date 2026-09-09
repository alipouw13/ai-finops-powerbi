#!/usr/bin/env python3
"""Offline validator for the AIFinOps PBIP project.

Checks the semantic model (TMDL) and the report (report.json) against the CSV
data files without needing Power BI Desktop, Fabric, or any license. Catches the
failures that otherwise only show up as a refresh error or a blank visual:

  1. model.tmdl 'ref table' entries that have no .tmdl file
  2. TMDL sourceColumn names that do not exist in the backing CSV header
  3. CSV values that will not coerce to the declared TMDL data type
  4. Relationship columns that do not exist, to-side keys that are not unique,
     and from-side keys that are orphaned (silent blank-row joins)
  5. DAX measures referencing columns/measures that do not exist
  6. report.json visual bindings referencing fields that do not exist
  7. Non-portable artifacts (hard-coded absolute paths in the DataFolder param)

Usage:
    python platform/validate/validate_pbip.py [--root <repo root>]
    python platform/validate/validate_pbip.py --fix-data-folder

`--fix-data-folder` rewrites the DataFolder parameter in model.tmdl to this
clone's data directory. PBIP has no relative-path primitive, so the committed
default is a placeholder and each clone must point it at itself once.

Exit code 0 = no errors, 1 = at least one error. Stdlib only.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from pathlib import Path

ERRORS: list[str] = []
WARNINGS: list[str] = []
PASSES: list[str] = []


def err(msg: str) -> None:
    ERRORS.append(msg)


def warn(msg: str) -> None:
    WARNINGS.append(msg)


def ok(msg: str) -> None:
    PASSES.append(msg)


# --------------------------------------------------------------------------- TMDL


class Table:
    def __init__(self, name: str) -> None:
        self.name = name
        self.columns: dict[str, str] = {}
        self.source_columns: dict[str, str] = {}
        self.measures: dict[str, str] = {}
        self.csv_file: str | None = None


NAME_RE = r"(?:'([^']+)'|([^\s=]+))"
PROP_RE = r"^(formatString|displayFolder|lineageTag|isHidden|annotation|changedProperty|dataType|summarizeBy|sortByColumn|sourceColumn|isKey|dataCategory|///)"


def _unquote(m: re.Match, g1: int, g2: int) -> str:
    return m.group(g1) or m.group(g2)


def parse_tmdl_table(path: Path) -> Table:
    lines = path.read_text(encoding="utf-8").splitlines()
    m = re.match(r"table\s+" + NAME_RE, lines[0].strip())
    table = Table(_unquote(m, 1, 2) if m else path.stem)

    current_col: str | None = None
    current_measure: str | None = None
    measure_buf: list[str] = []

    def flush_measure() -> None:
        nonlocal current_measure, measure_buf
        if current_measure is not None:
            table.measures[current_measure] = "\n".join(measure_buf)
        current_measure, measure_buf = None, []

    for raw in lines[1:]:
        stripped = raw.strip()

        col_m = re.match(r"column\s+" + NAME_RE, stripped)
        meas_m = re.match(r"measure\s+" + NAME_RE + r"\s*=\s*(.*)$", stripped)

        if col_m:
            flush_measure()
            current_col = _unquote(col_m, 1, 2)
            table.columns[current_col] = ""
            table.source_columns[current_col] = current_col
            continue
        if meas_m:
            flush_measure()
            current_measure = _unquote(meas_m, 1, 2)
            current_col = None
            tail = meas_m.group(3).strip()
            measure_buf = [tail] if tail else []
            continue
        if stripped.startswith("partition "):
            flush_measure()
            current_col = None
            continue

        if current_measure is not None:
            if not stripped:
                continue
            if re.match(PROP_RE, stripped):
                flush_measure()
            else:
                measure_buf.append(stripped)
                continue

        if current_col:
            dt = re.match(r"dataType:\s*(\S+)", stripped)
            if dt:
                table.columns[current_col] = dt.group(1)
            sc = re.match(r"sourceColumn:\s*(.+)$", stripped)
            if sc:
                table.source_columns[current_col] = sc.group(1).strip()

        csvm = re.search(r'DataFolder\s*&\s*"([^"]+\.csv)"', stripped)
        if csvm:
            table.csv_file = csvm.group(1)

    flush_measure()
    return table


def parse_model(model_tmdl: Path):
    text = model_tmdl.read_text(encoding="utf-8")
    rels = [
        m.groups()
        for m in re.finditer(
            r"relationship\s+(\S+)\s*\n\s*fromColumn:\s*(\S+)\.(\S+)\s*\n\s*toColumn:\s*(\S+)\.(\S+)", text
        )
    ]
    refs = [r.strip() for r in re.findall(r"^ref table\s+(.+)$", text, flags=re.M)]
    folder = re.search(r'expression DataFolder\s*=\s*"([^"]*)"', text)
    return rels, refs, folder.group(1) if folder else None


# --------------------------------------------------------------------------- CSV


def read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open(encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        rows = list(reader)
        return (reader.fieldnames or []), rows


BOOL_OK = {"true", "false", "1", "0", ""}
PLACEHOLDER_PREFIX = "C:\\path\\to\\"


def type_ok(value: str, dtype: str) -> bool:
    if value == "":
        return True
    if dtype == "int64":
        try:
            int(value)
            return True
        except ValueError:
            return False
    if dtype in ("double", "decimal"):
        try:
            float(value)
            return True
        except ValueError:
            return False
    if dtype == "boolean":
        return value.strip().lower() in BOOL_OK
    if dtype == "dateTime":
        return bool(re.match(r"^\d{4}-\d{2}-\d{2}", value.strip()))
    return True


# --------------------------------------------------------------------------- report.json


def _maybe_json(node: str):
    s = node.strip()
    if s.startswith("{") and s.endswith("}") and len(s) > 2:
        try:
            return json.loads(s)
        except (ValueError, RecursionError):
            return None
    return None


def walk_bindings(node, out: list[tuple[str, str, str]]) -> None:
    if isinstance(node, dict):
        for kind in ("Measure", "Column", "HierarchyLevel"):
            spec = node.get(kind)
            if isinstance(spec, dict) and "Property" in spec:
                src = spec.get("Expression", {}).get("SourceRef", {})
                entity = src.get("Entity") or src.get("Source")
                if entity:
                    out.append((kind, entity, spec["Property"]))
        for value in node.values():
            walk_bindings(value, out)
    elif isinstance(node, list):
        for value in node:
            walk_bindings(value, out)
    elif isinstance(node, str):
        parsed = _maybe_json(node)
        if parsed is not None:
            walk_bindings(parsed, out)


def resolve_aliases(report) -> dict[str, str]:
    """Map query alias (e.g. 'f') -> real table name from prototypeQuery From clauses."""
    aliases: dict[str, str] = {}

    def rec(node):
        if isinstance(node, dict):
            froms = node.get("From")
            if isinstance(froms, list):
                for f in froms:
                    if isinstance(f, dict) and "Name" in f and "Entity" in f:
                        aliases[f["Name"]] = f["Entity"]
            for v in node.values():
                rec(v)
        elif isinstance(node, list):
            for v in node:
                rec(v)
        elif isinstance(node, str):
            parsed = _maybe_json(node)
            if parsed is not None:
                rec(parsed)

    rec(report)
    return aliases


# --------------------------------------------------------------------------- main


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=None, help="repo root (default: two levels above this file)")
    ap.add_argument("--fix-data-folder", action="store_true",
                    help="rewrite the DataFolder parameter in model.tmdl to this clone")
    args = ap.parse_args()

    root = Path(args.root).resolve() if args.root else Path(__file__).resolve().parents[2]
    model_dir = root / "AIFinOps.SemanticModel"
    data_dir = model_dir / "data"
    tables_dir = model_dir / "definition" / "tables"
    model_tmdl = model_dir / "definition" / "model.tmdl"
    report_json = root / "AIFinOps.Report" / "report.json"

    for required in (model_tmdl, report_json, data_dir):
        if not required.exists():
            err(f"missing required path: {required}")
    if ERRORS:
        return report_results()

    rels, refs, data_folder = parse_model(model_tmdl)

    if args.fix_data_folder:
        want = str(data_dir) + "\\"
        text = model_tmdl.read_text(encoding="utf-8")
        # re.sub with a callable uses the return value literally, so backslashes
        # in the path must NOT be escaped. M string literals take backslashes raw.
        new_text = re.sub(
            r'(expression DataFolder\s*=\s*")[^"]*(")',
            lambda m: m.group(1) + want + m.group(2),
            text, count=1)
        if new_text == text:
            print(f"DataFolder already points at {want}")
        else:
            model_tmdl.write_text(new_text, encoding="utf-8")
            print(f"DataFolder -> {want}")
        data_folder = want

    tables = {t.name: t for t in (parse_tmdl_table(p) for p in sorted(tables_dir.glob("*.tmdl")))}
    ok(f"parsed {len(tables)} TMDL tables, {len(rels)} relationships, "
       f"{sum(len(t.measures) for t in tables.values())} measures")

    for r in refs:
        if r not in tables:
            err(f"model.tmdl declares 'ref table {r}' but no matching table definition exists")
    for name in tables:
        if name not in refs:
            warn(f"table '{name}' has a .tmdl file but is not listed as 'ref table' in model.tmdl")

    # ---- portability of the DataFolder parameter
    if data_folder is None:
        err("model.tmdl has no DataFolder expression; table partitions cannot resolve their CSVs")
    elif data_folder.rstrip("\\/").lower() == str(data_dir).rstrip("\\/").lower():
        ok("DataFolder resolves to this clone's data directory")
    elif data_folder.startswith(PLACEHOLDER_PREFIX):
        # Expected state on a fresh clone: PBIP cannot store a relative path, so
        # the committed default is a placeholder rather than someone's home dir.
        warn("DataFolder is still the committed placeholder. Before opening the PBIP run:"
             "\n            python platform/validate/validate_pbip.py --fix-data-folder")
    elif re.match(r"^[A-Za-z]:[\\/]", data_folder) or data_folder.startswith("\\\\"):
        err("model.tmdl DataFolder points at the machine-specific path "
            f"{data_folder!r}, which does not exist in this clone. Run "
            "`python platform/validate/validate_pbip.py --fix-data-folder`.")

    # ---- TMDL description blocks must immediately precede their object
    # A `///` block followed by a blank line is rejected by the Fabric TMDL
    # parser with "Unexpected line type: Empty!", even though Desktop tolerates
    # it. Likewise a decorative `//` comment directly above a `///` description
    # trips "Invalid indentation was detected". Both only surface on publish.
    for tmdl in sorted(tables_dir.glob("*.tmdl")) + [model_tmdl]:
        lines = tmdl.read_text(encoding="utf-8").splitlines()
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith("//") and not stripped.startswith("///"):
                nxt = lines[i + 1].strip() if i + 1 < len(lines) else ""
                if nxt.startswith("///"):
                    err(f"{tmdl.name}:{i + 1}: '//' comment directly above a '///' "
                        f"description; Fabric rejects this with 'Invalid indentation "
                        f"was detected'. Remove the divider comment.")
                continue
            if not stripped.startswith("///"):
                continue
            j = i + 1
            while j < len(lines) and lines[j].strip().startswith("///"):
                j += 1
            if j < len(lines) and not lines[j].strip():
                err(f"{tmdl.name}:{i + 1}: '///' description block is followed by a "
                    f"blank line; TMDL requires it to immediately precede the object "
                    f"it describes (Fabric rejects this with 'Unexpected line type: Empty')")

    # ---- CSV header + type checks
    csv_cache: dict[str, tuple[list[str], list[dict[str, str]]]] = {}
    for name, t in sorted(tables.items()):
        if not t.csv_file:
            err(f"table '{name}' has no CSV partition source")
            continue
        path = data_dir / t.csv_file
        if not path.exists():
            err(f"table '{name}' reads {t.csv_file} but {path} does not exist")
            continue
        header, rows = read_csv(path)
        csv_cache[name] = (header, rows)
        missing = [sc for sc in t.source_columns.values() if sc not in header]
        if missing:
            err(f"table '{name}': sourceColumn(s) {missing} not present in {t.csv_file} header {header}")
        extra = [h for h in header if h not in set(t.source_columns.values())]
        if extra:
            warn(f"table '{name}': CSV column(s) {extra} are not mapped in TMDL (silently dropped)")
        if not missing:
            ok(f"table '{name}': {len(t.columns)} columns bound to {t.csv_file} ({len(rows)} rows)")

        bad: dict[str, tuple[int, str]] = {}
        for row in rows:
            for col, dtype in t.columns.items():
                src = t.source_columns.get(col, col)
                if src not in row or not dtype:
                    continue
                if not type_ok(row[src] or "", dtype):
                    count, sample = bad.get(col, (0, row[src]))
                    bad[col] = (count + 1, sample)
        for col, (count, sample) in bad.items():
            err(f"table '{name}' column '{col}' declared {t.columns[col]} but {count} "
                f"value(s) will not coerce, e.g. {sample!r} -> refresh type error")

    # ---- relationships
    for rel_name, from_t, from_c, to_t, to_c in rels:
        if from_t not in tables or to_t not in tables:
            err(f"relationship {rel_name}: references unknown table ({from_t} -> {to_t})")
            continue
        if from_c not in tables[from_t].columns:
            err(f"relationship {rel_name}: column {from_t}[{from_c}] not defined in TMDL")
            continue
        if to_c not in tables[to_t].columns:
            err(f"relationship {rel_name}: column {to_t}[{to_c}] not defined in TMDL")
            continue
        if from_t not in csv_cache or to_t not in csv_cache:
            continue
        from_src = tables[from_t].source_columns[from_c]
        to_src = tables[to_t].source_columns[to_c]
        is_date = tables[to_t].columns[to_c] == "dateTime"

        def norm(v: str) -> str:
            v = (v or "").strip()
            return v[:10] if is_date else v

        to_vals = [norm(r.get(to_src, "")) for r in csv_cache[to_t][1]]
        to_set = set(to_vals)
        if len(to_set) != len(to_vals):
            err(f"relationship {rel_name}: one-side key {to_t}[{to_c}] is not unique "
                f"({len(to_vals)} rows, {len(to_set)} distinct) -> Power BI rejects the relationship")
        orphans = {norm(r.get(from_src, "")) for r in csv_cache[from_t][1]} - to_set
        orphans.discard("")
        if orphans:
            err(f"relationship {rel_name}: {len(orphans)} distinct {from_t}[{from_c}] value(s) have no "
                f"match in {to_t}[{to_c}], e.g. {sorted(orphans)[:5]} -> rows land on a blank member")
        else:
            ok(f"relationship {rel_name}: {from_t}[{from_c}] -> {to_t}[{to_c}] joins cleanly")

    # ---- DAX references
    all_measures = {m for t in tables.values() for m in t.measures}
    col_index = {(tn, c) for tn, t in tables.items() for c in t.columns}
    dax_errors = 0
    for tname, t in sorted(tables.items()):
        for mname, expr in t.measures.items():
            body = re.sub(r"//.*", "", expr)
            for ref_t, ref_c in re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*)\[([^\]]+)\]", body):
                if (ref_t, ref_c) not in col_index and ref_c not in all_measures:
                    err(f"measure [{mname}] on '{tname}' references undefined column {ref_t}[{ref_c}]")
                    dax_errors += 1
            for b in re.findall(r"(?<![A-Za-z0-9_\]])\[([^\]]+)\]", body):
                if b not in all_measures and (tname, b) not in col_index:
                    err(f"measure [{mname}] on '{tname}' references undefined measure [{b}]")
                    dax_errors += 1
    if not dax_errors:
        ok(f"all {len(all_measures)} DAX measures resolve against the model")

    # ---- report bindings
    report = json.loads(report_json.read_text(encoding="utf-8"))
    aliases = resolve_aliases(report)
    bindings: list[tuple[str, str, str]] = []
    walk_bindings(report, bindings)
    unresolved: dict[tuple[str, str], int] = {}
    for _kind, entity, prop in bindings:
        table_name = aliases.get(entity, entity)
        t = tables.get(table_name)
        if t is None or (prop not in t.columns and prop not in t.measures and prop not in all_measures):
            unresolved[(table_name, prop)] = unresolved.get((table_name, prop), 0) + 1
    for (tname, prop), count in sorted(unresolved.items()):
        err(f"report.json binds {tname}[{prop}] ({count} reference(s)) but the model has no such field -> blank visual")
    pages = len(report.get("sections", []))
    if not unresolved:
        ok(f"all {len(bindings)} report field bindings across {pages} pages resolve against the model")
    else:
        ok(f"checked {len(bindings)} report field bindings across {pages} pages")

    return report_results()


def report_results() -> int:
    print("=" * 78)
    print("PBIP VALIDATION")
    print("=" * 78)
    for p in PASSES:
        print(f"  PASS  {p}")
    if WARNINGS:
        print()
        for w in WARNINGS:
            print(f"  WARN  {w}")
    if ERRORS:
        print()
        for e in ERRORS:
            print(f"  FAIL  {e}")
    print()
    print(f"{len(PASSES)} passed, {len(WARNINGS)} warnings, {len(ERRORS)} errors")
    return 1 if ERRORS else 0


if __name__ == "__main__":
    sys.exit(main())
