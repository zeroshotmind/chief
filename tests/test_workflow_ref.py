"""workflow_ref: a step whose body is another whole workflow (extension).

Same shape as a checkpoint — the run reaches it, reports 'running', and stops — except the
external actor that unblocks it is a child run finishing, not a person deciding.
"""

from __future__ import annotations

from .conftest import Api, checkpoint, construct, task, workflow_ref

# --- shape -------------------------------------------------------------------------------


def test_a_workflow_ref_needs_a_template(api: Api):
    step = workflow_ref("sub", ref_template_id="")
    response = api.create_workflow([step])
    assert response.status_code == 422
    assert "needs a ref_template_id" in response.text


def test_only_a_workflow_ref_may_name_a_template(api: Api):
    step = task("step_01")
    step["ref_template_id"] = "tmpl_x"
    response = api.create_workflow([step])
    assert response.status_code == 422
    assert "apply only to workflow_ref steps" in response.text


# --- reaching one --------------------------------------------------------------------------


def test_reporting_a_workflow_ref_running_spawns_a_child_run_and_blocks(api: Api):
    template_id = api.create_template([task("only_step")])
    _, run_id = api.run([workflow_ref("sub", ref_template_id=template_id)])

    response = api.update_step(run_id, "sub", status="running")
    assert response.status_code == 200, response.text
    assert api.step_status(run_id, "sub") == "blocked"
    assert api.run_status(run_id) == "waiting_on_human"

    state = api.get_run(run_id)["step_states"]["sub"]
    assert state["child_run_id"]


def test_a_harness_cannot_report_how_a_workflow_ref_turned_out(api: Api):
    template_id = api.create_template([task("only_step")])
    _, run_id = api.run([workflow_ref("sub", ref_template_id=template_id)])
    response = api.update_step(run_id, "sub", status="completed")
    assert response.status_code == 409
    assert "cascaded" in response.text
    assert api.step_status(run_id, "sub") == "pending"


# --- the child run reports back -------------------------------------------------------------


def test_completing_the_child_run_completes_the_parent_step(api: Api):
    template_id = api.create_template([task("only_step")])
    _, run_id = api.run(
        [workflow_ref("sub", ref_template_id=template_id), task("after", depends_on=["sub"])]
    )
    api.update_step(run_id, "sub", status="running")
    child_run_id = api.get_run(run_id)["step_states"]["sub"]["child_run_id"]

    api.update_step(child_run_id, "only_step", status="completed")

    assert api.step_status(run_id, "sub") == "completed"
    assert api.run_status(run_id) == "running"


def test_failing_the_child_run_fails_the_parent_step_and_skips_what_came_after(api: Api):
    template_id = api.create_template([task("only_step")])
    _, run_id = api.run(
        [workflow_ref("sub", ref_template_id=template_id), task("after", depends_on=["sub"])]
    )
    api.update_step(run_id, "sub", status="running")
    child_run_id = api.get_run(run_id)["step_states"]["sub"]["child_run_id"]

    api.update_step(child_run_id, "only_step", status="failed")

    assert api.step_status(run_id, "sub") == "failed"
    assert api.step_status(run_id, "after") == "skipped"
    assert api.run_status(run_id) == "failed"


def test_a_checkpoint_finishing_a_child_run_also_cascades(api: Api):
    """The cascade point in resolve_checkpoint matters when the child run's own last step is
    itself a checkpoint, not just an ordinary task."""
    template_id = api.create_template([checkpoint("gate")])
    _, run_id = api.run([workflow_ref("sub", ref_template_id=template_id)])
    api.update_step(run_id, "sub", status="running")
    child_run_id = api.get_run(run_id)["step_states"]["sub"]["child_run_id"]

    api.update_step(child_run_id, "gate", status="running")
    api.resolve(child_run_id, "gate")

    assert api.step_status(run_id, "sub") == "completed"


# --- nested inside a construct ---------------------------------------------------------------


def test_a_workflow_ref_inside_a_loop_surfaces_and_cascades(api: Api):
    template_id = api.create_template([task("only_step")])
    _, run_id = api.run([construct("loop_01", "loop", ["sub"]), workflow_ref("sub", ref_template_id=template_id)])
    api.add_instance(run_id, "loop_01", instance_id="inst_00")
    api.nested_update(run_id, "loop_01/inst_00/sub", status="running")

    assert api.run_status(run_id) == "waiting_on_human"
    child_run_id = api.get_run(run_id)["step_states"]["loop_01"]["instances"][0]["step_states"][
        "sub"
    ]["child_run_id"]

    api.update_step(child_run_id, "only_step", status="completed")
    assert api.run_status(run_id) == "running"


# --- replay -----------------------------------------------------------------------------------


def test_replaying_a_workflow_ref_drops_the_stale_child_link(api: Api):
    template_id = api.create_template([task("only_step")])
    _, run_id = api.run([workflow_ref("sub", ref_template_id=template_id)])
    api.update_step(run_id, "sub", status="running")
    first_child = api.get_run(run_id)["step_states"]["sub"]["child_run_id"]
    api.update_step(first_child, "only_step", status="completed")
    assert api.step_status(run_id, "sub") == "completed"

    amendment = api.propose(
        run_id,
        [{"op": "replay_step", "target_step_id": "sub"}],
        kind="history_edit",
        reason="the template changed",
    ).json()
    assert api.approve(amendment["amendment_id"]).status_code == 200

    state = api.get_run(run_id)["step_states"]["sub"]
    assert state["status"] == "pending"
    assert state["child_run_id"] is None

    api.update_step(run_id, "sub", status="running")
    second_child = api.get_run(run_id)["step_states"]["sub"]["child_run_id"]
    assert second_child != first_child
