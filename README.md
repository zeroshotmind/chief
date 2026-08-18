# Chief

A local, single-user backend that tracks agentic workflows. An LLM harness (Claude Code,
Claude Cowork, a local Gemma/Qwen model, anything that can make an HTTP call) plans a
workflow, a human approves it, harnesses execute the steps and report results back, and a
harness can propose human-approved amendments mid-run — inserting steps, editing the plan,
replaying a failed iteration.

Chief **never executes anything**. It records what a harness plans and what it reports, and
it enforces the rules about what may change and who has to say yes.

---

## Install on a new machine

Needs **Python 3.11+** and **git**. Node is optional — only the UI smoke test uses it.

```bash
git clone https://github.com/zeroshotmind/chief.git
cd chief

python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

pip install -e ".[dev]"            # drop [dev] if you will not run the tests
```

Check it works:

```bash
pytest                             # 227 tests
chief --port 8080
```

Open <http://127.0.0.1:8080/> — that is the whole install. One process serves the REST API,
the web UI and the MCP endpoint; there is no separate UI command, port or flag, and no
build step for the front end.

| What | Where |
|---|---|
| Web UI | `http://127.0.0.1:8080/` (`/ui/`) |
| REST API | `/v1/...`, also served unprefixed (REQ-22) |
| OpenAPI docs | `/docs` |
| MCP endpoint | `/mcp/` |
| Health check | `/healthz` |

**The database is one file.** `--db chief.sqlite3` by default, created on first run,
relative to your working directory. Back it up by copying it; move machines by copying it.
Note that SQLite keeps recent writes in a `-wal` sidecar, so copy `chief.sqlite3`,
`chief.sqlite3-wal` and `chief.sqlite3-shm` together, or stop the server first.

There is **no authentication** — this is a local single-user tool (REQ-44, REQ-45). Keep it
bound to loopback. `--host` exists, but anything other than `127.0.0.1` puts an unauthenticated
API on your network.

### Keeping it running

Nothing here daemonises. Run it in a terminal, or under whatever supervisor you already use
— `launchd` on macOS, `systemd --user` on Linux. It holds no state beyond the SQLite file,
so restarting it is always safe.

### Seeing it with something in it

The demo seeds fixed ids so it is reproducible, so it wants an empty database — against a
populated one it skips what is already there:

```bash
python scripts/seed_demo.py --base http://127.0.0.1:8080/v1
```

---

## Connecting Claude Code

Two pieces doing different jobs. **Both are needed**: the MCP server is the capability
surface, and the skill is the protocol that makes tracking worth having.

**1. Register the MCP server** (Chief must be running):

```bash
claude mcp add --transport http chief http://127.0.0.1:8080/mcp/ -s user
```

`-s user` rather than `project`: project scope writes a `.mcp.json` meant to be committed
and shared with a team, and Chief is single-user with no auth. Verify with `claude mcp list`,
or ask Claude to call `list_workflows`.

**2. Install the skill:**

```bash
mkdir -p ~/.claude/skills/chief
ln -s "$PWD/integrations/claude-code/SKILL.md" ~/.claude/skills/chief/SKILL.md
```

A symlink so it tracks the repo — the protocol it describes is enforced by the code next to
it, and the two drifting apart is the failure worth avoiding. Copy it instead if you would
rather pin it. For one repo rather than globally, put it at `.claude/skills/chief/SKILL.md`
in that repo.

Full detail, including what Claude deliberately cannot do, is in
**[integrations/claude-code/](integrations/claude-code/README.md)**.

---

## Using it

### Tracking a piece of work

Tracking is **opt-in per task**, not a default. Ask for it — "track this in Chief", or `/chief`
— and the agent plans first: one step per unit of work, each with a goal and the harness that
will run it, ordered by explicit `depends_on` edges rather than by position.

That plan arrives as a **draft**, and a draft cannot take a run until you approve it. This is
the point of the tool. Read the graph in the UI, then approve it — or don't, and say what is
wrong (see below). Once approved the agent registers a run and reports each step as it starts
and finishes.

### Review notes — saying what is wrong with a draft

A plan you are not ready to approve is the normal case, and "say what is wrong" should not mean
typing it into a chat window Chief cannot see. Every workflow takes **review notes**.

**Click a node in the graph and the thread for it opens beside the plan**, with a box to add
to it — the same shape as commenting on a post. The box is a resizable textarea, because a
useful note is usually a sentence or two; Enter gives you a newline and ⌘/Ctrl+Enter sends. A node carrying feedback shows a 💬 count, so you can see what
has something to read without opening every one.

For feedback about the plan itself — "this is a chain and it should fan out" — there is a
**Feedback on the plan** button beside Approve, carrying its own count. That is also where a
note goes when the step it was left on is removed by a revision.

