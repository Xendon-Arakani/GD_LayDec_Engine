

from __future__ import annotations

from array import array
from collections import OrderedDict
import ctypes
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import sqlite3
import struct
import subprocess

from bridge.removal_plan import classifier_hash
from bridge.snapshot import SnapValidError
from ml.effect_graph import StateAtom

@dataclass(frozen=True)
class PieceLayoutUnion:


    kept_obj_ids: frozenset[int]
    proof: dict[str, object]
    piece_summaries: tuple[dict[str, object], ...]


class _ProcMemoryCounters(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.c_ulong),
        ("page_fault_count", ctypes.c_ulong),
        ("peak_working_set_size", ctypes.c_size_t),
        ("working_set_size", ctypes.c_size_t),
        ("quota_peak_paged_pool_usage", ctypes.c_size_t),
        ("quota_paged_pool_usage", ctypes.c_size_t),
        ("quota_peak_nonpaged_pool_usage", ctypes.c_size_t),
        ("quota_nonpaged_pool_usage", ctypes.c_size_t),
        ("pagefile_usage", ctypes.c_size_t),
        ("peak_pagefile_usage", ctypes.c_size_t),
        ("private_usage", ctypes.c_size_t),
    ]


def _proc_work_bytes() -> tuple[int, int]:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    kernel32.GetCurrentProcess.restype = ctypes.c_void_p
    psapi.GetProcessMemoryInfo.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(_ProcMemoryCounters),
        ctypes.c_ulong,
    ]
    psapi.GetProcessMemoryInfo.restype = ctypes.c_int
    counters = _ProcMemoryCounters()
    counters.cb = ctypes.sizeof(counters)
    if not psapi.GetProcessMemoryInfo(
        kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb
    ):
        raise OSError("could not read the index compiler's peak working set")
    return int(counters.working_set_size), int(counters.peak_working_set_size)


def _object_index(uid: str, object_count: int) -> int:
    if not uid.startswith("object-") or not uid.removeprefix("object-").isdigit():
        return -1
    index = int(uid.removeprefix("object-"))
    return index if 0 <= index < object_count else -1


