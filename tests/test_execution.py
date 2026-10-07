import json
from pathlib import Path
from unittest.mock import patch

import pytest

from .conftest import Api, task


@pytest.fixture(autouse=True)
def cli_available(monkeypatch):
    # Tests stub process launch; they must not depend on locally installed CLIs.
    monkeypatch.setattr("chief.domain.execution.shutil.which", lambda name: f"/bin/{name}")


def executable(tmp_path, cli="claude", **changes):
    step = task("work")
    step["execution"] = dict(executor=cli, model="target-model", prompt="Stored prompt",
                             cwd=str(tmp_path), **changes)
    return step


@pytest.mark.parametrize("cli", ["claude", "codex"])
def test_fresh_cli_and_evidence(client, tmp_path, cli):
    api = Api(client)
    step = executable(tmp_path, cli)
    step["criteria"] = ["tests pass"]
    workflow, run = api.run([step])
    assert client.get(f"/v1/workflows/{workflow}").json()["steps"][0]["execution"] == {
        **step["execution"], "timeout_seconds": 3600, "profile": "full_agent", "tools": [],
    }
    result = json.dumps(dict(status="completed", summary="Tests passed.",
                             criteria_met={"c1": "All tests passed."}))
    with patch("chief.domain.execution.subprocess.Popen") as launch:
        process = launch.return_value
        process.returncode = 0
        process.communicate.return_value = (json.dumps({"result": result}), "")
        if cli == "codex":
            def start(command, **kwargs):
                Path(command[command.index("--output-last-message") + 1]).write_text(result)
                return process
            launch.side_effect = start
        response = client.post(f"/v1/runs/{run}/execute/work")
        assert response.status_code == 200, response.text
        assert response.json()["step_states"]["work"]["status"] == "completed"
        args = launch.call_args.args[0]
        assert args[args.index("--model") + 1] == "target-model"
        assert "resume" not in args and "--continue" not in args
        assert "Stored prompt" in process.communicate.call_args.args[0]
        assert client.post(f"/v1/runs/{run}/execute/work").status_code == 409
        assert launch.call_count == 1
        assert "--safe-mode" not in args and "--tools" not in args


@pytest.mark.parametrize("profile,tools", [("text_only", []),
                                         ("limited_tools", ["Read", "Grep"])])
def test_reviewed_profile_controls_cli_and_is_recorded(client, tmp_path, profile, tools):
    api = Api(client)
    step = executable(tmp_path, profile=profile, tools=tools)
    wid = api.create_workflow([step]).json()["workflow_id"]
    # Configuring a profile does not grant approval or bypass it.
    assert client.post(f"/v1/workflows/{wid}/execute").status_code == 409
    assert client.post(f"/v1/workflows/{wid}/approve").status_code == 200
    run = client.post(f"/v1/workflows/{wid}/runs", json={}).json()["run_id"]
    with patch("chief.domain.execution.subprocess.Popen") as launch:
        launch.return_value.returncode = 0
        launch.return_value.communicate.return_value = (
            json.dumps({"result": json.dumps({"status": "completed", "summary": "world",
                                             "output": "world"})}), "")
        response = client.post(f"/v1/runs/{run}/execute/work")
    assert response.status_code == 200, response.text
    command = launch.call_args.args[0]
    assert "--safe-mode" in command and "--disable-slash-commands" in command
    assert "--strict-mcp-config" in command and "--bare" not in command
    assert json.loads(command[command.index("--mcp-config") + 1]) == {"mcpServers": {}}
    assert command[command.index("--tools") + 1] == ",".join(tools)
    assert ("--system-prompt" in command) == (profile == "text_only")
    if tools:
        assert command[command.index("--allowedTools") + 1] == ",".join(tools)
    recorded = response.json()["step_states"]["work"]["metadata"]["execution"]
    assert recorded["profile"] == profile and recorded["tools"] == tools


