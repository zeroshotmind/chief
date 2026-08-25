# How Chief is built

The data model, what the server derives, how amendments work, and the decisions behind
them. For the contract this implements, see [CONTRACT-NOTES.md](../CONTRACT-NOTES.md) and
[MCP-SURFACE.md](../MCP-SURFACE.md).

---

## The data model

Implements the *Chief API & Data Contract v1*: sections 1 (data model), 2 (REST) and 3 (MCP).
The MCP surface is a transport wrapper, not a second implementation — every tool is a method on
`Chief` in `domain/service.py`, which is where the invariants live. Section 3's tool list could
not be built as written; the reconciliation is in **[MCP-SURFACE.md](../MCP-SURFACE.md)**.

Places where implementation surfaced something the contract left open, ambiguous or inconsistent
are written up in **[CONTRACT-NOTES.md](../CONTRACT-NOTES.md)**. Read that alongside the contract;
several entries need a doc change.

Two documents, joined by step id, kept apart on purpose (REQ-38) so a harness can review or
amend a plan without reading execution state:

- **WorkflowDefinition** — the static plan. Steps have permanent ids, a goal, an assigned
  harness, and explicit `depends_on` edges. `loop` and `parallel` steps carry a `body` of child
  step ids; the iteration/branch count is *not* fixed at plan time.
- **RunState** — one execution. A `StepState` per top-level step; a `StepInstance` per loop
  iteration or parallel branch, registered on demand at runtime; and inside each instance, a
  `StepState` per step of the body, so a three-step iteration shows real per-step progress
  rather than one opaque status.

Harnesses register their own runs — Chief is a passive record-keeper, not a scheduler.

### What the server derives, and never accepts as input

This is most of the interesting logic, and it all lives in `domain/derive.py`:

- A **loop/parallel step's status** comes from its instances. It cannot complete until the
  harness sets `instances_closed` — "every instance so far is done" and "no more are coming"
  are different claims.
- An **instance's status** comes from the states of the steps in its body, by the same rule.
- **`skipped`** is server-only. It is applied down a failed dependency chain — otherwise a run
  sits `running` forever after a failure — and *retracted* if the failure that caused it is
  later replayed away, so a run never reports `completed` for steps that never ran.
- **`blocked`** is what a checkpoint enters when the harness reports reaching it. It is not
  reportable and not terminal; `blocked` anywhere in the tree surfaces as the run status
  `waiting_on_human`.
- A **run** is `completed` once every top-level step is completed or skipped, `failed` if any
  failed.

`on_instance_failure: continue` on a construct tolerates a failed iteration so the rest still
count. Failure still propagates *within* the iteration; it just doesn't travel up.

Recomputation is a full bottom-up pass after every write. At single-user scale that costs
nothing and removes the class of bug where an update path forgets to refresh an ancestor.

### Amendments

A harness proposes a patch — `insert_after`, `insert_before`, `update_step`, `remove_step`,
`replay_step` — against step ids, not a resubmitted document. The run pauses, a human decides,
and the whole operation set applies atomically or not at all.

Two kinds:

- **`forward`** touches only the not-yet-executed plan.
- **`history_edit`** alters or re-runs something already `completed` or `failed`. Required the
  moment an operation would do that, checked mechanically at submission so a malformed proposal
  never reaches a human. The prior result is snapshotted into `history` before anything is
  overwritten, and a history edit can *never* be auto-approved by policy.

`replay_step` scoped to an `instance_id` replays one failed iteration rather than the whole
loop, which is the case REQ-41 exists for.

Each run pins the definition version it started from and applies only its own approved
amendments. Two concurrent runs on one workflow do not drag each other onto plans they never
approved.

Every transition — creation, approval, archive, step update, instance registration, checkpoint
resolution, artifact comment, amendment proposed/approved/rejected/withdrawn, policy write —
lands in an append-only audit log with a timestamp (REQ-20), readable at `GET /audit`.

---

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
  web/        the UI: five static files, no build step
tests/        the suite
scripts/      seed_demo.py, smoke_ui.mjs (headless UI check)
integrations/   the shared SKILL.md that drives an agent, + per-client setup (claude-code/, codex/)
```

Invariants live in `service.py` rather than the route handlers, so the MCP surface got them
unchanged rather than reimplementing them. `tests/test_transport_parity.py` asserts that rather
than trusting it.

### Developing on it

```bash
pytest
ruff check src tests scripts
node scripts/smoke_ui.mjs       # headless render of every UI screen; needs node
NO_TEMPLATES=1 node scripts/smoke_ui.mjs   # same, against a server without /templates
node scripts/test_markdown.mjs  # the markdown and maths renderer, case by case
```

Python changes need the server restarted. Changes to `src/chief/web/` need only a browser
reload — the static files are served with `no-cache`, so a reload always picks them up.

---

## Choices worth knowing about

**Python + FastAPI + SQLite.** The contract leaves language and storage open. This is a local
single-user tool that has to be trivially self-hostable (REQ-21) and is mostly schema validation
and state-machine logic: pydantic gives the "validated before acceptance" requirement (REQ-34)
directly, FastAPI generates the OpenAPI spec third-party clients need (REQ-4, REQ-22), and
SQLite means one file, no daemon, back up by copying.

**MCP over HTTP, not stdio.** A stdio server is spawned as a child process by its client, which
would put a second process on the same SQLite file — and the store's lock is in-process and does
not cross that boundary.

**Nested constructs are addressable.** The contract allows a loop inside a parallel branch but
its endpoints only reach one level down. Run state is addressed here by a path of alternating
step and instance ids; the contract's routes are the short cases of the same resolver. See
CONTRACT-NOTES.md #6.

**Full recompute over incremental updates.** Correctness over throughput, at a scale where
throughput is not a concern.
