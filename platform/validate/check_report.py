#!/usr/bin/env python3
"""Validate a Power BI report definition before (and after) deployment.

Checks, offline:
  1. Geometry — no two visuals on a page overlap, nothing spills off canvas.
  2. Bindings — every queryRef resolves to a real table/column/measure.
  3. Query integrity — every projection queryRef appears in the visual's
     prototypeQuery Select, and every From alias is used.
  4. Blank measures — visuals bound to measures known to return no data.

The model contract is read from a live DAX probe result if supplied
(--model-json), otherwise from the committed TMDL.

Usage:
    python platform/validate/check_report.py --report AIFinOps.DirectLake.Report
    python platform/validate/check_report.py --report <dir> --blank "M365 Prompts" ...
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[2]
ERRORS: list[str] = []
WARNINGS: list[str] = []
PASSES: list[str] = []

NAME_RE = r"(?:'([^']+)'|([^\s=]+))"

# A run of 3+ consecutive single quotes in an emitted literal can only be a
# double-escaped apostrophe: prose that already doubled its quotes, doubled again
# by the emitter. A genuine escaped apostrophe mid-text is exactly two quotes; the
# literal's own delimiters are one each. Three or more means the source was
# escaped twice, so Power BI renders visible ''pairs'' of apostrophes.
TRIPLE_QUOTE_RE = re.compile(r"'{3,}")


def parse_model(model_dir: Path):
    """Return {table: {'columns': set, 'measures': set}} from TMDL."""
    tables: dict[str, dict[str, set]] = {}
    tdir = model_dir / "definition" / "tables"
    for f in sorted(tdir.glob("*.tmdl")):
        text = f.read_text(encoding="utf-8")
        m = re.match(r"table\s+" + NAME_RE, text.splitlines()[0].strip())
        # A leading /// description block pushes the table line down.
        if not m:
            for line in text.splitlines():
                m = re.match(r"table\s+" + NAME_RE, line.strip())
                if m:
                    break
        name = (m.group(1) or m.group(2)) if m else f.stem
        cols = set(re.findall(r"^\tcolumn\s+" + NAME_RE, text, re.M))
        meas = set(re.findall(r"^\tmeasure\s+" + NAME_RE, text, re.M))
        tables[name] = {
            "columns": {a or b for a, b in cols},
            "measures": {a or b for a, b in meas},
        }
    return tables


def overlaps(a, b) -> bool:
    return not (a["x"] + a["width"] <= b["x"] or b["x"] + b["width"] <= a["x"]
                or a["y"] + a["height"] <= b["y"] or b["y"] + b["height"] <= a["y"])


def _lum(hex_color: str) -> float:
    h = hex_color.lstrip("#")
    if len(h) != 6:
        return 0.0
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))

    def f(c):
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)


def contrast(fg: str, bg: str) -> float:
    a, b = _lum(fg), _lum(bg)
    hi, lo = max(a, b), min(a, b)
    return round((hi + 0.05) / (lo + 0.05), 2)


def _colors(obj):
    """Pull (role, hex) pairs out of a vcObjects/objects blob.

    Roles nest as fontColor -> solid -> color -> expr -> Literal, so keep the
    OUTERMOST role name; otherwise the inner `color` key overwrites `fontColor`
    and the pair is never found.
    """
    found = []
    ROLES = ("color", "fontColor", "background", "backColor", "fill", "labelColor")

    def walk(node, role):
        if isinstance(node, dict):
            for k, v in node.items():
                walk(v, role or (k if k in ROLES else ""))
        elif isinstance(node, list):
            for v in node:
                walk(v, role)
        elif isinstance(node, str):
            m = re.fullmatch(r"'(#[0-9A-Fa-f]{6})'", node)
            if m:
                found.append((role or "color", m.group(1)))

    walk(obj, "")
    return found


def check_contrast(pname, kind, x, y, vco):
    """Text on a container must clear WCAG AA (4.5:1) against its background."""
    pairs = dict(_colors(vco.get("background", [])))
    bg = pairs.get("color") or pairs.get("background")
    title = dict(_colors(vco.get("title", [])))
    fg = title.get("fontColor")
    if not bg or not fg:
        return
    ratio = contrast(fg, bg)
    if ratio < 4.5:
        ERRORS.append(
            f"{pname}/{kind} at ({x},{y}): title {fg} on {bg} is {ratio}:1, "
            f"below the WCAG AA 4.5:1 minimum — unreadable at KPI sizes")


def _lit_value(node):
    """Pull the Literal Value out of a {"expr": {"Literal": {"Value": ...}}} blob."""
    return (node or {}).get("expr", {}).get("Literal", {}).get("Value")


def check_header_tooltip(pname, kind, x, y, vco) -> bool:
    """A header tooltip is only visible if its icon renders. `visualHeaderTooltip`
    on its own configures the text but nothing shows it, so the answer to the
    stakeholder's question is silently swallowed — exactly the kind of no-error
    failure every gate in this file exists to catch. Fail when:
      * the tooltip text is declared but visualHeader.showTooltipButton isn't true,
      * the tooltip text is empty,
      * visualHeaderTooltip.type is anything other than 'Default'.
    Returns True when the visual carries a (valid or not) tooltip, so the caller
    can warn about pages that ship no informational tooltip at all."""
    vht = vco.get("visualHeaderTooltip")
    if not vht:
        return False
    props = vht[0].get("properties", {})
    text_val = _lit_value(props.get("text"))
    type_val = _lit_value(props.get("type"))

    vh = vco.get("visualHeader") or [{}]
    show_btn = _lit_value(vh[0].get("properties", {}).get("showTooltipButton"))
    if show_btn != "true":
        ERRORS.append(
            f"{pname}/{kind} at ({x},{y}): visualHeaderTooltip is set but "
            f"visualHeader.showTooltipButton is {show_btn!r}, not \"true\" — the "
            f"info icon never renders, so the tooltip is invisible")
    if not text_val or text_val in ("''", "'"):
        ERRORS.append(
            f"{pname}/{kind} at ({x},{y}): visualHeaderTooltip.text is empty — a "
            f"configured tooltip with no text tells the reader nothing")
    if type_val != "'Default'":
        ERRORS.append(
            f"{pname}/{kind} at ({x},{y}): visualHeaderTooltip.type is {type_val!r}, "
            f"expected \"'Default'\" — a 'Report' page tooltip needs a bound page "
            f"and renders blank here")
    return True


def check_quote_escaping(pname, kind, x, y, sv) -> None:
    """Fail when any literal string in the visual carries a run of 3+ consecutive
    single quotes. That signature only arises from escaping already-escaped text
    (e.g. a tooltip written with ''Unknown'' that the emitter then doubles to
    ''''Unknown''''), which renders as double apostrophes and errors nowhere -
    exactly the silent failure this file exists to catch. Covers title text,
    header tooltip text and textbox paragraph runs alike, wherever they nest."""

    def walk(node):
        if isinstance(node, dict):
            for v in node.values():
                yield from walk(v)
        elif isinstance(node, list):
            for v in node:
                yield from walk(v)
        elif isinstance(node, str):
            if TRIPLE_QUOTE_RE.search(node):
                yield node

    for s in walk(sv):
        ERRORS.append(
            f"{pname}/{kind} at ({x},{y}): literal {s!r} has 3+ consecutive single "
            f"quotes - the source text was quote-escaped twice. Write the prose "
            f"with plain apostrophes (or double quotes) and let the emitter escape "
            f"it once")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", required=True, help="report folder")
    ap.add_argument("--model", default="AIFinOps.DirectLake.SemanticModel",
                    help="semantic model folder the report binds to")
    ap.add_argument("--blank", nargs="*", default=[],
                    help="measures known to return no data on this dataset")
    args = ap.parse_args()

    rdir = ROOT / args.report if not Path(args.report).is_absolute() else Path(args.report)
    mdir = ROOT / args.model if not Path(args.model).is_absolute() else Path(args.model)
    rjson = rdir / "report.json"
    if not rjson.exists():
        print(f"FAIL  no report.json in {rdir}")
        return 1

    tables = parse_model(mdir)
    all_measures = {m for t in tables.values() for m in t["measures"]}
    PASSES.append(f"model: {len(tables)} tables, {len(all_measures)} measures")

    report = json.loads(rjson.read_text(encoding="utf-8"))
    pages = report.get("sections", [])
    blank = set(args.blank)

    total_visuals = 0
    for page in pages:
        pname = page.get("displayName", page.get("name"))
        vcs = page.get("visualContainers", [])
        total_visuals += len(vcs)

        # ---- 1. geometry
        boxes = []
        for vc in vcs:
            cfg = json.loads(vc["config"])
            box = {"x": vc["x"], "y": vc["y"], "width": vc["width"],
                   "height": vc["height"],
                   "name": cfg["singleVisual"]["visualType"],
                   "id": cfg.get("name")}
            boxes.append(box)
            if (box["x"] < 0 or box["y"] < 0
                    or box["x"] + box["width"] > page["width"]
                    or box["y"] + box["height"] > page["height"]):
                ERRORS.append(
                    f"{pname}: {box['name']} at ({box['x']},{box['y']}) "
                    f"{box['width']}x{box['height']} spills outside the "
                    f"{page['width']}x{page['height']} canvas")
        for i in range(len(boxes)):
            for j in range(i + 1, len(boxes)):
                if overlaps(boxes[i], boxes[j]):
                    a, b = boxes[i], boxes[j]
                    ERRORS.append(
                        f"{pname}: {a['name']} ({a['x']},{a['y']} {a['width']}x{a['height']}) "
                        f"overlaps {b['name']} ({b['x']},{b['y']} {b['width']}x{b['height']})")

        # ---- 2/3/4. bindings
        page_tooltips = 0
        for vc in vcs:
            cfg = json.loads(vc["config"])
            sv = cfg["singleVisual"]
            kind = sv["visualType"]
            vco = sv.get("vcObjects", {})
            check_contrast(pname, kind, vc["x"], vc["y"], vco)
            check_quote_escaping(pname, kind, vc["x"], vc["y"], sv)
            if check_header_tooltip(pname, kind, vc["x"], vc["y"], vco):
                page_tooltips += 1
            if kind == "slicer":
                # `data.mode` is the only property that produces a dropdown.
                # `general.orientation` accepts a value and silently ignores it,
                # so assert on the one that actually works.
                mode = (sv.get("objects", {}).get("data", [{}])[0]
                        .get("properties", {}).get("mode", {})
                        .get("expr", {}).get("Literal", {}).get("Value"))
                if mode != "'Dropdown'":
                    ERRORS.append(
                        f"{pname}/slicer: data.mode is {mode!r}, expected "
                        f"\"'Dropdown'\" — the slicer renders as a vertical list "
                        f"that shows only its first few members")
                if vc["height"] < 64:
                    ERRORS.append(
                        f"{pname}/slicer: height {vc['height']} < 64 — a dropdown "
                        f"control is clipped below that")
            if kind == "textbox":
                continue
            pq = sv.get("prototypeQuery", {})
            select_names = {s.get("Name") for s in pq.get("Select", [])}
            aliases = {f["Name"]: f["Entity"] for f in pq.get("From", [])}
            used_aliases = set()

            for s in pq.get("Select", []):
                spec = s.get("Measure") or s.get("Column") or {}
                src = spec.get("Expression", {}).get("SourceRef", {}).get("Source")
                prop = spec.get("Property")
                if src not in aliases:
                    ERRORS.append(f"{pname}/{kind}: Select uses undeclared alias {src!r}")
                    continue
                used_aliases.add(src)
                entity = aliases[src]
                if entity not in tables:
                    ERRORS.append(f"{pname}/{kind}: unknown table {entity!r}")
                    continue
                pool = (tables[entity]["measures"] if "Measure" in s
                        else tables[entity]["columns"])
                if prop not in pool:
                    kindname = "measure" if "Measure" in s else "column"
                    ERRORS.append(
                        f"{pname}/{kind}: {kindname} {entity}[{prop}] does not exist")
                if "Measure" in s and prop in blank:
                    ERRORS.append(
                        f"{pname}/{kind}: bound to [{prop}], which returns no data "
                        f"on this dataset — the visual would render empty")

            for a in set(aliases) - used_aliases:
                WARNINGS.append(f"{pname}/{kind}: From alias {a!r} "
                                f"({aliases[a]}) declared but unused")

            for role, items in sv.get("projections", {}).items():
                for it in items:
                    ref = it.get("queryRef")
                    if ref not in select_names:
                        ERRORS.append(
                            f"{pname}/{kind}: projection {role} -> {ref!r} has no "
                            f"matching entry in prototypeQuery.Select "
                            f"(visual renders its title and stays empty)")

        # ---- 5. informational tooltips. Every page should answer at least one
        # "why does this number look like that?" question in a header tooltip;
        # a page with none is usually an oversight, not a decision, so warn.
        if page_tooltips == 0:
            WARNINGS.append(f"{pname}: no visual carries an informational header "
                            f"tooltip — the page explains none of its numbers")

        PASSES.append(f"{pname}: {len(vcs)} visuals, no overlaps"
                      if not any(pname in e for e in ERRORS)
                      else f"{pname}: {len(vcs)} visuals")

    print("=" * 78)
    print("REPORT VALIDATION")
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
    print(f"{len(pages)} pages, {total_visuals} visuals — "
          f"{len(PASSES)} passed, {len(WARNINGS)} warnings, {len(ERRORS)} errors")
    return 1 if ERRORS else 0


if __name__ == "__main__":
    sys.exit(main())
