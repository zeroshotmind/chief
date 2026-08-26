"""MCP surface (REQ-2), mounted on the same app as the REST API.

Thirty tools against 61 routes. The two are reconciled in MCP-SURFACE.md; in short, the
seven update/instance routes are three service methods each parameterised by a state path,
and the approval-policy config, the audit query, artifact comments, draft review notes and
both destructive deletes are deliberately REST-only — a session that can edit the policy
governing its own approvals can approve its own work, no session behaviour is driven by
reading the audit log, erasing a record is not a session's to initiate, and both comment
channels run one way: they are what a person says *to* a harness, which a harness
writing or closing its own would make meaningless. Both are readable here, and neither
needs a call of its own: comments ride on the artifacts in the state ``get_run`` returns,
review notes on the plan ``get_workflow`` returns.

Tool arguments and results are the REST bodies — the same pydantic models, not a parallel
set of shapes. The one exception is ``get_run``, which folds two routes into one tool and so
has to name which is which: it returns ``{"state": ..., "plan": ...}`` rather than a bare
document. Every other tool returns the model its route returns.

Every tool resolves to a method on :class:`~chief.domain.service.Chief` that a REST route
also reaches. That is the property ``tests/test_transport_parity.py`` asserts, and the
reason this module holds no logic: the invariants live in the service layer, so a second
transport inherits them unchanged rather than reimplementing them.

Transport: HTTP, mounted on the running FastAPI app. Not stdio — a stdio server is spawned
as a child process by its client, which would mean a second process on the same SQLite
file, and ``Store._lock`` is an in-process lock that does not cross that boundary.
"""

from __future__ import annotations

import functools
from typing import Any

from mcp.server.mcpserver import MCPServer

from .domain.service import Chief
from .errors import ChiefError
from .models import (
    Amendment,
    AmendmentCreate,
    AmendmentDecision,
    CheckpointResolution,
    InstanceCreate,
    InstanceUpdate,
    ProofGraph,
    ProofGraphCompile,
    ProofGraphCreate,
    ProofGraphRevise,
    RunCreate,
    RunState,
    StepInstance,
    StepUpdate,
    TemplateCreate,
    TemplateFromWorkflow,
    TemplateInstantiate,
    WorkflowCreate,
    WorkflowDefinition,
    WorkflowRevise,
    WorkflowTemplate,
)
from .transport import current_transport

INSTRUCTIONS = """\
Chief tracks the plan and the state of a workflow. It never executes a step — you do, and
you report what happened.

Check list_templates first: if a template already covers the work, create_workflow_from_template
with its parameters rather than composing a plan from scratch. Otherwise create_workflow.

Either way the result is a draft. Then: approve_workflow -> register_run -> report_step_update
as each step starts and finishes. If a step turns out not to be executable, propose_amendment
instead of improvising; the run pauses until a human decides.

A `checkpoint` step is one the plan says a person decides. Report it running to say you have
reached it — the run blocks there — then wait for their answer rather than deciding it.

Artifacts in a run's state may carry `comments`: things a person said about that output,
after the fact. Read them before building on the artifact they hang off — they are how you
find out a draft was rejected or a file is stale without being told again.

Report artifacts with the path you actually wrote to, and put what you know about each one in
its `data` — both are shown to a person, and the path is resolved so the file can be opened.
`metadata` on an update is shown too, and merges across updates; on a loop or parallel
instance it is what tells one branch from another.

A workflow carries `review_notes` the same way: feedback a reviewer left on the draft while
deciding whether to approve it, each on a step or on the plan as a whole. get_workflow before
revise_draft, address every note with `resolved: false`, and say in the revision's reason
which note each change answers. Marking one resolved is theirs, not yours — there is no tool
for it, and there is none for writing a note either.

A plan whose steps have real preconditions between them — conditions an earlier artifact
must satisfy, expensive to find broken halfway through — can be written as a proof graph
first: a Lean file against the ProofGraph prelude (documented in the server repo's lean/
directory) where each step declares what it demands and what it promises, and the server
checks that every promise meets the demand it feeds before anyone approves anything.
create_proof_graph -> verify_proof_graph -> read the diagnostics, revise_proof_graph, verify
again until it holds -> compile_proof_graph, which yields an ordinary draft workflow with
the proven conditions as its steps' inputs and criteria. A short errand does not need this.
A proof graph carries review_notes exactly as a workflow draft does: get_proof_graph returns
them, and revising the source is how you answer them.
"""