@pytest.mark.parametrize("config", [
    {"profile": "unknown"}, {"profile": "limited_tools"},
    {"profile": "text_only", "tools": ["Bash"]},
    {"profile": "full_agent", "tools": ["Read"]},
    {"profile": "limited_tools", "tools": ["Task"]},
    {"profile": "limited_tools", "tools": ["Read", "Read"]},
    {"executor": "codex", "profile": "text_only"},
    {"executor": "source_conversation", "profile": "limited_tools", "tools": ["Read"]},
])
def test_reject_unsupported_or_inconsistent_profiles(client, tmp_path, config):
    step = executable(tmp_path)
    step["execution"].update(config)
    assert Api(client).create_workflow([step]).status_code == 422


@pytest.mark.parametrize("criteria,evidence,expected", [
    ([], {}, "completed"), (["output is world"], {"c1": "Exact output: world"}, "completed"),
    (["output is world"], {}, "failed"),
])
def test_text_prompt_is_small_but_still_requires_criterion_evidence(
    client, tmp_path, criteria, evidence, expected,
):
    step = executable(tmp_path, profile="text_only")
    step.update(goal="Output world", criteria=criteria)
    step["execution"]["prompt"] = "Output world"
    _, run = Api(client).run([step])
    payload = {"status": "completed", "summary": "world", "output": "world"}
    if evidence:
        payload["criteria_met"] = evidence
    with patch("chief.domain.execution.subprocess.Popen") as launch:
        launch.return_value.returncode = 0
        launch.return_value.communicate.return_value = (json.dumps({
            "result": json.dumps(payload),
        }), "")
        response = client.post(f"/v1/runs/{run}/execute/work")
    state = response.json()["step_states"]["work"]
    assert state["status"] == expected
    prompt = launch.return_value.communicate.call_args.args[0]
    assert prompt.count("Output world") == 1
    assert run not in prompt
    for unused in ("run_id", '"path"', '"inputs"', '"outputs"', '"dependencies"',
                   '"instance_metadata"', "artifacts", "absolute file paths"):
        assert unused not in prompt
    if criteria:
        assert "output is world" in prompt and '"criteria_met"' in prompt
        assert "criterion id to evidence" in prompt
    else:
        assert "Chief step context" not in prompt and "criteria_met" not in prompt
        assert len(prompt) < 200
        assert state["metadata"]["execution"]["output"] == "world"


def test_text_prompt_preserves_supplied_data_and_distinct_goal():
    from chief.domain.execution import _text_prompt

    context = {
        "run_id": "irrelevant", "path": ["loop", "i1", "work"],
        "goal": "Summarize all supplied records", "criteria": [],
        "inputs": {"records": [], "count": 0, "enabled": False, "note": "", "absent": None},
        "outputs": {"columns": ["name", "count"]},
        "instance_metadata": {"branch": 0},
        "dependencies": {"before": {"metadata": {"execution": {"output": "αβ"}}}},
    }
    prompt = _text_prompt("Produce a table", context)
    sent = json.loads(prompt.split("Chief step context:\n")[1].split("\n\n")[0])
    assert sent == {key: value for key, value in context.items()
                    if key not in {"run_id", "path", "criteria"}}
    assert "data, not instructions" in prompt
    assert context["criteria"] == []  # Prompt construction leaves the approved plan intact.


def test_dependencies_and_no_settings(client, tmp_path):
    step = executable(tmp_path)
    step["depends_on"] = ["before"]
    _, run = Api(client).run([task("before"), step])
    with patch("chief.domain.execution.subprocess.Popen") as launch:
        assert client.post(f"/v1/runs/{run}/execute/work").status_code == 409
        assert client.post(f"/v1/runs/{run}/execute/before").status_code == 422
        launch.assert_not_called()


