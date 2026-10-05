from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import mmap
from pathlib import Path
import struct

from bridge.level_effects import FX_SEM_PROP_KEYS, FX_SEM_REV
from bridge.local_levels import SavedLocalLevel
from bridge.snapshot import SnapValidError, ValidatedSnapshot


HEAD = struct.Struct("<8sIIQQQQQ32s32s")
OBJ = struct.Struct("<IIIHH")
PROP = struct.Struct("<IIHH")
ROOT = struct.Struct("<I")


@dataclass(frozen=True)
class NativeProjection:
    snapshot: ValidatedSnapshot
    roots: tuple[int, ...]
    report: dict[str, object]


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def load_native_report(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise SnapValidError(f"cannot read native projection report: {error}") from error
    if not isinstance(value, dict):
        raise SnapValidError("native projection report is not an object")
    if value.get("schema") != "gd-strip-native-stage-2":
        raise SnapValidError("unsupported native projection report schema")
    if value.get("schema_major") != 1:
        raise SnapValidError("unsupported native projection report version")
    if value.get("success") is not True:
        raise SnapValidError(str(value.get("error", "native projection failed")))
    return value


def make_native_snapshot(
    level: SavedLocalLevel,
    level_string: str,
    index_path: Path,
    report: dict[str, object],
    class_hash: str,
) -> NativeProjection:
    raw = level_string.encode("utf-8")
    source_hash = hashlib.sha256(raw).hexdigest().upper()
    if str(report.get("source_sha256", "")).upper() != source_hash:
        raise SnapValidError("native projection source hash mismatch")
    if str(report.get("classifier_sha256", "")).upper() != class_hash.upper():
        raise SnapValidError("native projection classifier mismatch")
    if index_path.stat().st_size != report.get("index_size_bytes"):
        raise SnapValidError("native projection byte count mismatch")
    if _hash(index_path) != str(report.get("index_sha256", "")).upper():
        raise SnapValidError("native projection index hash mismatch")
    with index_path.open("rb") as source:
        with mmap.mmap(source.fileno(), 0, access=mmap.ACCESS_READ) as view:
            if len(view) < HEAD.size:
                raise SnapValidError("native projection header is truncated")
            (
                magic,
                version,
                sem_rev,
                settings_len,
                object_count,
                prop_count,
                root_count,
                source_size,
                stored_source,
                stored_class,
            ) = HEAD.unpack_from(view)
            if magic != b"GDLIDX02" or version != 2:
                raise SnapValidError("unsupported native projection index")
            if sem_rev != FX_SEM_REV:
                raise SnapValidError("native projection semantics are stale")
            if source_size != len(raw) or stored_source.hex().upper() != source_hash:
                raise SnapValidError("native projection input identity mismatch")
            if stored_class.hex().upper() != class_hash.upper():
                raise SnapValidError("native projection class identity mismatch")
            expected_size = (
                HEAD.size + object_count * OBJ.size + prop_count * PROP.size
                + root_count * ROOT.size
            )
            if expected_size != len(view):
                raise SnapValidError("native projection table size mismatch")
            if settings_len <= 0 or settings_len >= len(raw) or raw[settings_len] != 59:
                raise SnapValidError("native projection settings boundary is invalid")
            obj_base = HEAD.size
            prop_base = obj_base + object_count * OBJ.size
            root_base = prop_base + prop_count * PROP.size
            ordered: list[dict[str, object]] = []
            next_off = settings_len + 1
            next_prop = 0
            for index in range(object_count):
                off, length, prop_start, obj_props, object_id = OBJ.unpack_from(
                    view, obj_base + index * OBJ.size
                )
                if off != next_off or length == 0 or off + length > len(raw):
                    raise SnapValidError("native projection object boundary is invalid")
                if prop_start != next_prop or prop_start + obj_props > prop_count:
                    raise SnapValidError("native projection property span is invalid")
                end = off + length
                if end < len(raw) and raw[end] != 59:
                    raise SnapValidError("native projection object separator is invalid")
                if object_id <= 0:
                    raise SnapValidError("native projection object ID is invalid")
                properties: dict[str, tuple[str, ...]] = {}
                for prop_index in range(prop_start, prop_start + obj_props):
                    val_off, val_len, key, reserved = PROP.unpack_from(
                        view, prop_base + prop_index * PROP.size
                    )
                    if reserved != 0 or str(key) not in FX_SEM_PROP_KEYS:
                        raise SnapValidError("native projection property identity is invalid")
                    if val_off < off or val_off + val_len > end:
                        raise SnapValidError("native projection property boundary is invalid")
                    key_text = str(key)
                    if key_text in properties:
                        raise SnapValidError("native projection repeats an effective property")
                    try:
                        value = raw[val_off:val_off + val_len].decode("utf-8")
                    except UnicodeDecodeError as error:
                        raise SnapValidError("native projection property is not UTF-8") from error
                    properties[key_text] = (value,)
                try:
                    record_text = raw[off:end].decode("utf-8")
                except UnicodeDecodeError as error:
                    raise SnapValidError("native projection object is not UTF-8") from error
                ordered.append(
                    {
                        "sample_local_uid": f"object-{index:08d}",
                        "serialization_order_index": index,
                        "typed_projection_derived": {"object_id": object_id},
                        "engine_effective_properties": properties,
                        "exact_save_string": record_text,
                    }
                )
                next_prop += obj_props
                next_off = end + 1
            if next_prop != prop_count or next_off < len(raw):
                raise SnapValidError("native projection does not cover the level")
            roots = tuple(
                ROOT.unpack_from(view, root_base + index * ROOT.size)[0]
                for index in range(root_count)
            )
            if tuple(sorted(set(roots))) != roots:
                raise SnapValidError("native projection roots are not strictly ordered")
            if roots and roots[-1] >= object_count:
                raise SnapValidError("native projection root is out of range")
    root_digest = hashlib.sha256()
    for index in roots:
        root_digest.update(ROOT.pack(index))
    if root_digest.hexdigest().upper() != str(report.get("root_index_sha256", "")).upper():
        raise SnapValidError("native projection root hash mismatch")
    if report.get("object_count") != object_count:
        raise SnapValidError("native projection object count mismatch")
    if report.get("property_count") != prop_count:
        raise SnapValidError("native projection property count mismatch")
    if report.get("true_layout_count") != root_count:
        raise SnapValidError("native projection root count mismatch")
    try:
        settings = raw[:settings_len].decode("utf-8")
    except UnicodeDecodeError as error:
        raise SnapValidError("native projection settings are not UTF-8") from error
    snapshot = ValidatedSnapshot(
        canonical={
            "game": {
                "version": "2.2081",
                "bindings_commit": "7f6c2a75742856de88dad354e576dcff8a28e881",
            },
            "capture": {
                "bridge_version": "0.14.12",
                "source": "native_projection",
                "classifier_sha256": class_hash,
            },
            "source": {
                "level_name": level.name,
                "creator_name": level.creator,
                "online_level_id": level.online_id,
                "audit_source": "native_projection",
            },
            "level": {
                "exact_level_string": level_string,
                "exact_level_string_sha256": source_hash,
                "settings_exact_save_string": settings,
                "ordered_objects": ordered,
                "serialized_object_count": object_count,
                "serialized_record_count": object_count,
                "compact_effect_projection": True,
                "native_effect_projection": True,
                "native_index_sha256": str(report["index_sha256"]).upper(),
            },
        },
        report={
            "effect_audit_only": True,
            "compact_effect_projection": True,
            "native_effect_projection": True,
            "serialized_record_count": object_count,
        },
    )
    return NativeProjection(snapshot=snapshot, roots=roots, report=report)
