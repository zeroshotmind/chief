# Build status

Traceability from the requirements doc (REQ-1 … REQ-48) and the API & Data Contract v1 to
what actually exists in this repo, so the remaining pieces can be designed and built
independently.

**As of:** the backend, the web UI and the MCP surface. The TUI remains deferred.

| Deliverable | Requirements | Status |
|---|---|---|
| Data model + REST API | REQ-1, REQ-3–REQ-15, REQ-19–REQ-48 | **Built** — 175 tests |
| MCP surface | REQ-2 | **Built** — `src/chief/mcp_server.py`, mounted at `/mcp` |
| Web UI | REQ-16, REQ-18 | **Built** — `src/chief/web/`, served at `/ui` |
| Terminal UI | REQ-17, REQ-18 | **Not built** |

Status vocabulary:

- **Built** — implemented and covered by tests.
- **Partial** — the mechanism exists but does not fully satisfy the requirement as worded.
- **Not built** — deferred, needs design.
- **Harness** — by design, the executing harness's responsibility, not the backend's
  (requirements §7, Out of Scope). The backend stores and serves the data involved.

---

## 1. Requirement-by-requirement

### 4.1 Integration & access

| REQ | Requirement | Status | Where / note |
|---|---|---|---|
| 1 | REST API for all core operations | Built | 58 routes under `/v1`; `api/routes.py` |
| 2 | MCP interface covering the same operations | Built | 30 tools against 58 routes, `mcp_server.py`, HTTP on the same app. Surface reconciled in MCP-SURFACE.md; `tests/test_transport_parity.py` holds the two apart from drifting |
| 3 | Not coupled to any agentic framework | Built | Plain REST + JSON; `harness` is an open string, not an enum |
| 4 | All functionality reachable through the API; nothing UI-exclusive | Built | Invariants live in `domain/service.py`, not in route handlers, which is why the MCP surface inherited them unchanged rather than reimplementing them. Asserted, not just claimed: `test_transport_parity.py` |

### 4.2 Workflow definition

| REQ | Requirement | Status | Where / note |
|---|---|---|---|
| 5 | Harness submits a definition; persisted as authoritative | Built | `POST /workflows` |
| 6 | Definition is a graph of steps incl. loop/parallel constructs | Built | `models/definition.py` |
| 7 | Constructs declared without iteration/thread counts | Built | `body` only; counts resolved at runtime |
| 31 | Two entry paths: import or generate | Built | `source: import \| generated`, `generated_by`. A third in practice: instantiating a template, which records `from_template` lineage and arrives as `import` |
| 32 | New workflow starts as draft, needs human approval before first execution | Built | `POST /workflows/{id}/approve`; registering a run against a draft is a 409. Approvable from the UI since the Workflows screen — see section 9 |
| 33 | Harness may propose updates at any intermediate point, not only when blocked | Built | `POST /runs/{id}/amendments` accepted at any time |

### 4.3 Execution & state tracking

| REQ | Requirement | Status | Where / note |
|---|---|---|---|
| 8 | Harness reports step updates with status, artifacts, metadata | Built | `POST /runs/{id}/steps/{id}/updates` |
| 9 | Full history of step updates per run | **Partial** | See "Known partials" below — the audit log records that each update happened, but not the full payload of every superseded update |
| 10 | Each loop iteration is its own trackable instance | Built | `StepInstance`, `kind: iteration` |
| 11 | Each parallel branch instance is trackable, any count | Built | `StepInstance`, `kind: branch` |
| 48 | Every update carries a human-readable summary | Built | Required non-empty at the API layer on all three update endpoints |

### 4.4 Adaptive re-planning

| REQ | Requirement | Status | Where / note |
|---|---|---|---|
| 12 | Harness can propose an amendment when a step is not executable | Built | `PatchOperation` set |
| 13 | Amendments are never auto-applied; explicit human approval | Built | Run pauses; `POST /amendments/{id}/approve` |
| 14 | Completed steps and results immutable by default | Built | Enforced at proposal *and* approval, and on the ordinary update endpoint |
| 15 | After approval, execution resumes on the amended plan; history shows both | Built | `applied_amendment_ids`, version snapshots, audit log |
| 41 | Editing/replaying a completed step is a distinct, flagged amendment type | Built | `kind: history_edit`, mechanically classified |
| 42 | Original result retained, not overwritten; superseding entry | Built | `StepState.history` / `StepInstance.history` |
| 43 | Approval defaults to human, configurable auto-approval by policy | Built | `GET/PUT /config/approval-policy` for amendments; `/config/workflow-approval-policy` for workflow approval (REQ-32). `history_edit` can never be auto-approved, and a workflow rule must be provably restricted to template instances — both proven at write time |

