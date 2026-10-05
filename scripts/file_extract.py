from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


ROOT = Path(sys._MEIPASS) if getattr(sys, "frozen", False) else Path(__file__).resolve().parents[1]


def compile_file(source: Path, folder: Path) -> tuple:
    from bridge.catalogue import load_catalogue
    from bridge.local_levels import SavedLocalLevel
    from bridge.native_index import load_native_report, make_native_snapshot
    from bridge.removal_plan import classifier_hash
    from scripts.compile_layout import compile_layout

    raw = source.read_text(encoding="utf-8")
    worker = ROOT / "native/gd-layout-worker.exe"
    if not worker.is_file():
        worker = ROOT / "native/build/gd-layout-worker.exe"
    index, report = folder / "objects.bin", folder / "objects.json"
    subprocess.run([worker, "parse", source, index, report], check=True,
                   capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
    mapped = make_native_snapshot(SavedLocalLevel(-1, source.stem, "", 0, ""),
                                  raw, index, load_native_report(report), classifier_hash())
    compiled = compile_layout(mapped.snapshot,
                              load_catalogue(ROOT / "gd/semantics/2.2081/catalogue.json"),
                              json.loads((ROOT / "gd/semantics/2.2081/effects.json").read_text()),
                              folder / "effects.bin", native_worker=worker)
    return mapped, compiled


def strip_file(source: Path, output: Path) -> dict:
    from bridge.removal_plan import make_removal_plan, valid_removal_plan

    source = source.resolve(strict=True)
    output = output.resolve()
    output.parent.resolve(strict=True)
    if output.exists() or output == source:
        raise FileExistsError(output)
    with tempfile.TemporaryDirectory(prefix="gd-layout-") as folder:
        mapped, compiled = compile_file(source, Path(folder))
        plan = make_removal_plan(mapped.snapshot,
                                 kept_save_ids=compiled.aggregation.kept_obj_ids,
                                 partition_proof=compiled.aggregation.proof)
        valid_removal_plan(plan.data)
        settings = mapped.snapshot.canonical["level"]["settings_exact_save_string"]
        rows = sorted(plan.data["retained_records"], key=lambda row: row["serialization_order_index"])
        data = (settings + ";" + "".join(row["exact_save_string"] + ";" for row in rows)).encode("utf-8")
        if hashlib.sha256(data).hexdigest().upper() != plan.data["expected_stripped_revision_sha256"]:
            raise ValueError("OUTPUT_HASH")
        with tempfile.NamedTemporaryFile(dir=output.parent, prefix=output.name + ".",
                                         suffix=".pending", delete=False) as pending:
            pending.write(data)
            pending.flush()
            os.fsync(pending.fileno())
            pending_path = Path(pending.name)
        os.rename(pending_path, output)
        return {"success": True, "output": str(output), "sha256": plan.data["expected_stripped_revision_sha256"],
                "objects": plan.data["source_object_count"], "kept": plan.data["retained_object_count"],
                "removed": plan.data["removed_object_count"], "roots": len(mapped.roots)}


def choose_file() -> None:
    from tkinter import Tk, filedialog, messagebox

    window = Tk()
    window.withdraw()
    try:
        game = filedialog.askopenfilename(title="Geometry Dash", filetypes=[("GeometryDash.exe", "GeometryDash.exe")])
        if not game:
            return
        os.environ["GD_GAME_EXE"] = game
        source = filedialog.askopenfilename(title="Level file", filetypes=[("Level", "*.level *.txt"), ("All", "*.*")])
        if not source:
            return
        output = filedialog.asksaveasfilename(title="Extracted level", defaultextension=".level",
                                             initialfile=Path(source).stem + "-layout.level")
        if not output:
            return
        result = strip_file(Path(source), Path(output))
        messagebox.showinfo("GD Layout Extractor", f"Extraction complete.\n{result['kept']} objects retained.")
    except Exception as error:
        messagebox.showerror("GD Layout Extractor", str(error))
    finally:
        window.destroy()
