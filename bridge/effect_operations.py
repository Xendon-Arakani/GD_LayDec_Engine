








from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from bridge.snapshot import SnapValidError


class EngineOperation(str, Enum):
    INTRINSIC_ONLY = "intrinsic_only"
    MOVE = "move"
    FOLLOW = "follow"
    FOLLOW_PLAYER_Y = "follow_player_y"
    AREA_TRANSFORM = "area_transform"
    EDIT_AREA_TRANSFORM = "edit_area_transform"
    AREA_STOP = "area_stop"
    ADVANCED_FOLLOW = "advanced_follow"
    EDIT_ADVANCED_FOLLOW = "edit_advanced_follow"
    RETARGET_ADV_FOLLOW = "retarget_advanced_follow"
    CAMERA_TRANSLATION = "camera_translation"
    CAMERA_ZOOM = "camera_zoom"
    CAMERA_TRANSFORM = "camera_transform"
    ROTATE = "rotate"
    SCALE = "scale"
    TOGGLE = "toggle"
    TOUCH = "touch"
    SPAWN = "spawn"
    RANDOM = "random"
    ADVANCED_RANDOM = "advanced_random"
    SEQUENCE = "sequence"
    EVENT = "event"
    COLLISION = "collision"
    INSTANT_COLLISION = "instant_collision"
    COLLISION_STATE = "collision_state"
    COUNT = "count"
    INSTANT_COUNT = "instant_count"
    ITEM_COMPARE = "item_compare"
    ITEM_EDIT = "item_edit"
    PICKUP = "pickup"
    PERSISTENT_ITEM = "persistent_item"
    TIME = "time"
    TIME_EVENT = "time_event"
    TIME_CONTROL = "time_control"
    RESET = "reset"
    OPTIONS = "options"
    PLAYER_CONTROL = "player_control"
    TELEPORT = "teleport"
    KEYFRAME_ANIMATION = "keyframe_animation"
    END = "end"
    DIRECT_GLOBAL = "direct_global"
    STOP_PAUSE_RESUME = "stop_pause_resume"
    ON_DEATH = "on_death"
    UNSUPPORTED = "unsupported"


