"""Review notes — feedback on a draft, for whoever revises it.

The mirror of artifact comments (test_artifact_comments.py): a comment is said about work
that is done, a note about work that has not started. The whole point is that a note
survives the revision it asked for, which is why it is not stored on the step. See
CONTRACT-NOTES.md #31.
"""

from __future__ import annotations

from .conftest import task

# --- leaving one ----------------------------------------------------------------------------


def test_a_note_lands_on_the_step_it_names(api):
    workflow_id = api.draft([task("s1"), task("s2")])
    response = api.note(workflow_id, "this should be a loop", step_id="s1", author="roy")
    assert response.status_code == 201, response.text

    note = response.json()
    assert note["note_id"].startswith("rvw_")
    assert note["step_id"] == "s1"
    assert note["body"] == "this should be a loop"
    assert note["author"] == "roy"
    assert note["resolved"] is False
    assert note["orphaned"] is False


def test_a_note_can_be_about_the_plan_rather_than_a_step(api):
    """Some feedback belongs to no node — 'this is a chain and it should fan out'."""
    workflow_id = api.draft([task("s1")])
    note = api.note(workflow_id, "wrong shape for the work").json()
    assert note["step_id"] is None
    assert note["step_goal"] is None
    assert note["orphaned"] is False


def test_a_note_keeps_the_goal_as_it_read_when_it_was_written(api):
    """So an orphan still says what it was about, rather than naming an id and nothing else."""
    workflow_id = api.draft([task("s1")])
    note = api.note(workflow_id, "too broad", step_id="s1").json()
    assert note["step_goal"] == "do s1"


def test_a_note_on_a_step_that_is_not_there_is_refused(api):
    workflow_id = api.draft([task("s1")])
    response = api.note(workflow_id, "hm", step_id="s9")
    assert response.status_code == 422, response.text
    assert "s9" in response.json()["error"]["message"]


def test_a_note_needs_something_in_it(api):
    workflow_id = api.draft([task("s1")])
    assert api.note(workflow_id, "").status_code == 422


def test_notes_come_back_in_the_order_they_were_left(api):
    workflow_id = api.draft([task("s1")])
    for body in ("first", "second", "third"):
        assert api.note(workflow_id, body).status_code == 201
    assert [n["body"] for n in api.notes(workflow_id)] == ["first", "second", "third"]


def test_notes_on_a_workflow_that_is_not_there_are_a_404(api):
    assert api.note("wf_nope").status_code == 404


# --- how the harness reads them -------------------------------------------------------------


def test_the_notes_ride_on_the_workflow_the_harness_already_fetches(api):
    """No second call, and no tool: the same arrangement artifact comments have."""
    workflow_id = api.draft([task("s1")])
    api.note(workflow_id, "narrow this", step_id="s1")

    workflow = api.client.get(f"/v1/workflows/{workflow_id}").json()
    assert [n["body"] for n in workflow["review_notes"]] == ["narrow this"]


def test_a_harness_cannot_smuggle_notes_in_with_a_plan(api):
    """Unlike ArtifactRef, the submitted shape simply does not declare the field."""
    response = api.create_workflow([task("s1")], review_notes=[])
    assert response.status_code == 422


def test_a_revision_cannot_wipe_the_notes_that_asked_for_it(api):
    """The reason notes are not stored on the workflow document.

    ``revise_draft`` replaces title and steps wholesale. A note kept inside that document
    would be destroyed by the very revision it prompted, and the reviewer would have nothing
    to check the new plan against.
    """
    workflow_id = api.draft([task("s1")])
    api.note(workflow_id, "split this in two", step_id="s1")

    assert api.revise(workflow_id, [task("s1"), task("s2")]).status_code == 200

    workflow = api.client.get(f"/v1/workflows/{workflow_id}").json()
    assert [n["body"] for n in workflow["review_notes"]] == ["split this in two"]


def test_the_workflow_list_leaves_the_notes_off(api):
    """It is a list of plans, not of feedback — and a badge nobody asked for is a query per
    row on a screen that is read constantly."""
    workflow_id = api.draft([task("s1")])
    api.note(workflow_id, "hm")
    listed = next(
        w for w in api.client.get("/v1/workflows").json() if w["workflow_id"] == workflow_id
    )
    assert listed["review_notes"] == []


# --- what happens when the plan moves underneath ---------------------------------------------


def test_a_note_survives_its_step_being_rewritten(api):
    """The id is the identity. A rewritten goal is still the same node of the plan."""
    workflow_id = api.draft([task("s1")])
    api.note(workflow_id, "too broad", step_id="s1")

    revised = task("s1")
    revised["goal"] = "do s1, but only the migration"
    assert api.revise(workflow_id, [revised]).status_code == 200

    (note,) = api.notes(workflow_id)
    assert note["step_id"] == "s1"
    assert note["orphaned"] is False
    # Still the goal as it read when the note was written, not the one that replaced it.
    assert note["step_goal"] == "do s1"


def test_a_note_whose_step_is_gone_is_orphaned_not_dropped(api):
    """The step vanishing may mean the feedback was acted on, or may mean the harness
    restructured around it. Telling those apart is the reviewer's job, so the note stays
    open and says which step it lost."""
    workflow_id = api.draft([task("s1"), task("s2")])
    api.note(workflow_id, "this step is the problem", step_id="s2")

    assert api.revise(workflow_id, [task("s1"), task("s3")]).status_code == 200

    (note,) = api.notes(workflow_id)
    assert note["orphaned"] is True
    assert note["resolved"] is False
    assert note["step_id"] == "s2"
    assert note["step_goal"] == "do s2"