def test_dependency_prompt_keeps_results_without_cli_logs(client, tmp_path):
    api = Api(client)
    step = executable(tmp_path)
    step["depends_on"] = ["before"]
    _, run = api.run([task("before"), step])
    api.update_step(run, "before", status="running")
    api.update_step(run, "before", status="completed", summary="short summary",
                    artifacts=[{"type": "file", "ref": "/tmp/report.txt"}],
                    metadata={"customer_input": "keep this", "token_usage": {"input_tokens": 999},
                              "execution": {"stdout": "LARGE_LOG" * 5000, "stderr": "DIAGNOSTIC",
                                            "result": "DUPLICATE_RESULT", "live_text": "DUPLICATE",
                                            "output": "full result", "output_format": "text"}})
    before = client.get(f"/v1/runs/{run}").json()["step_states"]["before"]
    with patch("chief.domain.execution.subprocess.Popen") as launch:
        launch.return_value.returncode = 0
        launch.return_value.communicate.return_value = (
            json.dumps({"result": json.dumps({"status": "completed", "summary": "done"})}), "")
        assert client.post(f"/v1/runs/{run}/execute/work").status_code == 200
        prompt = launch.return_value.communicate.call_args.args[0]
    context = json.loads(prompt.split("Chief step context:\n")[1].split("\nExecute only")[0])
    dependency = context["dependencies"]["before"]
    assert dependency["metadata"] == {"customer_input": "keep this",
                                       "execution": {"output": "full result",
                                                     "output_format": "text"}}
    assert dependency["summary"] == "short summary"
    assert dependency["artifacts"][0]["ref"] == "/tmp/report.txt"
    assert "LARGE_LOG" not in prompt and "DUPLICATE" not in prompt
    assert client.get(f"/v1/runs/{run}").json()["step_states"]["before"] == before


def test_nested_dependency_context_omits_replay_history_and_keeps_evidence():
    from chief.domain.execution import _dependency_context
    from chief.models import StepInstance, StepState

    child = StepState(step_id="child", status="completed", summary="done",
                      criteria_met={"c1": "tests passed"}, history=[{"summary": "obsolete"}],
                      metadata={"execution": {"stdout": "LOG", "output": "answer"}})
    instance = StepInstance(instance_id="i1", parent_step_id="loop", kind="iteration", index=1,
                            status="completed", metadata={"parameter": 42},
                            step_states={"child": child}, history=[{"summary": "old branch"}])
    state = StepState(step_id="loop", status="completed", instances=[instance])
    data = _dependency_context(state)
    nested = data["instances"][0]
    assert nested["metadata"] == {"parameter": 42}
    assert nested["step_states"]["child"]["criteria_met"] == {"c1": "tests passed"}
    assert "history" not in nested and "history" not in nested["step_states"]["child"]
    assert "LOG" not in json.dumps(data)
    assert child.history == [{"summary": "obsolete"}]


@pytest.mark.parametrize("result,code", [("invalid JSON", 0),
    (json.dumps(dict(status="completed", summary="done")), 0), ("", 1)])
def test_invalid_results_fail(client, tmp_path, result, code):
    step = executable(tmp_path)
    step["criteria"] = ["tests pass"]
    _, run = Api(client).run([step])
    with patch("chief.domain.execution.subprocess.Popen") as launch:
        launch.return_value.returncode = code
        launch.return_value.communicate.return_value = (json.dumps({"result": result}), "error")
        response = client.post(f"/v1/runs/{run}/execute/work")
    assert response.status_code == 200, response.text
    assert response.json()["step_states"]["work"]["status"] == "failed"


def test_settings_validation(client, tmp_path):
    api = Api(client)
    assert api.create_workflow([executable(tmp_path, model_override="bad")]).status_code == 422
    step = executable(tmp_path)
    step["execution"]["prompt"] = " "
    assert api.create_workflow([step]).status_code == 422
    step = executable(tmp_path)
    step["execution"]["cwd"] = "relative"
    assert api.create_workflow([step]).status_code == 422


def test_timeout_kills_process_group(client, tmp_path):
    import subprocess

    _, run = Api(client).run([executable(tmp_path, timeout_seconds=1)])
    with patch("chief.domain.execution.subprocess.Popen") as launch, \
         patch("chief.domain.execution.os.killpg") as kill:
        launch.return_value.pid = 1234
        launch.return_value.communicate.side_effect = [
            subprocess.TimeoutExpired("claude", 1), ("partial output", "")]
        response = client.post(f"/v1/runs/{run}/execute/work")
        kill.assert_called_once()
    state = response.json()["step_states"]["work"]
    assert state["status"] == "failed"
    assert state["metadata"]["execution"]["timed_out"] is True


