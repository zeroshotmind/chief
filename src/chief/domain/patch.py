"""Applying PatchOperations to a plan, and the immutability rules around them.

Contract 1.7, 1.8 and the invariants in section 4. Three separable concerns live here:

``classify_required_kind``
    Decide whether a set of operations is ``forward`` or must be ``history_edit``, by
    looking at the *current* status of each operation's target. This is the mechanical
    check REQ-14 asks for, run at submission time before a human ever sees the proposal.

``apply_to_steps``
    Produce the post-amendment step list. Pure, and validated as a whole so an amendment
    either fully applies or fully fails (never partially).

``apply_state_effects``
    The run-state consequences: history snapshots (REQ-42), replay resets, removal marking
    and materialisation of inserted steps.
"""

from __future__ import annotations

from copy import deepcopy

from ..errors import InvariantViolation, NotFound, ValidationFailed
from ..ids import now
from ..models import (
    HISTORY_LOCKED,
    MUTATING_OPS,
    AmendmentKind,
    PatchOperation,
    RunState,
    StepInstance,
    StepState,
    WorkflowDefinition,
    WorkflowStep,
)
from . import paths
from .derive import derive_instance_status, set_status
from .graph import containment, validate_steps

TERMINAL_ANY = frozenset({"completed", "failed", "skipped"})
_EXECUTED = frozenset({"running", "completed", "failed"})


# --- target resolution -------------------------------------------------------------------


def ancestors(steps: list[WorkflowStep], step_id: str) -> list[str]:
    """Construct ids enclosing ``step_id``, outermost first."""
    parent = containment(steps)
    chain: list[str] = []
    current = step_id
    while current in parent:
        current = parent[current]
        chain.append(current)
    chain.reverse()
    return chain


def target_state_path(defn: WorkflowDefinition, op: PatchOperation) -> tuple[list[str], str | None]:
    """Resolve an operation's target to ``(state_path, instance_id)``.

    ``instance_id`` is non-None when the operation addresses one instance *of* the target
    step rather than the step itself. The instance path must name exactly one instance per
    enclosing construct, plus optionally one more for the target's own instances.
    """
    step = defn.step(op.target_step_id)
    if step is None:
        raise NotFound(f"step '{op.target_step_id}' is not part of this workflow")

    chain = ancestors(defn.steps, op.target_step_id)
    supplied = op.resolved_instance_path()

    if len(supplied) == len(chain):
        instance_id = None
    elif len(supplied) == len(chain) + 1:
        if not step.is_construct:
            raise ValidationFailed(
                f"step '{step.id}' is type '{step.type}' and has no instances to scope to",
                details={"target_step_id": step.id},
            )
        instance_id = supplied[-1]
        supplied = supplied[:-1]
    else:
        raise ValidationFailed(
            f"step '{op.target_step_id}' is nested {len(chain)} level(s) deep; expected "
            f"{len(chain)} instance id(s) to address the step or {len(chain) + 1} to address "
            f"one of its instances, got {len(op.resolved_instance_path())}",
            details={"target_step_id": op.target_step_id, "ancestors": chain},
        )

    path: list[str] = []
    for depth, construct in enumerate(chain):
        path.extend([construct, supplied[depth]])
    path.append(op.target_step_id)
    return path, instance_id


def _materialised_statuses(
    run: RunState, defn: WorkflowDefinition, op: PatchOperation
) -> list[str]:
    """Current statuses of everything this operation would touch.

    Unscoped operations on a body step reach every instance the step is materialised in, so
    the status check is per target rather than per parent step (contract 1.7). Scoped
    operations must resolve — a typo in an instance id used to make the target look absent,
    which classified an edit of completed work as ``forward``.
    """
    if not op.resolved_instance_path():
        return [
            paths.resolve(run, found)[1].status
            for found in paths.find_step_paths(run, op.target_step_id)
        ]

    path, instance_id = target_state_path(defn, op)
    _, state, _ = paths.resolve(run, path)
    if instance_id is None:
        return [state.status]
    instance = state.instance(instance_id)
    if instance is None:
        raise NotFound(f"instance '{instance_id}' not found on step '{op.target_step_id}'")
    return [instance.status]


def _has_run(state: StepState) -> bool:
    """Whether this step has an execution record worth protecting.

    ``skipped`` is bookkeeping, not execution: a step skipped behind a failure, or marked
    skipped because an amendment removed it, never ran and is still free to be restructured.
    A history entry from an actual execution counts even when the live status is
    ``pending`` — that is a replayed step, and its preserved prior result (REQ-42) must not
    be dropped by a later restructuring. Bookkeeping snapshots (a skip being re-marked as a
    removal) do not count, since nothing ran.
    """
    if state.status in _EXECUTED:
        return True
    return any(entry.get("status") in _EXECUTED for entry in state.history)


