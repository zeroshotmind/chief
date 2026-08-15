"""Forward-looking amendments (REQ-12, REQ-13, REQ-15, REQ-33, REQ-39, contract 2.3)."""

from __future__ import annotations

from .conftest import Api, construct, task


def insert_after(target: str, step: dict) -> dict:
    return {"op": "insert_after", "target_step_id": target, "step": step}


def two_steps() -> list[dict]:
    return [task("step_01"), task("step_02", depends_on=["step_01"])]


def test_proposal_pauses_the_run_and_needs_approval(api: Api) -> None:
    _, run_id = api.run(two_steps())
    api.update_step(run_id, "step_01", status="completed")

    response = api.propose(
        run_id, [insert_after("step_02", task("step_03", depends_on=["step_02"]))]
    )
    assert response.status_code == 201
    amendment = response.json()
    assert amendment["status"] == "pending_approval"
    assert amendment["decided_at"] is None
    assert api.run_status(run_id) == "paused_for_approval"
    # Not applied yet (REQ-13).
    assert "step_03" not in api.get_run(run_id)["step_states"]


def test_only_one_amendment_may_be_pending_per_run(api: Api) -> None:
    _, run_id = api.run(two_steps())
    api.propose(run_id, [insert_after("step_02", task("step_03"))])
    second = api.propose(run_id, [insert_after("step_02", task("step_04"))])
    assert second.status_code == 409
    assert "one pending amendment" in second.json()["error"]["message"]


def test_approval_applies_the_plan_and_resumes_the_run(api: Api) -> None:
    workflow_id, run_id = api.run(two_steps())
    api.update_step(run_id, "step_01", status="completed")
    amendment_id = api.propose(
        run_id, [insert_after("step_02", task("step_03", depends_on=["step_02"]))]
    ).json()["amendment_id"]

    approved = api.approve(amendment_id).json()
    assert approved["status"] == "approved"
    assert approved["decided_by"] == "human"
    assert approved["decided_at"]
    assert approved["resulting_workflow_version"] == 2

    run = api.get_run(run_id)
    assert run["status"] == "running"
    assert run["applied_amendment_ids"] == [amendment_id]
    assert run["base_version"] == 1  # pinned; not dragged forward
    assert run["step_states"]["step_03"]["status"] == "pending"

    definition = api.client.get(f"/v1/workflows/{workflow_id}").json()
    assert definition["version"] == 2
    assert [s["id"] for s in definition["steps"]] == ["step_01", "step_02", "step_03"]
    assert api.client.get(f"/v1/workflows/{workflow_id}/versions/1").json()["version"] == 1


def test_rejection_leaves_the_plan_untouched_and_resumes_the_run(api: Api) -> None:
    workflow_id, run_id = api.run(two_steps())
    amendment_id = api.propose(run_id, [insert_after("step_02", task("step_03"))]).json()[
        "amendment_id"
    ]
    rejected = api.reject(amendment_id, reason="not needed").json()
    assert rejected["status"] == "rejected"
    assert rejected["decision_reason"] == "not needed"
    assert api.run_status(run_id) == "running"
    assert "step_03" not in api.get_run(run_id)["step_states"]
    assert api.client.get(f"/v1/workflows/{workflow_id}").json()["version"] == 1


def test_withdrawal_by_the_proposer(api: Api) -> None:
    _, run_id = api.run(two_steps())
    amendment_id = api.propose(run_id, [insert_after("step_02", task("step_03"))]).json()[
        "amendment_id"
    ]
    withdrawn = api.withdraw(amendment_id, reason="changed my mind").json()
    assert withdrawn["status"] == "withdrawn"
    assert api.run_status(run_id) == "running"
    # A decided amendment cannot be decided again.
    assert api.approve(amendment_id).status_code == 409
    assert api.withdraw(amendment_id).status_code == 409


def test_pause_clears_back_to_completed_not_running(api: Api) -> None:
    """A proposal near the end of a run must be able to resolve back into a terminal state."""
    _, run_id = api.run(two_steps())
    api.update_step(run_id, "step_01", status="completed")
    api.update_step(run_id, "step_02", status="completed")
    assert api.run_status(run_id) == "completed"

    amendment_id = api.propose(
        run_id,
        [{"op": "update_step", "target_step_id": "step_02", "step": task("step_02")}],
        kind="history_edit",
    ).json()["amendment_id"]
    assert api.run_status(run_id) == "paused_for_approval"
    api.reject(amendment_id)
    assert api.run_status(run_id) == "completed"


def test_listing_pending_amendments(api: Api) -> None:
    _, run_id = api.run(two_steps())
    api.propose(run_id, [insert_after("step_02", task("step_03"))])
    pending = api.client.get(f"/v1/runs/{run_id}/amendments?status=pending_approval").json()
    assert len(pending) == 1
    assert len(api.client.get(f"/v1/runs/{run_id}/amendments").json()) == 1
    single = api.client.get(f"/v1/amendments/{pending[0]['amendment_id']}")
    assert single.status_code == 200
    assert api.client.get("/v1/amendments/nope").status_code == 404


