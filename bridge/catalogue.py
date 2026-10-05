

from __future__ import annotations

import json
import os
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bridge.snapshot import SnapValidError, ValidatedSnapshot, sha256_file

CATALOGUE_SCHEMA = "gd-strip-object-capability-catalogue"
CATALOGUE_MAJOR = 1
EXP_GD_VER = "2.2081"
EXP_BIND_REV = "7f6c2a75742856de88dad354e576dcff8a28e881"

GAME_OBJECT_TYPES = {
    0: "Solid",
    2: "Hazard",
    3: "InverseGravityPortal",
    4: "NormalGravityPortal",
    5: "ShipPortal",
    6: "CubePortal",
    7: "Decoration",
    8: "YellowJumpPad",
    9: "PinkJumpPad",
    10: "GravityPad",
    11: "YellowJumpRing",
    12: "PinkJumpRing",
    13: "GravityRing",
    14: "InverseMirrorPortal",
    15: "NormalMirrorPortal",
    16: "BallPortal",
    17: "RegularSizePortal",
    18: "MiniSizePortal",
    19: "UfoPortal",
    20: "Modifier",
    21: "Breakable",
    22: "SecretCoin",
    23: "DualPortal",
    24: "SoloPortal",
    25: "Slope",
    26: "WavePortal",
    27: "RobotPortal",
    28: "TeleportPortal",
    29: "GreenRing",
    30: "Collectible",
    31: "UserCoin",
    32: "DropRing",
    33: "SpiderPortal",
    34: "RedJumpPad",
    35: "RedJumpRing",
    36: "CustomRing",
    37: "DashRing",
    38: "GravityDashRing",
    39: "CollisionObject",
    40: "Special",
    41: "SwingPortal",
    42: "GravityTogglePortal",
    43: "SpiderOrb",
    44: "SpiderPad",
    45: "EnterEffectObject",
    46: "TeleportOrb",
    47: "AnimatedHazard",
}
GD_OBJ_CLS_TYPES = {0, 1, 2, 4, 5}
GD_OBJ_TYPE_NAMES = {
    0: "Game",
    1: "Effect",
    2: "Animated",
    4: "Enhanced",
    5: "Smart",
}
ENG_CLASS_FUNCS = (
    "is_trigger",
    "is_basic_trigger",
    "is_color_trigger",
    "is_spawnable_trigger",
    "is_stoppable_trigger",
    "is_editor_spawnable_trigger",
    "is_special_object",
    "is_speed_object",
    "is_configurable_portal",
    "is_settings_object",
)


@dataclass(frozen=True)
class ValidatedCatalogue:
    data: dict[str, Any]
    entries_by_id: dict[int, dict[str, Any]]
    report: dict[str, Any]


def _require(mapping: dict[str, Any], key: str) -> Any:
    if key not in mapping:
        raise SnapValidError(f"catalogue.{key} is required")
    return mapping[key]


def _require_bool(mapping: dict[str, Any], key: str, expected: bool | None = None) -> bool:
    value = _require(mapping, key)
    if not isinstance(value, bool):
        raise SnapValidError(f"catalogue entry {key} must be a boolean")
    if expected is not None and value is not expected:
        raise SnapValidError(f"catalogue {key} must be {expected}")
    return value