def _run_record_reason(state: StepState) -> str:
    """Why a step counts as having run, phrased for an error message."""
    if state.status in _EXECUTED:
        return f"is '{state.status}'"
    return "carries a preserved result from an earlier run"


def created_ids(operations: list[PatchOperation]) -> set[str]:
    """Step ids an operation set introduces, so later operations may target them."""
    return {
        op.step.id for op in operations if op.op in ("insert_after", "insert_before") and op.step
    }


def validate_targets(
    run: RunState, defn: WorkflowDefinition, operations: list[PatchOperation]
) -> None:
    """Resolve every operation's target at submission time.

    Without this an amendment naming an instance that does not exist is accepted, pins the
    run in ``paused_for_approval``, and then fails at approval — leaving the run stuck
    behind a proposal that can never be applied.
    """
    pending_ids: set[str] = set()
    for op in operations:
        # A step introduced by an earlier operation in this same set has no entry in the
        # current plan and no run state. Only the "does this step exist" check is waived
        # for it; anything that needs recorded state is still impossible and is refused
        # here rather than at approval, where it would pin the run.
        is_fresh = op.target_step_id in pending_ids
        if op.op in ("insert_after", "insert_before") and op.step:
            pending_ids.add(op.step.id)
        if is_fresh:
            if op.resolved_instance_path():
                raise ValidationFailed(
                    f"step '{op.target_step_id}' is introduced by this amendment and has no "
                    "instances yet, so the operation cannot be scoped to one",
                    details={"target_step_id": op.target_step_id},
                )
            if op.op == "replay_step":
                raise InvariantViolation(
                    f"step '{op.target_step_id}' is introduced by this amendment and has "
                    "never run; there is nothing to replay",
                    details={"target_step_id": op.target_step_id},
                )
            continue
        if defn.step(op.target_step_id) is None:
            raise NotFound(f"step '{op.target_step_id}' is not part of this workflow")
        if op.resolved_instance_path():
            path, instance_id = target_state_path(defn, op)
            _, state, _ = paths.resolve(run, path)
            if instance_id is not None and state.instance(instance_id) is None:
                raise NotFound(f"instance '{instance_id}' not found on step '{op.target_step_id}'")
        elif op.op == "replay_step" and not paths.find_step_paths(run, op.target_step_id):
            raise NotFound(
                f"replay_step targets '{op.target_step_id}', which has no recorded state "
                "in this run"
            )
        if op.op == "replay_step":
            statuses = _materialised_statuses(run, defn, op)
            if not any(status in HISTORY_LOCKED for status in statuses):
                raise InvariantViolation(
                    f"replay_step targets '{op.target_step_id}', which has not completed or "
                    f"failed (currently {sorted(set(statuses)) or ['no recorded state']}); "
                    "there is nothing to replay",
                    details={"target_step_id": op.target_step_id, "statuses": statuses},
                )


def classify_required_kind(
    run: RunState, defn: WorkflowDefinition, operations: list[PatchOperation]
) -> AmendmentKind:
    """``history_edit`` if any operation alters or re-runs an already completed/failed target.

    ``insert_after``/``insert_before`` are excluded: inserting a neighbour neither alters nor
    re-executes the target, which is exactly what REQ-14 protects. ``replay_step`` is always
    a history edit by definition (contract 1.8). See CONTRACT-NOTES.md #3.
    """
    fresh = created_ids(operations)
    for op in operations:
        # Unconditional, and checked before the fresh-id skip: contract 1.8 makes
        # replay_step a history edit regardless of what it targets.
        if op.op == "replay_step":
            return "history_edit"
        if op.target_step_id in fresh:
            continue  # a step this same amendment introduces has no history to touch
        if op.op not in MUTATING_OPS:
            continue
        if any(status in HISTORY_LOCKED for status in _materialised_statuses(run, defn, op)):
            return "history_edit"
    return "forward"