def test_operations_apply_atomically(api: Api) -> None:
    """A remove paired with a rewire is valid; the same remove alone is not."""
    _, run_id = api.run(
        [
            task("step_01"),
            task("step_02", depends_on=["step_01"]),
            task("step_03", depends_on=["step_02"]),
        ]
    )
    unpaired = api.propose(run_id, [{"op": "remove_step", "target_step_id": "step_02"}])
    assert unpaired.status_code == 422
    assert "unknown step" in unpaired.json()["error"]["message"]
    assert api.run_status(run_id) == "running"  # nothing was applied

    paired = api.propose(
        run_id,
        [
            {"op": "remove_step", "target_step_id": "step_02"},
            {
                "op": "update_step",
                "target_step_id": "step_03",
                "step": task("step_03", depends_on=["step_01"]),
            },
        ],
    )
    assert paired.status_code == 201
    api.approve(paired.json()["amendment_id"])
    run = api.get_run(run_id)
    assert run["step_states"]["step_02"]["status"] == "skipped"
    assert run["step_states"]["step_03"]["status"] == "pending"


def test_step_ids_are_never_reused(api: Api) -> None:
    _, run_id = api.run(two_steps())
    reuse = api.propose(run_id, [insert_after("step_02", task("step_01"))])
    assert reuse.status_code == 422
    assert "already in use" in reuse.json()["error"]["message"]

    removal = api.propose(
        run_id,
        [
            {"op": "remove_step", "target_step_id": "step_02"},
        ],
    )
    # step_02 has no dependents, so removal alone is fine here.
    assert removal.status_code == 201
    api.approve(removal.json()["amendment_id"])
    revive = api.propose(run_id, [insert_after("step_01", task("step_02"))])
    assert revive.status_code == 422
    assert "never reused" in revive.json()["error"]["message"]


def test_update_step_cannot_rename_a_step(api: Api) -> None:
    _, run_id = api.run(two_steps())
    response = api.propose(
        run_id, [{"op": "update_step", "target_step_id": "step_02", "step": task("step_99")}]
    )
    assert response.status_code == 422
    assert "permanent" in response.json()["error"]["message"]


def test_insert_into_a_loop_body_reaches_in_flight_instances(api: Api) -> None:
    _, run_id = api.run([construct("step_01", "loop", ["step_02"]), task("step_02")])
    api.add_instance(run_id, "step_01")
    api.add_instance(run_id, "step_01")
    api.update_instance(run_id, "step_01", "inst_00", status="completed")

    amendment_id = api.propose(
        run_id, [insert_after("step_02", task("step_03", depends_on=["step_02"]))]
    ).json()["amendment_id"]
    api.approve(amendment_id)

    finished = api.instance(run_id, "step_01", "inst_00")
    in_flight = api.instance(run_id, "step_01", "inst_01")
    # The finished iteration keeps the body it ran (REQ-14).
    assert finished["body"] == ["step_02"]
    assert finished["status"] == "completed"
    # The in-flight one picks the new step up.
    assert in_flight["body"] == ["step_02", "step_03"]
    assert set(in_flight["step_states"]) == {"step_02", "step_03"}


def test_amendment_against_an_unknown_run(api: Api) -> None:
    assert api.propose("run_nope", [insert_after("step_02", task("step_03"))]).status_code == 404


def test_reason_is_required(api: Api) -> None:
    _, run_id = api.run(two_steps())
    response = api.client.post(
        f"/v1/runs/{run_id}/amendments",
        json={
            "proposed_by": "planner",
            "kind": "forward",
            "operations": [insert_after("step_02", task("step_03"))],
        },
    )
    assert response.status_code == 422


def test_step_type_cannot_change_after_the_step_has_started(api: Api) -> None:
    _, run_id = api.run(two_steps())
    api.update_step(run_id, "step_01", status="completed")
    response = api.propose(
        run_id,
        [
            {
                "op": "update_step",
                "target_step_id": "step_01",
                "step": construct("step_01", "loop", ["step_02"]),
            }
        ],
        kind="history_edit",
    )
    assert response.status_code == 409
    assert "type cannot be changed" in response.json()["error"]["message"]


def test_step_type_may_change_while_still_pending(api: Api) -> None:
    _, run_id = api.run([task("step_01"), task("step_02")])
    response = api.propose(
        run_id,
        [
            {
                "op": "update_step",
                "target_step_id": "step_01",
                "step": construct("step_01", "loop", ["step_02"]),
            }
        ],
    )
    assert response.status_code == 201
    api.approve(response.json()["amendment_id"])
    assert set(api.get_run(run_id)["step_states"]) == {"step_01"}


def test_a_started_step_cannot_be_moved_into_a_loop_body(api: Api) -> None:
    _, run_id = api.run([task("step_01"), task("step_02")])
    api.update_step(run_id, "step_02", status="completed")
    response = api.propose(
        run_id,
        [
            {
                "op": "update_step",
                "target_step_id": "step_01",
                "step": construct("step_01", "loop", ["step_02"]),
            }
        ],
    )
    assert response.status_code == 409
    assert "moved between the top level" in response.json()["error"]["message"]
