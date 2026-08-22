"""draft -> approved -> run, and the transitions that must be refused (REQ-32, contract 2.1)."""

from __future__ import annotations

import time

from fastapi.testclient import TestClient

from .conftest import Api, construct, task


def test_workflow_is_created_as_draft(api: Api) -> None:
    response = api.create_workflow([task("step_01")])
    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "draft"
    assert body["version"] == 1


def test_run_cannot_be_registered_against_a_draft(api: Api) -> None:
    workflow_id = api.create_workflow([task("step_01")]).json()["workflow_id"]
    response = api.client.post(f"/v1/workflows/{workflow_id}/runs", json={})
    assert response.status_code == 409
    assert "draft" in response.json()["error"]["message"]


def test_approve_does_not_create_a_run(api: Api) -> None:
    workflow_id = api.approved_workflow([task("step_01")])
    assert api.client.get(f"/v1/workflows/{workflow_id}").json()["status"] == "approved"
    assert api.client.get("/v1/runs").json() == []


def test_approved_is_terminal_except_for_archive(api: Api) -> None:
    workflow_id = api.approved_workflow([task("step_01")])
    assert api.client.post(f"/v1/workflows/{workflow_id}/approve").status_code == 409
    assert api.client.post(f"/v1/workflows/{workflow_id}/archive").status_code == 200
    assert api.client.get(f"/v1/workflows/{workflow_id}").json()["status"] == "archived"
    assert api.client.post(f"/v1/workflows/{workflow_id}/archive").status_code == 409


def test_a_draft_can_be_archived(api: Api) -> None:
    """A superseded draft needs a way out, or it asks to be approved forever."""
    response = api.create_workflow([task("step_01")])
    workflow_id = response.json()["workflow_id"]
    assert api.client.post(f"/v1/workflows/{workflow_id}/archive").status_code == 200
    assert api.client.get(f"/v1/workflows/{workflow_id}").json()["status"] == "archived"


def test_an_archived_draft_cannot_be_approved_back_to_life(api: Api) -> None:
    response = api.create_workflow([task("step_01")])
    workflow_id = response.json()["workflow_id"]
    api.client.post(f"/v1/workflows/{workflow_id}/archive")
    assert api.client.post(f"/v1/workflows/{workflow_id}/approve").status_code == 409


def test_archiving_records_the_state_it_came_from(api: Api) -> None:
    """Retiring an unused draft and retiring a workflow that ran are different acts."""
    draft = api.create_workflow([task("step_01")]).json()["workflow_id"]
    api.client.post(f"/v1/workflows/{draft}/archive")
    approved = api.approved_workflow([task("step_01")])
    api.client.post(f"/v1/workflows/{approved}/archive")

    def archived_from(workflow_id: str) -> str:
        entries = api.client.get("/v1/audit", params={"workflow_id": workflow_id}).json()
        return next(e for e in entries if e["event"] == "workflow.archived")["detail"]["from"]

    assert archived_from(draft) == "draft"
    assert archived_from(approved) == "approved"


def test_a_decision_can_carry_a_comment(api: Api) -> None:
    """Why a plan was approved is worth more than the fact that it was."""
    workflow_id = api.create_workflow([task("step_01")]).json()["workflow_id"]
    response = api.client.post(
        f"/v1/workflows/{workflow_id}/approve",
        json={"decided_by": "roy", "reason": "scope is right, the risk is all in step_01"},
    )
    assert response.status_code == 200

    entry = next(
        e
        for e in api.client.get("/v1/audit", params={"workflow_id": workflow_id}).json()
        if e["event"] == "workflow.approved"
    )
    assert entry["detail"]["decided_by"] == "roy"
    assert entry["detail"]["reason"] == "scope is right, the risk is all in step_01"


def test_a_decision_without_a_comment_still_works(api: Api) -> None:
    """The contract specifies a bodyless POST, and most approvals need no explanation."""
    workflow_id = api.create_workflow([task("step_01")]).json()["workflow_id"]
    assert api.client.post(f"/v1/workflows/{workflow_id}/approve").status_code == 200

    entry = next(
        e
        for e in api.client.get("/v1/audit", params={"workflow_id": workflow_id}).json()
        if e["event"] == "workflow.approved"
    )
    assert entry["detail"]["decided_by"] == "human"
    assert "reason" not in entry["detail"]


def test_discarding_a_draft_records_why(api: Api) -> None:
    workflow_id = api.create_workflow([task("step_01")]).json()["workflow_id"]
    api.client.post(
        f"/v1/workflows/{workflow_id}/archive",
        json={"decided_by": "roy", "reason": "superseded by the merged plan"},
    )
    entry = next(
        e
        for e in api.client.get("/v1/audit", params={"workflow_id": workflow_id}).json()
        if e["event"] == "workflow.archived"
    )
    assert entry["detail"]["reason"] == "superseded by the merged plan"
    assert entry["detail"]["from"] == "draft"


