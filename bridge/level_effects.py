

from __future__ import annotations

from dataclasses import dataclass, replace
from collections import defaultdict, deque
import math
from typing import Any, Callable, Iterable

from bridge.catalogue import ValidatedCatalogue
from bridge.effect_operations import EngineOperation, route_fx_cap
from bridge.snapshot import SnapValidError, ValidatedSnapshot
from ml.effect_graph import (
    EffectApplication,
    EffectClosure,
    StateAtom,
)
from ml.effect_targeting import EffectTargetFacts, expa_move_fx_2_2081

FX_SEM_REV = 12

FX_SEM_PROP_KEYS = frozenset(
    "1,10,21,22,23,28,29,34,50,51,52,56,57,58,59,63,68,69,70,71,72,73,76,80,82,90,93,95,"
    "100,101,110,121,133,138,141,142,150,151,152,153,154,160,164,165,199,"
    "200,201,203,204,205,206,207,209,218,219,225,231,232,233,234,235,236,237,238,239,240,241,242,243,249,260,262,263,264,265,270,"
    "271,274,287,306,307,339,345,347,352,353,354,355,366,373,394,397,401,"
    "278,283,343,344,430,435,436,441,442,452,469,471,472,473,474,475,476,477,478,480,481,"
    "482,491,492,493,494,521,522,523,534,535,538,540,541,542,543,545,546,"
    "573,577,581,591,593".split(",")
)


class _DiscardingList(list[dict[str, Any]]):


    def append(self, value: dict[str, Any]) -> None:
        del value

    def extend(self, values: Iterable[dict[str, Any]]) -> None:
        del values


@dataclass(frozen=True)
class ParsedLevelInstance:
    uid: str
    serialization_index: int
    object_id: int
    properties: dict[str, tuple[str, ...]]
    runtime_type: str
    is_trigger: bool
    is_color_trigger: bool
    is_speed_object: bool
    is_stoppable_trigger: bool
    activation_state: str = "activation"
    remap_context: tuple[tuple[tuple[int, int], ...], ...] = ()
    remap_root_uid: str | None = None

    def values(self, key: str) -> tuple[str, ...]:
        return self.properties.get(key, ())

    def one(self, key: str, default: str | None = None) -> str | None:
        values = self.values(key)
        if not values:
            return default
        if len(set(values)) != 1:
            raise SnapValidError(
                f"object {self.serialization_index} repeats semantic key {key} with conflicting values"
            )
        return values[0]


@dataclass(frozen=True)
class LevelEffectAnalysis:
    effects: tuple[EffectApplication, ...]
    true_layout_atoms: frozenset[StateAtom]
    true_layout_uids: frozenset[str]

    mechanical_closure: EffectClosure
    unsupported_uses: tuple[dict[str, Any], ...]
    report: dict[str, Any]


class _EffectCollector:

    def __init__(
        self,
        sink_atoms: Iterable[StateAtom],
        *,
        capture_fx: bool,
        effect_sink: Callable[[EffectApplication], None] | None = None,
        compute_closure: bool = True,
    ) -> None:
        self.capture_fx = capture_fx
        self.effect_sink = effect_sink
        self.compute_closure = compute_closure
        self._captured: list[EffectApplication] = []
        self._waiting: dict[StateAtom, list[EffectApplication]] = defaultdict(list)
        self._atom_pool: dict[StateAtom, StateAtom] = {}
        self._required: set[StateAtom] = {
            self._intern_atom(atom) for atom in sink_atoms
        }
        self._retained_effect_ids: set[str] = set()
        self._retained_keys: set[str] = set()
        self._kept_src_uids: set[str] = set()
        self._seen_effect_ids: set[str] = set()
        self.expanded_fx_count = 0
        self.kept_fx_count = 0

    def _intern_atom(self, atom: StateAtom) -> StateAtom:
        existing = self._atom_pool.get(atom)
        if existing is not None:
            return existing
        self._atom_pool[atom] = atom
        return atom

    def _compact(self, effect: EffectApplication) -> EffectApplication:
        return EffectApplication(
            effect_id=str(self.expanded_fx_count),
            source_uid=effect.source_uid,
            effect_kind=effect.effect_kind,
            inputs=tuple(self._intern_atom(atom) for atom in effect.inputs),
            outputs=tuple(self._intern_atom(atom) for atom in effect.outputs),
            selector_evidence=(),
        )

    def append(self, effect: EffectApplication) -> None:
        self.expanded_fx_count += 1
        if self.effect_sink is not None:
            self.effect_sink(effect)
        if self.capture_fx:
            if effect.effect_id in self._seen_effect_ids:
                raise ValueError(
                    "effect_id values must be unique after instance expansion"
                )
            self._seen_effect_ids.add(effect.effect_id)
            self._captured.append(effect)
            stored = effect
        elif not self.compute_closure:
            return
        else:
            stored = self._compact(effect)

        if not self.compute_closure:
            return

        if any(output in self._required for output in stored.outputs):
            self._retain(stored)
            return
        for output in stored.outputs:
            self._waiting[output].append(stored)

    def extend(self, effects: Iterable[EffectApplication]) -> None:
        for effect in effects:
            self.append(effect)

    def _retain(self, first: EffectApplication) -> None:
        pending_effects = deque((first,))
        pending_atoms: deque[StateAtom] = deque()
        while pending_effects or pending_atoms:
            while pending_effects:
                effect = pending_effects.popleft()
                if effect.effect_id in self._retained_keys:
                    continue
                self._retained_keys.add(effect.effect_id)
                self.kept_fx_count += 1
                if self.capture_fx:
                    self._retained_effect_ids.add(effect.effect_id)
                self._kept_src_uids.add(effect.source_uid)
                pending_atoms.extend(effect.inputs)
            while pending_atoms:
                atom = self._intern_atom(pending_atoms.popleft())
                if atom in self._required:
                    continue
                self._required.add(atom)
                pending_effects.extend(self._waiting.pop(atom, ()))

    def finish(self, sink_atoms: Iterable[StateAtom]) -> EffectClosure:
        if not self.compute_closure:
            self._atom_pool.clear()
            required = frozenset(self._required)
            self._required.clear()
            return EffectClosure(
                required_atoms=required,
                retained_effect_ids=frozenset(),
                retained_source_uids=frozenset(),
            )
        for atom in sink_atoms:
            canonical = self._intern_atom(atom)
            if canonical in self._required:
                continue
            self._required.add(canonical)
            for producer in self._waiting.pop(canonical, ()):
                self._retain(producer)
        self._waiting.clear()
        self._atom_pool.clear()
        required = frozenset(self._required)
        self._required.clear()
        self._retained_keys.clear()
        return EffectClosure(
            required_atoms=required,
            retained_effect_ids=frozenset(self._retained_effect_ids),
            retained_source_uids=frozenset(self._kept_src_uids),
        )

    def snapshot(self) -> tuple[EffectApplication, ...]:
        return tuple(self._captured)


def _integer(value: str | None, *, default: int = 0) -> int:
    if value is None or value == "":
        return default
    return int(value)


def _number(value: str | None, *, default: float = 0.0) -> float:
    if value is None or value == "":
        return default
    return float(value)


def _boolean(value: str | None) -> bool:
    return _integer(value) != 0


def _groups(value: str | None) -> tuple[int, ...]:
    if value is None or value == "":
        return ()

    return tuple(dict.fromkeys(int(part) for part in value.split(".") if part))


def _level_settings(snapshot: ValidatedSnapshot) -> dict[str, str]:

    raw = snapshot.canonical.get("level", {}).get(
        "settings_exact_save_string", ""
    )
    if not raw:
        return {}
    tokens = raw.split(",")
    if len(tokens) % 2:
        raise SnapValidError(
            "level settings have an incomplete key/value pair"
        )
    return {
        tokens[index]: tokens[index + 1]
        for index in range(0, len(tokens), 2)
    }


def _dot_pairs(value: str | None) -> tuple[tuple[int, float], ...]:


    if value is None or value == "":
        return ()
    parts = [part for part in value.split(".") if part != ""]
    if len(parts) % 2:
        raise SnapValidError(
            f"alternating selector/value list has odd field count: {value!r}"
        )
    return tuple(
        (int(parts[index]), float(parts[index + 1]))
        for index in range(0, len(parts), 2)
    )


def _integer_dot_pairs(value: str | None) -> tuple[tuple[int, int], ...]:


    return tuple((selector, int(mapped)) for selector, mapped in _dot_pairs(value))

_REMAP_SCALAR_PROPS = frozenset(
    {
        "51",
        "71",
        "76",
        "80",
        "95",
        "203",
        "204",
        "205",
        "206",
        "209",
        "225",
        "343",
        "344",
        "366",
        "392",
        "395",
        "401",
        "416",
        "432",
        "447",
        "455",
        "457",
        "516",
        "517",
        "518",
        "519",
        "525",
        "534",
        "560",
        "565",
    }
)


def _remap_id(
    value: int, context: tuple[tuple[tuple[int, int], ...], ...]
) -> int:


    for remap_pairs in context:
        value = dict(remap_pairs).get(value, value)
    return value


def _selector_integer(
    source: ParsedLevelInstance, key: str, *, default: int = 0
) -> int:
    value = _integer(source.one(key), default=default)
    if key in _REMAP_SCALAR_PROPS and source.remap_context:
        return _remap_id(value, source.remap_context)
    return value


def _acti_stat_name(
    context: tuple[tuple[tuple[int, int], ...], ...], root_uid: str | None
) -> str:
    if not context:
        return "activation"
    maps = "|".join(
        ",".join(f"{old}>{new}" for old, new in remap_pairs)
        for remap_pairs in context
    )
    return f"activation@spawn-remap:{maps}@root:{root_uid or '-'}"


def _item_uid(item_id: int, *, timer: bool = False) -> str:

    return f"__timer__:{item_id}" if timer else f"__item__:{item_id}"


def _item_value_atoms(mode: int, item_id: int) -> tuple[StateAtom, ...]:

    if mode == 1 and item_id > 0:
        uid = _item_uid(item_id)
        return (StateAtom(uid, "value"), StateAtom(uid, "persistence"))
    if mode == 2 and item_id > 0:
        uid = _item_uid(item_id, timer=True)
        return (StateAtom(uid, "value"), StateAtom(uid, "persistence"))
    global_property = {
        3: "points",
        4: "elapsed_time",
        5: "attempt_count",
    }.get(mode)
    if global_property is not None:
        return (StateAtom("__level__", global_property),)
    return ()


def _item_target_atom(mode: int, item_id: int) -> StateAtom | None:


    if mode == 1 and item_id > 0:
        return StateAtom(_item_uid(item_id), "value")
    if mode == 2 and item_id > 0:
        return StateAtom(_item_uid(item_id, timer=True), "value")
    if mode == 3:
        return StateAtom("__level__", "points")
    return None


