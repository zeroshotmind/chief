"""Templates: the reusable plan (extension).

A workflow is single-use — approved once, executed once — so reuse lives in a template
instead of in a second run of the same workflow. These cover the two directions (write a
template, extract one from a workflow that worked), the substitution rules, and the fact
that REQ-32's approval gate survives the whole thing.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from .conftest import Api, task


def template_body(**kw: Any) -> dict:
    body = {
        "title": "Triage {{ repo }}",
        "parameters": [
            {"name": "repo", "description": "owner/name"},
            {"name": "since", "default": "24h", "required": False},
        ],
        "steps": [
            {
                "id": "s1",
                "type": "task",
                "goal": "Fetch issues in {{ repo }} updated in the last {{ since }}",
                "harness": "claude-code",
                "inputs": {"repo": "{{ repo }}"},
            },
            {
                "id": "s2",
                "type": "task",
                "goal": "Summarise triage for {{ repo }}",
                "harness": "claude-code",
                "depends_on": ["s1"],
            },
        ],
    }
    body.update(kw)
    return body


@pytest.fixture()
def template_id(client: TestClient) -> str:
    response = client.post("/v1/templates", json=template_body())
    assert response.status_code == 201, response.text
    return response.json()["template_id"]


# --- authoring -------------------------------------------------------------------------


def test_a_placeholder_must_be_declared(client: TestClient) -> None:
    """Otherwise the plan renders with a literal {{ typo }} nobody notices until it is read."""
    body = template_body(parameters=[{"name": "repo"}])  # 'since' left undeclared
    response = client.post("/v1/templates", json=body)
    assert response.status_code == 422
    assert "since" in response.text


def test_a_template_plan_is_validated_as_a_graph(client: TestClient) -> None:
    """Placeholders never touch ids or edges, so the unrendered plan has the final graph."""
    body = template_body(
        steps=[{"id": "s1", "type": "task", "goal": "g", "harness": "h", "depends_on": ["nope"]}],
        parameters=[],
    )
    assert client.post("/v1/templates", json=body).status_code == 422


# --- instantiation ---------------------------------------------------------------------


def test_instantiating_substitutes_text_and_inputs(client: TestClient, template_id: str) -> None:
    response = client.post(
        f"/v1/templates/{template_id}/workflows", json={"parameters": {"repo": "acme/api"}}
    )
    assert response.status_code == 201, response.text
    workflow = response.json()

    assert workflow["title"] == "Triage acme/api"
    assert workflow["steps"][0]["goal"] == (
        "Fetch issues in acme/api updated in the last 24h"  # 'since' fell back to its default
    )
    assert workflow["steps"][0]["inputs"] == {"repo": "acme/api"}
    assert workflow["steps"][1]["depends_on"] == ["s1"], "structure must survive untouched"


def test_the_result_is_a_draft(client: TestClient, template_id: str) -> None:
    """REQ-32 is not weakened by templates: the instance still needs approving."""
    workflow = client.post(
        f"/v1/templates/{template_id}/workflows", json={"parameters": {"repo": "acme/api"}}
    ).json()
    assert workflow["status"] == "draft"
    assert client.post(f"/v1/workflows/{workflow['workflow_id']}/runs", json={}).status_code == 409


def test_lineage_records_what_it_was_made_from(client: TestClient, template_id: str) -> None:
    workflow = client.post(
        f"/v1/templates/{template_id}/workflows", json={"parameters": {"repo": "acme/api"}}
    ).json()
    origin = workflow["from_template"]
    assert origin["template_id"] == template_id
    assert origin["template_version"] == 1
    # The resolved values, not just the supplied ones: what this plan was actually built with.
    assert origin["parameters"] == {"repo": "acme/api", "since": "24h"}


def test_a_missing_required_parameter_is_refused(client: TestClient, template_id: str) -> None:
    response = client.post(f"/v1/templates/{template_id}/workflows", json={"parameters": {}})
    assert response.status_code == 422
    assert "repo" in response.text


def test_an_unknown_parameter_is_refused_rather_than_ignored(
    client: TestClient, template_id: str
) -> None:
    """A typo would otherwise leave the default in place and look like it worked."""
    response = client.post(
        f"/v1/templates/{template_id}/workflows",
        json={"parameters": {"repo": "acme/api", "sinse": "7d"}},
    )
    assert response.status_code == 422
    assert "sinse" in response.text


def test_an_archived_template_cannot_be_instantiated(
    client: TestClient, template_id: str
) -> None:
    assert client.post(f"/v1/templates/{template_id}/archive").status_code == 200
    response = client.post(
        f"/v1/templates/{template_id}/workflows", json={"parameters": {"repo": "acme/api"}}
    )
    assert response.status_code == 404


# --- extraction ------------------------------------------------------------------------


def test_a_template_can_be_extracted_from_a_workflow(api: Api) -> None:
    workflow_id = api.approved_workflow(
        [task("step_01"), task("step_02", depends_on=["step_01"])], title="Triage acme/api"
    )
    response = api.client.post(
        f"/v1/workflows/{workflow_id}/template",
        json={"substitutions": {"acme/api": "repo"}, "title": "Triage {{ repo }}"},
    )
    assert response.status_code == 201, response.text
    template = response.json()
    assert template["derived_from_workflow_id"] == workflow_id
    # The literal it replaced becomes the default, so the template reproduces the original.
    assert template["parameters"] == [
        {"name": "repo", "description": None, "required": True, "default": "acme/api"}
    ]


def test_extraction_round_trips_to_the_original(api: Api) -> None:
    steps = [
        {
            "id": "s1",
            "type": "task",
            "goal": "deploy acme/api to prod",
            "harness": "claude-code",
            "depends_on": [],
        }
    ]
    workflow_id = api.approved_workflow(steps)
    template_id = api.client.post(
        f"/v1/workflows/{workflow_id}/template", json={"substitutions": {"acme/api": "repo"}}
    ).json()["template_id"]

    rebuilt = api.client.post(f"/v1/templates/{template_id}/workflows", json={}).json()
    assert rebuilt["steps"][0]["goal"] == "deploy acme/api to prod"

    elsewhere = api.client.post(
        f"/v1/templates/{template_id}/workflows", json={"parameters": {"repo": "other/svc"}}
    ).json()
    assert elsewhere["steps"][0]["goal"] == "deploy other/svc to prod"


def test_longer_literals_are_substituted_first(api: Api) -> None:
    """Otherwise replacing 'main' chews a hole in 'maintenance'."""
    steps = [
        {
            "id": "s1",
            "type": "task",
            "goal": "run maintenance on main",
            "harness": "h",
            "depends_on": [],
        }
    ]
    workflow_id = api.approved_workflow(steps)
    template_id = api.client.post(
        f"/v1/workflows/{workflow_id}/template",
        json={"substitutions": {"main": "branch", "maintenance": "job"}},
    ).json()["template_id"]

    built = api.client.post(f"/v1/templates/{template_id}/workflows", json={}).json()
    assert built["steps"][0]["goal"] == "run maintenance on main"


# --- approval policy (REQ-32 + REQ-43) --------------------------------------------------


def test_a_policy_can_auto_approve_a_template_instance(
    client: TestClient, template_id: str
) -> None:
    policy = {
        "rules": [
            {
                "id": "trusted-triage",
                "match": f"workflow.template_id == '{template_id}'",
                "auto_approve": True,
            }
        ]
    }
    assert client.put("/v1/config/workflow-approval-policy", json=policy).status_code == 200

    workflow = client.post(
        f"/v1/templates/{template_id}/workflows", json={"parameters": {"repo": "acme/api"}}
    ).json()
    assert workflow["status"] == "approved"

    entry = next(
        e
        for e in client.get("/v1/audit", params={"workflow_id": workflow["workflow_id"]}).json()
        if e["event"] == "workflow.approved"
    )
    assert entry["detail"]["decided_by"] == "policy:trusted-triage"


def test_a_policy_cannot_auto_approve_a_hand_written_plan(client: TestClient) -> None:
    """The workflow half of REQ-32: only a plan whose template a human approved may skip.

    Proven at write time, the same way a history_edit rule is — so the bad rule cannot be
    stored, rather than being ignored later.
    """
    response = client.put(
        "/v1/config/workflow-approval-policy",
        json={"rules": [{"match": "workflow.source == 'import'", "auto_approve": True}]},
    )
    assert response.status_code == 422
    assert "template" in response.text


def test_an_auto_approval_never_reads_as_a_persons_decision(
    client: TestClient, template_id: str
) -> None:
    """A harness can trigger an auto-approval by instantiating a template — the policy is a
    standing human decision, so that is intended. What must not happen is the record making
    it look like someone decided in the moment.
    """
    client.put(
        "/v1/config/workflow-approval-policy",
        json={
            "rules": [
                {"id": "auto", "match": f"workflow.template_id == '{template_id}'",
                 "auto_approve": True}
            ]
        },
    )
    workflow = client.post(
        f"/v1/templates/{template_id}/workflows", json={"parameters": {"repo": "acme/api"}}
    ).json()

    entry = next(
        e
        for e in client.get("/v1/audit", params={"workflow_id": workflow["workflow_id"]}).json()
        if e["event"] == "workflow.approved"
    )
    assert entry["detail"]["decided_by"].startswith("policy:")
    assert entry["detail"]["decided_by"] != "human"
    # And which transport it arrived on, so the path is reconstructable afterwards.
    assert entry["detail"]["via"] in ("rest", "mcp")


def test_lineage_survives_extraction_atomically(api: Api) -> None:
    """One write, so a template can never exist without the lineage that explains it."""
    workflow_id = api.approved_workflow([task("step_01")])
    template = api.client.post(f"/v1/workflows/{workflow_id}/template", json={}).json()
    assert template["derived_from_workflow_id"] == workflow_id
    fetched = api.client.get(f"/v1/templates/{template['template_id']}").json()
    assert fetched["derived_from_workflow_id"] == workflow_id
    assert fetched["created_at"] == fetched["updated_at"]


def test_a_hand_written_workflow_is_untouched_by_the_policy(
    client: TestClient, template_id: str
) -> None:
    client.put(
        "/v1/config/workflow-approval-policy",
        json={
            "rules": [
                {"match": f"workflow.template_id == '{template_id}'", "auto_approve": True}
            ]
        },
    )
    response = client.post(
        "/v1/workflows",
        json={"title": "by hand", "source": "generated", "steps": [task("step_01")]},
    )
    assert response.json()["status"] == "draft"
