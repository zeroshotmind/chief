# MCP surface — reconciling contract §3

STATUS.md item 1: the §3 tool list cannot be built as written, because it names 14 tools
against what is now 69 REST routes and requires them to correspond one-to-one. This doc
reconciles the two. It is the source for the §3 rewrite.

**Status: built.** `src/chief/mcp_server.py`, mounted at `/mcp`, asserted by
`tests/test_transport_parity.py`. The contract doc itself still needs the rewrite below
lifted into it.

> **The contract doc is not in this working tree.** §3's correspondence rule and its 14-tool
> list are quoted from STATUS.md's paraphrase of it, not read from the contract. The route
> side is first-hand — read from `src/chief/api/routes.py`. Check the quoted rule and list
> against the contract before lifting any of this into it.

Two things drive it:

- **REQ-2** — an MCP interface covering the same operations, and **REQ-4** — nothing
  reachable through one transport that isn't reachable through the other.
- The tool list is loaded into an agent harness's context on every session. Twenty-three
  tools is a real cost paid on every turn, so the surface should be as small as REQ-2
  permits and no smaller.

---

## 1. The rule §3 should state

§3 currently says, in effect, *no tool exists that isn't also a REST route, and vice versa*.
The biconditional is wrong in both directions. Replace it with two separate rules:

**Soundness (unchanged in force, tightened in wording).** Every MCP tool resolves to a
method on `Chief` that a REST route also reaches. This is what REQ-4 actually protects
against: a transport acquiring a private capability. It is mechanically testable.

**Coverage.** A tool exists for every operation **an agent session legitimately performs —
either on its own initiative, or on an explicit human instruction in the current turn.**
Not every route: routes are addressing, operations are behaviour.

That criterion has to be stated exactly, because `HARNESS_OPERATIONS` is what the parity
test asserts against, and a fuzzy criterion encodes a judgement call rather than a rule.
It admits `approve_amendment`, which the harness never initiates but a human routinely
instructs. It excludes exactly four things, for four different reasons:

- **`GET/PUT /config/approval-policy`** — a session that can edit the policy governing its
  own amendments can approve its own work. No human instruction makes that safe; it is the
  loop REQ-13 exists to prevent.
- **`GET /audit`** — no session behaviour is driven by reading it. Observer surface.
- **`POST /templates/{id}/archive`** — retiring a template is lifecycle administration, and
  no session behaviour depends on it. Added with templates; same reasoning as the audit log.
- **`POST /runs/{id}/artifacts/{id}/comments`** — a comment is what a person says *to* a
  harness about its output. A harness writing its own would be annotating its work with its
  own opinion of it, which is what the summary is already for, and would make a comment
  useless as a signal that someone looked. Reading them needs no tool: they ride on the
  artifacts in the state `get_run` already returns.

The test asserts soundness over all tools, and coverage over `HARNESS_OPERATIONS`. Both are
enforceable; the biconditional is not.

---

## 2. Why the counts diverged

### Paths are not operations

Seven routes are three service methods, each already parameterised by a state path:

| Routes | Service method |
|---|---|
| `POST /runs/{r}/steps/{s}/updates`<br>`POST /runs/{r}/steps/{s}/instances/{i}/steps/{b}/updates`<br>`POST /runs/{r}/state/{path}/updates` | `report_step_update(run_id, path, body)` |
| `POST /runs/{r}/steps/{s}/instances`<br>`POST /runs/{r}/state/{path}/instances` | `register_instance(run_id, path, body)` |
| `POST /runs/{r}/steps/{s}/instances/{i}/updates`<br>`POST /runs/{r}/instance-updates/{path}` | `report_instance_update(run_id, path, instance_id, body)` |
| `POST /runs/{r}/steps/{s}/resolution`<br>`POST /runs/{r}/resolutions/{path}` | `resolve_checkpoint(run_id, path, body)` |

