"""Ad-hoc questions: a harness asking a person something mid-step (extension).

A `checkpoint` step is a person-decision the plan names in advance, at a step whose only job
is to ask it. This is the other direction: an ordinary step, partway through execution,
finding it needs something only a person knows — not declared anywhere in the plan. Unlike a
checkpoint the step does not end when it is answered; it goes back to `running` and the
harness is expected to keep working and report its own terminal status.
"""

from __future__ import annotations

from .conftest import Api, construct, task

# --- asking --------------------------------------------------------------------------------


def test_asking_from_a_running_step_blocks_it(api: Api):
    _, run_id = api.run([task("step_01")])
    api.update_step(run_id, "step_01", status="running")

    response = api.ask(run_id, "step_01", text="which environment — staging or prod?")
    assert response.status_code == 201, response.text
    assert api.step_status(run_id, "step_01") == "blocked"
    assert api.run_status(run_id) == "waiting_on_human"

    question = response.json()["step_states"]["step_01"]["questions"][0]
    assert question["text"] == "which environment — staging or prod?"
    assert question["response"] is None


def test_a_question_cannot_be_asked_before_the_step_is_running(api: Api):
    _, run_id = api.run([task("step_01")])
    response = api.ask(run_id, "step_01")
    assert response.status_code == 409
    assert "not 'running'" in response.text


def test_a_question_cannot_be_asked_on_a_construct(api: Api):
    """A loop/parallel's status is derived from its instances on every recompute — blocking
    it directly would be silently overwritten back to 'running' on the very call that set
    it, leaving an unanswerable question attached to a step that reads as unblocked."""
    _, run_id = api.run([construct("loop_01", "loop", ["step_01"]), task("step_01")])
    api.add_instance(run_id, "loop_01", instance_id="inst_00")
    assert api.step_status(run_id, "loop_01") == "running"

    response = api.ask(run_id, "loop_01")
    assert response.status_code == 409
    assert "its status is derived from its instances" in response.text


def test_only_one_question_may_be_open_at_a_time(api: Api):
    _, run_id = api.run([task("step_01")])
    api.update_step(run_id, "step_01", status="running")
    api.ask(run_id, "step_01")

    second = api.ask(run_id, "step_01", text="a second thing")
    assert second.status_code == 409
    assert "already has an unanswered question" in second.text


def test_asking_again_after_the_first_is_answered_is_fine(api: Api):
    _, run_id = api.run([task("step_01")])
    api.update_step(run_id, "step_01", status="running")
    first = api.ask(run_id, "step_01").json()["step_states"]["step_01"]["questions"][0]
    api.answer(run_id, "step_01", first["question_id"])

    second = api.ask(run_id, "step_01", text="one more thing")
    assert second.status_code == 201, second.text
    assert api.step_status(run_id, "step_01") == "blocked"


# --- answering -----------------------------------------------------------------------------


def test_answering_unblocks_the_step_back_to_running(api: Api):
    _, run_id = api.run([task("step_01")])
    api.update_step(run_id, "step_01", status="running")
    question = api.ask(run_id, "step_01").json()["step_states"]["step_01"]["questions"][0]

    response = api.answer(run_id, "step_01", question["question_id"])
    assert response.status_code == 200, response.text
    assert api.step_status(run_id, "step_01") == "running"
    assert api.run_status(run_id) == "running"

    answered = api.get_run(run_id)["step_states"]["step_01"]["questions"][0]
    assert answered["response"] == {"text": "go with the smaller one"}
    assert answered["answered_by"] == "human"
    assert answered["answered_at"]


def test_a_free_text_answer_must_be_exactly_the_text_key(api: Api):
    _, run_id = api.run([task("step_01")])
    api.update_step(run_id, "step_01", status="running")
    question = api.ask(run_id, "step_01").json()["step_states"]["step_01"]["questions"][0]

    response = api.answer(run_id, "step_01", question["question_id"], response={"answer": "x"})
    assert response.status_code == 422
    assert "free text" in response.text


def test_answering_with_the_wrong_question_id_is_refused(api: Api):
    _, run_id = api.run([task("step_01")])
    api.update_step(run_id, "step_01", status="running")
    open_question = api.ask(run_id, "step_01").json()["step_states"]["step_01"]["questions"][0]

    response = api.answer(run_id, "step_01", "qn_bogus")
    assert response.status_code == 422
    assert open_question["question_id"] in response.text
    assert "not 'qn_bogus'" in response.text