def _group_parent_ids(instance: ParsedLevelInstance) -> tuple[int, ...]:

    explicit = _groups(instance.one("274"))
    if explicit:
        return explicit
    if _boolean(instance.one("34")):
        return _groups(instance.one("57"))
    return ()


def _main_object_members(
    group_id: int,
    *,
    group_members: dict[int, list[ParsedLevelInstance]],
    group_parents: dict[int, ParsedLevelInstance],
) -> tuple[ParsedLevelInstance, ...]:


    if group_id <= 0:
        return ()
    parent = group_parents.get(group_id)
    if parent is not None:
        return (parent,)
    members = group_members.get(group_id, [])
    if len(members) == 1:
        return (members[0],)
    return ()


def _one_obj_members(
    group_id: int,
    *,
    group_members: dict[int, list[ParsedLevelInstance]],
    group_parents: dict[int, ParsedLevelInstance],
) -> tuple[ParsedLevelInstance, ...]:

    return _main_object_members(
        group_id, group_members=group_members, group_parents=group_parents
    )


def _parse_instances(
    snapshot: ValidatedSnapshot, catalogue: ValidatedCatalogue
) -> tuple[ParsedLevelInstance, ...]:
    instances: list[ParsedLevelInstance] = []
    for record in snapshot.canonical["level"]["ordered_objects"]:
        typed = record["typed_projection_derived"]
        object_id = typed["object_id"]
        catalogue_entry = catalogue.entries_by_id.get(object_id)
        if catalogue_entry is None:
            raise SnapValidError(
                f"captured object {record['serialization_order_index']} has unknown ID {object_id}"
            )
        properties: dict[str, list[str]] = {}

        compact_properties = record.get("engine_effective_properties")
        if compact_properties is not None:
            properties = compact_properties
        else:
            semantic_pairs = record.get(
                "engine_effective_pairs", record["ordered_raw_pairs"]
            )
            for pair in semantic_pairs:
                properties.setdefault(pair["key_text"], []).append(pair["value_text"])
        classifiers = catalogue_entry["engine_classifier_methods"]
        instances.append(
            ParsedLevelInstance(
                uid=record["sample_local_uid"],
                serialization_index=record["serialization_order_index"],
                object_id=object_id,
                properties=(
                    properties
                    if compact_properties is not None
                    else {key: tuple(values) for key, values in properties.items()}
                ),
                runtime_type=catalogue_entry["runtime_object_type_name"],
                is_trigger=classifiers["is_trigger"],
                is_color_trigger=classifiers["is_color_trigger"],
                is_speed_object=classifiers["is_speed_object"],
                is_stoppable_trigger=classifiers.get("is_stoppable_trigger", False),
            )
        )
    return tuple(instances)


def _capabilities_by_id(capabilities: dict[str, Any]) -> dict[int, dict[str, Any]]:
    if capabilities.get("schema") != "gd-strip-asset-effect-capabilities":
        raise SnapValidError("unsupported asset effect capabilities schema")
    return {entry["object_id"]: entry for entry in capabilities.get("entries", [])}


def _effect(
    *,
    source: ParsedLevelInstance,
    target_uid: str,
    kind: str,
    output: str,
    inputs: Iterable[StateAtom] = (),
    selector_evidence: Iterable[str] = (),
) -> EffectApplication:
    source_inputs = [
        StateAtom(source.uid, "effect_configuration"),
        StateAtom(source.uid, "enabled"),
        StateAtom(source.uid, source.activation_state),
    ]
    if source.is_stoppable_trigger:
        source_inputs.append(StateAtom(source.uid, "effect_execution_state"))
    return EffectApplication(
        effect_id=(
            f"{source.uid}:{source.activation_state}:{kind}:{target_uid}:{output}"
        ),
        source_uid=source.uid,
        effect_kind=kind,
        inputs=(*source_inputs, *tuple(inputs)),
        outputs=(StateAtom(target_uid, output),),
        selector_evidence=tuple(selector_evidence),
    )


def _spawn_group_effects(
    *,
    source: ParsedLevelInstance,
    target_group: int,
    group_members: dict[int, list[ParsedLevelInstance]],
    kind: str,
    inputs: Iterable[StateAtom] = (),
    selector_evidence: Iterable[str] = (),
    activation_context: tuple[tuple[tuple[int, int], ...], ...] | None = None,
    activation_root_uid: str | None = None,
    activation_state_for: Callable[
        [ParsedLevelInstance, tuple[tuple[tuple[int, int], ...], ...], str | None],
        str,
    ]
    | None = None,
) -> Iterable[EffectApplication]:


    child_context = (
        source.remap_context if activation_context is None else activation_context
    )
    child_root_uid = (
        source.remap_root_uid if activation_context is None else activation_root_uid
    )
    for target in group_members.get(target_group, []):
        if not target.is_trigger:
            continue
        output = "activation"
        if activation_state_for is not None:
            output = activation_state_for(target, child_context, child_root_uid)
        yield _effect(
            source=source,
            target_uid=target.uid,
            kind=kind,
            output=output,
            inputs=inputs,
            selector_evidence=(
                f"target_group:{target_group}",
                f"target_member:{target.uid}",
                *tuple(selector_evidence),
            ),
        )


def _group_cmd_fx(
    *,
    source: ParsedLevelInstance,
    target_group: int,
    activate: bool,
    group_members: dict[int, list[ParsedLevelInstance]],
    kind: str,
    inputs: Iterable[StateAtom] = (),
    selector_evidence: Iterable[str] = (),
    activation_state_for: Callable[
        [ParsedLevelInstance, tuple[tuple[tuple[int, int], ...], ...], str | None],
        str,
    ]
    | None = None,
) -> Iterable[EffectApplication]:


    for target in group_members.get(target_group, []):
        output = "activation" if activate and target.is_trigger else "enabled"
        if output == "activation" and activation_state_for is not None:
            output = activation_state_for(
                target, source.remap_context, source.remap_root_uid
            )
        yield _effect(
            source=source,
            target_uid=target.uid,
            kind=kind,
            output=output,
            inputs=inputs,
            selector_evidence=(
                f"target_group:{target_group}",
                f"target_member:{target.uid}",
                f"activate:{int(activate)}",
                *tuple(selector_evidence),
            ),
        )


_AREA_BASE_IDS = frozenset({3006, 3007, 3008, 3009, 3010})


def _spatial_input_atoms(instance: ParsedLevelInstance) -> tuple[StateAtom, ...]:

    atoms = [
        StateAtom(instance.uid, "world_position.x"),
        StateAtom(instance.uid, "world_position.y"),
    ]
    if instance.is_trigger:
        atoms.append(StateAtom(instance.uid, "runtime_display_position.y"))
    if instance.is_speed_object:
        atoms.append(StateAtom(instance.uid, "gameplay_world_position.y"))
    return tuple(atoms)


def _collide_atoms(
    instance: ParsedLevelInstance,
) -> tuple[StateAtom, ...]:

    return tuple(
        dict.fromkeys(
            (
                StateAtom(instance.uid, "mechanical_identity"),
                StateAtom(instance.uid, "enabled"),
                *_spatial_input_atoms(instance),
                StateAtom(instance.uid, "rotation"),
                StateAtom(instance.uid, "scale.x"),
                StateAtom(instance.uid, "scale.y"),
            )
        )
    )


def _collision_inputs(
    source: ParsedLevelInstance,
    *,
    collide_blocks_by_id: dict[int, list[ParsedLevelInstance]],
) -> tuple[tuple[StateAtom, ...], tuple[str, ...]]:


    block_a = _selector_integer(source, "80")
    block_b = _selector_integer(source, "95")
    target_p1 = _boolean(source.one("138"))
    target_p2 = _boolean(source.one("200"))
    target_either_player = _boolean(source.one("201"))

    if target_either_player:

        block_ids: tuple[int, ...] = ()
    elif target_p1 or target_p2:


        block_ids = (block_b,) if block_b > 0 else ()
    else:
        block_ids = tuple(
            block_id for block_id in (block_a, block_b) if block_id > 0
        )
    inputs = [
        atom
        for block_id in block_ids
        for block in collide_blocks_by_id.get(block_id, ())
        for atom in _collide_atoms(block)
    ]
    if target_p1 or target_either_player:
        inputs.extend(
            (
                StateAtom("__level__", "player_position.x"),
                StateAtom("__level__", "player_position.y"),
            )
        )
    if target_p2 or target_either_player:
        inputs.extend(
            (
                StateAtom("__level__", "player2_position.x"),
                StateAtom("__level__", "player2_position.y"),
            )
        )
    evidence: list[str] = []
    if not (target_p1 or target_p2 or target_either_player):
        evidence.extend(
            (f"collision_block_a_id:{block_a}", f"collision_block_b_id:{block_b}")
        )
    elif not target_either_player:
        evidence.append(f"collision_block_b_id:{block_b}")
    if target_p1:
        evidence.append("collision_second_operand:player1")
    if target_p2:
        evidence.append("collision_second_operand:player2")
    if target_either_player:
        evidence.append("collision_second_operand:either_player")
    return tuple(dict.fromkeys(inputs)), tuple(evidence)


def _special_space_in(special_target: int) -> tuple[StateAtom, ...]:


    if special_target == -1:
        return (
            StateAtom("__level__", "player_position.x"),
            StateAtom("__level__", "player_position.y"),
        )
    if special_target == -2:
        return (
            StateAtom("__level__", "player2_position.x"),
            StateAtom("__level__", "player2_position.y"),
        )
    if special_target == -3:
        return (
            StateAtom("__level__", "camera_position.x"),
            StateAtom("__level__", "camera_position.y"),
        )
    if special_target <= -4:


        return (
            StateAtom("__level__", "camera_position.x"),
            StateAtom("__level__", "camera_position.y"),
            StateAtom("__level__", "camera_zoom"),
        )
    return ()


def _area_center_inputs(
    source: ParsedLevelInstance,
    *,
    group_members: dict[int, list[ParsedLevelInstance]],
    group_parents: dict[int, ParsedLevelInstance],
) -> tuple[tuple[StateAtom, ...], tuple[str, ...]]:
    special_target = _selector_integer(source, "538")
    if special_target < 0:
        return (
            _special_space_in(special_target),
            (f"special_center:{special_target}",),
        )
    center_group = _selector_integer(source, "71")
    if center_group:
        centers = _main_object_members(
            center_group,
            group_members=group_members,
            group_parents=group_parents,
        )
        return (
            tuple(atom for center in centers for atom in _spatial_input_atoms(center)),
            (
                f"center_group:{center_group}",
                *tuple(f"main_center_member:{center.uid}" for center in centers),
            ),
        )
    return _spatial_input_atoms(source), ("center:self",)


