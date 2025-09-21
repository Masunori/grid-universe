"""Push interaction system.

Enables entities (typically agents) to push adjacent entities marked with the
``Pushable`` component into the next cell along the interaction vector,
provided the destination cell is free of blocking/collidable constraints.
Supports multi-entity stacks at the source tile by moving all pushables.
"""

from dataclasses import replace
from typing import Optional
from grid_universe.moves import wrap_around_move_fn
from grid_universe.state import State
from grid_universe.components import Position
from grid_universe.types import EntityID
from grid_universe.utils.ecs import entities_with_components_at
from grid_universe.utils.grid import is_blocked_at, is_in_bounds, wrap_position
from grid_universe.utils.trail import add_trail_position


def compute_destination(
    state: State, current_pos: Position, next_pos: Position
) -> Optional[Position]:
    """Compute push destination given current and occupant next positions.

    Returns the square beyond ``next_pos`` in the movement direction, applying
    wrap logic if the state's move function is the wrapping one. ``None`` if
    outside bounds and not wrapping.
    """
    dx = next_pos.x - current_pos.x
    dy = next_pos.y - current_pos.y
    dest_x = next_pos.x + dx
    dest_y = next_pos.y + dy

    if state.move_fn is wrap_around_move_fn:
        return wrap_position(dest_x, dest_y, state.width, state.height)

    target_position = Position(dest_x, dest_y)
    if not is_in_bounds(state, target_position):
        return None

    return target_position


def push_system(state: State, eid: EntityID, next_pos: Position) -> State:
    """Attempt to push any pushable entities at ``next_pos``.

    Args:
        state (State): Current immutable state.
        eid (EntityID): Entity initiating the push (must have a position).
        next_pos (Position): Adjacent position the entity is trying to move into.

    Returns:
        State: Updated state with moved positions if push succeeds; original state otherwise.
    """
    current_pos = state.position.get(eid)
    if current_pos is None:
        return state

    # Is there a pushable object at next_pos?
    pushable_ids = entities_with_components_at(state, next_pos, state.pushable)
    if not pushable_ids:
        return state  # Nothing to push

    pushable_id = pushable_ids[0]
    push_to = compute_destination(state, current_pos, next_pos)
    if push_to is None:
        return state

    if is_blocked_at(state, push_to, check_collidable=True):
        return state  # Push not possible

    new_position = state.position.set(eid, next_pos)
    for pushable_id in pushable_ids:
        new_position = new_position.set(pushable_id, push_to)
        add_trail_position(state, pushable_id, push_to)

    return replace(state, position=new_position)
