from __future__ import annotations

from dataclasses import asdict
from typing import Any, Dict, List, Optional, Tuple


from grid_universe.levels.grid import Level
from grid_universe.levels.convert import to_state, from_state
from grid_universe.levels.entity_spec import EntitySpec, COMPONENT_TO_FIELD
from grid_universe.levels.factories import (
    create_agent,
    create_coin,
    create_core,
    create_key,
    create_door,
    create_portal,
    create_monster,
    create_wall,
    create_floor,
    create_speed_effect,
)
from grid_universe.components.properties import PathfindingType, AppearanceName


# ---------- Helpers: Canonicalization ----------


def _obj_component_signature(obj: EntitySpec) -> Dict[str, Any]:
    """
    Capture an EntityObject's components (excluding authoring-only lists/refs) as a dict.
    """
    sig: Dict[str, Any] = {}
    for _, store_name in COMPONENT_TO_FIELD.items():
        comp = getattr(obj, store_name, None)
        if comp is not None:
            sig[store_name] = asdict(comp)
    return sig


def _obj_nested_signature(objs: List[EntitySpec]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for o in objs:
        out.append(_obj_component_signature(o))
    out.sort(key=lambda d: str(sorted(d.items())))
    return out


def canonicalize_level(level: Level) -> Dict[Tuple[int, int], List[Dict[str, Any]]]:
    """
    Build a canonical structure for Level:
      { (x,y): [ {components: {...}, inventory_list: [...], status_list: [...]}, ... ] }
    Entries are sorted deterministically.
    """
    cells: Dict[Tuple[int, int], List[Dict[str, Any]]] = {}
    for y in range(level.height):
        for x in range(level.width):
            entries: List[Dict[str, Any]] = []
            for obj in level.grid[y][x]:
                entries.append(
                    {
                        "components": _obj_component_signature(obj),
                        "inventory_list": _obj_nested_signature(obj.inventory_list),
                        "status_list": _obj_nested_signature(obj.status_list),
                    }
                )
            entries.sort(
                key=lambda e: (
                    str(sorted(e["components"].items())),
                    str(e["inventory_list"]),
                    str(e["status_list"]),
                )
            )
            if entries:
                cells[(x, y)] = entries
    return cells


def _state_entity_component_signature(state, eid) -> Dict[str, Any]:
    """
    Capture non-ID-bearing components of an entity as a plain dict. Excludes Position,
    Inventory, Status. Encodes pathfinding target by target position and portal pair by pair position.
    """
    sig: Dict[str, Any] = {}

    # Basic components
    for _, store_name in COMPONENT_TO_FIELD.items():
        if store_name in ("inventory", "status", "pathfinding", "portal"):
            continue
        store = getattr(state, store_name)
        comp = store.get(eid)
        if comp is not None:
            sig[store_name] = asdict(comp)

    # Pathfinding: encode type and target by target Position (if positioned)
    pf = state.pathfinding.get(eid)
    if pf is not None:
        tgt_pos: Optional[Tuple[int, int]] = None
        if pf.target is not None and pf.target in state.position:
            p = state.position.get(pf.target)
            if p is not None:
                tgt_pos = (p.x, p.y)
        sig["pathfinding"] = {"type": pf.type.name, "target_pos": tgt_pos}

    # Portal: encode pair by pair position (if positioned)
    pr = state.portal.get(eid)
    if pr is not None:
        pair_pos: Optional[Tuple[int, int]] = None
        if pr.pair_entity in state.position:
            pp = state.position.get(pr.pair_entity)
            if pp is not None:
                pair_pos = (pp.x, pp.y)
        sig["portal"] = {"pair_pos": pair_pos}

    return sig


def _state_nested_signatures(state, ids: List[int]) -> List[Dict[str, Any]]:
    """
    Signature for nested entities (inventory items or status effects), which have no Position.
    """
    out: List[Dict[str, Any]] = []
    for nid in sorted(ids):
        out.append(_state_entity_component_signature(state, nid))
    return out


def canonicalize_state(state) -> Dict[Tuple[int, int], List[Dict[str, Any]]]:
    """
    Build a canonical, comparable structure for State:
      { (x,y): [ {components: {...}, inventory: [...], status: [...]}, ... ] }
    Inventory/status entries are nested component dicts for each referenced entity.
    """
    cells: Dict[Tuple[int, int], List[Dict[str, Any]]] = {}

    inv_map: Dict[int, List[int]] = {
        eid: sorted(list(inv.item_ids)) for eid, inv in state.inventory.items()
    }
    st_map: Dict[int, List[int]] = {
        eid: sorted(list(st.effect_ids)) for eid, st in state.status.items()
    }

    for eid, pos in state.position.items():
        key = (pos.x, pos.y)
        comp_sig = _state_entity_component_signature(state, eid)
        inv_sig = _state_nested_signatures(state, inv_map.get(eid, []))
        st_sig = _state_nested_signatures(state, st_map.get(eid, []))
        entry = {"components": comp_sig, "inventory": inv_sig, "status": st_sig}
        cells.setdefault(key, []).append(entry)

    for key in cells:
        cells[key].sort(
            key=lambda e: (
                str(sorted(e["components"].items())),
                str(e["inventory"]),
                str(e["status"]),
            )
        )
    return cells


# ---------- Test fixtures ----------


def build_sample_level() -> Level:
    """
    Build a Level that exercises:
    - agent with empty Inventory/Status components and authoring lists
    - items/effects in inventory_list/status_list
    - portals paired by reference
    - monster pathfinding to agent by reference
    - some walls/floors
    """
    lvl = Level(
        width=7,
        height=5,
        move_fn=lambda s, e, a: [],
        objective_fn=lambda s, a: False,
        seed=123,
    )

    # Background floors
    for x in range(lvl.width):
        for y in range(lvl.height):
            lvl.add((x, y), create_floor(1))

    # Agent at (1,1) with explicit Inventory/Status components and empty authoring lists
    agent = create_agent(health=10)
    # put a key and a speed effect into authoring lists (they'll materialize as entities without Position)
    agent.inventory_list.append(create_key("red"))
    agent.status_list.append(create_speed_effect(2, time=5))
    lvl.add((1, 1), agent)

    # Coin and core on ground
    lvl.add((2, 1), create_coin(3))
    lvl.add((3, 1), create_core(5, required=True))

    # Door keyed to red key
    lvl.add((4, 1), create_door("red"))

    # Portals paired by reference
    p1 = create_portal()
    p2 = create_portal(pair=p1)
    lvl.add((0, 0), p1)
    lvl.add((6, 4), p2)

    # Monster pathfinding to agent (PATH)
    mon = create_monster(
        damage=2, lethal=False, pathfind_target=agent, path_type=PathfindingType.PATH
    )
    lvl.add((6, 1), mon)

    # Some walls
    lvl.add((0, 2), create_wall())
    lvl.add((6, 2), create_wall())

    return lvl


# ---------- Tests ----------


def test_level_roundtrip_lossless() -> None:
    """
    Level -> State -> Level preserves component structure and authoring lists.
    Also verifies wiring refs are restored (pathfinding target, portal pair).
    """
    level1 = build_sample_level()
    state = to_state(level1)
    level2 = from_state(state)

    # Canonical compare
    can1 = canonicalize_level(level1)
    can2 = canonicalize_level(level2)
    assert can1 == can2, (
        f"Level roundtrip mismatch.\nOriginal: {can1}\nRoundtrip: {can2}"
    )

    # Wiring refs: find agent and monster, ensure monster.target_ref is agent
    # Also ensure portals are paired by portal_pair_ref bidirectionally
    agent_obj = None
    monster_obj = None
    portal_objs: List[EntitySpec] = []
    for y in range(level2.height):
        for x in range(level2.width):
            for obj in level2.grid[y][x]:
                if obj.agent is not None:
                    agent_obj = obj
                if (
                    obj.damage is not None
                    and obj.appearance
                    and obj.appearance.name == AppearanceName.MONSTER
                ):
                    monster_obj = obj
                if obj.portal is not None:
                    portal_objs.append(obj)
    assert agent_obj is not None and monster_obj is not None
    assert monster_obj.pathfind_target_ref is agent_obj
    # portals should be paired
    assert len(portal_objs) == 2
    a, b = portal_objs
    assert a.portal_pair_ref is b and b.portal_pair_ref is a


def test_state_roundtrip_lossless() -> None:
    """
    State -> Level -> State preserves positioned entities, nested inventory/status entities,
    and pathfinding/portal semantics (compared canonically by positions).
    """
    level = build_sample_level()
    state1 = to_state(level)
    level2 = from_state(state1)
    state2 = to_state(level2)

    can1 = canonicalize_state(state1)
    can2 = canonicalize_state(state2)
    assert can1 == can2, (
        f"State roundtrip mismatch.\nOriginal: {can1}\nRoundtrip: {can2}"
    )