### 4.5 Viewing interfaces

| REQ | Requirement | Status | Where / note |
|---|---|---|---|
| 16 | Web browser UI | Built | `src/chief/web/`, mounted at `/ui` by `create_app`. One workflow lifecycle rather than workflows-and-runs: a list ordered by what needs a person, a detail screen that draws the plan as a graph and gains execution state as it runs, approve/discard with a comment, and the approvals inbox. `scripts/smoke_ui.mjs` renders every screen headlessly |
| 17 | Terminal UI with equivalent viewing | **Not built** | Work package C |
| 18 | Both UIs are pure API clients, no private data access | Built (web) | The web UI is static files with no server side of its own; every value it shows comes from `/v1`, and `?api=` points it at a Chief in another process. The TUI is still to come |

### 4.6 Per-step harness selection

| REQ | Requirement | Status | Where / note |
|---|---|---|---|
| 23 | Each step names the harness that executes it | Built | `WorkflowStep.harness` |
| 24 | Harness assignment set at plan time, changeable by amendment | Built | `update_step` carries it |
| 25 | Orchestrator does not run harnesses | Built by design | No execution path exists anywhere in the code |
| 26 | Extensible harness list, not a hardcoded pair | Built | Open string namespace; adding a harness is a value, not a schema change |

### 4.7 Per-step goals

| REQ | Requirement | Status | Where / note |
|---|---|---|---|
| 27 | Every step has an explicit goal | Built | Required, non-empty, rejected if blank |
| 28 | A goal is generated for each step at plan time | Built (server side) | The backend *enforces* presence; generating the text is the planning harness's job (REQ-47) |
| 29 | Goal passed to the assigned harness at execution time | Harness | Backend serves it via `GET /workflows/{id}` and `GET /runs/{id}/definition` |
| 30 | Inserted steps get a goal as part of the amendment | Built | Same validation applies to `insert_*` payloads |
| 47 | Goals generated by the planning harness, not a separate component | Harness | Backend is deliberately agnostic about who wrote the text |

### 5. Non-functional

| REQ | Requirement | Status | Where / note |
|---|---|---|---|
| 19 | Arbitrary runtime-determined concurrent branch instances | Built | Instances registered on demand, no predeclared count. Ceiling worth knowing: single process, SQLite, one writer lock — fine for single-user, revisit if branch writers ever become genuinely concurrent at scale |
| 20 | Full auditability, every transition timestamped and durable | Built | Append-only `audit_log`; 12 event types; `GET /audit`. Lifecycle decisions carry `decided_by` and an optional comment, read back on the workflow detail screen |
| 21 | Self-hostable | Built | One process, one SQLite file, no daemon |
| 22 | API versioning sufficient for third-party plugins | Built | `/v1` prefix, also served unprefixed; OpenAPI at `/docs` |
| 44 | Single-user, no multi-tenancy | Built | No ownership or scoping fields anywhere |
| 45 | No auth in v1 | Built | Deliberate |
| 46 | Artifacts are JSON metadata only, no blobs | Built | `ArtifactRef`; requires at least one of `ref` / `data` |

### 6. Representation format

| REQ | Requirement | Status | Where / note |
|---|---|---|---|
| 34 | Structured and schema-validated before acceptance | Built | Pydantic + structural validation in `domain/graph.py` |
| 35 | Permanent step ids, never reused or renumbered | Built | Removed ids are retired and refused on reuse |
| 36 | Ordering via explicit `depends_on`, not position | Built | Scoped and cycle-checked per scope |
| 37 | Constructs are steps distinguished by `type`, with a `body` | Built | Nesting supported |
| 38 | Definition and run state are separate documents joined by step id | Built | `WorkflowDefinition` / `RunState`. Kept apart in the API and joined only for presentation — the UI shows one lifecycle, since the split serves a harness amending a plan, not a person reading one |
| 39 | Amendments are patch operations, not full resubmission | Built | 5 operation types, applied atomically |
| 40 | Format renderable as a human-readable diff | **Not built** | No diff is produced anywhere. See "API gaps" below — this blocks the approval screen in both UIs |

