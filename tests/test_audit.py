"""Full auditability: every transition, update and amendment decision is logged (REQ-20)."""

from __future__ import annotations

from .conftest import Api, task


def test_every_transition_is_recorded_with_a_timestamp(api: Api) -> None:
    workflow_id, run_id = api.run([task("step_01"), task("step_02", depends_on=["step_01"])])
    api.update_step(run_id, "step_01", status="completed")
    amendment_id = api.propose(
        run_id,
        [{"op": "insert_after", "target_step_id": "step_02", "step": task("step_03")}],
        reason="need a cleanup step",
    ).json()["amendment_id"]
    api.approve(amendment_id, reason="looks right")

    entries = api.client.get(f"/v1/audit?workflow_id={workflow_id}").json()
    events = [entry["event"] for entry in entries]
    assert events == [
        "workflow.created",
        "workflow.approved",
        "run.registered",
        "step.updated",
        "amendment.proposed",
        "amendment.approved",
    ]
    assert all(entry["at"] for entry in entries)
    assert entries[-1]["detail"]["decided_by"] == "human"
    assert entries[-1]["detail"]["resulting_workflow_version"] == 2
    assert entries[-2]["detail"]["reason"] == "need a cleanup step"


def test_rejections_and_withdrawals_are_logged_too(api: Api) -> None:
    _, run_id = api.run([task("step_01")])
    first = api.propose(
        run_id, [{"op": "insert_after", "target_step_id": "step_01", "step": task("step_02")}]
    ).json()["amendment_id"]
    api.reject(first, reason="no")
    second = api.propose(
        run_id, [{"op": "insert_after", "target_step_id": "step_01", "step": task("step_03")}]
    ).json()["amendment_id"]
    api.withdraw(second, reason="never mind")

    events = [e["event"] for e in api.client.get(f"/v1/audit?run_id={run_id}").json()]
    assert "amendment.rejected" in events
    assert "amendment.withdrawn" in events


def test_audit_can_be_filtered_to_one_amendment(api: Api) -> None:
    _, run_id = api.run([task("step_01")])
    amendment_id = api.propose(
        run_id, [{"op": "insert_after", "target_step_id": "step_01", "step": task("step_02")}]
    ).json()["amendment_id"]
    entries = api.client.get(f"/v1/audit?amendment_id={amendment_id}").json()
    assert [e["event"] for e in entries] == ["amendment.proposed"]


def test_instance_registration_is_audited(api: Api) -> None:
    from .conftest import construct

    _, run_id = api.run([construct("step_01", "parallel", ["step_02"]), task("step_02")])
    api.add_instance(run_id, "step_01")
    events = [e["event"] for e in api.client.get(f"/v1/audit?run_id={run_id}").json()]
    assert "instance.registered" in events


def test_policy_writes_are_audited(api: Api) -> None:
    api.client.put(
        "/v1/config/approval-policy",
        json={"rules": [{"match": "amendment.kind == 'forward'", "auto_approve": True}]},
    )
    events = [e["event"] for e in api.client.get("/v1/audit").json()]
    assert "config.approval_policy_updated" in events
