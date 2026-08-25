# Using Chief from Codex

The same two pieces as [Claude Code](../claude-code/README.md), doing the same jobs:

- **The MCP server** is the capability surface — the same 30 tools, over the same
  streamable-HTTP endpoint. Codex and Claude Code connect to one server and one database;
  a workflow planned from one is reportable from the other, and `harness` on each step
  says which agent the work was planned for.
- **[`SKILL.md`](../SKILL.md)** is the protocol — one file, shared with Claude Code
  deliberately, because the protocol is a property of Chief, not of the client: plan
  first, summarise every update, propose an amendment rather than improvising, wait for
  the human, read what a person wrote back.

## 1. Run Chief

```bash
chief --port 8080          # api, ui and the MCP endpoint, one process
```

HTTP rather than stdio matters more for Codex than the convenience: a stdio server is
spawned as a child process per client, and a second process on the same SQLite file is
exactly the corruption the store's lock exists to prevent. One long-running Chief, every
client a network caller.

## 2. Register the server

```bash
codex mcp add chief --url http://127.0.0.1:8080/mcp/
```

This writes `[mcp_servers.chief]` into `~/.codex/config.toml`, which the CLI, the IDE
extension and the desktop app all share. Verify with `codex mcp list`, or ask Codex to
call `list_workflows`.

## 3. Install the skill

```bash
mkdir -p ~/.codex/skills/chief
ln -s "$PWD/integrations/SKILL.md" ~/.codex/skills/chief/SKILL.md
```

A symlink for the same reason as on the Claude side: the protocol the skill describes is
enforced by the code next to it, and the two drifting apart is the failure worth avoiding.

Codex has no slash-trigger; it surfaces skills by name and description. Asking to "track
this in Chief" is what brings it in — the same opt-in-per-task rule as everywhere else.

## Headless runs

Codex's sandbox counts the MCP HTTP call as network access. In an interactive session that
is a prompt to approve; under `codex exec`, where nobody can answer, a restrictive sandbox
auto-denies it and every Chief tool fails with "user cancelled MCP tool call" — which reads
as a Chief problem and is not one. Headless sessions that need Chief must run with a
sandbox that allows network (e.g. `-s danger-full-access` inside an environment that is
already contained); interactive sessions need nothing special.

## What Codex cannot do

Exactly what Claude cannot, for exactly the reasons in
[integrations/claude-code/README.md](../claude-code/README.md#what-claude-cannot-do) and
MCP-SURFACE.md: the approval policy, the audit log, artifact comments, review notes,
re-filing and renaming are REST-only, and the four decision tools (`approve_workflow`,
`approve_amendment`, `reject_amendment`, `resolve_checkpoint`) are human decisions to be
called only on an explicit instruction in the turn. Chief records which transport each
decision arrived on either way.