def test_archive_blocks_new_runs_but_not_running_ones(api: Api) -> None:
    workflow_id, run_id = api.run([task("step_01")])
    assert api.client.post(f"/v1/workflows/{workflow_id}/archive").status_code == 200
    assert api.client.post(f"/v1/workflows/{workflow_id}/runs", json={}).status_code == 409
    assert api.update_step(run_id, "step_01", status="completed").status_code == 200
    assert api.run_status(run_id) == "completed"


def test_run_pins_base_version_and_materialises_top_level_steps(api: Api) -> None:
    _, run_id = api.run([task("step_01"), task("step_02", depends_on=["step_01"])])
    run = api.get_run(run_id)
    assert run["base_version"] == 1
    assert run["applied_amendment_ids"] == []
    assert set(run["step_states"]) == {"step_01", "step_02"}
    assert run["status"] == "running"


def test_body_steps_are_not_top_level(api: Api) -> None:
    _, run_id = api.run([construct("step_01", "loop", ["step_02"]), task("step_02")])
    assert set(api.get_run(run_id)["step_states"]) == {"step_01"}


def test_full_happy_path_completes_the_run(api: Api) -> None:
    _, run_id = api.run([task("step_01"), task("step_02", depends_on=["step_01"])])
    api.update_step(run_id, "step_01", status="running")
    assert api.run_status(run_id) == "running"
    api.update_step(
        run_id,
        "step_01",
        status="completed",
        artifacts=[{"type": "file_ref", "ref": "/tmp/out.json"}],
    )
    assert api.step_status(run_id, "step_01") == "completed"
    assert api.run_status(run_id) == "running"
    api.update_step(run_id, "step_02", status="completed")
    assert api.run_status(run_id) == "completed"


def test_versions_endpoint_returns_the_original_plan(api: Api) -> None:
    workflow_id = api.approved_workflow([task("step_01")])
    response = api.client.get(f"/v1/workflows/{workflow_id}/versions/1")
    assert response.status_code == 200
    assert response.json()["version"] == 1
    assert api.client.get(f"/v1/workflows/{workflow_id}/versions/7").status_code == 404


def test_listing_filters(api: Api) -> None:
    api.create_workflow([task("a")], title="draft one")
    api.approved_workflow([task("b")], title="approved one")
    assert len(api.client.get("/v1/workflows").json()) == 2
    assert len(api.client.get("/v1/workflows?status=draft").json()) == 1
    assert len(api.client.get("/v1/workflows?status=approved").json()) == 1


def test_unversioned_paths_are_also_served(api: Api) -> None:
    assert api.client.get("/workflows").status_code == 200


# --- when it was added -------------------------------------------------------------------
#
# The store has always stamped a workflow row; the API never said so, which left the list
# with nothing to sort a never-run workflow by. The stamps are the record's, not the plan's:
# they are filled in on the way out and refused on the way in.


def test_a_workflow_reports_when_it_was_added(api: Api) -> None:
    created = api.create_workflow([task("step_01")]).json()
    assert created["created_at"], "a workflow says when it was added"
    assert created["updated_at"] == created["created_at"]

    listed = next(
        w
        for w in api.client.get("/v1/workflows").json()
        if w["workflow_id"] == created["workflow_id"]
    )
    assert listed["created_at"] == created["created_at"]


def test_approving_moves_updated_at_but_not_created_at(api: Api) -> None:
    created = api.create_workflow([task("step_01")]).json()
    workflow_id = created["workflow_id"]
    api.client.post(f"/v1/workflows/{workflow_id}/approve")

    after = api.client.get(f"/v1/workflows/{workflow_id}").json()
    assert after["created_at"] == created["created_at"], "added once, and that does not move"
    assert after["updated_at"] >= created["updated_at"]


def test_a_harness_cannot_choose_when_its_workflow_was_added(api: Api) -> None:
    """Otherwise the list's oldest workflow is whoever claimed the earliest date."""
    response = api.client.post(
        "/v1/workflows",
        json={
            "title": "t", "source": "generated", "steps": [task("step_01")],
            "created_at": "1999-01-01T00:00:00Z",
        },
    )
    assert response.status_code == 422


# --- revising a draft --------------------------------------------------------------------
#
# A draft is the one plan nobody has agreed to and nothing has executed from, so correcting
# it needs neither an approval nor a run to pause. Before this existed the only way to fix a
# draft was to write a second workflow, which left a reviewer holding two drafts and no
# statement of which one counted.


def test_a_draft_can_be_revised_in_place(client: TestClient) -> None:
    created = client.post(
        "/v1/workflows",
        json={"title": "first pass", "source": "generated", "steps": [task("step_01")]},
    ).json()
    workflow_id = created["workflow_id"]

    response = client.put(
        f"/v1/workflows/{workflow_id}",
        json={
            "title": "second pass",
            "steps": [task("step_01"), task("step_02", depends_on=["step_01"])],
            "reason": "step_01 needed a follow-up",
        },
    )
    assert response.status_code == 200, response.text
    revised = response.json()

    assert revised["workflow_id"] == workflow_id, "the same draft, not a new one"
    assert revised["title"] == "second pass"
    assert [s["id"] for s in revised["steps"]] == ["step_01", "step_02"]
    assert revised["status"] == "draft", "revising is not approving"
    # version counts approved amendments; pre-approval edits are not that.
    assert revised["version"] == 1


