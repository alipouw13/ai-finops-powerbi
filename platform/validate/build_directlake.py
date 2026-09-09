#!/usr/bin/env python3
"""Generate a Direct Lake semantic model from the committed import model.

The PBIP in this repo is an **import** model reading local CSVs, which is what
makes it openable with no Fabric dependency. That same model cannot refresh in
the Power BI service without a gateway.

This produces a **separate** Direct Lake artifact over the gold Lakehouse,
reusing the import model's tables, columns, relationships and all 42 DAX
measures verbatim. The only thing that changes is the partition: each table
swaps its M/CSV partition for an `entity` partition in `directLake` mode.

    import model                      Direct Lake model
    ------------                      -----------------
    partition t = m                   partition t = entity
      mode: import                      mode: directLake
      source = let Csv.Document(...)    source
                                          entityName: t
                                          schemaName: dbo
                                          expressionSource: 'DirectLake - AIFinOps'

The original AIFinOps.pbip is never modified, so the offline demo keeps working.

Usage:
    python platform/validate/build_directlake.py \
        --workspace <workspace-guid> --lakehouse <gold-lakehouse-guid>
    python platform/validate/build_directlake.py ... --out AIFinOps.DirectLake

Stdlib only.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[2]
SRC_MODEL = ROOT / "AIFinOps.SemanticModel"
EXPRESSION = "DirectLake - AIFinOps"

# TMDL table name -> Delta table in the gold lakehouse. They match except for
# dim_data_source, which the model names differently from its source table.
ENTITY_OVERRIDES = {"dim_data_source": "extractable_data_catalog"}


def strip_partition(text: str) -> str:
    """Remove the trailing `partition ... = m` block from a table TMDL."""
    idx = text.find("\n\tpartition ")
    return text[:idx] if idx != -1 else text.rstrip()


def directlake_partition(table: str) -> str:
    entity = ENTITY_OVERRIDES.get(table, table)
    return (
        f"\n\tpartition {table} = entity\n"
        f"\t\tmode: directLake\n"
        f"\t\tsource\n"
        f"\t\t\tentityName: {entity}\n"
        f"\t\t\tschemaName: dbo\n"
        f"\t\t\texpressionSource: '{EXPRESSION}'\n"
    )


def convert_model(text: str, ws_id: str, lh_id: str) -> str:
    """Swap the DataFolder parameter for the OneLake named expression."""
    expr = (
        f"/// Direct Lake connection to the gold Lakehouse. Tables bind to Delta\n"
        f"/// files in OneLake, so there is no refresh and no gateway.\n"
        f"expression '{EXPRESSION}' =\n"
        f"\t\tlet\n"
        f"\t\t    Source = AzureStorage.DataLake("
        f'"https://onelake.dfs.fabric.microsoft.com/{ws_id}/{lh_id}", '
        f"[HierarchicalNavigation=true])\n"
        f"\t\tin\n"
        f"\t\t    Source\n\n"
    )
    # Drop the DataFolder expression (including its doc comments and metadata)
    # and put the Direct Lake expression in its place.
    lines = text.splitlines(keepends=True)
    out, i, replaced = [], 0, False
    while i < len(lines):
        line = lines[i]
        if line.startswith("expression DataFolder"):
            # Back up over the preceding /// comment block.
            while out and out[-1].lstrip().startswith("///"):
                out.pop()
            out.append(expr)
            replaced = True
            i += 1
            # Skip the parameter's indented annotation/lineage lines.
            while i < len(lines) and (lines[i].startswith(("\t", " ")) or not lines[i].strip()):
                if lines[i].strip() and not lines[i].lstrip().startswith(
                        ("annotation", "lineageTag", "meta")):
                    break
                i += 1
            continue
        out.append(line)
        i += 1
    if not replaced:
        sys.exit("model.tmdl has no DataFolder expression to replace")
    return "".join(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workspace", required=True, help="workspace GUID")
    ap.add_argument("--lakehouse", required=True, help="GOLD lakehouse GUID")
    ap.add_argument("--out", default="AIFinOps.DirectLake",
                    help="output folder name (default: AIFinOps.DirectLake)")
    args = ap.parse_args()

    out_dir = ROOT / f"{args.out}.SemanticModel"
    defn = out_dir / "definition"
    tables_out = defn / "tables"
    tables_out.mkdir(parents=True, exist_ok=True)

    # database.tmdl — Direct Lake needs compatibility level 1604+.
    (defn / "database.tmdl").write_text("database\n\tcompatibilityLevel: 1604\n",
                                        encoding="utf-8")

    (out_dir / "definition.pbism").write_text(json.dumps({
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/"
                   "semanticModel/definitionProperties/1.0.0/schema.json",
        "version": "4.2", "settings": {},
    }, indent=2) + "\n", encoding="utf-8")

    (out_dir / ".platform").write_text(json.dumps({
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/"
                   "gitIntegration/platformProperties/2.0.0/schema.json",
        "metadata": {"type": "SemanticModel", "displayName": args.out},
        "config": {"version": "2.0",
                   "logicalId": "00000000-0000-0000-0000-000000000003"},
    }, indent=2) + "\n", encoding="utf-8")

    model_src = (SRC_MODEL / "definition" / "model.tmdl").read_text(encoding="utf-8")
    (defn / "model.tmdl").write_text(
        convert_model(model_src, args.workspace, args.lakehouse), encoding="utf-8")
    print(f"  model.tmdl        -> Direct Lake expression '{EXPRESSION}'")

    converted = 0
    for src in sorted((SRC_MODEL / "definition" / "tables").glob("*.tmdl")):
        text = src.read_text(encoding="utf-8")
        m = re.match(r"table\s+(?:'([^']+)'|(\S+))", text)
        table = (m.group(1) or m.group(2)) if m else src.stem
        body = strip_partition(text) + directlake_partition(table)
        (tables_out / src.name).write_text(body, encoding="utf-8")
        entity = ENTITY_OVERRIDES.get(table, table)
        note = f"  (entity: dbo.{entity})" if entity != table else ""
        print(f"  {src.name:34} -> directLake{note}")
        converted += 1

    print(f"\n{converted} table(s) converted to Direct Lake.")
    print(f"Output: {out_dir.relative_to(ROOT)}")
    print("\nDeploy with:")
    print(f"  python platform/deploy/deploy_semantic_model.py \\")
    print(f"      --workspace {args.workspace} --model-dir {out_dir.name}")
    _ = shutil
    return 0


if __name__ == "__main__":
    sys.exit(main())
