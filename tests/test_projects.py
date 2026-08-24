"""Projects — which body of work a plan belongs to, and where it was made.

Two fields, doing different jobs. ``project`` is a label: stable, chosen, the thing the UI
groups by. ``origin_dir`` is provenance: where the harness stood, recorded because "which
checkout was this?" is a real question later, and deliberately not something the server
resolves anything against. See CONTRACT-NOTES.md #32.
"""

from __future__ import annotations

from .conftest import task

# --- stating it at creation -----------------------------------------------------------------


def test_a_plan_can_say_what_it_belongs_to_and_where_it_was_made(api):
    response = api.create_workflow([task("s1")], project="chief", origin_dir="/w/chief")
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["project"] == "chief"
    assert body["origin_dir"] == "/w/chief"


def test_both_are_optional(api):
    """Every workflow that predates this has neither, and must keep working untouched."""
    body = api.create_workflow([task("s1")]).json()
    assert body["project"] is None
    assert body["origin_dir"] is None


def test_the_label_is_an_open_namespace(api):
    """Like `harness` (REQ-26): adding a project is adding a value, not a schema change.

    Nothing validates it against a list, because there is no list — see
    `test_the_project_list_is_derived_from_the_workflows`.
    """
    body = api.create_workflow([task("s1")], project="something nobody declared").json()
    assert body["project"] == "something nobody declared"


def test_two_workflows_in_one_project_can_come_from_different_places(api):
    """The reason the two fields are separate. A project is not a directory: one product
    can span several checkouts, and one checkout can host work for more than one."""
    a = api.create_workflow([task("s1")], project="acme", origin_dir="/w/acme-api").json()
    b = api.create_workflow([task("s1")], project="acme", origin_dir="/w/acme-web").json()
    assert a["project"] == b["project"] == "acme"
    assert a["origin_dir"] != b["origin_dir"]


# --- filing one afterwards ------------------------------------------------------------------


def test_an_existing_workflow_can_be_filed_under_a_project(api):
    workflow_id = api.draft([task("s1")])
    response = api.label(workflow_id, "chief")
    assert response.status_code == 200, response.text
    assert response.json()["project"] == "chief"


def test_a_finished_workflow_can_be_filed_too(api):
    """The main use. Every workflow that ran before projects existed is unlabelled, and a
    rule that only drafts could be labelled would leave the whole history unfilable.

    Nothing about the plan changes, so there is nothing for the immutability rules to
    protect — this is filing, not revising.
    """
    workflow_id, _ = api.run([task("s1")])
    response = api.label(workflow_id, "chief")
    assert response.status_code == 200, response.text
    assert response.json()["project"] == "chief"
    assert response.json()["status"] == "approved"


def test_an_archived_workflow_can_be_filed(api):
    workflow_id = api.draft([task("s1")])
    api.client.post(f"/v1/workflows/{workflow_id}/archive")
    assert api.label(workflow_id, "chief").status_code == 200


def test_a_label_can_be_cleared(api):
    """A label put on the wrong workflow would otherwise be permanent."""
    workflow_id = api.draft([task("s1")], project="wrong")
    assert api.label(workflow_id, None).json()["project"] is None


def test_filing_does_not_touch_the_plan(api):
    workflow_id = api.draft([task("s1"), task("s2", depends_on=["s1"])])
    before = api.client.get(f"/v1/workflows/{workflow_id}").json()
    after = api.label(workflow_id, "chief").json()
    assert after["steps"] == before["steps"]
    assert after["version"] == before["version"]


def test_a_revision_cannot_rewrite_where_the_plan_was_made(api):
    """`origin_dir` is a record of where the harness stood. A revision from somewhere else
    overwriting it would make it a lie, so the revise body does not accept it at all."""
    workflow_id = api.draft([task("s1")], origin_dir="/w/chief")
    assert api.revise(workflow_id, [task("s1")], origin_dir="/w/elsewhere").status_code == 422
    assert api.client.get(f"/v1/workflows/{workflow_id}").json()["origin_dir"] == "/w/chief"


