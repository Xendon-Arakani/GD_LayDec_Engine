from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bridge.effect_operations import EngineOperation, route_fx_cap
from bridge.level_effects import FX_SEM_PROP_KEYS, FX_SEM_REV, _NO_TOUCH_LOST_TYPES
from bridge.removal_plan import classifier_hash


def cpp_text(value: str) -> str:
    return json.dumps(value, ensure_ascii=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("catalogue", type=Path)
    parser.add_argument("effects", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()

    catalogue = json.loads(args.catalogue.read_text(encoding="utf-8"))
    effects = json.loads(args.effects.read_text(encoding="utf-8"))
    cat_rows = {int(row["object_id"]): row for row in catalogue["entries"]}
    fx_rows = {int(row["object_id"]): row for row in effects["entries"]}
    if set(cat_rows) != set(fx_rows) or not cat_rows:
        raise SystemExit("Native asset tables disagree")
    max_id = max(cat_rows)
    op_codes = {item: index for index, item in enumerate(EngineOperation)}
    op_names: list[str] = []
    name_ids: dict[str, int] = {}
    assets: list[tuple[int, int, int]] = [(0, 0, 0)] * (max_id + 1)
    for object_id in sorted(cat_rows):
        cat_row = cat_rows[object_id]
        fx_row = fx_rows[object_id]
        route = route_fx_cap(fx_row)
        op_name = route.operation_name
        if op_name not in name_ids:
            name_ids[op_name] = len(op_names)
            op_names.append(op_name)
        classifiers = cat_row["engine_classifier_methods"]
        runtime_type = str(cat_row["runtime_object_type_name"])
        flags = 1
        flags |= int(route.root_layout_cand) << 1
        flags |= int(bool(classifiers["is_trigger"])) << 2
        flags |= int(bool(classifiers["is_speed_object"])) << 3
        flags |= int(bool(classifiers.get("is_stoppable_trigger", False))) << 4
        flags |= int(runtime_type in _NO_TOUCH_LOST_TYPES or object_id == 2063) << 5
        flags |= int(route.mechanical_candidate) << 6
        flags |= int(route.needs_obj_expand) << 7
        assets[object_id] = (
            flags,
            op_codes[route.operation],
            name_ids[op_name],
        )

    props = sorted(int(value) for value in FX_SEM_PROP_KEYS)
    prop_slots = [-1] * 601
    for index, prop in enumerate(props):
        prop_slots[prop] = index
    lines = [
        "#pragma once",
        "",
        "#include <array>",
        "#include <cstdint>",
        "#include <string_view>",
        "",
        "namespace gd_native {",
        "",
        "struct Asset {",
        "    std::uint8_t flags;",
        "    std::uint8_t op;",
        "    std::uint16_t name;",
        "};",
        "",
        f"inline constexpr char kClassHash[] = {cpp_text(classifier_hash())};",
        f"inline constexpr std::uint32_t kSemRev = {FX_SEM_REV};",
        f"inline constexpr std::array<std::uint16_t, {len(props)}> kProps{{{{",
        "    " + ",".join(str(value) for value in props),
        "}};",
        f"inline constexpr std::array<std::int16_t, {len(prop_slots)}> kPropSlots{{{{",
        "    " + ",".join(str(value) for value in prop_slots),
        "}};",
        f"inline constexpr std::array<std::string_view, {len(op_names)}> kOpNames{{{{",
        *[f"    {cpp_text(value)}," for value in op_names],
        "}};",
        f"inline constexpr std::array<Asset, {len(assets)}> kAssets{{{{",
        *[
            f"    Asset{{{flags},{operation},{name}}},"
            for flags, operation, name in assets
        ],
        "}};",
        "",
        "}",
        "",
    ]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text("\n".join(lines), encoding="utf-8", newline="\n")


if __name__ == "__main__":
    main()
