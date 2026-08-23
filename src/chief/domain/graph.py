"""Structural rules for a WorkflowDefinition (REQ-34 to REQ-37).

Everything here operates on the definition alone — no run state is consulted, which is the
separation REQ-38 asks for. The same validation runs on submission (``POST /workflows``)
and on the post-amendment definition, so an amendment can never leave the plan malformed.
"""

from __future__ import annotations

from collections import defaultdict

from ..errors import ValidationFailed
from ..models import WorkflowDefinition, WorkflowStep

#: The harness a checkpoint declares. A value in the open namespace like any other, but the
#: one value Chief itself attaches a meaning to.
HUMAN_HARNESS = "human"


def containment(steps: list[WorkflowStep]) -> dict[str, str]:
    """Map body-step id -> the id of the construct whose ``body`` contains it."""
    parent: dict[str, str] = {}
    for step in steps:
        for child in step.body_ids:
            parent[child] = step.id
    return parent


def top_level_ids(steps: list[WorkflowStep]) -> list[str]:
    """Steps not contained in any ``body``, in declaration order.

    The contract's run-completion rule (1.3) is stated over "every top-level step" without
    defining the term; this is that definition. See CONTRACT-NOTES.md #4.
    """
    contained = containment(steps)
    return [s.id for s in steps if s.id not in contained]


def scope_members(steps: list[WorkflowStep], scope: str | None) -> list[str]:
    """Ids belonging to a dependency scope: the top level, or one construct's body."""
    if scope is None:
        return top_level_ids(steps)
    by_id = {s.id: s for s in steps}
    owner = by_id.get(scope)
    return owner.body_ids if owner else []


def _detect_cycle(nodes: list[str], edges: dict[str, list[str]]) -> list[str] | None:
    """Return a cycle as a list of ids, or None. Edges point from a step to its dependencies."""
    WHITE, GREY, BLACK = 0, 1, 2
    colour = dict.fromkeys(nodes, WHITE)
    stack: list[str] = []

    def visit(n: str) -> list[str] | None:
        colour[n] = GREY
        stack.append(n)
        for dep in edges.get(n, []):
            if dep not in colour:
                continue
            if colour[dep] == GREY:
                return stack[stack.index(dep) :] + [dep]
            if colour[dep] == WHITE:
                found = visit(dep)
                if found:
                    return found
        stack.pop()
        colour[n] = BLACK
        return None

    for n in nodes:
        if colour[n] == WHITE:
            found = visit(n)
            if found:
                return found
    return None


