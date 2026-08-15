"""Server-derived state (contract 1.3, 1.4, 1.5 and the invariants in section 4).

Three things are derived here and never accepted as client input:

* the status of a loop/parallel StepState, from its instances;
* the status of a StepInstance, from the states of the steps in its parent's body;
* ``RunState.status`` ``completed``/``failed``, from the top-level steps.

Plus the one server-only *write*: propagating ``skipped`` down a failed dependency chain,
which is what keeps a run from sitting ``running`` forever after a step fails.

Recomputation is a full bottom-up pass over the run. It is cheap at single-user scale and
removes a whole class of bug where an update path forgets to refresh an ancestor.
"""

from __future__ import annotations

from ..ids import now
from ..models import (
    TERMINAL_OK,
    RunState,
    RunStatus,
    StepInstance,
    StepState,
    StepStatus,
    WorkflowDefinition,
    WorkflowStep,
)
from .graph import top_level_ids

TERMINAL_ANY: frozenset[str] = frozenset({"completed", "failed", "skipped"})


def _terminal_set(policy: str) -> frozenset[str]:
    """States that count as "this instance/step is done" for completion purposes.

    With ``on_instance_failure: continue`` a failure is tolerated, so it counts as done
    rather than poisoning the parent.
    """
    return TERMINAL_ANY if policy == "continue" else TERMINAL_OK


def set_status(state: StepState | StepInstance, status: StepStatus) -> None:
    """Assign a status and keep the timestamps consistent with it."""
    previous = state.status
    state.status = status
    if status != "pending" and state.started_at is None:
        state.started_at = now()
    if status in TERMINAL_ANY:
        if state.completed_at is None or previous != status:
            state.completed_at = now()
    else:
        state.completed_at = None
        if status == "pending":
            state.started_at = None


def derive_construct_status(step: WorkflowStep, state: StepState) -> StepStatus:
    """Status of a loop/parallel step, from its instances."""
    instances = state.instances or []
    policy = step.failure_policy
    if policy == "fail_fast" and any(i.status == "failed" for i in instances):
        return "failed"
    terminal = _terminal_set(policy)
    if state.instances_closed and all(i.status in terminal for i in instances):
        return "completed"
    if instances:
        return "running"
    return "running" if state.started_at else "pending"


def derive_instance_status(parent: WorkflowStep, instance: StepInstance) -> StepStatus:
    """Status of one iteration/branch, from the states of the steps in the parent's body.

    For a single-step body this reduces to that one step's reported status, which is what
    contract 1.5 specifies; the instance-update endpoint writes through to the body step so
    both shapes go through the same rule.
    """
    # The instance's own recorded body, not the parent's current one: an amendment that
    # edits the body must not retroactively change what a finished iteration had to do.
    body = instance.body or parent.body_ids
    policy = parent.failure_policy
    present = [instance.step_states[b] for b in body if b in instance.step_states]
    if policy == "fail_fast" and any(s.status == "failed" for s in present):
        return "failed"
    terminal = _terminal_set(policy)
    if len(present) == len(body) and all(s.status in terminal for s in present):
        return "completed"
    if any(s.status != "pending" for s in present):
        return "running"
    return "running" if instance.started_at else "pending"


def _blocking_dependency(
    container: dict[str, StepState], step: WorkflowStep
) -> tuple[str, str] | None:
    """The first dependency that makes this step unrunnable, if any."""
    for dep in step.depends_on:
        blocker = container.get(dep)
        if blocker is not None and blocker.status in ("failed", "skipped"):
            return dep, blocker.status
    return None


