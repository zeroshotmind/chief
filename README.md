# Chief

**Your agents plan. You approve. Chief keeps the record.**

A local, single-user tracker for agentic workflows. An LLM harness — Claude Code, or
anything that can make an HTTP call — plans a workflow as a graph, you approve it, the
harness executes the steps and reports back, and when the plan stops fitting it proposes an
amendment that pauses the run until you decide.

Chief **never executes anything**. It records what a harness plans and what it reports, and
it enforces the rules about what may change and who has to say yes.

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

## Connecting Claude Code

Two pieces doing different jobs, and **both are needed**: the MCP server is the capability
surface, the skill is the protocol that makes tracking worth having.

```bash
# 1. Register the MCP server (Chief must be running)
claude mcp add --transport http chief http://127.0.0.1:8080/mcp/ -s user

# 2. Install the skill
mkdir -p ~/.claude/skills/chief
ln -s "$PWD/integrations/claude-code/SKILL.md" ~/.claude/skills/chief/SKILL.md
```

`-s user` rather than `project`: project scope writes a `.mcp.json` meant to be committed and
shared with a team, and Chief is single-user with no auth. The symlink means the skill tracks
the repo — the protocol it describes is enforced by the code next to it, and those two
drifting apart is the failure worth avoiding.

Full detail, including what Claude deliberately *cannot* do, is in
**[integrations/claude-code/](integrations/claude-code/README.md)**.

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

For work whose steps have real preconditions, there are **checked plans**. A plan is written
so each step declares what it needs from the ones before it, and the server proves — before
anyone approves anything — that every one of those demands is met by what feeds it. It then
compiles into an ordinary draft workflow. It says nothing about whether the work is any good;
what it rules out is a plan that was never going to hold together.
**→ [lean/README.md](lean/README.md)**, and Lean is optional — without it everything else is
unchanged.

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
lean/         the ChiefPlan Lean prelude a plan is written against
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
