#!/usr/bin/env python3
"""Static checks for the runnable Fabric medallion notebooks.

Runs locally with no Spark. Catches the PySpark mistake that otherwise only
appears as a failed Spark session ~40s into a remote notebook run:

    resolve = df.withColumn("alias", ...)
    silver.join(resolve, silver.identity_key == resolve.alias, "left")
                                                 ^^^^^^^^^^^^
    AttributeError: 'function' object has no attribute '_get_object_id'

`alias` is a DataFrame *method*, so `resolve.alias` yields the bound method
rather than the column. The same trap exists for ~90 other member names
(`count`, `filter`, `values`, `select`, ...).

Only attribute access in a position where a Column is required is flagged
(comparisons and join/filter/where conditions), so legitimate uses such as
`df.write` and `df.columns` are left alone.

Usage: python platform/validate/check_notebooks.py
"""
from __future__ import annotations

import ast
import csv
import re
import sys
from pathlib import Path

DF_MEMBERS = {
    "agg", "alias", "cache", "checkpoint", "coalesce", "colRegex", "collect",
    "columns", "corr", "count", "cov", "crossJoin", "crosstab", "cube",
    "describe", "distinct", "drop", "dropDuplicates", "dropna", "dtypes",
    "exceptAll", "explain", "fillna", "filter", "first", "foreach",
    "foreachPartition", "freqItems", "groupBy", "head", "hint", "id",
    "intersect", "intersectAll", "isEmpty", "isLocal", "isStreaming", "join",
    "limit", "mapInPandas", "melt", "na", "observe", "offset", "orderBy",
    "persist", "printSchema", "randomSplit", "rdd", "repartition", "replace",
    "rollup", "sample", "sampleBy", "schema", "select", "selectExpr",
    "semanticHash", "show", "sort", "sortWithinPartitions", "sparkSession",
    "stat", "storageLevel", "subtract", "summary", "tail", "take", "to", "toDF",
    "toJSON", "toLocalIterator", "toPandas", "transform", "union", "unionAll",
    "unionByName", "unpersist", "unpivot", "values", "where", "withColumn",
    "withColumnRenamed", "withColumns", "withMetadata", "write", "writeStream",
    "writeTo",
}

ROOT = Path(__file__).resolve().parents[2]
NOTEBOOKS = [
    ROOT / "platform" / "medallion" / "bronze" / "00_load_bronze_csv.py",
    ROOT / "platform" / "medallion" / "bronze" / "01_load_bronze_real_csv.py",
    ROOT / "platform" / "medallion" / "silver" / "10_conform_usage.py",
    ROOT / "platform" / "medallion" / "gold" / "20_build_star.py",
]

errors: list[str] = []


def _operands(node):
    """Yield a node and its operands, WITHOUT descending into call results.

    `df.select(...)` as join's first argument is a DataFrame and legitimate;
    `x.count()` inside a comparison is legitimate too. Only bare attribute
    access that reaches the comparison directly is suspicious.
    """
    if isinstance(node, ast.BinOp):
        yield from _operands(node.left)
        yield from _operands(node.right)
    elif isinstance(node, ast.BoolOp):
        for v in node.values:
            yield from _operands(v)
    elif isinstance(node, ast.UnaryOp):
        yield from _operands(node.operand)
    else:
        yield node


