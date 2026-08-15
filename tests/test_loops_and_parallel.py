"""Loop and parallel constructs: runtime instances and derived state.

REQ-10, REQ-11, REQ-19 and the derivation rules in contract 1.4 / 1.5.
"""

from __future__ import annotations

import pytest

from .conftest import Api, construct, task


def loop_workflow(*, on_instance_failure: str = "fail_fast") -> list[dict]:
    """One task, then a loop whose body is a two-step branch."""
    return [
        task("step_01"),
        construct(
            "step_02",
            "loop",
            ["step_03", "step_04"],
            depends_on=["step_01"],
            on_instance_failure=on_instance_failure,
        ),
        task("step_03"),
        task("step_04", depends_on=["step_03"]),
    ]


def test_instance_count_is_not_declared_up_front(api: Api) -> None:
    _, run_id = api.run(loop_workflow())
    api.update_step(run_id, "step_01", status="completed")
    for expected_index in range(3):
        response = api.add_instance(run_id, "step_02")
        assert response.status_code == 201
        assert response.json()["index"] == expected_index
        assert response.json()["kind"] == "iteration"
    assert len(api.get_run(run_id)["step_states"]["step_02"]["instances"]) == 3


def test_parallel_instances_are_branches(api: Api) -> None:
    _, run_id = api.run([construct("step_01", "parallel", ["step_02"]), task("step_02")])
    assert api.add_instance(run_id, "step_01").json()["kind"] == "branch"
    response = api.add_instance(run_id, "step_01", kind="iteration")
    assert response.status_code == 422


def test_instance_records_the_body_it_was_spawned_with(api: Api) -> None:
    _, run_id = api.run(loop_workflow())
    instance = api.add_instance(run_id, "step_02").json()
    assert instance["body"] == ["step_03", "step_04"]
    assert set(instance["step_states"]) == {"step_03", "step_04"}


def test_multi_step_body_tracks_progress_per_step_per_iteration(api: Api) -> None:
    _, run_id = api.run(loop_workflow())
    api.update_step(run_id, "step_01", status="completed")
    api.add_instance(run_id, "step_02")
    api.add_instance(run_id, "step_02")

    api.update_body_step(run_id, "step_02", "inst_00", "step_03", status="completed")
    api.update_body_step(run_id, "step_02", "inst_00", "step_04", status="running")

    first = api.instance(run_id, "step_02", "inst_00")
    assert first["step_states"]["step_03"]["status"] == "completed"
    assert first["step_states"]["step_04"]["status"] == "running"
    assert first["status"] == "running"
    assert api.instance(run_id, "step_02", "inst_01")["status"] == "pending"
    assert api.step_status(run_id, "step_02") == "running"


def test_instance_status_of_a_multi_step_body_cannot_be_reported(api: Api) -> None:
    _, run_id = api.run(loop_workflow())
    api.add_instance(run_id, "step_02")
    response = api.update_instance(run_id, "step_02", "inst_00", status="completed")
    assert response.status_code == 409
    assert "derived" in response.json()["error"]["message"]


def test_single_step_body_status_is_reported_on_the_instance(api: Api) -> None:
    _, run_id = api.run([construct("step_01", "loop", ["step_02"]), task("step_02")])
    api.add_instance(run_id, "step_01")
    assert api.update_instance(run_id, "step_01", "inst_00", status="completed").status_code == 200
    instance = api.instance(run_id, "step_01", "inst_00")
    assert instance["status"] == "completed"
    # Written through, so instance status stays derived from exactly one rule.
    assert instance["step_states"]["step_02"]["status"] == "completed"