The shallow routes exist because contract §2.2 specifies them; the `state/{path}` forms are
the extension that generalises them to arbitrary nesting. All six delegate to the same
method with the same invariants.

**Consequence for §3:** drop `report_instance_body_step_update` as a separate tool. It is
`report_step_update` with a three-token path, and shipping it as its own tool teaches the
harness that nested reporting is a different operation, which it isn't. One tool taking
`path: list[str]` covers every depth, including depths the contract's fixed routes cannot
address.

Similarly `GET /workflows/{id}` and `GET /workflows/{id}/versions/{v}` are one tool with an
optional `version`, and `GET /runs/{id}` and `GET /runs/{id}/definition` are one tool with
`include_plan`.

### §3 omits operations that genuinely are missing

`list_workflows`, `list_runs` and `list_amendments` are real operations with no tool. A
harness resuming work after a context break has no way to find the run it was driving.
These get added.

### One route has to be added

`list_amendments` is per-run only (`GET /runs/{id}/amendments`). Waiting on an approval —
which is the flow this integration exists for — means asking *is anything of mine pending*,
not asking once per run. STATUS.md §4 gap 1 already calls for `GET /amendments?status=`;
the MCP surface makes it load-bearing rather than a UI optimisation. **Add the route, then
the tool.**

---

## 3. The reconciled tool list

Twenty-three tools. Each names the `Chief` method it resolves to.

**Eighteen of them (marked ●) cover the flow this integration was asked for**: build the graph
first, register a run, push step updates as work proceeds, propose an amendment when a step
won't execute, and wait on the decision. **Five more (marked ○) cover session resumption and
retraction** — finding the run you were driving after a context break, reading an older
definition version, retracting your own proposal, archiving. They are defensible but they
are additions, not part of the stated ask. Cutting to the eighteen is a live option; §6
says what it costs.

### Harness core — planning and reporting

| Tool | Method | Note |
|---|---|---|
| ● `create_workflow` | `create_workflow` | Returns a **draft**. No run can register against it yet. |
| ● `revise_draft` | `revise_draft` | Replaces a draft's plan in place. Not an amendment: nobody has approved the plan and no run exists to pause, so there is nothing for an approval to protect. Refused once the workflow leaves `draft`. |
| ● `approve_workflow` | `approve_workflow` | Gate for REQ-32. See §4 on who may call it. |
| ○ `get_workflow` | `get_workflow` / `get_workflow_version` | Optional `version`. |
| ○ `list_workflows` | `list_workflows` | Optional `status`. |
| ● `register_run` | `register_run` | |
| ● `get_run` | `get_run` / `get_run_plan` | `include_plan` folds in `/definition`. The only tool returning an envelope (`{state, plan}`) rather than a bare document — folding two routes into one tool needs a way to name which is which. |
| ○ `list_runs` | `list_runs` | |
| ● `report_step_update` | `report_step_update` | `path: list[str]`, any depth. Requires a summary. |
| — | `resolve_checkpoint` | Extension. A human decision, like approving a workflow: the run is blocked at a checkpoint and only a person's answer moves it. |
| — | `ask_question` | Extension. A harness asking a person something mid-step, outside anything the plan declared — a checkpoint is a person-decision the plan names in advance, this is the other direction. Blocks the step without ending it. |
| — | `answer_question` | Extension. A human decision like `resolve_checkpoint`; unblocks the step back to `running` rather than deciding its outcome. |
| — | `mark_step_stale` / `mark_instance_stale` | Extension. Mark or clear a step or branch as not usable for the final result — a judgement call relayed on instruction, like `resolve_checkpoint`, not a change to what happened. |
| ● `register_step_instance` | `register_instance` | `path: list[str]`. Loop iteration or parallel branch. |
| ● `report_instance_update` | `report_instance_update` | `path: list[str]` + `instance_id`. |
| ○ `archive_workflow` | `archive_workflow` | Lifecycle. Included per §1's criterion — on instruction, indistinguishable from `approve_workflow`. |

