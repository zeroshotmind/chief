"""Step criteria and the completion gate (CONTRACT-NOTES.md #39).

The gate is forced enumeration, not verification: Chief cannot judge whether a criterion
holds, only refuse a completion that never addressed one. These tests are about that
distinction as much as the mechanics — note that a criterion answered with anything
non-blank passes, and that the refusal names the way out.
"""

from __future__ import annotations

from typing import Any

from .conftest import Api, checkpoint, construct, task


def with_criteria(step_id: str, criteria: list[Any], **kw: Any) -> dict:
    return {**task(step_id, **kw), "criteria": criteria}


# ── authoring ──────────────────────────────────────────────────────────────────────────


def test_criteria_are_authored_as_strings_and_come_back_numbered(api: Api) -> None:
    workflow_id = api.draft([with_criteria("step_01", ["suite is green", "no new lint"])])
    steps = api.client.get(f"/v1/workflows/{workflow_id}").json()["steps"]
    assert steps[0]["criteria"] == [
        {"id": "c1", "text": "suite is green"},
        {"id": "c2", "text": "no new lint"},
    ]


def test_criteria_are_optional_and_absent_by_default(api: Api) -> None:
    workflow_id = api.draft([task("step_01")])
    assert api.client.get(f"/v1/workflows/{workflow_id}").json()["steps"][0]["criteria"] == []


def test_a_blank_criterion_is_rejected(api: Api) -> None:
    assert api.create_workflow([with_criteria("step_01", ["   "])]).status_code == 422


def test_criteria_are_task_only(api: Api) -> None:
    for step in (
        {**checkpoint("step_01"), "criteria": ["someone said yes"]},
        {**construct("step_01", "loop", ["step_02"]), "criteria": ["converged"]},
    ):
        response = api.create_workflow([step, task("step_02")])
        assert response.status_code == 422, response.text
        assert "only to task steps" in response.json()["error"]["message"]


# ── the gate ───────────────────────────────────────────────────────────────────────────


def test_completion_is_refused_until_every_criterion_is_answered(api: Api) -> None:
    _, run_id = api.run([with_criteria("step_01", ["suite is green", "no new lint"])])
    response = api.update_step(
        run_id, "step_01", status="completed", criteria_met={"c1": "314 tests pass"}
    )
    assert response.status_code == 409, response.text
    error = response.json()["error"]
    assert [u["id"] for u in error["details"]["unmet"]] == ["c2"]
    # The refusal has to name the escape hatch, or a harness facing an impossible criterion
    # has nothing to do but loop.
    assert "amendment" in error["message"]
    assert "failed" in error["message"]
    # And the step is untouched: a refused completion is not a partial one.
    state = api.get_run(run_id)["step_states"]["step_01"]
    assert state["status"] == "pending"
    assert state["criteria_met"] == {}


def test_a_blank_answer_does_not_count(api: Api) -> None:
    _, run_id = api.run([with_criteria("step_01", ["suite is green"])])
    response = api.update_step(run_id, "step_01", status="completed", criteria_met={"c1": "  "})
    assert response.status_code == 409


def test_answering_every_criterion_completes_the_step(api: Api) -> None:
    _, run_id = api.run([with_criteria("step_01", ["suite is green", "no new lint"])])
    response = api.update_step(
        run_id,
        "step_01",
        status="completed",
        criteria_met={"c1": "314 tests pass", "c2": "ruff clean"},
    )
    assert response.status_code == 200, response.text
    state = api.get_run(run_id)["step_states"]["step_01"]
    assert state["status"] == "completed"
    assert state["criteria_met"] == {"c1": "314 tests pass", "c2": "ruff clean"}


def test_evidence_accumulates_across_updates_so_it_can_be_answered_as_you_go(api: Api) -> None:
    _, run_id = api.run([with_criteria("step_01", ["suite is green", "no new lint"])])
    api.update_step(run_id, "step_01", status="running", criteria_met={"c1": "314 tests pass"})
    response = api.update_step(run_id, "step_01", status="completed", criteria_met={"c2": "clean"})
    assert response.status_code == 200, response.text
    assert api.get_run(run_id)["step_states"]["step_01"]["criteria_met"] == {
        "c1": "314 tests pass",
        "c2": "clean",
    }


