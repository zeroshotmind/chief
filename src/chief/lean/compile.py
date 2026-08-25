"""Lowering a verified proof graph into an ordinary workflow.

Compilation is deliberately dull. Everything interesting happened in Lean; what is left is a
translation, and the less it invents the better. A node becomes a step, a data-dependency edge
becomes an entry in ``depends_on`` and an entry in ``inputs``, and the contract a step promised
becomes a criterion it will have to answer for when it reports done.

That last move is the one worth explaining. Chief has no field for "what this step produces",
and adding one would mean a schema change for something the existing acceptance machinery
already models: a criterion is a condition that has to hold before a step may be called done,
which is exactly what a postcondition is. So the promise proven at graph time is restated as a
criterion checked at run time. The two are not the same claim — Lean proved the contracts line
up for every possible value, the criterion asks a harness to confirm this particular value —
and that is the intended division. The shape was settled before anything ran; the runtime only
confirms the instance.

Nothing downstream can tell a compiled graph from a hand-written one, and that is the point: it
approves, runs, amends and archives like any other workflow.
"""

from __future__ import annotations

from ..models import ExtractedGraph, GraphNode, WorkflowCreate, WorkflowStep

#: A produced contract, restated as the condition the harness must answer for. Worded as a
#: check rather than as a promise because that is what a criterion is read as at report time.
_PRODUCES = "the {artifact} this step produces satisfies: {contract}"

#: A derived schema, restated the same way: the produced document must actually carry the
#: fields the plan's structure declares, and the harness validates that at report time.
_SCHEMA = "the {artifact} this step produces is a document with fields: {fields}"


def _schema_of(port) -> dict[str, object]:
    """The schema as data, nesting where the graph's row types were derived too."""

    def entry(field) -> object:
        if not field.fields:
            return field.type
        return {"type": field.type, "fields": {f.name: entry(f) for f in field.fields}}

    return {field.name: entry(field) for field in port.schema_}


def _schema_text(fields) -> str:
    """The schema as one criterion clause. Nesting goes one level deep here — the row type
    is what a validating harness needs; anything deeper is on the outputs data."""
    parts = []
    for field in fields:
        if field.fields:
            inner = ", ".join(f"{f.name} ({f.type})" for f in field.fields)
            parts.append(f"{field.name} ({field.type}: {inner})")
        else:
            parts.append(f"{field.name} ({field.type})")
    return ", ".join(parts)


def _inputs_for(node: GraphNode) -> dict[str, object]:
    """What the graph knows about each artifact this step reads.

    Keyed by the edge's label, which is the name the step's author gave it, so a harness reads
    ``inputs["dataset"]`` and gets the type it is expecting and the condition that was proven
    about it. ``from_step`` is redundant with ``depends_on`` and kept anyway — it says *why*
    the dependency exists, which ``depends_on`` alone cannot.
    """
    contracted: dict[str, object] = {
        port.label: {
            "artifact_type": port.artifact_type,
            "contract": port.contract,
            "from_step": port.source,
            "proven": port.refined,
            **({"schema": _schema_of(port)} if port.schema_ else {}),
        }
        for port in node.inputs
    }
    # Fixed inputs ride beside the contracted ones, in the artifact shape the run screens
    # already render: a thing with a type and a ref, not a condition.
    for fixed in node.fixed:
        contracted[fixed.label] = {
            "type": "file",
            "ref": fixed.ref,
            "description": fixed.description or fixed.label,
        }
    return contracted


def _outputs_for(node: GraphNode) -> dict[str, object]:
    """What the step promised about what it produces.

    Carried as data as well as restated as a criterion, and the two are not redundant: the
    criterion is what a harness answers for at report time, this is what a reader sees
    before anything runs. Both are written here from the same port, so they cannot drift.
    """
    port = node.produces
    if port is None:
        return {}
    return {
        port.label: {
            "artifact_type": port.artifact_type,
            "contract": port.contract,
            "proven": port.refined,
            **({"schema": _schema_of(port)} if port.schema_ else {}),
        }
    }


def _step_for(node: GraphNode) -> WorkflowStep:
    criteria = list(node.criteria)
    if node.type == "task" and node.produces is not None and node.produces.refined:
        criteria.append(
            _PRODUCES.format(
                artifact=node.produces.artifact_type, contract=node.produces.contract
            )
        )
    if node.type == "task" and node.produces is not None and node.produces.schema_:
        criteria.append(
            _SCHEMA.format(
                artifact=node.produces.artifact_type,
                fields=_schema_text(node.produces.schema_),
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
    # The algorithm travels too. It was checked at graph time and it is most useful at run
    # time — the person watching a step execute is the one who wants the exact operators —
    # so dropping it here would strand it on the screen nobody revisits.
    if node.algorithm is not None:
        step["algorithm"] = node.algorithm.model_dump()
    if node.type == "checkpoint":
        # Criteria are task-only, and a checkpoint's outcome is a person's to give — there is
        # nowhere for a harness to answer for a condition here. The fields are what the plan
        # asks them for.
        step["fields"] = [{"name": name} for name in node.fields]
    else:
        step["criteria"] = criteria
    return WorkflowStep.model_validate(step)


def compile_graph(
    graph: ExtractedGraph,
    *,
    title: str | None = None,
    project: str | None = None,
    origin_dir: str | None = None,
    generated_by: str | None = None,
) -> WorkflowCreate:
    """Turn a verified proof graph into a draft workflow.

    The result is a request body, not a stored workflow: it goes through ``create_workflow``
    like anything else, so a compiled graph is validated by the same rules as a hand-written
    one. A graph that somehow produced an invalid extraction is refused there, not trusted here.
    """
    return WorkflowCreate(
        title=title or graph.title,
        source="generated",
        generated_by=generated_by,
        project=project,
        origin_dir=origin_dir,
        steps=[_step_for(node) for node in graph.nodes],
    )
