from __future__ import annotations

import ctypes
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time


MOD_ID = "sebastian-anderson.gd-layout-stripper"


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for part in iter(lambda: source.read(1048576), b""):
            digest.update(part)
    return digest.hexdigest().upper()


def package_root() -> Path:
    if not getattr(sys, "frozen", False):
        raise RuntimeError("PACKAGE_REQUIRED")
    return Path(sys.executable).resolve(strict=True).parent


def install(game_dir: Path) -> dict:
    source = package_root()
    manifest = json.loads((source / "package.json").read_text(encoding="utf-8"))
    if manifest["mod_id"] != MOD_ID:
        raise ValueError("PACKAGE_IDENTITY")
    game_dir = game_dir.resolve(strict=True)
    game = game_dir / "GeometryDash.exe"
    if not game.is_file() or not (game_dir / "Geode.dll").is_file():
        raise ValueError("GEODE_REQUIRED")
    if file_hash(game) != manifest["game_sha256"]:
        raise ValueError("GAME_VERSION")
    tasks = subprocess.run([str(Path(os.environ["SystemRoot"]) / "System32/tasklist.exe"),
                            "/FI", "IMAGENAME eq GeometryDash.exe", "/FO", "CSV", "/NH"],
                           check=True, capture_output=True, text=True,
                           creationflags=subprocess.CREATE_NO_WINDOW)
    if '"GeometryDash.exe"' in tasks.stdout:
        raise RuntimeError("CLOSE_GEOMETRY_DASH")
    for row in manifest["files"]:
        path = (source / row["path"]).resolve(strict=True)
        if not path.is_relative_to(source) or file_hash(path) != row["sha256"]:
            raise ValueError("PACKAGE_HASH")
    save = Path(os.environ["LOCALAPPDATA"]) / "GeometryDash/geode/mods" / MOD_ID
    save.mkdir(parents=True, exist_ok=True)
    revision = str(time.time_ns())
    staged = save / ("runtime-" + revision)
    staged.mkdir()
    for row in manifest["files"]:
        relative = Path(row["path"])
        if relative.name.endswith(".geode"):
            continue
        target = staged / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / relative, target)
        if file_hash(target) != row["sha256"]:
            raise ValueError("INSTALL_HASH")
    active = save / "runtime"
    backup = save / ("runtime-backup-" + revision)
    mods = game_dir / "geode/mods"
    mods.mkdir(parents=True, exist_ok=True)
    mod = mods / (MOD_ID + ".geode")
    mod_backup = save / ("mod-backup-" + revision + ".geode")
    mod_pending = mods / (MOD_ID + "-" + revision + ".pending")
    shutil.copyfile(source / manifest["mod_file"], mod_pending)
    if file_hash(mod_pending) != manifest["mod_sha256"]:
        raise ValueError("MOD_HASH")
    if mod.exists():
        shutil.copyfile(mod, mod_backup)
    moved_old = False
    moved_new = False
    try:
        if active.exists():
            active.rename(backup)
            moved_old = True
        staged.rename(active)
        moved_new = True
        os.replace(mod_pending, mod)
    except Exception:
        if moved_new:
            active.rename(save / ("runtime-failed-" + revision))
        if moved_old:
            backup.rename(active)
        raise
    state = {"success": True, "mod_id": MOD_ID, "mod_path": str(mod),
             "runtime": str(active)}
    (save / "install-state.json").write_text(json.dumps(state), encoding="utf-8")
    return state


def choose_install() -> None:
    import tkinter as tk
    from tkinter import filedialog, messagebox

    root = tk.Tk()
    root.withdraw()
    try:
        folder = filedialog.askdirectory(title="Geometry Dash folder", mustexist=True)
        if not folder:
            return
        if not messagebox.askokcancel("GD Layout Stripper", "Install GD Layout Stripper? Geometry Dash must be closed."):
            return
        install(Path(folder))
        messagebox.showinfo("GD Layout Stripper", "Installed.")
    except PermissionError:
        command = subprocess.list2cmdline(["install", folder, "--notify"])
        launch = ctypes.windll.shell32.ShellExecuteW
        launch.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_wchar_p,
                           ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_int]
        launch.restype = ctypes.c_void_p
        result = launch(None, "runas", sys.executable, command, None, 0)
        if not result or result <= 32:
            messagebox.showerror("GD Layout Stripper", "Installation needs permission.")
    except Exception as error:
        messagebox.showerror("GD Layout Stripper", str(error))
    finally:
        root.destroy()
