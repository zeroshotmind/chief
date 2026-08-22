"""Instance parameters: what tells one branch of a runtime-sized construct from another.

A parallel step's branch count is decided while it runs, so what distinguishes a branch can
only arrive while it runs — in the instance's metadata. That already worked; what did not
exist was any way to *require* it. See CONTRACT-NOTES.md #40.
"""

from __future__ import annotations

from typing import Any

from .conftest import Api, construct, task


def fanout(*params: dict, kind: str = "parallel", body: list[str] | None = None) -> dict:
    step = construct("step_01", kind, body or ["step_02"])
    step["instance_params"] = list(params)
    return step


PAPER = {"name": "paper", "description": "which paper this branch reads"}


# ── declaring ──────────────────────────────────────────────────────────────────────────


def test_params_are_construct_only(api: Api) -> None:
    step = {**task("step_01"), "instance_params": [PAPER]}
    response = api.create_workflow([step])
    assert response.status_code == 422, response.text
    assert "only to loop/parallel" in response.json()["error"]["message"]


def test_the_same_param_cannot_be_declared_twice(api: Api) -> None:
    response = api.create_workflow([fanout(PAPER, PAPER), task("step_02")])
    assert response.status_code == 422
    assert "same instance parameter twice" in response.json()["error"]["message"]


def test_loops_take_them_too(api: Api) -> None:
    assert api.create_workflow(
        [fanout(PAPER, kind="loop"), task("step_02")]
    ).status_code == 201


# ── registering an instance ────────────────────────────────────────────────────────────


def test_a_branch_must_supply_every_declared_param(api: Api) -> None:
    _, run_id = api.run([fanout(PAPER), task("step_02")])
    response = api.add_instance(run_id, "step_01", metadata={"unrelated": 1})
    assert response.status_code == 422, response.text
    assert response.json()["error"]["details"]["missing"] == ["paper"]
    # The message says where to put it, because the field it belongs in is not obvious.
    assert "metadata" in response.json()["error"]["message"]


def test_supplying_it_registers_the_branch(api: Api) -> None:
    _, run_id = api.run([fanout(PAPER), task("step_02")])
    response = api.add_instance(run_id, "step_01", metadata={"paper": "arxiv:2401.11111"})
    assert response.status_code == 201, response.text
    assert api.instance(run_id, "step_01", "inst_00")["metadata"]["paper"] == "arxiv:2401.11111"


def test_undeclared_metadata_is_still_welcome(api: Api) -> None:
    """Params are a required subset, not a schema.

    Instance metadata is load-bearing free-form — seeds, timings, token counts — so copying
    a checkpoint's unknown-key rejection would make declaring a parameter cost more than it
    gives.
    """
    _, run_id = api.run([fanout(PAPER), task("step_02")])
    response = api.add_instance(
        run_id, "step_01", metadata={"paper": "arxiv:1", "seed": 7, "tokens": 41200}
    )
    assert response.status_code == 201, response.text


def test_a_falsy_value_counts_as_supplied(api: Api) -> None:
    """Presence, not truthiness: metadata is `Any`, so 0 and False are real answers."""
    _, run_id = api.run([fanout({"name": "shard"}, {"name": "warmup"}), task("step_02")])
    response = api.add_instance(run_id, "step_01", metadata={"shard": 0, "warmup": False})
    assert response.status_code == 201, response.text


def test_a_blank_string_does_not_count(api: Api) -> None:
    _, run_id = api.run([fanout(PAPER), task("step_02")])
    assert api.add_instance(run_id, "step_01", metadata={"paper": "   "}).status_code == 422


def test_an_optional_param_may_be_omitted(api: Api) -> None:
    _, run_id = api.run([fanout({"name": "note", "required": False}), task("step_02")])
    assert api.add_instance(run_id, "step_01", metadata={}).status_code == 201


def test_a_construct_declaring_nothing_is_unaffected(api: Api) -> None:
    """Every existing run in the wild registered instances with no declaration at all."""
    _, run_id = api.run([construct("step_01", "parallel", ["step_02"]), task("step_02")])
    assert api.add_instance(run_id, "step_01", metadata={}).status_code == 201


def test_adding_a_param_later_does_not_invalidate_registered_branches(api: Api) -> None:
    """Validation happens when an instance is registered, and never again.

    Otherwise an amendment tightening the plan would retroactively break branches that were
    valid when they ran — which is REQ-14's concern in a different shape.
    """
    _, run_id = api.run([construct("step_01", "parallel", ["step_02"]), task("step_02")])
    api.add_instance(run_id, "step_01", metadata={})

    amendment_id = api.propose(
        run_id,
        [{"op": "update_step", "target_step_id": "step_01", "step": fanout(PAPER)}],
        reason="the branches turned out to be per-paper",
    ).json()["amendment_id"]
    assert api.approve(amendment_id).json()["status"] == "approved"

    assert api.instance(run_id, "step_01", "inst_00")["metadata"] == {}
    # But the next one must comply.
    assert api.add_instance(run_id, "step_01", metadata={}).status_code == 422


# ── criteria on a parameterised body step ──────────────────────────────────────────────


def test_a_templated_criterion_still_gates_by_id(api: Api) -> None:
    """Rendering is a read-time display concern and must not reach the id or the gate.

    `criteria_met` is keyed against the criterion as written — `{{ paper }}` and all — so a
    branch answers `c1` whatever its own value happens to be.
    """
    body: dict[str, Any] = {
        **task("step_02"),
        "goal": "read {{ paper }}",
        "criteria": ["the summary of {{ paper }} cites its own results table"],
    }
    _, run_id = api.run([fanout(PAPER), body])
    api.add_instance(run_id, "step_01", metadata={"paper": "arxiv:2401.11111"})

    path = "step_01/inst_00/step_02"
    refused = api.nested_update(run_id, path, status="completed")
    assert refused.status_code == 409, refused.text
    assert refused.json()["error"]["details"]["unmet"][0]["text"] == (
        "the summary of {{ paper }} cites its own results table"
    )
    accepted = api.nested_update(
        run_id, path, status="completed", criteria_met={"c1": "table 3, cited in §4"}
    )
    assert accepted.status_code == 200, accepted.text