_IMPL_BY_LEDGER_NAME = {
    "Start Pos": EngineOperation.INTRINSIC_ONLY,
    "speed change": EngineOperation.INTRINSIC_ONLY,
    "Reverse": EngineOperation.INTRINSIC_ONLY,
    "Checkpoint": EngineOperation.INTRINSIC_ONLY,
    "Teleport Portal": EngineOperation.TELEPORT,
    "Teleport Orb": EngineOperation.TELEPORT,
    "Move": EngineOperation.MOVE,
    "Follow": EngineOperation.FOLLOW,
    "Follow Player Y": EngineOperation.FOLLOW_PLAYER_Y,
    "Area Move": EngineOperation.AREA_TRANSFORM,
    "Area Rotate": EngineOperation.AREA_TRANSFORM,
    "Area Scale": EngineOperation.AREA_TRANSFORM,
    "Edit Area Move": EngineOperation.EDIT_AREA_TRANSFORM,
    "Edit Area Rotate": EngineOperation.EDIT_AREA_TRANSFORM,
    "Edit Area Scale": EngineOperation.EDIT_AREA_TRANSFORM,
    "Edit Area Fade": EngineOperation.EDIT_AREA_TRANSFORM,
    "Edit Area Tint": EngineOperation.EDIT_AREA_TRANSFORM,
    "Area Stop": EngineOperation.AREA_STOP,
    "Advanced Follow": EngineOperation.ADVANCED_FOLLOW,
    "Edit Advanced Follow": EngineOperation.EDIT_ADVANCED_FOLLOW,
    "Re-Target Advanced Follow": EngineOperation.RETARGET_ADV_FOLLOW,
    "Static Camera": EngineOperation.CAMERA_TRANSLATION,
    "Camera Offset": EngineOperation.CAMERA_TRANSLATION,
    "Camera Edge": EngineOperation.CAMERA_TRANSLATION,
    "Gameplay Offset": EngineOperation.CAMERA_TRANSLATION,
    "Zoom": EngineOperation.CAMERA_ZOOM,
    "Shake": EngineOperation.CAMERA_TRANSFORM,
    "Camera Rotation": EngineOperation.CAMERA_TRANSFORM,
    "Rotate": EngineOperation.ROTATE,
    "Scale": EngineOperation.SCALE,
    "Toggle": EngineOperation.TOGGLE,
    "Touch": EngineOperation.TOUCH,
    "Spawn": EngineOperation.SPAWN,
    "Random": EngineOperation.RANDOM,
    "Advanced Random": EngineOperation.ADVANCED_RANDOM,
    "Sequence": EngineOperation.SEQUENCE,
    "Event": EngineOperation.EVENT,
    "Collision": EngineOperation.COLLISION,
    "Instant Collision": EngineOperation.INSTANT_COLLISION,
    "Collision State": EngineOperation.COLLISION_STATE,
    "Count": EngineOperation.COUNT,
    "Instant Count": EngineOperation.INSTANT_COUNT,
    "Item Compare": EngineOperation.ITEM_COMPARE,
    "Item Edit": EngineOperation.ITEM_EDIT,
    "Pickup": EngineOperation.PICKUP,
    "Persistent Item": EngineOperation.PERSISTENT_ITEM,
    "Time": EngineOperation.TIME,
    "Time Event": EngineOperation.TIME_EVENT,
    "Time Control": EngineOperation.TIME_CONTROL,
    "Reset": EngineOperation.RESET,
    "Options": EngineOperation.OPTIONS,
    "Player Control": EngineOperation.PLAYER_CONTROL,
    "Teleport": EngineOperation.TELEPORT,
    "Keyframe Animation": EngineOperation.KEYFRAME_ANIMATION,
    "TimeWarp": EngineOperation.DIRECT_GLOBAL,
    "Gravity": EngineOperation.DIRECT_GLOBAL,
    "Rotate Gameplay": EngineOperation.DIRECT_GLOBAL,
    "End": EngineOperation.END,
    "Platformer End": EngineOperation.END,
    "Camera Mode": EngineOperation.DIRECT_GLOBAL,
    "Stop Pause Resume": EngineOperation.STOP_PAUSE_RESUME,
    "On Death": EngineOperation.ON_DEATH,
}


@dataclass(frozen=True)
class OperationRoute:
    operation_name: str
    operation: EngineOperation
    mechanical_candidate: bool
    needs_obj_expand: bool
    root_layout_cand: bool
    declared_fx_kinds: tuple[str, ...]
    declared_visual_fx: tuple[str, ...]


def route_fx_cap(capability: dict[str, Any]) -> OperationRoute:







    operation_name = capability.get("engine_operation_name")
    if not isinstance(operation_name, str) or not operation_name:
        raise SnapValidError("asset effect row lacks engine operation identity")
    mechanical = capability.get("mechanical_effector_capability")
    if mechanical not in {"possible", "not_possible"}:
        raise SnapValidError("asset effect row has invalid mechanical capability")
    intrinsic = capability.get("intrinsic_layout_capability")
    if intrinsic not in {"possible", "not_possible"}:
        raise SnapValidError("asset effect row has invalid intrinsic capability")
    requires = capability.get("requires_instance_effect_expansion")
    if not isinstance(requires, bool):
        raise SnapValidError("asset effect row lacks expansion decision")

    effect_kinds = capability.get("effect_kinds", [])
    nonmechanical_kinds = capability.get("nonmechanical_effect_kinds", [])
    if not all(isinstance(value, str) and value for value in effect_kinds):
        raise SnapValidError("asset effect row has invalid effect kinds")
    if not all(isinstance(value, str) and value for value in nonmechanical_kinds):
        raise SnapValidError("asset effect row has invalid nonmechanical effects")

    operation = EngineOperation.INTRINSIC_ONLY
    if mechanical == "possible" and requires:
        operation = _IMPL_BY_LEDGER_NAME.get(
            operation_name, EngineOperation.UNSUPPORTED
        )
    return OperationRoute(
        operation_name=operation_name,
        operation=operation,
        mechanical_candidate=mechanical == "possible",
        needs_obj_expand=requires,
        root_layout_cand=intrinsic == "possible",
        declared_fx_kinds=tuple(effect_kinds),
        declared_visual_fx=tuple(nonmechanical_kinds),
    )
