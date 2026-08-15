"""Regressions for defects found while hardening the implementation.

Each test here corresponds to a bug that reached a wrong or unreachable state. They are
kept together so the failure modes stay documented rather than only fixed.
"""

from __future__ import annotations

from .conftest import Api, construct, task

# --- skipped must be a stable, server-owned terminal status --------------------------------


def test_a_skipped_construct_does_not_resurrect_on_the_next_recompute(api: Api) -> None:
    _, run_id = api.run(
        [
            task("step_01"),
            construct("step_02", "loop", ["step_03"], depends_on=["step_01"]),
            task("step_03"),
            task("step_04"),
        ]
    )
    api.update_step(run_id, "step_01", status="failed", summary="upstream blew up")
    assert api.step_status(run_id, "step_02") == "skipped"

    # Any later write triggers a full recompute; the construct's derivation must not
    # overwrite the skip with 'running'.
    api.update_step(run_id, "step_04", status="completed")
    assert api.step_status(run_id, "step_02") == "skipped"
    assert api.add_instance(run_id, "step_02").status_code == 409
    assert api.run_status(run_id) == "failed"


def test_a_completed_iteration_does_not_regress_and_the_run_terminates(api: Api) -> None:
    _, run_id = api.run(
        [
            construct("step_01", "loop", ["step_02", "step_03"], on_instance_failure="continue"),
            task("step_02"),
            construct("step_03", "loop", ["step_04"], depends_on=["step_02"]),
            task("step_04"),
        ]
    )
    api.add_instance(run_id, "step_01")
    api.update_body_step(run_id, "step_01", "inst_00", "step_02", status="failed")

    instance = api.instance(run_id, "step_01", "inst_00")
    assert instance["step_states"]["step_03"]["status"] == "skipped"
    assert instance["status"] == "completed"

    api.update_step(run_id, "step_01", instances_closed=True, summary="one iteration was enough")
    assert api.instance(run_id, "step_01", "inst_00")["status"] == "completed"
    assert api.step_status(run_id, "step_01") == "completed"
    assert api.run_status(run_id) == "completed"


# --- a skip is retracted once its cause is replayed away -----------------------------------


def test_replaying_a_failure_unblocks_everything_it_skipped(api: Api) -> None:
    _, run_id = api.run(
        [
            task("step_01"),
            task("step_02", depends_on=["step_01"]),
            task("step_03", depends_on=["step_02"]),
        ]
    )
    api.update_step(run_id, "step_01", status="failed", summary="flaky")
    assert api.step_status(run_id, "step_02") == "skipped"
    assert api.step_status(run_id, "step_03") == "skipped"
    assert api.run_status(run_id) == "failed"

    amendment_id = api.propose(
        run_id,
        [{"op": "replay_step", "target_step_id": "step_01"}],
        kind="history_edit",
        reason="retry after fixing the environment",
    ).json()["amendment_id"]
    api.approve(amendment_id)

    run = api.get_run(run_id)
    assert run["step_states"]["step_01"]["status"] == "pending"
    assert run["step_states"]["step_02"]["status"] == "pending"
    assert run["step_states"]["step_03"]["status"] == "pending"
    assert run["step_states"]["step_02"]["summary"] is None
    assert run["status"] == "running"

    for step_id in ("step_01", "step_02", "step_03"):
        assert api.update_step(run_id, step_id, status="completed").status_code == 200
    assert api.run_status(run_id) == "completed"


def test_a_removal_skip_is_never_retracted(api: Api) -> None:
    _, run_id = api.run([task("step_01"), task("step_02")])
    amendment_id = api.propose(run_id, [{"op": "remove_step", "target_step_id": "step_02"}]).json()[
        "amendment_id"
    ]
    api.approve(amendment_id)
    assert api.step_status(run_id, "step_02") == "skipped"
    api.update_step(run_id, "step_01", status="completed")
    assert api.step_status(run_id, "step_02") == "skipped"
    assert api.run_status(run_id) == "completed"


# --- a plan can never be emptied -----------------------------------------------------------


