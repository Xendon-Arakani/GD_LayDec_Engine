from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import time
from typing import Callable

from bridge.level_effects import LevelEffectAnalysis, analyze_level_fx
from bridge.snapshot import SnapValidError, ValidatedSnapshot
from scripts.run_highlight import PieceLayoutUnion, _DiskEffectCompiler, _proc_work_bytes


@dataclass(frozen=True)
class CompiledLayoutPart:
    analysis: LevelEffectAnalysis
    aggregation: PieceLayoutUnion
    expanded_fx_count: int
    effect_expansion_ms: float
    solo_closure_ms: float
    peak_proc_work_bytes: int
    native_details: dict[str, object]


def compile_layout(
    snapshot: ValidatedSnapshot,
    catalogue: dict[str, object],
    capabilities: dict[str, object],
    index_path: Path,
    *,
    native_worker: Path | None = None,
    capture_diag: bool = False,
    expansion_complete: Callable[[LevelEffectAnalysis, int], None] | None = None,
) -> CompiledLayoutPart:
    compiler = _DiskEffectCompiler(
        index_path, len(snapshot.canonical["level"]["ordered_objects"])
    )
    try:
        started = time.perf_counter()
        analysis = analyze_level_fx(
            snapshot, catalogue, capabilities, capture_fx=False,
            effect_sink=compiler.append, compute_mech_closure=False,
            capture_diag=capture_diag,
        )
        expansion_ms = round((time.perf_counter() - started) * 1000, 3)
        if analysis.unsupported_uses:
            raise SnapValidError("this revision contains unsupported concrete effect semantics")
        if expansion_complete is not None:
            expansion_complete(analysis, compiler.effect_count)
        worker = native_worker or (
            Path(os.environ["LOCALAPPDATA"]) / "GeometryDash" / "geode" / "mods"
            / "sebastian-anderson.gd-layout-stripper" / "native" / "gd-layout-worker.exe"
        )
        aggregation, details = compiler.native_union(analysis.true_layout_atoms, worker)
        _, peak = _proc_work_bytes()
        return CompiledLayoutPart(
            analysis, aggregation, compiler.effect_count, expansion_ms,
            round(float(details["query_ms"]), 3), peak, details,
        )
    finally:
        compiler.close()
