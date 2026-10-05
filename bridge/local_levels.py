





from __future__ import annotations

from dataclasses import dataclass
import base64
import gzip
import hashlib
from pathlib import Path
import re
import xml.etree.ElementTree as ET

from bridge.snapshot import SnapValidError, ValidatedSnapshot


_GD_PROP_SLOT_MAX = 600
_ATOI_PREFIX = re.compile(r"^[\t\n\v\f\r ]*([+-]?[0-9]+)")
_GAME_VERSION = "2.2081"
_BINDINGS_COMMIT = "7f6c2a75742856de88dad354e576dcff8a28e881"
_BRIDGE_VERSION = "0.14.12"


@dataclass(frozen=True)
class SavedLocalLevel:
    registry_index: int
    name: str
    creator: str
    online_id: int | None
    encoded_level_data: str


def _urlsafe_decode(value: bytes) -> bytes:
    return base64.urlsafe_b64decode(value + b"=" * (-len(value) % 4))


def _dict_values(element: ET.Element) -> dict[str, ET.Element]:
    if element.tag not in {"d", "dict"}:
        raise SnapValidError(
            f"expected Geometry Dash dictionary node, found {element.tag!r}"
        )
    children = list(element)
    if len(children) % 2:
        raise SnapValidError("Geometry Dash dictionary has an odd child count")
    values: dict[str, ET.Element] = {}
    for index in range(0, len(children), 2):
        key = children[index]
        value = children[index + 1]
        if key.tag != "k" or key.text is None:
            raise SnapValidError("Geometry Dash dictionary has a non-key entry")
        if key.text in values:
            raise SnapValidError(
                f"Geometry Dash dictionary repeats key {key.text!r}"
            )
        values[key.text] = value
    return values


def _text(element: ET.Element | None, *, default: str = "") -> str:
    if element is None:
        return default
    return element.text or default


def load_local_levels(path: Path) -> tuple[SavedLocalLevel, ...]:


    encoded = path.read_bytes()
    try:
        xml_bytes = gzip.decompress(
            _urlsafe_decode(bytes(value ^ 11 for value in encoded))
        )
        root = ET.fromstring(xml_bytes)
    except (ValueError, gzip.BadGzipFile, ET.ParseError) as error:
        raise SnapValidError(
            f"cannot decode Geometry Dash local-level database: {error}"
        ) from error

    top = root.find("dict")
    if top is None:
        top = root.find("d")
    if top is None:
        raise SnapValidError("local-level database lacks its root dictionary")
    local_array = _dict_values(top).get("LLM_01")
    if local_array is None:
        raise SnapValidError("local-level database lacks LLM_01")

    levels: list[SavedLocalLevel] = []
    for key, value in _dict_values(local_array).items():
        if not key.startswith("k_") or value.tag != "d":
            continue
        try:
            registry_index = int(key[2:])
        except ValueError as error:
            raise SnapValidError(
                f"invalid local-level registry key {key!r}"
            ) from error
        fields = _dict_values(value)
        name = _text(fields.get("k2"))
        encoded_level_data = _text(fields.get("k4"))
        if not name or not encoded_level_data:
            raise SnapValidError(
                f"local level {registry_index} lacks name or level data"
            )
        online_text = _text(fields.get("k42"))
        levels.append(
            SavedLocalLevel(
                registry_index=registry_index,
                name=name,
                creator=_text(fields.get("k5")),
                online_id=int(online_text) if online_text else None,
                encoded_level_data=encoded_level_data,
            )
        )
    return tuple(sorted(levels, key=lambda level: level.registry_index))


def find_local_level(
    levels: tuple[SavedLocalLevel, ...], exact_name: str
) -> SavedLocalLevel:
    matches = tuple(level for level in levels if level.name == exact_name)
    if not matches:
        raise SnapValidError(f"no saved local level named {exact_name!r}")
    if len(matches) != 1:
        indices = ", ".join(str(level.registry_index) for level in matches)
        raise SnapValidError(
            f"saved local level name {exact_name!r} is ambiguous at indices {indices}"
        )
    return matches[0]


def decode_saved_level(level: SavedLocalLevel) -> str:
    return decode_level_text(level.encoded_level_data, level_name=level.name)


def decode_level_text(encoded_level_data: str, *, level_name: str) -> str:


    try:
        return gzip.decompress(
            _urlsafe_decode(encoded_level_data.encode("ascii"))
        ).decode("utf-8")
    except (UnicodeError, ValueError, gzip.BadGzipFile) as error:
        raise SnapValidError(
            f"cannot decode level data for {level_name!r}: {error}"
        ) from error


def _gd_atoi(value: str) -> int:


    match = _ATOI_PREFIX.match(value)
    return int(match.group(1)) if match else 0