The agent reads them off the plan it fetches before revising — no extra call, and nothing to
repeat. Once it has revised, mark the notes it answered **resolved**; they fold away behind a
"resolved (n)" toggle, so what is still open stays readable through several rounds. Resolving
is yours alone, as writing is: a session that could close the feedback it was given could
decide its own work had been accepted.

If a revision removes the step a note was on, the note is **not** dropped and **not** quietly
resolved. It moves to the plan's thread — there is no node left to open it from — reading *was
on step_04: draft the migration script*, the goal that step had when you wrote the note. The
step disappearing might mean you were listened to, or might mean the plan was restructured
around you, and only you can tell those apart.

Nothing here is enforced. A draft with open notes can still be approved, and a revision that
orphans a note is not refused — Chief records the decision, it does not make it.

### When the plan stops fitting

A step the agent cannot execute as written is the case Chief exists for. Instead of improvising
around it, the agent proposes an **amendment** — insert a step, change one, remove one, replay a
failed iteration — and **the run pauses until you decide.** Pending amendments show up in the
approvals inbox, with the proposed changes drawn into the plan graph as dashed ghost nodes, so
you review the plan you are approving rather than a patch document.

Anything touching a step that already finished is a **history edit**: it always needs an explicit
decision, no policy can auto-approve it, and the original result is kept either way.

### Checkpoints — making the agent wait for you

Sometimes you want the run to stop and ask, without waiting for it to hit a problem. A
`checkpoint` is a step type whose harness is a person. The agent reports reaching it, the run
blocks, and it waits. A checkpoint can also declare **fields** — things it asks you for in
writing ("what is the budget?", "which variant?") — and your answers are recorded on the run
where the agent reads them back.

Approving completes the step. Rejecting fails it, which skips everything downstream — a
rejected checkpoint stops that branch of the plan rather than quietly letting it proceed. A
rejection needs a note saying why; approving does not.

### Comments — telling the agent something about work that is done

Every artifact a run produces takes **comments**. "This draft is the one, match its tone."
"The numbers in here are stale." They hang off the artifact and ride on the run state the
agent already fetches when it picks the work up, so nothing has to be repeated and no tool
call is needed to find them.

Comments are yours to write, not the agent's — a harness annotating its own output with its
own opinion of it is what the step summary is already for. They are append-only. Review notes
are the same channel one step earlier: a comment is said about work that is done, a note about
work that has not started.

### Opening the files a run produced

Artifacts are references, not blobs (REQ-46) — a harness reports `songs/personas.md`, relative
to wherever it was working. Chief does not record that directory, so **set a project folder**
in the artifacts panel: paths then resolve into editor links (`vscode://file/...`) and the copy
button hands you the full absolute path. The setting lives in your browser, so the same run
opened on another machine resolves against that machine's checkout.

### Templates

A workflow is single-use — approved once, executed once — so reuse lives in **templates**: a
plan with `{{ parameters }}` in it. Instantiating one produces a draft workflow, which still
needs approving. You can also turn a workflow you already ran into a template.

### Approval policy

If approving every routine plan by hand gets tiring, the approval policy can auto-approve
matching workflows and forward amendments. It is edited in the UI at `/ui`, deliberately not
through any agent-facing tool: a session that could edit the policy governing its own approvals
could approve its own work. History edits can never be auto-approved, and that is checked when
the policy is written rather than when a decision is made.

---

## The web UI

`src/chief/web/` — four static files, no build step and no CDN, served by the same process
(REQ-21). It is a pure API client (REQ-18): a workflow list, a detail screen that draws the
plan as a dependency graph with per-instance state and artifacts, an approvals inbox covering
both pending amendments and blocked checkpoints, and the decision controls.

`/ui/?api=http://other-host:8080/v1` points it at a Chief running elsewhere.

---

## The shape of it

Implements the *Chief API & Data Contract v1*: sections 1 (data model), 2 (REST) and 3 (MCP).
The MCP surface is a transport wrapper, not a second implementation — every tool is a method on
`Chief` in `domain/service.py`, which is where the invariants live. Section 3's tool list could
not be built as written; the reconciliation is in **[MCP-SURFACE.md](MCP-SURFACE.md)**.

Places where implementation surfaced something the contract left open, ambiguous or inconsistent
are written up in **[CONTRACT-NOTES.md](CONTRACT-NOTES.md)**. Read that alongside the contract;
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
  web/        the UI: four static files, no build step
tests/        227 tests
scripts/      seed_demo.py, smoke_ui.mjs (headless UI check)
integrations/claude-code/   MCP registration + the skill that drives it
```

Invariants live in `service.py` rather than the route handlers, so the MCP surface got them
unchanged rather than reimplementing them. `tests/test_transport_parity.py` asserts that rather
than trusting it.

### Developing on it

```bash
pytest                          # 227 tests
ruff check src tests scripts
node scripts/smoke_ui.mjs       # headless render of every UI screen; needs node
NO_TEMPLATES=1 node scripts/smoke_ui.mjs   # same, against a server without /templates
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