def test_construct_completes_only_once_instances_are_closed(api: Api) -> None:
    _, run_id = api.run(loop_workflow())
    api.update_step(run_id, "step_01", status="completed")
    api.add_instance(run_id, "step_02")
    api.update_body_step(run_id, "step_02", "inst_00", "step_03", status="completed")
    api.update_body_step(run_id, "step_02", "inst_00", "step_04", status="completed")

    assert api.instance(run_id, "step_02", "inst_00")["status"] == "completed"
    # Every instance so far is done, but the harness may still spawn more.
    assert api.step_status(run_id, "step_02") == "running"
    assert api.run_status(run_id) == "running"

    api.update_step(run_id, "step_02", instances_closed=True, summary="no more iterations")
    assert api.step_status(run_id, "step_02") == "completed"
    assert api.run_status(run_id) == "completed"


def test_closing_before_any_instance_completes_vacuously(api: Api) -> None:
    _, run_id = api.run([construct("step_01", "loop", ["step_02"]), task("step_02")])
    api.update_step(run_id, "step_01", instances_closed=True, summary="nothing to iterate over")
    assert api.step_status(run_id, "step_01") == "completed"
    assert api.run_status(run_id) == "completed"


def test_closed_construct_refuses_new_instances_and_reopening(api: Api) -> None:
    _, run_id = api.run([construct("step_01", "loop", ["step_02"]), task("step_02")])
    api.update_step(run_id, "step_01", instances_closed=True, summary="done")
    assert api.add_instance(run_id, "step_01").status_code == 409
    response = api.update_step(run_id, "step_01", instances_closed=False, summary="reopen")
    assert response.status_code == 409


def test_failure_inside_a_body_skips_the_rest_and_fails_the_instance(api: Api) -> None:
    _, run_id = api.run(loop_workflow())
    api.update_step(run_id, "step_01", status="completed")
    api.add_instance(run_id, "step_02")
    api.update_body_step(run_id, "step_02", "inst_00", "step_03", status="failed")

    instance = api.instance(run_id, "step_02", "inst_00")
    assert instance["step_states"]["step_04"]["status"] == "skipped"
    assert instance["status"] == "failed"
    assert api.step_status(run_id, "step_02") == "failed"
    assert api.run_status(run_id) == "failed"


def test_on_instance_failure_continue_tolerates_a_failed_iteration(api: Api) -> None:
    _, run_id = api.run(loop_workflow(on_instance_failure="continue"))
    api.update_step(run_id, "step_01", status="completed")
    api.add_instance(run_id, "step_02")
    api.add_instance(run_id, "step_02")
    api.update_body_step(run_id, "step_02", "inst_00", "step_03", status="failed")

    assert api.instance(run_id, "step_02", "inst_00")["status"] == "completed"
    assert api.step_status(run_id, "step_02") == "running"

    api.update_body_step(run_id, "step_02", "inst_01", "step_03", status="completed")
    api.update_body_step(run_id, "step_02", "inst_01", "step_04", status="completed")
    api.update_step(run_id, "step_02", instances_closed=True, summary="closed")
    assert api.step_status(run_id, "step_02") == "completed"
    assert api.run_status(run_id) == "completed"


@pytest.mark.parametrize("kind", ["loop", "parallel"])
def test_branches_scale_horizontally(api: Api, kind: str) -> None:
    _, run_id = api.run([construct("step_01", kind, ["step_02"]), task("step_02")])
    for _ in range(25):
        assert api.add_instance(run_id, "step_01").status_code == 201
    for index in range(25):
        api.update_instance(run_id, "step_01", f"inst_{index:02d}", status="completed")
    api.update_step(run_id, "step_01", instances_closed=True, summary="all branches done")
    assert api.step_status(run_id, "step_01") == "completed"


def test_duplicate_instance_index_is_refused(api: Api) -> None:
    _, run_id = api.run([construct("step_01", "loop", ["step_02"]), task("step_02")])
    api.add_instance(run_id, "step_01", index=4)
    assert api.add_instance(run_id, "step_01", index=4).status_code == 409


def test_instances_cannot_be_registered_on_a_task(api: Api) -> None:
    _, run_id = api.run([task("step_01")])
    assert api.add_instance(run_id, "step_01").status_code == 422