def test_a_revision_is_validated_as_a_whole_plan(client: TestClient) -> None:
    """It replaces rather than patches, so a broken graph cannot slip in on the old one."""
    workflow_id = client.post(
        "/v1/workflows",
        json={"title": "t", "source": "generated", "steps": [task("step_01")]},
    ).json()["workflow_id"]

    response = client.put(
        f"/v1/workflows/{workflow_id}",
        json={"title": "t", "steps": [task("step_01", depends_on=["nope"])]},
    )
    assert response.status_code == 422


def test_an_approved_workflow_cannot_be_revised(client: TestClient) -> None:
    """Once approved it may have a run against it, and rewriting the plan underneath a
    running harness is exactly what amendments exist to make deliberate."""
    workflow_id = client.post(
        "/v1/workflows",
        json={"title": "t", "source": "generated", "steps": [task("step_01")]},
    ).json()["workflow_id"]
    assert client.post(f"/v1/workflows/{workflow_id}/approve").status_code == 200

    response = client.put(
        f"/v1/workflows/{workflow_id}", json={"title": "t", "steps": [task("step_01")]}
    )
    assert response.status_code == 409
    assert "amendment" in response.text


def test_a_revision_is_audited(client: TestClient) -> None:
    workflow_id = client.post(
        "/v1/workflows",
        json={"title": "t", "source": "generated", "steps": [task("step_01")]},
    ).json()["workflow_id"]
    client.put(
        f"/v1/workflows/{workflow_id}",
        json={"title": "t", "steps": [task("step_01"), task("step_02")], "reason": "missed one"},
    )

    entry = next(
        e
        for e in client.get("/v1/audit", params={"workflow_id": workflow_id}).json()
        if e["event"] == "workflow.revised"
    )
    assert entry["detail"]["reason"] == "missed one"
    assert entry["detail"]["steps_before"] == 1
    assert entry["detail"]["steps_after"] == 2


# --- a loop's exit condition -------------------------------------------------------------
#
# The branch out of a loop is a decision, and it belongs on the graph, not buried in a body
# step's goal ("if the audit was clean, close the loop..."). `exit_when` names what decides
# it, so the UI can label the gate's two arrows. Chief records it; the harness judges it.


def test_a_loop_may_declare_its_exit_condition(api: Api) -> None:
    steps = [
        task("step_01"),
        {
            "id": "step_02", "type": "loop", "goal": "iterate", "harness": "claude-code",
            "depends_on": ["step_01"], "body": ["step_03"],
            "exit_when": "the audit is clean",
        },
        task("step_03"),
    ]
    response = api.create_workflow(steps)
    assert response.status_code == 201, response.text
    stored = next(s for s in response.json()["steps"] if s["id"] == "step_02")
    assert stored["exit_when"] == "the audit is clean"


def test_exit_when_is_refused_on_a_task(api: Api) -> None:
    steps = [{**task("step_01"), "exit_when": "done"}]
    response = api.create_workflow(steps)
    assert response.status_code == 422
    assert "exit_when" in response.text


def test_exit_when_is_refused_on_a_parallel(api: Api) -> None:
    """A parallel's branches all run — there is no exit decision to name."""
    steps = [
        {
            "id": "step_01", "type": "parallel", "goal": "fan out", "harness": "claude-code",
            "body": ["step_02"], "exit_when": "all done",
        },
        task("step_02"),
    ]
    response = api.create_workflow(steps)
    assert response.status_code == 422
    assert "exit_when" in response.text


def test_workflows_are_listed_most_recently_touched_first(api: Api) -> None:
    """The list is something you come back to, so what moved last comes first.

    Ordering by creation instead would put a workflow you just approved wherever it happened
    to be written, which is the one place you would not look for it.
    """
    ids = []
    for title in ("first", "second", "third"):
        response = api.create_workflow([task("a")], title=title)
        assert response.status_code == 201, response.text
        ids.append(response.json()["workflow_id"])
        time.sleep(0.002)  # `now()` is millisecond-resolution; a tie would sort by id instead

    listed = [w["workflow_id"] for w in api.client.get("/v1/workflows").json()]
    assert listed == list(reversed(ids))

    # Touching the oldest moves it to the head — the point of the ordering.
    assert api.client.post(f"/v1/workflows/{ids[0]}/approve").status_code == 200
    listed = [w["workflow_id"] for w in api.client.get("/v1/workflows").json()]
    assert listed[0] == ids[0]

    # And the filtered query orders the same way, rather than falling back to creation.
    listed = [
        w["workflow_id"]
        for w in api.client.get("/v1/workflows", params={"status": "draft"}).json()
    ]
    assert listed == [ids[2], ids[1]]
