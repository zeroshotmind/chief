"""The plan endpoints: storing a candidate, checking it, and lowering it into a workflow.

Most of this needs no Lean. Verification does, and is skipped where there is no toolchain —
but the rules *around* the verdict are the ones most worth pinning down, and they are all
testable by writing a verdict into the store directly: that a revision drops it, that a plan
verified by a toolchain nobody is running any more does not count as verified, and that a
workflow compiled from a plan is an ordinary workflow in every respect.
"""

from __future__ import annotations

import pytest

from chief.ids import now
from chief.ids import proof_graph_id as new_graph_id
from chief.lean import available, package_dir
from chief.models import ExtractedGraph, ProofGraph, VerifyResult

needs_lean = pytest.mark.skipif(not available(), reason="no Lean toolchain on this machine")

MINIMAL_SOURCE = 'import ProofGraph\ndef graph : GraphM Unit := pure ()\n#eval emitGraph "x" plan\n'


def example_source() -> str:
    package = package_dir()
    assert package is not None
    return (package / "Examples" / "Pipeline.lean").read_text(encoding="utf-8")


GRAPH = {
    "schema": "chief.proofgraph/v1",
    "title": "Stored plan",
    "nodes": [
        {
            "id": "fit",
            "type": "task",
            "goal": "Fit it.",
            "harness": "claude",
            "criteria": [],
            "fields": [],
            "depends_on": [],
            "inputs": [],
            "produces": {
                "label": "out",
                "source": "fit",
                "artifact_type": "Model",
                "contract": "auc ≥ 80",
                "refined": True,
            },
        }
    ],
    "problems": [],
    "stats": {
        "nodes": 1,
        "edges": 0,
        "contracts_total": 1,
        "contracts_refined": 1,
        "contracts_any": 0,
    },
}


def store_verified_plan(store, *, toolchain: str) -> ProofGraph:
    """A plan carrying a verdict, without needing a toolchain to produce one."""
    stamp = now()
    plan = ProofGraph(
        graph_id=new_graph_id(),
        title="Stored plan",
        lean_source=MINIMAL_SOURCE,
        status="verified",
        verified_at=stamp,
        verification=VerifyResult(
            status="verified",
            toolchain=toolchain,
            graph=ExtractedGraph.model_validate(GRAPH),
        ),
        created_at=stamp,
        updated_at=stamp,
    )
    with store.transaction() as conn:
        store.create_proof_graph(conn, plan)
    return plan


def create(client, **kw):
    body = {"title": "A plan", "lean_source": MINIMAL_SOURCE, **kw}
    return client.post("/v1/proof-graphs", json=body)


# --------------------------------------------------------------------------- the document


def test_a_new_plan_is_a_draft_that_has_not_been_checked(client) -> None:
    response = create(client, project="chief")

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "draft"
    assert body["verification"] is None
    assert body["verified_at"] is None
    assert body["compiled_to"] == []
    assert body["project"] == "chief"


def test_plans_are_listed_newest_first_and_filterable(client) -> None:
    create(client, project="one")
    create(client, project="two")

    assert len(client.get("/v1/proof-graphs").json()) == 2
    assert len(client.get("/v1/proof-graphs", params={"project": "one"}).json()) == 1
    assert len(client.get("/v1/proof-graphs", params={"status": "verified"}).json()) == 0


def test_a_missing_plan_is_a_404(client) -> None:
    assert client.get("/v1/proof-graphs/pg_nope").status_code == 404


def test_the_toolchain_route_says_whether_checking_is_possible_here(client) -> None:
    body = client.get("/v1/proof-graphs/toolchain").json()

    assert body["available"] is available()
    if body["available"]:
        assert body["toolchain"]


def test_renaming_a_plan_does_not_cost_it_the_verdict(client, store) -> None:
    """A rename goes through PATCH, not revise, precisely so the verdict survives: the title
    is not part of the text that was checked, so what was proven is exactly as proven."""
    plan = store_verified_plan(store, toolchain="leanprover/lean4:v4.33.1")

    response = client.patch(f"/v1/proof-graphs/{plan.graph_id}", json={"title": "Better name"})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["title"] == "Better name"
    assert body["status"] == "verified"
    assert body["verification"] is not None
    assert body["lean_source"] == MINIMAL_SOURCE


def test_a_plan_title_cannot_be_blanked(client) -> None:
    graph_id = create(client).json()["graph_id"]

    assert client.patch(f"/v1/proof-graphs/{graph_id}", json={"title": "   "}).status_code == 422
    assert client.get(f"/v1/proof-graphs/{graph_id}").json()["title"] == "A plan"


