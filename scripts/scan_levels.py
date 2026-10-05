

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import time

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from bridge.catalogue import load_catalogue
from bridge.level_effects import analyze_level_fx
from bridge.local_levels import (
    make_full_snapshot,
    decode_saved_level,
    find_local_level,
    load_local_levels,
)
from bridge.removal_plan import (
    make_removal_plan,
    valid_removal_plan,
)
from bridge.snapshot import SnapValidError
from scripts.compile_layout import compile_layout

MOD_ID = "sebastian-anderson.gd-layout-stripper"


def _default_database() -> Path:
    return Path(os.environ["LOCALAPPDATA"]) / "GeometryDash" / "CCLocalLevels.dat"


def _default_catalogue() -> Path:
    return PROJECT_ROOT / "gd" / "semantics" / "2.2081" / "catalogue.json"


def _default_plan_dir() -> Path:
    return (
        Path(os.environ["LOCALAPPDATA"])
        / "GeometryDash"
        / "geode"
        / "mods"
        / MOD_ID
        / "removal-plans"
    )


def _atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(value, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    temporary.replace(path)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("names", nargs="*")
    parser.add_argument("--list", action="store_true")
    parser.add_argument(
        "--all-public",
        action="store_true",
        help="audit every saved level that retains an online level ID",
    )
    parser.add_argument(
        "--all",
        dest="all_levels",
        action="store_true",
        help="audit every saved local level",
    )
    parser.add_argument(
        "--write-removal-plans",
        action="store_true",
        help="write eligible revision-locked plans for extraction",
    )
    parser.add_argument(
        "--removal-plan-directory",
        type=Path,
        default=_default_plan_dir(),
    )
    parser.add_argument("--database", type=Path, default=_default_database())
    parser.add_argument("--catalogue", type=Path, default=_default_catalogue())
    parser.add_argument(
        "--capabilities",
        type=Path,
        default=PROJECT_ROOT / "gd" / "semantics" / "2.2081" / "effects.json",
    )
    parser.add_argument(
        "--output-directory",
        type=Path,
        default=PROJECT_ROOT / "tmp" / "saved-local-level-audit",
    )
    args = parser.parse_args()

    status_path: Path | None = None
    status: dict[str, object] | None = None
    try:
        database_bytes = args.database.read_bytes()
        database_hash = hashlib.sha256(database_bytes).hexdigest().upper()
        levels = load_local_levels(args.database)
        if args.list:
            print(
                json.dumps(
                    [
                        {
                            "registry_index": level.registry_index,
                            "level_name": level.name,
                            "creator": level.creator,
                            "online_level_id": level.online_id,
                        }
                        for level in levels
                    ],
                    indent=2,
                    ensure_ascii=False,
                )
            )
            return
        if args.all_levels and args.all_public:
            raise SnapValidError("--all and --all-public are mutually exclusive")
        if args.all_levels:
            requested_levels = list(levels)
        elif args.all_public:
            requested_levels = [
                level for level in levels if level.online_id is not None
            ]
        else:
            requested_levels = [
                find_local_level(levels, exact_name)
                for exact_name in args.names
            ]
        if not requested_levels:
            raise SnapValidError("provide at least one saved level name")

        catalogue = load_catalogue(args.catalogue)
        capabilities = json.loads(args.capabilities.read_text(encoding="utf-8"))
        status_path = args.output_directory / "status.json"
        status = {
            "schema": "gd-strip-saved-level-audit-status",
            "schema_major": 1,
            "database_path": str(args.database.resolve()),
            "database_sha256": database_hash,
            "state": "running",
            "requested_level_names": [level.name for level in requested_levels],
            "completed": [],
            "failed": [],
        }
        _atomic_json(status_path, status)

        completed: list[dict[str, object]] = []
        failed: list[dict[str, object]] = []
        for level in requested_levels:
            started = time.perf_counter()
            try:
                exact_level_string = decode_saved_level(level)
                snapshot = make_full_snapshot(level, exact_level_string)
                compiled_partition = None
                if args.write_removal_plans:
                    source_hash = str(
                        snapshot.canonical["level"]["exact_level_string_sha256"]
                    ).lower()
                    compiled_partition = compile_layout(
                        snapshot,
                        catalogue,
                        capabilities,
                        args.output_directory / "indices" / f"{source_hash}.bin",
                        capture_diag=True,
                    )
                    analysis = compiled_partition.analysis
                else:
                    analysis = analyze_level_fx(
                        snapshot,
                        catalogue,
                        capabilities,
                        capture_fx=False,
                    )
            except (OSError, SnapValidError, ValueError) as error:
                failure = {
                    "level_name": level.name,
                    "registry_index": level.registry_index,
                    "state": "failed",
                    "error": str(error),
                    "elapsed_seconds": round(time.perf_counter() - started, 3),
                }
                failed.append(failure)
                status["failed"] = failed
                _atomic_json(status_path, status)
                print(json.dumps(failure, ensure_ascii=False), flush=True)
                continue
            report = {
                **analysis.report,
                "audit_source": "persisted_local_level_database",
                "database_sha256": database_hash,
                "registry_index": level.registry_index,
                "online_level_id": level.online_id,
                "serialized_record_count": snapshot.canonical["level"][
                    "serialized_record_count"
                ],
                "engine_ignored_record_count": snapshot.canonical["level"][
                    "engine_ignored_record_count"
                ],
                "dangling_key_record_count": snapshot.canonical["level"][
                    "dangling_key_record_count"
                ],
                "serialized_fragment_ready": True,
                "identical_duplicate_property_count": snapshot.canonical["level"][
                    "identical_duplicate_property_count"
                ],
                "conflicting_duplicate_property_count": snapshot.canonical["level"][
                    "conflicting_duplicate_property_count"
                ],
            }
            output_path = args.output_directory / f"{level.registry_index:03d}-analysis.json"
            _atomic_json(output_path, report)
            removal_plan_path: Path | None = None
            if args.write_removal_plans:
                try:
                    if compiled_partition is None:
                        raise SnapValidError(
                            "independent layout partition was not compiled"
                        )
                    aggregation = compiled_partition.aggregation
                    plan = make_removal_plan(
                        snapshot,
                        kept_save_ids=(
                            aggregation.kept_obj_ids
                        ),
                        partition_proof=aggregation.proof,
                    )
                    valid_removal_plan(plan.data)
                    removal_plan_path = args.removal_plan_directory / (
                        f"{plan.data['source_editor_revision_sha256'].lower()}.json"
                    )
                    _atomic_json(removal_plan_path, plan.data)
                    _atomic_json(
                        args.output_directory
                        / "piece-closures"
                        / f"{plan.data['source_editor_revision_sha256'].lower()}.json",
                        {
                            "schema": "gd-strip-independent-layout-piece-closures",
                            "schema_major": 1,
                            "level_name": level.name,
                            "source_revision_sha256": plan.data[
                                "source_editor_revision_sha256"
                            ],
                            "proof": aggregation.proof,
                            "pieces": aggregation.piece_summaries,
                        },
                    )
                except (OSError, SnapValidError, ValueError) as error:
                    failure = {
                        "level_name": level.name,
                        "registry_index": level.registry_index,
                        "state": "plan_failed",
                        "error": str(error),
                        "analysis_report_path": str(output_path.resolve()),
                        "elapsed_seconds": round(time.perf_counter() - started, 3),
                    }
                    failed.append(failure)
                    status["failed"] = failed
                    _atomic_json(status_path, status)
                    print(json.dumps(failure, ensure_ascii=False), flush=True)
                    continue
            result = {
                "level_name": level.name,
                "registry_index": level.registry_index,
                "online_level_id": level.online_id,
                "level_sha256": report["level_hash"],
                "object_count": report["object_count"],
                "expanded_effect_count": report["expanded_effect_count"],
                "unsupported_use_count": len(report["unsupported_uses"]),
                "effect_expansion_ready": report["effect_expansion_ready"],
                "layout_partition_proof": (
                    compiled_partition.aggregation.proof
                    if compiled_partition is not None
                    else None
                ),
                "effect_expansion_ms": (
                    compiled_partition.effect_expansion_ms
                    if compiled_partition is not None
                    else None
                ),
                "independent_piece_closure_ms": (
                    compiled_partition.solo_closure_ms
                    if compiled_partition is not None
                    else None
                ),
                "worker_peak_working_set_bytes": (
                    compiled_partition.peak_proc_work_bytes
                    if compiled_partition is not None
                    else None
                ),
                "serialized_record_count": report["serialized_record_count"],
                "engine_ignored_record_count": report[
                    "engine_ignored_record_count"
                ],
                "dangling_key_record_count": report["dangling_key_record_count"],
                "identical_duplicate_property_count": report[
                    "identical_duplicate_property_count"
                ],
                "conflicting_duplicate_property_count": report[
                    "conflicting_duplicate_property_count"
                ],
                "audit_ready": report["effect_expansion_ready"]
                and report["serialized_fragment_ready"],
                "elapsed_seconds": round(time.perf_counter() - started, 3),
                "report_path": str(output_path.resolve()),
                "removal_plan_path": str(removal_plan_path.resolve())
                if removal_plan_path is not None
                else None,
            }
            completed.append(result)
            status["completed"] = completed
            _atomic_json(status_path, status)
            print(json.dumps(result, ensure_ascii=False), flush=True)

        status["state"] = "complete" if not failed else "complete_with_failures"
        status["completed_level_count"] = len(completed)
        status["failed_level_count"] = len(failed)
        _atomic_json(status_path, status)
        print(
            json.dumps(
                {
                    "state": status["state"],
                    "completed_level_count": len(completed),
                    "failed_level_count": len(failed),
                    "status_path": str(status_path.resolve()),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
    except (KeyError, OSError, SnapValidError, ValueError) as error:
        if status_path is not None and status is not None:
            status["state"] = "failed"
            status["error"] = str(error)
            try:
                _atomic_json(status_path, status)
            except OSError:
                pass
        print(json.dumps({"state": "failed", "error": str(error)}, ensure_ascii=False))
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