# Coverage under MCP-SURFACE.md 1 is asserted against this list: a tool exists for every
# operation an agent session legitimately performs, on its own initiative or on an explicit
# human instruction in the turn.
HARNESS_OPERATIONS = [
    "create_workflow",
    "revise_draft",
    "approve_workflow",
    "archive_workflow",
    "get_workflow",
    "list_workflows",
    "register_run",
    "get_run",
    "list_runs",
    "report_step_update",
    "resolve_checkpoint",
    "register_step_instance",
    "report_instance_update",
    "propose_amendment",
    "get_amendment",
    "list_amendments",
    "approve_amendment",
    "reject_amendment",
    "withdraw_amendment",
    "list_templates",
    "get_template",
    "create_template",
    "create_template_from_workflow",
    "create_workflow_from_template",
    "create_proof_graph",
    "list_proof_graphs",
    "get_proof_graph",
    "revise_proof_graph",
    "verify_proof_graph",
    "compile_proof_graph",
]


class ToolFailed(Exception):
    """A domain rejection, rendered for a tool caller.

    ``ChiefError`` carries an HTTP status the caller has no use for, and a bare exception
    message would lose the machine-readable code. Both survive here, because the difference
    between "not found" and "not legal right now" changes what the caller should do next.
    """


def _guard(fn):
    """Turn a domain rejection into a tool error that says what to do about it.

    ``functools.wraps`` is load-bearing, not cosmetic: the tool schema is derived from the
    signature, and it is ``__wrapped__`` that lets ``inspect.signature`` see through to the
    real one. Without it every tool advertises ``(*args, **kwargs)``.
    """

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        token = current_transport.set("mcp")
        try:
            return fn(*args, **kwargs)
        except ChiefError as exc:
            raise ToolFailed(f"{exc.code}: {exc.message}") from exc
        finally:
            current_transport.reset(token)

    return wrapper