def check_type_changes(
    run: RunState, defn: WorkflowDefinition, operations: list[PatchOperation]
) -> None:
    """Refuse to change a step's ``type`` once it has started.

    The contract does not say whether ``update_step`` may turn a task into a loop. Doing so
    on a step that has already run would leave incoherent state behind (instances recorded
    against something that is no longer a construct, or a construct with no way to reach
    its derived status), so it is only allowed while the step is still ``pending``.
    """
    for op in operations:
        if op.op != "update_step" or op.step is None:
            continue
        current = defn.step(op.target_step_id)
        if current is None or current.type == op.step.type:
            continue
        for found in paths.find_step_paths(run, op.target_step_id):
            _, state, _ = paths.resolve(run, found)
            if _has_run(state):
                raise InvariantViolation(
                    f"step '{op.target_step_id}' {_run_record_reason(state)}; its type "
                    f"cannot be changed from '{current.type}' to '{op.step.type}' once it "
                    "has an execution record. Remove it and insert a replacement instead",
                    details={"step_id": op.target_step_id, "status": state.status},
                )


def check_scope_moves(run: RunState, before: WorkflowDefinition, after: WorkflowDefinition) -> None:
    """Refuse to move a step that has already started into or out of a construct body.

    An amendment can rewire a body, which relocates a step between the top level and a
    loop/parallel body. Doing that to a step that has already run would strand its recorded
    result in a scope that no longer exists, so it is only allowed while the step is still
    ``pending``.
    """
    before_scope = containment(before.steps)
    after_scope = containment(after.steps)
    known_before = before.steps_by_id()
    for step in after.steps:
        if step.id not in known_before:
            continue
        if before_scope.get(step.id) == after_scope.get(step.id):
            continue
        for found in paths.find_step_paths(run, step.id):
            _, state, _ = paths.resolve(run, found)
            if _has_run(state):
                raise InvariantViolation(
                    f"step '{step.id}' {_run_record_reason(state)}; it cannot be moved "
                    "between the top level and a loop/parallel body once it has an "
                    "execution record, because that record belongs to the scope it ran in",
                    details={"step_id": step.id, "status": state.status},
                )


# --- definition rewriting ----------------------------------------------------------------


def _insert_into(sequence: list[str], target: str, new_id: str, after: bool) -> None:
    index = sequence.index(target)
    sequence.insert(index + 1 if after else index, new_id)


def apply_to_steps(
    steps: list[WorkflowStep],
    operations: list[PatchOperation],
    *,
    retired_step_ids: set[str] | None = None,
) -> list[WorkflowStep]:
    """Return the post-amendment step list, or raise. Never mutates the input.

    Validation runs once over the *resulting* state, so a ``remove_step`` paired with an
    ``update_step`` that rewires the reference is accepted while an unpaired one is not.
    """
    working = [s.model_copy(deep=True) for s in steps]
    known_ids = {s.id for s in working} | (retired_step_ids or set())

    for op in operations:
        by_id = {s.id: s for s in working}
        if op.op in ("insert_after", "insert_before"):
            assert op.step is not None
            if op.target_step_id not in by_id:
                raise NotFound(f"insert target '{op.target_step_id}' is not part of this workflow")
            if op.step.id in known_ids:
                raise ValidationFailed(
                    f"step id '{op.step.id}' is already in use; ids are permanent and are "
                    "never reused (REQ-35)",
                    details={"step_id": op.step.id},
                )
            known_ids.add(op.step.id)
            parent = containment(working).get(op.target_step_id)
            after = op.op == "insert_after"
            if parent is None:
                _insert_into_steps(working, op.target_step_id, op.step, after)
            else:
                body = list(by_id[parent].body_ids)
                _insert_into(body, op.target_step_id, op.step.id, after)
                by_id[parent].body = body
                _insert_into_steps(working, op.target_step_id, op.step, after)

        elif op.op == "update_step":
            assert op.step is not None
            if op.target_step_id not in by_id:
                raise NotFound(f"step '{op.target_step_id}' is not part of this workflow")
            if op.step.id != op.target_step_id:
                raise ValidationFailed(
                    "update_step cannot change a step id; ids are permanent (REQ-35). "
                    f"target_step_id='{op.target_step_id}' but step.id='{op.step.id}'",
                    details={"target_step_id": op.target_step_id, "step_id": op.step.id},
                )
            position = next(i for i, s in enumerate(working) if s.id == op.target_step_id)
            working[position] = op.step.model_copy(deep=True)

        elif op.op == "remove_step":
            if op.target_step_id not in by_id:
                raise NotFound(f"step '{op.target_step_id}' is not part of this workflow")
            working = [s for s in working if s.id != op.target_step_id]

        elif op.op == "replay_step":
            if op.target_step_id not in by_id:
                raise NotFound(f"step '{op.target_step_id}' is not part of this workflow")
            # No definition change; the effect is entirely on run state.

    validate_steps(working)
    return working


def _insert_into_steps(
    working: list[WorkflowStep], target_id: str, step: WorkflowStep, after: bool
) -> None:
    index = next(i for i, s in enumerate(working) if s.id == target_id)
    working.insert(index + 1 if after else index, step.model_copy(deep=True))


