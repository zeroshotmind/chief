"""A loop inside a parallel branch (contract 1.2 nesting, 1.5 instance scoping).

The contract allows this shape but its endpoint list only reaches one level down; these
tests exercise the generalised ``/runs/{run_id}/state/{path}`` addressing that fills the gap.
"""

from __future__ import annotations

from .conftest import Api, construct, task


def nested_workflow() -> list[dict]:
    return [
        construct("step_01", "parallel", ["step_02"]),
        construct("step_02", "loop", ["step_03"]),
        task("step_03"),
    ]


def test_definition_accepts_a_construct_inside_a_body(api: Api) -> None:
    _, run_id = api.run(nested_workflow())
    assert set(api.get_run(run_id)["step_states"]) == {"step_01"}


def test_nested_instances_are_scoped_under_the_parent_instance(api: Api) -> None:
    _, run_id = api.run(nested_workflow())
    assert api.add_instance(run_id, "step_01").status_code == 201
    assert api.nested_instance(run_id, "step_01/inst_00/step_02").status_code == 201
    assert (
        api.nested_update(
            run_id, "step_01/inst_00/step_02/inst_00/step_03", status="completed"
        ).status_code
        == 200
    )

    outer = api.instance(run_id, "step_01", "inst_00")
    inner_state = outer["step_states"]["step_02"]
    inner_instance = inner_state["instances"][0]
    assert inner_instance["step_states"]["step_03"]["status"] == "completed"
    assert inner_instance["status"] == "completed"
    # The inner loop is still open, so nothing above it can be complete yet.
    assert inner_state["status"] == "running"
    assert outer["status"] == "running"
    assert api.run_status(run_id) == "running"


def test_closing_bubbles_completion_all_the_way_up(api: Api) -> None:
    _, run_id = api.run(nested_workflow())
    api.add_instance(run_id, "step_01")
    api.nested_instance(run_id, "step_01/inst_00/step_02")
    api.nested_update(run_id, "step_01/inst_00/step_02/inst_00/step_03", status="completed")

    api.nested_update(run_id, "step_01/inst_00/step_02", instances_closed=True)
    assert api.instance(run_id, "step_01", "inst_00")["status"] == "completed"

    api.update_step(run_id, "step_01", instances_closed=True, summary="all branches spawned")
    assert api.step_status(run_id, "step_01") == "completed"
    assert api.run_status(run_id) == "completed"


def test_failure_deep_inside_bubbles_up_to_the_run(api: Api) -> None:
    _, run_id = api.run(nested_workflow())
    api.add_instance(run_id, "step_01")
    api.nested_instance(run_id, "step_01/inst_00/step_02")
    api.nested_update(run_id, "step_01/inst_00/step_02/inst_00/step_03", status="failed")
    assert api.step_status(run_id, "step_01") == "failed"
    assert api.run_status(run_id) == "failed"


def test_nested_instance_update_endpoint(api: Api) -> None:
    _, run_id = api.run(nested_workflow())
    api.add_instance(run_id, "step_01")
    api.nested_instance(run_id, "step_01/inst_00/step_02")
    response = api.nested_instance_update(
        run_id, "step_01/inst_00/step_02/inst_00", status="completed"
    )
    assert response.status_code == 200
    outer = api.instance(run_id, "step_01", "inst_00")
    assert outer["step_states"]["step_02"]["instances"][0]["status"] == "completed"


def test_malformed_paths_are_refused(api: Api) -> None:
    _, run_id = api.run(nested_workflow())
    api.add_instance(run_id, "step_01")
    # even number of tokens: does not end on a step id
    assert api.nested_update(run_id, "step_01/inst_00", status="completed").status_code == 422
    # step_03 is not in step_01's body
    assert api.nested_instance(run_id, "step_01/inst_00/step_03").status_code == 422
    # unknown instance
    assert (
        api.nested_update(run_id, "step_01/inst_09/step_02", instances_closed=True).status_code
        == 404
    )


def test_documented_depth_one_routes_are_the_same_resolver(api: Api) -> None:
    _, run_id = api.run(nested_workflow())
    api.add_instance(run_id, "step_01")
    # The contract's body-step route reaches the nested construct itself.
    response = api.update_body_step(run_id, "step_01", "inst_00", "step_02", instances_closed=True)
    assert response.status_code == 200
    assert api.instance(run_id, "step_01", "inst_00")["step_states"]["step_02"]["status"] == (
        "completed"
    )
