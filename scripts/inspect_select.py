from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path


root = Path(__file__).resolve().parents[1]
state_path = (
    Path(os.environ["LOCALAPPDATA"])
    / "GeometryDash"
    / "geode"
    / "mods"
    / "sebastian-anderson.gd-layout-stripper"
    / "selection-state.json"
)


def proc_alive(pid: int) -> bool:
    if os.name != "nt" or pid <= 0:
        return False
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        return False
    code = ctypes.c_ulong()
    alive = bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
    kernel.CloseHandle(handle)
    return alive


def main() -> None:
    if not state_path.is_file():
        print(json.dumps({
            "telemetry_status": "missing",
            "live_process": False,
            "state_path": str(state_path),
        }, indent=2))
        raise SystemExit(2)
    state = json.loads(state_path.read_text(encoding="utf-8"))
    pid = int(state.get("game_pid", 0))
    state["live_process"] = proc_alive(pid)
    state["telemetry_status"] = (
        "live" if state["live_process"] else "stale_process"
    )
    catalogue = json.loads(
        (root / "gd" / "semantics" / "2.2081" / "catalogue.json").read_text(
            encoding="utf-8"
        )
    )
    entries = {int(row["object_id"]): row for row in catalogue["entries"]}
    for selected in state.get("selected", []):
        entry = entries.get(int(selected["object_id"]))
        if entry is None:
            selected["gd_catalogue"] = None
            continue
        selected["gd_catalogue"] = {
            "frame_name": entry["frame_name"],
            "frame_name_status": entry["frame_name_status"],
            "runtime_type": entry["runtime_object_type_name"],
            "is_decoration_1": entry["is_decoration_flag_1"],
            "is_decoration_2": entry["is_decoration_flag_2"],
            "is_invisible_block": entry["is_invisible_block"],
            "is_no_touch": entry["is_no_touch"],
            "is_passable": entry["is_passable"],
            "object_material": entry["object_material"],
            "toolbox_tab": entry["editor_tab_index"],
            "toolbox_page": entry["editor_page_index"],
            "classifiers": entry["engine_classifier_methods"],
        }
    print(json.dumps(state, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
