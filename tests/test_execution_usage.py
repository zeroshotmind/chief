import json

import pytest

from chief.domain.execution_output import stream_output
from chief.domain.execution_usage import normalize

from .conftest import Api, task


def parse(*events):
    return stream_output("\n".join(map(json.dumps, events)))["usage"]


def test_codex_cache_and_reasoning_are_subsets():
    report = parse({"type": "turn.completed", "usage": {
        "input_tokens": 1000, "cached_input_tokens": 800,
        "output_tokens": 200, "reasoning_output_tokens": 150,
    }})
    assert report["tokens"] == {"input_tokens": 1000, "cache_read_tokens": 800,
                                "output_tokens": 200, "reasoning_tokens": 150,
                                "total_tokens": 1200}
    assert "total_cost_usd" not in report


def test_codex_accumulates_turns_and_preserves_unknown_reasoning():
    report = parse(*[{"type": "turn.completed", "usage": usage} for usage in [
        {"input_tokens": 100, "output_tokens": 10, "reasoning_output_tokens": 2},
        {"input_tokens": 200, "output_tokens": 20},
    ]])
    assert report["tokens"]["total_tokens"] == 330
    assert "reasoning_tokens" not in report["tokens"]


def test_claude_cache_is_added_to_input_and_final_usage_replaces_live():
    event = {"type": "assistant", "message": {"id": "m1", "usage": {
        "input_tokens": 10, "cache_read_input_tokens": 40,
        "cache_creation_input_tokens": 50, "output_tokens": 1,
    }}}
    live = parse(event, event)
    assert live["tokens"]["input_tokens"] == 100
    assert "output_tokens" not in live["tokens"]  # placeholder is not a real count
    assert live["provisional"]
    final = parse(event, event, {"type": "result", "usage": {
        "input_tokens": 30, "cache_read_input_tokens": 90,
        "cache_creation_input_tokens": 80, "output_tokens": 20,
    }, "total_cost_usd": .02})
    assert final["tokens"]["total_tokens"] == 220
    assert final["tokens"]["input_tokens"] == 200
    assert "reasoning_tokens" not in final["tokens"]
    assert not final["provisional"]
    assert final["total_cost_usd"] == .02


def test_claude_model_totals_include_subagents_without_adding_main_loop_again():
    report = parse({"type": "result", "usage": {"input_tokens": 1, "output_tokens": 1},
                    "modelUsage": {
                        "main": {"inputTokens": 10, "outputTokens": 20,
                                 "cacheReadInputTokens": 50, "cacheCreationInputTokens": 30},
                        "helper": {"inputTokens": 5, "outputTokens": 10,
                                   "cacheReadInputTokens": 10, "cacheCreationInputTokens": 15},
                    }})
    assert report["tokens"]["input_tokens"] == 120
    assert report["tokens"]["output_tokens"] == 30
    assert report["tokens"]["total_tokens"] == 150
    assert report["scope"] == "all_reported_models"


def test_claude_crash_keeps_input_counts_without_inventing_output():
    report = parse({"type": "assistant", "message": {"id": "m1", "usage": {
        "input_tokens": 10, "output_tokens": 1}}}, {"type": "error", "message": "crash"})
    assert report["tokens"] == {"input_tokens": 10}
    assert report["provisional"]


@pytest.mark.parametrize("bad", [None, -1, 1.2, "100", True, float("nan")])
def test_invalid_counts_stay_unavailable(bad):
    assert normalize({"input_tokens": bad, "output_tokens": bad}, "codex") == {}


def test_old_saved_usage_is_available_on_get_and_list_without_changing_storage(client):
    _, run_id = Api(client).run([task("work")])
    response = client.post(f"/v1/runs/{run_id}/steps/work/updates", json={
        "status": "running", "summary": "Working",
        "metadata": {"execution": {"executor": "claude", "stdout": json.dumps({
            "result": "Done", "usage": {"input_tokens": 10, "output_tokens": 5,
            "cache_read_input_tokens": 20}, "total_cost_usd": .01})}},
    })
    assert response.status_code == 200, response.text
    fetched = client.get(f"/v1/runs/{run_id}").json()
    listed = client.get("/v1/runs").json()[0]
    for run in [fetched, listed]:
        usage = run["step_states"]["work"]["metadata"]["execution"]["usage"]
        assert usage["tokens"]["total_tokens"] == 35
        assert usage["total_cost_usd"] == .01
    stored, _ = client.app.state.store.get_run(run_id)
    assert "usage" not in stored.step_states["work"].metadata["execution"]
