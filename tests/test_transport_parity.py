"""The MCP surface and the REST API stay in correspondence (REQ-2, REQ-4).

Contract section 3 asked for one-to-one correspondence between tools and routes. That rule
cannot hold — seven routes are three path-parameterised service methods, and some routes are
deliberately not tools — so MCP-SURFACE.md replaces it with two rules that can:

* **Soundness** — every tool resolves to a ``Chief`` method some REST route also reaches.
  This is what REQ-4 protects: no transport acquires a private capability.
* **Coverage** — a tool exists for every operation an agent session legitimately performs.

Both are asserted here, by reading which service methods each transport calls. The check is
textual because that is what makes it total: it sees every call site in the module, so a
tool added without a route fails the build rather than drifting quietly.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import re
from pathlib import Path

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types.methods import KNOWN_PROTOCOL_VERSIONS

from chief import lean as chief_lean
from chief import mcp_server
from chief.api import routes
from chief.domain.service import Chief
from chief.mcp_server import HARNESS_OPERATIONS, build_mcp
from chief.storage import Store

CALL = re.compile(r"service\.(\w+)\(")

# REST-only by design (MCP-SURFACE.md 1). Named rather than derived, because the whole point
# is that omitting them is a decision someone made, not an accident to be inferred.
REST_ONLY = {
    "get_approval_policy",  # editing the policy that governs your own amendments is
    "put_approval_policy",  # self-approval — the loop REQ-13 exists to prevent
    "audit_entries",  # observer surface; no session behaviour is driven by reading it
    # Both comment channels run one way. A harness reads them — they ride on the documents
    # `get_run` and `get_workflow` already return — and writes neither. A session that could
    # write the feedback it was given, or close it, could tell itself its work was accepted.
    "comment_on_artifact",
    "add_review_note",
    "list_review_notes",
    "decide_review_note",
    # A harness states the project when it creates the plan. Re-filing one afterwards, and
    # reading the list of labels in use, are both housekeeping in front of a person.
    "label_workflow",
    "list_projects",
    # A harness wrote the file and can open it directly. This route exists for the browser,
    # which cannot.
    "artifact_content",
    "artifact_modules",
    # Permanent deletion of a plan and everything it ran. `approve_workflow` is a human
    # decision the harness may only make on an instruction in the turn; this one has no
    # legitimate agent-initiated form at all, so it does not get a tool.
    "delete_workflow",
}


def service_methods(module) -> set[str]:
    return set(CALL.findall(inspect.getsource(module)))


@pytest.fixture()
def service() -> Chief:
    store = Store(":memory:")
    yield Chief(store)
    store.close()


@pytest.fixture()
def mcp(service: Chief):
    return build_mcp(service)


@pytest.fixture()
def tools(mcp) -> dict:
    return {tool.name: tool for tool in asyncio.run(mcp.list_tools())}


def call(mcp, name: str, **arguments):
    result = asyncio.run(mcp.call_tool(name, arguments))
    if result.is_error:
        raise AssertionError(f"{name} failed: {result.content}")
    return result.structured_content


def call_expecting_error(mcp, name: str, **arguments) -> str:
    """Rejections surface either way: schema failures and domain failures both reach the
    caller through ``ToolError`` when the server is driven directly, and as an error result
    when it is driven over the wire. Accept both, and return the message either way."""
    try:
        result = asyncio.run(mcp.call_tool(name, arguments))
    except ToolError as exc:
        return str(exc)
    assert result.is_error, f"{name} was expected to fail"
    return " ".join(getattr(block, "text", "") for block in result.content)


# --- the two rules --------------------------------------------------------------------


def test_soundness_every_tool_resolves_to_a_method_rest_also_reaches():
    """Module-level, not per-tool: it asserts that the set of service methods the MCP module
    calls is a subset of the set the routes call. That is the drift worth catching — a tool
    reaching a capability no route exposes — but it would not catch a tool calling a method
    some *other* line in the module also legitimately calls."""
    called = service_methods(mcp_server)
    # Without this the check is vacuous: the regex keys on the name `service`, so renaming
    # that closure variable would leave an empty set, and the empty set is a subset of
    # everything. A green test that has stopped testing is worse than a red one.
    assert called, "found no service calls in mcp_server — has the regex stopped matching?"
    assert called <= service_methods(routes)


def test_coverage_every_harness_operation_has_a_tool(tools):
    """Internal consistency, not a check against the contract: ``HARNESS_OPERATIONS`` is
    hand-maintained. It catches a tool added or removed without the list moving with it,
    which is the drift that actually happens."""
    assert set(tools) == set(HARNESS_OPERATIONS)


def test_rest_only_operations_are_not_tools():
    assert service_methods(mcp_server) & REST_ONLY == set()


def test_rest_only_operations_really_are_reachable_over_rest():
    """The exclusions narrow the agent-facing subset, not the API. REQ-4 turns on this."""
    assert REST_ONLY <= service_methods(routes)


def test_the_documented_route_inventory_matches_the_router():
    """STATUS.md's appendix lists every route, and the count appears in four places.

    It has gone stale twice — templates and checkpoints were added without it moving, and
    the number in `mcp_server`'s docstring drifted separately. Both are the kind of error
    nobody notices by reading, so the router is the source and the docs are checked against
    it. `/healthz` is excluded: it is a liveness probe, not an operation.
    """
    # One entry per method+path, which is what the appendix lists — several paths answer to
    # two methods, so counting distinct paths gives a different (and previously documented)
    # number. `/healthz` is excluded: it is a liveness probe, not an operation.
    live = [
        (method, route.path.replace(":path", ""))
        for route in routes.router.routes
        if route.path != "/healthz"
        for method in sorted(route.methods - {"HEAD", "OPTIONS"})
    ]
    inventory = Path("STATUS.md").read_text()
    documented = set(re.findall(r"^(GET|PUT|POST|PATCH|DELETE)\s+/v1(\S+)", inventory, re.M))

    assert documented == set(live), (
        f"STATUS.md is missing {sorted(set(live) - documented)} "
        f"and lists {sorted(documented - set(live))} that no longer exist"
    )
    for doc in (inventory, inspect.getsource(mcp_server), Path("MCP-SURFACE.md").read_text()):
        assert f"{len(live)} routes" in doc or f"{len(live)} REST routes" in doc


def test_every_tool_advertises_a_usable_schema(tools):
    """A tool whose signature was lost advertises ``(*args, **kwargs)`` and is uncallable.

    ``_guard`` wraps every tool, and without ``functools.wraps`` the schema derives from the
    wrapper instead of the function. That failure is silent at import time.
    """
    for name, tool in tools.items():
        properties = set(tool.input_schema.get("properties", {}))
        assert not properties & {"args", "kwargs"}, f"{name} lost its signature"
        assert tool.description, f"{name} has no description"


MCP_HEADERS = {
    "accept": "application/json, text/event-stream",
    "content-type": "application/json",
    # The streamable-HTTP transport checks Host to block DNS rebinding, and rejects
    # TestClient's default "testserver" with a 421. Present as the loopback client the
    # server is bound for. The allowlist is host:port, so a bare host is rejected too.
    "host": "127.0.0.1:8080",
}


def rpc_payload(response) -> dict:
    """Read the JSON-RPC message, however this revision chose to frame it.

    Older revisions answer as an event stream, where the message is the ``data:`` line;
    2026-07-28 answers with a plain JSON body. Accepting both is the point — the test exists
    to check every revision a client might negotiate.
    """
    if response.headers.get("content-type", "").startswith("text/event-stream"):
        for line in response.text.splitlines():
            if line.startswith("data: "):
                return json.loads(line[len("data: ") :])
        raise AssertionError(f"no data frame in event stream: {response.text!r}")
    return response.json()


@pytest.mark.parametrize("version", KNOWN_PROTOCOL_VERSIONS)
def test_a_client_can_list_the_tools_at_every_protocol_version(client, version):
    """A client negotiating an older revision must still be able to list the tools.

    ``StepInstance`` is recursive, so pydantic emits its schema as a bare ``$ref`` with no
    ``type``, and the 2025-06-18 revision requires ``outputSchema.type``. Such a client then
    gets ``-32603`` back and loses *every* tool, because one bad schema sinks the whole
    ``tools/list`` response. A test against a current client does not catch it — which is how
    it reached a real one. So this drives the mounted endpoint, at each version the server
    claims to speak.
    """
    handshake = client.post(
        "/mcp/",
        headers=MCP_HEADERS,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": version,
                "capabilities": {},
                "clientInfo": {"name": "parity-test", "version": "1"},
            },
        },
    )
    assert handshake.status_code == 200, handshake.text
    session = {
        "mcp-session-id": handshake.headers["mcp-session-id"],
        "mcp-protocol-version": version,
        **MCP_HEADERS,
    }
    client.post(
        "/mcp/", headers=session, json={"jsonrpc": "2.0", "method": "notifications/initialized"}
    )

    listing = client.post(
        "/mcp/",
        # 2026-07-28 also requires the method named in a header, matching the body.
        headers={**session, "mcp-method": "tools/list"},
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/list",
            # The 2026-07-28 revision requires this envelope on every request. Older
            # revisions ignore it, so sending it always keeps one request shape for all.
            "params": {
                "_meta": {
                    "io.modelcontextprotocol/protocolVersion": version,
                    "io.modelcontextprotocol/clientCapabilities": {},
                }
            },
        },
    )
    payload = rpc_payload(listing)
    assert "error" not in payload, payload["error"]
    assert len(payload["result"]["tools"]) == len(HARNESS_OPERATIONS)


# --- the surface works ----------------------------------------------------------------


def plan() -> dict:
    return {
        "title": "ship it",
        "source": "generated",
        "generated_by": "claude_code",
        "steps": [
            {"id": "step_01", "type": "task", "goal": "write it", "harness": "claude_code"},
            {
                "id": "step_02",
                "type": "task",
                "goal": "test it",
                "harness": "claude_code",
                "depends_on": ["step_01"],
            },
        ],
    }


def test_the_documented_flow_runs_end_to_end(mcp):
    workflow = call(mcp, "create_workflow", body=plan())
    assert workflow["status"] == "draft"

    workflow_id = workflow["workflow_id"]
    call(mcp, "approve_workflow", workflow_id=workflow_id)
    run = call(mcp, "register_run", workflow_id=workflow_id, body={})
    run_id = run["run_id"]

    call(
        mcp,
        "report_step_update",
        run_id=run_id,
        path=["step_01"],
        body={"status": "completed", "summary": "wrote it"},
    )
    state = call(mcp, "get_run", run_id=run_id, include_plan=True)
    assert state["state"]["step_states"]["step_01"]["status"] == "completed"
    assert state["plan"]["steps"][0]["goal"] == "write it"


def test_a_draft_cannot_take_a_run(mcp):
    """REQ-32 is enforced in the service layer, so the MCP surface inherits it unchanged."""
    workflow_id = call(mcp, "create_workflow", body=plan())["workflow_id"]
    message = call_expecting_error(mcp, "register_run", workflow_id=workflow_id, body={})
    assert "invalid_transition" in message


def test_an_update_without_a_summary_is_refused(mcp):
    """REQ-48. Schema validation happens before the tool body runs."""
    workflow_id = call(mcp, "create_workflow", body=plan())["workflow_id"]
    call(mcp, "approve_workflow", workflow_id=workflow_id)
    run_id = call(mcp, "register_run", workflow_id=workflow_id, body={})["run_id"]
    call_expecting_error(
        mcp, "report_step_update", run_id=run_id, path=["step_01"], body={"status": "completed"}
    )


def test_an_amendment_pauses_the_run_and_waits(mcp):
    """REQ-13: proposing is not applying. The tool surface cannot shortcut that."""
    workflow_id = call(mcp, "create_workflow", body=plan())["workflow_id"]
    call(mcp, "approve_workflow", workflow_id=workflow_id)
    run_id = call(mcp, "register_run", workflow_id=workflow_id, body={})["run_id"]

    amendment = call(
        mcp,
        "propose_amendment",
        run_id=run_id,
        body={
            "proposed_by": "claude_code",
            "kind": "forward",
            "reason": "step_02 needs a fixture that does not exist yet",
            "operations": [
                {
                    "op": "insert_after",
                    "target_step_id": "step_01",
                    "step": {
                        "id": "step_03",
                        "type": "task",
                        "goal": "build the fixture",
                        "harness": "claude_code",
                    },
                }
            ],
        },
    )
    assert amendment["status"] == "pending_approval"
    assert call(mcp, "get_run", run_id=run_id)["state"]["status"] == "paused_for_approval"

    # The waiting question — "is anything of mine pending?" — is one call, not one per run.
    pending = call(mcp, "list_amendments", status="pending_approval")
    assert [entry["amendment_id"] for entry in pending["result"]] == [amendment["amendment_id"]]


def test_an_approval_records_the_transport_it_arrived_on(mcp, service):
    """MCP-SURFACE.md 4: a decision made over MCP stays distinguishable from one made in
    the UI, so 'the user told me to' is not silently recorded as 'the harness decided'."""
    workflow_id = call(mcp, "create_workflow", body=plan())["workflow_id"]
    call(mcp, "approve_workflow", workflow_id=workflow_id)

    # Read the audit log through the service — it is deliberately not a tool.
    entries = service.audit_entries(workflow_id=workflow_id)
    assert [entry["detail"]["via"] for entry in entries] == ["mcp", "mcp"]


def test_rest_calls_are_recorded_as_rest(client, api):
    """The context variable is per-call, not sticky. A REST client that never sets it must
    not inherit 'mcp' from whatever ran before it."""
    workflow_id = api.approved_workflow(
        [{"id": "step_01", "type": "task", "goal": "g", "harness": "h"}]
    )
    entries = client.get("/v1/audit", params={"workflow_id": workflow_id}).json()
    assert {entry["detail"]["via"] for entry in entries} == {"rest"}


def test_a_checkpoint_decided_over_mcp_says_so(mcp):
    """A harness relaying an answer the user gave it is legitimate — and has to stay
    distinguishable afterwards from a person deciding it in the UI themselves."""
    workflow = call(
        mcp,
        "create_workflow",
        body={
            "title": "p",
            "source": "generated",
            "steps": [
                {"id": "step_01", "type": "checkpoint", "goal": "ship it?", "harness": "human"}
            ],
        },
    )
    workflow_id = workflow["workflow_id"]
    call(mcp, "approve_workflow", workflow_id=workflow_id)
    run_id = call(mcp, "register_run", workflow_id=workflow_id, body={})["run_id"]

    call(
        mcp,
        "report_step_update",
        run_id=run_id,
        path=["step_01"],
        body={"status": "running", "summary": "reached the checkpoint"},
    )
    state = call(mcp, "get_run", run_id=run_id)["state"]
    assert state["step_states"]["step_01"]["status"] == "blocked"
    assert state["status"] == "waiting_on_human"

    # The outcome is not reportable, whatever the transport.
    message = call_expecting_error(
        mcp,
        "report_step_update",
        run_id=run_id,
        path=["step_01"],
        body={"status": "completed", "summary": "decided it myself"},
    )
    assert "resolve_checkpoint" in message

    call(
        mcp,
        "resolve_checkpoint",
        run_id=run_id,
        path=["step_01"],
        body={"decision": "approved", "decided_by": "roy", "note": "they said go"},
    )
    decided = call(mcp, "get_run", run_id=run_id)["state"]["step_states"]["step_01"]
    assert decided["status"] == "completed"
    assert decided["checkpoint"]["via"] == "mcp"
    assert decided["checkpoint"]["decided_by"] == "roy"


def test_instructions_name_the_flow_the_skill_documents():
    """The server's instructions are the one piece of text every session sees."""
    assert "propose_amendment" in mcp_server.INSTRUCTIONS
    assert json.dumps(HARNESS_OPERATIONS)  # the list is serialisable, i.e. plain strings