# --- run-state effects -------------------------------------------------------------------


def _snapshot(state: StepState) -> dict:
    entry = state.snapshot()
    entry["superseded_at"] = now()
    return entry


def _reset(state: StepState) -> None:
    state.summary = None
    state.skip_cause = None
    state.artifacts = []
    state.metadata = {}
    # Evidence belongs to the attempt that produced it. Carried across a replay it would
    # answer the new attempt's criteria with the old attempt's work — and since the gate
    # only checks that each criterion has *an* answer, the replayed step would sail through
    # the one path where re-checking matters most. The snapshot in `history` keeps it.
    state.criteria_met = {}
    state.instances = [] if state.instances is not None else None
    state.instances_closed = False
    # A replayed checkpoint is being asked again, so the previous answer stops being the
    # current one. Nothing is lost — the caller snapshots into `history` first — and leaving
    # it attached would show a pending step reading as already decided.
    state.checkpoint = None
    # Likewise a replayed workflow_ref: the old child run stays in the store (it is not
    # deleted or re-linked), but this step is asking for a fresh one, so the stale id is
    # dropped rather than left pointing at a run this attempt has nothing to do with.
    state.child_run_id = None
    # And any question the previous attempt asked: the snapshot in `history` keeps the
    # record of what was asked and answered, but a fresh attempt starts with nothing
    # outstanding rather than reading as still blocked on a question this attempt never
    # asked.
    state.questions = []
    set_status(state, "pending")


def _reset_instance(instance: StepInstance) -> None:
    snapshot = instance.model_dump(mode="json", exclude={"history"})
    snapshot["superseded_at"] = now()
    instance.history.append(snapshot)
    instance.summary = None
    instance.artifacts = []
    instance.metadata = {}
    for entry in instance.step_states.values():
        _reset(entry)
    set_status(instance, "pending")


def _drop_stale_evidence(
    state: StepState, before: WorkflowStep | None, after: WorkflowStep | None
) -> None:
    """Forget what was said about a criterion whose wording has changed.

    Criterion ids are positional, so rewording c1 leaves it addressed as c1 — and the
    evidence recorded against the old wording would then stand as an answer to the new one.
    That is the replay problem in a smaller shape: an answer belongs to the question it was
    given for. Only the changed ones are dropped, so an amendment that adds a fourth
    criterion does not discard the three already answered.
    """
    if before is None or after is None or not state.criteria_met:
        return
    was = {c.id: c.text for c in before.criteria}
    for criterion in after.criteria:
        if was.get(criterion.id) != criterion.text:
            state.criteria_met.pop(criterion.id, None)
    for gone in set(state.criteria_met) - {c.id for c in after.criteria}:
        state.criteria_met.pop(gone, None)


def apply_state_effects(
    run: RunState,
    defn_before: WorkflowDefinition,
    defn_after: WorkflowDefinition,
    operations: list[PatchOperation],
) -> None:
    """Apply the run-state consequences of an already-validated operation set."""
    for op in operations:
        if op.op in ("insert_after", "insert_before"):
            continue

        scoped = bool(op.resolved_instance_path())
        if scoped:
            path, instance_id = target_state_path(defn_before, op)
            targets = [path]
        else:
            # An unscoped operation on a body step reaches every instance it is
            # materialised in; on a top-level step that is exactly one place.
            path, instance_id = None, None
            targets = paths.find_step_paths(run, op.target_step_id)

        if op.op == "replay_step":
            if instance_id is not None:
                assert path is not None
                _, state, _ = paths.resolve(run, path)
                instance = state.instance(instance_id)
                if instance is None:
                    raise NotFound(f"instance '{instance_id}' not found on '{op.target_step_id}'")
                _reset_instance(instance)
            else:
                if not targets:
                    raise InvariantViolation(
                        f"replay_step targets '{op.target_step_id}', which has no recorded state"
                    )
                replayed = 0
                for found in targets:
                    _, state, _ = paths.resolve(run, found)
                    if state.status not in HISTORY_LOCKED:
                        # An unscoped replay of a body step reaches every instance. Only the
                        # copies that actually finished are replayed; discarding an in-flight
                        # sibling's work is not what "replay this step" means.
                        continue
                    state.history.append(_snapshot(state))
                    _reset(state)
                    replayed += 1
                if not replayed:
                    raise InvariantViolation(
                        f"replay_step targets '{op.target_step_id}', which has not completed "
                        "or failed anywhere in this run"
                    )
            continue

        for found in targets:
            try:
                _, state, _ = paths.resolve(run, found)
            except NotFound:
                continue
            if op.op == "update_step":
                # Preserve the prior result before the amended definition takes effect
                # (REQ-42). The live state is left alone: update_step changes the plan,
                # replay_step is what re-runs it.
                if state.status in HISTORY_LOCKED:
                    state.history.append(_snapshot(state))
                _drop_stale_evidence(state, defn_before.step(op.target_step_id), op.step)
            elif op.op == "remove_step":
                # Preserve rather than delete the record (contract 1.4).
                if state.skip_cause != "removed":
                    # Also covers a step already skipped behind a failed dependency: the
                    # record has to say it left the plan, not just that it was blocked.
                    if state.status != "pending":
                        state.history.append(_snapshot(state))
                    set_status(state, "skipped")
                    state.skip_cause = "removed"
                    state.summary = "Removed from the plan by an approved amendment."

    _resync_structure(run, defn_after)


