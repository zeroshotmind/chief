"""Schema and structural validation of a submitted plan (REQ-34 to REQ-37)."""

from __future__ import annotations

import pytest

from .conftest import Api, construct, task


def test_duplicate_step_ids_are_rejected(api: Api) -> None:
    response = api.create_workflow([task("step_01"), task("step_01")])
    assert response.status_code == 422
    assert "duplicate step id" in response.json()["error"]["message"]


def test_goal_is_mandatory(api: Api) -> None:
    step = task("step_01")
    step["goal"] = "   "
    assert api.create_workflow([step]).status_code == 422


def test_construct_needs_a_body(api: Api) -> None:
    response = api.create_workflow([construct("step_01", "loop", [])])
    assert response.status_code == 422


def test_task_must_not_have_a_body(api: Api) -> None:
    step = task("step_01")
    step["body"] = ["step_02"]
    assert api.create_workflow([step, task("step_02")]).status_code == 422


def test_task_cannot_declare_on_instance_failure(api: Api) -> None:
    step = task("step_01")
    step["on_instance_failure"] = "continue"
    response = api.create_workflow([step])
    assert response.status_code == 422
    assert "loop/parallel" in response.json()["error"]["message"]


def test_body_must_reference_existing_steps(api: Api) -> None:
    response = api.create_workflow([construct("step_01", "loop", ["nope"])])
    assert response.status_code == 422
    assert "unknown step" in response.json()["error"]["message"]


def test_a_step_cannot_belong_to_two_bodies(api: Api) -> None:
    response = api.create_workflow(
        [
            construct("step_01", "loop", ["step_03"]),
            construct("step_02", "parallel", ["step_03"]),
            task("step_03"),
        ]
    )
    assert response.status_code == 422
    assert "more than one body" in response.json()["error"]["message"]


def test_containment_cycles_are_rejected(api: Api) -> None:
    response = api.create_workflow(
        [construct("step_01", "loop", ["step_02"]), construct("step_02", "loop", ["step_01"])]
    )
    assert response.status_code == 422


@pytest.mark.parametrize("kind", ["loop", "parallel"])
def test_body_steps_may_only_depend_within_their_body(api: Api, kind: str) -> None:
    response = api.create_workflow(
        [
            task("step_01"),
            construct("step_02", kind, ["step_03"]),
            task("step_03", depends_on=["step_01"]),
        ]
    )
    assert response.status_code == 422
    assert "same scope" in response.json()["error"]["message"]


def test_dependency_cycles_are_rejected(api: Api) -> None:
    response = api.create_workflow(
        [task("step_01", depends_on=["step_02"]), task("step_02", depends_on=["step_01"])]
    )
    assert response.status_code == 422
    assert "dependency cycle" in response.json()["error"]["message"]


def test_unknown_dependency_is_rejected(api: Api) -> None:
    assert api.create_workflow([task("step_01", depends_on=["ghost"])]).status_code == 422


def test_empty_workflow_is_rejected(api: Api) -> None:
    assert api.create_workflow([]).status_code == 422


def test_unknown_field_is_rejected(api: Api) -> None:
    step = task("step_01")
    step["retries"] = 3
    assert api.create_workflow([step]).status_code == 422


def test_artifact_needs_ref_or_data(api: Api) -> None:
    _, run_id = api.run([task("step_01")])
    response = api.update_step(
        run_id, "step_01", status="completed", artifacts=[{"type": "file_ref"}]
    )
    assert response.status_code == 422


def test_artifact_id_is_generated_when_omitted(api: Api) -> None:
    _, run_id = api.run([task("step_01")])
    api.update_step(
        run_id, "step_01", status="completed", artifacts=[{"type": "url", "ref": "https://x"}]
    )
    artifacts = api.get_run(run_id)["step_states"]["step_01"]["artifacts"]
    assert artifacts[0]["artifact_id"].startswith("art_")
