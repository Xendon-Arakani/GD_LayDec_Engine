







from __future__ import annotations

from dataclasses import dataclass

from ml.effect_graph import EffectApplication, StateAtom


GAME_VERSION = "2.2081"


@dataclass(frozen=True)
class EffectTargetFacts:


    instance_uid: str
    is_trigger: bool
    is_speed_object: bool

    def __post_init__(self) -> None:
        if not self.instance_uid:
            raise ValueError("instance_uid cannot be empty")


@dataclass(frozen=True)
class MoveEffectExpansion:


    effects: tuple[EffectApplication, ...]
    blocked_parts: tuple[str, ...]


def expa_move_fx_2_2081(
    *,
    source_uid: str,
    target: EffectTargetFacts,
    move_x: float,
    move_y: float,
    lock_player_x: bool = False,
    lock_player_y: bool = False,
    lock_camera_x: bool = False,
    lock_camera_y: bool = False,
    selector_evidence: tuple[str, ...] = (),
    extra_inputs: tuple[StateAtom, ...] = (),
    effect_kind: str = "move",
    activation_property: str = "activation",
) -> MoveEffectExpansion:















    if not source_uid:
        raise ValueError("source_uid cannot be empty")

    common_inputs = (
        StateAtom(source_uid, "effect_configuration"),
        StateAtom(source_uid, activation_property),
        *extra_inputs,
    )
    output_properties: list[str] = []
    suppressed: list[str] = []
    x_configured = bool(move_x or lock_player_x or lock_camera_x)
    y_configured = bool(move_y or lock_player_y or lock_camera_y)

    if target.is_trigger:
        if x_configured:
            suppressed.append("world_position.x")
        if y_configured:
            output_properties.append("runtime_display_position.y")
    elif target.is_speed_object:
        if x_configured:
            suppressed.append("world_position.x")
        if y_configured:
            output_properties.append("gameplay_world_position.y")
    else:
        if x_configured:
            output_properties.append("world_position.x")
        if y_configured:
            output_properties.append("world_position.y")

    effects = tuple(
        EffectApplication(
            effect_id=(
                f"{source_uid}:{activation_property}:{effect_kind}:"
                f"{target.instance_uid}:{property_name}"
            ),
            source_uid=source_uid,
            effect_kind=effect_kind,
            inputs=common_inputs,
            outputs=(StateAtom(target.instance_uid, property_name),),
            selector_evidence=selector_evidence,
        )
        for property_name in output_properties
    )
    return MoveEffectExpansion(
        effects=effects,
        blocked_parts=tuple(suppressed),
    )