---

## 2. Known partials

**REQ-9 — full history of step updates.** What exists: the current `StepState` for every
step, snapshots of superseded states in `history` when a history edit replaces one, and an
audit-log row for every update recording path, status, summary and artifact *count*. What
does not exist: the full artifact and metadata payload of each superseded update. If a step
is reported `running` with artifacts and then `completed` with different ones, the first
payload is gone.

Decide before the UIs are built, because a run timeline view is the natural consumer:

- If "history" means an auditable trail of *what happened when*, today's audit log is
  enough and REQ-9 is satisfied as-is.
- If it means "replay the run and see every intermediate payload", the audit-log `detail`
  column needs to carry the whole update body, or a separate `step_updates` table is needed.
  Cheap to add now, awkward to backfill later.

**REQ-40 — human-readable diff.** The web UI renders one, in `opRows()` in
`src/chief/web/app.js`: each `PatchOperation` becomes a badge, the affected step, and —
where the run's definition is loaded — the before/after of the goal and of `depends_on`.
That satisfies the requirement for the screen that needs it, but it lives in a *client*.
The TUI would have to reimplement it, and the two could drift. Still worth moving behind
the API (`GET /amendments/{id}/diff`) before a second client exists; the UI would then
render the server's rows instead of deriving its own. Not urgent while there is one client.

---

## 3. Not built — work packages

### A. MCP surface (REQ-2, contract §3) — **built**

Every operation was already a method on `Chief` in `domain/service.py` with every invariant
enforced there rather than in the route handlers, specifically so a second transport would
inherit them unchanged. It did: `src/chief/mcp_server.py` holds no logic, only the mapping.

**The tool list is reconciled in MCP-SURFACE.md**, which supersedes contract §3 and is the
source for the §3 rewrite the doc still needs. Twenty-four tools, not the 14 §3 names nor the
58 routes: the seven update/instance routes are three path-parameterised service methods, so
`report_instance_body_step_update` disappears into `report_step_update`; and the one-to-one
correspondence rule is replaced by a soundness rule (every tool resolves to a method a REST
route also reaches) plus a coverage rule over the operations an agent session legitimately
performs. Config and audit are REST-only by design — the reasoning is in MCP-SURFACE.md §1.

What shipped:

- `src/chief/mcp_server.py` — 24 tools over the same `Chief` instance the REST app uses, so
  both transports share one `Store`, one connection and one lock.
- **HTTP, mounted at `/mcp` on the existing app.** Not stdio: a stdio server is spawned as a
  child process by its client, which would mean a second process on the same SQLite file,
  and `Store._lock` is an in-process lock that does not cross that boundary — the failure
  class section 6 documents. One process, REQ-21 intact.
- `GET /amendments?status=&run_id=` — the 27th route. A prerequisite, not an optimisation:
  waiting on an approval is the flow this exists for and cannot be done per-run.
- Transport recorded on every audit entry, as `detail.via`. An approval that arrived over
  MCP stays distinguishable from one made in the UI (MCP-SURFACE.md §4).
- `tests/test_transport_parity.py` — 17 tests: soundness over every tool, coverage over
  `HARNESS_OPERATIONS`, the REST-only exclusions, the documented flow end to end, and
  `tools/list` driven over the mounted endpoint at every protocol revision the server
  speaks. See section 8 for why that last one exists.
- `integrations/claude-code/` — the SKILL.md carrying the protocol no tool description
  conveys, and how to register the server.

Still open: the contract doc itself has not been rewritten, and `mcp>=2.0` is a new runtime
dependency.

Left deliberately: the tools return whole `RunState` documents. Fine at this size; if a run
with many steps ever makes a tool result unwieldy in a session's context, the answer is a
projection parameter, not a second endpoint.

### B. Web UI (REQ-16, REQ-18) — **built**

See section 5 below for what shipped and what was left out.

### C. Terminal UI (REQ-17, REQ-18)

Equivalent viewing to the web UI. Same endpoints, same gaps. Worth deciding whether it is
view-only or can also approve; the requirements only mandate viewing parity, but an
approval prompt in the terminal is arguably the most natural place for it given the users.

---

## 4. API gaps the UIs will hit