def _parse_engine_record(exact: str, *, path: str) -> dict[str, object]:










    tokens = exact.split(",")
    raw_pairs = [
        {"key_text": tokens[index], "value_text": tokens[index + 1]}
        for index in range(0, len(tokens) - 1, 2)
    ]
    occurrences: dict[int, list[dict[str, str]]] = {}
    effective: dict[int, str] = {}
    for pair in raw_pairs:
        key = _gd_atoi(pair["key_text"])
        if key <= 0:
            continue
        if key > _GD_PROP_SLOT_MAX:
            raise SnapValidError(
                f"{path} addresses property slot {key} beyond the pinned "
                f"GD 2.2081 loader table ({_GD_PROP_SLOT_MAX})"
            )
        occurrences.setdefault(key, []).append(pair)
        effective[key] = pair["value_text"]

    object_id = _gd_atoi(effective.get(1, ""))
    duplicate_properties = [
        {
            "numeric_key": key,
            "occurrence_count": len(pairs),
            "value_texts": [pair["value_text"] for pair in pairs],
            "effective_value_text": pairs[-1]["value_text"],
            "values_identical": len({pair["value_text"] for pair in pairs}) == 1,
        }
        for key, pairs in sorted(occurrences.items())
        if len(pairs) > 1
    ]
    return {
        "tokens": tokens,
        "raw_pairs": raw_pairs,
        "raw_pair_parse_complete": len(tokens) % 2 == 0,
        "dangling_key_text": tokens[-1] if len(tokens) % 2 else None,
        "effective_pairs": [
            {"key_text": str(key), "value_text": value}
            for key, value in sorted(effective.items())
        ],
        "object_id": object_id if object_id > 0 else None,
        "duplicate_properties": duplicate_properties,
    }


def make_full_snapshot(
    level: SavedLocalLevel, exact_level_string: str
) -> ValidatedSnapshot:


    segments = exact_level_string.split(";")
    if not segments or not segments[0]:
        raise SnapValidError(f"level {level.name!r} lacks settings data")
    ordered_objects = []
    eng_ignored_records = []
    same_dup_props = []
    conflict_dup_props = []
    dangling_key_records = []
    saved_track_count = 0
    for segment_index, segment in enumerate(segments[1:], start=1):
        if not segment:
            continue
        saved_track_id = saved_track_count
        saved_track_count += 1
        parsed = _parse_engine_record(
            segment,
            path=(
                f"level {level.name!r} serialized record "
                f"{saved_track_id}"
            ),
        )
        object_id = parsed["object_id"]
        if parsed["dangling_key_text"] is not None:
            dangling_key_records.append(
                {
                    "serialized_record_index": saved_track_id,
                    "serialized_segment_index": segment_index,
                    "exact_segment": segment,
                    "dangling_key_text": parsed["dangling_key_text"],
                }
            )
        if object_id is None:
            eng_ignored_records.append(
                {
                    "serialized_record_index": saved_track_id,
                    "serialized_segment_index": segment_index,
                    "exact_segment": segment,
                    "reason": "effective_object_id_absent_or_non_positive",
                    "ordered_raw_tokens": parsed["tokens"],
                    "ordered_raw_pairs": parsed["raw_pairs"],
                    "dangling_key_text": parsed["dangling_key_text"],
                }
            )
            continue
        serialization_index = len(ordered_objects)
        for duplicate in parsed["duplicate_properties"]:
            row = {
                "serialization_order_index": serialization_index,
                "serialized_record_index": saved_track_id,
                "serialized_segment_index": segment_index,
                "object_id": object_id,
                "key_text": str(duplicate["numeric_key"]),
                "value_text": duplicate["effective_value_text"],
                "value_texts": duplicate["value_texts"],
                "occurrence_count": duplicate["occurrence_count"],
                "effective_rule": "last_value_wins",
            }
            if duplicate["values_identical"]:
                same_dup_props.append(row)
            else:
                conflict_dup_props.append(row)
        ordered_objects.append(
            {
                "sample_local_uid": f"object-{serialization_index:08d}",
                "serialization_order_index": serialization_index,
                "serialized_record_index": saved_track_id,
                "serialized_segment_index": segment_index,
                "exact_save_string": segment,
                "ordered_raw_tokens": parsed["tokens"],
                "raw_pair_parse_complete": parsed["raw_pair_parse_complete"],
                "ordered_raw_pairs": parsed["raw_pairs"],
                "engine_effective_pairs": parsed["effective_pairs"],
                "typed_projection_derived": {"object_id": object_id},
            }
        )







    editor_level_string = (
        segments[0]
        + ";"
        + "".join(
            f"{record['exact_save_string']};" for record in ordered_objects
        )
    )
    level_hash = hashlib.sha256(
        editor_level_string.encode("utf-8")
    ).hexdigest().upper()
    persisted_hash = hashlib.sha256(
        exact_level_string.encode("utf-8")
    ).hexdigest().upper()
    return ValidatedSnapshot(
        canonical={
            "game": {
                "version": _GAME_VERSION,
                "bindings_commit": _BINDINGS_COMMIT,
            },
            "capture": {
                "bridge_version": _BRIDGE_VERSION,
                "source": "persisted_local_level_database",
            },
            "source": {
                "level_name": level.name,
                "creator_name": level.creator,
                "online_level_id": level.online_id,
                "audit_source": "persisted_local_level_database",
            },
            "level": {
                "exact_level_string": editor_level_string,
                "exact_level_string_sha256": level_hash,
                "persisted_download_level_string_sha256": persisted_hash,
                "editor_projection_removed_only_engine_ignored_records": True,
                "settings_exact_save_string": segments[0],
                "ordered_objects": ordered_objects,
                "serialized_object_count": len(ordered_objects),
                "serialized_record_count": saved_track_count,
                "engine_ignored_records": eng_ignored_records,
                "engine_ignored_record_count": len(eng_ignored_records),
                "dangling_key_records": dangling_key_records,
                "dangling_key_record_count": len(dangling_key_records),
                "identical_duplicate_properties": same_dup_props,
                "identical_duplicate_property_count": len(
                    same_dup_props
                ),
                "conflicting_duplicate_properties": conflict_dup_props,
                "conflicting_duplicate_property_count": len(
                    conflict_dup_props
                ),
            },
        },
        report={
            "effect_audit_only": True,
            "serialized_record_count": saved_track_count,
            "engine_ignored_record_count": len(eng_ignored_records),
            "dangling_key_record_count": len(dangling_key_records),
            "identical_duplicate_property_count": len(
                same_dup_props
            ),
            "conflicting_duplicate_property_count": len(
                conflict_dup_props
            ),
        },
    )