def column_positions(tree: ast.AST):
    """Yield AST nodes that must evaluate to a Column."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            # `c not in df.columns` is a legitimate membership test.
            if any(isinstance(op, (ast.In, ast.NotIn)) for op in node.ops):
                continue
            yield from _operands(node.left)
            for c in node.comparators:
                yield from _operands(c)
        elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.args):
            # join(other, <condition>, how) -> only arg 1 is a Column.
            if node.func.attr == "join" and len(node.args) > 1:
                yield from _operands(node.args[1])
            elif node.func.attr in ("filter", "where"):
                yield from _operands(node.args[0])


def check(path: Path) -> None:
    rel = path.relative_to(ROOT).as_posix()
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError as e:
        errors.append(f"{rel}:{e.lineno}: syntax error: {e.msg}")
        return

    flagged = set()
    for node in column_positions(tree):
        if (isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.attr in DF_MEMBERS):
            key = (node.lineno, node.value.id, node.attr)
            if key in flagged:
                continue
            flagged.add(key)
            errors.append(
                f"{rel}:{node.lineno}: `{node.value.id}.{node.attr}` is used where a "
                f"Column is required, but DataFrame.{node.attr} is a member — "
                f'use {node.value.id}["{node.attr}"]')
    print(f"  checked {rel}")


def check_medallion_layering() -> None:
    """Silver must publish the documented entities, and Gold must read Silver.

    Two architectural regressions this catches, both of which look harmless in a
    diff and are invisible at runtime:

    1. Silver collapsing back to one table. Silver is the curation layer; a
       single fact-shaped output leaves nowhere to resolve identity once, hold a
       de-duplicated reference entity, or run a quality gate.
    2. Gold reaching past Silver into Bronze. That duplicates conforming logic
       in two places, and the copies drift.

    The expected table list is docs/medallion-tables.md, which specified this
    design long before it was implemented.
    """
    silver = ROOT / "platform" / "medallion" / "silver" / "10_conform_usage.py"
    gold = ROOT / "platform" / "medallion" / "gold" / "20_build_star.py"
    if not silver.exists() or not gold.exists():
        return

    expected = {
        "silver_org_hierarchy", "silver_identity_resolved",
        "silver_application_map", "silver_model_map", "silver_rate_card",
        "silver_usage_foundry", "silver_usage_m365", "silver_usage_m365_cowork",
        "silver_usage_ghc", "silver_usage_studio", "silver_usage_unified",
        "silver_cost_reconciliation", "silver_grain_audit",
    }
    src = silver.read_text(encoding="utf-8")
    # Nested parens in the write_silver(...) argument defeat a naive
    # "write_silver(...)" match, so look for the quoted table names directly.
    written = set(re.findall(r'"(silver_[a-z0-9_]+)"', src))
    missing = sorted(expected - written)
    if missing:
        errors.append(
            f"10_conform_usage.py does not write {missing} — silver is the "
            f"curated-entity layer, not a single conformed fact "
            f"(see docs/medallion-tables.md)")
    else:
        print(f"  checked silver publishes {len(written)} curated entities")

    # Gold may inspect bronze for provenance labels, but must not source a
    # dimension from it.
    gsrc = gold.read_text(encoding="utf-8")
    for m in re.finditer(r"^(dim_\w+|fact)\s*=\s*.*?read_bronze\(", gsrc, re.M):
        errors.append(
            f"20_build_star.py builds {m.group(1)} from read_bronze() — gold "
            f"must consume the silver entity instead, or the conforming logic "
            f"exists twice and the copies drift")
    if not re.search(r"def silver\(", gsrc):
        errors.append("20_build_star.py has no silver() reader — gold should "
                      "source its dimensions from the silver layer")
    else:
        print("  checked gold sources its dimensions from silver")

    # The grain guard is the one piece of silver that prevents a silent
    # order-of-magnitude overcount; assert it is still wired.
    if "ACCUMULATION" not in src or "def grain_guard" not in src:
        errors.append("10_conform_usage.py lost the cumulative-vs-delta grain "
                      "guard — summing a cumulative feed overstates spend "
                      "silently")
    else:
        print("  checked cumulative-vs-delta grain guard is present")


def check_gold_contract() -> None:
    """Gold's declared schemas must match the CSV headers exactly.

    fact CONTRACT is order-sensitive (a CSV export from gold must be drop-in for
    the import model). GOLD_CONTRACT covers every table: a Direct Lake model
    refuses to frame with "Delta protocol violation: the column X is not found"
    when a gold table omits something the TMDL declares.
    """
    gold = ROOT / "platform" / "medallion" / "gold" / "20_build_star.py"
    data = ROOT / "AIFinOps.SemanticModel" / "data"
    if not gold.exists() or not data.is_dir():
        return
    src = gold.read_text(encoding="utf-8")

    def csv_header(name: str):
        p = data / f"{name}.csv"
        if not p.exists():
            return None
        with p.open(encoding="utf-8-sig", newline="") as fh:
            return next(csv.reader(fh))

    # ---- fact CONTRACT: order-sensitive
    m = re.search(r"^CONTRACT = \[(.*?)\]", src, re.S | re.M)
    if not m:
        errors.append("20_build_star.py: no CONTRACT list found")
    else:
        declared = [t.strip().strip('"') for t in m.group(1).replace("\n", " ").split(",")
                    if t.strip()]
        header = csv_header("fact_ai_usage")
        if header and declared != header:
            only_gold = [c for c in declared if c not in header]
            only_csv = [c for c in header if c not in declared]
            if only_gold or only_csv:
                errors.append(f"gold CONTRACT vs fact_ai_usage.csv — only in gold: "
                              f"{only_gold}, only in CSV: {only_csv}")
            else:
                errors.append("gold CONTRACT has the right columns in the wrong order; "
                              "a CSV export from gold would not match fact_ai_usage.csv")
        elif header:
            print(f"  checked gold CONTRACT vs fact_ai_usage.csv ({len(header)} columns)")

    # ---- GOLD_CONTRACT: per-table column sets
    gm = re.search(r"^GOLD_CONTRACT = \{(.*?)^\}", src, re.S | re.M)
    if not gm:
        errors.append("20_build_star.py: no GOLD_CONTRACT dict found")
        return
    checked = 0
    for tbl, cols_blob in re.findall(r'"([a-z_]+)":\s*\[(.*?)\]', gm.group(1), re.S):
        declared = [c.strip().strip('"') for c in cols_blob.replace("\n", " ").split(",")
                    if c.strip()]
        header = csv_header(tbl)
        if header is None:
            continue
        if set(declared) != set(header):
            errors.append(
                f"GOLD_CONTRACT['{tbl}'] does not match {tbl}.csv — only in gold: "
                f"{[c for c in declared if c not in header]}, only in CSV: "
                f"{[c for c in header if c not in declared]}")
        checked += 1
    if checked:
        print(f"  checked GOLD_CONTRACT vs {checked} CSV header(s)")


def check_deterministic_ids() -> None:
    """gen_bronze_data.py must not build surrogate ids from the built-in hash().

    Python randomizes hash() of a str per process (PYTHONHASHSEED), so a bare
    `hash(...)` call silently makes platform/fabric/bronze_out/ non-reproducible
    even with random.seed(42) and FINOPS_MOCK_END pinned: `assignee_id` and other
    ids change on every run, and a real data change (like the Cowork overlap that
    hid in bronze_m365_copilot_credits) cannot be told apart from hash churn.
    stable_id()/zlib.crc32 is process-independent; use it instead.

    ast, not a text scan, so mentions of hash() in comments/docstrings are fine
    and only genuine `hash(...)` calls (not `x.hash()`) are flagged.
    """
    gen = ROOT / "platform" / "fabric" / "gen_bronze_data.py"
    if not gen.exists():
        return
    try:
        tree = ast.parse(gen.read_text(encoding="utf-8"), filename=str(gen))
    except SyntaxError as e:
        errors.append(f"gen_bronze_data.py:{e.lineno}: syntax error: {e.msg}")
        return
    found = False
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "hash"):
            found = True
            errors.append(
                f"gen_bronze_data.py:{node.lineno}: bare hash() call — the built-in "
                f"hash() is per-process random (PYTHONHASHSEED), so this breaks the "
                f"byte-for-byte reproducibility of bronze_out/. Use stable_id()/crc32.")
    if not found:
        print("  checked gen_bronze_data.py builds ids without process-random hash()")


def main() -> int:
    print("notebook static checks (PySpark column/member shadowing)")
    for nb in NOTEBOOKS:
        if not nb.exists():
            errors.append(f"missing notebook: {nb}")
            continue
        check(nb)
    check_gold_contract()
    check_medallion_layering()
    check_deterministic_ids()
    print()
    for e in errors:
        print(f"  FAIL {e}")
    print(f"{len(NOTEBOOKS)} notebook(s) checked, {len(errors)} error(s)")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