def _area_move_parts(source: ParsedLevelInstance) -> tuple[bool, bool]:


    if _boolean(source.one("241")):
        return (
            bool(_number(source.one("237")) or _number(source.one("238"))),
            bool(_number(source.one("239")) or _number(source.one("240"))),
        )
    if not (_number(source.one("218")) or _number(source.one("219"))):
        return False, False
    if _boolean(source.one("287")) or _number(source.one("232")):
        return True, True
    angle = math.radians(_number(source.one("231")))
    return abs(math.cos(angle)) > 1e-7, abs(math.sin(angle)) > 1e-7


def _adv_foll_targ_in(
    source: ParsedLevelInstance,
    *,
    group_members: dict[int, list[ParsedLevelInstance]],
    group_parents: dict[int, ParsedLevelInstance],
) -> tuple[tuple[StateAtom, ...], tuple[str, ...]]:
    special = 0
    if _boolean(source.one("138")):
        special = -1
    elif _boolean(source.one("200")):
        special = -2
    elif _boolean(source.one("201")):
        special = -3
    if special:
        return _special_space_in(special), (f"follow_special:{special}",)

    follow_group = _selector_integer(source, "71")
    centers = _main_object_members(
        follow_group,
        group_members=group_members,
        group_parents=group_parents,
    )
    return (
        tuple(atom for center in centers for atom in _spatial_input_atoms(center)),
        (
            f"follow_group:{follow_group}",
            *tuple(f"main_follow_member:{center.uid}" for center in centers),
        ),
    )


def _adv_follow_ref_in(
    source: ParsedLevelInstance,
    *,
    group_members: dict[int, list[ParsedLevelInstance]],
    group_parents: dict[int, ParsedLevelInstance],
) -> tuple[StateAtom, ...]:
    inputs: list[StateAtom] = []
    for property_key in ("560", "565"):
        reference_group = _selector_integer(source, property_key)
        for reference in _one_obj_members(
            reference_group,
            group_members=group_members,
            group_parents=group_parents,
        ):
            inputs.extend(_spatial_input_atoms(reference))
    range_group = _selector_integer(source, "366")
    for reference in _main_object_members(
        range_group,
        group_members=group_members,
        group_parents=group_parents,
    ):
        inputs.extend(_spatial_input_atoms(reference))
    return tuple(dict.fromkeys(inputs))


def _rotate_tracking_in(
    source: ParsedLevelInstance,
    *,
    follow_mode: bool,
    group_members: dict[int, list[ParsedLevelInstance]],
    group_parents: dict[int, ParsedLevelInstance],
) -> tuple[tuple[StateAtom, ...], tuple[str, ...], bool]:

    if _boolean(source.one("138")):
        components = ("player_rotation",) if follow_mode else (
            "player_position.x",
            "player_position.y",
        )
        return (
            tuple(StateAtom("__level__", component) for component in components),
            ("rotation_target:player1",),
            True,
        )
    if _boolean(source.one("200")):

        components = (
            ("player_rotation", "player2_rotation")
            if follow_mode
            else (
                "player_position.x",
                "player_position.y",
                "player2_position.x",
                "player2_position.y",
            )
        )
        return (
            tuple(StateAtom("__level__", component) for component in components),
            ("rotation_target:player2_with_player1_fallback",),
            True,
        )

    rot_target_group = _selector_integer(source, "401")
    targets = _one_obj_members(
        rot_target_group,
        group_members=group_members,
        group_parents=group_parents,
    )
    inputs = tuple(
        atom
        for target in targets
        for atom in (
            (StateAtom(target.uid, "rotation"),)
            if follow_mode
            else _spatial_input_atoms(target)
        )
    )
    return (
        inputs,
        (
            f"rotation_target_group:{rot_target_group}",
            *tuple(f"reachable_rotation_target:{target.uid}" for target in targets),
        ),
        bool(targets),
    )


def _rotate_limit_inputs(
    source: ParsedLevelInstance,
    *,
    group_members: dict[int, list[ParsedLevelInstance]],
    group_parents: dict[int, ParsedLevelInstance],
) -> tuple[tuple[StateAtom, ...], tuple[str, ...]]:


    inputs: list[StateAtom] = []
    evidence: list[str] = []
    for property_key, boundary_name in (
        ("516", "min_x"),
        ("517", "min_y"),
        ("518", "max_x"),
        ("519", "max_y"),
    ):
        group_id = _selector_integer(source, property_key)
        if not group_id:
            continue
        boundaries = _one_obj_members(
            group_id,
            group_members=group_members,
            group_parents=group_parents,
        )
        evidence.append(f"{boundary_name}_group:{group_id}")
        evidence.extend(
            f"reachable_{boundary_name}_member:{boundary.uid}"
            for boundary in boundaries
        )
        for boundary in boundaries:
            inputs.extend(_spatial_input_atoms(boundary))
    return tuple(dict.fromkeys(inputs)), tuple(evidence)


def _camera_components(source: ParsedLevelInstance) -> tuple[str, ...]:
    mode = _integer(source.one("101"))
    if mode == 1:
        return ("camera_position.x",)
    if mode == 2:
        return ("camera_position.y",)
    return ("camera_position.x", "camera_position.y")


_NO_TOUCH_LOST_TYPES = frozenset(
    {
        "AnimatedHazard",
        "BallPortal",
        "Breakable",
        "Collectible",
        "CubePortal",
        "CustomRing",
        "DashRing",
        "DropRing",
        "DualPortal",
        "GravityDashRing",
        "GravityPad",
        "GravityRing",
        "GravityTogglePortal",
        "GreenRing",
        "Hazard",
        "InverseGravityPortal",
        "InverseMirrorPortal",
        "MiniSizePortal",
        "NormalGravityPortal",
        "NormalMirrorPortal",
        "PinkJumpPad",
        "PinkJumpRing",
        "RedJumpPad",
        "RedJumpRing",
        "RegularSizePortal",
        "RobotPortal",
        "SecretCoin",
        "ShipPortal",
        "Slope",
        "Solid",
        "SoloPortal",
        "Special",
        "SpiderOrb",
        "SpiderPad",
        "SpiderPortal",
        "SwingPortal",
        "TeleportOrb",
        "TeleportPortal",
        "UfoPortal",
        "UserCoin",
        "WavePortal",
        "YellowJumpPad",
        "YellowJumpRing",
    }
)


def _is_root_layout_obj(instance: ParsedLevelInstance) -> bool:

    no_touch_mutable = (
        instance.runtime_type in _NO_TOUCH_LOST_TYPES
        or instance.object_id == 2063
    )
    return not (no_touch_mutable and _boolean(instance.one("121")))


def _trac_type_item_ids(
    source: ParsedLevelInstance,
    route_operation: EngineOperation,
    normal_item_ids: set[int],
    timer_item_ids: set[int],
) -> None:

    def add_typed(mode: int, item_id: int) -> None:
        if item_id <= 0:
            return
        if mode == 1:
            normal_item_ids.add(item_id)
        elif mode == 2:
            timer_item_ids.add(item_id)

    if route_operation in {
        EngineOperation.COUNT,
        EngineOperation.INSTANT_COUNT,
        EngineOperation.PICKUP,
    }:
        item_id = _selector_integer(source, "80")
        if item_id > 0:
            normal_item_ids.add(item_id)
    elif route_operation in {EngineOperation.ITEM_COMPARE, EngineOperation.ITEM_EDIT}:
        add_typed(_integer(source.one("476")), _selector_integer(source, "80"))
        add_typed(_integer(source.one("477")), _selector_integer(source, "95"))
        if route_operation is EngineOperation.ITEM_EDIT:
            add_typed(
                _integer(source.one("478"), default=1),
                _selector_integer(source, "51"),
            )
    elif route_operation is EngineOperation.PERSISTENT_ITEM:
        if not _boolean(source.one("492")):
            item_id = _selector_integer(source, "80")
            if item_id > 0:
                target = timer_item_ids if _boolean(source.one("494")) else normal_item_ids
                target.add(item_id)
    elif route_operation in {
        EngineOperation.TIME,
        EngineOperation.TIME_EVENT,
        EngineOperation.TIME_CONTROL,
    }:
        item_id = _selector_integer(source, "80")
        if item_id > 0:
            timer_item_ids.add(item_id)