def test_an_amendment_cannot_remove_the_last_step(api: Api) -> None:
    _, run_id = api.run([task("step_01")])
    response = api.propose(run_id, [{"op": "remove_step", "target_step_id": "step_01"}])
    assert response.status_code == 422
    assert "at least one step" in response.json()["error"]["message"]


# --- amending a body step without naming an instance ---------------------------------------


def test_a_body_step_can_be_amended_across_every_instance(api: Api) -> None:
    _, run_id = api.run(
        [construct("step_01", "loop", ["step_02", "step_03"]), task("step_02"), task("step_03")]
    )
    api.add_instance(run_id, "step_01")
    api.add_instance(run_id, "step_01")

    response = api.propose(
        run_id,
        [
            {
                "op": "update_step",
                "target_step_id": "step_03",
                "step": task("step_03", harness="local:qwen"),
            }
        ],
    )
    assert response.status_code == 201
    api.approve(response.json()["amendment_id"])
    definition = api.client.get(f"/v1/runs/{run_id}/definition").json()
    assert definition["steps"][2]["harness"] == "local:qwen"


def test_an_unscoped_edit_of_a_body_step_completed_in_any_instance_is_a_history_edit(
    api: Api,
) -> None:
    _, run_id = api.run(
        [construct("step_01", "loop", ["step_02", "step_03"]), task("step_02"), task("step_03")]
    )
    api.add_instance(run_id, "step_01")
    api.add_instance(run_id, "step_01")
    api.update_body_step(run_id, "step_01", "inst_00", "step_02", status="completed")

    forward = api.propose(
        run_id, [{"op": "update_step", "target_step_id": "step_02", "step": task("step_02")}]
    )
    assert forward.status_code == 409
    assert forward.json()["error"]["details"]["required_kind"] == "history_edit"


# --- unresolvable targets are refused at proposal time -------------------------------------


def test_an_unknown_instance_id_cannot_smuggle_an_edit_past_the_history_check(api: Api) -> None:
    _, run_id = api.run([construct("step_01", "loop", ["step_02"]), task("step_02")])
    api.add_instance(run_id, "step_01")
    api.update_instance(run_id, "step_01", "inst_00", status="completed")

    response = api.propose(
        run_id,
        [
            {
                "op": "update_step",
                "target_step_id": "step_02",
                "instance_id": "inst_ghost",
                "step": task("step_02", harness="local:qwen"),
            }
        ],
    )
    assert response.status_code == 404
    assert "inst_ghost" in response.json()["error"]["message"]
    assert api.run_status(run_id) == "running"


def test_an_instance_scoped_edit_of_a_completed_instance_needs_a_history_edit(api: Api) -> None:
    _, run_id = api.run([construct("step_01", "loop", ["step_02"]), task("step_02")])
    api.add_instance(run_id, "step_01")
    api.update_instance(run_id, "step_01", "inst_00", status="completed")

    op = {
        "op": "update_step",
        "target_step_id": "step_02",
        "instance_id": "inst_00",
        "step": task("step_02", harness="local:qwen"),
    }
    assert api.propose(run_id, [op]).status_code == 409
    assert api.propose(run_id, [op], kind="history_edit").status_code == 201


def test_a_replay_with_an_unresolvable_target_never_pins_the_run(api: Api) -> None:
    _, run_id = api.run([construct("step_01", "loop", ["step_02"]), task("step_02")])
    api.add_instance(run_id, "step_01")

    unknown_instance = api.propose(
        run_id,
        [{"op": "replay_step", "target_step_id": "step_01", "instance_id": "inst_99"}],
        kind="history_edit",
    )
    assert unknown_instance.status_code == 404
    assert api.run_status(run_id) == "running"

    never_ran = api.propose(
        run_id, [{"op": "replay_step", "target_step_id": "step_02"}], kind="history_edit"
    )
    assert never_ran.status_code == 409
    assert "nothing to replay" in never_ran.json()["error"]["message"]
    assert api.run_status(run_id) == "running"
    # The run is not stuck behind an unapprovable proposal.
    assert (
        api.propose(
            run_id, [{"op": "insert_after", "target_step_id": "step_02", "step": task("step_03")}]
        ).status_code
        == 201
    )


