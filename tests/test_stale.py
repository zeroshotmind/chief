"""Stale marks: a step or branch labelled not usable for the final result (extension).

`skipped` says a step did not run; a stale mark says the opposite — it ran, possibly to
completion, and simply is not part of what counts. A parallel construct's branches are the
main case: several ran concurrently, one was chosen, and the others should read as set aside
without anything about their recorded result changing.
"""

from __future__ import annotations

from .conftest import Api, construct, task

# --- marking a step ------------------------------------------------------------------------


def test_marking_a_step_stale_is_visible_on_the_run(api: Api):
    _, run_id = api.run([task("step_01")])
    response = api.mark_stale(run_id, "step_01", reason="wrong dataset")
    assert response.status_code == 200, response.text

    stale = api.get_run(run_id)["step_states"]["step_01"]["stale"]
    assert stale["reason"] == "wrong dataset"
    assert stale["marked_by"] == "human"
    assert stale["marked_at"]


def test_a_completed_step_can_be_marked_stale(api: Api):
    """The primary case: the step finished fine, it just is not the one being used."""
    _, run_id = api.run([task("step_01")])
    api.update_step(run_id, "step_01", status="completed")

    response = api.mark_stale(run_id, "step_01")
    assert response.status_code == 200, response.text
    assert api.step_status(run_id, "step_01") == "completed"


def test_marking_stale_does_not_touch_status_or_run_status(api: Api):
    _, run_id = api.run([task("step_01"), task("step_02", depends_on=["step_01"])])
    api.mark_stale(run_id, "step_01")

    assert api.step_status(run_id, "step_01") == "pending"
    assert api.run_status(run_id) == "running"


def test_a_blank_reason_clears_rather_than_marks(api: Api):
    """Same rule `label_workflow` uses for clearing a project: whitespace is not a reason,
    so it reads as 'nothing to say' rather than as an error to reject."""
    _, run_id = api.run([task("step_01")])
    response = api.mark_stale(run_id, "step_01", reason="   ")
    stale = api.get_run(run_id)["step_states"].get("step_01", {}).get("stale")
    assert response.status_code == 200, response.text
    assert stale is None


def test_clearing_a_stale_mark(api: Api):
    _, run_id = api.run([task("step_01")])
    api.mark_stale(run_id, "step_01", reason="wrong dataset")
    assert api.get_run(run_id)["step_states"]["step_01"]["stale"] is not None

    response = api.mark_stale(run_id, "step_01", reason=None)
    assert response.status_code == 200, response.text
    assert api.get_run(run_id)["step_states"]["step_01"]["stale"] is None


def test_re_marking_stale_replaces_the_reason(api: Api):
    _, run_id = api.run([task("step_01")])
    api.mark_stale(run_id, "step_01", reason="first reason")
    api.mark_stale(run_id, "step_01", reason="actually, this one")

    assert api.get_run(run_id)["step_states"]["step_01"]["stale"]["reason"] == "actually, this one"


# --- marking an instance ---------------------------------------------------------------------


def test_marking_a_parallel_branch_stale(api: Api):
    _, run_id = api.run([construct("par_01", "parallel", ["step_01"]), task("step_01")])
    api.add_instance(run_id, "par_01", instance_id="p0")
    api.add_instance(run_id, "par_01", instance_id="p1")

    response = api.mark_instance_stale(run_id, "par_01", "p0", reason="p1's approach won")
    assert response.status_code == 200, response.text

    run = api.get_run(run_id)
    instances = {i["instance_id"]: i for i in run["step_states"]["par_01"]["instances"]}
    assert instances["p0"]["stale"]["reason"] == "p1's approach won"
    assert instances["p1"]["stale"] is None


def test_marking_a_completed_instance_stale_does_not_change_its_result(api: Api):
    _, run_id = api.run([construct("par_01", "parallel", ["step_01"]), task("step_01")])
    api.add_instance(run_id, "par_01", instance_id="p0")
    api.update_instance(run_id, "par_01", "p0", status="completed", summary="worked fine")

    response = api.mark_instance_stale(run_id, "par_01", "p0")
    assert response.status_code == 200, response.text

    instance = api.get_run(run_id)["step_states"]["par_01"]["instances"][0]
    assert instance["status"] == "completed"
    assert instance["summary"] == "worked fine"
    assert instance["stale"] is not None


def test_marking_stale_on_an_unknown_instance_is_a_404(api: Api):
    _, run_id = api.run([construct("par_01", "parallel", ["step_01"]), task("step_01")])
    response = api.mark_instance_stale(run_id, "par_01", "nope")
    assert response.status_code == 404


# --- nested addressing -----------------------------------------------------------------------


def test_marking_a_step_stale_inside_a_loop(api: Api):
    _, run_id = api.run([construct("loop_01", "loop", ["step_01"]), task("step_01")])
    api.add_instance(run_id, "loop_01", instance_id="inst_00")

    response = api.nested_mark_stale(run_id, "loop_01/inst_00/step_01", reason="bad attempt")
    assert response.status_code == 200, response.text

    state = api.get_run(run_id)["step_states"]["loop_01"]["instances"][0]["step_states"]["step_01"]
    assert state["stale"]["reason"] == "bad attempt"


def test_marking_an_instance_stale_by_nested_path(api: Api):
    _, run_id = api.run([construct("par_01", "parallel", ["step_01"]), task("step_01")])
    api.add_instance(run_id, "par_01", instance_id="p0")

    response = api.nested_mark_instance_stale(run_id, "par_01/p0", reason="superseded")
    assert response.status_code == 200, response.text

    instance = api.get_run(run_id)["step_states"]["par_01"]["instances"][0]
    assert instance["stale"]["reason"] == "superseded"


# --- replay --------------------------------------------------------------------------------


def test_replaying_a_step_clears_its_stale_mark(api: Api):
    _, run_id = api.run([task("step_01")])
    api.update_step(run_id, "step_01", status="completed")
    api.mark_stale(run_id, "step_01", reason="wrong approach")

    amendment = api.propose(
        run_id,
        [{"op": "replay_step", "target_step_id": "step_01"}],
        kind="history_edit",
        reason="try again",
    ).json()
    assert api.approve(amendment["amendment_id"]).status_code == 200

    state = api.get_run(run_id)["step_states"]["step_01"]
    assert state["status"] == "pending"
    assert state["stale"] is None
    assert state["history"][0]["stale"]["reason"] == "wrong approach"


# --- the record ------------------------------------------------------------------------------


def test_marking_and_clearing_are_audited(api: Api, client):
    _, run_id = api.run([task("step_01")])
    api.mark_stale(run_id, "step_01", reason="wrong dataset")
    api.mark_stale(run_id, "step_01", reason=None)

    entries = client.get("/v1/audit", params={"run_id": run_id}).json()
    marked = [e for e in entries if e["event"] == "stale.marked"]
    cleared = [e for e in entries if e["event"] == "stale.cleared"]
    assert len(marked) == 1 and marked[0]["detail"]["reason"] == "wrong dataset"
    assert len(cleared) == 1


def test_marking_stale_bumps_the_runs_updated_at(api: Api):
    _, run_id = api.run([task("step_01")])
    before = api.get_run(run_id)["updated_at"]
    api.mark_stale(run_id, "step_01")
    after = api.get_run(run_id)["updated_at"]
    assert after >= before
