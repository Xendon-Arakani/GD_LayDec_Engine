

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from bridge.catalogue import load_catalogue
from bridge.removal_plan import make_removal_plan, valid_removal_plan
from bridge.snapshot import load_snapshot
from scripts.compile_layout import compile_layout

MOD_ID = "sebastian-anderson.gd-layout-stripper"


def _mod_path(*parts: str) -> Path:
    return (
        Path(os.environ["LOCALAPPDATA"])
        / "GeometryDash"
        / "geode"
        / "mods"
        / MOD_ID
        / Path(*parts)
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "snapshot", nargs="?", type=Path, default=_mod_path("captures", "latest.json")
    )
    parser.add_argument(
        "--catalogue", type=Path, default=_mod_path("catalogues", "latest.json")
    )
    parser.add_argument(
        "--capabilities",
        type=Path,
        default=PROJECT_ROOT
        / "data"
        / "processed"
        / "catalogues"
        / "asset-effect-capabilities-2.2081.json",
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    snapshot = load_snapshot(args.snapshot)
    catalogue = load_catalogue(args.catalogue)
    capabilities = json.loads(args.capabilities.read_text(encoding="utf-8"))
    source_hash = str(
        snapshot.canonical["level"]["exact_level_string_sha256"]
    ).lower()
    compiled_partition = compile_layout(
        snapshot,
        catalogue,
        capabilities,
        _mod_path("removal-plan-indices", f"{source_hash}.bin"),
        capture_diag=False,
    )
    aggregation = compiled_partition.aggregation
    plan = make_removal_plan(
        snapshot,
        kept_save_ids=aggregation.kept_obj_ids,
        partition_proof=aggregation.proof,
    )
    valid_removal_plan(plan.data)

    output = args.output
    if output is None:
        output = _mod_path(
            "removal-plans",
            f"{plan.data['source_editor_revision_sha256'].lower()}.json",
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(plan.data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(json.dumps({
        "status": "ready" if plan.eligible else "blocked",
        "level_name": plan.data["level_name"],
        "source_editor_revision_sha256": plan.data["source_editor_revision_sha256"],
        "removed_object_count": plan.data["removed_object_count"],
        "retained_object_count": plan.data["retained_object_count"],
        "output": str(output.resolve()),
    }, indent=2))


if __name__ == "__main__":
    main()
