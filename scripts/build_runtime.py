from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil

import PyInstaller.__main__


ROOT = Path(__file__).resolve().parents[1]


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for data in iter(lambda: source.read(1048576), b""):
            digest.update(data)
    return digest.hexdigest().upper()


def build(output: Path, analysis: bool = False) -> dict:
    output = output.resolve()
    if output.exists():
        raise FileExistsError(output)
    options = [str(ROOT / "scripts/runtime.py"), "--name", "gd-layout", "--onedir",
               "--noupx", "--icon", "NONE", "--hide-console", "hide-early", "--log-level", "WARN",
               "--paths", str(ROOT), "--distpath", str(output / "dist"),
               "--workpath", str(output / "work"), "--specpath", str(output / "spec"),
               "--exclude-module", "torch", "--exclude-module", "numpy", "--exclude-module", "scipy",
               "--exclude-module", "pytest", "--exclude-module", "IPython", "--exclude-module", "matplotlib"]
    for directory in ["bridge", "ml", "gd/semantics"]:
        for source in sorted((ROOT / directory).rglob("*")):
            if source.is_file() and source.suffix in {".py", ".json"} and "__pycache__" not in source.parts:
                options.extend(["--add-data", str(source) + ":" + source.parent.relative_to(ROOT).as_posix()])
    for name in ["compile_layout.py", "gen_native.py", "run_decimal.py", "run_highlight.py"]:
        options.extend(["--add-data", str(ROOT / "scripts" / name) + ":scripts"])
    options.extend(["--add-binary", str(ROOT / "native/build/gd-layout-worker.exe") + ":native"])
    PyInstaller.__main__.run(options)
    package = output / "dist/gd-layout"
    mod = ROOT / "native/build/sebastian-anderson.gd-layout-stripper.geode"
    if not analysis:
        shutil.copyfile(mod, package / mod.name)
    for path in (package / "_internal").glob("*.dll"):
        if path.name.startswith("api-ms-win-") or path.name.lower() == "ucrtbase.dll":
            path.unlink()
    shutil.copytree(ROOT / "licenses", package / "licenses")
    rows = [{"path": path.relative_to(package).as_posix(), "bytes": path.stat().st_size,
             "sha256": file_hash(path)} for path in sorted(package.rglob("*")) if path.is_file()]
    catalogue = json.loads((ROOT / "gd/semantics/2.2081/catalogue.json").read_text())
    manifest = {"mod_id": "sebastian-anderson.gd-layout-stripper", "mod_file": mod.name if not analysis else None,
                "game_sha256": catalogue["executable_sha256"].upper(),
                "mod_sha256": file_hash(mod) if not analysis else None,
                "component_mode": "analysis" if analysis else "mod", "files": rows}
    (package / "package.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return {"package": str(package), "executable": str(package / "gd-layout.exe"),
            "files": len(rows), "bytes": sum(row["bytes"] for row in rows)}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    parser.add_argument("--analysis", action="store_true")
    args = parser.parse_args()
    print(json.dumps(build(args.output, args.analysis)))


if __name__ == "__main__":
    main()