def validate_catalogue(
    data: dict[str, Any],
    *,
    source_path: Path | None = None,
    verify_executable: bool = True,
) -> ValidatedCatalogue:
    if data.get("schema") != CATALOGUE_SCHEMA:
        raise SnapValidError("unsupported capability catalogue schema")
    if data.get("schema_major") != CATALOGUE_MAJOR:
        raise SnapValidError("unsupported capability catalogue major version")
    if data.get("game_version") != EXP_GD_VER:
        raise SnapValidError("capability catalogue game version mismatch")
    if data.get("bindings_commit") != EXP_BIND_REV:
        raise SnapValidError("capability catalogue bindings mismatch")
    _require_bool(data, "catalogue_is_final_causal_partition", False)
    _require_bool(data, "unknown_ids_must_fail_closed", True)
    _require_bool(data, "runtime_flag_evidence_is_semantic_authority", False)
    _require_bool(data, "engine_classifier_evidence_is_semantic_authority", False)
    _require_bool(data, "concrete_runtime_identity_is_semantic_authority", False)
    if data.get("probe_mode") != "editor_create_delete_exact_roundtrip":
        raise SnapValidError("catalogue was not created through the real editor path")
    _require_bool(data, "editor_temporarily_mutated", True)
    _require_bool(data, "editor_exact_level_restored", True)
    _require_bool(data, "editor_runtime_object_set_restored", True)

    executable_verified = False
    exp_executable_hash = str(_require(data, "executable_sha256")).upper()
    if verify_executable:
        executable_path = Path(os.environ.get("GD_GAME_EXE", str(_require(data, "executable_path"))))
        if not executable_path.is_file():
            raise SnapValidError(
                f"catalogue executable does not exist: {executable_path}"
            )
        if sha256_file(executable_path) != exp_executable_hash:
            raise SnapValidError("catalogue executable SHA-256 mismatch")
        executable_verified = True

    entries = _require(data, "entries")
    coverage = _require(data, "coverage")
    if not isinstance(entries, list) or not entries:
        raise SnapValidError("catalogue entries must be a non-empty array")
    if not isinstance(coverage, dict):
        raise SnapValidError("catalogue coverage must be an object")

    entries_by_id: dict[int, dict[str, Any]] = {}
    type_counts: Counter[str] = Counter()
    editor_count = 0
    toolbox_count = 0
    previous_id = 0
    for index, raw in enumerate(entries):
        if not isinstance(raw, dict):
            raise SnapValidError(f"catalogue entry {index} is not an object")
        object_id = raw.get("object_id")
        if isinstance(object_id, bool) or not isinstance(object_id, int) or object_id <= 0:
            raise SnapValidError(f"catalogue entry {index} has an invalid object ID")
        if object_id <= previous_id:
            raise SnapValidError("catalogue object IDs are not strictly increasing")
        previous_id = object_id
        entries_by_id[object_id] = raw

        in_toolbox = _require_bool(raw, "present_in_toolbox")
        in_editor = _require_bool(raw, "present_in_editor_buttons")
        if not in_toolbox and not in_editor:
            raise SnapValidError(f"object ID {object_id} has no inventory source")
        toolbox_count += int(in_toolbox)
        editor_count += int(in_editor)

        frame_name = _require(raw, "frame_name")
        if frame_name is not None and not isinstance(frame_name, str):
            raise SnapValidError(
                f"object ID {object_id} has a non-string diagnostic frame name"
            )
        frame_status = _require(raw, "frame_name_status")
        allowed_frame_states = {
            "bounded_ascii_from_intKeyToFrame",
            "bounded_ascii_from_editor_button",
            "unavailable_or_rejected_diagnostic",
        }
        if frame_status not in allowed_frame_states:
            raise SnapValidError(
                f"object ID {object_id} has an unknown frame-name status"
            )
        if frame_name is not None and (
            len(frame_name) > 512
            or any(ord(character) < 0x20 or ord(character) > 0x7E for character in frame_name)
        ):
            raise SnapValidError(
                f"object ID {object_id} has unsafe diagnostic frame-name data"
            )
        if (frame_name is None) != (
            frame_status == "unavailable_or_rejected_diagnostic"
        ):
            raise SnapValidError(
                f"object ID {object_id} frame-name value/status disagree"
            )

        if raw.get("create_status") != "created_by_editor_then_removed":
            raise SnapValidError(
                f"object ID {object_id} was not created through the editor path"
            )
        if raw.get("created_object_id") != object_id:
            raise SnapValidError(f"object ID {object_id} created as a different ID")
        made_run_count = raw.get("created_runtime_object_count")
        if (
            isinstance(made_run_count, bool)
            or not isinstance(made_run_count, int)
            or made_run_count < 1
        ):
            raise SnapValidError(
                f"object ID {object_id} has an invalid initialized runtime count"
            )
        _require_bool(raw, "runtime_cleanup_complete", True)
        default_save = _require(raw, "initialized_default_save_string")
        if not isinstance(default_save, str) or not default_save:
            raise SnapValidError(
                f"object ID {object_id} has no initialized default serialization"
            )
        type_value = raw.get("runtime_object_type")
        if type_value not in GAME_OBJECT_TYPES:
            raise SnapValidError(
                f"object ID {object_id} has unknown runtime type {type_value}"
            )
        type_name = GAME_OBJECT_TYPES[type_value]
        if raw.get("runtime_object_type_name") != type_name:
            raise SnapValidError(f"object ID {object_id} runtime type name mismatch")
        _require_bool(raw, "runtime_type_known", True)
        if _require_bool(raw, "runtime_non_decoration_type") != (type_value != 7):
            raise SnapValidError(
                f"object ID {object_id} has inconsistent decoration-type evidence"
            )
        class_type = raw.get("class_type")
        if class_type not in GD_OBJ_CLS_TYPES:
            raise SnapValidError(
                f"object ID {object_id} has unknown class type {class_type}"
            )
        if raw.get("class_type_name") != GD_OBJ_TYPE_NAMES[class_type]:
            raise SnapValidError(
                f"object ID {object_id} class type name mismatch"
            )
        vtable_rva = raw.get("runtime_vtable_rva_hex")
        if not isinstance(vtable_rva, str) or not re.fullmatch(
            r"[0-9A-F]+", vtable_rva
        ):
            raise SnapValidError(
                f"object ID {object_id} has invalid concrete runtime identity"
            )
        engine_classifiers = raw.get("engine_classifier_methods")
        if not isinstance(engine_classifiers, dict):
            raise SnapValidError(
                f"object ID {object_id} lacks engine classifier evidence"
            )
        if set(engine_classifiers) != set(ENG_CLASS_FUNCS):
            raise SnapValidError(
                f"object ID {object_id} engine classifier evidence is incomplete"
            )
        for classifier in ENG_CLASS_FUNCS:
            if not isinstance(engine_classifiers[classifier], bool):
                raise SnapValidError(
                    f"object ID {object_id} classifier {classifier} is not boolean"
                )
        for boolean_field in (
            "is_trigger",
            "is_decoration_flag_1",
            "is_decoration_flag_2",
            "is_invisible_block",
            "is_start_position",
            "is_passable",
            "is_no_touch",
            "slope_is_hazard",
            "has_extended_collision",
        ):
            value = _require_bool(raw, boolean_field)
            raw_byte = _require(raw, f"{boolean_field}_raw_byte")
            canonical_storage = _require_bool(
                raw, f"{boolean_field}_canonical_storage"
            )
            if (
                isinstance(raw_byte, bool)
                or not isinstance(raw_byte, int)
                or not 0 <= raw_byte <= 255
            ):
                raise SnapValidError(
                    f"object ID {object_id} has invalid {boolean_field} raw-byte evidence"
                )
            if value != (raw_byte != 0):
                raise SnapValidError(
                    f"object ID {object_id} has inconsistent {boolean_field} evidence"
                )
            if canonical_storage != (raw_byte <= 1):
                raise SnapValidError(
                    f"object ID {object_id} has inconsistent {boolean_field} storage status"
                )
        _require_bool(raw, "has_base_color_state")
        _require_bool(raw, "has_detail_color_state")
        type_counts[type_name] += 1

    expected_counts = {
        "toolbox_unique_ids": toolbox_count,
        "editor_button_unique_ids": editor_count,
        "union_unique_ids": len(entries),
        "create_failures": 0,
        "unknown_runtime_types": 0,
        "noncanonical_runtime_flag_storage": sum(
            int(not raw[f"{field}_canonical_storage"])
            for raw in entries
            for field in (
                "is_trigger",
                "is_decoration_flag_1",
                "is_decoration_flag_2",
                "is_invisible_block",
                "is_start_position",
                "is_passable",
                "is_no_touch",
                "slope_is_hazard",
                "has_extended_collision",
            )
        ),
        "runtime_cleanup_failures": 0,
    }
    for key, expected in expected_counts.items():
        if coverage.get(key) != expected:
            raise SnapValidError(
                f"catalogue coverage {key} is {coverage.get(key)}, expected {expected}"
            )

    report = {
        "status": "passed",
        "source_path": str(source_path.resolve()) if source_path else None,
        "source_file_sha256": sha256_file(source_path) if source_path else None,
        "game_version": EXP_GD_VER,
        "executable_sha256": exp_executable_hash,
        "executable_verified": executable_verified,
        "union_unique_ids": len(entries),
        "toolbox_unique_ids": toolbox_count,
        "editor_button_unique_ids": editor_count,
        "runtime_type_counts": dict(sorted(type_counts.items())),
        "catalogue_is_final_causal_partition": False,
        "noncanonical_runtime_flag_storage": expected_counts[
            "noncanonical_runtime_flag_storage"
        ],
        "editor_exact_level_restored": True,
        "editor_runtime_object_set_restored": True,
    }
    return ValidatedCatalogue(data=data, entries_by_id=entries_by_id, report=report)