These are backend work items discovered by thinking through the UI screens. All of them
must be solved behind the API, not in a client, or REQ-4 and REQ-18 break.

1. **No global pending-amendments endpoint.** Today: `GET /runs/{run_id}/amendments?status=pending_approval`
   — per run only. The approval inbox therefore fans out one request per run on every
   poll, which is what first surfaced the shared-connection bug in section 6. Still needs
   `GET /amendments?status=pending_approval`; the UI would drop from 2+N requests a tick
   to 3. **No longer just a UI optimisation** — the MCP surface makes it a prerequisite
   (work package A), because a session waiting on its own approval cannot poll per-run.
2. **Diff rendering (REQ-40) lives in the client.** See partials above.
3. **No push or streaming.** Clients must poll `GET /runs/{id}`. For a live run view that
   means a poll loop. Decide whether to add SSE or a WebSocket, or accept polling and
   document an interval. Polling is defensible at single-user scale; the decision should
   just be conscious.
4. **No pagination** (contract Open Item 3, consciously skipped). List endpoints return
   everything. Fine early, and the UI is where it will first hurt.
5. **No search or free-text filter.** Lists filter by status only. Finding a run by title
   or a step by goal is not possible through the API.
6. **Audit log has no time-range or event-type filter.** `GET /audit` filters by
   `workflow_id` / `run_id` / `amendment_id` only, and returns everything matching.
7. **No aggregate counts.** A dashboard wanting "3 runs active, 1 awaiting approval" has to
   fetch full lists and count client-side.

---

## 5. The web UI (REQ-16, REQ-18)

`src/chief/web/` — `index.html`, `chief.css`, `api.js`, `app.js`. Four static files with no
build step and no CDN, mounted at `/ui` by `create_app` and reachable at `/`. The design is
the "Chief Runs v5" Claude Design project, ported to plain DOM; its Nocturne tokens are the
top of `chief.css` and the palette follows the viewer's light/dark preference.

Screens:

1. **Runs list** — every run with status, relative age and a live dot. `GET /runs`,
   `GET /workflows` (for titles).
2. **Run detail** — the effective plan laid out as a dependency graph, one node per
   top-level step. Node colour is the step's outcome, not merely its status: a completed
   construct whose instance failed reads amber, not green. Loop and parallel steps show a
   dot per instance and an "N iterations…" label, the ellipsis meaning `instances_closed`
   is still false. Edges carry the upstream step's outcome colour. An inspector panel
   shows the selected step's goal, harness, per-instance summaries and artifacts —
   markdown, images, video and audio each rendered in kind, anything else as its ref.
3. **Approvals inbox** — everything awaiting a decision, across runs.
4. **Approve / reject** — a dialog with an optional reason, which is sent as
   `decided_by: human` and lands in the audit log. History edits carry an explicit warning
   that approval re-runs a finished step.

A pending amendment is drawn *into* the graph rather than only listed: proposed insertions
appear as dashed "proposed" ghost nodes wired to their anchor, steps the amendment touches
get a "proposed change" badge, and the inspector shows the operation diff. The point is
that the reviewer sees the shape of the plan they are approving, not a patch document.

Polling, not streaming (gap 3 above): `/runs` plus one amendment list per run every 15s,
with the heavy documents fetched only for the run on screen.

**Not built, deliberately:** workflow list and workflow-version history screens (screens 1
and 2 of the original work package), and the audit-trail view. The requirements ask for
viewing *runs*; those three are reachable through the API and are the obvious next screens.
There is also no filtering UI over `?status=`, and no pagination (gap 4).

---

## 6. A backend change the UI forced

`Store` shares one `sqlite3.Connection` across threads (`check_same_thread=False`) and
FastAPI runs the sync handlers on a threadpool. Writes took `self._lock`; reads went
straight to the connection. Sequential clients never noticed. The UI's first screen issues
2+N reads at once, and that produced `InterfaceError: bad parameter or other API misuse`
and — worse, because it looks like data rather than a fault — requests answering with
another request's rows, surfacing as 404s for runs that plainly exist.

Reads now go through `Store._one` / `Store._all`, which hold the same reentrant lock until
the rows are materialised. `tests/test_concurrent_reads.py` covers it and fails without the
fix. This is the ceiling REQ-19 already flagged, met from the read side rather than the
write side.

---

