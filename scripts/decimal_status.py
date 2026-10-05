

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path


MOD_ID = "sebastian-anderson.gd-layout-stripper"


def default_status_path() -> Path:
    return (
        Path(os.environ["LOCALAPPDATA"])
        / "GeometryDash"
        / "geode"
        / "mods"
        / MOD_ID
        / "toggle-audits"
        / "status-latest.json"
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--path", type=Path, default=default_status_path())
    parser.add_argument("--operation-id")
    args = parser.parse_args()

    status: dict[str, object] | None = None
    try:
        candidate = json.loads(args.path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        candidate = None
    if isinstance(candidate, dict) and (
        args.operation_id is None or candidate.get("operation_id") == args.operation_id
    ):
        status = candidate

    if status is None:
        print(json.dumps({
            "status": "unavailable",
            "terminal": False,
            "path": str(args.path),
            "requested_operation_id": args.operation_id,
        }, indent=2))
        raise SystemExit(2)

    print(json.dumps(status, indent=2, ensure_ascii=False))
    if status.get("terminal") is not True:
        raise SystemExit(3)
    raise SystemExit(0 if status.get("success") is True else 1)


if __name__ == "__main__":
    main()
