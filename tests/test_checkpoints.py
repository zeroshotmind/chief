"""Checkpoints: the step type where the run waits for a person (extension).

Chief already had two human gates — approving a plan, and approving an amendment — and both
are about *the plan*. A checkpoint is about the work: a point the plan itself names as one a
person has to decide, and optionally answer in their own words before the run goes on.

The invariant these tests circle is the same one the amendment protocol protects: the
harness reaches the checkpoint, and stops. It cannot report how one turned out, because the
outcome is not its to give.
"""

from __future__ import annotations

from .conftest import Api, checkpoint, construct, task

# --- shape -------------------------------------------------------------------------------


def test_a_checkpoint_must_name_the_human_harness(api: Api):
    step = checkpoint("gate")
    step["harness"] = "claude_cli"
    response = api.create_workflow([step])
    assert response.status_code == 422
    assert "harness must be 'human'" in response.text


def test_only_a_checkpoint_may_ask_a_person_for_fields(api: Api):
    step = task("step_01")
    step["fields"] = [{"name": "answer"}]
    response = api.create_workflow([step])
    assert response.status_code == 422
    assert "fields applies only to checkpoint steps" in response.text


def test_a_checkpoint_cannot_ask_for_the_same_field_twice(api: Api):
    response = api.create_workflow(
        [checkpoint("gate", fields=[{"name": "budget"}, {"name": "budget"}])]
    )
    assert response.status_code == 422
    assert "same field twice" in response.text


def test_a_task_may_still_name_a_person_as_its_harness(api: Api):
    """The harness namespace stays open. Manual work someone reports afterwards is a task;
    a decision the run *waits on* is a checkpoint. Only the second is Chief's business."""
    assert api.create_workflow([task("step_01", harness="human")]).status_code == 201


# --- reaching one ------------------------------------------------------------------------


def test_reporting_a_checkpoint_running_blocks_it(api: Api):
    _, run_id = api.run([task("step_01"), checkpoint("gate", depends_on=["step_01"])])
    api.update_step(run_id, "step_01", status="completed")

    assert api.update_step(run_id, "gate", status="running").status_code == 200
    assert api.step_status(run_id, "gate") == "blocked"
    assert api.run_status(run_id) == "waiting_on_human"


def test_a_harness_cannot_report_how_a_checkpoint_turned_out(api: Api):
    _, run_id = api.run([checkpoint("gate")])
    response = api.update_step(run_id, "gate", status="completed")
    assert response.status_code == 409
    assert "resolve_checkpoint" in response.text
    assert api.step_status(run_id, "gate") == "pending"


def test_blocked_is_not_a_status_a_harness_can_claim(api: Api):
    """It is reached by reporting `running`, never asserted directly — the same rule the
    server-derived statuses follow."""
    _, run_id = api.run([checkpoint("gate")])
    assert api.update_step(run_id, "gate", status="blocked").status_code == 422


# --- deciding one ------------------------------------------------------------------------


def test_approving_a_checkpoint_completes_it_and_the_run_moves_on(api: Api):
    _, run_id = api.run([checkpoint("gate"), task("step_02", depends_on=["gate"])])
    api.update_step(run_id, "gate", status="running")

    response = api.resolve(run_id, "gate", note="looks right")
    assert response.status_code == 200, response.text
    assert api.step_status(run_id, "gate") == "completed"
    assert api.run_status(run_id) == "running"

    outcome = api.get_run(run_id)["step_states"]["gate"]["checkpoint"]
    assert outcome["decision"] == "approved"
    assert outcome["decided_by"] == "human"
    assert outcome["decided_at"]
    assert "looks right" in api.get_run(run_id)["step_states"]["gate"]["summary"]


def test_rejecting_a_checkpoint_fails_it_and_skips_what_came_after(api: Api):
    _, run_id = api.run([checkpoint("gate"), task("step_02", depends_on=["gate"])])
    api.update_step(run_id, "gate", status="running")

    assert api.resolve(run_id, "gate", "rejected", note="wrong dataset").status_code == 200
    assert api.step_status(run_id, "gate") == "failed"
    assert api.step_status(run_id, "step_02") == "skipped"
    assert api.run_status(run_id) == "failed"


