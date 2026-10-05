






from __future__ import annotations

import hashlib
import json
from pathlib import Path
import struct
from dataclasses import dataclass
from typing import Any, Iterable

from bridge.level_effects import FX_SEM_REV, LevelEffectAnalysis
from bridge.snapshot import SnapValidError, ValidatedSnapshot


PLAN_SCHEMA = "gd-strip-full-decoration-removal-plan"
PLAN_SCHEMA_MAJOR = 1
PLAN_SCHEMA_MINOR = 8

CLASS_INPUTS = (
    "scripts/compile_layout.py",
    "scripts/gen_native.py",
    "scripts/run_decimal.py",
    "scripts/run_highlight.py",
)

_PARTITION_ALGORITHM = (
    "immutable_piece_roots_exact_reverse_union"
)
_PART_TRUE_FIELDS = (
    "every_true_layout_piece_seeded",
    "root_identity_preserved",
    "monotone_reverse_effect_closure",
    "union_equals_independent_closures",
    "cross_piece_results_never_create_edges",
)


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest().upper()


def _canonical_hash(value: dict[str, Any]) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest().upper()


def classifier_hash() -> str:
    root = Path(__file__).resolve().parents[1]
    inputs = {root / relative for relative in CLASS_INPUTS}
    for directory in (root / "bridge", root / "ml"):
        inputs.update(directory.rglob("*.py"))
    inputs.update(
        path
        for path in (root / "gd" / "semantics" / "2.2081").rglob("*")
        if path.is_file()
    )
    joined = "".join(
        f"{path.relative_to(root).as_posix()}\0{hashlib.sha256(path.read_bytes()).hexdigest()}\n"
        for path in sorted(inputs)
    )
    return _sha256_text(joined)


def _kept_ids_hash(indices: Iterable[int]) -> str:
    digest = hashlib.sha256()
    for index in sorted(indices):
        digest.update(struct.pack("<I", index))
    return digest.hexdigest().upper()


def _valid_part_proof(
    proof: object,
    *,
    kept_obj_count: int | None = None,
) -> dict[str, Any]:
    if not isinstance(proof, dict):
        raise SnapValidError(
            "removal plan lacks the independent true-layout-piece proof"
        )
    if proof.get("algorithm") != _PARTITION_ALGORITHM:
        raise SnapValidError("removal plan uses the wrong layout partition algorithm")
    for key in _PART_TRUE_FIELDS:
        if proof.get(key) is not True:
            raise SnapValidError(
                f"removal plan lacks layout partition guarantee: {key}"
            )
    piece_count = proof.get("piece_count")
    done_piece_count = proof.get("seeded_piece_count")
    if (
        not isinstance(piece_count, int)
        or isinstance(piece_count, bool)
        or piece_count < 0
        or done_piece_count != piece_count
    ):
        raise SnapValidError(
            "removal plan did not seed every true-layout piece"
        )
    proof_retained_count = proof.get("retained_object_count")
    if (
        not isinstance(proof_retained_count, int)
        or isinstance(proof_retained_count, bool)
        or proof_retained_count < 0
    ):
        raise SnapValidError("removal plan has an invalid proven retained count")
    if (
        kept_obj_count is not None
        and proof_retained_count != kept_obj_count
    ):
        raise SnapValidError(
            "removal plan retained count disagrees with its per-piece proof"
        )
    for key in ("piece_manifest_sha256", "aggregate_retained_indices_sha256"):
        digest = proof.get(key)
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789ABCDEFabcdef" for character in digest)
        ):
            raise SnapValidError(f"removal plan has an invalid {key}")
    return json.loads(json.dumps(proof))


@dataclass(frozen=True)
class RemovalPlan:
    data: dict[str, Any]

    @property
    def eligible(self) -> bool:
        return self.data.get("eligible_for_editor_mutation") is True


def _level_from_records(settings: str, records: list[str]) -> str:


    return settings + ";" + "".join(f"{record};" for record in records)


