"""Step reporting rules and failure propagation (REQ-8, REQ-48, contract 1.4, 2.2, 4)."""

from __future__ import annotations

from .conftest import Api, construct, task


def test_summary_is_required_on_every_update(api: Api) -> None:
    _, run_id = api.run([task("step_01")])
    response = api.client.post(
        f"/v1/runs/{run_id}/steps/step_01/updates", json={"status": "completed"}
    )
    assert response.status_code == 422


def test_blank_summary_is_rejected(api: Api) -> None:
    _, run_id = api.run([task("step_01")])
    response = api.update_step(run_id, "step_01", status="completed", summary="   ")
    assert response.status_code == 422


def test_harness_cannot_report_skipped(api: Api) -> None:
    _, run_id = api.run([task("step_01")])
    response = api.update_step(run_id, "step_01", status="skipped")
    assert response.status_code == 422  # not in the reportable enum at all


def test_task_step_cannot_close_instances(api: Api) -> None:
    _, run_id = api.run([task("step_01")])
    response = api.update_step(run_id, "step_01", instances_closed=True)
    assert response.status_code == 422
    assert "no instances" in response.json()["error"]["message"]


def test_construct_status_cannot_be_reported(api: Api) -> None:
    _, run_id = api.run([construct("step_01", "loop", ["step_02"]), task("step_02")])
    response = api.update_step(run_id, "step_01", status="completed")
    assert response.status_code == 409
    assert "derived" in response.json()["error"]["message"]


def test_metadata_and_artifacts_accumulate(api: Api) -> None:
    _, run_id = api.run([task("step_01")])
    api.update_step(
        run_id,
        "step_01",
        status="running",
        metadata={"attempt": 1},
        artifacts=[{"type": "text", "data": "first"}],
    )
    api.update_step(
        run_id,
        "step_01",
        status="completed",
        metadata={"exit_code": 0},
        artifacts=[{"type": "text", "data": "second"}],
    )
    state = api.get_run(run_id)["step_states"]["step_01"]
    assert state["metadata"] == {"attempt": 1, "exit_code": 0}
    assert len(state["artifacts"]) == 2
    assert state["started_at"] and state["completed_at"]


def test_failure_propagates_down_the_dependency_chain(api: Api) -> None:
    _, run_id = api.run(
        [
            task("step_01"),
            task("step_02", depends_on=["step_01"]),
            task("step_03", depends_on=["step_02"]),
            task("step_04"),
        ]
    )
    api.update_step(run_id, "step_01", status="failed", summary="compiler blew up")
    run = api.get_run(run_id)
    assert run["step_states"]["step_02"]["status"] == "skipped"
    assert run["step_states"]["step_03"]["status"] == "skipped"
    assert run["step_states"]["step_04"]["status"] == "pending"
    assert run["status"] == "failed"
    assert "step_01" in run["step_states"]["step_02"]["summary"]


def test_run_completes_when_every_top_level_step_is_completed_or_skipped(api: Api) -> None:
    _, run_id = api.run([task("step_01"), task("step_02", depends_on=["step_01"])])
    api.update_step(run_id, "step_01", status="completed")
    api.update_step(run_id, "step_02", status="completed")
    assert api.run_status(run_id) == "completed"


def test_a_skipped_step_cannot_be_updated(api: Api) -> None:
    _, run_id = api.run([task("step_01"), task("step_02", depends_on=["step_01"])])
    api.update_step(run_id, "step_01", status="failed")
    response = api.update_step(run_id, "step_02", status="completed")
    assert response.status_code == 409
    assert "replay_step" in response.json()["error"]["message"]


def test_body_step_cannot_be_addressed_at_the_top_level(api: Api) -> None:
    _, run_id = api.run([construct("step_01", "loop", ["step_02"]), task("step_02")])
    response = api.update_step(run_id, "step_02", status="completed")
    assert response.status_code == 422
    assert "instance" in response.json()["error"]["message"]


def test_unknown_run_and_step(api: Api) -> None:
    assert api.client.get("/v1/runs/nope").status_code == 404
    _, run_id = api.run([task("step_01")])
    assert api.update_step(run_id, "ghost", status="completed").status_code == 404