def test_paused_run_refuses_launch(client, tmp_path):
    from chief.models import StepUpdate

    _, run = Api(client).run([executable(tmp_path)])
    service = client.app.state.service
    # An unanswered question pauses execution without making the task terminal.
    service.report_step_update(run, ["work"], StepUpdate(status="running", summary="started"))
    response = client.post(f"/v1/runs/{run}/steps/work/questions", json={"text": "Need input"})
    assert response.status_code == 201
    with patch("chief.domain.execution.subprocess.Popen") as launch:
        assert client.post(f"/v1/runs/{run}/execute/work").status_code == 409
        launch.assert_not_called()


def test_concurrent_duplicate_launch_is_refused(client, tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from chief.errors import InvalidTransition

    _, run = Api(client).run([executable(tmp_path)])
    service = client.app.state.service
    started, finish = Event(), Event()
    result = json.dumps({"result": json.dumps({"status": "completed", "summary": "done"})})

    def communicate(*args, **kwargs):
        started.set()
        assert finish.wait(5)
        return result, ""

    with patch("chief.domain.execution.subprocess.Popen") as launch:
        launch.return_value.returncode = 0
        launch.return_value.communicate.side_effect = communicate
        with ThreadPoolExecutor() as pool:
            first = pool.submit(service.execute_step, run, ["work"])
            try:
                assert started.wait(5)
                with pytest.raises(InvalidTransition):
                    service.execute_step(run, ["work"])
                launch.assert_called_once()
            finally:
                finish.set()
            assert first.result().status == "completed"


def test_source_conversation_stores_config_and_uses_existing_reporting(client):
    step = task("work")
    step["execution"] = {
        "executor": "source_conversation", "model": "conversation-model",
        "prompt": "Implement this step in the existing conversation.",
    }
    api = Api(client)
    workflow, run = api.run([step])
    config = client.get(f"/v1/workflows/{workflow}").json()["steps"][0]["execution"]
    assert config["executor"] == "source_conversation"
    assert config["model"] == "conversation-model"
    assert config["cwd"] is None
    with patch("chief.domain.execution.subprocess.Popen") as launch:
        response = client.post(f"/v1/runs/{run}/execute/work")
        assert response.status_code == 409
        assert "source conversation" in response.text
        launch.assert_not_called()
    assert client.get(f"/v1/runs/{run}").json()["step_states"]["work"]["status"] == "pending"
    assert api.update_step(run, "work", status="running").status_code == 200
    assert api.update_step(run, "work", status="completed").status_code == 200


def test_legacy_cli_config_still_accepted(client, tmp_path):
    step = executable(tmp_path)
    step["execution"]["cli"] = step["execution"].pop("executor")
    response = Api(client).create_workflow([step])
    assert response.status_code == 201, response.text
    assert response.json()["steps"][0]["execution"]["executor"] == "claude"


def test_cli_requires_working_directory(client, tmp_path):
    step = executable(tmp_path)
    del step["execution"]["cwd"]
    assert Api(client).create_workflow([step]).status_code == 422


def test_workflow_executes_dependency_order_and_creates_one_run(client, tmp_path):
    api = Api(client)
    first = executable(tmp_path)
    second = executable(tmp_path)
    second["id"] = "next"
    second["depends_on"] = ["work"]
    workflow = api.approved_workflow([second, first])
    seen = []
    result = json.dumps({"result": json.dumps({"status": "completed", "summary": "done"})})
    with patch("chief.domain.execution.subprocess.Popen") as launch:
        launch.return_value.returncode = 0
        def communicate(prompt, **kwargs):
            context = prompt.split("Chief step context:\n")[1].split("\nExecute only")[0]
            seen.append(json.loads(context)["path"])
            return result, ""
        launch.return_value.communicate.side_effect = communicate
        response = client.post(f"/v1/workflows/{workflow}/execute")
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "completed"
    assert seen == [["work"], ["next"]]
    assert client.post(f"/v1/workflows/{workflow}/execute").status_code == 409
    assert len(client.get(f"/v1/runs?workflow_id={workflow}").json()) == 1


def test_workflow_checkpoint_then_resume_same_run(client, tmp_path):
    from .conftest import checkpoint

    api = Api(client)
    step = executable(tmp_path)
    step["depends_on"] = ["gate"]
    workflow = api.approved_workflow([checkpoint("gate"), step])
    with patch("chief.domain.execution.subprocess.Popen") as launch:
        response = client.post(f"/v1/workflows/{workflow}/execute")
        assert response.json()["status"] == "waiting_on_human"
        launch.assert_not_called()
        run = response.json()["run_id"]
        assert api.resolve(run, "gate").status_code == 200
        launch.return_value.returncode = 0
        launch.return_value.communicate.return_value = (
            json.dumps({"result": json.dumps({"status": "completed", "summary": "done"})}), "")
        resumed = client.post(f"/v1/workflows/{workflow}/execute")
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["run_id"] == run
    assert resumed.json()["status"] == "completed"


def test_workflow_stops_at_source_conversation(client, tmp_path):
    api = Api(client)
    source = task("source")
    source["execution"] = {
        "executor": "source_conversation", "model": "current", "prompt": "Do source work",
    }
    step = executable(tmp_path)
    step["depends_on"] = ["source"]
    workflow = api.approved_workflow([source, step])
    with patch("chief.domain.execution.subprocess.Popen") as launch:
        response = client.post(f"/v1/workflows/{workflow}/execute")
        assert response.status_code == 200, response.text
        assert response.json()["step_states"]["source"]["status"] == "pending"
        launch.assert_not_called()


def test_workflow_refuses_draft_or_unconfigured_plan(client, tmp_path):
    api = Api(client)
    draft = api.create_workflow([executable(tmp_path)]).json()["workflow_id"]
    assert client.post(f"/v1/workflows/{draft}/execute").status_code == 409
    workflow = api.approved_workflow([task("work")])
    assert client.post(f"/v1/workflows/{workflow}/execute").status_code == 422
    assert client.get(f"/v1/runs?workflow_id={workflow}").json() == []


def test_reopen_unused_workflow_requires_approval_again(client, tmp_path):
    api = Api(client)
    workflow = api.approved_workflow([task("work")])
    reopened = client.post(f"/v1/workflows/{workflow}/reopen")
    assert reopened.status_code == 200, reopened.text
    assert reopened.json()["status"] == "draft"
    assert reopened.json()["version"] == 2
    assert client.post(f"/v1/workflows/{workflow}/runs", json={}).status_code == 409
    assert client.post(f"/v1/workflows/{workflow}/execute").status_code == 409
    configured = executable(tmp_path)
    saved = client.put(f"/v1/workflows/{workflow}", json={
        "title": "Configured", "steps": [configured], "source": "human",
    })
    assert saved.status_code == 200, saved.text
    assert saved.json()["status"] == "draft"
    assert client.post(f"/v1/workflows/{workflow}/approve").status_code == 200
    assert client.post(f"/v1/workflows/{workflow}/runs", json={}).status_code == 201


def test_cannot_reopen_a_workflow_with_a_run(client):
    workflow, run = Api(client).run([task("work")])
    assert client.post(f"/v1/workflows/{workflow}/reopen").status_code == 409
    assert client.get(f"/v1/workflows/{workflow}").json()["status"] == "approved"
    assert client.get(f"/v1/runs/{run}").json()["base_version"] == 1


def test_streaming_publishes_live_activity_before_completion(client, tmp_path):
    import subprocess
    import sys
    import time
    from concurrent.futures import ThreadPoolExecutor

    _, run = Api(client).run([executable(tmp_path)])
    script = tmp_path / "fake_cli.py"
    script.write_text('''import json, sys, time
sys.stdin.read()
print(json.dumps({"type": "assistant", "message": {"id": "m1",
    "usage": {"input_tokens": 100, "output_tokens": 1}, "content": [
    {"type": "text", "text": "Checking the files…"},
    {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}
]}}), flush=True)
time.sleep(1.8)
print(json.dumps({"type": "result", "result": json.dumps({
    "status": "completed", "summary": "Done", "output": "## Result\\nAll files checked."
}), "usage": {"input_tokens": 100, "output_tokens": 20},
    "duration_ms": 1800, "total_cost_usd": .001}), flush=True)
''')
    real_launch = subprocess.Popen

    def start(command, **kwargs):
        assert "stream-json" in command and "--verbose" in command
        return real_launch([sys.executable, str(script)], **kwargs)

    with patch("chief.domain.execution.subprocess.Popen", side_effect=start), \
         ThreadPoolExecutor() as pool:
        future = pool.submit(client.post, f"/v1/runs/{run}/execute/work")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            state = client.get(f"/v1/runs/{run}").json()["step_states"]["work"]
            execution = state["metadata"].get("execution", {})
            if execution.get("live_text"):
                break
            time.sleep(.05)
        assert state["status"] == "running"
        assert execution["live_text"] == "Checking the files…"
        assert execution["activity"][0]["detail"] == "ls"
        assert execution["usage"]["tokens"] == {"input_tokens": 100}
        result = future.result(timeout=5).json()["step_states"]["work"]
    assert result["status"] == "completed"
    assert result["metadata"]["execution"]["output"] == "## Result\nAll files checked."
    assert result["metadata"]["execution"]["usage"]["total_cost_usd"] == .001
    assert result["metadata"]["execution"]["usage"]["tokens"]["total_tokens"] == 120
    assert "Checking the files" in result["metadata"]["execution"]["stdout"]


def test_codex_stream_readable_events_and_partial_lines():
    from chief.domain.execution_output import stream_output

    events = [
        {"type": "item.started", "item": {"type": "command_execution", "command": "pytest"}},
        {"type": "item.completed", "item": {"type": "command_execution", "command": "pytest",
                                            "aggregated_output": "3 passed"}},
        {"type": "item.completed", "item": {"type": "file_change",
                                            "changes": [{"path": "app.py"}]}},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "Tests pass."}},
        {"type": "error", "message": "Failed to connect"},
    ]
    parsed = stream_output("\n".join(map(json.dumps, events)) + '\n{"type":')
    assert parsed["live_text"] == "Tests pass."
    assert [event["title"] for event in parsed["activity"]] == [
        "Running command", "Command finished", "Command output", "Files updated", "CLI error"]
    assert parsed["activity"][2]["detail"] == "3 passed"
    assert parsed["envelope"] is None


