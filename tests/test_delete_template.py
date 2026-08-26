"""Deleting a template: no cascade, because a template owns nothing else in the schema — a
workflow made from one carries its own `from_template` lineage record rather than a live
reference back here (see ``Service.delete_template``).
"""

from __future__ import annotations

from .conftest import Api, task


def test_deleting_removes_the_template(api: Api) -> None:
    template_id = api.create_template([task("step_01")])

    response = api.client.delete(f"/v1/templates/{template_id}")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["template_id"] == template_id
    assert body["deleted"] is True

    assert api.client.get(f"/v1/templates/{template_id}").status_code == 404
    assert template_id not in [t["template_id"] for t in api.client.get("/v1/templates").json()]


def test_deleting_a_template_that_is_not_there_is_a_404(api: Api) -> None:
    assert api.client.delete("/v1/templates/tpl_nope").status_code == 404


def test_the_audit_trail_survives_and_records_the_deletion(api: Api) -> None:
    template_id = api.create_template([task("step_01")], title="the one to remove")
    api.client.delete(f"/v1/templates/{template_id}")

    entries = api.client.get("/v1/audit").json()
    matching = [e for e in entries if e.get("detail", {}).get("template_id") == template_id]
    events = [e["event"] for e in matching]
    assert "template.created" in events
    assert events[-1] == "template.deleted"
    assert matching[-1]["detail"]["title"] == "the one to remove"


def test_a_workflow_already_made_from_the_template_survives(api: Api) -> None:
    """It carries its own steps and its own lineage record the moment it is instantiated,
    not a live reference back to the template — deleting the shape does not break what was
    already built from it."""
    template_id = api.create_template([task("step_01")])
    response = api.client.post(f"/v1/templates/{template_id}/workflows", json={})
    assert response.status_code == 201, response.text
    workflow_id = response.json()["workflow_id"]

    api.client.delete(f"/v1/templates/{template_id}")

    assert api.client.get(f"/v1/workflows/{workflow_id}").status_code == 200


def test_deleting_leaves_a_neighbouring_template_untouched(api: Api) -> None:
    doomed = api.create_template([task("step_01")], title="doomed")
    keeper = api.create_template([task("step_01")], title="keeper")

    api.client.delete(f"/v1/templates/{doomed}")

    assert api.client.get(f"/v1/templates/{keeper}").status_code == 200


def test_delete_is_not_on_the_mcp_surface() -> None:
    """Same reasoning as `delete_workflow`: there is no agent session that legitimately
    needs to erase a reusable shape on its own initiative."""
    from chief.mcp_server import HARNESS_OPERATIONS

    assert "delete_template" not in HARNESS_OPERATIONS