def propagate_skips(
    container: dict[str, StepState],
    members: list[str],
    steps_by_id: dict[str, WorkflowStep],
) -> None:
    """Keep dependency-driven ``skipped`` in sync with the failures that caused it.

    Skips are applied down a failed dependency chain, and *retracted* when the blocking
    failure goes away: an approved ``replay_step`` resets a failed step to ``pending``, and
    everything skipped behind it has to become runnable again — otherwise the run would
    later report ``completed`` for steps that never ran.

    Only skips this function produced (``skip_cause == "dependency"``) are retracted; a step
    skipped by ``remove_step`` is out of the plan for good. Runs to a fixpoint so both the
    cascade and the retraction reach the whole chain.
    """
    changed = True
    while changed:
        changed = False
        for member in members:
            state = container.get(member)
            step = steps_by_id.get(member)
            if state is None or step is None:
                continue
            blocker = _blocking_dependency(container, step)
            if state.status == "pending" and blocker is not None:
                dep, dep_status = blocker
                set_status(state, "skipped")
                state.skip_cause = "dependency"
                state.summary = (
                    f"Auto-skipped by Chief: dependency '{dep}' is {dep_status}."
                )
                changed = True
            elif state.status == "skipped" and state.skip_cause == "dependency" and not blocker:
                set_status(state, "pending")
                state.skip_cause = None
                state.summary = None
                changed = True


def _recompute_scope(
    container: dict[str, StepState],
    members: list[str],
    defn: WorkflowDefinition,
) -> None:
    steps_by_id = defn.steps_by_id()

    # Retract skips whose blocking failure has been replayed away, before deriving anything
    # from them.
    propagate_skips(container, members, steps_by_id)

    for member in members:
        state = container.get(member)
        step = steps_by_id.get(member)
        if state is None or step is None or not step.is_construct:
            continue
        if state.status == "skipped":
            # Server-set and terminal: a construct behind a failed dependency, or one an
            # amendment removed. Deriving from its (absent) instances would resurrect it.
            continue
        for instance in state.instances or []:
            _recompute_scope(instance.step_states, instance.body or step.body_ids, defn)
            derived = derive_instance_status(step, instance)
            if derived != instance.status:
                set_status(instance, derived)
        derived_step = derive_construct_status(step, state)
        if derived_step != state.status:
            set_status(state, derived_step)

    # Cascade any failure the derivation just surfaced.
    propagate_skips(container, members, steps_by_id)


def any_blocked(container: dict[str, StepState]) -> bool:
    """Is any step anywhere under here waiting on a person?

    A full-tree walk, not the top level: a checkpoint inside a loop body leaves its
    construct deriving ``running``, so scanning only top-level steps would let a run sit
    silently waiting with nothing on the surface saying so.
    """
    for state in container.values():
        if state.status == "blocked":
            return True
        for instance in state.instances or []:
            if any_blocked(instance.step_states):
                return True
    return False


def derive_run_status(run: RunState, defn: WorkflowDefinition) -> RunStatus:
    """``completed`` when every top-level step is completed/skipped, ``failed`` if any failed.

    Run-level failure is always fail-fast; the contract flags a per-run override as Open
    Item 4 and does not specify one.

    ``waiting_on_human`` is checked after both: a run that has already failed elsewhere is
    failed whatever else is outstanding, and ``blocked`` is not terminal so it cannot
    coexist with ``completed``.
    """
    ids = top_level_ids(defn.steps)
    states = [run.step_states.get(i) for i in ids]
    if any(s is not None and s.status == "failed" for s in states):
        return "failed"
    if all(s is not None and s.status in TERMINAL_OK for s in states):
        return "completed"
    if any_blocked(run.step_states):
        return "waiting_on_human"
    return "running"


def recompute(run: RunState, defn: WorkflowDefinition, *, paused: bool = False) -> None:
    """Refresh every derived field on the run, in place.

    ``paused`` forces ``paused_for_approval``; otherwise the status is re-derived, which is
    what lets a pause raised near the end of a run clear back into ``completed``/``failed``
    rather than being forced back to ``running`` (contract 1.3). See CONTRACT-NOTES.md #7.
    """
    _recompute_scope(run.step_states, top_level_ids(defn.steps), defn)
    run.status = "paused_for_approval" if paused else derive_run_status(run, defn)
    run.updated_at = now()
