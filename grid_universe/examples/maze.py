"""Procedural maze level generator example.

This module demonstrates authoring a parameterized maze-based level using the
``Level`` authoring API and factory helpers, then converting to an immutable
``State`` suitable for simulation or Gym-style environments.

Design Goals
------------
* Showcase composition of factories (agent, walls, doors, portals, hazards,
  power-ups, enemies) with authoring-time references (e.g., portal pairing,
  enemy pathfinding target reference to the agent) that are resolved during
  ``to_state`` conversion.
* Provide tunable difficulty levers: wall density, counts of required
  objectives, rewards, hazards, enemies, doors, portals and power-ups.
* Illustrate how movement styles (static, directional patrol, straight-line
  pathfinding, full pathfinding) can be expressed via component choices.

Usage Example
-------------
    from grid_universe.examples import maze
    state = maze.generate(width=20, height=20, seed=123)

    # Render / step the state using the engine's systems or gym wrapper.

Key Concepts Illustrated
------------------------
Required Items:
    Use cores flagged as ``required=True`` which the default objective logic
    expects to be collected before reaching the exit.
Power-Ups:
    Effects created with optional time or usage limits (speed, immunity,
    phasing) acting as pickups.
Enemies:
    Configurable movement style and lethality; pathfinding enemies reference
    the agent to resolve target entity IDs later.
Essential Path:
    Minimal union of shortest paths that touch required items and exit. Other
    entities (hazards, enemies, boxes) prefer non-essential cells.
"""

from __future__ import annotations

from enum import StrEnum, auto
from typing import Any, Callable, Dict, List, Optional, Tuple, Set

import random

from grid_universe.state import State
from grid_universe.types import (
    EffectLimit,
    EffectLimitAmount,
    EffectType,
    MoveFn,
    ObjectiveFn,
)
from grid_universe.moves import default_move_fn
from grid_universe.objectives import default_objective_fn
from grid_universe.components.properties import (
    AppearanceName,
    Moving,
    MovingAxis,
    PathfindingType,
)
from grid_universe.levels.grid import Level, Position
from grid_universe.levels.convert import to_state
from grid_universe.levels.entity_spec import EntitySpec
from grid_universe.levels.factories import (
    create_agent,
    create_floor,
    create_wall,
    create_exit,
    create_coin,
    create_core,
    create_key,
    create_door,
    create_portal,
    create_box,
    create_monster,
    create_hazard,
    create_speed_effect,
    create_immunity_effect,
    create_phasing_effect,
)
from grid_universe.utils.maze import (
    generate_perfect_maze,
    adjust_maze_wall_percentage,
    all_required_path_positions,
)


# -------------------------
# Specs and defaults
# -------------------------

EffectOption = Dict[str, Any]
PowerupSpec = Tuple[
    EffectType, Optional[EffectLimit], Optional[EffectLimitAmount], EffectOption
]
DamageAmount = int
IsLethal = bool
HazardSpec = Tuple[AppearanceName, DamageAmount, IsLethal]

DEFAULT_POWERUPS: List[PowerupSpec] = [
    (EffectType.SPEED, EffectLimit.TIME, 10, {"multiplier": 2}),
    (EffectType.PHASING, EffectLimit.TIME, 10, {}),
    (EffectType.IMMUNITY, EffectLimit.USAGE, 5, {}),
]

DEFAULT_HAZARDS: List[HazardSpec] = [
    (AppearanceName.LAVA, 5, True),
    (AppearanceName.SPIKE, 3, False),
]


class MovementType(StrEnum):
    STATIC = auto()
    DIRECTIONAL = auto()
    PATHFINDING_LINE = auto()
    PATHFINDING_PATH = auto()


EnemySpec = Tuple[DamageAmount, IsLethal, MovementType, int]
BoxSpec = Tuple[bool, int]

DEFAULT_ENEMIES: List[EnemySpec] = [
    (5, True, MovementType.DIRECTIONAL, 2),
    (3, False, MovementType.PATHFINDING_LINE, 1),
]

DEFAULT_BOXES: List[BoxSpec] = [
    (True, 0),
    (False, 1),
    (False, 2),
]


# -------------------------
# Internal helpers
# -------------------------