### Harness core — adaptation

| Tool | Method | Note |
|---|---|---|
| ● `propose_amendment` | `propose_amendment` | Pauses the run. Never auto-applies. |
| ● `get_amendment` | `get_amendment` | How the harness polls for a decision. |
| ● `list_amendments` | `list_amendments` | Optional `run_id`, optional `status`. **Needs the new route.** |
| ○ `withdraw_amendment` | `withdraw_amendment` | The proposer's own retraction. |

### Templates — reuse

Not in the contract at all. A workflow is single-use, so reuse lives in a template: the plan
you keep, with parameters where the original was specific. This is the harness's normal
entry point — check for a template before composing a plan from scratch.

| Tool | Method | Note |
|---|---|---|
| ● `list_templates` | `list_templates` | What is available to build from. |
| ● `get_template` | `get_template` | Including which parameters it needs. |
| ● `create_workflow_from_template` | `instantiate_template` | Values in, draft workflow out. Missing required or unknown names are refused. |
| ● `create_template` | `create_template` | Write a reusable plan directly. |
| ● `create_template_from_workflow` | `create_template_from_workflow` | Generalise a plan that worked. |

`archive_template` is REST-only for now: retiring a template is lifecycle administration a
person does, and no session behaviour depends on it.

### Proof graphs — logic checked before approval

Not in the contract either. A proof graph is a workflow graph whose every edge is a theorem:
each step declares what it needs from the ones before it, and a proof assistant says whether
the whole thing hangs together before a person is asked to approve anything. Worth the extra
round-trip when the work has real preconditions between steps; not worth it for a short
errand.

| Tool | Method | Note |
|---|---|---|
| ● `create_proof_graph` | `create_proof_graph` | Store a Lean proof graph as a draft. Nothing is checked yet. |
| ● `list_proof_graphs` | `list_proof_graphs` | What is here, newest first. |
| ● `get_proof_graph` | `get_proof_graph` | Source, verdict, and the extracted graph. |
| ● `verify_proof_graph` | `verify_proof_graph` | Check it. A graph that fails comes back 200 with diagnostics, not as an error. |
| ● `revise_proof_graph` | `revise_proof_graph` | Fix the source. The verdict does not survive the edit. |
| ● `compile_proof_graph` | `compile_proof_graph` | Verified graph in, draft workflow out. |

`DELETE /proof-graphs/{id}` is REST-only, on the same reasoning as `delete_workflow`: erasing
a record is not something a session needs to do on its own initiative.

### Human-in-the-loop

| Tool | Method | Note |
|---|---|---|
| ● `approve_amendment` / `reject_amendment` | `approve_amendment` / `reject_amendment` | See §4. |

### Deliberately not tools

| Route | Why |
|---|---|
| `GET/PUT /config/approval-policy` | Self-approval loop — §1. |
| `GET /audit` | Observer surface — §1. |
| `POST /runs/{id}/artifacts/{id}/comments` | A comment is said *to* a harness — §1. Readable through `get_run`. |
| `POST /workflows/{id}/notes` | Review feedback is said *to* a harness, like a comment. Readable through `get_workflow`. |
| `POST/GET/PATCH /proof-graphs/{id}/notes…` | The same one-way channel on a proof graph: a person writes and closes notes, a harness reads them off `get_proof_graph` and answers by revising the source. |
| `GET /workflows/{id}/notes` | Same data the workflow document already carries; a second way to fetch it would be a tool that buys nothing. |
| `PATCH /workflows/{id}/notes/{note_id}` | Closing the feedback you were given is deciding your own work was accepted — the loop §1 is about. |
| `DELETE /proof-graphs/{graph_id}` | Erasing a record, like `delete_workflow` — not a session's to initiate. |
| `GET /proof-graphs/toolchain` | Whether this instance can check proof graphs at all. A tool would learn it by failing, which is what this exists to avoid; the failure a session does see says it plainly. |
| `PATCH /workflows/{id}` (title, project, directory) | The harness states these when it creates the plan; renaming or re-filing one afterwards is housekeeping in front of a person, not a step in any session. |
| `GET /projects` | Derived from the workflows `list_workflows` already returns, so a tool would buy nothing. |
| `GET /runs/{id}/artifacts/{id}/content` | A harness has the file already — it is the one that wrote it, and it has a filesystem. This exists so a *browser* can see it. |
| `GET /runs/{id}/artifacts/{id}/modules` | Same: the harness wrote the components too. |