def test_a_step_with_no_open_question_cannot_be_answered(api: Api):
    _, run_id = api.run([task("step_01")])
    api.update_step(run_id, "step_01", status="running")

    response = api.answer(run_id, "step_01", "qn_nope")
    assert response.status_code == 409
    assert "no unanswered question" in response.text


def test_declared_fields_are_validated_like_a_checkpoints(api: Api):
    _, run_id = api.run([task("step_01")])
    api.update_step(run_id, "step_01", status="running")
    question = api.ask(
        run_id, "step_01", text="what budget?", fields=[{"name": "budget"}]
    ).json()["step_states"]["step_01"]["questions"][0]

    missing = api.answer(run_id, "step_01", question["question_id"], response={})
    assert missing.status_code == 422
    assert "requires ['budget']" in missing.text

    typo = api.answer(
        run_id, "step_01", question["question_id"], response={"buget": "$400"}
    )
    assert typo.status_code == 422
    assert "did not ask for ['buget']" in typo.text

    ok = api.answer(run_id, "step_01", question["question_id"], response={"budget": "$400"})
    assert ok.status_code == 200, ok.text


# --- the harness keeps going ---------------------------------------------------------------


def test_the_step_can_be_completed_after_being_answered(api: Api):
    """The point of the whole mechanism: asking does not end the step."""
    _, run_id = api.run([task("step_01")])
    api.update_step(run_id, "step_01", status="running")
    question = api.ask(run_id, "step_01").json()["step_states"]["step_01"]["questions"][0]
    api.answer(run_id, "step_01", question["question_id"])

    response = api.update_step(run_id, "step_01", status="completed")
    assert response.status_code == 200, response.text
    assert api.step_status(run_id, "step_01") == "completed"


# --- inside a construct ---------------------------------------------------------------------


def test_a_question_inside_a_loop_surfaces_on_the_run(api: Api):
    _, run_id = api.run([construct("loop_01", "loop", ["step_01"]), task("step_01")])
    api.add_instance(run_id, "loop_01", instance_id="inst_00")
    api.nested_update(run_id, "loop_01/inst_00/step_01", status="running")

    asked = api.nested_ask(run_id, "loop_01/inst_00/step_01")
    assert asked.status_code == 201, asked.text
    assert api.run_status(run_id) == "waiting_on_human"

    question = asked.json()["step_states"]["loop_01"]["instances"][0]["step_states"]["step_01"][
        "questions"
    ][0]
    answered = api.nested_answer(run_id, "loop_01/inst_00/step_01", question["question_id"])
    assert answered.status_code == 200, answered.text
    assert api.run_status(run_id) == "running"


# --- replay ----------------------------------------------------------------------------------


def test_replaying_a_step_clears_its_unanswered_question(api: Api):
    _, run_id = api.run([task("step_01")])
    api.update_step(run_id, "step_01", status="completed")

    amendment = api.propose(
        run_id,
        [{"op": "replay_step", "target_step_id": "step_01"}],
        kind="history_edit",
        reason="need to redo it",
    ).json()
    assert api.approve(amendment["amendment_id"]).status_code == 200

    api.update_step(run_id, "step_01", status="running")
    asked = api.ask(run_id, "step_01")
    assert asked.status_code == 201, asked.text
    assert api.step_status(run_id, "step_01") == "blocked"


# --- the record ------------------------------------------------------------------------------


def test_asking_and_answering_are_audited(api: Api, client):
    _, run_id = api.run([task("step_01")])
    api.update_step(run_id, "step_01", status="running")
    question = api.ask(run_id, "step_01", text="which one?").json()["step_states"]["step_01"][
        "questions"
    ][0]
    api.answer(run_id, "step_01", question["question_id"])

    entries = client.get("/v1/audit", params={"run_id": run_id}).json()
    asked = [e for e in entries if e["event"] == "step.question_asked"]
    answered = [e for e in entries if e["event"] == "step.question_answered"]
    assert len(asked) == 1 and asked[0]["detail"]["text"] == "which one?"
    assert len(answered) == 1 and answered[0]["detail"]["question_id"] == question["question_id"]
