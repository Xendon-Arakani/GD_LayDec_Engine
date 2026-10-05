








from __future__ import annotations

from dataclasses import dataclass
from collections import defaultdict, deque
from typing import Iterable


LIGHT_MAX_REQ_ATOMS = 750_000
LIGHT_MAX_KEPT_FX = 1_000_000
LIGHT_MAX_ORG_OBJS = 250_000


class LightLimitError(RuntimeError):


    def __init__(self, *, resource: str, limit: int, observed: int) -> None:
        self.resource = resource
        self.limit = limit
        self.observed = observed
        super().__init__(
            f"highlight system exceeds {resource} limit "
            f"({observed} > {limit}); no partial system is valid"
        )


class EffectGraphIndex:


    def __init__(self, effects: Iterable[EffectApplication]) -> None:
        self.effects = tuple(effects)
        effect_ids = [effect.effect_id for effect in self.effects]
        if len(effect_ids) != len(set(effect_ids)):
            raise ValueError("effect_id values must be unique after instance expansion")
        producers: dict[StateAtom, list[int]] = defaultdict(list)
        for index, effect in enumerate(self.effects):
            for output in effect.outputs:
                producers[output].append(index)
        self.producers_by_output = {
            atom: tuple(indices) for atom, indices in producers.items()
        }

    def upstream_closure(
        self,
        sink_atoms: Iterable[StateAtom],
        *,
        max_required_atoms: int = LIGHT_MAX_REQ_ATOMS,
        max_retained_effects: int = LIGHT_MAX_KEPT_FX,
    ) -> EffectClosure:


        required = set(sink_atoms)
        pending = deque(required)
        retained_indices: set[int] = set()
        sources: set[str] = set()
        while pending:
            atom = pending.popleft()
            for effect_index in self.producers_by_output.get(atom, ()):
                if effect_index in retained_indices:
                    continue
                retained_indices.add(effect_index)
                if len(retained_indices) > max_retained_effects:
                    raise LightLimitError(
                        resource="retained effects",
                        limit=max_retained_effects,
                        observed=len(retained_indices),
                    )
                effect = self.effects[effect_index]
                sources.add(effect.source_uid)
                for input_atom in effect.inputs:
                    if input_atom in required:
                        continue
                    required.add(input_atom)
                    if len(required) > max_required_atoms:
                        raise LightLimitError(
                            resource="required atoms",
                            limit=max_required_atoms,
                            observed=len(required),
                        )
                    pending.append(input_atom)
        return EffectClosure(
            required_atoms=frozenset(required),
            retained_effect_ids=frozenset(
                self.effects[index].effect_id for index in retained_indices
            ),
            retained_source_uids=frozenset(sources),
        )


@dataclass(frozen=True, order=True, slots=True)
class StateAtom:


    instance_uid: str
    property_name: str

    def __post_init__(self) -> None:
        if not self.instance_uid:
            raise ValueError("instance_uid cannot be empty")
        if not self.property_name:
            raise ValueError("property_name cannot be empty")


@dataclass(frozen=True, slots=True)
class EffectApplication:







    effect_id: str
    source_uid: str
    effect_kind: str
    inputs: tuple[StateAtom, ...]
    outputs: tuple[StateAtom, ...]
    selector_evidence: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.effect_id:
            raise ValueError("effect_id cannot be empty")
        if not self.source_uid:
            raise ValueError("source_uid cannot be empty")
        if not self.effect_kind:
            raise ValueError("effect_kind cannot be empty")
        if not self.outputs:
            raise ValueError("an effect application must have at least one output")


@dataclass(frozen=True, slots=True)
class EffectClosure:


    required_atoms: frozenset[StateAtom]
    retained_effect_ids: frozenset[str]
    retained_source_uids: frozenset[str]


@dataclass(frozen=True, slots=True)
class HighlightSystem:








    sel_obj_uids: frozenset[str]
    seed_atoms: frozenset[StateAtom]
    required_atoms: frozenset[StateAtom]
    retained_effect_ids: frozenset[str]
    orange_instance_uids: frozenset[str]


