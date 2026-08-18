from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from chief.app import create_app
from chief.storage import Store


@pytest.fixture()
def store() -> Store:
    store = Store(":memory:")
    yield store
    store.close()


@pytest.fixture()
def client(store: Store) -> TestClient:
    with TestClient(create_app(store)) as client:
        yield client


def task(step_id: str, *, depends_on: list[str] | None = None, harness: str = "claude_cli") -> dict:
    return {
        "id": step_id,
        "type": "task",
        "goal": f"do {step_id}",
        "harness": harness,
        "depends_on": depends_on or [],
    }


def construct(
    step_id: str,
    kind: str,
    body: list[str],
    *,
    depends_on: list[str] | None = None,
    on_instance_failure: str = "fail_fast",
) -> dict:
    return {
        "id": step_id,
        "type": kind,
        "goal": f"run {step_id}",
        "harness": "claude_cli",
        "depends_on": depends_on or [],
        "body": body,
        "on_instance_failure": on_instance_failure,
    }


def checkpoint(
    step_id: str,
    *,
    depends_on: list[str] | None = None,
    fields: list[dict] | None = None,
    goal: str | None = None,
) -> dict:
    step = {
        "id": step_id,
        "type": "checkpoint",
        "goal": goal or f"decide {step_id}",
        "harness": "human",
        "depends_on": depends_on or [],
    }
    if fields is not None:
        step["fields"] = fields
    return step


class Api:
    """Thin helper so tests read as workflow narrative rather than URL plumbing."""

    def __init__(self, client: TestClient) -> None:
        self.client = client

    def create_workflow(self, steps: list[dict], *, title: str = "test", **kw: Any):
        payload = {"title": title, "source": "generated", "generated_by": "planner", **kw}
        payload["steps"] = steps
        return self.client.post("/v1/workflows", json=payload)

    def approved_workflow(self, steps: list[dict], **kw: Any) -> str:
        response = self.create_workflow(steps, **kw)
        assert response.status_code == 201, response.text
        workflow_id = response.json()["workflow_id"]
        assert self.client.post(f"/v1/workflows/{workflow_id}/approve").status_code == 200
        return workflow_id

    def run(self, steps: list[dict], **kw: Any) -> tuple[str, str]:
        workflow_id = self.approved_workflow(steps, **kw)
        response = self.client.post(f"/v1/workflows/{workflow_id}/runs", json={})
        assert response.status_code == 201, response.text
        return workflow_id, response.json()["run_id"]

    def update_step(self, run_id: str, step_id: str, **body: Any):
        body.setdefault("summary", f"updated {step_id}")
        return self.client.post(f"/v1/runs/{run_id}/steps/{step_id}/updates", json=body)

    def resolve(self, run_id: str, step_id: str, decision: str = "approved", **body: Any):
        return self.client.post(
            f"/v1/runs/{run_id}/steps/{step_id}/resolution", json={"decision": decision, **body}
        )

    def nested_resolve(self, run_id: str, path: str, decision: str = "approved", **body: Any):
        return self.client.post(
            f"/v1/runs/{run_id}/resolutions/{path}", json={"decision": decision, **body}
        )

    def comment(self, run_id: str, artifact_id: str, body: str = "worth knowing", **kw: Any):
        return self.client.post(
            f"/v1/runs/{run_id}/artifacts/{artifact_id}/comments", json={"body": body, **kw}
        )

    def note(self, workflow_id: str, body: str = "this needs rethinking", **kw: Any):
        return self.client.post(f"/v1/workflows/{workflow_id}/notes", json={"body": body, **kw})

    def notes(self, workflow_id: str, **params: Any) -> list[dict]:
        response = self.client.get(f"/v1/workflows/{workflow_id}/notes", params=params)
        assert response.status_code == 200, response.text
        return response.json()

    def decide_note(self, workflow_id: str, note_id: str, **body: Any):
        return self.client.patch(f"/v1/workflows/{workflow_id}/notes/{note_id}", json=body)

    def draft(self, steps: list[dict], **kw: Any) -> str:
        response = self.create_workflow(steps, **kw)
        assert response.status_code == 201, response.text
        return response.json()["workflow_id"]

    def revise(self, workflow_id: str, steps: list[dict], *, title: str = "test", **kw: Any):
        return self.client.put(
            f"/v1/workflows/{workflow_id}", json={"title": title, "steps": steps, **kw}
        )

    def add_instance(self, run_id: str, step_id: str, **body: Any):
        return self.client.post(f"/v1/runs/{run_id}/steps/{step_id}/instances", json=body)

    def update_instance(self, run_id: str, step_id: str, instance_id: str, **body: Any):
        body.setdefault("summary", f"updated {instance_id}")
        return self.client.post(
            f"/v1/runs/{run_id}/steps/{step_id}/instances/{instance_id}/updates", json=body
        )

    def update_body_step(
        self, run_id: str, step_id: str, instance_id: str, body_step_id: str, **body: Any
    ):
        body.setdefault("summary", f"updated {body_step_id}")
        return self.client.post(
            f"/v1/runs/{run_id}/steps/{step_id}/instances/{instance_id}"
            f"/steps/{body_step_id}/updates",
            json=body,
        )

    # generalised nested addressing
    def nested_update(self, run_id: str, path: str, **body: Any):
        body.setdefault("summary", f"updated {path}")
        return self.client.post(f"/v1/runs/{run_id}/state/{path}/updates", json=body)

    def nested_instance(self, run_id: str, path: str, **body: Any):
        return self.client.post(f"/v1/runs/{run_id}/state/{path}/instances", json=body)

    def nested_instance_update(self, run_id: str, path: str, **body: Any):
        body.setdefault("summary", f"updated {path}")
        return self.client.post(f"/v1/runs/{run_id}/instance-updates/{path}", json=body)

    def propose(self, run_id: str, operations: list[dict], *, kind: str = "forward", **kw: Any):
        payload = {
            "proposed_by": kw.pop("proposed_by", "planner"),
            "kind": kind,
            "reason": kw.pop("reason", "because the plan changed"),
            "operations": operations,
        }
        return self.client.post(f"/v1/runs/{run_id}/amendments", json=payload)

    def approve(self, amendment_id: str, **body: Any):
        return self.client.post(f"/v1/amendments/{amendment_id}/approve", json=body)

    def reject(self, amendment_id: str, **body: Any):
        return self.client.post(f"/v1/amendments/{amendment_id}/reject", json=body)

    def withdraw(self, amendment_id: str, **body: Any):
        return self.client.post(f"/v1/amendments/{amendment_id}/withdraw", json=body)

    def get_run(self, run_id: str) -> dict:
        response = self.client.get(f"/v1/runs/{run_id}")
        assert response.status_code == 200, response.text
        return response.json()

    def step_status(self, run_id: str, step_id: str) -> str:
        return self.get_run(run_id)["step_states"][step_id]["status"]

    def run_status(self, run_id: str) -> str:
        return self.get_run(run_id)["status"]

    def instance(self, run_id: str, step_id: str, instance_id: str) -> dict:
        state = self.get_run(run_id)["step_states"][step_id]
        return next(i for i in state["instances"] if i["instance_id"] == instance_id)


@pytest.fixture()
def api(client: TestClient) -> Api:
    return Api(client)


def _get_amendment_status(self: Api, amendment_id: str) -> str:
    response = self.client.get(f"/v1/amendments/{amendment_id}")
    assert response.status_code == 200, response.text
    return response.json()["status"]


Api.get_amendment_status = _get_amendment_status
