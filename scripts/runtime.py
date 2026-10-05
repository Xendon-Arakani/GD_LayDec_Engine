from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys


ROOT = Path(sys._MEIPASS) if getattr(sys, "frozen", False) else Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def check_runtime(game: Path | None) -> dict:
    from bridge.catalogue import load_catalogue
    from bridge.removal_plan import classifier_hash

    worker = ROOT / "native/gd-layout-worker.exe"
    if not worker.is_file():
        worker = ROOT / "native/build/gd-layout-worker.exe"
    if not worker.is_file():
        raise FileNotFoundError(worker)
    if game is not None:
        load_catalogue(ROOT / "gd/semantics/2.2081/catalogue.json")
    return {"success": True, "frozen": bool(getattr(sys, "frozen", False)),
            "classifier_sha256": classifier_hash(),
            "worker_sha256": hashlib.sha256(worker.read_bytes()).hexdigest().upper()}


def main() -> None:
    if len(sys.argv) == 1:
        package = Path(sys.executable).parent / "package.json"
        if package.is_file() and json.loads(package.read_text()).get("component_mode") == "analysis":
            from scripts.file_extract import choose_file
            choose_file()
            return
        from scripts.install_runtime import choose_install
        choose_install()
        return
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["check", "extract", "strip", "map", "install"])
    parser.add_argument("path", nargs="?", type=Path)
    parser.add_argument("--game", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--notify", action="store_true")
    args = parser.parse_args()
    if args.game is not None:
        game = args.game.resolve(strict=True)
        if game.name != "GeometryDash.exe":
            raise ValueError("GAME_IDENTITY")
        os.environ["GD_GAME_EXE"] = str(game)
    if args.command == "check":
        result = check_runtime(args.game)
    elif args.command == "install":
        package = Path(sys.executable).parent / "package.json"
        if package.is_file() and json.loads(package.read_text()).get("component_mode") == "analysis":
            raise ValueError("INSTALL_UNAVAILABLE")
        from scripts.install_runtime import install
        result = install(args.path)
        if args.notify:
            from tkinter import messagebox
            messagebox.showinfo("GD Layout Stripper", "Installed.")
    elif args.command == "extract":
        from scripts.run_decimal import run
        result = run(args.path, ROOT / "gd/semantics/2.2081/catalogue.json",
                     ROOT / "gd/semantics/2.2081/effects.json")
    elif args.command == "strip":
        from scripts.file_extract import strip_file
        result = strip_file(args.path, args.output)
    else:
        from scripts.file_extract import compile_file
        from bridge.removal_plan import classifier_hash

        folder = args.output
        folder.mkdir(parents=True, exist_ok=True)
        mapped, compiled = compile_file(args.path, folder)
        result = {"success": True, "objects": len(mapped.snapshot.canonical["level"]["ordered_objects"]),
                  "roots": len(mapped.roots), "kept": len(compiled.aggregation.kept_obj_ids),
                  "classifier_sha256": classifier_hash()}
    print(json.dumps(result))


if __name__ == "__main__":
    try:
        main()
    except Exception as failure:
        print(json.dumps({"success": False, "error_code": type(failure).__name__,
                          "error": str(failure)}))
        raise SystemExit(1) from failure
