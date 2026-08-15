# Using Chief from Claude Code

Two pieces, doing different jobs:

- **The MCP server** is the capability surface — 17 tools (REQ-2). Without it Claude Code
  cannot reach Chief at all.
- **`SKILL.md`** is the protocol — plan first, summarise every update, propose an amendment
  rather than improvising, wait for the human. No tool description conveys that, and it is
  what makes the tracking worth having.

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

`GET/PUT /config/approval-policy` and `GET /audit` are REST-only, by design. A session that
can edit the policy governing its own amendments can approve its own work, which is the
loop REQ-13 exists to prevent. Both remain fully available in the web UI at `/ui` and over
REST — the reasoning is in MCP-SURFACE.md.