def make_effect_snapshot(
    level: SavedLocalLevel,
    exact_level_string: str,
    *,
    sem_prop_keys: frozenset[str],
    with_exact_records: bool = False,
) -> ValidatedSnapshot:









    settings, separator, object_text = exact_level_string.partition(";")
    if not separator or not settings:
        raise SnapValidError(f"level {level.name!r} lacks settings data")
    ordered_objects: list[dict[str, object]] = []
    saved_track_count = 0
    for segment in object_text.split(";"):
        if not segment:
            continue
        saved_track_id = saved_track_count
        saved_track_count += 1
        tokens = segment.split(",")
        effective: dict[str, tuple[str, ...]] = {}
        object_id = 0
        for index in range(0, len(tokens) - 1, 2):
            numeric_key = _gd_atoi(tokens[index])
            if numeric_key <= 0:
                continue
            if numeric_key > _GD_PROP_SLOT_MAX:
                raise SnapValidError(
                    f"level {level.name!r} serialized record "
                    f"{saved_track_id} addresses property slot "
                    f"{numeric_key} beyond the pinned GD 2.2081 loader table "
                    f"({_GD_PROP_SLOT_MAX})"
                )
            value = tokens[index + 1]
            if numeric_key == 1:
                object_id = _gd_atoi(value)
            key = str(numeric_key)
            if key in sem_prop_keys:
                effective[key] = (value,)
        if object_id <= 0:
            raise SnapValidError(
                f"live editor record {saved_track_id} has no effective object ID"
            )
        serialization_index = len(ordered_objects)
        record: dict[str, object] = {
            "sample_local_uid": f"object-{serialization_index:08d}",
            "serialization_order_index": serialization_index,
            "typed_projection_derived": {"object_id": object_id},
            "engine_effective_properties": effective,
        }
        if with_exact_records:



            record["exact_save_string"] = segment
        ordered_objects.append(record)
    level_hash = hashlib.sha256(exact_level_string.encode("utf-8")).hexdigest().upper()
    return ValidatedSnapshot(
        canonical={
            "game": {"version": _GAME_VERSION, "bindings_commit": _BINDINGS_COMMIT},
            "capture": {"bridge_version": _BRIDGE_VERSION, "source": "live_editor"},
            "source": {
                "level_name": level.name,
                "creator_name": level.creator,
                "online_level_id": level.online_id,
                "audit_source": "live_editor_compact_effect_projection",
            },
              "level": {
                  **(
                      {"exact_level_string": exact_level_string}
                      if with_exact_records else {}
                  ),
                  "exact_level_string_sha256": level_hash,
                "settings_exact_save_string": settings,
                "ordered_objects": ordered_objects,
                "serialized_object_count": len(ordered_objects),
                "serialized_record_count": saved_track_count,
                "compact_effect_projection": True,
            },
        },
        report={
            "effect_audit_only": True,
            "compact_effect_projection": True,
            "serialized_record_count": saved_track_count,
        },
    )