def test_a_plan_can_be_refiled_like_a_workflow(client) -> None:
    """The other labels ride the same PATCH: project and origin_dir are clearable, and a
    request that sends nothing at all is a mistake to say so about, not a silent no-op."""
    graph_id = create(client, project="one").json()["graph_id"]

    assert client.patch(
        f"/v1/proof-graphs/{graph_id}", json={"project": None}
    ).json()["project"] is None
    assert client.patch(f"/v1/proof-graphs/{graph_id}", json={}).status_code == 422


# --------------------------------------------------------------------------- the verdict


def test_revising_a_plan_drops_the_verdict(client, store) -> None:
    """A verdict belongs to the text that earned it."""
    plan = store_verified_plan(store, toolchain="leanprover/lean4:v4.33.1")

    response = client.put(
        f"/v1/proof-graphs/{plan.graph_id}",
        json={"lean_source": MINIMAL_SOURCE + "-- one more line\n", "reason": "tightened it"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "draft"
    assert body["verification"] is None
    assert body["verified_at"] is None


def test_a_verdict_from_another_toolchain_is_stale(client, store) -> None:
    """"Verified" names the thing that did the verifying."""
    plan = store_verified_plan(store, toolchain="leanprover/lean4:v0.0.1-not-installed")

    body = client.get(f"/v1/proof-graphs/{plan.graph_id}").json()

    assert body["status"] == "verified"
    assert body["stale"] is True


def test_a_stale_plan_cannot_be_compiled(client, store) -> None:
    plan = store_verified_plan(store, toolchain="leanprover/lean4:v0.0.1-not-installed")

    response = client.post(f"/v1/proof-graphs/{plan.graph_id}/workflows", json={})

    assert response.status_code == 409, response.text
    assert "verify it again" in response.json()["error"]["message"]


def test_a_draft_cannot_be_compiled(client) -> None:
    graph_id = create(client).json()["graph_id"]

    response = client.post(f"/v1/proof-graphs/{graph_id}/workflows", json={})

    assert response.status_code == 409, response.text
    assert "only a verified graph" in response.json()["error"]["message"]


# --------------------------------------------------------------------------- compiling


def test_compiling_produces_an_ordinary_draft_workflow(client, store) -> None:
    plan = store_verified_plan(store, toolchain="leanprover/lean4:v4.33.1")

    response = client.post(
        f"/v1/proof-graphs/{plan.graph_id}/workflows",
        json={"project": "chief", "title": "Compiled"},
    )

    assert response.status_code == 201, response.text
    workflow = response.json()
    assert workflow["status"] == "draft"
    assert workflow["title"] == "Compiled"
    assert workflow["project"] == "chief"
    assert [step["id"] for step in workflow["steps"]] == ["fit"]
    # It approves and runs like anything else.
    assert client.post(f"/v1/workflows/{workflow['workflow_id']}/approve").status_code == 200


def test_a_compiled_workflow_records_what_it_was_made_from(client, store) -> None:
    """Lineage, and enough of the verdict to be re-examined without the plan."""
    plan = store_verified_plan(store, toolchain="leanprover/lean4:v4.33.1")

    workflow = client.post(f"/v1/proof-graphs/{plan.graph_id}/workflows", json={}).json()

    origin = workflow["from_graph"]
    assert origin["graph_id"] == plan.graph_id
    assert origin["toolchain"] == "leanprover/lean4:v4.33.1"
    assert origin["contracts_refined"] == 1
    assert origin["contracts_any"] == 0


def test_the_plan_records_what_was_compiled_from_it(client, store) -> None:
    plan = store_verified_plan(store, toolchain="leanprover/lean4:v4.33.1")

    compile_url = f"/v1/proof-graphs/{plan.graph_id}/workflows"
    first = client.post(compile_url, json={}).json()["workflow_id"]
    second = client.post(compile_url, json={}).json()["workflow_id"]

    assert client.get(f"/v1/proof-graphs/{plan.graph_id}").json()["compiled_to"] == [first, second]


def test_deleting_a_plan_leaves_the_workflows_made_from_it(client, store) -> None:
    plan = store_verified_plan(store, toolchain="leanprover/lean4:v4.33.1")
    made = client.post(f"/v1/proof-graphs/{plan.graph_id}/workflows", json={}).json()
    workflow_id = made["workflow_id"]

    response = client.delete(f"/v1/proof-graphs/{plan.graph_id}")

    assert response.status_code == 200, response.text
    assert response.json()["compiled_to"] == [workflow_id]
    assert client.get(f"/v1/proof-graphs/{plan.graph_id}").status_code == 404
    assert client.get(f"/v1/workflows/{workflow_id}").status_code == 200


# --------------------------------------------------------------------------- with a toolchain


@needs_lean
def test_checking_a_plan_that_holds_up(client) -> None:
    graph_id = create(client, lean_source=example_source()).json()["graph_id"]

    response = client.post(f"/v1/proof-graphs/{graph_id}/verification")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "verified"
    assert body["stale"] is False
    assert body["verification"]["graph"]["stats"]["contracts_any"] == 0
    assert body["verification"]["graph"]["nodes"][0]["id"] == "harvest"


@needs_lean
def test_a_plan_that_does_not_hold_up_is_a_verdict_not_an_error(client) -> None:
    """The check ran and reached a conclusion — that is the request succeeding."""
    broken = example_source().replace(
        '(fun m => m.auc ≥ 80) "auc ≥ 80"', '(fun m => m.auc ≥ 70) "auc ≥ 70"'
    )
    graph_id = create(client, lean_source=broken).json()["graph_id"]

    response = client.post(f"/v1/proof-graphs/{graph_id}/verification")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "failed"
    errors = [d for d in body["verification"]["diagnostics"] if d["severity"] == "error"]
    assert errors
    # Drawn on the step whose demand went unmet, so a reader is pointed at the right node.
    assert "review" in {d["step_id"] for d in errors}


@needs_lean
def test_the_whole_journey_from_source_to_an_approved_workflow(client) -> None:
    graph_id = create(client, lean_source=example_source(), project="chief").json()["graph_id"]
    assert client.post(f"/v1/proof-graphs/{graph_id}/verification").json()["status"] == "verified"

    workflow = client.post(f"/v1/proof-graphs/{graph_id}/workflows", json={}).json()
    approved = client.post(f"/v1/workflows/{workflow['workflow_id']}/approve")

    assert approved.status_code == 200, approved.text
    run = client.post(f"/v1/workflows/{workflow['workflow_id']}/runs", json={})
    assert run.status_code == 201, run.text
    # The conditions the plan proved travel with it, as the inputs a harness reads.
    steps = {step["id"]: step for step in workflow["steps"]}
    assert steps["fit_model"]["inputs"]["dataset"]["contract"] == "rows ≥ 500, labelled"
    assert steps["fit_model"]["inputs"]["dataset"]["from_step"] == "build_dataset"


def test_review_notes_ride_on_a_proof_graph(client, store) -> None:
    """The same conversation as notes on a workflow draft, on the graph's own routes: a
    note lands on a step or on the graph as a whole, rides out on the single read, and is
    closed by a person — there is no MCP tool for any of it."""
    graph = store_verified_plan(store, toolchain="leanprover/lean4:v4.33.1")

    on_step = client.post(
        f"/v1/proof-graphs/{graph.graph_id}/notes",
        json={"body": "hold out a validation year", "step_id": "fit", "author": "roy"},
    )
    assert on_step.status_code == 201, on_step.text
    note = on_step.json()
    assert note["step_id"] == "fit"
    # The goal is copied on at write time, so an orphaned note can still say what it was about.
    assert note["step_goal"]

    whole = client.post(
        f"/v1/proof-graphs/{graph.graph_id}/notes",
        json={"body": "this assumes one events language", "author": "roy"},
    )
    assert whole.status_code == 201 and whole.json()["step_id"] is None

    # A step the extraction does not have is refused, not filed.
    missing = client.post(
        f"/v1/proof-graphs/{graph.graph_id}/notes",
        json={"body": "?", "step_id": "nothere", "author": "roy"},
    )
    assert missing.status_code == 422

    # The single read carries them, like get_workflow.
    doc = client.get(f"/v1/proof-graphs/{graph.graph_id}").json()
    assert [n["body"] for n in doc["review_notes"]] == [
        "hold out a validation year",
        "this assumes one events language",
    ]

    # Closing is a decision, and deciding the same way twice is refused.
    closed = client.patch(
        f"/v1/proof-graphs/{graph.graph_id}/notes/{note['note_id']}",
        json={"resolved": True, "resolved_by": "roy"},
    )
    assert closed.status_code == 200 and closed.json()["resolved"] is True
    again = client.patch(
        f"/v1/proof-graphs/{graph.graph_id}/notes/{note['note_id']}",
        json={"resolved": True, "resolved_by": "roy"},
    )
    assert again.status_code == 409

    listed = client.get(f"/v1/proof-graphs/{graph.graph_id}/notes?resolved=false").json()
    assert [n["body"] for n in listed] == ["this assumes one events language"]
