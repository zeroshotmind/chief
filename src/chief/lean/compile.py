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

import re

from ..models import WorkflowCreate, WorkflowStep
from .verify import Diagnostic, PlanGraph, PlanNode

#: A produced contract, restated as the condition the harness must answer for. Worded as a
#: check rather than as a promise because that is what a criterion is read as at report time.
_PRODUCES = "the {artifact} this step produces satisfies: {contract}"

_DEF = re.compile(r"^\s*def\s+(?P<name>[A-Za-z_][A-Za-z0-9_'!?]*)", re.MULTILINE)
_STEP_CALL = re.compile(r"\b(?:task|checkpoint)\s+\"(?P<id>[^\"]+)\"")


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
        "inputs": _inputs_for(node),
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


def attribute_diagnostics(
    source: str, graph: PlanGraph | None, diagnostics: list[Diagnostic]
) -> list[Diagnostic]:
    """Best-effort: point each diagnostic at the step it is about.

    A failing entailment is reported where the edge is written, which is in the plan's ``do``
    block — a line naming the consuming *function*, not the step id. So the mapping goes
    through the source: each ``def`` is scanned for the ``task``/``checkpoint`` call inside it,
    which gives function name to step id, and a diagnostic is attributed to the definition it
    falls inside, or failing that to whichever known function is named on its own line.

    Heuristic, and labelled as such. It decides which node a failure is drawn on in the UI, and
    nothing else — the message stays exactly as Lean wrote it, and a diagnostic that cannot be
    placed simply has no ``step_id``.

    The step ids come from the source rather than from ``graph``, because the case this exists
    for is the case where there is no graph: a plan that failed to compile printed nothing, and
    that is precisely when a reader needs to know which node broke.
    """
    lines = source.splitlines()
    known = (
        {node.id for node in graph.nodes}
        if graph is not None
        else {m.group("id") for m in _STEP_CALL.finditer(source)}
    )

    spans: list[tuple[int, str]] = [
        (source.count("\n", 0, m.start()) + 1, m.group("name")) for m in _DEF.finditer(source)
    ]
    owner: dict[str, str] = {}
    for index, (start, name) in enumerate(spans):
        end = spans[index + 1][0] if index + 1 < len(spans) else len(lines) + 1
        body = "\n".join(lines[start - 1 : end - 1])
        call = _STEP_CALL.search(body)
        if call and call.group("id") in known:
            owner[name] = call.group("id")

    def enclosing(line: int) -> str | None:
        found = None
        for start, name in spans:
            if start <= line:
                found = name
            else:
                break
        return found

    out: list[Diagnostic] = []
    for diagnostic in diagnostics:
        step_id = None
        if diagnostic.line is not None:
            name = enclosing(diagnostic.line)
            if name is not None and name in owner:
                step_id = owner[name]
            elif 1 <= diagnostic.line <= len(lines):
                text = lines[diagnostic.line - 1]
                for candidate, mapped in owner.items():
                    if re.search(rf"\b{re.escape(candidate)}\b", text):
                        step_id = mapped
                        break
        out.append(diagnostic if step_id is None else diagnostic.model_copy(
            update={"step_id": step_id}
        ))
    return out