def backward_fx_closure(
    effects: Iterable[EffectApplication],
    sink_atoms: Iterable[StateAtom],
) -> EffectClosure:







    ordered_effects = tuple(effects)
    effect_ids = [effect.effect_id for effect in ordered_effects]
    if len(effect_ids) != len(set(effect_ids)):
        raise ValueError("effect_id values must be unique after instance expansion")

    required = set(sink_atoms)
    retained: set[str] = set()
    sources: set[str] = set()
    changed = True
    while changed:
        changed = False
        for effect in ordered_effects:
            if effect.effect_id in retained:
                continue
            if not required.intersection(effect.outputs):
                continue
            retained.add(effect.effect_id)
            sources.add(effect.source_uid)
            required.update(effect.inputs)
            changed = True

    return EffectClosure(
        required_atoms=frozenset(required),
        retained_effect_ids=frozenset(retained),
        retained_source_uids=frozenset(sources),
    )


def upstream_light_sys(
    effects: Iterable[EffectApplication],
    *,
    sel_obj_uids: Iterable[str],
    seed_atoms: Iterable[StateAtom],
    max_required_atoms: int = LIGHT_MAX_REQ_ATOMS,
    max_retained_effects: int = LIGHT_MAX_KEPT_FX,
    max_orange_instances: int = LIGHT_MAX_ORG_OBJS,
    index: EffectGraphIndex | None = None,
) -> HighlightSystem:













    selected = frozenset(sel_obj_uids)
    if not selected:
        raise ValueError("selected_instance_uids cannot be empty")
    seeds = frozenset(seed_atoms)
    if not seeds:
        raise ValueError("seed_atoms cannot be empty")
    if any(atom.instance_uid not in selected for atom in seeds):
        raise ValueError("every highlight seed atom must belong to the selection")

    for name, limit in (
        ("required atoms", max_required_atoms),
        ("retained effects", max_retained_effects),
        ("orange instances", max_orange_instances),
    ):
        if limit < 1:
            raise ValueError(f"{name} limit must be positive")

    graph = index if index is not None else EffectGraphIndex(effects)
    closure = graph.upstream_closure(
        seeds,
        max_required_atoms=max_required_atoms,
        max_retained_effects=max_retained_effects,
    )
    upstream_instances = {
        atom.instance_uid
        for atom in closure.required_atoms
    }
    upstream_instances.update(closure.retained_source_uids)
    upstream_instances.difference_update(selected)
    if len(upstream_instances) > max_orange_instances:
        raise LightLimitError(
            resource="orange instances",
            limit=max_orange_instances,
            observed=len(upstream_instances),
        )
    return HighlightSystem(
        sel_obj_uids=selected,
        seed_atoms=seeds,
        required_atoms=closure.required_atoms,
        retained_effect_ids=closure.retained_effect_ids,
        orange_instance_uids=frozenset(upstream_instances),
    )


def expand_sel_prop_fx(
    *,
    source_uid: str,
    effect_kind: str,
    selected_target_uids: Iterable[str],
    output_property: str,
    selector_evidence: Iterable[str] = (),
    extra_inputs: Iterable[StateAtom] = (),
) -> tuple[EffectApplication, ...]:






    targets = tuple(dict.fromkeys(selected_target_uids))
    common_inputs = (
        StateAtom(source_uid, "effect_configuration"),
        StateAtom(source_uid, "activation"),
        *tuple(extra_inputs),
    )
    selector_tuple = tuple(selector_evidence)
    return tuple(
        EffectApplication(
            effect_id=f"{source_uid}:{effect_kind}:{target_uid}:{output_property}",
            source_uid=source_uid,
            effect_kind=effect_kind,
            inputs=common_inputs,
            outputs=(StateAtom(target_uid, output_property),),
            selector_evidence=selector_tuple,
        )
        for target_uid in targets
    )