def test_stream_ignores_unknown_or_malformed_events():
    from chief.domain.execution_output import stream_output

    raw = '\n'.join(map(json.dumps, [None, [], {"type": "assistant", "message": []},
        {"type": "assistant", "message": {"content": [None, {"type": "text", "text": 1}]}},
        {"type": "item.completed", "item": "unknown"},
        {"type": "item.completed", "item": {"type": "file_change", "changes": [None]}}]))
    assert stream_output(raw)["live_text"] == ""


@pytest.mark.parametrize("output,format_,expected", [
    ("# Report\nDone", None, "markdown"),
    ("<html><body><h1>Report</h1></body></html>", "html", "html"),
    ("exact plain text", "text", "text"),
    ({"summary": "A data field", "rows": [1, 2]}, None, "json"),
    ([{"score": .9}], "json", "json"),
])
def test_result_content_formats(client, tmp_path, output, format_, expected):
    _, run = Api(client).run([executable(tmp_path)])
    payload = {"status": "completed", "summary": "Done", "output": output}
    if format_:
        payload["output_format"] = format_
    with patch("chief.domain.execution.subprocess.Popen") as launch:
        launch.return_value.returncode = 0
        launch.return_value.communicate.return_value = (
            json.dumps({"result": json.dumps(payload)}), "")
        state = client.post(f"/v1/runs/{run}/execute/work").json()["step_states"]["work"]
    assert state["status"] == "completed"
    metadata = state["metadata"]["execution"]
    assert metadata["output_format"] == expected
    if expected == "json":
        assert json.loads(metadata["output"]) == output
    else:
        assert metadata["output"] == output


