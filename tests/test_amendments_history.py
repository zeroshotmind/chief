"""History edits and the immutability rules around them.

REQ-14 (completed work is immutable by default), REQ-41 (edit/replay as an explicit
exception), REQ-42 (the original result is retained, never overwritten).
"""

from __future__ import annotations

from .conftest import Api, construct, task


def two_steps() -> list[dict]:
    return [task("step_01"), task("step_02", depends_on=["step_01"])]


def update_op(step_id: str, **kw) -> dict:
    return {"op": "update_step", "target_step_id": step_id, "step": task(step_id, **kw)}


def replay_op(step_id: str, **kw) -> dict:
    return {"op": "replay_step", "target_step_id": step_id, **kw}


def test_forward_amendment_cannot_touch_a_completed_step(api: Api) -> None:
    _, run_id = api.run(two_steps())
    api.update_step(run_id, "step_01", status="completed")
    response = api.propose(run_id, [update_op("step_01", harness="local:qwen")])
    assert response.status_code == 409
    assert response.json()["error"]["details"]["required_kind"] == "history_edit"
    # Rejected before it ever reaches a human.
    assert api.client.get(f"/v1/runs/{run_id}/amendments").json() == []
    assert api.run_status(run_id) == "running"


def test_forward_amendment_cannot_touch_a_failed_step(api: Api) -> None:
    """Replaying a failed step is the motivating case for REQ-41, so failed counts too."""
    _, run_id = api.run(two_steps())
    api.update_step(run_id, "step_01", status="failed")
    assert api.propose(run_id, [update_op("step_01")]).status_code == 409
    assert api.propose(run_id, [update_op("step_01")], kind="history_edit").status_code == 201


def test_inserting_after_a_completed_step_stays_forward(api: Api) -> None:
    """Inserting a neighbour neither alters nor re-executes the target (REQ-14)."""
    _, run_id = api.run(two_steps())
    api.update_step(run_id, "step_01", status="completed")
    response = api.propose(
        run_id,
        [
            {
                "op": "insert_after",
                "target_step_id": "step_01",
                "step": task("step_03", depends_on=["step_01"]),
            }
        ],
    )
    assert response.status_code == 201
    assert response.json()["kind"] == "forward"
    api.approve(response.json()["amendment_id"])
    run = api.get_run(run_id)
    assert run["step_states"]["step_01"]["status"] == "completed"
    assert run["step_states"]["step_01"]["history"] == []
    assert run["step_states"]["step_03"]["status"] == "pending"


def test_replay_is_always_a_history_edit(api: Api) -> None:
    _, run_id = api.run(two_steps())
    api.update_step(run_id, "step_01", status="completed")
    assert api.propose(run_id, [replay_op("step_01")]).status_code == 409


def test_replay_preserves_the_original_result_and_resets_the_step(api: Api) -> None:
    _, run_id = api.run(two_steps())
    api.update_step(
        run_id,
        "step_01",
        status="completed",
        summary="built with the wrong flags",
        artifacts=[{"type": "file_ref", "ref": "/tmp/first"}],
    )
    api.update_step(run_id, "step_02", status="completed")
    assert api.run_status(run_id) == "completed"

    amendment_id = api.propose(
        run_id, [replay_op("step_01")], kind="history_edit", reason="wrong compiler flags"
    ).json()["amendment_id"]
    approved = api.approve(amendment_id).json()
    assert approved["status"] == "approved"

    state = api.get_run(run_id)["step_states"]["step_01"]
    assert state["status"] == "pending"
    assert state["summary"] is None
    assert state["artifacts"] == []
    # REQ-42: the original is retained, not overwritten.
    assert len(state["history"]) == 1
    assert state["history"][0]["status"] == "completed"
    assert state["history"][0]["artifacts"][0]["ref"] == "/tmp/first"
    assert state["history"][0]["superseded_at"]
    assert api.run_status(run_id) == "running"

    api.update_step(run_id, "step_01", status="completed", summary="rebuilt")
    assert api.run_status(run_id) == "completed"
    assert len(api.get_run(run_id)["step_states"]["step_01"]["history"]) == 1


def test_update_step_on_a_completed_target_snapshots_but_does_not_rerun(api: Api) -> None:
    _, run_id = api.run(two_steps())
    api.update_step(run_id, "step_01", status="completed", summary="original")
    amendment_id = api.propose(
        run_id, [update_op("step_01", harness="local:gemma")], kind="history_edit"
    ).json()["amendment_id"]
    api.approve(amendment_id)

    state = api.get_run(run_id)["step_states"]["step_01"]
    assert state["status"] == "completed"
    assert len(state["history"]) == 1
    definition = api.client.get(f"/v1/runs/{run_id}/definition").json()
    assert definition["steps"][0]["harness"] == "local:gemma"