# --- a recorded result is immutable outside an amendment -----------------------------------


def test_a_completed_step_cannot_be_reset_by_a_plain_update(api: Api) -> None:
    _, run_id = api.run([task("step_01")])
    api.update_step(run_id, "step_01", status="completed", summary="done")
    assert api.run_status(run_id) == "completed"

    response = api.update_step(run_id, "step_01", status="pending", summary="never mind")
    assert response.status_code == 409
    assert "immutable" in response.json()["error"]["message"]
    state = api.get_run(run_id)["step_states"]["step_01"]
    assert state["status"] == "completed"
    assert state["summary"] == "done"
    assert api.run_status(run_id) == "completed"


def test_a_failed_step_cannot_be_retried_by_a_plain_update(api: Api) -> None:
    _, run_id = api.run([task("step_01")])
    api.update_step(run_id, "step_01", status="failed", summary="boom")
    response = api.update_step(run_id, "step_01", status="completed", summary="worked this time")
    assert response.status_code == 409
    assert "REQ-41" in response.json()["error"]["message"]


def test_a_completed_instance_cannot_be_reset_by_a_plain_update(api: Api) -> None:
    _, run_id = api.run([construct("step_01", "loop", ["step_02"]), task("step_02")])
    api.add_instance(run_id, "step_01")
    api.update_instance(run_id, "step_01", "inst_00", status="completed")
    response = api.update_instance(run_id, "step_01", "inst_00", status="failed")
    assert response.status_code == 409
    assert api.instance(run_id, "step_01", "inst_00")["status"] == "completed"


# --- concurrent runs on one workflow -------------------------------------------------------


def test_each_run_keeps_its_own_effective_plan(api: Api) -> None:
    workflow_id = api.approved_workflow([task("step_01"), task("step_02")])
    run_a = api.client.post(f"/v1/workflows/{workflow_id}/runs", json={}).json()["run_id"]
    run_b = api.client.post(f"/v1/workflows/{workflow_id}/runs", json={}).json()["run_id"]

    first = api.propose(
        run_a, [{"op": "insert_after", "target_step_id": "step_01", "step": task("step_0a")}]
    ).json()["amendment_id"]
    api.approve(first)
    second = api.propose(
        run_b, [{"op": "insert_after", "target_step_id": "step_01", "step": task("step_0b")}]
    ).json()["amendment_id"]
    api.approve(second)

    # B never picked up A's amendment.
    b_definition = api.client.get(f"/v1/runs/{run_b}/definition").json()
    assert [s["id"] for s in b_definition["steps"]] == ["step_01", "step_0b", "step_02"]
    # The effective plan carries its own lineage rather than a shared version number.
    assert b_definition["base_version"] == 1
    assert b_definition["applied_amendment_ids"] == [second]
    assert "version" not in b_definition
    assert api.get_run(run_b)["base_version"] == 1

    a_definition = api.client.get(f"/v1/runs/{run_a}/definition").json()
    assert [s["id"] for s in a_definition["steps"]] == ["step_01", "step_0a", "step_02"]

    # The shared definition carries both, at version 3.
    workflow = api.client.get(f"/v1/workflows/{workflow_id}").json()
    assert workflow["version"] == 3
    assert {s["id"] for s in workflow["steps"]} == {"step_01", "step_0a", "step_0b", "step_02"}


def test_a_sibling_run_that_removed_the_target_makes_approval_fail_cleanly(api: Api) -> None:
    workflow_id = api.approved_workflow([task("step_01"), task("step_02")])
    run_a = api.client.post(f"/v1/workflows/{workflow_id}/runs", json={}).json()["run_id"]
    run_b = api.client.post(f"/v1/workflows/{workflow_id}/runs", json={}).json()["run_id"]

    conflicting = api.propose(
        run_b, [{"op": "update_step", "target_step_id": "step_02", "step": task("step_02")}]
    ).json()["amendment_id"]
    removal = api.propose(run_a, [{"op": "remove_step", "target_step_id": "step_02"}]).json()[
        "amendment_id"
    ]
    api.approve(removal)

    response = api.approve(conflicting)
    assert response.status_code == 409
    assert "sibling run" in response.json()["error"]["message"]
    # Nothing partially applied.
    assert api.client.get(f"/v1/workflows/{workflow_id}").json()["version"] == 2
    assert api.get_amendment_status(conflicting) == "pending_approval"


