# Chief

**Your agents plan. You approve. Chief keeps the record.**

A local, single-user tracker for agentic workflows. An LLM harness — Claude Code, or
anything that can make an HTTP call — plans a workflow as a graph, you approve it, the
harness executes the steps and reports back, and when the plan stops fitting it proposes an
amendment that pauses the run until you decide.

Chief records what a harness plans and reports, and enforces approval rules. Optional
per-step CLI execution can launch Claude Code or Codex using settings approved with the plan.
Existing external execution and reporting continue to work unchanged.

→ **[zeroshotmind.github.io/chief](https://zeroshotmind.github.io/chief/)**

---

## Install

Needs **Python 3.11+** and **git**. Node is optional — only the UI checks use it.

```bash
git clone https://github.com/zeroshotmind/chief.git
cd chief
python3 -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"                              # drop [dev] to skip the test deps

chief --port 8080
```

Open <http://127.0.0.1:8080/>. That is the whole install — one process serves the REST API,
the web UI and the MCP endpoint, with no separate UI command and no build step.

| | |
|---|---|
| Web UI | `http://127.0.0.1:8080/` (`/ui/`) |
| REST API | `/v1/…`, also served unprefixed |
| OpenAPI docs | `/docs` |
| MCP endpoint | `/mcp/` |

**The database is one file.** `chief.sqlite3`, created on first run. Back it up by copying
it — but copy `chief.sqlite3`, `chief.sqlite3-wal` and `chief.sqlite3-shm` together, or stop
the server first, since SQLite keeps recent writes in the sidecar.

**There is no authentication.** This is a local single-user tool (REQ-44, REQ-45). Keep it on
loopback; `--host` exists, but anything other than `127.0.0.1` puts an unauthenticated API on
your network.

To see it with something in it, against an empty database:

```bash
python scripts/seed_demo.py --base http://127.0.0.1:8080/v1
```

## Connecting Claude Code or Codex

Two pieces doing different jobs, and **both are needed**: the MCP server is the capability
surface, the skill is the protocol that makes tracking worth having.

```bash
# 1. Register the MCP server (Chief must be running)
claude mcp add --transport http chief http://127.0.0.1:8080/mcp/ -s user

# 2. Install the skill
mkdir -p ~/.claude/skills/chief
ln -s "$PWD/integrations/SKILL.md" ~/.claude/skills/chief/SKILL.md
```

`-s user` rather than `project`: project scope writes a `.mcp.json` meant to be committed and
shared with a team, and Chief is single-user with no auth. The symlink means the skill tracks
the repo — the protocol it describes is enforced by the code next to it, and those two
drifting apart is the failure worth avoiding.

Full detail, including what Claude deliberately *cannot* do, is in
**[integrations/claude-code/](integrations/claude-code/README.md)**.

Codex works the same way — same server, same skill file, `codex mcp add chief --url
http://127.0.0.1:8080/mcp/` and a symlink into `~/.codex/skills/chief/`. Both agents share
one database, so a workflow planned by one is reportable by the other. Setup is in
**[integrations/codex/](integrations/codex/README.md)**.

## How it goes

Tracking is **opt-in per task**. Ask for it — "track this in Chief", or `/chief` — and:

1. **The agent plans.** One step per unit of work, each with a goal, the criteria that decide
   whether it is done, and the harness that will run it, ordered by explicit `depends_on`
   edges. It arrives as a **draft**.
2. **You approve — or say what is wrong.** A draft cannot take a run until you approve it.
   Leave **review notes** on any node and the agent reads them off the plan it fetches before
   revising, so nothing has to be repeated.
3. **The run stays honest.** Each step is reported as it starts and finishes. When the plan
   stops fitting, the agent proposes an **amendment** and the run pauses until you decide.
   Anything touching finished work is a **history edit** — always an explicit decision, never
   auto-approvable, and the original result is kept either way.

Along the way there are **checkpoints** that block until you answer in writing, **comments**
on the artifacts a run produced, a file viewer that renders markdown, maths, images and MDX,
**projects** to file workflows under, **templates** for plans worth reusing, and an
**approval policy** for the routine ones.

For work whose steps have real preconditions, there are **proof graphs** — workflows that
compile. A proof graph is a workflow graph whose every edge is a theorem: each step declares
what it needs from the ones before it, and the server proves — before anyone approves
anything — that every one of those demands is met by what feeds it. It stands on its own as
the checked definition of a process, and compiles into an ordinary draft workflow when you
want one. It says nothing about whether the work is any good; what it rules out is a process
that was never going to hold together.
**→ [lean/README.md](lean/README.md)**, and Lean is optional — without it everything else is
unchanged. Enabling it is one install (`elan`, the Lean version manager); the pinned
toolchain and the build follow from there on their own.

**→ [docs/using-chief.md](docs/using-chief.md)** covers all of it, and why each part behaves
the way it does.

## Working on it

```bash
pytest
ruff check src tests scripts
node scripts/smoke_ui.mjs                  # headless render of every UI screen
NO_TEMPLATES=1 node scripts/smoke_ui.mjs   # the same, against a server without /templates
node scripts/test_markdown.mjs             # the markdown and maths renderer, case by case
```

Python changes need the server restarted. Changes under `src/chief/web/` need only a browser
reload — the static files are served `no-cache`.

```
src/chief/
  models/     pydantic schemas for every contract object
  domain/     graph validation, path addressing, derivation, amendments, service logic
  storage/    SQLite document store + audit log
  api/        REST routes
  mcp_server.py   MCP tools, mounted at /mcp on the same app
  lean/       checking a plan's logic, and compiling it into a workflow
  web/        the UI: static files, no build step
lean/         the ProofGraph Lean prelude a proof graph is written against
site/         the landing page, published to GitHub Pages
```

Invariants live in `domain/service.py` rather than in route handlers, so the MCP surface got
them unchanged rather than reimplementing them — `tests/test_transport_parity.py` asserts
that rather than trusting it.

## Reading further

| | |
|---|---|
| [docs/using-chief.md](docs/using-chief.md) | Every feature, and why it works that way |
| [docs/internals.md](docs/internals.md) | Data model, derivation, amendments, design choices |
| [CONTRACT-NOTES.md](CONTRACT-NOTES.md) | Where implementation found the contract open or inconsistent |
| [lean/README.md](lean/README.md) | Checked plans: what is proven, what is not, and how to write one |
| [MCP-SURFACE.md](MCP-SURFACE.md) | Why the MCP tool list is not the contract's tool list |
| [STATUS.md](STATUS.md) | Requirement-by-requirement state, and the full route inventory |

## Optional per-step CLI execution

In **New workflow**, choose **Workflow execution** before starting the plan. Choose
Chief with Claude or Codex to launch CLI tasks, **Source conversation** to keep execution
in the current chat, or **Tracking only** for the existing reporting-only flow. Each task
with stored execution needs its own model and prompt; CLI tasks also need an absolute
working directory. The editor lists missing values and blocks Save until they are filled.
New tasks inherit executor/model defaults but always require a new prompt.

For an approved workflow that has never run, **Configure execution…** returns it to draft
and opens the editor. Choose the workflow execution mode, fill each task's settings, save,
and approve the new plan before executing it. Once any run exists, plan changes continue
through the amendment mechanism. The human UI's reopen endpoint is
`POST /v1/workflows/{workflow_id}/reopen`; it preserves the old version and audit history.

A task may declare an `execution` object during planning, before approval:

```json
{
  "id": "implement",
  "type": "task",
  "goal": "Implement the approved change",
  "harness": "codex",
  "execution": {
    "executor": "codex",
    "model": "your-model-id",
    "prompt": "Implement the change and verify the acceptance criteria.",
    "cwd": "/absolute/path/to/project",
    "timeout_seconds": 3600
  }
}
```

`executor` accepts `source_conversation`, `codex`, or `claude`. The legacy `cli` key
is still accepted. Model and prompt are stored for all three executors. With
`source_conversation`, the current agent executes and reports the step through the existing
reporting APIs; `cwd` is optional and Chief does not launch a CLI or switch that conversation's
model. The model names the planned model; the reporting harness can record the actual model
in step metadata. With `codex` or `claude`, model ids are passed directly to the installed CLI
and an absolute `cwd` is required. Executor choice is independent of who manages the workflow:
an external conversation can orchestrate CLI steps by calling `execute_step`.
The step panel’s **Execution settings** disclosure shows these settings and the complete
prompt before approval. Use its
**Execute step** button, the MCP `execute_step(run_id, path)` tool, or
`POST /v1/runs/{run_id}/execute/{step_id}` after approval. Nested tasks use an alternating
step/instance path, for example `loop/iteration_01/task`. The request waits for completion.
Each CLI invocation starts a new conversation without resuming or saving session history.
The CLI must be installed and authenticated on the Chief host. Codex uses its workspace-write
sandbox; Claude uses `dontAsk` permissions. Neither launch bypasses permissions.

After approval, **Execute workflow** starts a run and advances through ready CLI tasks in
dependency order. **Resume workflow** continues that same run after a blocker is resolved.
The matching API is `POST /v1/workflows/{workflow_id}/execute`; MCP exposes
`execute_workflow(workflow_id)`. Execution stops at failures, pending amendments, human
checkpoints, source-conversation tasks, and constructs that need externally supplied
instances. Active construct instances with configured tasks can be executed. Chief does
not invent loop/parallel instance parameters or decide loop exit conditions. Workflows
with multiple runs use the individual-step API. Existing externally managed workflows
continue to work without these actions.

Individual-step execution is also available. Dependencies must be completed and non-stale;
paused runs and duplicate launches are refused. The stored prompt receives the goal,
inputs, outputs, dependency results, instance metadata, and criteria as context. The CLI
must return a final JSON result with `status`, `summary`, and `criteria_met`, plus an optional
`output` with the complete result and `output_format` (`markdown`, `text`, `json`, or `html`); missing
criterion evidence, invalid output, timeouts, and CLI errors fail the step. Output, stderr,
exit code and execution id are retained in step metadata (output is size-limited).
In the editor, select a model from the required dropdown or choose **Custom model ID…**.
`GET /v1/execution/models` supplies visible models from the host's Codex cache and Claude
CLI aliases without reading credentials or contacting providers. Switching executor clears
the old model. Missing CLIs and working directories are caught before creating a new run.
Select a task and click **View execution** in its step inspector to open output on demand
in a resizable right drawer. During execution, streamed CLI messages and tool activity
update automatically. The step inspector shows the model, elapsed time, and available cost.
The drawer provides rendered
Preview, Source, Activity, Logs, and Files tabs. Markdown renders headings, tables, code,
math, and diagrams; JSON uses a folding tree; standalone HTML renders in a sandboxed frame.
Structured JSON objects/arrays are also accepted as `output`. Generated `artifacts` open in
the existing file viewer, with a return action to the step’s output. Copy and download act
on the result body. Completed pages stop refreshing automatically.

Retrying finished work uses the existing approved history-edit/replay flow. A server
interruption can leave a step running; reconcile it through the existing reporting flow.
Changing launch settings on an approved run uses the existing amendment mechanism.
Steps without `execution` retain the existing tracking-only behavior.
