

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA_NAME = "gd-strip-editor-snapshot"
SCHEMA_MAJOR = 1
SCHEMA_MINOR = 1
BINDINGS_COMMIT = "7f6c2a75742856de88dad354e576dcff8a28e881"
EXP_GD_VER = "2.2081"


class SnapValidError(ValueError):
    pass


@dataclass(frozen=True)
class ValidatedSnapshot:
    canonical: dict[str, Any]
    report: dict[str, Any]


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest().upper()


def sha256_text(value: str) -> str:
    return sha256_bytes(value.encode("utf-8"))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def _object(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SnapValidError(f"{path} must be an object")
    return value


def _array(value: Any, path: str) -> list[Any]:
    if not isinstance(value, list):
        raise SnapValidError(f"{path} must be an array")
    return value


def _string(value: Any, path: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value):
        qualifier = "a string" if allow_empty else "a non-empty string"
        raise SnapValidError(f"{path} must be {qualifier}")
    return value


def _integer(value: Any, path: str, *, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SnapValidError(f"{path} must be an integer")
    if minimum is not None and value < minimum:
        raise SnapValidError(f"{path} must be at least {minimum}")
    return value


def _required(mapping: dict[str, Any], key: str, path: str) -> Any:
    if key not in mapping:
        raise SnapValidError(f"{path}.{key} is required")
    return mapping[key]


def split_keep_empty(value: str, delimiter: str) -> list[str]:
    return value.split(delimiter)


def parse_ordered_pairs(exact: str, path: str) -> tuple[list[str], list[dict[str, str]]]:
    tokens = split_keep_empty(exact, ",")
    if len(tokens) % 2:
        raise SnapValidError(
            f"{path} has an odd raw token count; refusing to normalize malformed state"
        )
    pairs = [
        {"key_text": tokens[index], "value_text": tokens[index + 1]}
        for index in range(0, len(tokens), 2)
    ]
    return tokens, pairs


def _validate_file_hash(path_text: str, expected_hash: str, label: str) -> Path:
    path = Path(path_text)
    if not path.is_file():
        raise SnapValidError(f"{label} does not exist: {path}")
    actual = sha256_file(path)
    if actual != expected_hash.upper():
        raise SnapValidError(
            f"{label} SHA-256 mismatch: expected {expected_hash}, got {actual}"
        )
    return path


def validate_snapshot(
    data: dict[str, Any],
    *,
    source_path: Path | None = None,
    verify_local_files: bool = True,
) -> ValidatedSnapshot:







    root = _object(data, "snapshot")
    schema = _object(_required(root, "schema", "snapshot"), "schema")
    if _string(_required(schema, "name", "schema"), "schema.name") != SCHEMA_NAME:
        raise SnapValidError("unsupported snapshot schema name")
    if _integer(_required(schema, "major", "schema"), "schema.major") != SCHEMA_MAJOR:
        raise SnapValidError("unsupported snapshot schema major version")
    minor = _integer(_required(schema, "minor", "schema"), "schema.minor", minimum=0)
    if minor > SCHEMA_MINOR:
        raise SnapValidError("snapshot schema minor version is newer than this validator")

    capture = _object(_required(root, "capture", "snapshot"), "capture")
    request_id = _string(_required(capture, "request_id", "capture"), "capture.request_id")
    if capture.get("read_only") is not True:
        raise SnapValidError("capture.read_only must be true")
    if capture.get("editor_mutated") is not False:
        raise SnapValidError("capture.editor_mutated must be false")

    recovery_verified = False
    if minor >= 1:
        recovery_path_text = _string(
            _required(capture, "raw_recovery_path", "capture"),
            "capture.raw_recovery_path",
        )
        if verify_local_files:
            recovery_path = Path(recovery_path_text)
            try:
                recovery = json.loads(recovery_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as error:
                raise SnapValidError(
                    f"cannot read raw recovery record {recovery_path}: {error}"
                ) from error
            recovery = _object(recovery, "raw recovery")
            if recovery.get("schema") != "gd-strip-editor-raw-recovery":
                raise SnapValidError("raw recovery schema mismatch")
            if recovery.get("schema_major") != 1:
                raise SnapValidError("raw recovery major version mismatch")
            if recovery.get("capture_complete") is not False:
                raise SnapValidError(
                    "raw recovery must describe the pre-enrichment incomplete state"
                )
            if recovery.get("request_id") != request_id:
                raise SnapValidError("raw recovery request ID mismatch")
            recovery_verified = True

    game = _object(_required(root, "game", "snapshot"), "game")
    game_version = _string(_required(game, "version", "game"), "game.version")
    if game_version != EXP_GD_VER:
        raise SnapValidError(
            f"expected Geometry Dash {EXP_GD_VER}, got {game_version}"
        )
    bindings = _string(
        _required(game, "bindings_commit", "game"), "game.bindings_commit"
    )
    if bindings != BINDINGS_COMMIT:
        raise SnapValidError("snapshot was not produced by the pinned bindings commit")
    executable_verified = False
    if verify_local_files:
        executable_hash = _string(
            _required(game, "executable_sha256", "game"), "game.executable_sha256"
        )
        _validate_file_hash(
            _string(_required(game, "executable_path", "game"), "game.executable_path"),
            executable_hash,
            "Geometry Dash executable",
        )
        executable_verified = True

    level = _object(_required(root, "level", "snapshot"), "level")
    exact_level = _string(
        _required(level, "exact_level_string", "level"), "level.exact_level_string"
    )
    expected_level_hash = _string(
        _required(level, "exact_level_string_sha256", "level"),
        "level.exact_level_string_sha256",
    ).upper()
    actual_level_hash = sha256_text(exact_level)
    if actual_level_hash != expected_level_hash:
        raise SnapValidError("level string SHA-256 mismatch")
    if level.get("editor_revision_fingerprint", "").upper() != actual_level_hash:
        raise SnapValidError("editor revision fingerprint is not the saved level hash")

    if minor >= 1 and verify_local_files:
        if recovery.get("exact_level_string") != exact_level:
            raise SnapValidError("raw recovery level string mismatch")
        if str(recovery.get("exact_level_string_sha256", "")).upper() != actual_level_hash:
            raise SnapValidError("raw recovery level hash mismatch")

    settings = _string(
        _required(level, "settings_exact_save_string", "level"),
        "level.settings_exact_save_string",
    )
    if sha256_text(settings) != _string(
        _required(level, "settings_sha256", "level"), "level.settings_sha256"
    ).upper():
        raise SnapValidError("settings SHA-256 mismatch")
    if minor >= 1 and verify_local_files:
        if recovery.get("settings_exact_save_string") != settings:
            raise SnapValidError("raw recovery settings string mismatch")

    segments = split_keep_empty(exact_level, ";")
    captured_segments = _array(
        _required(level, "serialized_segments", "level"), "level.serialized_segments"
    )
    if captured_segments != segments:
        raise SnapValidError("serialized segments do not fully rebuild the level string")
    if not segments or segments[0] != settings:
        raise SnapValidError("settings string is not the saved first level segment")
    exp_obj_strings = [segment for segment in segments[1:] if segment]

    ordered = _array(_required(level, "ordered_objects", "level"), "level.ordered_objects")
    if _integer(
        _required(level, "serialized_object_count", "level"),
        "level.serialized_object_count",
        minimum=0,
    ) != len(ordered):
        raise SnapValidError("serialized object count disagrees with ordered object list")
    if len(ordered) != len(exp_obj_strings):
        raise SnapValidError("ordered object count disagrees with the saved level string")

    canonical_records: list[dict[str, Any]] = []
    for index, (raw_record, expected_exact) in enumerate(
        zip(ordered, exp_obj_strings, strict=True)
    ):
        record = _object(raw_record, f"level.ordered_objects[{index}]")
        if _integer(
            _required(record, "serialization_order_index", f"object {index}"),
            f"object {index}.serialization_order_index",
            minimum=0,
        ) != index:
            raise SnapValidError(f"object {index} has a non-canonical order index")
        expected_uid = f"object-{index:08}"
        if _string(_required(record, "sample_local_uid", f"object {index}"), "uid") != expected_uid:
            raise SnapValidError(f"object {index} has an invalid sample-local UID")
        exact = _string(
            _required(record, "exact_save_string", f"object {index}"),
            f"object {index}.exact_save_string",
        )
        if exact != expected_exact:
            raise SnapValidError(f"object {index} does not match serialization order")
        if sha256_text(exact) != _string(
            _required(record, "exact_save_string_sha256", f"object {index}"),
            f"object {index}.exact_save_string_sha256",
        ).upper():
            raise SnapValidError(f"object {index} SHA-256 mismatch")
        tokens, pairs = parse_ordered_pairs(exact, f"object {index}")
        if record.get("raw_pair_parse_complete") is not True:
            raise SnapValidError(f"object {index} raw pair parse is incomplete")
        if record.get("ordered_raw_tokens") != tokens:
            raise SnapValidError(f"object {index} raw tokens were normalized")
        if record.get("ordered_raw_pairs") != pairs:
            raise SnapValidError(f"object {index} raw pairs were normalized")
        canonical_records.append(dict(record))

    runtime = _object(
        _required(root, "runtime_observations", "snapshot"), "runtime_observations"
    )
    observations = _array(
        _required(runtime, "objects", "runtime_observations"),
        "runtime_observations.objects",
    )
    if _integer(
        _required(runtime, "object_count", "runtime_observations"),
        "runtime_observations.object_count",
        minimum=0,
    ) != len(observations):
        raise SnapValidError("runtime object count disagrees with observations")

    edit_only_obse: list[Any] = []
    if minor >= 1:
        edit_only_obse = _array(
            _required(runtime, "editor_only_objects", "runtime_observations"),
            "runtime_observations.editor_only_objects",
        )
        if _integer(
            _required(
                runtime,
                "editor_only_object_count",
                "runtime_observations",
            ),
            "runtime_observations.editor_only_object_count",
            minimum=0,
        ) != len(edit_only_obse):
            raise SnapValidError(
                "editor-only runtime object count disagrees with observations"
            )
        for index, raw_helper in enumerate(edit_only_obse):
            helper = _object(raw_helper, f"editor-only runtime object {index}")
            if _integer(
                _required(
                    helper,
                    "runtime_iteration_index",
                    f"editor-only runtime object {index}",
                ),
                f"editor-only runtime object {index}.runtime_iteration_index",
                minimum=0,
            ) != index:
                raise SnapValidError(
                    "editor-only runtime iteration indices are not contiguous"
                )
            typed = _object(
                _required(
                    helper,
                    "typed_projection_untrusted",
                    f"editor-only runtime object {index}",
                ),
                f"editor-only runtime object {index}.typed_projection_untrusted",
            )
            if typed.get("object_id") != 749:
                raise SnapValidError(
                    "only the fixture-proven Geometry Dash 2.2081 object ID 749 "
                    "may be separated as an editor-only runtime helper"
                )
            exact = _string(
                _required(
                    helper,
                    "exact_save_string",
                    f"editor-only runtime object {index}",
                ),
                f"editor-only runtime object {index}.exact_save_string",
            )
            if sha256_text(exact) != _string(
                _required(
                    helper,
                    "exact_save_string_sha256",
                    f"editor-only runtime object {index}",
                ),
                f"editor-only runtime object {index}.exact_save_string_sha256",
            ).upper():
                raise SnapValidError(
                    "editor-only runtime object SHA-256 mismatch"
                )
            if exact in exp_obj_strings:
                raise SnapValidError(
                    "object ID 749 was serialized and cannot be excluded as runtime-only"
                )
            if not _string(
                _required(
                    helper,
                    "exclusion_reason",
                    f"editor-only runtime object {index}",
                ),
                f"editor-only runtime object {index}.exclusion_reason",
            ).startswith("GD 2.2081 EditorUI::copyObjectsDetailed"):
                raise SnapValidError(
                    "editor-only helper lacks the versioned exclusion evidence"
                )

    obse_by_exac: dict[str, deque[dict[str, Any]]] = defaultdict(deque)
    runtime_strings: list[str] = []
    for index, raw_observation in enumerate(observations):
        observation = _object(raw_observation, f"runtime object {index}")
        if _integer(
            _required(observation, "runtime_iteration_index", f"runtime object {index}"),
            f"runtime object {index}.runtime_iteration_index",
            minimum=0,
        ) != index:
            raise SnapValidError("runtime iteration indices are not contiguous")
        exact = _string(
            _required(observation, "exact_save_string", f"runtime object {index}"),
            f"runtime object {index}.exact_save_string",
        )
        if sha256_text(exact) != _string(
            _required(observation, "exact_save_string_sha256", f"runtime object {index}"),
            f"runtime object {index}.exact_save_string_sha256",
        ).upper():
            raise SnapValidError(f"runtime object {index} SHA-256 mismatch")
        typed = _object(
            _required(observation, "typed_projection_untrusted", f"runtime object {index}"),
            f"runtime object {index}.typed_projection_untrusted",
        )
        runtime_strings.append(exact)
        obse_by_exac[exact].append(typed)

    if Counter(runtime_strings) != Counter(exp_obj_strings):
        missing = Counter(exp_obj_strings) - Counter(runtime_strings)
        extra = Counter(runtime_strings) - Counter(exp_obj_strings)
        raise SnapValidError(
            f"runtime enumeration is not a lossless multiset match; missing={sum(missing.values())}, "
            f"extra={sum(extra.values())}"
        )
    for record in canonical_records:
        exact = record["exact_save_string"]
        record["typed_projection_derived"] = obse_by_exac[exact].popleft()

    analysis = _object(_required(root, "analysis_status", "snapshot"), "analysis_status")
    if analysis.get("causal_partition") != "not_run":
        raise SnapValidError("first-slice capture must not claim a causal partition")
    if analysis.get("controller_dependency_mapping") != "not_run":
        raise SnapValidError("first-slice capture must not claim controller mapping")
    if analysis.get("unmapped_does_not_mean_no_dependency") is not True:
        raise SnapValidError("unmapped controller semantics must remain explicit")
    if analysis.get("generated_records_present") is not False:
        raise SnapValidError("read-only capture cannot contain generated records")

    song = _object(_required(root, "song", "snapshot"), "song")
    audio_status = _string(
        _required(song, "resolution_status", "song"), "song.resolution_status"
    )
    if audio_status not in {"resolved", "missing"}:
        raise SnapValidError("unknown song resolution status")
    audio_path: Path | None = None
    audio_verified = False
    if audio_status == "resolved":
        expected_audio_hash = _string(
            _required(song, "original_sha256", "song"), "song.original_sha256"
        )
        if verify_local_files:
            audio_path = _validate_file_hash(
                _string(
                    _required(song, "resolved_local_path", "song"),
                    "song.resolved_local_path",
                ),
                expected_audio_hash,
                "resolved song",
            )
            expected_size = _integer(
                _required(song, "original_size_bytes", "song"),
                "song.original_size_bytes",
                minimum=1,
            )
            if audio_path.stat().st_size != expected_size:
                raise SnapValidError("resolved song size mismatch")
            audio_verified = True

    canonical = json.loads(json.dumps(root))
    canonical["level"]["ordered_objects"] = canonical_records
    canonical["validation"] = {
        "validator_schema_major": SCHEMA_MAJOR,
        "exact_serialization_verified": True,
        "runtime_bijection_verified": True,
        "local_executable_verified": executable_verified,
        "local_audio_verified": audio_verified,
        "causal_partition_performed": False,
        "controller_mapping_performed": False,
    }
    report = {
        "status": "passed",
        "request_id": request_id,
        "source_path": str(source_path.resolve()) if source_path else None,
        "source_file_sha256": sha256_file(source_path) if source_path else None,
        "editor_revision_fingerprint": actual_level_hash,
        "serialized_object_count": len(canonical_records),
        "runtime_object_count": len(observations),
        "editor_only_runtime_object_count": len(edit_only_obse),
        "raw_recovery_verified": recovery_verified,
        "duplicate_exact_records": sum(
            count - 1 for count in Counter(exp_obj_strings).values() if count > 1
        ),
        "audio_status": audio_status,
        "audio_path": str(audio_path) if audio_path else song.get("resolved_local_path", ""),
        "audio_sha256": song.get("original_sha256"),
        "local_files_verified": verify_local_files,
        "read_only": True,
        "editor_mutated": False,
        "causal_partition": "not_run",
        "controller_dependency_mapping": "not_run",
    }
    return ValidatedSnapshot(canonical=canonical, report=report)


def load_snapshot(
    path: Path, *, verify_local_files: bool = True
) -> ValidatedSnapshot:
    path = path.resolve(strict=True)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SnapValidError(f"cannot read snapshot {path}: {error}") from error
    return validate_snapshot(
        _object(data, "snapshot"),
        source_path=path,
        verify_local_files=verify_local_files,
    )
