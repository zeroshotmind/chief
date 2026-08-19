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

### When a plan is bigger than the window

A wide fan-out — twelve independent steps side by side — needs more width than any window
has. The graph draws at the width the plan actually needs and the viewport **scrolls
sideways**; it is not scaled down to fit, because a twelve-wide plan shrunk to a third is a
picture of a plan rather than one you can read.

Artifact descriptions wrap rather than ending in an ellipsis, and anything genuinely long —
a description over two lines, a markdown preview over nine — collapses with a **show more**
control. Nothing is ever collapsed without one.

`scripts/seed_stress.py` builds both shapes against a running server if you want to see them:

```bash
python scripts/seed_stress.py --base http://127.0.0.1:8080/v1
```

### Reading an artifact's file

**Click the path** and it opens in a drawer down the right. A URL artifact is **framed** —
your dev server renders it, Chief only shows it — and a file — markdown through the
is read by the server: markdown
through the renderer, images inline, PDFs in the browser's own viewer, **JSON as a tree you
can fold**, code and logs as text, anything else as a size and a download. Clicking the name of a thing to
see the thing is what a reader tries first, so that is what it does; the editor deep link is
the small **↗** beside it, since most of the time the question is "what is in this" rather
than "let me change it".

**The open file is in the URL** — `#/workflow/wf_wide/art_1957` — so a refresh lands back on
it and the link can be sent to someone. Nothing else about how you are looking at the page is:
filters, the selected node and the panel widths stay out, because they are *how* you are
looking rather than *where* you are. An id that names an artifact the run does not have is
dropped rather than retried, and the URL heals itself back to the workflow.

It opens down the **right** of the window, because a file is read *against* the run that
produced it and a panel along the bottom pushes the plan off the screen to do that. Drag its
left edge to resize — up to 70% of the window, so a wide log still has room — and the width is
remembered. The page is inset rather than covered, so the artifact list you opened it from
stays reachable for the next one.

**Chief's server reads the file**, which is the point: it works when you reach the UI through
an SSH tunnel from the machine the files are actually on. A browser-side file picker cannot
do that — it would read your laptop, not the host.

That is the one place Chief touches the filesystem, and it is deliberately narrow:

- **You never send a path.** The URL names a run and an artifact, both ids Chief issued, and
  the path comes from the artifact's own `ref` resolved against the workflow's `origin_dir`.
  There is no `?path=` to traverse. The only readable files are ones a harness already
  reported.
- **A relative ref cannot climb out** of the directory the workflow ran in.
- **Nothing is served under its own type.** The response is always opaque bytes; the type the
  browser may apply travels in a header and the page applies it itself. An `.html` or `.svg`
  artifact served under its own type from Chief's origin would be script running next to the
  run you are reading.
- **A `Host` header that is not loopback is refused**, which blocks DNS rebinding — a page on
  the open web pointing its own domain at `127.0.0.1` so a fetch looks same-origin. If you
  reach the UI under a name rather than `localhost`, allow it explicitly:

  ```bash
  chief --port 8080 --allow-host chief.internal
  ```
- **25MB cap**, and directories, devices and missing files are refused with a reason.

A workflow with no `origin_dir` cannot resolve a relative ref, and says so rather than
guessing. New plans record it themselves — the agent passes the directory it is working in
when it calls `create_workflow`. For anything planned before Chief asked for one, **set the
directory on the workflow detail screen** and its files become readable; that is the only
route, since a revision is deliberately not allowed to rewrite where the work happened.

### Markdown and maths

Artifact bodies, step summaries, comments and review notes are rendered rather than dumped
as one run-on line. **Newlines are kept** — a single one is a line break, not a space, because
a harness reporting a summary and a person typing in a textarea both mean the break they
typed. On top of that: headings, bold and italic, `code` and fenced blocks, lists,
blockquotes, links, and **LaTeX** between `$…$` or `$$…$$`.

The maths is translated to MathML and typeset by the browser. That is what makes it possible
without a dependency — no CDN tag on a loopback tool, and no 300KB of vendored KaTeX for text
that is usually three lines of prose.

**MDX** files render **with their components**, when the components sit beside them. Put
`Callout.jsx` next to `post.mdx`, import it as `./Callout`, and it compiles and runs —
hooks, state, event handlers and all. Chief ships a JSX transform and a small component
runtime (`jsx.js`, `mdx-runtime.js`) rather than React itself: what agent-written components
use is function components and five hooks, and that is a few hundred lines rather than a
vendored framework and a build step.

