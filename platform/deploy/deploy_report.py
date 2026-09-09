#!/usr/bin/env python3
"""Deploy a PBIR report folder to a Fabric workspace.

Shares the auth / retry / LRO helpers with deploy_semantic_model.py so both
deployers behave identically against a flaky network.

    python platform/deploy/deploy_report.py \
        --workspace <workspace-guid> --report-dir AIFinOps.DirectLake.Report

The report binds to its dataset through definition.pbir (`byConnection`), so the
dataset must already exist in the workspace. Stdlib only; requires `az login`.
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from deploy_semantic_model import (  # noqa: E402
    FABRIC, ROOT, az_token, collect_parts, find_item, poll, req,
)

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workspace", required=True, help="workspace GUID")
    ap.add_argument("--report-dir", required=True,
                    help="report folder, e.g. AIFinOps.DirectLake.Report")
    ap.add_argument("--name", default=None,
                    help="display name in Fabric (default: folder name)")
    args = ap.parse_args()

    rdir = args.report_dir
    if not os.path.isabs(rdir):
        rdir = os.path.join(ROOT, rdir)
    if not os.path.isdir(rdir):
        sys.exit("not a directory: %s" % rdir)

    name = args.name or os.path.basename(rdir.rstrip("/\\"))
    token = az_token()

    parts = collect_parts(rdir)
    paths = {p["path"] for p in parts}
    for required in ("definition.pbir", "report.json"):
        if required not in paths:
            sys.exit("report folder is missing %s" % required)

    print("report : %s" % name)
    print("parts  : %d  (%s)" % (len(parts), ", ".join(sorted(paths))))

    existing = find_item(token, args.workspace, "Report", name)
    definition = {"parts": parts}

    if existing:
        print("\nupdating existing report %s ..." % existing)
        status, resp, loc = req(
            "POST", "%s/workspaces/%s/reports/%s/updateDefinition"
            % (FABRIC, args.workspace, existing), token, {"definition": definition})
        if status == 202:
            poll(loc, token, "updateDefinition")
        elif status not in (200, 201):
            sys.exit("updateDefinition failed (%s): %s" % (status, str(resp)[:1200]))
        item_id = existing
        print("  ✓ definition replaced")
    else:
        print("\ncreating report ...")
        status, resp, loc = req(
            "POST", "%s/workspaces/%s/reports" % (FABRIC, args.workspace), token,
            {"displayName": name, "definition": definition})
        if status == 202:
            poll(loc, token, "create report")
        elif status not in (200, 201):
            sys.exit("create failed (%s): %s" % (status, str(resp)[:1200]))
        item_id = find_item(token, args.workspace, "Report", name)
        print("  ✓ created id=%s" % item_id)

    print("\nhttps://app.powerbi.com/groups/%s/reports/%s"
          % (args.workspace, item_id))
    return 0


if __name__ == "__main__":
    sys.exit(main())
