# Chief

A local, single-user backend that tracks agentic workflows. LLM harnesses (Claude Code,
Claude Cowork, local Gemma/Qwen models, anything that can make an HTTP call) plan a
workflow, a human approves it, harnesses execute the steps and report results back, and a
harness can propose human-approved amendments mid-run — inserting steps, editing the plan,
replaying a failed iteration.

Chief **never executes anything**. It records what a harness plans and what it
reports, and it enforces the rules about what may change and who has to say yes.

Implements the *Chief API & Data Contract v1*: sections 1 (data model), 2 (REST) and 3
(MCP). The MCP surface is a transport wrapper, not a second implementation — every tool is
a method on `Chief` in `domain/service.py`, which is where the invariants live.
Section 3's tool list could not be built as written; the reconciliation is in
**[MCP-SURFACE.md](MCP-SURFACE.md)**.

Places where implementation surfaced something the contract left open, ambiguous or
inconsistent are written up in **[CONTRACT-NOTES.md](CONTRACT-NOTES.md)**. Read that
alongside the contract; several entries need a doc change.

## Running it

```bash
pip install -e ".[dev]"
chief --db chief.sqlite3 --port 8080
```

One process serves everything — there is no separate UI command, port or flag. The web UI
is at `http://127.0.0.1:8080/` (`/ui/`), OpenAPI docs at `/docs`, and the MCP endpoint at
`/mcp/`. To drive Chief from Claude Code, see
**[integrations/claude-code/](integrations/claude-code/README.md)**. Routes are served both at
`/v1/...` and unprefixed (REQ-22). No auth — this is a local single-user tool (REQ-44,
REQ-45).

To see it with something in it, seed a few demo runs through the API. The ids are fixed so
the demo is reproducible, so it wants an empty database — against a populated one it skips
what is already there:

```bash
python scripts/seed_demo.py --base http://127.0.0.1:8080/v1
```

```bash
pytest          # 138 tests
ruff check .
```

## The web UI

`src/chief/web/` — four static files, no build step and no CDN, served by the same process
(REQ-21). It is a pure API client (REQ-18): a runs list, a run detail that draws the plan
as a dependency graph with per-instance state and artifacts, an approvals inbox, and the
approve/reject decision. A pending amendment is drawn into the graph — proposed insertions
appear as dashed ghost nodes — so the reviewer sees the plan they are approving rather than
a patch document.

`/ui/?api=http://other-host:8080/v1` points it at a Chief running elsewhere.

## The shape of it

Two documents, joined by step id, kept apart on purpose (REQ-38) so a harness can review or
amend a plan without reading execution state:

- **WorkflowDefinition** — the static plan. Steps have permanent ids, an auto-generated
  goal, an assigned harness, and explicit `depends_on` edges. `loop` and `parallel` steps
  carry a `body` of child step ids; the iteration/branch count is *not* fixed at plan time.
- **RunState** — one execution. A `StepState` per top-level step; a `StepInstance` per loop
  iteration or parallel branch, registered on demand at runtime; and inside each instance, a
  `StepState` per step of the body, so a three-step iteration shows real per-step progress
  rather than one opaque status.

A workflow is created as `draft` and needs explicit human approval before any run can be
registered against it. Harnesses register their own runs — Chief is a passive
record-keeper, not a scheduler.

### What the server derives, and never accepts as input

This is most of the interesting logic, and it all lives in `domain/derive.py`:

- A **loop/parallel step's status** comes from its instances. It cannot complete until the
  harness sets `instances_closed` — "every instance so far is done" and "no more are
  coming" are different claims.
- An **instance's status** comes from the states of the steps in its body, by the same rule.
- **`skipped`** is server-only. It is applied down a failed dependency chain — otherwise a
  run sits `running` forever after a failure — and *retracted* if the failure that caused it
  is later replayed away, so a run never reports `completed` for steps that never ran.
- A **run** is `completed` once every top-level step is completed or skipped, `failed` if
  any failed.

`on_instance_failure: continue` on a construct tolerates a failed iteration so the rest
still count. Failure still propagates *within* the iteration; it just doesn't travel up.

Recomputation is a full bottom-up pass after every write. At single-user scale that costs
nothing and removes the class of bug where an update path forgets to refresh an ancestor.

### Amendments

A harness proposes a patch — `insert_after`, `insert_before`, `update_step`, `remove_step`,
`replay_step` — against step ids, not a resubmitted document. The run pauses, a human
decides, and the whole operation set applies atomically or not at all.

Two kinds:

- **`forward`** touches only the not-yet-executed plan.
- **`history_edit`** alters or re-runs something already `completed` or `failed`. Required
  the moment an operation would do that, checked mechanically at submission so a malformed
  proposal never reaches a human. The prior result is snapshotted into `history` before
  anything is overwritten, and a history edit can *never* be auto-approved by policy — that
  is checked when the policy is written, not when a decision is made.

`replay_step` scoped to an `instance_id` replays one failed iteration rather than the whole
loop, which is the case REQ-41 exists for.

Each run pins the definition version it started from and applies only its own approved
amendments. Two concurrent runs on one workflow do not drag each other onto plans they
never approved.

Every transition — creation, approval, archive, step update, instance registration,
amendment proposed/approved/rejected/withdrawn, policy write — lands in an append-only
audit log with a timestamp (REQ-20), readable at `GET /audit`.

## Layout

```
src/chief/
  models/     pydantic schemas for every contract object
  domain/
    graph.py        structural validation of a plan (REQ-34..REQ-37)
    paths.py        addressing state at any nesting depth
    derive.py       everything the server derives
    patch.py        amendment classification, application, state effects
    policy_eval.py  approval-policy expression language
    service.py      business logic; every invariant is enforced here
  storage/    SQLite document store + audit log
  api/        REST routes
  mcp_server.py MCP tools (REQ-2), mounted at /mcp on the same app
  transport.py  which transport the current call arrived on
tests/        150 tests
integrations/claude-code/   MCP registration + the skill that drives it
```

Invariants live in `service.py` rather than the route handlers, so the MCP surface got them
unchanged rather than reimplementing them. `tests/test_transport_parity.py` asserts that
rather than trusting it.

## Choices worth knowing about

**Python + FastAPI + SQLite.** The contract leaves language and storage open. This is a
local single-user tool that has to be trivially self-hostable (REQ-21) and is mostly
schema validation and state-machine logic: pydantic gives the "validated before acceptance"
requirement (REQ-34) directly, FastAPI generates the OpenAPI spec third-party clients need
(REQ-4, REQ-22), and SQLite means one file, no daemon, back up by copying.

**Nested constructs are addressable.** The contract allows a loop inside a parallel branch
but its endpoints only reach one level down. Run state is addressed here by a path of
alternating step and instance ids; the contract's routes are the short cases of the same
resolver. See CONTRACT-NOTES.md #6.

**Full recompute over incremental updates.** Correctness over throughput, at a scale where
throughput is not a concern.