Two rules make it safe and bounded. The document's code runs in a **sandboxed frame at an
opaque origin** — it cannot reach the page reading the run, its API or its storage. And the
module graph is derived server-side from the file's own imports, confined to **its own
directory**: `./Chart` resolves, `../secret` and `/etc/passwd` are not resolved at all, and a
bare `react` is left to the runtime. The client still never names a path.

What it does not do: TypeScript (`.tsx`), npm packages, or components living elsewhere in a
repo behind a bundler alias. For an MDX page that belongs to a built site — one importing
`@/components/…` — report the URL your dev server serves it at instead; Chief frames that
page and you get the real thing. A document whose components cannot be found falls back to
prose with them named, rather than showing nothing.
Evaluating JSX out of an artifact would need a build step this project does not have and an
execution surface it does not want. So `<Callout>…</Callout>` becomes a framed block labelled
with its tag and props, with the prose inside it rendered as the markdown it is; a
self-closing `<Chart data={rows} />` becomes the same frame with nothing in it; `import` and
`export` lines fold away into one line you can open. You lose the styling and keep the
content, and you can see exactly what was there. **Frontmatter** is read as metadata rather
than as a horizontal rule followed by stray colons — in `.md` as well as `.mdx`.

The maths is a **subset**, and it says so when it meets something outside it: unrenderable maths is
shown as its own source on a marked background, so `$\begin{matrix}…$` reads as *not
rendered* rather than disappearing. Covered: fractions, roots, sub- and superscripts, sums and
integrals with limits, Greek, the usual relations and arrows, `\left…\right` fences and
manual sizing, `\text`/`\operatorname`, font commands, accents, `\underbrace`, and
environments — `matrix`, `cases`, `aligned` and friends — as tables. Not covered: macros, and
alignment within an environment.

The coverage is measured rather than asserted: every `$…$` and `$$…$$` in a corpus of 2230
real `.mdx` write-ups — 10,838 expressions — renders at **99.6%**, and the commands in
`scripts/test_markdown.mjs` were chosen by reading what came back as source, in frequency
order. Of the 39 that still do not, 38 are PromQL and shell snippets that were never maths.

Nothing is ever built as an HTML string — every node is created and its text set through
`textContent`. Artifact bodies come from outside, so that is a security property rather than a
style preference, and `scripts/test_markdown.mjs` asserts it directly.

### Sizing the panel

The panel on the right — run overview, step detail, artifacts, feedback — is dragged from
its **left edge**, and the width is remembered per browser. Double-click the grip to go back
to the default; with it focused, arrow keys move it (hold shift for bigger steps), because a
drag is not available to everyone.

It has a floor, so it cannot be collapsed to a sliver its cards cannot render in, and a
ceiling relative to the window, so a width saved on a large monitor does not swallow the
graph when the same browser opens on a laptop.

### Projects

Every workflow can carry a **project** — a short label like `chief` or `songs`. The agent
sets it when it plans, and the workflow list grows a row of chips to narrow by. Workflows
with no label are not hidden: they collect under **Unfiled**, which is where everything
planned before projects existed lives until you file it.

A project is deliberately **not a directory**. One product spans several checkouts and one
checkout carries work for more than one, so tying the two together would only ever be wrong
for somebody. What *is* recorded per workflow is `origin_dir` — where the agent was standing
when it planned — shown on the detail screen as *made in …*. It is a memory, not a live path:
nothing on the server resolves against it, and if the tree has moved it is simply wrong. Its
one job beyond the record is to offer itself as the **project folder** for opening artifacts,
in one click, which you can override.

File or refile a workflow from its detail screen, at any status — including ones that
finished long ago, which are exactly the ones worth filing.

Templates carry a project too, and a workflow made from one inherits it.

### Exporting a template to a project

A template detail screen has **Export to a file**. What comes out is exactly the body
`POST /templates` takes, id included, so you can commit it beside the code it describes and
register it again — on this machine or another — by posting it back. The file is written by
your browser, not by Chief: the server reads nothing off disk and writes nothing to it, which
is what keeps it a tracker rather than a file server.

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

`src/chief/web/` — five static files, no build step and no CDN, served by the same process
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
  web/        the UI: five static files, no build step
tests/        227 tests
scripts/      seed_demo.py, smoke_ui.mjs (headless UI check)
integrations/claude-code/   MCP registration + the skill that drives it
```

Invariants live in `service.py` rather than the route handlers, so the MCP surface got them
unchanged rather than reimplementing them. `tests/test_transport_parity.py` asserts that rather
than trusting it.

### Developing on it

```bash
pytest                          # 301 tests
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