## 7. Contract open items

| Item | Status |
|---|---|
| 1. ApprovalPolicy `match` grammar and rule ordering | **Decided** — small boolean expression language, first-match-wins, with write-time proof that an auto-approving rule cannot match a `history_edit`. See CONTRACT-NOTES.md #5 |
| 2. Storage engine | **Decided** — SQLite. The contract's queryability assumption holds |
| 3. Pagination | **Skipped**, consciously. Revisit for the UIs (gap 4 above) |
| 4. Run-level equivalent of `on_instance_failure` | **Not implemented**. Run-level failure is always fail-fast, as the contract has it. Still open |

Separately, CONTRACT-NOTES.md lists 27 places where implementation surfaced something the
contract left open, ambiguous or wrong. Several need a doc change before a second
implementation (or the MCP surface) is written against the contract as it stands.

---

## 8. One bug worth keeping

The first real client to connect got `-32603 Handler returned an invalid result` on
`tools/list`, and therefore **no tools at all**, while the same server answered a
current-generation Python client perfectly.

`StepInstance` is recursive — it contains `StepState`, which contains `StepInstance` — so
pydantic cannot inline it and emits its schema as a bare `{"$ref": ...}` with no `type`.
That is valid JSON Schema, and the newest protocol revision accepts it. The 2025-06-18
revision requires `outputSchema.type`, and one bad schema fails the serialisation of the
*whole* result, so a single recursive model cost every tool.

Two things generalise from it:

- **Testing against one client version tests one client version.** The probe that passed
  negotiated a newer revision than the client that failed. The regression test now drives
  the mounted endpoint at all five revisions in `KNOWN_PROTOCOL_VERSIONS`; removing the fix
  fails four of them.
- **A related trap, fixed at the same time.** The MCP transport checks the `Host` header
  against an allowlist built from the host it thinks it is bound to, which defaulted to
  loopback whatever `--host` said. Binding Chief to `0.0.0.0` would have left REST working
  and MCP answering 421 — which reads as "MCP is broken", not "the host does not match".
  `CHIEF_HOST` now carries `--host` through to it.

---

## 9. Two gaps the first real use found

Driving Chief from Claude Code for the first time surfaced two things that every
clause-by-clause reading of the requirements had called Built.

**REQ-32's approval gate had no human interface.** The requirement says a person approves a
workflow before its first execution, and the API enforced exactly that. But the web UI was
organised entirely around *runs* — a draft has no run, so a draft was invisible. The one
decision the requirement insists a human make was the one decision the UI could not make;
`approve_workflow` was reachable only from curl, MCP, or the API. REQ-16 and REQ-32 were
each true alone and wrong together.

Fixed by a Workflows screen listing drafts with their full plan — approving is a decision
*about the plan*, so the plan has to be on screen — grouped ahead of approved and archived,
with the draft count on the nav.

**A draft had no way to be retired.** `archive` refused anything that was not `approved`, so
a superseded draft could not be approved (it was wrong), archived (not approved), or deleted
(no such route). It sat asking to be approved forever — harmless while nothing listed
drafts, and immediately visible once something did. `archive_workflow` now accepts a draft,
and the audit entry records the state it came from, because retiring an unused draft and
retiring a workflow that ran are different acts.

Both were found by *using* the thing, not by reading it. The requirements traceability in
section 1 is honest per-clause and was still no substitute.

---

## 10. Suggested order

1. **Reconcile the §3 tool list** — **done**, in MCP-SURFACE.md; the contract doc still
   needs the rewrite lifted into it. REQ-9's scope remains open, but is not on the path to
   the tool surface.
2. **Backend gaps that block UI work** — global pending-amendments endpoint, diff
   rendering, and a decision on polling versus streaming. Small, and both UIs depend on
   them, so doing them first stops the same thing being solved twice.
3. **MCP surface.** Independent of the UIs and the smallest package; it also proves the
   "invariants live in the service layer" claim by exercising a second transport.
4. **TUI** (the web UI is now built). It should render the diff the API hands it rather
   than porting `opRows()` a second time — see the REQ-40 partial.

---

## Appendix: current endpoint inventory