These remain fully reachable over REST, so REQ-4 holds: the *union* of transports is not
narrowed, only the agent-facing subset.

---

## 4. Two calls that need a decision, not a default

**`approve_workflow` (REQ-32) and `approve_amendment` / `reject_amendment` (REQ-13).** Both
requirements say a *human* approves. A tool call made by an agent on a human's instruction
is a genuinely ambiguous case: the human did decide, but the record will say the harness
called it, and nothing in the payload distinguishes "the user told me to" from "I decided
to keep going".

They are listed as tools above because §3 lists them and because a human driving Claude
Code should be able to say "approve it". But two things should land with them:

1. The audit entry must record the transport, so an approval that arrived over MCP is
   distinguishable after the fact from one that arrived from the UI. Today `audit_log` has
   no such field.
2. The SKILL must state that these are never called on the harness's own initiative — only
   on an explicit instruction in the current turn.

`history_edit` needs no special handling here: it can never be auto-approved regardless of
policy, and that is already enforced in `domain/service.py`.

**A third path, added with templates.** `create_workflow_from_template` can cause a workflow
to reach `approved` inside one tool call, with no human in the turn, when the workflow
approval policy covers it. That is intended — the policy is a standing human decision, and
the server refuses to store a rule that could fire for anything but a template instance
(`provably_requires_a_template`). It is called out because the session *triggers* an approval
it cannot *grant*: the audit entry records `decided_by: policy:<rule>` and the transport it
arrived on, so it never reads as someone deciding in the moment.
`tests/test_templates.py` asserts exactly that.

---

## 5. What this changes

**Contract doc (§3):**

- Replace the one-to-one correspondence rule with the soundness/coverage pair from §1.
- Replace the 14-tool list with the 23 above (or the 17, per §6).
- Drop `report_instance_body_step_update`; state that `report_step_update` takes a path.
- Note that config and audit are REST-only by design, with the reason.

**Repo — done:**

- `GET /amendments?status=&run_id=` — new route, `Chief.list_amendments` generalised.
- The transport recorded on every audit entry as `detail.via` (§4).
- `tests/test_transport_parity.py` — soundness over every tool, coverage over
  `HARNESS_OPERATIONS`.

---

## 6. If you cut to the eighteen

All twenty-three shipped. Dropping the five ○ tools remains a real option — it costs three
things, all recoverable:

- **No session resumption.** Without `list_runs` / `list_workflows`, a session that loses
  context cannot find the run it was driving. The human has to paste the id back in. For a
  single-user tool with the web UI open, that is a small cost.
- **No version history.** Without `get_workflow`, a session sees only what
  `create_workflow` returned and what `get_run(include_plan=True)` carries. Enough to
  execute; not enough to answer "what changed since v1".
- **No self-retraction.** Without `withdraw_amendment`, a proposal the harness realises was
  wrong sits pending until a human rejects it. Mildly worse than the harness cleaning up
  after itself.

Coverage under §1's criterion still holds for the eighteen — they are `HARNESS_OPERATIONS`
narrowed, not violated — provided `HARNESS_OPERATIONS` is narrowed with them, and the
omission is recorded here rather than left as drift.

---

## 7. Still open

**Not blocked by this doc:** REQ-9's scope and REQ-40's diff both remain open (STATUS.md
§2). Neither is on the path to the tool surface — the diff matters to the approval *screen*,
and a harness polling `get_amendment` reads the operations directly.