def test_the_gate_only_bites_on_completion(api: Api) -> None:
    _, run_id = api.run([with_criteria("step_01", ["suite is green"])])
    assert api.update_step(run_id, "step_01", status="running").status_code == 200
    # Failing with a criterion unmet is exactly what a harness should do rather than
    # reporting completion around it.
    assert api.update_step(run_id, "step_01", status="failed").status_code == 200


def test_a_step_without_criteria_is_unaffected(api: Api) -> None:
    _, run_id = api.run([task("step_01")])
    assert api.update_step(run_id, "step_01", status="completed").status_code == 200


def test_answering_a_criterion_that_does_not_exist_is_rejected(api: Api) -> None:
    _, run_id = api.run([with_criteria("step_01", ["suite is green"])])
    response = api.update_step(
        run_id, "step_01", status="completed", criteria_met={"c1": "yes", "c9": "invented"}
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["details"]["unknown"] == ["c9"]


def test_the_gate_reaches_steps_inside_an_instance_body(api: Api) -> None:
    steps = [
        construct("step_01", "parallel", ["step_02"]),
        with_criteria("step_02", ["branch produced a ranked slate"]),
    ]
    _, run_id = api.run(steps)
    api.client.post(f"/v1/runs/{run_id}/steps/step_01/instances", json={})
    path = "step_01/inst_00/step_02"
    refused = api.client.post(
        f"/v1/runs/{run_id}/state/{path}/updates", json={"summary": "done", "status": "completed"}
    )
    assert refused.status_code == 409, refused.text
    assert "cannot be completed yet" in refused.json()["error"]["message"]
    accepted = api.client.post(
        f"/v1/runs/{run_id}/state/{path}/updates",
        json={"summary": "done", "status": "completed", "criteria_met": {"c1": "five, ranked"}},
    )
    assert accepted.status_code == 200, accepted.text


# ── replay ─────────────────────────────────────────────────────────────────────────────


def test_replay_clears_the_evidence_so_the_gate_bites_again(api: Api) -> None:
    """A replayed step must answer for its criteria afresh.

    Evidence belongs to the attempt that produced it. Carried across, it would answer the
    new attempt's criteria with the old attempt's work — and because the gate only checks
    that each criterion has *an* answer, the replayed step would pass vacuously on exactly
    the path a person reached for because the first result was wrong.
    """
    _, run_id = api.run([with_criteria("step_01", ["suite is green"])])
    api.update_step(
        run_id, "step_01", status="completed", criteria_met={"c1": "green, with the wrong flags"}
    )

    amendment_id = api.propose(
        run_id,
        [{"op": "replay_step", "target_step_id": "step_01"}],
        kind="history_edit",
        reason="built with the wrong compiler flags",
    ).json()["amendment_id"]
    assert api.approve(amendment_id).json()["status"] == "approved"

    state = api.get_run(run_id)["step_states"]["step_01"]
    assert state["status"] == "pending"
    assert state["criteria_met"] == {}
    # Not lost, just no longer current: REQ-42 keeps the original beside it.
    assert state["history"][0]["criteria_met"] == {"c1": "green, with the wrong flags"}
    # And the gate is live again rather than satisfied by the attempt being replaced.
    assert api.update_step(run_id, "step_01", status="completed").status_code == 409


def test_rewording_a_criterion_forgets_what_was_said_about_it(api: Api) -> None:
    """An answer belongs to the question it was given for.

    Ids are positional, so rewording c1 leaves it addressed as c1 — and without this the
    evidence for the old wording would stand as an answer to the new one, which is the
    replay problem in a smaller shape.
    """
    steps = [with_criteria("step_01", ["the suite is green", "the docs mention it"])]
    _, run_id = api.run(steps)
    api.update_step(
        run_id, "step_01", status="running", criteria_met={"c1": "326 pass", "c2": "note #39"}
    )

    amended = {
        **with_criteria("step_01", ["the suite is green on Windows too", "the docs mention it"]),
    }
    amendment_id = api.propose(
        run_id,
        [{"op": "update_step", "target_step_id": "step_01", "step": amended}],
        reason="Windows was the case that actually broke",
    ).json()["amendment_id"]
    assert api.approve(amendment_id).json()["status"] == "approved"

    # c1 was reworded, so its evidence is gone; c2 is untouched and stays answered.
    assert api.get_run(run_id)["step_states"]["step_01"]["criteria_met"] == {"c2": "note #39"}
    refused = api.update_step(run_id, "step_01", status="completed")
    assert refused.status_code == 409
    assert [u["id"] for u in refused.json()["error"]["details"]["unmet"]] == ["c1"]