def make_removal_plan(
    snapshot: ValidatedSnapshot,
    analysis: LevelEffectAnalysis | None = None,
    *,
    kept_save_ids: Iterable[int] | None = None,
    partition_proof: dict[str, Any] | None = None,
    unsupported_uses: Iterable[dict[str, Any]] = (),
) -> RemovalPlan:







    level = snapshot.canonical["level"]
    records = level["ordered_objects"]
    exact_level = level["exact_level_string"]
    expected_hash = str(level["exact_level_string_sha256"]).upper()
    if _sha256_text(exact_level) != expected_hash:
        raise SnapValidError("removal-plan source level hash mismatch")

    validated_part_proof = _valid_part_proof(partition_proof)

    expected_indices = set(range(len(records)))
    if analysis is not None:
        if kept_save_ids is not None:
            raise SnapValidError(
                "removal plan received both an analysis and a retained set"
            )
        target_rows = analysis.report.get("fully_target_side_records", [])
        target_indices = {
            int(row["serialization_order_index"]) for row in target_rows
        }
        if len(target_indices) != len(target_rows):
            raise SnapValidError("removal plan has duplicate target indices")
        unsupported = [dict(row) for row in analysis.unsupported_uses]
    else:
        if kept_save_ids is None:
            raise SnapValidError(
                "removal plan requires an analysis or a retained set"
            )
        retained_indices = {int(index) for index in kept_save_ids}
        if not retained_indices.issubset(expected_indices):
            raise SnapValidError("removal plan retains an unknown record index")
        target_indices = expected_indices - retained_indices
        unsupported = [dict(row) for row in unsupported_uses]

    if not target_indices.issubset(expected_indices):
        raise SnapValidError("removal plan targets an unknown record index")

    removed: list[dict[str, Any]] = []
    retained: list[dict[str, Any]] = []
    classifications: list[dict[str, Any]] = []
    retained_exact: list[str] = []
    for index, record in enumerate(records):
        exact = record["exact_save_string"]
        typed = record["typed_projection_derived"]
        role = "target_side_removed" if index in target_indices else "causal_retained"
        row = {
            "serialization_order_index": index,
            "sample_local_uid": record["sample_local_uid"],
            "object_id": typed["object_id"],
            "exact_save_string": exact,
            "exact_save_string_sha256": _sha256_text(exact),
            "editor_layer_1": typed.get("editor_layer_1"),
            "editor_layer_2": typed.get("editor_layer_2"),
            "z_layer": typed.get("z_layer"),
            "z_order": typed.get("z_order"),
            "group_string": typed.get("group_string", ""),
        }
        classifications.append(
            {
                "serialization_order_index": index,
                "sample_local_uid": record["sample_local_uid"],
                "object_id": typed["object_id"],
                "role": role,
            }
        )
        if index in target_indices:
            removed.append(row)
        else:
            retained.append(row)
            retained_exact.append(exact)

    settings = level["settings_exact_save_string"]
    stripped_level = _level_from_records(settings, retained_exact)



    merged_by_index = {
        row["serialization_order_index"]: row["exact_save_string"]
        for row in (*retained, *removed)
    }
    if set(merged_by_index) != expected_indices:
        raise SnapValidError("removal-plan classification is not exhaustive")
    reconstructed = _level_from_records(
        settings, [merged_by_index[index] for index in range(len(records))]
    )
    if reconstructed != exact_level:
        raise SnapValidError("causal + removed records do not fully reconstruct source")

    data: dict[str, Any] = {
        "schema": PLAN_SCHEMA,
        "schema_major": PLAN_SCHEMA_MAJOR,
        "schema_minor": PLAN_SCHEMA_MINOR,
        "effect_semantics_revision": FX_SEM_REV,
        "classifier_sha256": classifier_hash(),
        "game_version": snapshot.canonical["game"]["version"],
        "bindings_commit": snapshot.canonical["game"]["bindings_commit"],
        "bridge_version": snapshot.canonical["capture"]["bridge_version"],
        "level_name": snapshot.canonical["source"].get("level_name"),
        "source_editor_revision_sha256": expected_hash,
        "expected_stripped_revision_sha256": _sha256_text(stripped_level),
        "source_object_count": len(records),
        "retained_object_count": len(retained),
        "removed_object_count": len(removed),
        "all_records_classified": len(classifications) == len(records),
        "effect_expansion_complete_for_revision": not unsupported,
        "conditioning_layout_effect_closure_complete": not unsupported,
        "exact_inverse_merge_proven": reconstructed == exact_level,
        "whole_record_removal": True,
        "property_fragment_mutation": False,
        "layer_agnostic_classification": True,
        "layer_exact_raw_restoration": True,
        "revision_locked": True,
        "eligible_for_editor_mutation": not unsupported,
        "unsupported_uses": unsupported,
        "classifications": classifications,
        "retained_records": retained,
        "removed_records": removed,
    }
    _valid_part_proof(
        validated_part_proof,
        kept_obj_count=len(retained),
    )
    if validated_part_proof["aggregate_retained_indices_sha256"].upper() != (
        _kept_ids_hash(row["serialization_order_index"] for row in retained)
    ):
        raise SnapValidError(
            "removal plan retained indices disagree with the per-piece proof digest"
        )
    data["layout_partition_proof"] = validated_part_proof
    data["plan_sha256"] = _canonical_hash(data)
    return RemovalPlan(data=data)