```
POST   /v1/workflows
GET    /v1/workflows
GET    /v1/workflows/{workflow_id}
PUT    /v1/workflows/{workflow_id}                         (extension, revise a draft)
PATCH  /v1/workflows/{workflow_id}                         (extension, rename or re-file)
DELETE /v1/workflows/{workflow_id}                         (extension, delete permanently)
GET    /v1/workflows/{workflow_id}/versions/{version}
POST   /v1/workflows/{workflow_id}/approve
POST   /v1/workflows/{workflow_id}/archive

POST   /v1/workflows/{workflow_id}/notes                   (extension, review notes)
GET    /v1/workflows/{workflow_id}/notes                   (extension, review notes)
PATCH  /v1/workflows/{workflow_id}/notes/{note_id}         (extension, review notes)

POST   /v1/templates                                       (extension, reuse)
GET    /v1/templates                                       (extension, reuse)
GET    /v1/templates/{template_id}                         (extension, reuse)
POST   /v1/templates/{template_id}/archive                 (extension, reuse)
POST   /v1/templates/{template_id}/workflows               (extension, reuse)
POST   /v1/workflows/{workflow_id}/template                (extension, reuse)

POST   /v1/workflows/{workflow_id}/runs
GET    /v1/runs
GET    /v1/runs/{run_id}
GET    /v1/runs/{run_id}/definition                        (extension)
POST   /v1/runs/{run_id}/steps/{step_id}/updates
POST   /v1/runs/{run_id}/steps/{step_id}/instances
POST   /v1/runs/{run_id}/steps/{step_id}/instances/{instance_id}/updates
POST   /v1/runs/{run_id}/steps/{step_id}/instances/{instance_id}/steps/{body_step_id}/updates
POST   /v1/runs/{run_id}/state/{state_path}/updates        (extension, nesting)
POST   /v1/runs/{run_id}/state/{state_path}/instances      (extension, nesting)
POST   /v1/runs/{run_id}/instance-updates/{state_path}     (extension, nesting)

POST   /v1/runs/{run_id}/steps/{step_id}/resolution        (extension, checkpoints)
POST   /v1/runs/{run_id}/resolutions/{state_path}          (extension, checkpoints)

GET    /v1/runs/{run_id}/artifacts/{artifact_id}/content   (extension, file preview)
GET    /v1/runs/{run_id}/artifacts/{artifact_id}/modules   (extension, mdx + co-located components)
POST   /v1/runs/{run_id}/artifacts/{artifact_id}/comments  (extension, artifact comments)

POST   /v1/runs/{run_id}/amendments
GET    /v1/runs/{run_id}/amendments
GET    /v1/amendments                                      (extension, global inbox)
GET    /v1/amendments/{amendment_id}
POST   /v1/amendments/{amendment_id}/approve
POST   /v1/amendments/{amendment_id}/reject
POST   /v1/amendments/{amendment_id}/withdraw

GET    /v1/config/approval-policy
PUT    /v1/config/approval-policy
GET    /v1/config/workflow-approval-policy                 (extension, draft auto-approval)
PUT    /v1/config/workflow-approval-policy                 (extension, draft auto-approval)

GET    /v1/projects                                        (extension, labels in use)

POST   /v1/proof-graphs                                    (extension, proof graphs)
GET    /v1/proof-graphs                                    (extension, proof graphs)
GET    /v1/proof-graphs/toolchain                          (extension, proof graphs)
GET    /v1/proof-graphs/{graph_id}                         (extension, proof graphs)
PUT    /v1/proof-graphs/{graph_id}                         (extension, proof graphs)
DELETE /v1/proof-graphs/{graph_id}                         (extension, proof graphs)
POST   /v1/proof-graphs/{graph_id}/verification            (extension, proof graphs)
POST   /v1/proof-graphs/{graph_id}/workflows               (extension, compile to a draft)
POST   /v1/proof-graphs/{graph_id}/notes                    (extension, review notes)
GET    /v1/proof-graphs/{graph_id}/notes                    (extension, review notes)
PATCH  /v1/proof-graphs/{graph_id}/notes/{note_id}          (extension, review notes)

GET    /v1/audit                                           (extension)
```

58 routes, also served without the `/v1` prefix; `/healthz` is not counted here. Full
schemas at `/docs` when running. The count is asserted against the router in
`tests/test_transport_parity.py` — this list went stale twice before that guard existed.

The MCP surface is not in this list by design: it is 30 tools over these same service
methods, mounted at `/mcp`. See MCP-SURFACE.md.