def test_a_revision_that_orphans_a_note_is_not_refused(api):
    """Chief records; it does not enforce. Blocking the revision would make the feedback a
    veto over the plan rather than a comment on it."""
    workflow_id = api.draft([task("s1")])
    api.note(workflow_id, "drop this entirely", step_id="s1")
    assert api.revise(workflow_id, [task("s2")]).status_code == 200


def test_a_step_id_that_comes_back_un_orphans_its_note(api):
    """Orphaning is derived from the plan as it stands, never stored — so a note cannot be
    left permanently marked lost by a revision that was itself revised."""
    workflow_id = api.draft([task("s1")])
    api.note(workflow_id, "keep an eye on this", step_id="s1")
    api.revise(workflow_id, [task("s2")])
    assert api.notes(workflow_id)[0]["orphaned"] is True

    api.revise(workflow_id, [task("s1"), task("s2")])
    assert api.notes(workflow_id)[0]["orphaned"] is False


# --- resolving ------------------------------------------------------------------------------


def test_resolving_a_note_records_who_closed_it(api):
    workflow_id = api.draft([task("s1")])
    note_id = api.note(workflow_id, "narrow this", step_id="s1").json()["note_id"]

    response = api.decide_note(workflow_id, note_id, resolved=True, resolved_by="roy")
    assert response.status_code == 200, response.text

    note = response.json()
    assert note["resolved"] is True
    assert note["resolved_by"] == "roy"
    assert note["resolved_at"]


def test_a_resolved_note_can_be_put_back(api):
    workflow_id = api.draft([task("s1")])
    note_id = api.note(workflow_id).json()["note_id"]
    api.decide_note(workflow_id, note_id, resolved=True)

    note = api.decide_note(workflow_id, note_id, resolved=False).json()
    assert note["resolved"] is False
    assert note["resolved_at"] is None
    assert note["resolved_by"] is None


def test_resolving_a_note_twice_is_refused(api):
    workflow_id = api.draft([task("s1")])
    note_id = api.note(workflow_id).json()["note_id"]
    api.decide_note(workflow_id, note_id, resolved=True)
    assert api.decide_note(workflow_id, note_id, resolved=True).status_code == 409


def test_open_and_resolved_can_be_asked_for_separately(api):
    workflow_id = api.draft([task("s1")])
    done = api.note(workflow_id, "handled").json()["note_id"]
    api.note(workflow_id, "still open")
    api.decide_note(workflow_id, done, resolved=True)

    assert [n["body"] for n in api.notes(workflow_id, resolved=False)] == ["still open"]
    assert [n["body"] for n in api.notes(workflow_id, resolved=True)] == ["handled"]
    assert len(api.notes(workflow_id)) == 2


def test_deciding_a_note_that_is_not_there_is_a_404(api):
    workflow_id = api.draft([task("s1")])
    assert api.decide_note(workflow_id, "rvw_nope", resolved=True).status_code == 404


def test_a_note_belongs_to_its_workflow(api):
    """Ids are unguessable but not scoped, so the route checks rather than trusting."""
    mine = api.draft([task("s1")])
    theirs = api.draft([task("s1")])
    note_id = api.note(mine).json()["note_id"]
    assert api.decide_note(theirs, note_id, resolved=True).status_code == 404


# --- not only drafts ------------------------------------------------------------------------


def test_an_approved_plan_can_still_be_commented_on(api):
    """Notes are not gated on ``draft``. A plan is worth saying something about after it is
    approved too, and a rule that stopped you would only send the remark somewhere Chief
    cannot see. Nothing about a note changes the plan, so nothing needs protecting.
    """
    workflow_id = api.approved_workflow([task("s1")])
    assert api.note(workflow_id, "worked well, template this", step_id="s1").status_code == 201


def test_approving_a_draft_with_open_notes_is_allowed(api):
    """Chief records; it does not enforce. Deciding the feedback no longer matters is a
    decision a person is allowed to make, and the note stays in the record either way."""
    workflow_id = api.draft([task("s1")])
    api.note(workflow_id, "unaddressed", step_id="s1")
    assert api.client.post(f"/v1/workflows/{workflow_id}/approve").status_code == 200
    assert api.notes(workflow_id)[0]["resolved"] is False


# --- the record -----------------------------------------------------------------------------


def test_leaving_and_closing_a_note_are_both_audited(api):
    workflow_id = api.draft([task("s1")])
    note_id = api.note(workflow_id, "narrow this", step_id="s1", author="roy").json()["note_id"]
    api.decide_note(workflow_id, note_id, resolved=True, resolved_by="roy")

    entries = api.client.get("/v1/audit", params={"workflow_id": workflow_id}).json()
    events = {e["event"]: e for e in entries}

    added = events["workflow.note_added"]
    assert added["detail"]["note_id"] == note_id
    assert added["detail"]["step_id"] == "s1"
    assert added["detail"]["author"] == "roy"
    assert added["detail"]["via"] == "rest"

    assert events["workflow.note_resolved"]["detail"]["decided_by"] == "roy"


def test_a_note_records_the_transport_it_arrived_on(api):
    workflow_id = api.draft([task("s1")])
    assert api.note(workflow_id).json()["via"] == "rest"


def test_the_author_defaults_to_human(api):
    workflow_id = api.draft([task("s1")])
    assert api.note(workflow_id).json()["author"] == "human"