# --- server-derived fields are not accepted as input ---------------------------------------


def test_run_state_cannot_be_dictated_at_registration(api: Api) -> None:
    workflow_id = api.approved_workflow([task("step_01")])
    for payload in ({"status": "completed"}, {"step_states": {}}, {"base_version": 9}):
        response = api.client.post(f"/v1/workflows/{workflow_id}/runs", json=payload)
        assert response.status_code == 422, payload


def test_summary_is_required_on_the_instance_endpoints(api: Api) -> None:
    _, run_id = api.run([construct("step_01", "loop", ["step_02"]), task("step_02")])
    api.add_instance(run_id, "step_01")
    assert (
        api.client.post(
            f"/v1/runs/{run_id}/steps/step_01/instances/inst_00/updates",
            json={"status": "completed"},
        ).status_code
        == 422
    )
    assert (
        api.client.post(
            f"/v1/runs/{run_id}/steps/step_01/instances/inst_00/steps/step_02/updates",
            json={"status": "completed"},
        ).status_code
        == 422
    )


def test_skipped_is_rejected_on_the_instance_endpoints(api: Api) -> None:
    _, run_id = api.run([construct("step_01", "loop", ["step_02"]), task("step_02")])
    api.add_instance(run_id, "step_01")
    assert api.update_instance(run_id, "step_01", "inst_00", status="skipped").status_code == 422
    assert (
        api.update_body_step(run_id, "step_01", "inst_00", "step_02", status="skipped").status_code
        == 422
    )


# --- an amendment is re-validated against the state at approval time ----------------------


def test_a_forward_amendment_cannot_be_approved_after_its_target_completed(api: Api) -> None:
    """A run keeps executing while an amendment waits for a human."""
    _, run_id = api.run([task("step_01"), task("step_02")])
    amendment_id = api.propose(run_id, [{"op": "remove_step", "target_step_id": "step_02"}]).json()[
        "amendment_id"
    ]

    api.update_step(run_id, "step_02", status="completed", summary="shipped the release")

    response = api.approve(amendment_id)
    assert response.status_code == 409
    assert response.json()["error"]["details"]["required_kind"] == "history_edit"
    state = api.get_run(run_id)["step_states"]["step_02"]
    assert state["status"] == "completed"
    assert state["summary"] == "shipped the release"


def test_a_type_change_cannot_be_approved_after_the_step_completed(api: Api) -> None:
    _, run_id = api.run([task("step_01"), task("step_02"), task("step_03")])
    amendment_id = api.propose(
        run_id,
        [
            {
                "op": "update_step",
                "target_step_id": "step_02",
                "step": construct("step_02", "loop", ["step_03"]),
            }
        ],
    ).json()["amendment_id"]

    api.update_step(run_id, "step_02", status="completed")
    response = api.approve(amendment_id)
    assert response.status_code == 409
    assert api.step_status(run_id, "step_02") == "completed"

    # Even resubmitted as a history_edit, the type change is refused once the step has run.
    api.reject(amendment_id, reason="superseded")
    resubmitted = api.propose(
        run_id,
        [
            {
                "op": "update_step",
                "target_step_id": "step_02",
                "step": construct("step_02", "loop", ["step_03"]),
            }
        ],
        kind="history_edit",
    )
    assert resubmitted.status_code == 409
    assert "type cannot be changed" in resubmitted.json()["error"]["message"]


# --- multi-operation amendments ------------------------------------------------------------