def test_replaying_one_failed_iteration_not_the_whole_loop(api: Api) -> None:
    _, run_id = api.run(
        [
            construct("step_01", "loop", ["step_02", "step_03"]),
            task("step_02"),
            task("step_03", depends_on=["step_02"]),
        ]
    )
    api.add_instance(run_id, "step_01")
    api.add_instance(run_id, "step_01")
    api.update_body_step(run_id, "step_01", "inst_00", "step_02", status="completed")
    api.update_body_step(run_id, "step_01", "inst_00", "step_03", status="completed")
    api.update_body_step(
        run_id, "step_01", "inst_01", "step_02", status="failed", summary="flaky network"
    )
    assert api.instance(run_id, "step_01", "inst_01")["status"] == "failed"
    assert api.step_status(run_id, "step_01") == "failed"

    # The loop step itself is 'failed' while inst_00 is completed: the check is per target.
    forward = api.propose(run_id, [replay_op("step_01", instance_id="inst_01")])
    assert forward.status_code == 409

    amendment_id = api.propose(
        run_id,
        [replay_op("step_01", instance_id="inst_01")],
        kind="history_edit",
        reason="retry the flaky iteration",
    ).json()["amendment_id"]
    api.approve(amendment_id)

    untouched = api.instance(run_id, "step_01", "inst_00")
    replayed = api.instance(run_id, "step_01", "inst_01")
    assert untouched["status"] == "completed"
    assert untouched["history"] == []
    assert replayed["status"] == "pending"
    assert len(replayed["history"]) == 1
    assert replayed["history"][0]["status"] == "failed"
    assert replayed["step_states"]["step_02"]["status"] == "pending"
    assert replayed["step_states"]["step_03"]["status"] == "pending"
    assert api.step_status(run_id, "step_01") == "running"
    assert api.run_status(run_id) == "running"

    api.update_body_step(run_id, "step_01", "inst_01", "step_02", status="completed")
    api.update_body_step(run_id, "step_01", "inst_01", "step_03", status="completed")
    api.update_step(run_id, "step_01", instances_closed=True, summary="both iterations done")
    assert api.run_status(run_id) == "completed"


def test_replaying_a_single_body_step_within_one_instance(api: Api) -> None:
    _, run_id = api.run(
        [
            construct("step_01", "loop", ["step_02", "step_03"]),
            task("step_02"),
            task("step_03", depends_on=["step_02"]),
        ]
    )
    api.add_instance(run_id, "step_01")
    api.update_body_step(run_id, "step_01", "inst_00", "step_02", status="completed")
    api.update_body_step(run_id, "step_01", "inst_00", "step_03", status="completed")

    amendment_id = api.propose(
        run_id,
        [{"op": "replay_step", "target_step_id": "step_03", "instance_id": "inst_00"}],
        kind="history_edit",
        reason="output was wrong",
    ).json()["amendment_id"]
    api.approve(amendment_id)

    instance = api.instance(run_id, "step_01", "inst_00")
    assert instance["step_states"]["step_02"]["status"] == "completed"
    assert instance["step_states"]["step_03"]["status"] == "pending"
    assert len(instance["step_states"]["step_03"]["history"]) == 1
    assert instance["status"] == "running"


def test_amendment_may_be_proposed_after_the_run_finished(api: Api) -> None:
    _, run_id = api.run([task("step_01")])
    api.update_step(run_id, "step_01", status="completed")
    assert api.run_status(run_id) == "completed"
    amendment_id = api.propose(
        run_id, [replay_op("step_01")], kind="history_edit", reason="rerun"
    ).json()["amendment_id"]
    assert api.run_status(run_id) == "paused_for_approval"
    api.approve(amendment_id)
    assert api.run_status(run_id) == "running"


def test_history_edit_snapshots_survive_repeated_replays(api: Api) -> None:
    _, run_id = api.run([task("step_01")])
    for attempt in range(3):
        api.update_step(run_id, "step_01", status="completed", summary=f"attempt {attempt}")
        amendment_id = api.propose(
            run_id, [replay_op("step_01")], kind="history_edit", reason="again"
        ).json()["amendment_id"]
        api.approve(amendment_id)
    history = api.get_run(run_id)["step_states"]["step_01"]["history"]
    assert [entry["summary"] for entry in history] == ["attempt 0", "attempt 1", "attempt 2"]
    assert all("history" not in entry for entry in history)
