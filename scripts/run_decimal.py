

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from bridge.catalogue import load_catalogue
from bridge.decimal_jobs import atomic_write_json, load_decimal_job, write_decimal_status
from bridge.local_levels import SavedLocalLevel
from bridge.native_index import load_native_report, make_native_snapshot
from bridge.removal_plan import (
    classifier_hash,
    make_removal_plan,
    valid_removal_plan,
)
from bridge.snapshot import SnapValidError
from scripts.compile_layout import compile_layout


MOD_ID = "sebastian-anderson.gd-layout-stripper"


def _mod_data(*parts: str) -> Path:
    return (
        Path(os.environ["LOCALAPPDATA"])
        / "GeometryDash"
        / "geode"
        / "mods"
        / MOD_ID
        / Path(*parts)
    )


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def _atomic_copy(source: Path, target: Path, expected_hash: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_file():
        if _file_hash(target) != expected_hash:
            raise SnapValidError("published graph content differs from its identity")
        return
    temporary = target.with_name(target.name + ".tmp")
    shutil.copyfile(source, temporary)
    temporary.replace(target)


def _publish_graph(compiled: object, source_hash: str) -> Path:
    directory = _mod_data("semantic-graphs")
    base = source_hash.lower()
    graph_source = Path(str(compiled.native_details["native_index_path"]))
    graph_hash = str(compiled.native_details["native_index_sha256"]).upper()
    graph_target = directory / f"{base}.{graph_hash.lower()}.bin"
    _atomic_copy(graph_source, graph_target, graph_hash)
    values = compiled.native_details
    seed_source = Path(str(values["native_seed_path"]))
    seed_hash = str(values["native_seed_sha256"]).upper()
    seed_target = directory / f"{base}.{seed_hash.lower()}.seeds"
    _atomic_copy(seed_source, seed_target, seed_hash)
    seed_hash = _file_hash(seed_target)
    if seed_hash != str(values["native_seed_sha256"]).upper():
        raise SnapValidError("published layout seed hash changed")
    roots = {
        "path": str(seed_target.resolve()),
        "sha256": seed_hash,
        "atom_count": int(values["seed_atom_count"]),
        "piece_count": int(values["layout_piece_count"]),
    }
    graph_hash = _file_hash(graph_target)
    if graph_hash != str(compiled.native_details["native_index_sha256"]).upper():
        raise SnapValidError("published semantic graph hash changed")
    manifest = directory / f"{base}.json"
    atomic_write_json(manifest, {
        "schema": "gd-strip-causal-graph",
        "schema_major": 1,
        "source_revision_sha256": source_hash.upper(),
        "classifier_sha256": classifier_hash(),
        "graph_path": str(graph_target.resolve()),
        "graph_sha256": graph_hash,
        "roots": roots,
    })
    return manifest


def run(
    manifest: Path,
    catalogue: Path,
    capabilities: Path,
    *,
    removal_plan_dir: Path | None = None,
    piece_closure_dir: Path | None = None,
) -> dict[str, object]:
    job = load_decimal_job(manifest)
    try:
        write_decimal_status(
            job,
            state="building_native_projection",
            terminal=False,
            success=None,
            message=f'Building the native projection for "{job.level_name}".',
        )
        saved = SavedLocalLevel(
            registry_index=int(job.manifest.get("level_index", -1)),
            name=job.level_name,
            creator="",
            online_id=(
                int(job.manifest["original_level_id"])
                if int(job.manifest.get("original_level_id", 0)) > 0
                else None
            ),
            encoded_level_data="decimal-job-uses-exact-live-editor-string",
        )
        native_run = subprocess.run(
            [
                job.native_worker_path,
                "parse",
                job.raw_path,
                job.native_index_path,
                job.native_report_path,
            ],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        native_report = load_native_report(job.native_report_path)
        if native_run.returncode != 0:
            raise SnapValidError(
                f"native projection worker exited with code {native_run.returncode}"
            )
        projection = make_native_snapshot(
            saved,
            job.level_string,
            job.native_index_path,
            native_report,
            classifier_hash(),
        )
        snapshot = projection.snapshot
        snapshot.canonical["capture"].update(
            {
                "bridge_version": str(job.manifest["bridge_version"]),
                "source": "decimal_live_editor",
                "operation_id": job.operation_id,
            }
        )
        snapshot.canonical["source"].update(
            {
                "level_name": job.level_name,
                "audit_source": "decimal_live_editor",
            }
        )
        write_decimal_status(
            job,
            state="expanding_exact_mechanical_effects",
            terminal=False,
            success=None,
            message=f'Expanding layout effects for "{job.level_name}".',
            details={
                "source_object_count": snapshot.canonical["level"][
                    "serialized_object_count"
                ]
            },
        )
        validated_catalogue = load_catalogue(catalogue)
        capability_data = json.loads(capabilities.read_text(encoding="utf-8"))
        object_count = len(snapshot.canonical["level"]["ordered_objects"])
        index_path = (
            job.manifest_path.parent.parent
            / "indices"
            / f"{job.operation_id}.bin"
        )
        def expansion_complete(_analysis: object, effect_count: int) -> None:
            write_decimal_status(
                job,
                state="closing_true_layout_pieces_independently",
                terminal=False,
                success=None,
                message=(
                    f'Closing every true-layout piece independently for '
                    f'"{job.level_name}", then aggregating completed results.'
                ),
                details={
                    "source_object_count": object_count,
                    "expanded_effect_count": effect_count,
                },
            )

        compiled = compile_layout(
            snapshot,
            validated_catalogue,
            capability_data,
            index_path,
            native_worker=job.native_worker_path,
            capture_diag=False,
            expansion_complete=expansion_complete,
        )
        analysis = compiled.analysis
        python_roots = tuple(sorted(
            int(uid.removeprefix("object-"))
            for uid in analysis.true_layout_uids
        ))
        if python_roots != projection.roots:
            raise SnapValidError("native and expansion roots disagree")
        aggregation = compiled.aggregation
        expansion_ms = compiled.effect_expansion_ms
        solo_closure_ms = compiled.solo_closure_ms
        peak_work_set_bytes = compiled.peak_proc_work_bytes

        piece_audit_path = (
            (piece_closure_dir or job.manifest_path.parent.parent / "piece-closures")
            / f"{job.operation_id}.json"
        )
        atomic_write_json(
            piece_audit_path,
            {
                "schema": "gd-strip-independent-layout-piece-closures",
                "schema_major": 1,
                "operation_id": job.operation_id,
                "level_name": job.level_name,
                "source_revision_sha256": job.source_hash,
                "proof": aggregation.proof,
            },
        )
        plan = make_removal_plan(
            snapshot,
            kept_save_ids=aggregation.kept_obj_ids,
            partition_proof=aggregation.proof,
        )
        valid_removal_plan(plan.data)
        plan_path = (
            removal_plan_dir or _mod_data("removal-plans")
        ) / f"{plan.data['source_editor_revision_sha256'].lower()}.json"
        atomic_write_json(plan_path, plan.data)
        graph_path = _publish_graph(
            compiled, plan.data["source_editor_revision_sha256"]
        )
        return write_decimal_status(
            job,
            state="exact_removal_plan_ready",
            terminal=True,
            success=True,
            message=f'Live-editor removal plan is ready for "{job.level_name}".',
            details={
                "removal_plan_path": str(plan_path.resolve()),
                "removal_plan_sha256": plan.data["plan_sha256"],
                "semantic_graph_path": str(graph_path.resolve()),
                "layout_piece_audit_path": str(piece_audit_path.resolve()),
                "layout_partition_proof": aggregation.proof,
                "effect_expansion_ms": expansion_ms,
                "independent_piece_closure_ms": solo_closure_ms,
                "worker_peak_working_set_bytes": peak_work_set_bytes,
                "removed_object_count": plan.data["removed_object_count"],
                "retained_object_count": plan.data["retained_object_count"],
                "expanded_effect_count": compiled.expanded_fx_count,
                "native_parse_ms": native_report["parse_ms"],
                "native_index_sha256": native_report["index_sha256"],
                "native_root_count": native_report["true_layout_count"],
                "native_closure_ms": compiled.native_details["query_ms"],
                "native_close_ms": compiled.native_details["query_ms"],
                "required_atom_count": compiled.native_details[
                    "required_atom_count"
                ],
                "retained_effect_count": compiled.native_details[
                    "retained_effect_count"
                ],
            },
        )
    except Exception as error:
        write_decimal_status(
            job,
            state="exact_removal_plan_failed",
            terminal=True,
            success=False,
            message=f'Extraction preparation failed for "{job.level_name}": {error}',
            details={"error": str(error)},
        )
        raise


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest", type=Path)
    parser.add_argument(
        "--catalogue", type=Path, default=PROJECT_ROOT / "gd" / "semantics" / "2.2081" / "catalogue.json"
    )
    parser.add_argument(
        "--capabilities",
        type=Path,
        default=PROJECT_ROOT / "gd" / "semantics" / "2.2081" / "effects.json",
    )
    parser.add_argument("--removal-plan-directory", dest="removal_plan_dir", type=Path)
    parser.add_argument("--piece-closure-directory", dest="piece_closure_dir", type=Path)
    args = parser.parse_args()
    try:
        result = run(
            args.manifest,
            args.catalogue,
            args.capabilities,
            removal_plan_dir=args.removal_plan_dir,
            piece_closure_dir=args.piece_closure_dir,
        )
    except (OSError, ValueError, SnapValidError) as error:
        print(json.dumps({"status": "failed", "error": str(error)}, indent=2))
        raise SystemExit(1) from error
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