def validate_steps(steps: list[WorkflowStep]) -> None:
    """Raise ValidationFailed if the step set violates any structural rule."""
    if not steps:
        # Also reached via an amendment that removes the last step. An empty plan has no
        # top-level step, so a run against it could never reach a terminal status.
        raise ValidationFailed("a workflow must declare at least one step")

    seen: set[str] = set()
    for step in steps:
        if step.id in seen:
            raise ValidationFailed(f"duplicate step id '{step.id}'", details={"step_id": step.id})
        seen.add(step.id)

    by_id = {s.id: s for s in steps}

    # body presence and shape (contract 1.2)
    for step in steps:
        if step.is_construct:
            if not step.body_ids:
                raise ValidationFailed(
                    f"step '{step.id}' is type '{step.type}' and needs a non-empty body",
                    details={"step_id": step.id},
                )
            if len(set(step.body_ids)) != len(step.body_ids):
                raise ValidationFailed(
                    f"step '{step.id}' lists a duplicate id in its body",
                    details={"step_id": step.id},
                )
            # Normalise so the stored document always states the policy explicitly.
            step.on_instance_failure = step.failure_policy
            names = [p.name for p in step.instance_param_specs]
            if len(set(names)) != len(names):
                raise ValidationFailed(
                    f"step '{step.id}' declares the same instance parameter twice",
                    details={"step_id": step.id, "instance_params": names},
                )
            if step.exit_when is not None and step.type != "loop":
                raise ValidationFailed(
                    f"step '{step.id}' is type '{step.type}'; exit_when applies only to "
                    "loop steps — a parallel's branches all run, there is no exit decision",
                    details={"step_id": step.id},
                )
        else:
            if step.body is not None:
                raise ValidationFailed(
                    f"step '{step.id}' is type '{step.type}' and must not declare a body",
                    details={"step_id": step.id},
                )
            if step.on_instance_failure is not None:
                raise ValidationFailed(
                    f"step '{step.id}' is type '{step.type}'; on_instance_failure applies "
                    "only to loop/parallel steps",
                    details={"step_id": step.id},
                )
            if step.exit_when is not None:
                raise ValidationFailed(
                    f"step '{step.id}' is type '{step.type}'; exit_when applies only to loop steps",
                    details={"step_id": step.id},
                )
            if step.instance_params is not None:
                raise ValidationFailed(
                    f"step '{step.id}' is type '{step.type}'; instance_params applies only to "
                    "loop/parallel steps — a task runs once and has nothing to tell apart",
                    details={"step_id": step.id},
                )

        # Criteria are an attestation point, and a step has one only if a harness gets to
        # say how it turned out. A construct's status is derived from its instances and a
        # checkpoint's outcome is a person's to give, so neither has anywhere to answer for
        # a criterion — the body steps inside a construct do. See CONTRACT-NOTES.md #39.
        if step.criteria and step.type != "task":
            raise ValidationFailed(
                f"step '{step.id}' is type '{step.type}'; criteria apply only to task steps "
                "— put them on the steps in its body, which are the ones reported completed",
                details={"step_id": step.id},
            )

        # checkpoint shape (extension). A checkpoint is the one step Chief knows the
        # executor of, so naming any other harness is a plan that disagrees with itself.
        if step.is_checkpoint:
            if step.harness != HUMAN_HARNESS:
                raise ValidationFailed(
                    f"step '{step.id}' is type 'checkpoint'; its harness must be "
                    f"'{HUMAN_HARNESS}' — a person decides it, no harness executes it",
                    details={"step_id": step.id, "harness": step.harness},
                )
            names = [f.name for f in step.field_specs]
            if len(set(names)) != len(names):
                raise ValidationFailed(
                    f"checkpoint '{step.id}' asks for the same field twice",
                    details={"step_id": step.id, "fields": names},
                )
        elif step.fields is not None:
            # Deliberately not the converse rule: harness stays an open namespace, so a
            # *task* may still name a person as its executor. That is manual work someone
            # reports afterwards, which is a different thing from a decision the run waits on.
            raise ValidationFailed(
                f"step '{step.id}' is type '{step.type}'; fields applies only to checkpoint "
                "steps — a task's inputs come from the plan, not from a person at runtime",
                details={"step_id": step.id},
            )

        # workflow_ref shape (extension). Nothing to instantiate is a step that can never
        # move past 'running'.
        if step.is_workflow_ref:
            if not (step.ref_template_id or "").strip():
                raise ValidationFailed(
                    f"step '{step.id}' is type 'workflow_ref' and needs a ref_template_id "
                    "naming the template to instantiate as its child run",
                    details={"step_id": step.id},
                )
        elif step.ref_template_id is not None or step.ref_parameters:
            raise ValidationFailed(
                f"step '{step.id}' is type '{step.type}'; ref_template_id/ref_parameters "
                "apply only to workflow_ref steps",
                details={"step_id": step.id},
            )

    # body membership: referenced, existing, and owned by exactly one construct
    owners: dict[str, list[str]] = defaultdict(list)
    for step in steps:
        for child in step.body_ids:
            if child not in by_id:
                raise ValidationFailed(
                    f"step '{step.id}' body references unknown step '{child}'",
                    details={"step_id": step.id, "missing": child},
                )
            if child == step.id:
                raise ValidationFailed(
                    f"step '{step.id}' cannot contain itself", details={"step_id": step.id}
                )
            owners[child].append(step.id)
    for child, parents in owners.items():
        if len(parents) > 1:
            raise ValidationFailed(
                f"step '{child}' appears in more than one body: {sorted(parents)}",
                details={"step_id": child, "parents": sorted(parents)},
            )

    # containment must be a forest, not a cycle
    containment_edges = {s.id: list(s.body_ids) for s in steps}
    cycle = _detect_cycle([s.id for s in steps], containment_edges)
    if cycle:
        raise ValidationFailed(
            f"loop/parallel bodies form a containment cycle: {' -> '.join(cycle)}",
            details={"cycle": cycle},
        )

    # dependency scoping (contract 1.2: body steps may only depend on steps in the same body)
    parent = containment(steps)
    for step in steps:
        scope = parent.get(step.id)
        for dep in step.depends_on:
            if dep not in by_id:
                raise ValidationFailed(
                    f"step '{step.id}' depends_on unknown step '{dep}'",
                    details={"step_id": step.id, "missing": dep},
                )
            if dep == step.id:
                raise ValidationFailed(
                    f"step '{step.id}' depends on itself", details={"step_id": step.id}
                )
            if parent.get(dep) != scope:
                where = f"body of '{scope}'" if scope else "top level"
                raise ValidationFailed(
                    f"step '{step.id}' ({where}) may only depend on steps in the same scope; "
                    f"'{dep}' is not",
                    details={"step_id": step.id, "dependency": dep, "scope": scope},
                )

    # acyclic dependencies within each scope
    scopes: set[str | None] = {None} | set(parent.values())
    for scope in scopes:
        members = scope_members(steps, scope)
        edges = {m: [d for d in by_id[m].depends_on if d in members] for m in members}
        cycle = _detect_cycle(members, edges)
        if cycle:
            where = f"body of '{scope}'" if scope else "top level"
            raise ValidationFailed(
                f"dependency cycle in {where}: {' -> '.join(cycle)}",
                details={"cycle": cycle, "scope": scope},
            )


def validate_definition(defn: WorkflowDefinition) -> None:
    validate_steps(defn.steps)


def dependency_closure(steps: list[WorkflowStep], step_id: str) -> set[str]:
    """All ids reachable from ``step_id`` by following depends_on (its transitive deps)."""
    by_id = {s.id: s for s in steps}
    seen: set[str] = set()
    frontier = list(by_id[step_id].depends_on) if step_id in by_id else []
    while frontier:
        current = frontier.pop()
        if current in seen or current not in by_id:
            continue
        seen.add(current)
        frontier.extend(by_id[current].depends_on)
    return seen
