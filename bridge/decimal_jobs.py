

from __future__ import annotations

from dataclasses import dataclass
import ctypes
import hashlib
import json
import os
from pathlib import Path
import time
from typing import Any

from bridge.snapshot import SnapValidError


SCHEMA = "gd-strip-decimal-job"
STATUS_SCHEMA = "gd-strip-decimal-job-status"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest().upper()


def atomic_write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(value, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    temporary.replace(path)


@dataclass(frozen=True)
class DecimalJob:
    manifest: dict[str, Any]
    manifest_path: Path
    raw_path: Path
    status_path: Path
    latest_status_path: Path
    native_worker_path: Path
    native_index_path: Path
    native_report_path: Path
    level_string: str

    @property
    def operation_id(self) -> str:
        return str(self.manifest["operation_id"])

    @property
    def level_name(self) -> str:
        return str(self.manifest["level_name"])

    @property
    def source_hash(self) -> str:
        return str(self.manifest["source_revision_sha256"])

def load_decimal_job(path: Path) -> DecimalJob:
    manifest_path = path.resolve(strict=True)
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise SnapValidError(f"cannot read Decimal manifest: {error}") from error
    if not isinstance(manifest, dict) or manifest.get("schema") != SCHEMA:
        raise SnapValidError("unsupported Decimal manifest schema")
    if manifest.get("schema_major") != 1:
        raise SnapValidError("unsupported Decimal manifest major version")
    operation_id = manifest.get("operation_id")
    if not isinstance(operation_id, str) or not operation_id:
        raise SnapValidError("Decimal operation ID is missing")
    if any(character not in "0123456789-abcdefABCDEF" for character in operation_id):
        raise SnapValidError("Decimal operation ID has an invalid format")
    if manifest.get("game_version") != "2.2081":
        raise SnapValidError("Decimal job is not from Geometry Dash 2.2081")
    if manifest.get("bindings_commit") != (
        "7f6c2a75742856de88dad354e576dcff8a28e881"
    ):
        raise SnapValidError("Decimal job is not from the pinned bindings")
    level_name = manifest.get("level_name")
    if not isinstance(level_name, str) or not level_name:
        raise SnapValidError("Decimal level name is missing")
    expected_hash = manifest.get("source_revision_sha256")
    if not isinstance(expected_hash, str) or len(expected_hash) != 64:
        raise SnapValidError("Decimal source SHA-256 is invalid")

    root = manifest_path.parent.parent
    expected_raw = (root / "raw" / f"{operation_id}.level").resolve()
    expected_status = (root / "statuses" / f"{operation_id}.json").resolve()
    expected_worker = (root.parent / "native" / "gd-layout-worker.exe").resolve()
    expected_index = (root / "native-indices" / f"{operation_id}.bin").resolve()
    expected_report = (root / "native-statuses" / f"{operation_id}.json").resolve()
    raw_path = Path(str(manifest.get("raw_level_path", ""))).resolve(strict=True)
    status_path = Path(str(manifest.get("status_path", ""))).resolve()
    native_worker_path = Path(
        str(manifest.get("native_worker_path", ""))
    ).resolve(strict=True)
    native_index_path = Path(
        str(manifest.get("native_index_path", ""))
    ).resolve()
    native_report_path = Path(
        str(manifest.get("native_report_path", ""))
    ).resolve()
    if raw_path != expected_raw:
        raise SnapValidError("Decimal raw level path escapes its operation slot")
    if status_path != expected_status:
        raise SnapValidError("Decimal status path escapes its operation slot")
    if native_worker_path != expected_worker:
        raise SnapValidError("Decimal native worker path escapes its fixed slot")
    if native_index_path != expected_index:
        raise SnapValidError("Decimal native index path escapes its operation slot")
    if native_report_path != expected_report:
        raise SnapValidError("Decimal native report path escapes its operation slot")
    raw = raw_path.read_bytes()
    if len(raw) != manifest.get("source_size_bytes"):
        raise SnapValidError("Decimal raw level byte count mismatch")
    if _sha256(raw) != expected_hash.upper():
        raise SnapValidError("Decimal raw level SHA-256 mismatch")
    try:
        level_string = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise SnapValidError("Decimal level data is not UTF-8") from error
    if ";" not in level_string:
        raise SnapValidError("Decimal input is not a full plain editor level string")
    return DecimalJob(
        manifest=manifest,
        manifest_path=manifest_path,
        raw_path=raw_path,
        status_path=status_path,
        latest_status_path=(root / "status-latest.json").resolve(),
        native_worker_path=native_worker_path,
        native_index_path=native_index_path,
        native_report_path=native_report_path,
        level_string=level_string,
    )


def write_decimal_status(
    job: DecimalJob,
    *,
    state: str,
    terminal: bool,
    success: bool | None,
    message: str,
    details: dict[str, object] | None = None,
) -> dict[str, object]:
    value: dict[str, object] = {
        "schema": STATUS_SCHEMA,
        "schema_major": 1,
        "operation_id": job.operation_id,
        "level_name": job.level_name,
        "source_revision_sha256": job.source_hash,
        "state": state,
        "terminal": terminal,
        "success": success,
        "message": message,
        "updated_unix_ms": time.time_ns() // 1_000_000,
        "details": details or {},
    }
    atomic_write_json(job.status_path, value)
    atomic_write_json(job.latest_status_path, value)
    event_name = job.manifest.get("phase_event_name")
    if os.name == "nt" and isinstance(event_name, str) and event_name:
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenEventW.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_wchar_p]
        kernel.OpenEventW.restype = ctypes.c_void_p
        kernel.SetEvent.argtypes = [ctypes.c_void_p]
        kernel.SetEvent.restype = ctypes.c_int
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel.CloseHandle.restype = ctypes.c_int
        handle = kernel.OpenEventW(0x0002, 0, event_name)
        if handle:
            kernel.SetEvent(handle)
            kernel.CloseHandle(handle)
    return value
