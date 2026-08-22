# Using Chief from Claude Code

Two pieces, doing different jobs:

- **The MCP server** is the capability surface — 24 tools (REQ-2). Without it Claude Code
  cannot reach Chief at all.
- **`SKILL.md`** is the protocol — plan first, summarise every update, propose an amendment
  rather than improvising, wait for the human, read what a person wrote back: the comments
  on the artifacts, and the review notes on a draft you are about to revise. No tool
  description conveys that, and it is what makes the tracking worth having.

## 1. Run Chief

```bash
chief --port 8080          # api, ui and the MCP endpoint, one process
```

The MCP endpoint is `http://127.0.0.1:8080/mcp/`, mounted on the same app as the REST API
so both transports share one SQLite connection and one lock. It is HTTP rather than stdio
deliberately: a stdio server is spawned as a child process by its client, which would put a
second process on the same database file, and the store's lock does not cross that boundary
(STATUS.md section 6 is what that looks like when it goes wrong).

## 2. Register the server

```bash
claude mcp add --transport http chief http://127.0.0.1:8080/mcp/ -s user
```

`-s user` rather than `project`: project scope writes a `.mcp.json` meant to be committed
and shared with a team, and Chief is single-user with no auth (REQ-44, REQ-45). Use
`-s project` if you do want it checked in for a specific repo.

Verify with `claude mcp list`, or ask Claude to call `list_workflows`.

## 3. Install the skill

```bash
mkdir -p ~/.claude/skills/chief
ln -s "$PWD/integrations/claude-code/SKILL.md" ~/.claude/skills/chief/SKILL.md
```

A symlink so it tracks the repo — the protocol it describes is enforced by the code next to
it, and the two drifting apart is the failure worth avoiding. Copy it instead if you would
rather pin it.

For one repo rather than globally, put it at `.claude/skills/chief/SKILL.md` in that repo.

## What Claude cannot do

Seven things are REST-only, by design:

- **`GET/PUT /config/approval-policy`** — a session that can edit the policy governing its
  own amendments can approve its own work, which is the loop REQ-13 exists to prevent.
- **`GET /audit`** — no session behaviour is driven by reading it. Observer surface.
- **`POST /templates/{id}/archive`** — lifecycle administration; nothing depends on it.
- **`POST /runs/{id}/artifacts/{id}/comments`** — a comment is what a person says *to* a
  harness about its output. A harness writing its own would be annotating the work with its
  own opinion of it, which is what the step summary is for. Claude *reads* comments freely:
  they ride on the artifacts in the state `get_run` already returns.
- **`/workflows/{id}/notes`** — review feedback on a draft, in both directions. A harness
  writing its own note is the same problem as a harness writing its own comment; a harness
  *closing* a note is worse, because it is a session deciding the feedback it was given has
  been dealt with. Claude reads notes freely: they ride on the plan `get_workflow` returns.
- **`PATCH /workflows/{id}` and `GET /projects`** — filing a workflow under a project after
  the fact is a person's housekeeping. A harness states the project and the directory when it
  *creates* the plan, which it does through `create_workflow` like anything else.
- **`GET /runs/{id}/artifacts/{id}/content` and `/modules`** — not withheld so much as
  pointless: a harness wrote the file and has a filesystem. These exist so a *browser* can
  read an artifact, and a browser is the one client that cannot.

`approve_workflow`, `approve_amendment`, `reject_amendment` and `resolve_checkpoint` are
tools, but they are human decisions — the skill says to call them only when you have been
asked to in that turn. Chief records which transport each decision arrived on, so an
approval made by an agent is distinguishable afterwards from one made in the UI.

All of these remain fully available in the web UI at `/ui` and over REST — the reasoning is
in MCP-SURFACE.md.