def test_a_revision_keeps_the_label(api):
    workflow_id = api.draft([task("s1")], project="chief")
    assert api.revise(workflow_id, [task("s1"), task("s2")]).status_code == 200
    assert api.client.get(f"/v1/workflows/{workflow_id}").json()["project"] == "chief"


def test_filing_a_workflow_that_is_not_there_is_a_404(api):
    assert api.label("wf_nope", "chief").status_code == 404


# --- the list of projects -------------------------------------------------------------------


def test_the_project_list_is_derived_from_the_workflows(api):
    """A project has no lifecycle: it is whatever labels are on the workflows. Nothing to
    create, rename or delete, and nothing that can disagree with what it claims."""
    api.create_workflow([task("s1")], project="chief")
    api.create_workflow([task("s1")], project="chief")
    api.create_workflow([task("s1")], project="songs")

    listed = api.client.get("/v1/projects").json()
    assert {p["project"]: p["workflows"] for p in listed} == {"chief": 2, "songs": 1}


def test_the_unlabelled_are_counted_rather_than_hidden(api):
    """On any database that predates this they are the majority. A list that left them out
    would be a list that quietly hid most of the history."""
    api.create_workflow([task("s1")], project="chief")
    api.create_workflow([task("s1")])
    api.create_workflow([task("s1")])

    listed = api.client.get("/v1/projects").json()
    assert {p["project"]: p["workflows"] for p in listed} == {"chief": 1, None: 2}


def test_the_unlabelled_sort_last(api):
    api.create_workflow([task("s1")])
    api.create_workflow([task("s1")], project="zebra")
    api.create_workflow([task("s1")], project="Apple")
    assert [p["project"] for p in api.client.get("/v1/projects").json()] == [
        "Apple", "zebra", None,
    ]


def test_a_label_that_stops_being_used_stops_being_listed(api):
    workflow_id = api.draft([task("s1")], project="typo")
    assert "typo" in [p["project"] for p in api.client.get("/v1/projects").json()]
    api.label(workflow_id, None)
    assert "typo" not in [p["project"] for p in api.client.get("/v1/projects").json()]


# --- templates carry it too -----------------------------------------------------------------


def test_a_template_can_belong_to_a_project(api):
    response = api.client.post("/v1/templates", json={
        "title": "nightly", "steps": [task("s1")], "project": "chief",
    })
    assert response.status_code == 201, response.text
    assert response.json()["project"] == "chief"


def test_a_workflow_inherits_its_template_s_project(api):
    """"This project's templates" is only worth asking for if what they make lands in the
    project too."""
    template_id = api.client.post("/v1/templates", json={
        "title": "nightly", "steps": [task("s1")], "project": "chief",
    }).json()["template_id"]

    made = api.client.post(f"/v1/templates/{template_id}/workflows", json={}).json()
    assert made["project"] == "chief"


def test_instantiating_can_override_the_project(api):
    """A shared template used on one project is the case that needs this."""
    template_id = api.client.post("/v1/templates", json={
        "title": "nightly", "steps": [task("s1")], "project": "chief",
    }).json()["template_id"]

    made = api.client.post(f"/v1/templates/{template_id}/workflows",
                           json={"project": "songs", "origin_dir": "/w/songs"}).json()
    assert made["project"] == "songs"
    assert made["origin_dir"] == "/w/songs"


def test_a_template_kept_from_a_workflow_keeps_its_project(api):
    workflow_id = api.draft([task("s1")], project="chief")
    template = api.client.post(f"/v1/workflows/{workflow_id}/template", json={}).json()
    assert template["project"] == "chief"


def test_a_template_can_be_generalised_away_from_its_project(api):
    """An empty string means "no project", as distinct from omitting the field, which means
    "keep the workflow's"."""
    workflow_id = api.draft([task("s1")], project="chief")
    template = api.client.post(f"/v1/workflows/{workflow_id}/template",
                               json={"project": ""}).json()
    assert template["project"] is None


# --- the record -----------------------------------------------------------------------------


def test_filing_is_audited_with_what_it_was_before(api):
    workflow_id = api.draft([task("s1")], project="wrong")
    api.label(workflow_id, "chief")
    entries = api.client.get("/v1/audit", params={"workflow_id": workflow_id}).json()
    (entry,) = [e for e in entries if e["event"] == "workflow.labelled"]
    assert entry["detail"]["project"] == "chief"
    assert entry["detail"]["project_was"] == "wrong"