class _DiskEffectCompiler:

    _CACHE_LIMIT = 32_768
    _WRITE_CHUNK = 65_536
    _INSERT_CHUNK = 32_768

    def __init__(self, index_path: Path, object_count: int) -> None:
        self.index_path = index_path
        self.object_count = object_count
        self.database_path = index_path.with_name(index_path.name + ".build.sqlite")
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.database_path.unlink(missing_ok=True)
        self.connection = sqlite3.connect(self.database_path)
        self.connection.executescript("""
            PRAGMA journal_mode=OFF;
            PRAGMA synchronous=OFF;
            PRAGMA temp_store=FILE;
            PRAGMA cache_size=-32768;
            PRAGMA locking_mode=EXCLUSIVE;
            CREATE TABLE atoms (
                atom_id INTEGER PRIMARY KEY,
                instance_uid TEXT NOT NULL,
                property_name TEXT NOT NULL,
                object_index INTEGER NOT NULL,
                UNIQUE(instance_uid, property_name)
            );
            CREATE TABLE effects (
                effect_id INTEGER PRIMARY KEY,
                source_index INTEGER NOT NULL
            );
            CREATE TABLE inputs (
                effect_id INTEGER NOT NULL,
                ordinal INTEGER NOT NULL,
                atom_id INTEGER NOT NULL,
                PRIMARY KEY(effect_id, ordinal)
            ) WITHOUT ROWID;
            CREATE TABLE producers (
                atom_id INTEGER NOT NULL,
                effect_id INTEGER NOT NULL,
                PRIMARY KEY(atom_id, effect_id)
            ) WITHOUT ROWID;
            CREATE TABLE output_roots (
                object_index INTEGER NOT NULL,
                atom_id INTEGER NOT NULL,
                PRIMARY KEY(object_index, atom_id)
            ) WITHOUT ROWID;
        """)
        self.connection.execute("BEGIN")
        self._atom_cache: OrderedDict[StateAtom, int] = OrderedDict()
        self.atom_count = 0
        self.effect_count = 0
        self._effect_rows: list[tuple[int, int]] = []
        self._input_rows: list[tuple[int, int, int]] = []
        self._producer_rows: list[tuple[int, int]] = []
        self._output_root_rows: list[tuple[int, int]] = []
        self._roots_prepared = False
        self._layout_pieces: list[tuple[str, tuple[int, ...]]] = []

    def _flush_rows(self) -> None:
        if self._effect_rows:
            self.connection.executemany(
                "INSERT INTO effects VALUES (?, ?)", self._effect_rows
            )
            self._effect_rows.clear()
        if self._input_rows:
            self.connection.executemany(
                "INSERT INTO inputs VALUES (?, ?, ?)", self._input_rows
            )
            self._input_rows.clear()
        if self._producer_rows:
            self.connection.executemany(
                "INSERT OR IGNORE INTO producers VALUES (?, ?)", self._producer_rows
            )
            self._producer_rows.clear()
        if self._output_root_rows:
            self.connection.executemany(
                "INSERT OR IGNORE INTO output_roots VALUES (?, ?)",
                self._output_root_rows,
            )
            self._output_root_rows.clear()

    def _atom_id(self, atom: StateAtom) -> int:
        cached = self._atom_cache.get(atom)
        if cached is not None:
            self._atom_cache.move_to_end(atom)
            return cached
        row = self.connection.execute(
            "SELECT atom_id FROM atoms WHERE instance_uid=? AND property_name=?",
            (atom.instance_uid, atom.property_name),
        ).fetchone()
        if row is None:
            result = self.atom_count
            self.atom_count += 1
            self.connection.execute(
                "INSERT INTO atoms VALUES (?, ?, ?, ?)",
                (
                    result,
                    atom.instance_uid,
                    atom.property_name,
                    _object_index(atom.instance_uid, self.object_count),
                ),
            )
        else:
            result = int(row[0])
        self._atom_cache[atom] = result
        if len(self._atom_cache) > self._CACHE_LIMIT:
            self._atom_cache.popitem(last=False)
        return result

    def append(self, effect: object) -> None:
        effect_id = self.effect_count
        self.effect_count += 1
        self._effect_rows.append(
            (effect_id, _object_index(effect.source_uid, self.object_count))
        )
        for ordinal, atom in enumerate(effect.inputs):
            self._input_rows.append(
                (effect_id, ordinal, self._atom_id(atom))
            )
        for atom in effect.outputs:
            atom_id = self._atom_id(atom)
            self._producer_rows.append((atom_id, effect_id))
            object_index = _object_index(atom.instance_uid, self.object_count)
            if object_index >= 0:
                self._output_root_rows.append((object_index, atom_id))
        if (
            len(self._input_rows) >= self._INSERT_CHUNK
            or len(self._producer_rows) >= self._INSERT_CHUNK
        ):
            self._flush_rows()

    @staticmethod
    def _write_values(destination: object, typecode: str, rows: object) -> None:
        values = array(typecode)
        for row in rows:
            values.append(int(row[0]))
            if len(values) >= _DiskEffectCompiler._WRITE_CHUNK:
                values.tofile(destination)
                values = array(typecode)
        if values:
            values.tofile(destination)

    def _write_offsets(
        self, destination: object, domain_count: int, table: str, key: str
    ) -> None:
        counts = iter(self.connection.execute(
            f"SELECT {key}, COUNT(*) FROM {table} GROUP BY {key} ORDER BY {key}"
        ))
        current = next(counts, None)
        offset = 0
        values = array("Q", [0])
        for index in range(domain_count):
            if current is not None and int(current[0]) == index:
                offset += int(current[1])
                current = next(counts, None)
            values.append(offset)
            if len(values) >= self._WRITE_CHUNK:
                values.tofile(destination)
                values = array("Q")
        if current is not None:
            raise SnapValidError(f"{table} contains an out-of-range key")
        if values:
            values.tofile(destination)

    def _write_query_off(
        self, destination: object, domain_count: int, query: str
    ) -> None:
        counts = iter(self.connection.execute(query))
        current = next(counts, None)
        offset = 0
        values = array("Q", [0])
        for index in range(domain_count):
            if current is not None and int(current[0]) == index:
                offset += int(current[1])
                current = next(counts, None)
            values.append(offset)
            if len(values) >= self._WRITE_CHUNK:
                values.tofile(destination)
                values = array("Q")
        if current is not None:
            raise SnapValidError("reverse index contains an out-of-range key")
        if values:
            values.tofile(destination)

    def _pieces_for(
        self, root_atoms: frozenset[StateAtom]
    ) -> list[tuple[str, tuple[int, ...]]]:
        object_piece_atoms: dict[int, list[StateAtom]] = {}
        virtual_piece_atoms: list[StateAtom] = []
        for atom in sorted(root_atoms):
            object_index = _object_index(atom.instance_uid, self.object_count)
            if object_index < 0:
                virtual_piece_atoms.append(atom)
            else:
                object_piece_atoms.setdefault(object_index, []).append(atom)
        pieces: list[tuple[str, tuple[int, ...]]] = []
        for object_index in sorted(object_piece_atoms):
            atoms = object_piece_atoms[object_index]
            pieces.append(
                (
                    f"object-{object_index:08d}",
                    tuple(sorted({self._atom_id(atom) for atom in atoms})),
                )
            )
        for atom in virtual_piece_atoms:
            pieces.append(
                (f"{atom.instance_uid}:{atom.property_name}", (self._atom_id(atom),))
            )
        return pieces

    def _prepare_roots(self, true_layout_atoms: frozenset[StateAtom]) -> None:
        if self._roots_prepared:
            return
        self._flush_rows()
        self.connection.executescript("""
            CREATE TABLE true_objects (
                object_index INTEGER PRIMARY KEY
            );
            CREATE TABLE roots (
                object_index INTEGER NOT NULL,
                atom_id INTEGER NOT NULL,
                PRIMARY KEY(object_index, atom_id)
            ) WITHOUT ROWID;
        """)
        self._layout_pieces = self._pieces_for(true_layout_atoms)
        for piece_uid, root_ids in self._layout_pieces:
            object_index = _object_index(piece_uid, self.object_count)
            if object_index < 0:
                continue
            self.connection.execute(
                "INSERT OR IGNORE INTO true_objects VALUES (?)", (object_index,)
            )
            for atom_id in root_ids:
                self.connection.execute(
                    "INSERT OR IGNORE INTO roots VALUES (?, ?)",
                    (object_index, atom_id),
                )

        self.connection.execute("""
            INSERT OR IGNORE INTO roots
            SELECT object_index, atom_id FROM atoms
            WHERE object_index >= 0
        """)
        self.connection.commit()
        self._roots_prepared = True

    def _piece_proof(
        self,
        retained_ids: frozenset[int],
    ) -> PieceLayoutUnion:
        active_pieces = self._layout_pieces
        piece_summaries: list[dict[str, object]] = []
        manifest_digest = hashlib.sha256()
        for piece_index, (piece_uid, root_ids) in enumerate(active_pieces):
            root_digest = hashlib.sha256()
            for root_id in root_ids:
                if root_id < 0 or root_id >= self.atom_count:
                    raise SnapValidError(
                        f"layout piece references unknown atom {root_id}"
                    )
                root_digest.update(struct.pack("<I", root_id))
            summary: dict[str, object] = {
                "piece_index": piece_index,
                "root_uid": piece_uid,
                "root_atom_count": len(root_ids),
                "root_atoms_sha256": root_digest.hexdigest().upper(),
            }
            piece_summaries.append(summary)
            manifest_digest.update(json.dumps(
                summary, sort_keys=True, separators=(",", ":")
            ).encode("utf-8"))
            manifest_digest.update(b"\n")
        aggregate_digest = hashlib.sha256()
        for index in sorted(retained_ids):
            aggregate_digest.update(struct.pack("<I", index))
        proof: dict[str, object] = {
            "algorithm": "immutable_piece_roots_exact_reverse_union",
            "closure_enabled": True,
            "every_true_layout_piece_seeded": True,
            "root_identity_preserved": True,
            "monotone_reverse_effect_closure": True,
            "union_equals_independent_closures": True,
            "cross_piece_results_never_create_edges": True,
            "piece_count": len(active_pieces),
            "seeded_piece_count": len(piece_summaries),
            "retained_object_count": len(retained_ids),
            "piece_manifest_sha256": manifest_digest.hexdigest().upper(),
            "aggregate_retained_indices_sha256": aggregate_digest.hexdigest().upper(),
        }
        return PieceLayoutUnion(
            kept_obj_ids=retained_ids,
            proof=proof,
            piece_summaries=tuple(piece_summaries),
        )

    def _write_seed(
        self,
        pieces: list[tuple[str, tuple[int, ...]]],
        seed_path: Path,
    ) -> tuple[int, str]:
        seed_temp = seed_path.with_name(seed_path.name + ".tmp")
        seed_count = sum(len(root_ids) for _, root_ids in pieces)
        with seed_temp.open("wb") as destination:
            destination.write(struct.pack(
                "<8sIIIIQ", b"GDLSEED1", 1, self.object_count,
                len(pieces), 0, seed_count,
            ))
            values = array("I")
            for _piece_uid, root_ids in pieces:
                for root_id in root_ids:
                    values.append(root_id)
                    if len(values) >= self._WRITE_CHUNK:
                        values.tofile(destination)
                        values = array("I")
            if values:
                values.tofile(destination)
        seed_temp.replace(seed_path)
        digest = hashlib.sha256()
        with seed_path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        return seed_count, digest.hexdigest().upper()

    def finish(self, true_layout_atoms: frozenset[StateAtom]) -> dict[str, int | str]:
        self._prepare_roots(true_layout_atoms)
        work_before_flatten, _ = _proc_work_bytes()
        database_size_bytes = self.database_path.stat().st_size
        root_edge_count = int(
            self.connection.execute("SELECT COUNT(*) FROM roots").fetchone()[0]
        )
        producer_edge_count = int(
            self.connection.execute("SELECT COUNT(*) FROM producers").fetchone()[0]
        )
        input_edge_count = int(
            self.connection.execute("SELECT COUNT(*) FROM inputs").fetchone()[0]
        )
        source_edge_count = int(self.connection.execute(
            "SELECT COUNT(*) FROM effects WHERE source_index >= 0"
        ).fetchone()[0])

        temporary = self.index_path.with_name(self.index_path.name + ".tmp")
        with temporary.open("wb") as destination:
            destination.write(struct.pack(
                "<8sIIIIQQQQQQ", b"GDHSIDX2", 2, self.object_count,
                self.atom_count, self.effect_count, root_edge_count,
                producer_edge_count, input_edge_count, input_edge_count,
                producer_edge_count, source_edge_count,
            ))
            self._write_offsets(destination, self.object_count, "roots", "object_index")
            self._write_values(destination, "I", self.connection.execute(
                "SELECT atom_id FROM roots ORDER BY object_index, atom_id"
            ))
            self._write_values(destination, "i", self.connection.execute(
                "SELECT object_index FROM atoms ORDER BY atom_id"
            ))
            self._write_offsets(destination, self.atom_count, "producers", "atom_id")
            self._write_values(destination, "I", self.connection.execute(
                "SELECT effect_id FROM producers ORDER BY atom_id, effect_id"
            ))
            self._write_values(destination, "i", self.connection.execute(
                "SELECT source_index FROM effects ORDER BY effect_id"
            ))
            self._write_offsets(destination, self.effect_count, "inputs", "effect_id")
            self._write_values(destination, "I", self.connection.execute(
                "SELECT atom_id FROM inputs ORDER BY effect_id, ordinal"
            ))
            self._write_query_off(
                destination, self.atom_count,
                "SELECT atom_id, COUNT(*) FROM inputs GROUP BY atom_id ORDER BY atom_id",
            )
            self._write_values(destination, "I", self.connection.execute(
                "SELECT effect_id FROM inputs ORDER BY atom_id, effect_id, ordinal"
            ))
            self._write_query_off(
                destination, self.effect_count,
                "SELECT effect_id, COUNT(*) FROM producers GROUP BY effect_id ORDER BY effect_id",
            )
            self._write_values(destination, "I", self.connection.execute(
                "SELECT atom_id FROM producers ORDER BY effect_id, atom_id"
            ))
            self._write_query_off(
                destination, self.object_count,
                "SELECT source_index, COUNT(*) FROM effects WHERE source_index >= 0 GROUP BY source_index ORDER BY source_index",
            )
            self._write_values(destination, "I", self.connection.execute(
                "SELECT effect_id FROM effects WHERE source_index >= 0 ORDER BY source_index, effect_id"
            ))
        temporary.replace(self.index_path)
        seed_path = self.index_path.with_name(self.index_path.name + ".seeds")
        seed_count, seed_hash = self._write_seed(
            self._layout_pieces, seed_path
        )
        digest = hashlib.sha256()
        with self.index_path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        work_after_flatten, peak_working_set = _proc_work_bytes()
        details: dict[str, int | str] = {
            "native_index_path": str(self.index_path.resolve()),
            "native_index_size_bytes": self.index_path.stat().st_size,
            "native_index_sha256": digest.hexdigest().upper(),
            "native_seed_path": str(seed_path.resolve()),
            "native_seed_sha256": seed_hash,
            "seed_atom_count": seed_count,
            "layout_piece_count": len(self._layout_pieces),
            "atom_count": self.atom_count,
            "effect_count": self.effect_count,
            "root_edge_count": root_edge_count,
            "producer_edge_count": producer_edge_count,
            "input_edge_count": input_edge_count,
            "compiler_cache_limit_atoms": self._CACHE_LIMIT,
            "expanded_effects_materialized_in_python": 0,
            "compiler_database_size_bytes": database_size_bytes,
            "working_set_before_flatten_bytes": work_before_flatten,
            "working_set_after_flatten_bytes": work_after_flatten,
            "compiler_peak_working_set_bytes": peak_working_set,
        }
        return details

    def native_union(
        self,
        true_layout_atoms: frozenset[StateAtom],
        worker_path: Path,
    ) -> tuple[PieceLayoutUnion, dict[str, object]]:
        index_details = self.finish(true_layout_atoms)
        pieces = self._layout_pieces
        seed_path = Path(str(index_details["native_seed_path"]))
        seed_count = int(index_details["seed_atom_count"])
        kept_path = self.index_path.with_name(
            self.index_path.name + ".kept"
        )
        report_path = self.index_path.with_name(
            self.index_path.name + ".close.json"
        )
        native_run = subprocess.run(
            [
                str(worker_path), "close", str(self.index_path), str(seed_path),
                str(kept_path), str(report_path),
            ],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if not report_path.is_file():
            raise SnapValidError("native closure did not publish a result")
        report = json.loads(report_path.read_text(encoding="utf-8"))
        if native_run.returncode != 0:
            raise SnapValidError(
                f"native closure exited with code {native_run.returncode}: "
                f"{report.get('error', 'no error was recorded')}"
            )
        if (
            report.get("schema") != "gd-strip-native-close"
            or report.get("schema_major") != 1
            or report.get("success") is not True
            or str(report.get("classifier_sha256", "")).upper()
            != classifier_hash()
            or int(report.get("object_count", -1)) != self.object_count
            or int(report.get("piece_count", -1)) != len(pieces)
            or int(report.get("seed_atom_count", -1))
            != seed_count
        ):
            raise SnapValidError("native closure result does not match this analysis")
        raw = kept_path.read_bytes()
        if len(raw) < 24:
            raise SnapValidError("native closure output is truncated")
        magic, version, object_count, kept_count = struct.unpack_from(
            "<8sIIQ", raw, 0
        )
        if (
            magic != b"GDLCLOSE"
            or version != 1
            or object_count != self.object_count
            or kept_count > self.object_count
            or len(raw) != 24 + kept_count * 4
        ):
            raise SnapValidError("native closure output header is invalid")
        kept_values = struct.unpack_from(f"<{kept_count}I", raw, 24)
        if (
            any(value >= self.object_count for value in kept_values)
            or any(left >= right for left, right in zip(kept_values, kept_values[1:]))
        ):
            raise SnapValidError("native closure output indices are invalid")
        file_hash = hashlib.sha256(raw).hexdigest().upper()
        index_digest = hashlib.sha256()
        for value in kept_values:
            index_digest.update(struct.pack("<I", value))
        index_hash = index_digest.hexdigest().upper()
        if (
            file_hash != str(report.get("kept_file_sha256", "")).upper()
            or index_hash != str(report.get("kept_index_sha256", "")).upper()
            or kept_count != int(report.get("retained_object_count", -1))
        ):
            raise SnapValidError("native closure output digest is invalid")
        retained_ids = frozenset(kept_values)
        aggregation = self._piece_proof(retained_ids)
        return aggregation, {
            **index_details,
            **report,
        }

    def close(self) -> None:
        self.connection.close()
        self.database_path.unlink(missing_ok=True)