def _random_axis_and_dir(rng: random.Random) -> Tuple[MovingAxis, int]:
    """Choose a random movement axis and direction.

    Parameters
    ----------
    rng:
        Random source.

    Returns
    -------
    (MovingAxis, int)
        Selected axis and signed direction (+1 or -1).
    """
    axis: MovingAxis = rng.choice([MovingAxis.HORIZONTAL, MovingAxis.VERTICAL])
    direction: int = rng.choice([-1, 1])
    return axis, direction


def _pop_or_fallback(positions: List[Position], fallback: Position) -> Position:
    """Pop a position if available else return a fallback.

    Useful when the parameterization may request more placements than there
    are open tiles.
    """
    return positions.pop() if positions else fallback


# -------------------------
# Main generator
# -------------------------


def generate(
    width: int,
    height: int,
    num_required_items: int = 1,
    num_rewardable_items: int = 1,
    num_portals: int = 1,
    num_doors: int = 1,
    health: int = 5,
    movement_cost: int = 1,
    required_item_reward: int = 10,
    rewardable_item_reward: int = 10,
    boxes: List[BoxSpec] = DEFAULT_BOXES,
    powerups: List[PowerupSpec] = DEFAULT_POWERUPS,
    hazards: List[HazardSpec] = DEFAULT_HAZARDS,
    enemies: List[EnemySpec] = DEFAULT_ENEMIES,
    wall_percentage: float = 0.8,
    move_fn: MoveFn = default_move_fn,
    objective_fn: ObjectiveFn = default_objective_fn,
    seed: Optional[int] = None,
) -> State:
    """Generate a randomized maze game state.

    This function orchestrates maze carving, tile classification, entity
    placement and authoring-time reference wiring before producing the
    immutable simulation ``State``.

    Args:
        width (int): Width of the maze grid.
        height (int): Height of the maze grid.
        num_required_items (int): Number of required cores that must be collected before exit.
        num_rewardable_items (int): Number of optional reward coins.
        num_portals (int): Number of portal pairs to place (each pair consumes two open cells).
        num_doors (int): Number of door/key pairs; each door is locked by its matching key.
        health (int): Initial agent health points.
        movement_cost (int): Per-tile movement cost encoded in floor components.
        required_item_reward (int): Reward granted for collecting each required item.
        rewardable_item_reward (int): Reward granted for each optional reward item (coin).
        boxes (List[BoxSpec]): List defining ``(pushable?, speed)`` for box entities; speed > 0 creates moving boxes.
        powerups (List[PowerupSpec]): Effect specifications converted into pickup entities.
        hazards (List[HazardSpec]): Hazard specifications ``(appearance, damage, lethal)``.
        enemies (List[EnemySpec]): Enemy specifications ``(damage, lethal, movement type, speed)``.
        wall_percentage (float): Fraction of original maze walls to retain (``0.0`` => open field, ``1.0`` => perfect maze).
        move_fn (MoveFn): Movement candidate function injected into the level.
        objective_fn (ObjectiveFn): Win condition predicate injected into the level.
        seed (int | None): RNG seed for deterministic generation.

    Returns:
        State: Fully wired immutable state ready for simulation.
    """
    rng = random.Random(seed)

    # 1) Base maze -> adjust walls
    maze_grid = generate_perfect_maze(width, height, rng)
    maze_grid = adjust_maze_wall_percentage(maze_grid, wall_percentage, rng)

    # 2) Level
    level = Level(
        width=width,
        height=height,
        move_fn=move_fn,
        objective_fn=objective_fn,
        seed=seed,
    )

    # 3) Collect positions
    open_positions: List[Position] = [
        pos for pos, is_open in maze_grid.items() if is_open
    ]
    wall_positions: List[Position] = [
        pos for pos, is_open in maze_grid.items() if not is_open
    ]
    rng.shuffle(open_positions)  # randomize for placement variety

    # 4) Floors on all open cells
    for pos in open_positions:
        level.add(pos, create_floor(cost_amount=movement_cost))

    # 5) Agent and exit
    start_pos: Position = _pop_or_fallback(open_positions, (0, 0))
    agent = create_agent(health=health)
    level.add(start_pos, agent)

    goal_pos: Position = _pop_or_fallback(open_positions, (width - 1, height - 1))
    level.add(goal_pos, create_exit())

    # 6) Required cores
    required_positions: List[Position] = []
    for _ in range(num_required_items):
        if not open_positions:
            break
        pos = open_positions.pop()
        level.add(pos, create_core(reward=required_item_reward, required=True))
        required_positions.append(pos)

    # Compute essential path set
    essential_path: Set[Position] = all_required_path_positions(
        maze_grid, start_pos, required_positions, goal_pos
    )

    # 7) Rewardable coins
    for _ in range(num_rewardable_items):
        if not open_positions:
            break
        level.add(open_positions.pop(), create_coin(reward=rewardable_item_reward))

    # 8) Portals (explicit pairing by reference)
    for _ in range(num_portals):
        if len(open_positions) < 2:
            break
        p1 = create_portal()
        p2 = create_portal(pair=p1)  # reciprocal authoring-time reference
        level.add(open_positions.pop(), p1)
        level.add(open_positions.pop(), p2)

    # 9) Doors/keys
    for i in range(num_doors):
        if len(open_positions) < 2:
            break
        key_pos = open_positions.pop()
        door_pos = open_positions.pop()
        key_id_str = f"key{i}"
        level.add(key_pos, create_key(key_id=key_id_str))
        level.add(door_pos, create_door(key_id=key_id_str))

    # 10) Powerups (as pickups)
    create_effect_fn_map: dict[EffectType, Callable[..., EntitySpec]] = {
        EffectType.SPEED: create_speed_effect,
        EffectType.IMMUNITY: create_immunity_effect,
        EffectType.PHASING: create_phasing_effect,
    }
    for type_, lim_type, lim_amount, extra in powerups:
        if not open_positions:
            break
        pos = open_positions.pop()
        create_effect_fn = create_effect_fn_map[type_]
        kwargs = {
            "time": lim_amount if lim_type == EffectLimit.TIME else None,
            "usage": lim_amount if lim_type == EffectLimit.USAGE else None,
        }
        level.add(pos, create_effect_fn(**extra, **kwargs))

    # 11) Non-essential positions (for enemies, hazards, moving boxes)
    open_non_essential: List[Position] = [
        p for p in open_positions if p not in essential_path
    ]
    rng.shuffle(open_non_essential)

    # 12) Boxes
    for pushable, speed in boxes:
        if not open_non_essential:
            break
        pos = open_non_essential.pop()
        box = create_box(pushable=pushable)
        if speed > 0:
            axis, direction = _random_axis_and_dir(rng)
            box.moving = Moving(axis=axis, direction=direction, speed=speed)
        level.add(pos, box)

    # 13) Enemies (wire pathfinding to agent by reference if requested)
    for dmg, lethal, mtype, mspeed in enemies:
        if not open_non_essential:
            break
        pos = open_non_essential.pop()

        # Explicit pathfinding via reference to the agent (authoring-time)
        path_type: Optional[PathfindingType] = None
        if mtype == MovementType.PATHFINDING_LINE:
            path_type = PathfindingType.STRAIGHT_LINE
        elif mtype == MovementType.PATHFINDING_PATH:
            path_type = PathfindingType.PATH

        # If path_type is set, wire target to agent; otherwise directional/static
        if path_type is not None:
            enemy = create_monster(
                damage=dmg, lethal=lethal, pathfind_target=agent, path_type=path_type
            )
        else:
            enemy = create_monster(damage=dmg, lethal=lethal)
            if mtype == MovementType.DIRECTIONAL and mspeed > 0:
                axis, direction = _random_axis_and_dir(rng)
                enemy.moving = Moving(axis=axis, direction=direction, speed=mspeed)

        level.add(pos, enemy)

    # 14) Hazards
    for app_name, dmg, lethal in hazards:
        if not open_non_essential:
            break
        level.add(
            open_non_essential.pop(),
            create_hazard(app_name, damage=dmg, lethal=lethal, priority=7),
        )

    # 15) Walls
    for pos in wall_positions:
        level.add(pos, create_wall())

    # Convert to immutable State (wiring is resolved inside to_state)
    return to_state(level)