# --- correcting where it ran ----------------------------------------------------------------


def test_the_directory_can_be_set_after_the_fact(api):
    """The only route by which a workflow planned before Chief asked for one can have one —
    and without it, those workflows can never show their files."""
    workflow_id = api.draft([task("s1")])
    response = api.client.patch(
        f"/v1/workflows/{workflow_id}", json={"origin_dir": "/w/chief"}
    )
    assert response.status_code == 200, response.text
    assert response.json()["origin_dir"] == "/w/chief"


def test_setting_one_field_leaves_the_other_alone(api):
    """`null` clears; omitting does not. They arrive identical without `model_fields_set`,
    and the bug would be silently erasing a directory while filing a project."""
    workflow_id = api.draft([task("s1")], project="chief", origin_dir="/w/chief")

    api.client.patch(f"/v1/workflows/{workflow_id}", json={"project": "songs"})
    after = api.client.get(f"/v1/workflows/{workflow_id}").json()
    assert after["project"] == "songs"
    assert after["origin_dir"] == "/w/chief"

    api.client.patch(f"/v1/workflows/{workflow_id}", json={"origin_dir": "/w/songs"})
    after = api.client.get(f"/v1/workflows/{workflow_id}").json()
    assert after["project"] == "songs"
    assert after["origin_dir"] == "/w/songs"


def test_an_explicit_null_still_clears(api):
    workflow_id = api.draft([task("s1")], project="chief", origin_dir="/w/chief")
    api.client.patch(f"/v1/workflows/{workflow_id}", json={"origin_dir": None})
    after = api.client.get(f"/v1/workflows/{workflow_id}").json()
    assert after["origin_dir"] is None
    assert after["project"] == "chief"


def test_an_empty_patch_is_refused_rather_than_silently_doing_nothing(api):
    workflow_id = api.draft([task("s1")])
    response = api.client.patch(f"/v1/workflows/{workflow_id}", json={})
    assert response.status_code == 422
    assert "nothing to change" in response.json()["error"]["message"]


def test_both_can_be_set_at_once(api):
    workflow_id = api.draft([task("s1")])
    response = api.client.patch(
        f"/v1/workflows/{workflow_id}", json={"project": "chief", "origin_dir": "/w/chief"}
    )
    assert response.json()["project"] == "chief"
    assert response.json()["origin_dir"] == "/w/chief"


def test_a_finished_workflow_can_have_its_directory_corrected(api):
    """The case this exists for: a run that already happened, whose files are still there."""
    workflow_id, _ = api.run([task("s1")])
    response = api.client.patch(
        f"/v1/workflows/{workflow_id}", json={"origin_dir": "/w/chief"}
    )
    assert response.status_code == 200, response.text


# --- renaming --------------------------------------------------------------------------------


def test_a_workflow_can_be_renamed_at_any_status(api):
    """The same PATCH as filing, for the same reason: a rename says nothing about the plan,
    and the workflows most in need of a better name are the ones already running. The runs
    follow, because the definition is the one document they all read."""
    workflow_id, _ = api.run([task("s1")])
    response = api.client.patch(
        f"/v1/workflows/{workflow_id}", json={"title": "A better name"}
    )
    assert response.status_code == 200, response.text
    assert response.json()["title"] == "A better name"
    assert response.json()["status"] == "approved"
    # The audit records what it was, so the rename is a correction, not an erasure.
    entries = api.client.get(f"/v1/audit?workflow_id={workflow_id}").json()
    labelled = [e for e in entries if e["event"] == "workflow.labelled"]
    assert labelled and labelled[-1]["detail"]["title_was"] == "test"


def test_a_title_cannot_be_blanked(api):
    """Unlike the labels, a title is not clearable — a workflow without one is not a record
    of anything — so blank is refused rather than read as 'remove'."""
    workflow_id = api.draft([task("s1")])
    response = api.client.patch(f"/v1/workflows/{workflow_id}", json={"title": "   "})
    assert response.status_code == 422
    assert api.client.get(f"/v1/workflows/{workflow_id}").json()["title"] == "test"