def load_catalogue(
    path: Path, *, verify_executable: bool = True
) -> ValidatedCatalogue:
    path = path.resolve(strict=True)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise SnapValidError(f"cannot read catalogue {path}: {error}") from error
    if not isinstance(data, dict):
        raise SnapValidError("capability catalogue root must be an object")
    return validate_catalogue(
        data, source_path=path, verify_executable=verify_executable
    )


def check_cat_coverage(
    snapshot: ValidatedSnapshot, catalogue: ValidatedCatalogue
) -> dict[str, Any]:
    missing_ids: set[int] = set()
    type_mismatches: list[tuple[int, int, int]] = []
    checked = 0
    for record in snapshot.canonical["level"]["ordered_objects"]:
        typed = record["typed_projection_derived"]
        object_id = typed.get("object_id")
        runtime_type = typed.get("runtime_object_type")
        if not isinstance(object_id, int) or not isinstance(runtime_type, int):
            raise SnapValidError("snapshot typed projection lacks object ID/type")
        entry = catalogue.entries_by_id.get(object_id)
        if entry is None:
            missing_ids.add(object_id)
            continue
        expected_type = entry["runtime_object_type"]
        if runtime_type != expected_type:
            type_mismatches.append((object_id, runtime_type, expected_type))
        checked += 1
    if missing_ids:
        raise SnapValidError(
            f"snapshot contains object IDs absent from the build catalogue: {sorted(missing_ids)}"
        )
    if type_mismatches:
        raise SnapValidError(
            f"snapshot runtime types disagree with catalogue: {type_mismatches[:10]}"
        )
    return {
        "status": "passed",
        "objects_checked": checked,
        "distinct_object_ids": len(
            {
                record["typed_projection_derived"]["object_id"]
                for record in snapshot.canonical["level"]["ordered_objects"]
            }
        ),
        "missing_catalogue_ids": 0,
        "runtime_type_mismatches": 0,
        "causal_partition_performed": False,
    }