def test_one_amendment_may_insert_a_step_and_then_build_on_it(api: Api) -> None:
    _, run_id = api.run([task("step_01"), task("step_02")])
    response = api.propose(
        run_id,
        [
            {
                "op": "insert_after",
                "target_step_id": "step_01",
                "step": task("step_0a", depends_on=["step_01"]),
            },
            {
                "op": "insert_after",
                "target_step_id": "step_0a",
                "step": task("step_0b", depends_on=["step_0a"]),
            },
        ],
    )
    assert response.status_code == 201
    assert response.json()["kind"] == "forward"
    api.approve(response.json()["amendment_id"])
    plan = api.client.get(f"/v1/runs/{run_id}/definition").json()
    assert [s["id"] for s in plan["steps"]] == ["step_01", "step_0a", "step_0b", "step_02"]


def test_one_amendment_may_insert_a_step_and_then_edit_it(api: Api) -> None:
    _, run_id = api.run([task("step_01")])
    response = api.propose(
        run_id,
        [
            {"op": "insert_after", "target_step_id": "step_01", "step": task("step_0a")},
            {
                "op": "update_step",
                "target_step_id": "step_0a",
                "step": task("step_0a", harness="local:gemma"),
            },
        ],
    )
    assert response.status_code == 201
    api.approve(response.json()["amendment_id"])
    plan = api.client.get(f"/v1/runs/{run_id}/definition").json()
    assert plan["steps"][1]["harness"] == "local:gemma"


# --- unscoped replay leaves in-flight siblings alone ----------------------------------------


def test_an_unscoped_replay_does_not_discard_an_in_flight_sibling(api: Api) -> None:
    _, run_id = api.run(
        [
            construct("step_01", "loop", ["step_02"], on_instance_failure="continue"),
            task("step_02"),
        ]
    )
    api.add_instance(run_id, "step_01")
    api.add_instance(run_id, "step_01")
    api.update_body_step(run_id, "step_01", "inst_00", "step_02", status="failed")
    api.update_body_step(
        run_id, "step_01", "inst_01", "step_02", status="running", summary="halfway"
    )

    amendment_id = api.propose(
        run_id,
        [{"op": "replay_step", "target_step_id": "step_02"}],
        kind="history_edit",
        reason="retry the failed copy",
    ).json()["amendment_id"]
    api.approve(amendment_id)

    replayed = api.instance(run_id, "step_01", "inst_00")["step_states"]["step_02"]
    in_flight = api.instance(run_id, "step_01", "inst_01")["step_states"]["step_02"]
    assert replayed["status"] == "pending"
    assert len(replayed["history"]) == 1
    assert in_flight["status"] == "running"
    assert in_flight["summary"] == "halfway"


# --- removal bookkeeping ---------------------------------------------------------------------


def test_removing_a_step_that_was_already_skipped_records_the_removal(api: Api) -> None:
    _, run_id = api.run([task("step_01"), task("step_02", depends_on=["step_01"]), task("step_03")])
    api.update_step(run_id, "step_01", status="failed")
    assert api.step_status(run_id, "step_02") == "skipped"

    amendment_id = api.propose(run_id, [{"op": "remove_step", "target_step_id": "step_02"}]).json()[
        "amendment_id"
    ]
    api.approve(amendment_id)

    state = api.get_run(run_id)["step_states"]["step_02"]
    assert state["status"] == "skipped"
    assert state["skip_cause"] == "removed"
    assert "Removed from the plan" in state["summary"]
    assert len(state["history"]) == 1


def test_a_body_step_removed_from_a_loop_can_be_put_back(api: Api) -> None:
    _, run_id = api.run(
        [
            construct("step_01", "loop", ["step_02", "step_03"]),
            task("step_02"),
            task("step_03"),
        ]
    )
    api.add_instance(run_id, "step_01")

    shrink = api.propose(
        run_id,
        [
            {
                "op": "update_step",
                "target_step_id": "step_01",
                "step": construct("step_01", "loop", ["step_02"]),
            }
        ],
    )
    assert shrink.status_code == 201
    api.approve(shrink.json()["amendment_id"])

    restore = api.propose(
        run_id,
        [
            {
                "op": "update_step",
                "target_step_id": "step_01",
                "step": construct("step_01", "loop", ["step_02", "step_03"]),
            }
        ],
    )
    assert restore.status_code == 201, restore.text
    api.approve(restore.json()["amendment_id"])
    instance = api.instance(run_id, "step_01", "inst_00")
    assert instance["body"] == ["step_02", "step_03"]
    assert instance["step_states"]["step_03"]["status"] == "pending"
    assert instance["step_states"]["step_03"]["skip_cause"] is None


