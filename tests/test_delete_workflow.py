"""Deleting a workflow: what goes, and the two things that deliberately do not.

Archiving and deleting are different acts on purpose (see ``Service.delete_workflow``), so
what is asserted here is mostly the boundary of the cascade rather than the cascade itself.
"""

from __future__ import annotations

from .conftest import Api, task


def _ran_workflow(api: Api) -> tuple[str, str]:
    workflow_id, run_id = api.run([task("step_01"), task("step_02", depends_on=["step_01"])])
    assert api.update_step(run_id, "step_01", status="completed").status_code == 200
    return workflow_id, run_id


def test_deleting_removes_the_workflow_and_its_run(api: Api) -> None:
    workflow_id, run_id = _ran_workflow(api)

    response = api.client.delete(f"/v1/workflows/{workflow_id}")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["workflow_id"] == workflow_id
    # The counts are the only remaining description of what was there.
    assert body["removed"]["workflows"] == 1
    assert body["removed"]["runs"] == 1

    assert api.client.get(f"/v1/workflows/{workflow_id}").status_code == 404
    assert api.client.get(f"/v1/runs/{run_id}").status_code == 404
    assert workflow_id not in [w["workflow_id"] for w in api.client.get("/v1/workflows").json()]
    assert run_id not in [r["run_id"] for r in api.client.get("/v1/runs").json()]


def test_deleting_a_workflow_that_is_not_there_is_a_404(api: Api) -> None:
    assert api.client.delete("/v1/workflows/wf_nope").status_code == 404


def test_the_audit_trail_survives_and_records_the_deletion(api: Api) -> None:
    """A delete that erases the record of itself is not auditable. REQ-20 makes the log
    append-only; this checks the cascade actually respects that rather than matching on
    ``workflow_id`` across every table that has one."""
    workflow_id, run_id = _ran_workflow(api)
    api.client.delete(f"/v1/workflows/{workflow_id}")

    entries = api.client.get(f"/v1/audit?workflow_id={workflow_id}").json()
    events = [e["event"] for e in entries]
    assert "workflow.created" in events and "workflow.approved" in events
    assert events[-1] == "workflow.deleted"

    detail = entries[-1]["detail"]
    assert detail["runs"] == [run_id]
    assert detail["status"] == "approved"
    assert detail["removed"]["runs"] == 1


def test_a_template_saved_from_the_workflow_survives(api: Api) -> None:
    """It stopped being part of this workflow the moment it was saved, and keeping the plan
    for next time is the entire point of saving one."""
    workflow_id, _ = _ran_workflow(api)
    response = api.client.post(
        f"/v1/workflows/{workflow_id}/template", json={"title": "kept", "parameters": []}
    )
    assert response.status_code == 201, response.text
    template_id = response.json()["template_id"]

    api.client.delete(f"/v1/workflows/{workflow_id}")
    assert api.client.get(f"/v1/templates/{template_id}").status_code == 200


def test_review_notes_and_amendments_go_with_it(api: Api) -> None:
    workflow_id, run_id = _ran_workflow(api)
    assert api.client.post(
        f"/v1/workflows/{workflow_id}/notes", json={"body": "reconsider step two"}
    ).status_code == 201
    assert api.propose(
        run_id,
        [{"op": "insert_after", "target_step_id": "step_02",
          "step": task("step_03", depends_on=["step_02"])}],
    ).status_code == 201

    removed = api.client.delete(f"/v1/workflows/{workflow_id}").json()["removed"]
    assert removed["review_notes"] == 1
    assert removed["amendments"] == 1
    assert api.client.get(f"/v1/workflows/{workflow_id}/notes").status_code == 404
    assert api.client.get("/v1/amendments").json() == []


def test_deleting_leaves_a_neighbouring_workflow_untouched(api: Api) -> None:
    """The cascade is keyed on the id, and a `DELETE FROM … WHERE workflow_id = ?` with the
    parameter forgotten takes the whole table with it."""
    doomed, _ = _ran_workflow(api)
    keeper, keeper_run = _ran_workflow(api)

    api.client.delete(f"/v1/workflows/{doomed}")

    assert api.client.get(f"/v1/workflows/{keeper}").status_code == 200
    assert api.client.get(f"/v1/runs/{keeper_run}").status_code == 200


def test_a_running_execution_does_not_block_the_delete(api: Api) -> None:
    """Deliberate: the run is not a lock on the record, and someone deleting a workflow
    mid-run has usually just decided the run is the thing they want gone."""
    workflow_id, run_id = api.run([task("step_01")])
    assert api.update_step(run_id, "step_01", status="running").status_code == 200
    assert api.client.get(f"/v1/runs/{run_id}").json()["status"] == "running"

    assert api.client.delete(f"/v1/workflows/{workflow_id}").status_code == 200
    assert api.client.get(f"/v1/runs/{run_id}").status_code == 404


def test_delete_is_not_on_the_mcp_surface() -> None:
    """Approving is already a decision the harness may not make on its own initiative;
    erasing a plan and the record of what it did is further down that road."""
    from chief.mcp_server import HARNESS_OPERATIONS

    assert "delete_workflow" not in HARNESS_OPERATIONS
