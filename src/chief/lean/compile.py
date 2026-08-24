"""Lowering a verified plan into an ordinary workflow.

Compilation is deliberately dull. Everything interesting happened in Lean; what is left is a
translation, and the less it invents the better. A node becomes a step, a data-dependency edge
becomes an entry in ``depends_on`` and an entry in ``inputs``, and the contract a step promised
becomes a criterion it will have to answer for when it reports done.

That last move is the one worth explaining. Chief has no field for "what this step produces",
and adding one would mean a schema change for something the existing acceptance machinery
already models: a criterion is a condition that has to hold before a step may be called done,
which is exactly what a postcondition is. So the promise proven at plan time is restated as a
criterion checked at run time. The two are not the same claim — Lean proved the contracts line
up for every possible value, the criterion asks a harness to confirm this particular value —
and that is the intended division. The shape was settled before anything ran; the runtime only
confirms the instance.

Nothing downstream can tell a compiled plan from a hand-written one, and that is the point: it
approves, runs, amends and archives like any other workflow.
"""

from __future__ import annotations

from ..models import PlanGraph, PlanNode, WorkflowCreate, WorkflowStep

#: A produced contract, restated as the condition the harness must answer for. Worded as a
#: check rather than as a promise because that is what a criterion is read as at report time.
_PRODUCES = "the {artifact} this step produces satisfies: {contract}"


def _inputs_for(node: PlanNode) -> dict[str, object]:
    """What the plan knows about each artifact this step reads.

    Keyed by the edge's label, which is the name the step's author gave it, so a harness reads
    ``inputs["dataset"]`` and gets the type it is expecting and the condition that was proven
    about it. ``from_step`` is redundant with ``depends_on`` and kept anyway — it says *why*
    the dependency exists, which ``depends_on`` alone cannot.
    """
    return {
        port.label: {
            "artifact_type": port.artifact_type,
            "contract": port.contract,
            "from_step": port.source,
            "proven": port.refined,
        }
        for port in node.inputs
    }


def _outputs_for(node: PlanNode) -> dict[str, object]:
    """What the step promised about what it produces.

    Carried as data as well as restated as a criterion, and the two are not redundant: the
    criterion is what a harness answers for at report time, this is what a reader sees at plan
    time. Both are written here from the same port, so they cannot drift.
    """
    port = node.produces
    if port is None:
        return {}
    return {
        port.label: {
            "artifact_type": port.artifact_type,
            "contract": port.contract,
            "proven": port.refined,
        }
    }


def _step_for(node: PlanNode) -> WorkflowStep:
    criteria = list(node.criteria)
    if node.type == "task" and node.produces is not None and node.produces.refined:
        criteria.append(
            _PRODUCES.format(
                artifact=node.produces.artifact_type, contract=node.produces.contract
            )
        )

    step: dict[str, object] = {
        "id": node.id,
        "type": node.type,
        "goal": node.goal,
        "harness": node.harness,
        "depends_on": list(node.depends_on),
        "group": node.group,
        "inputs": _inputs_for(node),
        "outputs": _outputs_for(node),
    }
    if node.type == "checkpoint":
        # Criteria are task-only, and a checkpoint's outcome is a person's to give — there is
        # nowhere for a harness to answer for a condition here. The fields are what the plan
        # asks them for.
        step["fields"] = [{"name": name} for name in node.fields]
    else:
        step["criteria"] = criteria
    return WorkflowStep.model_validate(step)


def compile_plan(
    graph: PlanGraph,
    *,
    title: str | None = None,
    project: str | None = None,
    origin_dir: str | None = None,
    generated_by: str | None = None,
) -> WorkflowCreate:
    """Turn a verified plan's graph into a draft workflow.

    The result is a request body, not a stored workflow: it goes through ``create_workflow``
    like anything else, so a compiled plan is validated by the same rules as a hand-written
    one. A plan that somehow produced an invalid graph is refused there, not trusted here.
    """
    return WorkflowCreate(
        title=title or graph.title,
        source="generated",
        generated_by=generated_by,
        project=project,
        origin_dir=origin_dir,
        steps=[_step_for(node) for node in graph.nodes],
    )
