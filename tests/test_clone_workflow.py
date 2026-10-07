import pytest

from .conftest import Api, task


@pytest.mark.parametrize("status", ["draft", "completed", "archived"])
def test_clone_is_independent_draft_with_execution_settings(client, tmp_path, status):
    api = Api(client)
    step = task("work")
    step["group"] = "phase"
    step["criteria"] = ["result checked"]
    step["execution"] = {"executor": "claude", "model": "sonnet", "prompt": "Output world",
                         "cwd": str(tmp_path), "timeout_seconds": 90,
                         "profile": "limited_tools", "tools": ["Read", "Grep"]}
    response = api.create_workflow([step], title="Original", project="chief",
                                   groups=[{"path": "phase", "description": "Test phase"}],
                                   origin_dir=str(tmp_path))
    original = response.json()
    wid = original["workflow_id"]
    run_id = None
    if status != "draft":
        assert client.post(f"/v1/workflows/{wid}/approve").status_code == 200
        run_id = client.post(f"/v1/workflows/{wid}/runs", json={}).json()["run_id"]
        api.update_step(run_id, "work", status="running")
        assert api.update_step(run_id, "work", status="completed", summary="world",
                               criteria_met={"c1": "checked"},
                               metadata={"token_usage": {"input_tokens": 100}}).status_code == 200
        if status == "archived":
            assert client.post(f"/v1/workflows/{wid}/archive").status_code == 200
    source_before = client.get(f"/v1/workflows/{wid}").json()
    response = client.post(f"/v1/workflows/{wid}/clone",
                           params={"run_id": run_id} if run_id else {})
    assert response.status_code == 201, response.text
    cloned = response.json()
    cid = cloned["workflow_id"]
    assert cid != wid
    assert cloned["title"] == "Original (copy)"
    assert cloned["status"] == "draft" and cloned["version"] == 1
    assert cloned["steps"] == original["steps"]
    assert cloned["groups"] == original["groups"]
    assert cloned["project"] == "chief" and cloned["origin_dir"] == str(tmp_path)
    assert cloned["review_notes"] == []
    assert cloned["from_template"] is None and cloned["from_graph"] is None
    assert client.get(f"/v1/runs?workflow_id={cid}").json() == []
    assert client.get(f"/v1/workflows/{wid}").json() == source_before
    # Approval remains mandatory and the new run starts without results or usage.
    assert client.post(f"/v1/workflows/{cid}/runs", json={}).status_code == 409
    assert client.post(f"/v1/workflows/{cid}/approve").status_code == 200
    run = client.post(f"/v1/workflows/{cid}/runs", json={}).json()
    assert run["step_states"]["work"]["status"] == "pending"
    assert run["step_states"]["work"]["metadata"] == {}


def test_clone_uses_selected_runs_effective_plan(client):
    api = Api(client)
    wid, run_id = api.run([task("work")])
    updated = task("work")
    updated["goal"] = "amended goal"
    amendment = api.propose(run_id, [{"op": "update_step", "target_step_id": "work",
                                      "step": updated}])
    assert amendment.status_code == 201, amendment.text
    assert api.approve(amendment.json()["amendment_id"]).status_code == 200
    cloned = client.post(f"/v1/workflows/{wid}/clone", params={"run_id": run_id})
    assert cloned.status_code == 201, cloned.text
    assert cloned.json()["steps"][0]["goal"] == "amended goal"
    other = api.create_workflow([task("other")]).json()["workflow_id"]
    assert client.post(f"/v1/workflows/{other}/clone",
                       params={"run_id": run_id}).status_code == 422
    assert client.post("/v1/workflows/wf_missing/clone").status_code == 404
