"""Configurable approval authority (REQ-43, contract 1.9, 2.4)."""

from __future__ import annotations

import pytest

from chief.domain import policy_eval
from chief.errors import ValidationFailed

from .conftest import Api, task


def two_steps() -> list[dict]:
    return [task("step_01"), task("step_02", depends_on=["step_01"])]


def insert(step_id: str) -> dict:
    return {"op": "insert_after", "target_step_id": "step_02", "step": task(step_id)}


def test_default_policy_is_empty_so_everything_needs_a_human(api: Api) -> None:
    assert api.client.get("/v1/config/approval-policy").json() == {"rules": []}
    _, run_id = api.run(two_steps())
    assert api.propose(run_id, [insert("step_03")]).json()["status"] == "pending_approval"


def test_forward_amendments_can_be_auto_approved(api: Api) -> None:
    response = api.client.put(
        "/v1/config/approval-policy",
        json={
            "rules": [
                {
                    "id": "trusted-planner",
                    "match": "amendment.kind == 'forward' && amendment.proposed_by == 'planner'",
                    "auto_approve": True,
                }
            ]
        },
    )
    assert response.status_code == 200

    _, run_id = api.run(two_steps())
    amendment = api.propose(run_id, [insert("step_03")]).json()
    assert amendment["status"] == "approved"
    assert amendment["decided_by"] == "policy:trusted-planner"
    assert api.run_status(run_id) == "running"
    assert api.get_run(run_id)["step_states"]["step_03"]["status"] == "pending"


def test_non_matching_proposer_still_needs_a_human(api: Api) -> None:
    api.client.put(
        "/v1/config/approval-policy",
        json={
            "rules": [
                {
                    "match": "amendment.kind == 'forward' && amendment.proposed_by == 'planner'",
                    "auto_approve": True,
                }
            ]
        },
    )
    _, run_id = api.run(two_steps())
    amendment = api.propose(run_id, [insert("step_03")], proposed_by="someone_else").json()
    assert amendment["status"] == "pending_approval"


def test_a_policy_that_could_auto_approve_a_history_edit_is_refused_at_write_time(
    api: Api,
) -> None:
    for expression in ("true", "amendment.proposed_by == 'planner'", "amendment.kind != 'x'"):
        response = api.client.put(
            "/v1/config/approval-policy",
            json={"rules": [{"match": expression, "auto_approve": True}]},
        )
        assert response.status_code == 422, expression
        assert "history_edit" in response.json()["error"]["message"]
    # Not silently stored either.
    assert api.client.get("/v1/config/approval-policy").json() == {"rules": []}


def test_a_non_approving_rule_may_match_anything(api: Api) -> None:
    response = api.client.put(
        "/v1/config/approval-policy",
        json={"rules": [{"match": "true", "auto_approve": False}]},
    )
    assert response.status_code == 200


def test_history_edits_are_never_auto_approved_even_by_hand(api: Api) -> None:
    _, run_id = api.run(two_steps())
    api.update_step(run_id, "step_01", status="completed")
    amendment_id = api.propose(
        run_id, [{"op": "replay_step", "target_step_id": "step_01"}], kind="history_edit"
    ).json()["amendment_id"]
    refused = api.approve(amendment_id, decided_by="policy:whatever")
    assert refused.status_code == 409
    assert "human decision" in refused.json()["error"]["message"]
    assert api.approve(amendment_id, decided_by="human").status_code == 200


def test_first_matching_rule_wins(api: Api) -> None:
    api.client.put(
        "/v1/config/approval-policy",
        json={
            "rules": [
                {
                    "id": "no-inserts",
                    "match": "amendment.kind == 'forward' && amendment.ops subset_of "
                    "['insert_after']",
                    "auto_approve": False,
                },
                {"id": "catch-all", "match": "amendment.kind == 'forward'", "auto_approve": True},
            ]
        },
    )
    _, run_id = api.run(two_steps())
    assert api.propose(run_id, [insert("step_03")]).json()["status"] == "pending_approval"


def test_syntax_errors_are_reported(api: Api) -> None:
    response = api.client.put(
        "/v1/config/approval-policy",
        json={"rules": [{"match": "amendment.kind = 'forward'", "auto_approve": False}]},
    )
    assert response.status_code == 422


@pytest.mark.parametrize(
    "expression",
    [
        "amendment.nope == 'x'",
        "run.kind == 'forward'",
        "amendment.ops == 'insert_after'",
        "amendment.kind subset_of ['forward']",
        "amendment.kind == 'forward' &&",
        "(amendment.kind == 'forward'",
    ],
)
def test_expression_grammar_rejects_nonsense(expression: str) -> None:
    with pytest.raises(ValidationFailed):
        policy_eval.parse(expression)