def build_mcp(service: Chief, *, name: str = "chief") -> MCPServer:
    """Build the MCP server over an existing service instance.

    Takes the *same* ``Chief`` the REST app uses, so both transports share one ``Store``,
    one connection and one lock.
    """
    mcp = MCPServer(name=name, version="1.0.0", instructions=INSTRUCTIONS)
    tool = mcp.tool

    # --- workflows ------------------------------------------------------------------------

    @tool()
    @_guard
    def create_workflow(body: WorkflowCreate) -> WorkflowDefinition:
        """Submit a workflow plan. It is created as a **draft**.

        Every step needs a goal and the harness that will run it. Ordering is expressed with
        depends_on, never by position. Find the plan's shape rather than defaulting to a
        chain: independent steps you can name now fan out side by side (shared dependent to
        fan back in); retry / iterate-until work is a `loop` with the check as a body step
        and the exit condition in `exit_when`; a `parallel` construct is only for branches
        you cannot count until running. A draft cannot take a run until it is approved.

        On a loop or parallel step, declare `instance_params` — the names each iteration or
        branch must supply about itself, e.g. `paper`. Every instance is then required to
        give a value, and a body step may write `{{ paper }}` in its goal or criteria to be
        filled in per branch when the plan is read.

        Keep each goal to two or three lines. What decides whether a step is finished goes
        in its `criteria` — a list of short checkable statements — not into the goal's prose.
        Reporting that step `completed` then requires saying how each criterion was met, so
        write conditions you will be able to answer for.

        Set `origin_dir` to the absolute path of the directory you are working in, and
        `project` to the label for the body of work. Neither can be worked out here — this
        server runs in its own process and has no idea where you are — and without the
        directory the web UI cannot open a single file this run goes on to report.
        """
        return service.create_workflow(body)

    @tool()
    @_guard
    def revise_draft(workflow_id: str, body: WorkflowRevise) -> WorkflowDefinition:
        """Replace a draft's plan with a corrected one, in place.

        Use this whenever a draft you created is not yet approved and needs changing — a
        step in the wrong shape, a missed dependency, a loop that should have been a loop.
        Creating a second workflow instead leaves the reviewer to work out which draft
        supersedes which.

        The whole plan is replaced, so send every step you want to keep, not just the
        changed ones. Only a draft can be revised: once it is approved, changing the plan
        means proposing an amendment against its run.
        """
        return service.revise_draft(workflow_id, body)

    @tool()
    @_guard
    def approve_workflow(
        workflow_id: str, body: AmendmentDecision | None = None
    ) -> WorkflowDefinition:
        """Approve a draft so runs can register against it (REQ-32).

        A human decision. Call it only when the user has asked you to in this turn, never to
        unblock yourself. If they said why, pass it as the decision reason — that is the
        record of whose call it was.
        """
        return service.approve_workflow(workflow_id, body)

    @tool()
    @_guard
    def archive_workflow(
        workflow_id: str, body: AmendmentDecision | None = None
    ) -> WorkflowDefinition:
        """Retire a workflow. No new runs can register against it; runs in flight continue.

        Works on a draft too, which is how a plan that will never run is put away.
        """
        return service.archive_workflow(workflow_id, body)

    @tool()
    @_guard
    def get_workflow(workflow_id: str, version: int | None = None) -> WorkflowDefinition:
        """Read a workflow definition. Omit version for the current one."""
        if version is None:
            return service.get_workflow(workflow_id)
        return service.get_workflow_version(workflow_id, version)

    @tool()
    @_guard
    def list_workflows(status: str | None = None) -> list[WorkflowDefinition]:
        """List workflows, optionally by status (draft, approved, archived)."""
        return service.list_workflows(status)

    # --- templates ------------------------------------------------------------------------
    #
    # A workflow is single-use. Reuse is a template: the plan you keep, with parameters for
    # whatever made the original specific.

    @tool()
    @_guard
    def list_templates(status: str | None = None) -> list[WorkflowTemplate]:
        """Templates available to build a workflow from. Worth checking before planning
        from scratch — a template carries a plan someone already approved the shape of."""
        return service.list_templates(status)

    @tool()
    @_guard
    def get_template(template_id: str) -> WorkflowTemplate:
        """Read a template, including which parameters it needs."""
        return service.get_template(template_id)

    @tool()
    @_guard
    def create_workflow_from_template(
        template_id: str, body: TemplateInstantiate | None = None
    ) -> WorkflowDefinition:
        """Build a draft workflow from a template, supplying its parameters.

        Every required parameter must be given, and an unknown name is refused rather than
        ignored — a typo would otherwise silently leave a default in place. The result is a
        draft like any other; a policy may approve it automatically, but that is the policy's
        decision, not yours.
        """
        return service.instantiate_template(template_id, body or TemplateInstantiate())

    @tool()
    @_guard
    def create_template(body: TemplateCreate) -> WorkflowTemplate:
        """Write a reusable plan. Use {{ parameter_name }} in goals, harnesses and inputs;
        every placeholder must be declared in parameters."""
        return service.create_template(body)

    @tool()
    @_guard
    def create_template_from_workflow(
        workflow_id: str, body: TemplateFromWorkflow | None = None
    ) -> WorkflowTemplate:
        """Generalise a workflow that worked into a template.

        `substitutions` maps the literals that made it specific to parameter names, so
        {"acme/api": "repo"} turns that repo into a knob. Each becomes a parameter defaulting
        to the literal it replaced, so the template reproduces the original by default.
        """
        return service.create_template_from_workflow(workflow_id, body or TemplateFromWorkflow())

    # --- proof graphs ----------------------------------------------------------------------------
    #
    # `delete_proof_graph` is deliberately absent, for the same reason `delete_workflow` is: erasing
    # a record is not something a session needs to do on its own initiative.

    @tool()
    @_guard
    def create_proof_graph(body: ProofGraphCreate) -> ProofGraph:
        """Write a proof graph: a workflow graph whose every edge is a theorem, checked before
        anyone is asked to approve anything.

        `lean_source` is a whole Lean file written against the ProofGraph prelude, where each
        step declares what it needs from the ones before it. Checking it establishes that
        every one of those demands is met by what feeds it. Nothing is checked yet — this
        stores the graph as a draft; call `verify_proof_graph` next.

        Reach for this when the work has real preconditions between steps that would be
        expensive to discover halfway through. A short errand does not need it.
        """
        return service.create_proof_graph(body)

    @tool()
    @_guard
    def list_proof_graphs(
        status: str | None = None, project: str | None = None
    ) -> list[ProofGraph]:
        """Proof graphs on this server, newest first. Filter by status or project."""
        return service.list_proof_graphs(status=status, project=project)

    @tool()
    @_guard
    def get_proof_graph(graph_id: str) -> ProofGraph:
        """Read a proof graph: its source, the verdict on it, and the extracted graph."""
        return service.get_proof_graph(graph_id)

    @tool()
    @_guard
    def revise_proof_graph(graph_id: str, body: ProofGraphRevise) -> ProofGraph:
        """Replace a proof graph's source, usually to fix what verification reported.

        The verdict does not survive the edit — the graph goes back to draft and must be
        verified again. That is the point: a verdict belongs to the text that earned it.
        """
        return service.revise_proof_graph(graph_id, body)

    @tool()
    @_guard
    def verify_proof_graph(graph_id: str) -> ProofGraph:
        """Check the proof graph and record what came back.

        A graph that does not hold up is not an error — it comes back with `status: failed`
        and `verification.diagnostics` saying what failed and where. Read those, fix the
        source with `revise_proof_graph`, and verify again. The diagnostics name the exact condition
        that does not follow, with both sides in view, so they are worth reading closely
        rather than guessing from.

        The usual failure is a step demanding more than the step feeding it promised. Either
        strengthen the upstream promise or weaken the downstream demand — whichever is true
        of the work, not whichever makes the message go away.
        """
        return service.verify_proof_graph(graph_id)

    @tool()
    @_guard
    def compile_proof_graph(
        graph_id: str, body: ProofGraphCompile | None = None
    ) -> WorkflowDefinition:
        """Turn a verified proof graph into a draft workflow.

        Refused unless the graph is verified. The result is a draft like any other: it still
        needs `approve_workflow` and a run, and the conditions the graph proved travel with it
        as the steps' inputs and criteria.
        """
        return service.compile_proof_graph(graph_id, body or ProofGraphCompile())

    # --- runs -----------------------------------------------------------------------------

    @tool()
    @_guard
    def register_run(workflow_id: str, body: RunCreate) -> RunState:
        """Start tracking an execution of an approved workflow."""
        return service.register_run(workflow_id, body)

    @tool()
    @_guard
    def get_run(run_id: str, include_plan: bool = False) -> dict[str, Any]:
        """Read a run's state under "state". include_plan adds "plan": the definition it is
        executing.

        Use include_plan when resuming: the plan carries each step's goal, its harness and
        its dependencies, which is what tells you what to do next.
        """
        state = service.get_run(run_id)
        if not include_plan:
            return {"state": state.model_dump(mode="json")}
        return {
            "state": state.model_dump(mode="json"),
            "plan": service.get_run_plan(run_id).model_dump(mode="json"),
        }

    @tool()
    @_guard
    def list_runs(workflow_id: str | None = None, status: str | None = None) -> list[RunState]:
        """List runs, optionally filtered. How to find a run again after losing context."""
        return service.list_runs(workflow_id=workflow_id, status=status)

    # --- reporting ------------------------------------------------------------------------
    #
    # ``path`` addresses a step at any depth: ["step_03"] for a top-level step,
    # ["step_06", "inst_01", "step_09"] for a step inside a loop iteration or a parallel
    # branch. The contract's fixed depth-1 routes are the two- and three-token cases.

    @tool()
    @_guard
    def report_step_update(run_id: str, path: list[str], body: StepUpdate) -> RunState:
        """Report what happened to a step: status, artifacts, and a summary.

        The summary is required and must be human-readable (REQ-48) — it is what a person
        reads to know what happened and whether they need to open anything. Two or three
        sentences; findings, tables and comparisons belong in an artifact the summary
        points at, not in the summary itself.

        Before reporting `completed` on a step with criteria, check each one yourself — run
        it, open it, look — and pass `criteria_met` keyed by criterion id with a sentence of
        what satisfied each. If one does not hold the step is not finished: keep working, or
        report `failed`, or propose an amendment changing the criterion. Never write an
        answer for a criterion you have not actually checked.

        `completed` is refused while any criterion is unanswered, and the refusal names
        them. That is a backstop, not the checklist: it catches a criterion you said nothing
        about, never one you answered carelessly.

        A completed step is immutable. Changing one is a history_edit amendment, not an
        update, and it always needs an explicit human decision.
        """
        return service.report_step_update(run_id, path, body)

    @tool()
    @_guard
    def resolve_checkpoint(run_id: str, path: list[str], body: CheckpointResolution) -> RunState:
        """Record a person's decision at a checkpoint the run is blocked on.

        A human decision, like approving a workflow: call it only when the user has told you
        their answer in this turn, and pass what they actually said. Never to unblock
        yourself — a checkpoint exists precisely because this call is not yours to make.

        Reaching the checkpoint is your part: report it 'running', tell the user the run is
        waiting on them and what is being asked, then read the answer back from get_run.
        """
        return service.resolve_checkpoint(run_id, path, body)

    @tool()
    @_guard
    def register_step_instance(
        run_id: str, path: list[str], body: InstanceCreate | None = None
    ) -> StepInstance:
        """Open a loop iteration or a parallel branch on a loop/parallel step.

        Counts are never declared up front — register instances as the run produces them,
        as many as it turns out to need.

        If the step declares `instance_params`, pass a value for each in `metadata`; the
        instance is refused without them. That metadata is how you know which branch you are
        working on, and what a person reads to tell the branches apart, so put whatever else
        distinguishes it there too.
        """
        return service.register_instance(run_id, path, body or InstanceCreate())[1]

    @tool()
    @_guard
    def report_instance_update(
        run_id: str, path: list[str], instance_id: str, body: InstanceUpdate
    ) -> RunState:
        """Report on an instance as a whole. path addresses its construct step."""
        return service.report_instance_update(run_id, path, instance_id, body)

    # --- amendments -----------------------------------------------------------------------

    @tool()
    @_guard
    def propose_amendment(run_id: str, body: AmendmentCreate) -> Amendment:
        """Propose a change to the plan when a step is not executable as written (REQ-12).

        This is the move when reality diverges from the plan — not working around it and
        reporting success. The run pauses. Nothing is applied until a human approves
        (REQ-13), so expect to wait: poll get_amendment, or list_amendments with
        status="pending_approval".
        """
        return service.propose_amendment(run_id, body)

    @tool()
    @_guard
    def get_amendment(amendment_id: str) -> Amendment:
        """Read an amendment and its decision. How you find out whether you may proceed."""
        return service.get_amendment(amendment_id)

    @tool()
    @_guard
    def list_amendments(run_id: str | None = None, status: str | None = None) -> list[Amendment]:
        """List amendments across every run, or one run. status="pending_approval" is what
        is waiting on a human."""
        return service.list_amendments(run_id, status)

    @tool()
    @_guard
    def approve_amendment(
        amendment_id: str, body: AmendmentDecision | None = None
    ) -> Amendment:
        """Approve an amendment and resume the run on the amended plan (REQ-15).

        A human decision. Call it only on an explicit instruction in this turn — never to
        unblock your own proposal.
        """
        return service.approve_amendment(amendment_id, body or AmendmentDecision())

    @tool()
    @_guard
    def reject_amendment(amendment_id: str, body: AmendmentDecision | None = None) -> Amendment:
        """Reject an amendment. A human decision, as approve_amendment."""
        return service.reject_amendment(amendment_id, body or AmendmentDecision())

    @tool()
    @_guard
    def withdraw_amendment(amendment_id: str, reason: str | None = None) -> Amendment:
        """Retract your own proposal, if you proposed it and no longer want it."""
        return service.withdraw_amendment(amendment_id, reason)

    _normalise_output_schemas(mcp)
    return mcp


def _normalise_output_schemas(mcp: MCPServer) -> None:
    """Give every output schema an explicit ``type``.

    A recursive model — ``StepInstance`` contains ``StepState`` contains ``StepInstance`` —
    cannot be inlined, so pydantic emits the root as a bare ``{"$ref": ...}``. That is valid
    JSON Schema, and newer MCP protocol revisions accept it, but the 2025-06-18 revision
    requires ``outputSchema.type``, and a client negotiating that version gets
    ``-32603 Handler returned an invalid result`` for ``tools/list`` — every tool, not just
    the offending one, because the whole result fails to serialise.

    Adding ``type`` alongside ``$ref`` changes nothing about what validates: the referent is
    an object either way.
    """
    for tool in mcp._tool_manager._tools.values():
        schema = tool.output_schema
        if schema is not None and "type" not in schema and "$ref" in schema:
            schema["type"] = "object"