def test_a_rejection_has_to_say_why(api: Api):
    """An approval needs no words; a "no" does. It is the only thing the harness has to
    work from when it decides what to propose instead."""
    _, run_id = api.run([checkpoint("gate")])
    api.update_step(run_id, "gate", status="running")

    response = api.resolve(run_id, "gate", "rejected")
    assert response.status_code == 422
    assert "needs a note saying why" in response.text
    assert api.step_status(run_id, "gate") == "blocked"


def test_a_checkpoint_can_only_be_decided_once(api: Api):
    _, run_id = api.run([checkpoint("gate")])
    api.update_step(run_id, "gate", status="running")
    assert api.resolve(run_id, "gate").status_code == 200

    second = api.resolve(run_id, "gate")
    assert second.status_code == 409
    assert "already been decided" in second.text


def test_a_checkpoint_nobody_has_reached_cannot_be_decided(api: Api):
    _, run_id = api.run([task("step_01"), checkpoint("gate", depends_on=["step_01"])])
    response = api.resolve(run_id, "gate")
    assert response.status_code == 409
    assert "not 'blocked'" in response.text


def test_resolving_something_that_is_not_a_checkpoint_is_refused(api: Api):
    _, run_id = api.run([task("step_01")])
    assert api.resolve(run_id, "step_01").status_code == 422


# --- what the person typed ---------------------------------------------------------------


def test_the_answers_come_back_to_the_harness_by_name(api: Api):
    _, run_id = api.run([checkpoint("gate", fields=[{"name": "budget", "label": "How much?"}])])
    api.update_step(run_id, "gate", status="running")
    assert api.resolve(run_id, "gate", response={"budget": "$400"}).status_code == 200

    assert api.get_run(run_id)["step_states"]["gate"]["checkpoint"]["response"] == {
        "budget": "$400"
    }


def test_a_required_field_left_empty_is_refused(api: Api):
    _, run_id = api.run([checkpoint("gate", fields=[{"name": "budget"}])])
    api.update_step(run_id, "gate", status="running")

    blank = api.resolve(run_id, "gate", response={"budget": "   "})
    assert blank.status_code == 422
    assert "requires ['budget']" in blank.text
    assert api.resolve(run_id, "gate").status_code == 422


def test_an_optional_field_may_be_left_out(api: Api):
    _, run_id = api.run([checkpoint("gate", fields=[{"name": "note", "required": False}])])
    api.update_step(run_id, "gate", status="running")
    assert api.resolve(run_id, "gate").status_code == 200


def test_an_answer_to_a_question_nobody_asked_is_refused(api: Api):
    """A typo'd key would otherwise be stored happily and surface as a missing answer much
    later, at the point the harness reads it back."""
    _, run_id = api.run([checkpoint("gate", fields=[{"name": "budget"}])])
    api.update_step(run_id, "gate", status="running")

    response = api.resolve(run_id, "gate", response={"budget": "$400", "buget": "typo"})
    assert response.status_code == 422
    assert "did not ask for ['buget']" in response.text


# --- inside a construct ------------------------------------------------------------------


def test_a_checkpoint_inside_a_loop_surfaces_on_the_run(api: Api):
    """The construct derives `running` either way, so a run that scanned only its top-level
    steps would sit silently waiting with nothing on the surface saying so."""
    _, run_id = api.run([construct("loop_01", "loop", ["gate"]), checkpoint("gate")])
    api.add_instance(run_id, "loop_01", instance_id="inst_00")
    api.nested_update(run_id, "loop_01/inst_00/gate", status="running")

    assert api.run_status(run_id) == "waiting_on_human"

    assert api.nested_resolve(run_id, "loop_01/inst_00/gate").status_code == 200
    assert api.run_status(run_id) == "running"


# --- the record ---------------------------------------------------------------------------


def test_a_decision_records_which_transport_it_arrived_on(api: Api):
    """So a decision an agent relayed stays distinguishable from one made by hand (REQ-43)."""
    _, run_id = api.run([checkpoint("gate")])
    api.update_step(run_id, "gate", status="running")
    api.resolve(run_id, "gate", decided_by="roy")

    outcome = api.get_run(run_id)["step_states"]["gate"]["checkpoint"]
    assert outcome["via"] == "rest"
    assert outcome["decided_by"] == "roy"