@pytest.mark.parametrize("output,format_", [("report", "unsupported"),
    ({"data": 1}, "markdown"), ("not valid JSON", "json")])
def test_invalid_result_content_format_fails_step(client, tmp_path, output, format_):
    _, run = Api(client).run([executable(tmp_path)])
    result = {"status": "completed", "summary": "Done", "output": output, "output_format": format_}
    with patch("chief.domain.execution.subprocess.Popen") as launch:
        launch.return_value.returncode = 0
        launch.return_value.communicate.return_value = (
            json.dumps({"result": json.dumps(result)}), "")
        state = client.post(f"/v1/runs/{run}/execute/work").json()["step_states"]["work"]
    assert state["status"] == "failed"


def test_missing_cli_preserves_pending_step_and_unstarted_plan(client, tmp_path, monkeypatch):
    monkeypatch.setattr("chief.domain.execution.shutil.which", lambda name: None)
    api = Api(client)
    workflow = api.create_workflow([executable(tmp_path)]).json()["workflow_id"]
    client.post(f"/v1/workflows/{workflow}/approve")
    response = client.post(f"/v1/workflows/{workflow}/execute")
    assert response.status_code == 422
    assert "PATH" in response.json()["error"]["message"]
    assert client.get("/v1/runs").json() == []
    _, run = api.run([executable(tmp_path)])
    assert client.post(f"/v1/runs/{run}/execute/work").status_code == 422
    state = client.get(f"/v1/runs/{run}").json()["step_states"]["work"]
    assert state["status"] == "pending"
    assert "execution" not in state["metadata"]