def analyze_level_fx(
    snapshot: ValidatedSnapshot,
    catalogue: ValidatedCatalogue,
    capabilities: dict[str, Any],
    *,
    capture_fx: bool = True,
    effect_sink: Callable[[EffectApplication], None] | None = None,
    compute_mech_closure: bool = True,
    capture_diag: bool = True,
) -> LevelEffectAnalysis:


    instances = _parse_instances(snapshot, catalogue)
    capability_by_id = _capabilities_by_id(capabilities)
    group_members: dict[int, list[ParsedLevelInstance]] = {}
    group_parents: dict[int, ParsedLevelInstance] = {}
    collide_blocks_by_id: dict[int, list[ParsedLevelInstance]] = {}
    areas_by_fx: dict[int, list[ParsedLevelInstance]] = {}
    followers_by_ctl: dict[int, list[ParsedLevelInstance]] = {}
    markers_by_seq: dict[int, list[ParsedLevelInstance]] = {}
    for instance in instances:
        for group_id in _groups(instance.one("57")):
            group_members.setdefault(group_id, []).append(instance)
        for group_id in _group_parent_ids(instance):

            group_parents.setdefault(group_id, instance)
        if instance.runtime_type == "CollisionObject":
            block_id = _integer(instance.one("80"))
            if block_id:
                collide_blocks_by_id.setdefault(block_id, []).append(instance)
        if instance.object_id in _AREA_BASE_IDS:
            areas_by_fx.setdefault(
                _selector_integer(instance, "225"), []
            ).append(instance)
        if instance.object_id == 3016:
            followers_by_ctl.setdefault(
                _selector_integer(instance, "534"), []
            ).append(instance)
        if instance.object_id == 3032:
            markers_by_seq.setdefault(
                _integer(instance.one("373")), []
            ).append(instance)
    layout_roots: set[StateAtom] = set()
    layout_uids: set[str] = set()
    root_obj_exclusions: list[dict[str, Any]] = (
        [] if capture_diag else _DiscardingList()
    )
    for instance in instances:
        capability = capability_by_id.get(instance.object_id)
        if capability is None:
            raise SnapValidError(
                f"asset effect ledger lacks captured ID {instance.object_id}"
            )
        if capability["intrinsic_layout_capability"] != "possible":
            continue
        if not _is_root_layout_obj(instance):
            root_obj_exclusions.append(
                {
                    "sample_local_uid": instance.uid,
                    "serialization_order_index": instance.serialization_index,
                    "object_id": instance.object_id,
                    "runtime_object_type_name": instance.runtime_type,
                    "reason": "property_121_no_touch_removes_inherent_contact_behavior",
                }
            )
            continue
        layout_uids.add(instance.uid)
        layout_roots.update(
            {
                StateAtom(instance.uid, "mechanical_identity"),
                StateAtom(instance.uid, "enabled"),
                StateAtom(instance.uid, "world_position.x"),
                StateAtom(instance.uid, "world_position.y"),
                StateAtom(instance.uid, "rotation"),
                StateAtom(instance.uid, "scale.x"),
                StateAtom(instance.uid, "scale.y"),
            }
        )
        if instance.is_speed_object:
            layout_roots.add(
                StateAtom(instance.uid, "gameplay_world_position.y")
            )

    gameplay_roots = {
        StateAtom("__level__", "global_time_rate"),
        StateAtom("__level__", "player_gravity"),
        StateAtom("__level__", "player_position.x"),
        StateAtom("__level__", "player_position.y"),
        StateAtom("__level__", "player_velocity"),
        StateAtom("__level__", "player_movement"),
        StateAtom("__level__", "gameplay_coordinate_frame"),
        StateAtom("__level__", "player_mode_bounds"),
        StateAtom("__level__", "player_control_state"),
        StateAtom("__level__", "points"),
        StateAtom("__level__", "respawn_state"),
        StateAtom("__level__", "level_completion"),
    }
    true_layout_atoms = layout_roots | gameplay_roots

    effects = _EffectCollector(
        true_layout_atoms,
        capture_fx=capture_fx,
        effect_sink=effect_sink,
        compute_closure=compute_mech_closure,
    )
    suppressed: list[dict[str, Any]] = (
        [] if capture_diag else _DiscardingList()
    )
    unsupported: list[dict[str, Any]] = []
    visual_ops: list[dict[str, Any]] = (
        [] if capture_diag else _DiscardingList()
    )
    normal_item_ids: set[int] = set()
    timer_item_ids: set[int] = set()
    pending_all_items: list[ParsedLevelInstance] = []
    enter_members: dict[int, list[ParsedLevelInstance]] = {}
    for instance in instances:
        enter_channel = _selector_integer(instance, "343")
        if enter_channel > 0:
            enter_members.setdefault(enter_channel, []).append(instance)

    settings = _level_settings(snapshot)
    spawn_group = _integer(settings.get("kA36"))
    platformer_mode = _boolean(settings.get("kA22"))
    if spawn_group > 0:
        spawn_anchors = _one_obj_members(
            spawn_group,
            group_members=group_members,
            group_parents=group_parents,
        )
        for anchor in spawn_anchors:
            coordinates = ("x", "y") if platformer_mode else ("y",)
            for coordinate in coordinates:
                effects.append(
                    EffectApplication(
                        effect_id=(
                            f"level-settings:spawn-group:{spawn_group}:"
                            f"{anchor.uid}:player_position.{coordinate}"
                        ),
                        source_uid=anchor.uid,
                        effect_kind="level_spawn_group_position",
                        inputs=(
                            StateAtom(anchor.uid, "mechanical_identity"),
                            StateAtom(
                                anchor.uid, f"world_position.{coordinate}"
                            ),
                        ),
                        outputs=(
                            StateAtom(
                                "__level__", f"player_position.{coordinate}"
                            ),
                        ),
                        selector_evidence=(
                            "level_setting:kA36",
                            f"spawn_group:{spawn_group}",
                            f"target_member:{anchor.uid}",
                            f"platformer_mode:{int(platformer_mode)}",
                        ),
                    )
                )

    execution_sources = list(instances)
    queued_exec_states = {
        (instance.uid, instance.activation_state) for instance in instances
    }

    def activation_state_for(
        target: ParsedLevelInstance,
        context: tuple[tuple[tuple[int, int], ...], ...],
        root_uid: str | None,
    ) -> str:
        state = _acti_stat_name(context, root_uid)
        key = (target.uid, state)
        if context and key not in queued_exec_states:
            queued_exec_states.add(key)
            execution_sources.append(
                replace(
                    target,
                    activation_state=state,
                    remap_context=context,
                    remap_root_uid=root_uid,
                )
            )
        return state

    for source in execution_sources:
        capability = capability_by_id[source.object_id]
        route = route_fx_cap(capability)
        _trac_type_item_ids(
            source,
            route.operation,
            normal_item_ids,
            timer_item_ids,
        )

        if not route.mechanical_candidate:
            if route.declared_visual_fx:
                visual_ops.append(
                    {
                        "source_uid": source.uid,
                        "serialization_order_index": source.serialization_index,
                        "object_id": source.object_id,
                        "engine_operation_name": route.operation_name,
                        "outputs": list(route.declared_visual_fx),
                    }
                )
            continue

        if not route.needs_obj_expand or (
            route.root_layout_cand
            and not source.is_trigger
            and route.operation_name.startswith("runtime:")
        ):
            continue

        if route.operation is EngineOperation.UNSUPPORTED:
            unsupported.append(
                {
                    "serialization_order_index": source.serialization_index,
                    "object_id": source.object_id,
                    "blocker_kind": "unsupported_concrete_effect",
                    "effect_kind": route.operation_name,
                    "unsupported_property": "configured_effect_outputs",
                    "evidence_required": "operation-level 2.2081 effect expander",
                }
            )
            continue

        if route.operation is EngineOperation.INTRINSIC_ONLY:
            continue

        if route.operation is EngineOperation.MOVE:
            target_group = _selector_integer(source, "51")
            for target in group_members.get(target_group, []):
                execution_input = (
                    (StateAtom(source.uid, "effect_execution_state"),)
                    if source.is_stoppable_trigger
                    else ()
                )
                expansion = expa_move_fx_2_2081(
                    source_uid=source.uid,
                    target=EffectTargetFacts(
                        target.uid, target.is_trigger, target.is_speed_object
                    ),
                    move_x=_number(source.one("28")),
                    move_y=_number(source.one("29")),
                    lock_player_x=_boolean(source.one("58")),
                    lock_player_y=_boolean(source.one("59")),
                    lock_camera_x=_boolean(source.one("141")),
                    lock_camera_y=_boolean(source.one("142")),
                    selector_evidence=(
                        f"target_group:{target_group}",
                        f"target_member:{target.uid}",
                    ),
                    extra_inputs=execution_input,
                    activation_property=source.activation_state,
                )
                effects.extend(expansion.effects)
                for component in expansion.blocked_parts:
                    suppressed.append(
                        {
                            "source_uid": source.uid,
                            "source_serialization_index": source.serialization_index,
                            "target_uid": target.uid,
                            "target_serialization_index": target.serialization_index,
                            "target_object_id": target.object_id,
                            "component": component,
                        }
                    )

        elif route.operation is EngineOperation.FOLLOW:
            target_group = _selector_integer(source, "51")
            follow_group = _selector_integer(source, "71")
            centers = _main_object_members(
                follow_group,
                group_members=group_members,
                group_parents=group_parents,
            )
            if not centers:
                continue
            center = centers[0]
            follow_x = _number(source.one("72"), default=1.0)
            follow_y = _number(source.one("73"), default=1.0)
            if follow_x == 0.0 and follow_y == 0.0:
                continue
            execution_input = (
                (StateAtom(source.uid, "effect_execution_state"),)
                if source.is_stoppable_trigger
                else ()
            )
            center_inputs = (
                StateAtom(center.uid, "world_position.x"),
                StateAtom(center.uid, "world_position.y"),
                *execution_input,
            )
            for target in group_members.get(target_group, []):
                expansion = expa_move_fx_2_2081(
                    source_uid=source.uid,
                    target=EffectTargetFacts(
                        target.uid, target.is_trigger, target.is_speed_object
                    ),
                    move_x=follow_x,
                    move_y=follow_y,
                    selector_evidence=(
                        f"target_group:{target_group}",
                        f"target_member:{target.uid}",
                        f"follow_group:{follow_group}",
                        f"main_follow_member:{center.uid}",
                    ),
                    extra_inputs=center_inputs,
                    effect_kind="follow",
                    activation_property=source.activation_state,
                )
                effects.extend(expansion.effects)
                for component in expansion.blocked_parts:
                    suppressed.append(
                        {
                            "source_uid": source.uid,
                            "source_serialization_index": source.serialization_index,
                            "target_uid": target.uid,
                            "target_serialization_index": target.serialization_index,
                            "target_object_id": target.object_id,
                            "component": f"follow:{component}",
                        }
                    )

        elif route.operation is EngineOperation.FOLLOW_PLAYER_Y:
            target_group = _selector_integer(source, "51")
            follow_speed = _number(source.one("90"))
            if follow_speed <= 0.0:
                continue
            execution_input = (
                (StateAtom(source.uid, "effect_execution_state"),)
                if source.is_stoppable_trigger
                else ()
            )
            for target in group_members.get(target_group, []):
                expansion = expa_move_fx_2_2081(
                    source_uid=source.uid,
                    target=EffectTargetFacts(
                        target.uid, target.is_trigger, target.is_speed_object
                    ),
                    move_x=0.0,
                    move_y=follow_speed,
                    selector_evidence=(
                        f"target_group:{target_group}",
                        f"target_member:{target.uid}",
                        f"follow_speed:{follow_speed}",
                    ),
                    extra_inputs=(
                        StateAtom("__level__", "player_position.y"),
                        *execution_input,
                    ),
                    effect_kind="follow_player_y",
                    activation_property=source.activation_state,
                )
                effects.extend(expansion.effects)

        elif route.operation is EngineOperation.ROTATE:
            target_group = _selector_integer(source, "51")
            center_group = _selector_integer(source, "71")
            targets = group_members.get(target_group, [])
            if not targets:
                continue
            aim_mode = _boolean(source.one("100"))
            follow_mode = _boolean(source.one("394"))
            tracking_mode = aim_mode or follow_mode
            centers = (
                _one_obj_members(
                    center_group,
                    group_members=group_members,
                    group_parents=group_parents,
                )
                if center_group
                else ()
            )

            if center_group and not centers:
                continue
            angle = _number(source.one("68")) + 360.0 * _number(source.one("69"))
            if not tracking_mode and angle == 0.0:
                continue
            tracking_inputs: tuple[StateAtom, ...] = ()
            tracking_evidence: tuple[str, ...] = ()
            if tracking_mode:
                (
                    tracking_inputs,
                    tracking_evidence,
                    has_tracking_target,
                ) = _rotate_tracking_in(
                    source,
                    follow_mode=follow_mode,
                    group_members=group_members,
                    group_parents=group_parents,
                )

                if not has_tracking_target:
                    continue
            limit_inputs, limit_evidence = _rotate_limit_inputs(
                source,
                group_members=group_members,
                group_parents=group_parents,
            )
            lock_object_rotation = _boolean(source.one("70"))
            for target in targets:
                possible_centers = centers if centers else (target,)
                target_space_in = _spatial_input_atoms(target)
                center_space_in = tuple(
                    atom
                    for center in possible_centers
                    for atom in _spatial_input_atoms(center)
                )
                position_inputs = (
                    *target_space_in,
                    *center_space_in,
                    *tracking_inputs,
                    *limit_inputs,
                )
                rotation_inputs = (
                    StateAtom(target.uid, "rotation"),

                    *tuple(
                        target_space_in
                        if aim_mode
                        else ()
                    ),
                    *tracking_inputs,
                    *limit_inputs,
                )
                effect_kind = (
                    "rotate_follow" if follow_mode else "rotate_aim"
                ) if tracking_mode else "rotate"
                evidence = (
                    f"target_group:{target_group}",
                    f"target_member:{target.uid}",
                    f"center_group:{center_group}",
                    *tuple(
                        f"reachable_center_member:{center.uid}"
                        for center in possible_centers
                    ),
                    f"mode:{effect_kind}",
                    f"dynamic:{int(_boolean(source.one('397')))}",
                    *tracking_evidence,
                    *limit_evidence,
                    *(() if tracking_mode else (f"angle:{angle}",)),
                )
                outputs: list[str] = []
                if center_group:
                    outputs.extend(("world_position.x", "world_position.y"))
                if not lock_object_rotation:
                    outputs.append("rotation")
                for output in outputs:
                    effects.append(
                        _effect(
                            source=source,
                            target_uid=target.uid,
                            kind=effect_kind,
                            output=output,
                            inputs=(
                                rotation_inputs
                                if output == "rotation"
                                else position_inputs
                            ),
                            selector_evidence=evidence,
                        )
                    )

        elif route.operation is EngineOperation.SCALE:
            target_group = _selector_integer(source, "51")
            center_group = _selector_integer(source, "71")
            targets = group_members.get(target_group, [])
            if not targets:
                continue
            centers = (
                _one_obj_members(
                    center_group,
                    group_members=group_members,
                    group_parents=group_parents,
                )
                if center_group
                else ()
            )
            if center_group and not centers:
                continue
            scale_x = _number(source.one("150"), default=1.0)
            scale_y = _number(source.one("151"), default=1.0)
            only_move = _boolean(source.one("133"))
            divide_x = _boolean(source.one("153"))
            divide_y = _boolean(source.one("154"))
            relative_rotation = _boolean(source.one("452"))
            relative_scale = _boolean(source.one("577"))
            for target in targets:
                possible_centers = centers if centers else (target,)
                inputs = [
                    StateAtom(target.uid, "world_position.x"),
                    StateAtom(target.uid, "world_position.y"),
                    StateAtom(target.uid, "scale.x"),
                    StateAtom(target.uid, "scale.y"),
                ]
                if center_group:
                    for center in possible_centers:
                        inputs.extend(
                            (
                                StateAtom(center.uid, "world_position.x"),
                                StateAtom(center.uid, "world_position.y"),
                            )
                        )
                    if relative_rotation:
                        inputs.extend(
                            StateAtom(center.uid, "rotation")
                            for center in possible_centers
                        )
                    if relative_scale:
                        for center in possible_centers:
                            inputs.extend(
                                (
                                    StateAtom(center.uid, "scale.x"),
                                    StateAtom(center.uid, "scale.y"),
                                )
                            )
                evidence = (
                    f"target_group:{target_group}",
                    f"target_member:{target.uid}",
                    f"center_group:{center_group}",
                    *tuple(
                        f"reachable_center_member:{center.uid}"
                        for center in possible_centers
                    ),
                    f"divide_x:{int(divide_x)}",
                    f"divide_y:{int(divide_y)}",
                    f"relative_rotation:{int(relative_rotation)}",
                    f"relative_scale:{int(relative_scale)}",
                )
                outputs: list[str] = []
                if center_group:
                    outputs.extend(("world_position.x", "world_position.y"))
                if not only_move and scale_x != 1.0:
                    outputs.append("scale.x")
                if not only_move and scale_y != 1.0:
                    outputs.append("scale.y")
                for output in outputs:
                    effects.append(
                        _effect(
                            source=source,
                            target_uid=target.uid,
                            kind="scale",
                            output=output,
                            inputs=inputs,
                            selector_evidence=evidence,
                        )
                        )

        elif route.operation is EngineOperation.AREA_TRANSFORM:
            target_group = _selector_integer(source, "51")
            targets = group_members.get(target_group, [])
            center_inputs, center_evidence = _area_center_inputs(
                source,
                group_members=group_members,
                group_parents=group_parents,
            )
            area_configuration = StateAtom(source.uid, "area_runtime_configuration")
            for target in targets:
                target_inputs = _spatial_input_atoms(target)
                common_inputs = (
                    area_configuration,
                    *center_inputs,
                    *target_inputs,
                )
                evidence = (
                    f"target_group:{target_group}",
                    f"target_member:{target.uid}",
                    *center_evidence,
                    f"effect_id:{_selector_integer(source, '225')}",
                )
                if route.operation_name == "Area Move":
                    move_x, move_y = _area_move_parts(source)
                    execution_input = (
                        (StateAtom(source.uid, "effect_execution_state"),)
                        if source.is_stoppable_trigger
                        else ()
                    )
                    expansion = expa_move_fx_2_2081(
                        source_uid=source.uid,
                        target=EffectTargetFacts(
                            target.uid, target.is_trigger, target.is_speed_object
                        ),
                        move_x=float(move_x),
                        move_y=float(move_y),
                        selector_evidence=evidence,
                        extra_inputs=(*common_inputs, *execution_input),
                        effect_kind="area_move",
                        activation_property=source.activation_state,
                    )
                    effects.extend(expansion.effects)
                    for component in expansion.blocked_parts:
                        suppressed.append(
                            {
                                "source_uid": source.uid,
                                "source_serialization_index": source.serialization_index,
                                "target_uid": target.uid,
                                "target_serialization_index": target.serialization_index,
                                "target_object_id": target.object_id,
                                "component": f"area_move:{component}",
                            }
                        )
                elif route.operation_name == "Area Rotate":
                    if not (
                        _number(source.one("270"))
                        or _number(source.one("271"))
                    ):
                        continue
                    effects.append(
                        _effect(
                            source=source,
                            target_uid=target.uid,
                            kind="area_rotate",
                            output="rotation",
                            inputs=common_inputs,
                            selector_evidence=evidence,
                        )
                    )
                elif route.operation_name == "Area Scale":
                    scale_x = _number(source.one("233"), default=1.0)
                    scale_y = _number(source.one("235"), default=1.0)
                    if scale_x != 1.0 or _number(source.one("234")):
                        effects.append(
                            _effect(
                                source=source,
                                target_uid=target.uid,
                                kind="area_scale_x",
                                output="scale.x",
                                inputs=common_inputs,
                                selector_evidence=evidence,
                            )
                        )
                    if scale_y != 1.0 or _number(source.one("236")):
                        effects.append(
                            _effect(
                                source=source,
                                target_uid=target.uid,
                                kind="area_scale_y",
                                output="scale.y",
                                inputs=common_inputs,
                                selector_evidence=evidence,
                            )
                        )

        elif route.operation is EngineOperation.EDIT_AREA_TRANSFORM:
            selector = _selector_integer(source, "51")
            if _boolean(source.one("355")):
                selected_areas = areas_by_fx.get(selector, [])
                selector_evidence = (f"effect_id:{selector}",)
            else:
                selected_areas = [
                    candidate
                    for candidate in group_members.get(selector, [])
                    if candidate.object_id in _AREA_BASE_IDS
                ]
                selector_evidence = (f"area_group:{selector}",)
            for area in selected_areas:
                effects.append(
                    _effect(
                        source=source,
                        target_uid=area.uid,
                        kind="edit_area_runtime_configuration",
                        output="area_runtime_configuration",
                        selector_evidence=(
                            *selector_evidence,
                            f"area_member:{area.uid}",
                            f"area_object_id:{area.object_id}",
                        ),
                    )
                )

        elif route.operation is EngineOperation.AREA_STOP:
            effect_id = _selector_integer(source, "51")
            for area in areas_by_fx.get(effect_id, []):
                effects.append(
                    _effect(
                        source=source,
                        target_uid=area.uid,
                        kind="area_stop",
                        output="effect_execution_state",
                        selector_evidence=(
                            f"effect_id:{effect_id}",
                            f"area_member:{area.uid}",
                            f"area_object_id:{area.object_id}",
                        ),
                    )
                )

        elif route.operation is EngineOperation.ADVANCED_FOLLOW:
            target_group = _selector_integer(source, "51")
            follow_inputs, follow_evidence = _adv_foll_targ_in(
                source,
                group_members=group_members,
                group_parents=group_parents,
            )
            reference_inputs = _adv_follow_ref_in(
                source,
                group_members=group_members,
                group_parents=group_parents,
            )
            follow_x = not _boolean(source.one("307"))
            follow_y = not _boolean(source.one("306"))
            for target in group_members.get(target_group, []):
                execution_input = (
                    (StateAtom(source.uid, "effect_execution_state"),)
                    if source.is_stoppable_trigger
                    else ()
                )
                inputs = (
                    StateAtom(source.uid, "advanced_follow_target_state"),
                    StateAtom(target.uid, "advanced_follow_physics_state"),
                    *_spatial_input_atoms(target),
                    *follow_inputs,
                    *reference_inputs,
                    *execution_input,
                )
                evidence = (
                    f"target_group:{target_group}",
                    f"target_member:{target.uid}",
                    *follow_evidence,
                    f"control_id:{_selector_integer(source, '534')}",
                )
                expansion = expa_move_fx_2_2081(
                    source_uid=source.uid,
                    target=EffectTargetFacts(
                        target.uid, target.is_trigger, target.is_speed_object
                    ),
                    move_x=float(follow_x),
                    move_y=float(follow_y),
                    selector_evidence=evidence,
                    extra_inputs=inputs,
                    effect_kind="advanced_follow",
                    activation_property=source.activation_state,
                )
                effects.extend(expansion.effects)
                for component in expansion.blocked_parts:
                    suppressed.append(
                        {
                            "source_uid": source.uid,
                            "source_serialization_index": source.serialization_index,
                            "target_uid": target.uid,
                            "target_serialization_index": target.serialization_index,
                            "target_object_id": target.object_id,
                            "component": f"advanced_follow:{component}",
                        }
                    )
                if _boolean(source.one("339")):
                    effects.append(
                        _effect(
                            source=source,
                            target_uid=target.uid,
                            kind="advanced_follow_rotation",
                            output="rotation",
                            inputs=inputs,
                            selector_evidence=evidence,
                        )
                    )

        elif route.operation is EngineOperation.EDIT_ADVANCED_FOLLOW:
            selector = _selector_integer(source, "51")
            if _boolean(source.one("535")):
                selected_followers = followers_by_ctl.get(selector, [])
                target_groups = [
                    _selector_integer(candidate, "51")
                    for candidate in selected_followers
                ]
                selector_evidence = (
                    f"target_control_id:{selector}",
                    *tuple(
                        f"advanced_follow_member:{candidate.uid}"
                        for candidate in selected_followers
                    ),
                )
            else:
                target_groups = [selector]
                selector_evidence = (f"target_group:{selector}",)
            reference_inputs = _adv_follow_ref_in(
                source,
                group_members=group_members,
                group_parents=group_parents,
            )
            seen_targets: set[str] = set()
            for target_group in target_groups:
                for target in group_members.get(target_group, []):
                    if target.uid in seen_targets:
                        continue
                    seen_targets.add(target.uid)
                    effects.append(
                        _effect(
                            source=source,
                            target_uid=target.uid,
                            kind="edit_advanced_follow_physics",
                            output="advanced_follow_physics_state",
                            inputs=reference_inputs,
                            selector_evidence=(
                                *selector_evidence,
                                f"resolved_target_group:{target_group}",
                                f"target_member:{target.uid}",
                            ),
                        )
                    )

        elif route.operation is EngineOperation.RETARGET_ADV_FOLLOW:
            selector = _selector_integer(source, "51")
            if _boolean(source.one("535")):
                selected_followers = followers_by_ctl.get(selector, [])
                selector_evidence = (f"target_control_id:{selector}",)
            else:
                selected_followers = [
                    candidate
                    for candidate in group_members.get(selector, [])
                    if candidate.object_id == 3016
                ]
                selector_evidence = (f"advanced_follow_group:{selector}",)
            follow_inputs, follow_evidence = _adv_foll_targ_in(
                source,
                group_members=group_members,
                group_parents=group_parents,
            )
            for follower in selected_followers:
                effects.append(
                    _effect(
                        source=source,
                        target_uid=follower.uid,
                        kind="retarget_advanced_follow",
                        output="advanced_follow_target_state",
                        inputs=follow_inputs,
                        selector_evidence=(
                            *selector_evidence,
                            f"advanced_follow_member:{follower.uid}",
                            *follow_evidence,
                        ),
                    )
                )

        elif route.operation is EngineOperation.CAMERA_TRANSLATION:
            inputs: tuple[StateAtom, ...] = ()
            evidence: tuple[str, ...] = ()
            if route.operation_name == "Static Camera" and not _boolean(
                source.one("110")
            ):
                target_group = _selector_integer(source, "71")
                targets = _main_object_members(
                    target_group,
                    group_members=group_members,
                    group_parents=group_parents,
                )
                if not targets:
                    continue
                inputs = tuple(
                    atom for target in targets for atom in _spatial_input_atoms(target)
                )
                evidence = (
                    f"camera_target_group:{target_group}",
                    *tuple(f"main_camera_target:{target.uid}" for target in targets),
                )
            elif route.operation_name == "Camera Edge":
                target_group = _selector_integer(source, "51")
                targets = _main_object_members(
                    target_group,
                    group_members=group_members,
                    group_parents=group_parents,
                )
                inputs = tuple(
                    atom for target in targets for atom in _spatial_input_atoms(target)
                )
                edge = _integer(source.one("164"))
                components = (
                    ("camera_position.x",) if edge in {0, 1}
                    else ("camera_position.y",)
                )
                evidence = (
                    f"camera_edge:{edge}",
                    f"camera_target_group:{target_group}",
                    *tuple(f"main_camera_target:{target.uid}" for target in targets),
                )
            else:
                components = _camera_components(source)
            if route.operation_name != "Camera Edge":
                components = _camera_components(source)
            for output in components:
                effects.append(
                    _effect(
                        source=source,
                        target_uid="__level__",
                        kind=f"{route.operation_name.lower().replace(' ', '_')}_{output}",
                        output=output,
                        inputs=inputs,
                        selector_evidence=evidence,
                    )
                )

        elif route.operation is EngineOperation.CAMERA_ZOOM:
            effects.append(
                _effect(
                    source=source,
                    target_uid="__level__",
                    kind="camera_zoom",
                    output="camera_zoom",
                )
            )

        elif route.operation is EngineOperation.CAMERA_TRANSFORM:
            outputs = (
                ("camera_rotation",)
                if route.operation_name == "Camera Rotation"
                else ("camera_position.x", "camera_position.y")
            )
            for output in outputs:
                effects.append(
                    _effect(
                        source=source,
                        target_uid="__level__",
                        kind=f"camera_transform_{output}",
                        output=output,
                    )
                )

        elif route.operation is EngineOperation.TOGGLE:
            target_group = _selector_integer(source, "51")
            for target in group_members.get(target_group, []):
                effects.append(
                    _effect(
                        source=source,
                        target_uid=target.uid,
                        kind="toggle",
                        output="enabled",
                        selector_evidence=(
                            f"target_group:{target_group}",
                            f"target_member:{target.uid}",
                            f"enabled_value:{int(_boolean(source.one('56')))}",
                        ),
                    )
                )

        elif route.operation in {EngineOperation.TOUCH, EngineOperation.ON_DEATH}:
            target_group = _selector_integer(source, "51")
            if route.operation is EngineOperation.TOUCH:
                mode = _integer(source.one("82"))
                activate = mode != 2
                kind = "touch_group_command"
                mode_evidence = f"touch_mode:{mode}"
            else:
                activate = _boolean(source.one("56"))
                kind = "on_death_group_command"
                mode_evidence = f"activate_group:{int(activate)}"
            effects.extend(
                _group_cmd_fx(
                    source=source,
                    target_group=target_group,
                    activate=activate,
                    group_members=group_members,
                    kind=kind,
                    selector_evidence=(mode_evidence,),
                    activation_state_for=activation_state_for,
                )
            )

        elif route.operation is EngineOperation.SPAWN:
            target_group = _selector_integer(source, "51")
            own_remap = _integer_dot_pairs(source.one("442"))
            if own_remap:
                reset_remap = _boolean(source.one("581"))
                child_context = (own_remap,)
                if source.remap_context and not reset_remap:
                    child_context += source.remap_context
                child_root_uid = (
                    source.uid
                    if reset_remap or source.remap_root_uid is None
                    else source.remap_root_uid
                )
            else:

                child_context = ()
                child_root_uid = None
            effects.extend(
                _spawn_group_effects(
                    source=source,
                    target_group=target_group,
                    group_members=group_members,
                    kind=f"spawn_group_{target_group}",
                    selector_evidence=(
                        f"delay:{_number(source.one('63'))}",
                        f"ordered:{int(_boolean(source.one('441')))}",
                        f"remap_pairs:{len(own_remap)}",
                        f"reset_remap:{int(_boolean(source.one('581')))}",
                    ),
                    activation_context=child_context,
                    activation_root_uid=child_root_uid,
                    activation_state_for=activation_state_for,
                )
            )

        elif route.operation is EngineOperation.RANDOM:
            chance = _number(source.one("10"))
            branches = (
                (_selector_integer(source, "51"), chance, "primary"),
                (_selector_integer(source, "71"), 100.0 - chance, "secondary"),
            )
            for target_group, weight, branch in branches:
                if not target_group or weight <= 0.0:
                    continue
                effects.extend(
                    _spawn_group_effects(
                        source=source,
                        target_group=target_group,
                        group_members=group_members,
                        kind=f"random_{branch}_group_{target_group}",
                        selector_evidence=(f"branch_weight:{weight}",),
                        activation_state_for=activation_state_for,
                    )
                )

        elif route.operation is EngineOperation.ADVANCED_RANDOM:
            branches = _dot_pairs(source.one("152"))


            if source.remap_context:
                branches = tuple(
                    (_remap_id(target_group, source.remap_context), weight)
                    for target_group, weight in branches
                )
            for branch_index, (target_group, weight) in enumerate(branches):
                if not target_group or weight <= 0.0:
                    continue
                effects.extend(
                    _spawn_group_effects(
                        source=source,
                        target_group=target_group,
                        group_members=group_members,
                        kind=f"advanced_random_{branch_index}_group_{target_group}",
                        selector_evidence=(f"branch_weight:{weight}",),
                        activation_state_for=activation_state_for,
                    )
                )

        elif route.operation is EngineOperation.SEQUENCE:
            for branch_index, (target_group, count) in enumerate(
                _dot_pairs(source.one("435"))
            ):
                if not target_group or count <= 0.0:
                    continue
                effects.extend(
                    _spawn_group_effects(
                        source=source,
                        target_group=target_group,
                        group_members=group_members,
                        kind=f"sequence_{branch_index}_group_{target_group}",
                        selector_evidence=(
                            f"count:{count}",
                            f"mode:{_integer(source.one('436'))}",
                        ),
                        activation_state_for=activation_state_for,
                    )
                )

        elif route.operation is EngineOperation.EVENT:
            target_group = _selector_integer(source, "51")
            event_ids = _groups(source.one("430"))
            effects.extend(
                _spawn_group_effects(
                    source=source,
                    target_group=target_group,
                    group_members=group_members,
                    kind=f"event_group_{target_group}",
                    selector_evidence=tuple(
                        f"event_id:{event_id}" for event_id in event_ids
                    ),
                    activation_state_for=activation_state_for,
                )
            )

        elif route.operation in {
            EngineOperation.COLLISION,
            EngineOperation.INSTANT_COLLISION,
            EngineOperation.COLLISION_STATE,
        }:
            collision_inputs, collision_evidence = _collision_inputs(
                source, collide_blocks_by_id=collide_blocks_by_id
            )
            if route.operation is EngineOperation.COLLISION:
                target_group = _selector_integer(source, "51")
                effects.extend(
                    _group_cmd_fx(
                        source=source,
                        target_group=target_group,
                        activate=_boolean(source.one("56")),
                        group_members=group_members,
                        kind=f"collision_group_{target_group}",
                        inputs=collision_inputs,
                        selector_evidence=(
                            *collision_evidence,
                            f"trigger_on_exit:{int(_boolean(source.one('93')))}",
                        ),
                        activation_state_for=activation_state_for,
                    )
                )
            elif route.operation is EngineOperation.INSTANT_COLLISION:


                for branch, target_group in (
                    ("true", _selector_integer(source, "51")),
                    ("false", _selector_integer(source, "71")),
                ):
                    if not target_group:
                        continue
                    effects.extend(
                        _spawn_group_effects(
                            source=source,
                            target_group=target_group,
                            group_members=group_members,
                            kind=f"instant_collision_{branch}_group_{target_group}",
                            inputs=collision_inputs,
                            selector_evidence=(
                                *collision_evidence,
                                f"comparison_branch:{branch}",
                            ),
                            activation_state_for=activation_state_for,
                        )
                    )
            else:

                state_inputs = (
                    *_spatial_input_atoms(source),
                    StateAtom(source.uid, "enabled"),
                    StateAtom(source.uid, "rotation"),
                    StateAtom(source.uid, "scale.x"),
                    StateAtom(source.uid, "scale.y"),
                    StateAtom("__level__", "player_position.x"),
                    StateAtom("__level__", "player_position.y"),
                    StateAtom("__level__", "player2_position.x"),
                    StateAtom("__level__", "player2_position.y"),
                )
                for phase, target_group in (
                    ("enter", _selector_integer(source, "51")),
                    ("exit", _selector_integer(source, "71")),
                ):
                    if not target_group:
                        continue
                    effects.extend(
                        _spawn_group_effects(
                            source=source,
                            target_group=target_group,
                            group_members=group_members,
                            kind=f"collision_state_{phase}_group_{target_group}",
                            inputs=state_inputs,
                            selector_evidence=("tested_player:active_players",),
                            activation_state_for=activation_state_for,
                        )
                    )

        elif route.operation in {
            EngineOperation.COUNT,
            EngineOperation.INSTANT_COUNT,
        }:
            item_id = _selector_integer(source, "80")
            target_group = _selector_integer(source, "51")
            item_inputs = (
                StateAtom(_item_uid(item_id), "value"),
                StateAtom(_item_uid(item_id), "persistence"),
            )
            effects.extend(
                _group_cmd_fx(
                    source=source,
                    target_group=target_group,
                    activate=_boolean(source.one("56")),
                    group_members=group_members,
                    kind=f"{route.operation.value}_group_{target_group}",
                    inputs=item_inputs,
                    selector_evidence=(f"item_id:{item_id}",),
                    activation_state_for=activation_state_for,
                )
            )

        elif route.operation is EngineOperation.ITEM_COMPARE:
            item1_id = _selector_integer(source, "80")
            item2_id = _selector_integer(source, "95")
            item1_mode = _integer(source.one("476"))
            item2_mode = _integer(source.one("477"))
            item_inputs = tuple(
                dict.fromkeys(
                    (
                        *_item_value_atoms(item1_mode, item1_id),
                        *_item_value_atoms(item2_mode, item2_id),
                    )
                )
            )
            for branch, target_group in (
                ("true", _selector_integer(source, "51")),
                ("false", _selector_integer(source, "71")),
            ):
                if not target_group:
                    continue
                effects.extend(
                    _spawn_group_effects(
                        source=source,
                        target_group=target_group,
                        group_members=group_members,
                        kind=f"item_compare_{branch}_group_{target_group}",
                        inputs=item_inputs,
                        selector_evidence=(
                            f"item1_mode:{item1_mode}",
                            f"item1_id:{item1_id}",
                            f"item2_mode:{item2_mode}",
                            f"item2_id:{item2_id}",
                            f"comparison_branch:{branch}",
                        ),
                        activation_state_for=activation_state_for,
                    )
                )

        elif route.operation is EngineOperation.ITEM_EDIT:
            item1_id = _selector_integer(source, "80")
            item2_id = _selector_integer(source, "95")
            target_id = _selector_integer(source, "51")
            item1_mode = _integer(source.one("476"))
            item2_mode = _integer(source.one("477"))
            target_mode = _integer(source.one("478"), default=1)
            target = _item_target_atom(target_mode, target_id)
            if target is None:
                suppressed.append(
                    {
                        "source_uid": source.uid,
                        "source_serialization_index": source.serialization_index,
                        "target_uid": None,
                        "target_serialization_index": None,
                        "target_object_id": None,
                        "component": "item_edit:no_runtime_target",
                    }
                )
                continue
            item_inputs = [
                *_item_value_atoms(item1_mode, item1_id),
                *_item_value_atoms(item2_mode, item2_id),
            ]


            result_type1 = _integer(source.one("480"))
            if result_type1 != 0:
                item_inputs.append(target)
            effects.append(
                _effect(
                    source=source,
                    target_uid=target.instance_uid,
                    kind="item_edit",
                    output=target.property_name,
                    inputs=tuple(dict.fromkeys(item_inputs)),
                    selector_evidence=(
                        f"item1_mode:{item1_mode}",
                        f"item1_id:{item1_id}",
                        f"item2_mode:{item2_mode}",
                        f"item2_id:{item2_id}",
                        f"target_mode:{target_mode}",
                        f"target_id:{target_id}",
                        f"result_type1:{result_type1}",
                        f"result_type2:{_integer(source.one('481'), default=1)}",
                        f"result_type3:{_integer(source.one('482'), default=3)}",
                    ),
                )
            )

        elif route.operation is EngineOperation.PICKUP:
            item_id = _selector_integer(source, "80")
            if item_id:
                effects.append(
                    _effect(
                        source=source,
                        target_uid=_item_uid(item_id),
                        kind=f"pickup_item_{item_id}",
                        output="value",
                        selector_evidence=(f"item_id:{item_id}",),
                    )
                )

        elif route.operation is EngineOperation.PERSISTENT_ITEM:
            item_id = _selector_integer(source, "80")
            if _boolean(source.one("492")):

                pending_all_items.append(source)
                continue
            if item_id:
                item_uid = _item_uid(item_id, timer=_boolean(source.one("494")))
                outputs = ["persistence"]
                if _boolean(source.one("493")):
                    outputs.append("value")
                for output in outputs:
                    effects.append(
                        _effect(
                            source=source,
                            target_uid=item_uid,
                            kind=f"persistent_item_{item_id}",
                            output=output,
                            selector_evidence=(
                                f"item_id:{item_id}",
                                f"timer:{int(_boolean(source.one('494')))}",
                                f"persistent:{int(_boolean(source.one('491')))}",
                                f"reset:{int(_boolean(source.one('493')))}",
                            ),
                        )
                    )

        elif route.operation is EngineOperation.TIME:
            item_id = _selector_integer(source, "80")
            if item_id:
                for output in ("value", "active", "rate"):
                    effects.append(
                        _effect(
                            source=source,
                            target_uid=_item_uid(item_id, timer=True),
                            kind=f"time_item_{item_id}",
                            output=output,
                            selector_evidence=(
                                f"timer_item_id:{item_id}",
                                f"start_paused:{int(_boolean(source.one('471')))}",
                                f"ignore_time_warp:{int(_boolean(source.one('469')))}",
                            ),
                        )
                    )
            target_group = _selector_integer(source, "51")
            if item_id and target_group and _boolean(source.one("474")):
                effects.extend(
                    _spawn_group_effects(
                        source=source,
                        target_group=target_group,
                        group_members=group_members,
                        kind=f"time_group_{target_group}",
                        inputs=(
                            StateAtom(_item_uid(item_id, timer=True), "value"),
                            StateAtom(_item_uid(item_id, timer=True), "active"),
                            StateAtom(_item_uid(item_id, timer=True), "rate"),
                            StateAtom(_item_uid(item_id, timer=True), "persistence"),
                        ),
                        selector_evidence=(f"stop_enabled:{int(_boolean(source.one('474')))}",),
                        activation_state_for=activation_state_for,
                    )
                )

        elif route.operation is EngineOperation.TIME_EVENT:
            item_id = _selector_integer(source, "80")
            target_group = _selector_integer(source, "51")
            if item_id and target_group:
                timer_uid = _item_uid(item_id, timer=True)
                effects.extend(
                    _spawn_group_effects(
                        source=source,
                        target_group=target_group,
                        group_members=group_members,
                        kind=f"time_event_group_{target_group}",
                        inputs=(
                            StateAtom(timer_uid, "value"),
                            StateAtom(timer_uid, "active"),
                            StateAtom(timer_uid, "rate"),
                            StateAtom(timer_uid, "persistence"),
                        ),
                        selector_evidence=(
                            f"timer_item_id:{item_id}",
                            f"target_time:{source.one('473', '0')}",
                            f"multi_activate:{int(_boolean(source.one('475')))}",
                        ),
                        activation_state_for=activation_state_for,
                    )
                )

        elif route.operation is EngineOperation.TIME_CONTROL:
            item_id = _selector_integer(source, "80")
            control_type = _integer(source.one("472"))
            if item_id and control_type in {0, 1}:
                effects.append(
                    _effect(
                        source=source,
                        target_uid=_item_uid(item_id, timer=True),
                        kind=f"time_control_{'resume' if control_type == 0 else 'pause'}",
                        output="active",
                        selector_evidence=(
                            f"timer_item_id:{item_id}",
                            f"control_type:{control_type}",
                        ),
                    )
                )
            elif item_id:
                suppressed.append(
                    {
                        "source_uid": source.uid,
                        "source_serialization_index": source.serialization_index,
                        "target_uid": _item_uid(item_id, timer=True),
                        "target_serialization_index": None,
                        "target_object_id": None,
                        "component": f"time_control:no_op_type_{control_type}",
                    }
                )

        elif route.operation is EngineOperation.RESET:
            target_group = _selector_integer(source, "51")
            for target in group_members.get(target_group, []):
                if target.object_id != 2063 and target.runtime_type not in {
                    "Collectible",
                    "Breakable",
                }:
                    suppressed.append(
                        {
                            "source_uid": source.uid,
                            "source_serialization_index": source.serialization_index,
                            "target_uid": target.uid,
                            "target_serialization_index": target.serialization_index,
                            "target_object_id": target.object_id,
                            "component": "reset:not_collectible_breakable_or_checkpoint",
                        }
                    )
                    continue
                effects.append(
                    _effect(
                        source=source,
                        target_uid=target.uid,
                        kind="reset_restore_object",
                        output="enabled",
                        selector_evidence=(
                            f"target_group:{target_group}",
                            f"target_member:{target.uid}",
                            f"target_runtime_type:{target.runtime_type}",
                        ),
                    )
                )

        elif route.operation is EngineOperation.PLAYER_CONTROL:

            effects.append(
                _effect(
                    source=source,
                    target_uid="__level__",
                    kind="player_control",
                    output="player_control_state",
                    selector_evidence=(
                        f"target_player1:{int(_boolean(source.one('138')))}",
                        f"target_player2:{int(_boolean(source.one('200')))}",
                        f"stop_jump:{int(_boolean(source.one('540')))}",
                        f"stop_move:{int(_boolean(source.one('541')))}",
                        f"stop_rotation:{int(_boolean(source.one('542')))}",
                        f"stop_slide:{int(_boolean(source.one('543')))}",
                    ),
                )
            )

        elif route.operation is EngineOperation.OPTIONS:
            option_outputs = []
            if _boolean(source.one("160")):
                option_outputs.append("player_gravity")
            if _boolean(source.one("165")) or _boolean(source.one("199")):
                option_outputs.append("player_control_state")
            if _boolean(source.one("573")):
                option_outputs.append("respawn_state")
            if _boolean(source.one("593")):
                option_outputs.append("player_movement")
            for output in dict.fromkeys(option_outputs):
                effects.append(
                    _effect(
                        source=source,
                        target_uid="__level__",
                        kind=f"options_{output}",
                        output=output,
                    )
                )

        elif route.operation is EngineOperation.TELEPORT:
            target_group = _selector_integer(source, "51")
            targets = (
                _one_obj_members(
                    target_group,
                    group_members=group_members,
                    group_parents=group_parents,
                )
                if target_group
                else ()
            )
            if not targets:
                continue
            inputs = tuple(
                atom
                for target in targets
                for atom in (
                    StateAtom(target.uid, "world_position.x"),
                    StateAtom(target.uid, "world_position.y"),
                    StateAtom(target.uid, "rotation"),
                )
            )
            outputs = []
            if not _boolean(source.one("352")):
                outputs.append("player_position.x")
            if not _boolean(source.one("353")):
                outputs.append("player_position.y")
            if _integer(source.one("354")):
                outputs.append("player_gravity")
            if (
                _boolean(source.one("345"))
                or _boolean(source.one("347"))
                or _boolean(source.one("591"))
            ):
                outputs.append("player_velocity")
            for output in outputs:
                effects.append(
                    _effect(
                        source=source,
                        target_uid="__level__",
                        kind=f"teleport_{output}",
                        output=output,
                        inputs=inputs,
                        selector_evidence=(
                            f"target_group:{target_group}",
                            *tuple(
                                f"reachable_target_member:{target.uid}"
                                for target in targets
                            ),
                        ),
                    )
                )

        elif route.operation is EngineOperation.KEYFRAME_ANIMATION:
            animation_group = _selector_integer(source, "76")
            if not animation_group:
                continue

            seed_markers = [
                member
                for member in group_members.get(animation_group, [])
                if member.object_id == 3032
            ]
            processed_seq_ids: set[int] = set()
            for seed in reversed(seed_markers):
                sequence_id = _integer(seed.one("373"))
                if sequence_id in processed_seq_ids:
                    continue
                processed_seq_ids.add(sequence_id)

                markers = markers_by_seq.get(sequence_id, [])
                target_group = _selector_integer(source, "51") or _remap_id(
                    _integer(seed.one("51")), source.remap_context
                )
                targets = group_members.get(target_group, [])
                if not markers or not targets:
                    continue

                parent_group = _selector_integer(source, "71")
                centers = _main_object_members(
                    parent_group,
                    group_members=group_members,
                    group_parents=group_parents,
                )
                marker_inputs = tuple(
                    atom
                    for marker in markers
                    for atom in (
                        StateAtom(marker.uid, "effect_configuration"),
                        StateAtom(marker.uid, "world_position.x"),
                        StateAtom(marker.uid, "world_position.y"),
                        StateAtom(marker.uid, "rotation"),
                        StateAtom(marker.uid, "scale.x"),
                        StateAtom(marker.uid, "scale.y"),
                    )
                )
                center_inputs = tuple(
                    atom
                    for center in centers
                    for atom in (
                        StateAtom(center.uid, "world_position.x"),
                        StateAtom(center.uid, "world_position.y"),
                    )
                )
                position_x = _number(source.one("521"), default=1.0)
                position_y = _number(source.one("545"), default=1.0)
                rotation = _number(source.one("522"), default=1.0)
                scale_x = _number(source.one("523"), default=1.0)
                scale_y = _number(source.one("546"), default=1.0)
                outputs: list[str] = []
                if position_x != 0.0:
                    outputs.append("world_position.x")
                if position_y != 0.0:
                    outputs.append("world_position.y")
                if rotation != 0.0:
                    outputs.append("rotation")
                if scale_x != 0.0:
                    outputs.append("scale.x")
                if scale_y != 0.0:
                    outputs.append("scale.y")
                if centers and (rotation != 0.0 or scale_x != 0.0 or scale_y != 0.0):
                    outputs.extend(("world_position.x", "world_position.y"))

                evidence = (
                    f"animation_group:{animation_group}",
                    f"seed_marker:{seed.uid}",
                    f"keyframe_sequence:{sequence_id}",
                    f"target_group:{target_group}",
                    f"parent_group:{parent_group}",
                    *tuple(f"sequence_marker:{marker.uid}" for marker in markers),
                    *tuple(f"main_parent:{center.uid}" for center in centers),
                )
                sequence_state = (
                    f"keyframe_sequence:{sequence_id}:{source.activation_state}"
                )
                sequence_atom = StateAtom(source.uid, sequence_state)
                effects.append(
                    _effect(
                        source=source,
                        target_uid=source.uid,
                        kind=f"keyframe_sequence_{sequence_id}",
                        output=sequence_state,
                        inputs=(*marker_inputs, *center_inputs),
                        selector_evidence=evidence,
                    )
                )
                for target in targets:
                    for output in dict.fromkeys(outputs):
                        effects.append(
                            _effect(
                                source=source,
                                target_uid=target.uid,
                                kind=f"keyframe_animation_{sequence_id}",
                                output=output,
                                inputs=(sequence_atom,),
                                selector_evidence=(
                                    *evidence,
                                    f"target_member:{target.uid}",
                                ),
                            )
                        )

        elif route.operation is EngineOperation.END:
            end_inputs: tuple[StateAtom, ...] = ()
            if route.operation_name == "Platformer End":
                target_pos_group = _selector_integer(source, "71")
                target_positions = _one_obj_members(
                    target_pos_group,
                    group_members=group_members,
                    group_parents=group_parents,
                )
                end_inputs = tuple(
                    atom
                    for target in target_positions
                    for atom in _spatial_input_atoms(target)
                )
                if not target_positions:
                    end_inputs = _spatial_input_atoms(source)
                spawn_group = _selector_integer(source, "51")
                if spawn_group:
                    effects.extend(
                        _spawn_group_effects(
                            source=source,
                            target_group=spawn_group,
                            group_members=group_members,
                            kind=f"platformer_end_spawn_group_{spawn_group}",
                            inputs=end_inputs,
                            selector_evidence=(
                                f"target_position_group:{target_pos_group}",
                            ),
                            activation_state_for=activation_state_for,
                        )
                    )
            effects.append(
                _effect(
                    source=source,
                    target_uid="__level__",
                    kind=f"{route.operation_name.lower().replace(' ', '_')}_completion",
                    output="level_completion",
                    inputs=end_inputs,
                    selector_evidence=(
                        f"target_position_group:{_selector_integer(source, '71')}",
                        f"spawn_or_legacy_target_group:{_selector_integer(source, '51')}",
                    ),
                )
            )

        elif route.operation is EngineOperation.DIRECT_GLOBAL:
            outputs_by_operation = {
                "TimeWarp": ("global_time_rate",),
                "Gravity": ("player_gravity",),
                "Rotate Gameplay": ("gameplay_coordinate_frame",),
                "Camera Mode": ("player_mode_bounds", "camera_follow_mode"),
            }
            outputs = outputs_by_operation.get(route.operation_name)
            if outputs is None:
                unsupported.append(
                    {
                        "serialization_order_index": source.serialization_index,
                        "object_id": source.object_id,
                        "blocker_kind": "unsupported_concrete_effect",
                        "effect_kind": route.operation_name,
                        "unsupported_property": "direct_global_output",
                        "evidence_required": "named 2.2081 global-state output",
                    }
                )
                continue
            for output in outputs:
                effects.append(
                    _effect(
                        source=source,
                        target_uid="__level__",
                        kind=f"direct_global_{output}",
                        output=output,
                    )
                )

        elif route.operation is EngineOperation.STOP_PAUSE_RESUME:
            target_group = _selector_integer(source, "51")
            for target in group_members.get(target_group, []):
                if not target.is_stoppable_trigger:
                    suppressed.append(
                        {
                            "source_uid": source.uid,
                            "source_serialization_index": source.serialization_index,
                            "target_uid": target.uid,
                            "target_serialization_index": target.serialization_index,
                            "target_object_id": target.object_id,
                            "component": "effect_execution_state:not_stoppable",
                        }
                    )
                    continue
                effects.append(
                    _effect(
                        source=source,
                        target_uid=target.uid,
                        kind="stop_pause_resume",
                        output="effect_execution_state",
                        selector_evidence=(
                            f"target_group:{target_group}",
                            f"target_member:{target.uid}",
                            f"use_control_id:{int(_boolean(source.one('535')))}",
                            f"control_id:{_selector_integer(source, '534')}",
                        ),
                    )
                )

    for source in pending_all_items:
        outputs = ["persistence"]
        if _boolean(source.one("493")):
            outputs.append("value")
        for timer, item_ids in (
            (False, sorted(normal_item_ids)),
            (True, sorted(timer_item_ids)),
        ):
            for item_id in item_ids:
                for output in outputs:
                    effects.append(
                        _effect(
                            source=source,
                            target_uid=_item_uid(item_id, timer=timer),
                            kind="persistent_item_target_all",
                            output=output,
                            selector_evidence=(
                                "target_all:1",
                                f"item_id:{item_id}",
                                f"timer:{int(timer)}",
                                f"persistent:{int(_boolean(source.one('491')))}",
                                f"reset:{int(_boolean(source.one('493')))}",
                            ),
                        )
                    )

    true_layout_atoms = layout_roots | gameplay_roots
    closure = effects.finish(true_layout_atoms)
    if compute_mech_closure:
        instance_by_uid = {instance.uid: instance for instance in instances}
        causal_instance_uids = {
            atom.instance_uid
            for atom in closure.required_atoms
            if atom.instance_uid in instance_by_uid
        } | set(closure.retained_source_uids)
        full_targ_side_objs = [
            instance for instance in instances if instance.uid not in causal_instance_uids
        ]
    else:
        causal_instance_uids = set()
        full_targ_side_objs = []
    report = {
        "schema": "gd-strip-level-effect-analysis",
        "schema_major": 1,
        "schema_minor": 1,
        "effect_semantics_revision": FX_SEM_REV,
        "game_version": "2.2081",
        "level_name": snapshot.canonical["source"].get("level_name"),
        "level_hash": snapshot.canonical["level"]["exact_level_string_sha256"],
        "object_count": len(instances),
        "expanded_effect_count": effects.expanded_fx_count,
        "retained_effect_count": effects.kept_fx_count,
        "retained_effect_ids_complete": capture_fx,
        "mechanical_closure_computed": compute_mech_closure,
        "diagnostic_report_complete": capture_diag,
        "true_layout_atom_count": len(true_layout_atoms),
        "intrinsic_layout_instance_exclusions": root_obj_exclusions,
        "retained_effect_ids": sorted(closure.retained_effect_ids),
        "retained_effect_source_uids": sorted(closure.retained_source_uids),
        "causal_record_uids": sorted(causal_instance_uids),
        "conditioning_layout_record_uids": sorted(causal_instance_uids),
        "fully_target_side_records": [
            {
                "sample_local_uid": instance.uid,
                "serialization_order_index": instance.serialization_index,
                "object_id": instance.object_id,
            }
            for instance in full_targ_side_objs
        ],
        "fully_target_side_record_count": len(full_targ_side_objs),
        "suppressed_configured_components": suppressed,
        "proven_nonmechanical_operations": visual_ops,
        "unsupported_uses": unsupported,
        "effect_expansion_ready": not unsupported,
    }
    return LevelEffectAnalysis(
        effects=effects.snapshot(),
        true_layout_atoms=frozenset(true_layout_atoms),
        true_layout_uids=frozenset(layout_uids),
        mechanical_closure=closure,
        unsupported_uses=tuple(unsupported),
        report=report,
    )