def test_the_decision_is_audited(api: Api, client):
    _, run_id = api.run([checkpoint("gate", fields=[{"name": "budget"}])])
    api.update_step(run_id, "gate", status="running")
    api.resolve(run_id, "gate", "rejected", note="too costly", response={"budget": "$0"})

    entries = client.get("/v1/audit", params={"run_id": run_id}).json()
    resolved = [e for e in entries if e["event"] == "checkpoint.resolved"]
    assert len(resolved) == 1
    assert resolved[0]["detail"]["decision"] == "rejected"
    assert resolved[0]["detail"]["note"] == "too costly"
    # The answers themselves stay on the run; the log records only which were given.
    assert resolved[0]["detail"]["fields"] == ["budget"]


# --- saying no, and asking again ----------------------------------------------------------


def test_a_rejection_does_not_have_to_answer_the_questions(api: Api):
    """Saying no is the case where you do not have the answers — the person rejecting
    "which maintenance window?" is rejecting because there isn't one. Demanding them would
    make the cheapest way past a checkpoint be to type anything into it."""
    _, run_id = api.run([checkpoint("gate", fields=[{"name": "window"}])])
    api.update_step(run_id, "gate", status="running")

    response = api.resolve(run_id, "gate", "rejected", note="not until the audit lands")
    assert response.status_code == 200, response.text
    assert api.step_status(run_id, "gate") == "failed"


def test_a_typo_is_still_refused_when_rejecting(api: Api):
    """A withheld answer and a misspelled one are different mistakes."""
    _, run_id = api.run([checkpoint("gate", fields=[{"name": "window"}])])
    api.update_step(run_id, "gate", status="running")

    response = api.resolve(run_id, "gate", "rejected", note="no", response={"windwo": "x"})
    assert response.status_code == 422
    assert "did not ask for ['windwo']" in response.text


def test_replaying_a_checkpoint_asks_the_question_again(api: Api):
    """The old answer goes to history rather than staying on a step that is pending again —
    otherwise the step reads as decided while it is waiting to be decided."""
    _, run_id = api.run([checkpoint("gate", fields=[{"name": "window"}])])
    api.update_step(run_id, "gate", status="running")
    api.resolve(run_id, "gate", response={"window": "Sat 02:00"})

    amendment = api.propose(
        run_id,
        [{"op": "replay_step", "target_step_id": "gate"}],
        kind="history_edit",
        reason="the window moved",
    ).json()
    assert api.approve(amendment["amendment_id"]).status_code == 200

    state = api.get_run(run_id)["step_states"]["gate"]
    assert state["status"] == "pending"
    assert state["checkpoint"] is None
    # Nothing was lost: the decision is in the snapshot the replay preserved (REQ-42).
    assert state["history"][0]["checkpoint"]["response"] == {"window": "Sat 02:00"}

    # And it can be reached and decided again.
    api.update_step(run_id, "gate", status="running")
    assert api.step_status(run_id, "gate") == "blocked"
    assert api.resolve(run_id, "gate", response={"window": "Sun 03:00"}).status_code == 200


# --- through a template --------------------------------------------------------------------


def test_a_checkpoint_survives_the_template_round_trip(api: Api, client):
    """Generalising a plan and instantiating it again must not quietly drop what a
    checkpoint asks for — a gate that stops asking is a gate that stops being one."""
    workflow_id = api.approved_workflow(
        [
            task("step_01"),
            checkpoint(
                "gate",
                depends_on=["step_01"],
                goal="Deploy acme/api to production?",
                fields=[{"name": "window", "label": "Which window?", "required": True}],
            ),
        ]
    )
    template = client.post(
        f"/v1/workflows/{workflow_id}/template",
        json={"title": "Deploy {{ repo }}", "substitutions": {"acme/api": "repo"}},
    )
    assert template.status_code == 201, template.text
    template_id = template.json()["template_id"]

    made = client.post(
        f"/v1/templates/{template_id}/workflows", json={"parameters": {"repo": "acme/web"}}
    )
    assert made.status_code == 201, made.text
    gate = next(s for s in made.json()["steps"] if s["id"] == "gate")
    assert gate["type"] == "checkpoint"
    assert gate["harness"] == "human"
    # The goal is substituted like any other text; the fields come through as declared.
    assert gate["goal"] == "Deploy acme/web to production?"
    assert gate["fields"] == [
        {"name": "window", "label": "Which window?", "hint": None, "required": True}
    ]