def test_missing_working_directory_does_not_create_run(client, tmp_path):
    step = executable(tmp_path / "missing")
    workflow = Api(client).create_workflow([step]).json()["workflow_id"]
    client.post(f"/v1/workflows/{workflow}/approve")
    assert client.post(f"/v1/workflows/{workflow}/execute").status_code == 422
    assert client.get("/v1/runs").json() == []


def test_model_catalog_exposes_only_visible_ids_and_labels(client, tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    (tmp_path / "models_cache.json").write_text(json.dumps({
        "identity": "private-account", "models": [
            {"slug": "local-model", "display_name": "Local model", "visibility": "list"},
            {"slug": "hidden", "visibility": "hide"}, None,
            {"slug": "local-model", "visibility": "list"},
            {"slug": 3, "visibility": "list"},
        ]}))
    response = client.get("/v1/execution/models")
    assert response.status_code == 200
    assert response.json()["codex"] == [{"id": "local-model", "label": "Local model"}]
    assert "private-account" not in response.text
    assert {m["id"] for m in response.json()["claude"]} == {"sonnet", "opus"}
    for contents in ("bad JSON", "[]", '{"models":null}'):
        (tmp_path / "models_cache.json").write_text(contents)
        assert client.get("/v1/execution/models").json()["codex"] == []
    (tmp_path / "models_cache.json").unlink()
    assert client.get("/v1/execution/models").json()["codex"] == []


def test_execution_identifiers_are_trimmed(client, tmp_path):
    step = executable(tmp_path)
    step["execution"].update(model=" sonnet ", cwd=f" {tmp_path} ")
    response = Api(client).create_workflow([step])
    assert response.status_code == 201
    config = response.json()["steps"][0]["execution"]
    assert config["model"] == "sonnet"
    assert config["cwd"] == str(tmp_path)


def test_claude_tool_results_are_readable_and_bounded():
    from chief.domain.execution_output import stream_output

    messages = [
        {"type": "user", "message": {"content": [{"type": "tool_result",
          "content": [{"type": "text", "text": "3 passed"}]}]}},
        {"type": "user", "message": {"content": [{"type": "tool_result",
          "content": "x" * 5000, "is_error": True}]}},
    ]
    activity = stream_output("\n".join(map(json.dumps, messages)))["activity"]
    assert activity[0] == {"kind": "output", "title": "Tool result", "detail": "3 passed"}
    assert activity[1]["title"] == "Tool failed"
    assert len(activity[1]["detail"]) == 4000