def test_an_operation_on_a_freshly_inserted_step_cannot_pin_the_run(api: Api) -> None:
    """The waiver for ids created within an amendment is narrow: existence only."""
    _, run_id = api.run([task("step_01"), task("step_02")])

    replay = api.propose(
        run_id,
        [
            {"op": "insert_after", "target_step_id": "step_01", "step": task("step_0a")},
            {"op": "replay_step", "target_step_id": "step_0a"},
        ],
        kind="history_edit",
    )
    assert replay.status_code == 409
    assert "nothing to replay" in replay.json()["error"]["message"]

    scoped = api.propose(
        run_id,
        [
            {"op": "insert_after", "target_step_id": "step_01", "step": task("step_0b")},
            {
                "op": "update_step",
                "target_step_id": "step_0b",
                "instance_id": "inst_ghost",
                "step": task("step_0b", harness="local:qwen"),
            },
        ],
    )
    assert scoped.status_code == 422
    assert "cannot be scoped" in scoped.json()["error"]["message"]

    assert api.run_status(run_id) == "running"
    assert api.client.get(f"/v1/runs/{run_id}/amendments").json() == []


def test_a_replay_is_a_history_edit_even_beside_an_insert(api: Api) -> None:
    _, run_id = api.run([task("step_01"), task("step_02")])
    api.update_step(run_id, "step_01", status="completed")
    response = api.propose(
        run_id,
        [
            {"op": "insert_after", "target_step_id": "step_02", "step": task("step_0a")},
            {"op": "replay_step", "target_step_id": "step_01"},
        ],
    )
    assert response.status_code == 409
    assert response.json()["error"]["details"]["required_kind"] == "history_edit"


def test_moving_a_skipped_step_into_a_body_leaves_no_stale_top_level_record(api: Api) -> None:
    _, run_id = api.run(
        [
            construct("step_01", "loop", ["step_02"]),
            task("step_02"),
            task("step_03"),
            task("step_04", depends_on=["step_03"]),
        ]
    )
    api.add_instance(run_id, "step_01")
    api.update_step(run_id, "step_03", status="failed")
    assert api.step_status(run_id, "step_04") == "skipped"

    amendment_id = api.propose(
        run_id,
        [
            {"op": "update_step", "target_step_id": "step_04", "step": task("step_04")},
            {
                "op": "update_step",
                "target_step_id": "step_01",
                "step": construct("step_01", "loop", ["step_02", "step_04"]),
            },
        ],
    ).json()["amendment_id"]
    api.approve(amendment_id)

    run = api.get_run(run_id)
    assert "step_04" not in run["step_states"]
    instance = api.instance(run_id, "step_01", "inst_00")
    assert instance["step_states"]["step_04"]["status"] == "pending"
    assert instance["step_states"]["step_04"]["skip_cause"] is None


# --- operations apply sequentially, so validation simulates them ---------------------------


def test_a_second_replay_of_the_same_target_is_refused_at_proposal(api: Api) -> None:
    _, run_id = api.run([task("step_01"), task("step_02")])
    api.update_step(run_id, "step_01", status="failed")
    response = api.propose(
        run_id,
        [
            {"op": "replay_step", "target_step_id": "step_01"},
            {"op": "replay_step", "target_step_id": "step_01"},
        ],
        kind="history_edit",
    )
    assert response.status_code == 409
    assert api.run_status(run_id) == "failed"
    assert api.client.get(f"/v1/runs/{run_id}/amendments").json() == []


