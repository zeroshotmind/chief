"""Addressing state anywhere in a run, including inside nested constructs.

Contract 1.2 allows a loop/parallel step inside another construct's ``body``, and 1.5 says
its instances are scoped under the parent instance — but the endpoint list in 2.2 only
reaches one level down. This module generalises the addressing so nesting is actually
usable; the documented depth-1 routes are the two- and three-segment cases of it.
See CONTRACT-NOTES.md #6.

A *state path* is an odd-length list of tokens alternating step id and instance id::

    ["step_06"]                                  # a top-level step
    ["step_06", "inst_01", "step_04"]            # step_04 inside iteration 1 of step_06
    ["step_06", "inst_01", "step_09", "inst_02", "step_11"]   # one more level down
"""

from __future__ import annotations

from ..errors import NotFound, ValidationFailed
from ..models import RunState, StepInstance, StepState, WorkflowStep


def parse_path(raw: str) -> list[str]:
    tokens = [t for t in raw.split("/") if t]
    if not tokens:
        raise ValidationFailed("empty state path")
    return tokens


def format_path(path: list[str]) -> str:
    return "/".join(path)


def check_shape(path: list[str]) -> None:
    if len(path) % 2 == 0:
        raise ValidationFailed(
            "a state path must alternate step id and instance id and end on a step id",
            details={"path": path},
        )


def validate_against_definition(steps_by_id: dict[str, WorkflowStep], path: list[str]) -> None:
    """Check every hop is legal in the plan: parents are constructs, children are in their body."""
    check_shape(path)
    for index in range(0, len(path), 2):
        step_id = path[index]
        step = steps_by_id.get(step_id)
        if step is None:
            raise NotFound(f"step '{step_id}' is not part of this workflow")
        if index + 1 < len(path):
            if not step.is_construct:
                raise ValidationFailed(
                    f"step '{step_id}' is type '{step.type}' and has no instances",
                    details={"step_id": step_id},
                )
            child = path[index + 2]
            if child not in step.body_ids:
                raise ValidationFailed(
                    f"step '{child}' is not in the body of '{step_id}'",
                    details={"step_id": step_id, "child": child},
                )


def resolve(
    run: RunState,
    path: list[str],
    *,
    create: bool = False,
) -> tuple[dict[str, StepState], StepState, StepInstance | None]:
    """Resolve a state path.

    Returns ``(container, state, enclosing_instance)`` where ``container`` is the map the
    state lives in and ``enclosing_instance`` is the StepInstance whose body it belongs to
    (None for a top-level step). With ``create=True`` a missing body-step entry is
    materialised as ``pending``; missing instances are never created implicitly.
    """
    check_shape(path)
    container = run.step_states
    enclosing: StepInstance | None = None

    for index in range(0, len(path), 2):
        step_id = path[index]
        state = container.get(step_id)
        if state is None:
            if not create:
                raise NotFound(f"no state recorded for step '{step_id}' at {format_path(path)}")
            state = StepState(step_id=step_id)
            container[step_id] = state
        if index + 1 == len(path):
            return container, state, enclosing
        instance_id = path[index + 1]
        instance = state.instance(instance_id)
        if instance is None:
            raise NotFound(f"instance '{instance_id}' not found on step '{step_id}'")
        container = instance.step_states
        enclosing = instance

    raise AssertionError("unreachable: path shape was validated")


def resolve_instance(run: RunState, step_path: list[str], instance_id: str) -> StepInstance:
    _, state, _ = resolve(run, step_path)
    instance = state.instance(instance_id)
    if instance is None:
        raise NotFound(f"instance '{instance_id}' not found on step '{step_path[-1]}'")
    return instance


def instance_path(step_path: list[str], instance_id: str) -> list[str]:
    return [*step_path, instance_id]


def find_step_paths(run: RunState, step_id: str) -> list[list[str]]:
    """Every state path at which ``step_id`` currently has a materialised StepState.

    A body step has one materialised state per instance of its parent, so this returns a
    list. Used by amendment application, which must reach all of them.
    """
    found: list[list[str]] = []

    def walk(container: dict[str, StepState], prefix: list[str]) -> None:
        for key, state in container.items():
            here = [*prefix, key]
            if key == step_id:
                found.append(here)
            for instance in state.instances or []:
                walk(instance.step_states, [*here, instance.instance_id])

    walk(run.step_states, [])
    return found
