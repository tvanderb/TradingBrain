#!/usr/bin/env python3
"""
Build self-contained Grafana dashboard JSON.

Converts library panel references back to inline definitions so the dashboard
survives Grafana volume resets. Sources:
  - Old dashboard (pre-Session-V commit a5b8007^): all panels inline
  - Current dashboard: layout with library refs + Session W inline panels
  - Orchestrator text panels: built from scratch (new in Session V)

Usage:
    python3 monitoring/build_dashboard.py
"""

import json
import subprocess
import copy
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DASHBOARD_PATH = (
    REPO_ROOT
    / "monitoring"
    / "grafana"
    / "provisioning"
    / "dashboards"
    / "json"
    / "trading-brain.json"
)

# Old datasource UIDs to replace
OLD_PROMETHEUS_UID = "PBFA97CFB590B2093"
OLD_LOKI_UID = "P8E80F9AEF21F6940"

# New explicit UIDs (from datasources.yml)
NEW_PROMETHEUS_UID = "prometheus"
NEW_LOKI_UID = "loki"


def load_old_dashboard() -> dict:
    """Extract pre-Session-V dashboard from git (all panels inline)."""
    result = subprocess.run(
        ["git", "show", "a5b8007^:monitoring/grafana/provisioning/dashboards/json/trading-brain.json"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    if result.returncode != 0:
        print(f"ERROR: git show failed: {result.stderr}", file=sys.stderr)
        sys.exit(1)
    return json.loads(result.stdout)


def load_current_dashboard() -> dict:
    """Load current dashboard (has library refs + Session W inline panels)."""
    return json.loads(DASHBOARD_PATH.read_text())


def build_panel_index(dashboard: dict) -> dict[int, dict]:
    """Build panel_id -> full definition mapping from old dashboard."""
    index = {}

    def walk(panels):
        for panel in panels:
            if "id" in panel:
                index[panel["id"]] = panel
            # Rows have nested panels
            if panel.get("type") == "row" and "panels" in panel:
                walk(panel["panels"])

    walk(dashboard.get("panels", []))
    return index


def normalize_datasource_uids(obj):
    """Recursively replace old auto-generated UIDs with explicit ones."""
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key == "uid" and value == OLD_PROMETHEUS_UID:
                obj[key] = NEW_PROMETHEUS_UID
            elif key == "uid" and value == OLD_LOKI_UID:
                obj[key] = NEW_LOKI_UID
            else:
                normalize_datasource_uids(value)
    elif isinstance(obj, list):
        for item in obj:
            normalize_datasource_uids(item)


def normalize_loki_label(obj):
    """Replace container= with compose_service= in Loki queries."""
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key == "expr" and isinstance(value, str) and '{container="trading-brain"}' in value:
                obj[key] = value.replace('{container="trading-brain"}', '{compose_service="trading-brain"}')
            else:
                normalize_loki_label(value)
    elif isinstance(obj, list):
        for item in obj:
            normalize_loki_label(item)


def make_session_v_panels() -> dict[str, dict]:
    """
    Build panels that were new in Session V (not in old dashboard).
    3 orchestrator text panels + 1 candidate log panel.
    Returns uid -> panel definition.
    """
    panels = {}

    # Market Outlook: latest observation's market field
    panels["tb-lib-orch-market-outlook"] = {
        "datasource": {"type": "loki", "uid": NEW_LOKI_UID},
        "fieldConfig": {"defaults": {}, "overrides": []},
        "options": {
            "dedupStrategy": "none",
            "enableLogDetails": False,
            "prettifyLogMessage": False,
            "showCommonLabels": False,
            "showLabels": False,
            "showTime": False,
            "sortOrder": "Descending",
            "wrapLogMessage": True,
        },
        "targets": [
            {
                "expr": '{compose_service="trading-brain"} |= "orchestrator.observation_stored" | json | line_format "{{.market}}"',
                "refId": "A",
                "maxLines": 1,
            }
        ],
        "title": "Orchestrator: Market Outlook",
        "type": "logs",
    }

    # Strategy Assessment: latest observation's assessment field
    panels["tb-lib-orch-strategy-assessment"] = {
        "datasource": {"type": "loki", "uid": NEW_LOKI_UID},
        "fieldConfig": {"defaults": {}, "overrides": []},
        "options": {
            "dedupStrategy": "none",
            "enableLogDetails": False,
            "prettifyLogMessage": False,
            "showCommonLabels": False,
            "showLabels": False,
            "showTime": False,
            "sortOrder": "Descending",
            "wrapLogMessage": True,
        },
        "targets": [
            {
                "expr": '{compose_service="trading-brain"} |= "orchestrator.observation_stored" | json | line_format "{{.assessment}}"',
                "refId": "A",
                "maxLines": 1,
            }
        ],
        "title": "Orchestrator: Strategy Assessment",
        "type": "logs",
    }

    # Cross-Reference Findings: nested inside detail JSON
    panels["tb-lib-orch-cross-ref"] = {
        "datasource": {"type": "loki", "uid": NEW_LOKI_UID},
        "fieldConfig": {"defaults": {}, "overrides": []},
        "options": {
            "dedupStrategy": "none",
            "enableLogDetails": False,
            "prettifyLogMessage": False,
            "showCommonLabels": False,
            "showLabels": False,
            "showTime": False,
            "sortOrder": "Descending",
            "wrapLogMessage": True,
        },
        "targets": [
            {
                "expr": '{compose_service="trading-brain"} |= "orchestrator.thought_stored" | json | step="analysis" | line_format "{{.detail}}" | json | line_format "{{.cross_reference_findings}}"',
                "refId": "A",
                "maxLines": 1,
            }
        ],
        "title": "Orchestrator: Cross-Reference Findings",
        "type": "logs",
    }

    # Candidate Log: candidate lifecycle events (new in Session V)
    panels["tb-lib-910"] = {
        "datasource": {"type": "loki", "uid": NEW_LOKI_UID},
        "fieldConfig": {"defaults": {}, "overrides": []},
        "options": {
            "dedupStrategy": "none",
            "enableLogDetails": True,
            "prettifyLogMessage": False,
            "showCommonLabels": False,
            "showLabels": False,
            "showTime": True,
            "sortOrder": "Descending",
            "wrapLogMessage": True,
        },
        "targets": [
            {
                "expr": '{compose_service="trading-brain"} |= "candidate." | json | line_format "{{.level | ToUpper}} [{{.event}}] slot={{.slot}} {{if .version}}v={{.version}}{{end}} {{if .positions}}pos={{.positions}}{{end}} {{if .value}}val={{.value}}{{end}} {{if .reason}}reason={{.reason}}{{end}}"',
                "refId": "A",
            }
        ],
        "title": "Candidate Log",
        "type": "logs",
    }

    return panels


def replace_library_refs(panel: dict, panel_index: dict[int, dict], extra_panels: dict[str, dict]) -> dict:
    """Replace a libraryPanel reference with the full inline definition."""
    if "libraryPanel" not in panel:
        return panel

    uid = panel["libraryPanel"]["uid"]
    panel_id = panel["id"]
    grid_pos = panel["gridPos"]

    # Check orchestrator panels first (not in old dashboard)
    if uid in extra_panels:
        new_panel = copy.deepcopy(extra_panels[uid])
        new_panel["id"] = panel_id
        new_panel["gridPos"] = grid_pos
        return new_panel

    # Look up in old dashboard by panel ID
    if panel_id in panel_index:
        new_panel = copy.deepcopy(panel_index[panel_id])
        # Use gridPos from the CURRENT layout (Session V may have moved panels)
        new_panel["gridPos"] = grid_pos
        return new_panel

    # Panel not found — leave as-is with a warning
    print(f"WARNING: Panel id={panel_id} uid={uid} not found in old dashboard or orchestrator panels", file=sys.stderr)
    return panel


def process_panels(panels: list, panel_index: dict[int, dict], extra_panels: dict[str, dict]) -> list:
    """Walk panel list, replacing library refs. Handles rows with nested panels."""
    result = []
    for panel in panels:
        if panel.get("type") == "row" and "panels" in panel:
            new_row = copy.deepcopy(panel)
            new_row["panels"] = []
            for child in panel["panels"]:
                resolved = replace_library_refs(child, panel_index, extra_panels)
                new_row["panels"].append(resolved)
            result.append(new_row)
        else:
            resolved = replace_library_refs(panel, panel_index, extra_panels)
            result.append(resolved)
    return result


def main():
    print("Loading old dashboard from git (a5b8007^)...")
    old_dash = load_old_dashboard()

    print("Loading current dashboard...")
    current_dash = load_current_dashboard()

    print("Building panel index from old dashboard...")
    panel_index = build_panel_index(old_dash)
    print(f"  Found {len(panel_index)} panels in old dashboard")

    print("Building Session V panels (orchestrator text + candidate log)...")
    extra_panels = make_session_v_panels()
    print(f"  Built {len(extra_panels)} extra panels")

    # Count library refs before
    lib_count = 0
    def count_refs(panels):
        nonlocal lib_count
        for p in panels:
            if "libraryPanel" in p:
                lib_count += 1
            if p.get("type") == "row" and "panels" in p:
                count_refs(p["panels"])
    count_refs(current_dash.get("panels", []))
    print(f"  {lib_count} library panel references to resolve")

    print("Replacing library panel references with inline definitions...")
    current_dash["panels"] = process_panels(
        current_dash["panels"], panel_index, extra_panels
    )

    print("Normalizing datasource UIDs...")
    normalize_datasource_uids(current_dash)

    print("Normalizing Loki labels...")
    normalize_loki_label(current_dash)

    # Bump version
    current_dash["version"] = 9

    print(f"Writing to {DASHBOARD_PATH}...")
    DASHBOARD_PATH.write_text(json.dumps(current_dash, indent=2) + "\n")

    # Verify valid JSON
    json.loads(DASHBOARD_PATH.read_text())

    # Count final state
    final_panels = 0
    final_lib = 0
    def count_final(panels):
        nonlocal final_panels, final_lib
        for p in panels:
            if p.get("type") != "row":
                final_panels += 1
            if "libraryPanel" in p:
                final_lib += 1
            if p.get("type") == "row" and "panels" in p:
                count_final(p["panels"])
    count_final(current_dash.get("panels", []))

    print(f"\nDone! {final_panels} inline panels, {final_lib} library refs remaining")
    if final_lib > 0:
        print(f"WARNING: {final_lib} unresolved library panel references!", file=sys.stderr)
        sys.exit(1)
    print("Dashboard is fully self-contained.")


if __name__ == "__main__":
    main()