def valid_removal_plan(plan: dict[str, Any]) -> RemovalPlan:


    if plan.get("schema") != PLAN_SCHEMA:
        raise SnapValidError("unsupported removal-plan schema")
    if plan.get("schema_major") != PLAN_SCHEMA_MAJOR:
        raise SnapValidError("unsupported removal-plan major version")
    if plan.get("schema_minor") != PLAN_SCHEMA_MINOR:
        raise SnapValidError("unsupported removal-plan minor version")
    if plan.get("effect_semantics_revision") != FX_SEM_REV:
        raise SnapValidError("stale removal-plan effect semantics")
    if plan.get("classifier_sha256") != classifier_hash():
        raise SnapValidError("stale removal-plan classifier inputs")
    expected_plan_hash = str(plan.get("plan_sha256", "")).upper()
    unhashed = dict(plan)
    unhashed.pop("plan_sha256", None)
    if _canonical_hash(unhashed) != expected_plan_hash:
        raise SnapValidError("removal-plan manifest hash mismatch")
    required_true = (
        "all_records_classified",
        "effect_expansion_complete_for_revision",
        "conditioning_layout_effect_closure_complete",
        "exact_inverse_merge_proven",
        "whole_record_removal",
        "layer_agnostic_classification",
        "layer_exact_raw_restoration",
        "revision_locked",
        "eligible_for_editor_mutation",
    )
    for key in required_true:
        if plan.get(key) is not True:
            raise SnapValidError(f"removal plan is not eligible: {key}")
    if plan.get("property_fragment_mutation") is not False:
        raise SnapValidError("editor removal plan may not mutate record fragments")
    if plan.get("unsupported_uses") != []:
        raise SnapValidError("eligible removal plan contains unsupported uses")

    source_count = plan.get("source_object_count")
    retained_count = plan.get("retained_object_count")
    removed_count = plan.get("removed_object_count")
    removed = plan.get("removed_records")
    retained = plan.get("retained_records")
    classifications = plan.get("classifications")
    if (
        not isinstance(source_count, int)
        or isinstance(source_count, bool)
        or source_count < 0
    ):
        raise SnapValidError("invalid removal-plan source count")
    if (
        not isinstance(retained_count, int)
        or isinstance(retained_count, bool)
        or retained_count < 0
        or not isinstance(removed_count, int)
        or isinstance(removed_count, bool)
        or removed_count < 0
        or retained_count + removed_count != source_count
    ):
        raise SnapValidError("invalid removal-plan partition counts")
    _valid_part_proof(
        plan.get("layout_partition_proof"),
        kept_obj_count=retained_count,
    )
    if not all(isinstance(value, list) for value in (removed, retained, classifications)):
        raise SnapValidError("removal-plan records must be arrays")
    if len(removed) != removed_count or len(retained) != retained_count:
        raise SnapValidError("removal-plan arrays disagree with partition counts")
    if str(
        plan["layout_partition_proof"]["aggregate_retained_indices_sha256"]
    ).upper() != _kept_ids_hash(
        row.get("serialization_order_index") for row in retained
    ):
        raise SnapValidError(
            "removal plan retained indices disagree with the per-piece proof digest"
        )
    indices = [row.get("serialization_order_index") for row in (*removed, *retained)]
    if sorted(indices) != list(range(source_count)):
        raise SnapValidError("removal-plan record partition is not exhaustive")
    if len(classifications) != source_count:
        raise SnapValidError("removal-plan classification count mismatch")
    for row in (*removed, *retained):
        exact = row.get("exact_save_string")
        if not isinstance(exact, str) or _sha256_text(exact) != row.get(
            "exact_save_string_sha256"
        ):
            raise SnapValidError("removal-plan raw record hash mismatch")
    return RemovalPlan(data=json.loads(json.dumps(plan)))