def test_every_field_a_harness_fills_says_what_to_put_in_it():
    """The tool schema is the only guidance a harness is guaranteed to see.

    ``summary`` went a long time without a description while ``metadata`` and
    ``criteria_met`` had careful ones, and the result was exactly what an undescribed
    required field invites: prose sized against the floor ("not 'Done'") because nothing
    stated a ceiling. The ceiling and the instruction to put detail in an artifact are the
    load-bearing half — assert both, so removing either fails here rather than in a run.
    """
    from chief.models.updates import StepUpdate

    described = StepUpdate.model_json_schema()["properties"]
    for field in ("summary", "metadata", "criteria_met"):
        assert described[field].get("description"), f"{field} reaches a harness undescribed"

    summary = described["summary"]["description"]
    assert "two or three sentences" in summary.lower()
    assert "artifacts" in summary


def open_mcp_session(client):
    """Handshake, and hand back a function that calls a tool and returns its parsed result."""
    handshake = client.post(
        "/mcp/",
        headers=MCP_HEADERS,
        json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18", "capabilities": {},
                "clientInfo": {"name": "parity-test", "version": "1"},
            },
        },
    )
    assert handshake.status_code == 200, handshake.text
    session = {**MCP_HEADERS, "mcp-session-id": handshake.headers["mcp-session-id"],
               "mcp-protocol-version": "2025-06-18"}
    client.post(
        "/mcp/", headers={**session, "mcp-method": "notifications/initialized"},
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
    )

    def call(name, arguments, request_id):
        response = client.post(
            "/mcp/", headers={**session, "mcp-method": "tools/call"},
            json={
                "jsonrpc": "2.0", "id": request_id, "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            },
        )
        payload = rpc_payload(response)
        assert "error" not in payload, payload["error"]
        return json.loads(payload["result"]["content"][0]["text"])

    return call


@pytest.mark.skipif(not chief_lean.available(), reason="no Lean toolchain on this machine")
def test_both_transports_spell_a_plan_graph_the_same_way(client):
    """REQ-4 at the level of a field name, which is where it is easiest to break.

    ``PlanGraph`` names its version marker ``schema_`` and aliases it back to ``schema``,
    because a bare ``schema`` would shadow a BaseModel attribute; ``PlanPort`` does the same
    for an artifact's field schema. FastAPI serialises response models by alias and
    pydantic's own ``model_dump`` does not, so a transport that reached for the latter would
    emit ``schema_`` where REST emits ``schema`` — the same document spelled two ways
    depending on how you asked for it. These two are the only aliases in the models, and the
    node-level equality below covers the port-level one, so this is the whole exposure and
    it is worth a test rather than a memory.
    """
    call = open_mcp_session(client)
    source = (chief_lean.package_dir() / "Examples" / "Pipeline.lean").read_text(encoding="utf-8")

    made = call("create_plan", {"body": {"title": "parity", "lean_source": source}}, 2)
    checked = call("verify_plan", {"plan_id": made["plan_id"]}, 3)

    assert checked["status"] == "verified", checked["verification"]["diagnostics"]
    over_mcp = checked["verification"]["graph"]
    over_rest = client.get(f"/v1/plans/{made['plan_id']}").json()["verification"]["graph"]
    assert sorted(over_mcp) == sorted(over_rest)
    assert "schema" in over_mcp
    assert over_mcp["nodes"] == over_rest["nodes"]