def _resync_structure(run: RunState, defn: WorkflowDefinition) -> None:
    """Bring materialised state in line with the amended definition.

    Top-level steps added by the amendment get a ``pending`` state. In-flight instances pick
    up body changes; instances that already reached a terminal status keep the body they
    ran, so a forward amendment cannot retroactively un-complete them (REQ-14).
    """
    steps_by_id = defn.steps_by_id()
    contained = containment(defn.steps)

    for step in defn.steps:
        if step.id not in contained and step.id not in run.step_states:
            run.step_states[step.id] = StepState(step_id=step.id)
    # A step that an amendment moved into a body is no longer top level. Only untouched
    # states are dropped; check_scope_moves has already refused the case where one ran.
    for step_id in list(run.step_states):
        state = run.step_states[step_id]
        step = steps_by_id.get(step_id)
        moved_into_body = step is not None and step_id in contained
        if moved_into_body and not _has_run(state):
            # Same gate check_scope_moves used to permit the move, so a skipped step does
            # not leave a contradictory second record behind at the top level.
            del run.step_states[step_id]
        elif step is None and state.status == "pending":
            del run.step_states[step_id]

    def walk(container: dict[str, StepState]) -> None:
        for step_id, state in list(container.items()):
            step = steps_by_id.get(step_id)
            if step is None or not step.is_construct:
                continue
            for instance in state.instances or []:
                # Derive rather than read the stored field: an earlier operation in this
                # same amendment may have replayed the instance, which makes it live again
                # in substance while `status` still says it finished. `recompute` only
                # refreshes that field afterwards, which is too late to decide this.
                live = derive_instance_status(step, instance)
                if live not in TERMINAL_ANY:
                    _resync_instance_body(instance, step)
                walk(instance.step_states)

    walk(run.step_states)


def _resync_instance_body(instance: StepInstance, step: WorkflowStep) -> None:
    body = step.body_ids
    for body_step_id in body:
        existing_state = instance.step_states.get(body_step_id)
        if existing_state is None:
            instance.step_states[body_step_id] = StepState(step_id=body_step_id)
        elif existing_state.skip_cause == "removed":
            # Put back into the body by a later amendment: it never ran, so it becomes
            # runnable again rather than staying marked as removed.
            _reset(existing_state)
    for existing in list(instance.step_states):
        if existing not in body:
            state = instance.step_states[existing]
            if state.skip_cause != "removed":
                if state.status != "pending":
                    state.history.append(_snapshot(state))
                set_status(state, "skipped")
                state.skip_cause = "removed"
                state.summary = "Removed from the plan by an approved amendment."
    instance.body = list(body)


def preview_definition(
    defn: WorkflowDefinition, operations: list[PatchOperation], *, retired: set[str]
) -> WorkflowDefinition:
    """Definition as it would look after the operations, without touching the stored one."""
    candidate = deepcopy(defn)
    candidate.steps = apply_to_steps(defn.steps, operations, retired_step_ids=retired)
    return candidate


def dry_run_state_effects(
    run: RunState,
    defn_before: WorkflowDefinition,
    defn_after: WorkflowDefinition,
    operations: list[PatchOperation],
) -> None:
    """Apply the state effects to a throwaway copy of the run, purely to see if they raise.

    Operations apply *sequentially*, so an earlier one can destroy the state a later one was
    checked against — replaying a construct deletes the instance the next operation is scoped
    to, replaying a step twice leaves nothing for the second replay to do. Checking each
    operation against the initial snapshot misses all of that and produces an amendment that
    is accepted, cannot be approved, and pins the run behind it.

    Simulating the real application closes the whole class rather than the known cases.
    """
    apply_state_effects(run.model_copy(deep=True), defn_before, defn_after, operations)