def test_an_op_scoped_to_an_instance_an_earlier_op_destroys_is_refused(api: Api) -> None:
    _, run_id = api.run([construct("step_01", "loop", ["step_02"]), task("step_02")])
    api.add_instance(run_id, "step_01")
    api.update_instance(run_id, "step_01", "inst_00", status="failed")

    response = api.propose(
        run_id,
        [
            {"op": "replay_step", "target_step_id": "step_01"},
            {"op": "replay_step", "target_step_id": "step_02", "instance_id": "inst_00"},
        ],
        kind="history_edit",
    )
    assert response.status_code == 404
    assert api.run_status(run_id) == "failed"
    # The run is free to accept a well-formed proposal instead.
    assert (
        api.propose(
            run_id,
            [{"op": "replay_step", "target_step_id": "step_01"}],
            kind="history_edit",
        ).status_code
        == 201
    )


def test_a_replayed_steps_preserved_result_is_not_dropped_by_a_later_move(api: Api) -> None:
    _, run_id = api.run(
        [construct("step_01", "loop", ["step_02"]), task("step_02"), task("step_03")]
    )
    api.add_instance(run_id, "step_01")
    api.update_step(run_id, "step_03", status="completed", summary="first attempt")

    replay = api.propose(
        run_id,
        [{"op": "replay_step", "target_step_id": "step_03"}],
        kind="history_edit",
        reason="wrong output",
    ).json()["amendment_id"]
    api.approve(replay)
    state = api.get_run(run_id)["step_states"]["step_03"]
    assert state["status"] == "pending"
    assert state["history"][0]["summary"] == "first attempt"

    move = api.propose(
        run_id,
        [
            {
                "op": "update_step",
                "target_step_id": "step_01",
                "step": construct("step_01", "loop", ["step_02", "step_03"]),
            }
        ],
    )
    assert move.status_code == 409
    assert "preserved result" in move.json()["error"]["message"]
    assert api.get_run(run_id)["step_states"]["step_03"]["history"][0]["summary"] == (
        "first attempt"
    )


def test_a_replay_and_a_body_edit_in_one_amendment_both_take_effect(api: Api) -> None:
    """The instance is live again after the replay, so it must pick the body change up."""
    _, run_id = api.run(
        [
            construct("step_01", "loop", ["step_02"], on_instance_failure="continue"),
            task("step_02"),
            task("step_03"),
        ]
    )
    api.add_instance(run_id, "step_01")
    api.update_body_step(run_id, "step_01", "inst_00", "step_02", status="failed")
    assert api.instance(run_id, "step_01", "inst_00")["status"] == "completed"

    amendment_id = api.propose(
        run_id,
        [
            {"op": "replay_step", "target_step_id": "step_02"},
            {
                "op": "update_step",
                "target_step_id": "step_01",
                "step": construct(
                    "step_01", "loop", ["step_02", "step_03"], on_instance_failure="continue"
                ),
            },
        ],
        kind="history_edit",
        reason="retry with the extra check",
    ).json()["amendment_id"]
    api.approve(amendment_id)

    instance = api.instance(run_id, "step_01", "inst_00")
    assert instance["body"] == ["step_02", "step_03"]
    assert instance["step_states"]["step_03"]["status"] == "pending"

    api.update_body_step(run_id, "step_01", "inst_00", "step_02", status="completed")
    api.update_step(run_id, "step_01", instances_closed=True, summary="closed")
    # The run must not report success while the amended step has not run.
    assert api.run_status(run_id) == "running"
    api.update_body_step(run_id, "step_01", "inst_00", "step_03", status="completed")
    assert api.run_status(run_id) == "completed"


def test_the_ui_is_always_revalidated(client) -> None:
    """A stale app.js against a current API looks like the UI is broken, not the cache.

    The page and the server are versioned independently — editing the UI does not restart
    Chief — so the browser must ask before reusing a script it already has.
    """
    response = client.get("/ui/app.js")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-cache"

    # Still cheap: unchanged files answer 304 rather than resending.
    again = client.get("/ui/app.js", headers={"If-None-Match": response.headers["etag"]})
    assert again.status_code == 304
