# Contract notes

Things implementation surfaced that the design review didn't. Each entry says what the
contract says, what was ambiguous or wrong, what this implementation does, and whether the
doc needs a change.

The contract went through four adversarial passes and holds up well — nothing here is a
structural flaw of the kind pass 3 found. What is left is mostly **fields referenced but
never defined**, **rules stated for one shape and silently assumed for another**, and
**states with no producer**. Items 1, 2, 4, 6 and 8 are gaps a reader cannot close without
guessing, and 24-25 are holes in the approval flow as written; the rest are decisions the doc leaves open or wording that reads one way and
means another.

Numbers are stable identifiers; code comments cite them.

---

## Needs a doc change

### 1. `instances_closed` is used but never defined

Contract 2.2 lists it in the step-update payload and §7's changelog says it was "added to
the step-update payload", but it appears in no schema, and §1.4's completion rule for
loop/parallel steps depends on it.

Without it the derivation is impossible: "every instance is completed" and "no more
instances are coming" are different claims, and the server only knows the first.

**Implemented:** `instances_closed: bool` on `StepState`, default `false`, settable only
through the step-update endpoint and only for `loop`/`parallel` steps (a task step sending
it is a 422). Monotonic — reopening a closed construct is a 409, because the alternative
lets a construct oscillate in and out of `completed`. A construct closed with zero
instances completes vacuously.

**Doc change:** add the field to §1.4 and state the closure semantics and monotonicity.

### 2. `on_instance_failure` is referenced but never defined

§1.5 says instance-status derivation is "subject to the same `on_instance_failure`
setting" and Open Item 4 asks whether it needs a top-level equivalent. It has no schema
location, no allowed values and no default anywhere in the doc.

**Implemented:** `on_instance_failure: "fail_fast" | "continue"` on the loop/parallel
`WorkflowStep`, defaulting to `fail_fast`, rejected on a `task` step. It applies at both
levels the doc mentions: a failed *instance* fails the construct, and a failed step inside
an instance body fails the instance — `continue` tolerates both, scoped to that construct.
Run-level failure stays fail-fast with no override (Open Item 4, left as the doc has it).

Note that `continue` does not mean failures are ignored: a failed body step still skips
whatever depended on it inside that iteration. It only means the failure doesn't propagate
*upward*.

**Doc change:** add the field to §1.2 and state which levels it governs.

### 4. "top-level step" is never defined

§1.3 derives run completion from "every top-level step" without saying what that is. With
`body` referencing step ids that also live in the flat `steps` list, this matters: taking
it as "everything in `steps`" would make a run wait for body steps that only exist inside
instances.

**Implemented:** top-level = steps not contained in any step's `body`. Containment is
validated as a forest, so at least one always exists.

**Doc change:** one sentence in §1.3.

### 6. Nesting is allowed by the schema but unreachable through the API

§1.2 says "a body step may itself be `type: loop|parallel` (nesting is allowed)" and §1.5
says nested instances are scoped under the parent instance. §2.2's deepest route is
`.../instances/{instance_id}/steps/{body_step_id}/updates` — there is no way to *register*
an instance of a nested construct, so a nested loop can be planned but never executed.
This is the same class of defect pass 4 was fixing (a state with no producer).

**Implemented:** run state is addressed by a *state path* — an odd-length list alternating
step id and instance id (`step_06/inst_01/step_09/inst_02/step_11`). The contract's routes
are the two- and three-segment cases of the same resolver, unchanged; generic routes
(`/runs/{run_id}/state/{path}/updates`, `/runs/{run_id}/state/{path}/instances`,
`/runs/{run_id}/instance-updates/{path}`) reach any depth. `PatchOperation` gains
`instance_path` alongside `instance_id` for the same reason.

**Doc change:** either add the nested routes to §2.2 or say nesting is out of scope for v1.

### 8. `StepInstance` has nowhere to preserve a replaced result

§1.8 says `replay_step` with an `instance_id` "replays just that one instance", and
REQ-42 / §4 require the prior result to be preserved rather than overwritten. `history`
exists only on `StepState` (§1.4), so an instance-scoped replay has nowhere to put the old
instance record.

**Implemented:** `StepInstance.history`, mirroring `StepState.history`.

**Doc change:** add `history` to §1.5.

---

## Wording that reads one way and means another

### 3. "any operation targeting a completed/failed step" is too broad

§1.7: `kind` must be `history_edit` if any operation targets a step or instance whose
current status is `completed` or `failed`. Read literally that includes `insert_after` on a
completed step — which contradicts REQ-14 ("a forward-looking plan amendment never alters
or re-executes" completed steps; inserting a neighbour does neither), makes an ordinary
forward insertion permanently ineligible for auto-approval, and triggers the §4 rule that a
`history_edit` approval "always preserves the prior StepState in `history`" on a step
nothing is being done to.

**Implemented (confirmed with the doc owner):** only `update_step`, `remove_step` and
`replay_step` force `history_edit`. `insert_after`/`insert_before` are always `forward`.
`replay_step` is always `history_edit` regardless of target, per §1.8.

**Doc change:** narrow §1.7 and §4 to the mutating operations.

### 7. "clears back to `running`" contradicts §1.3, and both contradict the derivation

§2.3 says approve/reject "clears that run's `paused_for_approval` back to `running`". §1.3
says it clears "back to whatever the run's status was immediately before the pause", and
explicitly warns that a `history_edit` near the end of a run must be able to resolve into
`completed`/`failed`. But §1.3 also says `completed`/`failed` are *server-derived* — so
restoring a remembered status is wrong too: approving an amendment can change what the
correct status is.

**Implemented:** the pause clears by **re-deriving** the run status, which yields
`running`, `completed` or `failed` correctly in every case, including after a replay resets
a completed step back to `pending`.

**Doc change:** replace both sentences with "re-derived per the rule above".

### 10. A run's effective plan and the global version counter can disagree

§1.3 is right that each run pins `base_version` + `applied_amendment_ids`, and §2.3 is
right that approving bumps `WorkflowDefinition.version`. Together they imply something the
doc never states: `WorkflowDefinition.version = n` and "run X's effective plan" are
different documents, and an amendment valid against a run's effective plan may not apply to
the global head at all (a sibling run may have removed the step it targets).

**Implemented:** the run's effective plan is stored per run and is authoritative for that
run. Approval also applies the operations to the global head; if that fails, approval is
refused with `409` naming the conflict, and nothing is written. `GET /runs/{id}/definition`
returns a `RunPlan` — `base_version`, `applied_amendment_ids` and `steps` — rather than a
`WorkflowDefinition`, because neither the pinned version nor the global counter names the
materialised plan, and stamping it with either makes two different documents claim the same
version.

**Doc change:** state that a version number identifies the shared definition only, that a
run's plan is identified by `base_version` + `applied_amendment_ids`, and that approval can
fail on a sibling-run conflict.

---

## Left open by the doc, decided here

### 5. Open Item 1 — `ApprovalPolicy.rules[].match` grammar and ordering

**Implemented:** a small boolean expression language over `amendment.<field>`, with
`kind`, `proposed_by`, `run_id`, `workflow_id` (compared with `==`, `!=`, `in`) and `ops`
(compared with `subset_of`), combined with `&&`, `||`, `!`, parentheses, and the literals
`true` / `false`. First matching rule wins. No `eval`, no attribute traversal.

```
amendment.kind == 'forward' && amendment.proposed_by in ['planner', 'claude_cli']
amendment.kind == 'forward' && amendment.ops subset_of ['insert_after', 'insert_before']
```

The reason for a real grammar rather than a structured filter is §1.9's requirement that a
rule which could auto-approve a `history_edit` is *rejected when written*, not ignored at
decision time. Expressions are evaluated with three-valued logic: bind `amendment.kind` to
`history_edit`, leave every other field unknown, and require a definite `false`. A rule
that cannot be proven to exclude history edits is refused with a 422 pointing at the fix.
`ApprovalRule` gains an optional `id` so `Amendment.decided_by` can record `policy:<id>` —
§1.7 says `decided_by` may be a `policy_id`, but §1.9 gave rules no id.

### Open Item 2 — storage engine

SQLite, one file, documents stored as JSON with identifying columns lifted out. The
contract's assumption (queryable by `workflow_id` / `run_id` / `step_id`) holds; step state
is nested inside its run document because a run is always read whole. Fits REQ-21 and
REQ-44: no daemon, no server to operate, trivially self-hostable and backed up by copying
one file.

### Open Item 3 — pagination

Consciously skipped, as the doc suggests. `GET /workflows` and `GET /runs` take a `status`
filter and return everything.

### Open Item 4 — run-level failure override

Not implemented. Run-level failure is always fail-fast, matching the doc as written.

---

## Smaller gaps, decided without needing a doc change

11. **`insert_after` / `insert_before` semantics under REQ-36.** REQ-36 says ordering comes
    from `depends_on`, not list position, which leaves "after" with no execution meaning.
    Implemented as *positional only*: the step is placed relative to the target in the
    `steps` list and in the containing `body`, and the harness must supply `depends_on`
    itself. This matches §1.8's explicit "no implicit rewiring" stance for `remove_step`.
    **Worth a doc sentence** — a harness that omits `depends_on` gets a step that is
    ordered visually but unordered semantically, and nothing warns it.
12. **`update_step` and step ids.** Not stated whether the payload's `step.id` must equal
    `target_step_id`. It must — otherwise `update_step` renames a step, violating REQ-35.
    Enforced with a 422.
13. **`update_step` changing `type`, and body rewiring moving a step between scopes.** Not
    addressed at all. Both are refused once the step has left `pending`: the first would
    leave instances recorded against something that is no longer a construct, the second
    would strand a recorded result in a scope that no longer exists.
14. **`remove_step` and the prior status.** §1.4 says removal sets `skipped` "rather than
    deleting the record, preserving history" — but overwriting the status *is* losing
    history. The prior state is snapshotted into `history` first.
15. **`ArtifactRef.artifact_id` is "required" but nothing says who mints it.** Accepted
    from the client, generated server-side when omitted. Strictly more permissive than the
    doc; a client that supplies one is unaffected.
16. **Instance registration payload is unspecified.** `POST .../instances` takes an optional
    `instance_id`, `index`, `kind`, `summary` and `metadata`. `index` defaults to the next
    free one, `instance_id` to `inst_NN`, and `kind` is derived from the parent's type
    (supplying a mismatched one is a 422).
17. **Updates while `paused_for_approval`.** Unspecified. Accepted — in-flight parallel
    branches shouldn't lose results because a pause was raised elsewhere — but the run stays
    pinned at `paused_for_approval` until the amendment is decided.
18. **Over-declaring `history_edit`.** §1.7 constrains only one direction. Submitting
    `history_edit` when `forward` would do is allowed: it is fail-safe (it can only *add* a
    human decision) and lets a harness force review of something it is unsure about.
19. **Updating a terminal step.** Not stated. A step or instance that is `completed`,
    `failed` or `skipped` refuses further updates with a 409 pointing at `replay_step` —
    otherwise REQ-14's immutability is enforced against amendments but trivially bypassable
    through the ordinary update endpoint.
20. **Retracting a skip.** `skipped` is derived from a failure, and REQ-41 lets that failure
    be replayed away. Nothing in the doc says what happens to the steps skipped behind it;
    leaving them skipped makes the run report `completed` for steps that never ran. Skips
    caused by a dependency are retracted when their cause clears; skips caused by
    `remove_step` are permanent. The two are distinguished by a `skip_cause` field on
    `StepState`.
21. **An amendment that empties the plan.** `remove_step` on the last remaining step
    produced a definition `POST /workflows` would reject, and a run that could never reach a
    terminal status. The post-amendment plan is now held to the same validation as a
    submitted one.
22. **`replay_step` on something that never ran.** Refused with a 409: there is nothing to
    replay, and accepting it pins the run behind an amendment with no effect. An *unscoped*
    replay of a body step reaches every instance it is materialised in, but only replays the
    copies that finished — discarding an in-flight sibling's work is not what "replay this
    step" means.
24. **The window between proposal and approval.** The contract classifies an amendment at
    submission ("rejected before it ever reaches a human") and never revisits it. But a run
    keeps executing while a human decides: a target that was `pending` at classification can
    be `completed` by the time approval arrives, at which point a `forward` amendment would
    destroy a completed result with no history edit and no human ever seeing it as one. Every
    check is re-run at approval against current state, and approval fails with a 409 asking
    for resubmission as `history_edit`. **Worth a doc sentence** — this is a real hole in
    §2.3 as written, not just an implementation detail.
25. **Operations apply sequentially, so validation has to simulate them.** §1.8 says an
    amendment applies atomically and that validation "runs against the resulting
    post-amendment state" — which is right for the *plan* but says nothing about run state,
    where operations genuinely apply one after another. An earlier operation can destroy
    what a later one was checked against (replaying a construct deletes the instance the
    next operation is scoped to; replaying the same step twice leaves nothing for the second
    to do). Checking each operation against the pre-amendment snapshot accepts such a set
    and then fails at approval, wedging the run. Validation applies the state effects to a
    throwaway copy of the run, which closes the class rather than the known cases.
26. **Multi-operation amendments that build on themselves.** §1.8 says operations apply as a
    set, which implies a later operation may target a step an earlier one inserts. Validation
    tracks ids introduced within the same amendment so `insert_after step_X` followed by
    `update_step step_X` is one atomic proposal rather than two separate human approvals.
    The waiver is narrow — only "does this step exist" — so a `replay_step` or an
    instance-scoped operation on a brand-new step is still refused.
27. **Restructuring a step that carries preserved history.** A replayed step is `pending`
    but holds the snapshot REQ-42 requires. Moving it into a loop body would discard that
    record, so a step with an execution entry in `history` is treated as having run and
    cannot be restructured — same rule as a step that is currently running.
23. **An instance's body versus the current body.** An amendment can edit a loop body
    mid-run. An instance now records the body it was spawned with, so an already-finished
    iteration is not retroactively re-opened by a step added afterwards (REQ-14); in-flight
    instances do pick the change up.
28. **A step type the harness cannot execute: `checkpoint`.** Not in the contract at all —
    an extension. The doc gives a human two gates, approving a plan (REQ-32) and approving
    an amendment (REQ-13), and both are about *the plan*. Neither covers "the run has
    reached a point a person has to decide", which a harness could otherwise only express by
    abusing `propose_amendment` as a question — minting a plan revision and a version bump
    for something that is not a plan change.

    A `checkpoint` is a leaf step whose `harness` must be `human`. It is reached like any
    other step — the harness reports `running` — and the server records the new status
    `blocked`, which is deliberately not reportable and not terminal, so the ordinary
    completion and skip derivations are untouched. `blocked` anywhere in the tree surfaces
    as the run status `waiting_on_human`; an amendment pause still wins, since a pause
    suspends the whole run rather than one step of it.

    The decision arrives through `resolve_checkpoint`, which is the only way out of
    `blocked`: `approved` completes the step, `rejected` fails it and skips what depended on
    it through the existing dependency rule. A checkpoint may also declare `fields`, the
    things it asks a person for in writing; the answers are validated against those names on
    resolution and kept on the step's state as `checkpoint.response`, which is where the
    harness reads them back from.

    Two edges left where they are, deliberately. A required field is required only to
    *approve*: rejecting is exactly the case where the answers do not exist, and demanding
    them to decline would make filling the form in with anything the cheapest way past a
    gate. And a checkpoint already `blocked` when an amendment replays its dependency stays
    blocked — `propagate_skips` acts on `pending` and `skipped`, not `blocked` — so the
    question can still be answered while the work behind it re-runs. Replaying the
    checkpoint itself is the way to ask again, and that clears the recorded decision into
    `history` rather than leaving a pending step reading as already decided.

    Blocking is *not* derived from "all dependencies satisfied". That was the tempting
    design and it is wrong twice over: a checkpoint with no `depends_on` would block
    vacuously at `register_run`, and an approved `replay_step` resetting a dependency to
    `pending` would leave a checkpoint blocked with nothing to retract it — the failure mode
    `skip_cause` exists to prevent, in a second fixpoint loop.

29. **An artifact ref names a file, but nothing says where.** REQ-46 is clear that an
    artifact is metadata and never a blob, and a harness reporting one names the file the
    way it saw it: `songs/personas.md`, relative to whatever directory it was working in.
    That directory is not recorded anywhere, so the reference reads fine and resolves to
    nothing.

    The base deliberately does **not** go on the model. A `cwd` on the run is the obvious
    place and it is wrong three ways: it grows the harness-facing surface, it is stale the
    moment the tree moves, and it fixes nothing for runs already recorded. Chief also stays
    out of serving the file — a `GET /files?path=` would make the tracker a file server and
    put path-traversal containment in a process that currently reads nothing off disk.

    So the base lives in `localStorage`, set from the artifacts section of the inspector
    where the paths it applies to are visible. A relative ref resolves against it into a
    `vscode://file/…` link; an absolute one is used as-is; an `http(s)` ref is left exactly
    as reported. With no base set the path renders as plain text rather than as a link built
    on a guess — a `vscode://` URL on the wrong root opens "file not found" in the editor,
    which reads as the editor failing rather than the base being unset. A copy control sits
    beside every ref regardless, since copying is the one action that works either way.

    This is entirely a property of the browser reading the run, not of the run: the same
    workflow opened on another machine resolves against that machine's checkout, which is
    the correct answer and not one a stored field could give.

30. **Comments on artifacts — the channel that ran the wrong way.** An extension. Every
    write in the contract flows harness → Chief: what was produced, what happened, what
    could not be done. A person reviewing the output has nowhere to put "this draft is the
    one, match its tone" except a checkpoint, which blocks the run, or an amendment, which
    mints a plan revision. Both are the wrong shape for an aside.

    An `ArtifactComment` (`comment_id`, `body`, `author`, `created_at`, `via`) hangs off the
    artifact and rides on the run state `get_run` already returns, so the harness reads them
    with no new call and the MCP surface gains no tool. Append-only, like the artifact list
    itself: a comment is a thing someone said at a point in the run, and letting it be
    rewritten would make the record of what the harness was told disagree with what it acted
    on. Writing one is REST-only — a harness commenting on its own output is annotating the
    work with its own opinion of it, which is what `summary` is already for.

    Two things are load-bearing here.

    `ArtifactRef` is both the stored shape and the shape a harness submits in a `StepUpdate`,
    so `extra="forbid"` does *not* catch a harness sending `comments` — the field is
    legitimately declared. `_stamp_artifacts` refuses it explicitly, the same way
    `report_step_update` refuses a harness reporting a checkpoint's outcome. Splitting the
    model was the alternative and was rejected: artifacts appear in six places in the
    contract, and a second shape for one of them is a worse cost than one guard.

    The completed-step rule is deliberately bypassed, and that exemption *is* the feature.
    Commenting on finished work is the main case — nothing gets reviewed before it exists —
    and a comment annotates a result rather than changing one, so it is not a `history_edit`
    and needs no amendment. The `ref`, the `data` and the step's status are untouched: what
    the harness reported still says exactly what it said.

    Addressing is by `artifact_id`, not by state path, unlike everything else that reaches
    into a run. The id is unique within the run and is stamped before anyone can see the
    artifact, whereas the path is not something the reader holds — both UIs read artifacts
    as one flat list of everything a run produced, and requiring a path would mean carrying
    one back through that flattening for no gain.

31. **Review notes on a plan — the same channel, one step earlier.** #30 gave a person
    somewhere to say something about work that is done. A draft is the other moment worth
    saying something at, and it had the same gap: a reviewer looking at a plan they are not
    ready to approve could reject it, archive it, or type the reason into a chat window
    Chief cannot see. None of those leaves the harness anything to revise against.

    A `ReviewNote` (`note_id`, `step_id`, `step_goal`, `body`, `author`, `created_at`,
    `resolved`, `resolved_at`, `resolved_by`, `via`) hangs off the workflow and rides on the
    document `get_workflow` already returns, so the harness reads feedback with no new call
    and the MCP surface again gains no tool. `step_id` is nullable: some feedback is about
    the plan's shape and belongs to no node.

    Three things are load-bearing.

    **The note is not stored on the step, or anywhere in the workflow document.**
    `revise_draft` replaces title and steps wholesale — that is what makes it a revision
    rather than a patch — so a note kept inside the document would be destroyed by the very
    revision it asked for, leaving the reviewer nothing to check the new plan against. The
    notes live in their own table and are attached on the way out, exactly as `created_at`
    and `updated_at` are attached from the row's columns. That also settles the
    dual-shape trap #30 had to guard against by hand: `WorkflowCreate` and `WorkflowRevise`
    are separate models that never declare the field, so `extra="forbid"` refuses a harness
    submitting notes with no service-layer check needed.

    **A note whose step is gone is orphaned, not dropped and not auto-resolved.** Step ids
    are permanent under amendment (REQ-35), but a *draft's* ids are only as stable as the
    harness chooses to make them — a revision may rename, split or remove a step a note was
    left on. Silently discarding the note loses the feedback; silently resolving it claims
    the feedback was addressed, which is the thing nobody but the reviewer can say. The step
    vanishing may mean the note was acted on, or may mean the plan was restructured around
    it. So the note stays open, flagged, and carries `step_goal` — the goal as it read when
    the note was written — so an orphan says what it was about rather than naming a
    meaningless id. Orphaning is derived against the plan as it stands rather than stored: a
    later revision can bring the id back, and a stored flag would then be a lie.

    In the UI a note is a comment thread on a node: selecting a step opens its thread in the
    inspector beside the graph, and the node carries a count of what is open. The plan's own
    thread is the panel with nothing selected — which is also where orphans go, since the
    node they were left on no longer exists to open them from. It needs a button of its own
    beside Approve: "nothing selected" is true when you arrive and false from your first
    click onwards, so a thread reachable only in that state reads as one that disappeared. The first design put all of
    it in one card with a target dropdown, which was a form for filing feedback rather than
    a place to say something about the thing you are looking at.

    **Resolving is REST-only, like writing.** A session that can close the feedback it was
    given can decide its own work was accepted, which is the loop REQ-13 exists to prevent.
    The harness reads the notes and revises the plan; a person judges whether that answered
    them. Nothing here is enforced, in either direction — a draft with open notes can still
    be approved, and a revision that orphans a note is not refused. Chief records; the
    decision stays with the person.

32. **Projects — a label, and a path that is only ever a memory.** An extension, and the
    second time the same question has been asked. #29 refused to put a `cwd` on the run;
    the proposal here was larger — keep each project's workflows and templates in a
    `.chief/` directory under that project, with a global registry of directory paths.

    That was refused for a reason #29 does not cover. Workflow ids are a 4-hex token, a
    16-bit space, and they are unique **only because one table's primary key enforces it**:
    at 100 workflows two independent stores collide 7% of the time, at 300 it is even
    money. Relocating the store therefore means widening or namespacing ids first — a
    breaking change to every workflow already recorded — before any of the rest. The single
    in-process lock (`Store._lock`, STATUS.md §6) and the global "is anything waiting on
    me" query are real costs too, but secondary: they make it slower and more complicated,
    not wrong.

    What survived is the part that was actually being asked for, in two fields.

    **`project` is a label.** An open string namespace like `harness` (REQ-26). It is *not*
    a directory, and that separation is the point: one product spans several checkouts, and
    one checkout hosts work for more than one. A label is chosen and stable; a path rots.

    **`origin_dir` is provenance.** Where the harness stood when the plan was made. It
    looks like the `cwd` #29 refused and is admissible for one reason: nothing resolves
    against it. The server never reads it, and artifact refs still resolve against the
    folder named in the browser. It answers "which checkout was this?" a month later, and
    it seeds that browser-side folder as a *suggestion* the reader can override — which is
    the only safe way to use a path that may have moved since it was recorded. Not
    accepted on `WorkflowRevise`: a revision made somewhere else overwriting it would turn
    a record into a lie.

    Two consequences worth naming. The browser's folder is now keyed by project, because
    one folder for everything resolves the wrong tree the moment there is a second
    checkout; the unkeyed value stays the fallback so a folder set before projects existed
    keeps working, and an empty string is stored to mean "deliberately none" so clearing
    one is distinguishable from never having set it.

    And labelling is `PATCH /workflows/{id}`, allowed at any status including archived,
    because every workflow that predates this has no label and the ones most worth filing
    are the ones that already ran. `GET /projects` is derived by counting labels rather
    than stored: a project has no lifecycle, so there is nothing to create, rename or
    delete, and nothing that can disagree with the workflows it claims. The unlabelled are
    counted under a null name rather than omitted — on any existing database they are the
    majority, and a list that hid them would hide most of the history.

    Templates carry the label too, and a workflow made from one inherits it. Exporting a
    template writes a file from the *browser*, never the server: what comes out is exactly
    a `POST /templates` body, id included, so it can be committed beside the project and
    registered again idempotently. That is as close to "the project owns its templates" as
    Chief can get without becoming a file server, which #29 already ruled out.

33. **Rendering what a harness wrote.** Summaries, artifact bodies, comments and review notes
    were all rendered as flat text, which meant a newline collapsed to a space and a
    twenty-line write-up arrived as one paragraph. Artifact bodies had a stand-in renderer
    that understood `## ` and `- ` and nothing else.

    The obvious fix is a library, and REQ-21 rules it out: Chief is static files served by
    the same process, no build step and no CDN. A CDN tag makes a loopback tool depend on the
    network; vendoring KaTeX is 300KB of JavaScript plus a font family for text that is
    usually three lines. So `web/markdown.js` is a deliberate subset, and the interesting
    decisions are in what it does when it reaches the edge of that subset.

    **Nothing is assembled as an HTML string.** Every node is created and its text set
    through `textContent`, which escapes by construction, and link hrefs are checked against
    a scheme allowlist so a `javascript:` URL in a reported artifact cannot be clicked into
    execution. There is no sanitiser because there is nothing to sanitise — the parser emits
    elements, never markup. `scripts/test_markdown.mjs` asserts this for headings, lists,
    quotes and emphasis rather than trusting it.

    **Maths is LaTeX in, MathML out**, typeset by the browser's own engine. That is the whole
    reason a dependency-free path exists: the hard part is the typesetting, and every current
    browser already does it.

    **What cannot be rendered is shown, not swallowed.** An expression the translator does not
    understand falls back to its own source on a marked background. A reader who sees
    `$\begin{matrix} a \end{matrix}$` knows what was meant and that it did not render; one
    who sees a blank, or a silently mangled fragment, knows neither. The same principle as
    the artifact `show more` control and the orphaned review note: the failure is visible, and
    the reader decides what to do about it.

    One divergence from CommonMark, on purpose: a single newline inside a paragraph becomes a
    line break rather than a space. CommonMark reflows soft breaks, which is right for prose
    written in a text editor and wrong for both writers here — a harness reporting a summary
    and a person typing in a textarea each mean the break they typed. GitHub made the same
    choice for comments.

34. **Reading the file an artifact names — reversing #29 without reopening it.** #29 refused
    `GET /files?path=` because a path parameter is a traversal surface on a service with no
    auth, and containment would have to be invented and then defended forever. The UI still
    needed to show a file: a page served over http cannot read `file://`, so the bytes have
    to come from somewhere.

    The browser could have read them. The File System Access API grants a page access to a
    folder the person picks, with no server involvement at all, and that was the first
    answer. It is the wrong one here for a reason that has nothing to do with security: the
    UI is often reached through an SSH tunnel from the machine the run happened on, and the
    browser would then read the *laptop's* files, not the host's. A design that fails
    precisely when the files are somewhere else is not a design for a tool used remotely.

    A startup flag naming a root was the second answer, and it does not survive contact with
    #32: every workflow records its own `origin_dir`, and a single root is wrong the moment
    the second workflow runs somewhere else.

    What is here is neither. **The client never supplies a path.** It names a run and an
    artifact — ids Chief issued — and the path is the artifact's own `ref` resolved against
    that workflow's recorded `origin_dir`. The set of readable files is exactly the set a
    harness already reported. There is nothing to traverse because there is nothing to ask
    with, which is a stronger property than any containment check: `secret.txt` next to the
    artifact is unreachable not because a rule refuses it but because no request can name it.

    Four things guard the rest.

    A relative ref is *joined* to a base rather than used as given, so `../../..` could walk
    out of the directory the workflow ran in. That is the one containment this has to enforce
    itself, and it does. An absolute ref is left alone: the harness named that exact file.

    **Nothing is served under its own type.** The response is always `application/octet-stream`
    with `nosniff` and an attachment disposition; the type the browser may render it as
    travels in `X-Chief-Media-Type`, and the UI applies it to a blob it makes itself. An
    `.html` or `.svg` artifact served under its own type would be script executing at Chief's
    origin, next to the run being read. `.html` is readable — as source.

    **A `Host` header outside loopback is refused**, the same DNS-rebinding defence the MCP
    transport already carries, applied to this route alone so nothing that works today can
    stop working. `--allow-host` covers a UI reached under a name.

    Two follow-ons this forced. `origin_dir` and `project` are declared with real pydantic
    field descriptions rather than `#:` comments, because those are what reach a harness
    through the MCP tool's JSON schema — a field an agent sees as a bare nullable string is a
    field it leaves null, and a null directory means no file it reports can ever be opened.
    And `PATCH /workflows/{id}` accepts `origin_dir` as well as `project`, distinguishing an
    omitted field from an explicit null through `model_fields_set`: without that, filing a
    project would silently erase the directory beside it, and without the field at all every
    workflow planned before this could never show a file.

    And a **25MB cap**, with directories, devices and unreadable paths refused by reason
    rather than by exception. `tests/conftest.py` now gives `TestClient` a loopback base URL:
    its default `Host: testserver` would otherwise make every test of this route pass or fail
    for the wrong reason, which it briefly did.

35. **MDX — rendering a file whose point is that it runs.** An MDX artifact is markdown with
    JSX in it: components, imports, and `{expressions}` that a build step turns into a page.
    Chief has no build step (REQ-21) and, more to the point, should not acquire the ability
    to evaluate code out of a file a harness reported — the viewer exists so a person can
    read an artifact, and running one would make reading it an execution.

    So the prose renders and the components are **named rather than run**. A paired
    `<Callout>…</Callout>` becomes a framed block carrying the tag and the props as written,
    with its children parsed as the markdown they almost always are; a self-closing component
    becomes the same frame with nothing inside. Imports and exports fold into one line. A
    `{expression}` is shown on a marked background, unevaluated.

    The frame is deliberately dashed and labelled rather than styled to look like output. A
    component rendered *as if it had worked* would be the worst outcome available — the
    reader would trust a layout that never existed. This is the same bargain as unrenderable
    maths (#33) and the orphaned review note (#31): the failure is visible, the content
    survives, and the reader decides.

    The maths coverage was then measured rather than guessed at. Every `$…$` and `$$…$$` in
    2230 real `.mdx` files — 10,838 expressions — was pushed through `renderMath`, and the
    failures read in frequency order: `\operatorname` (257), `\{` (70), environments (68),
    `\succ` (44), `\varnothing` (39), `\underbrace` (33). Adding those took it from 94.2%
    to 99.6%, and the tests name them in that order so the next reader knows the list came
    from a corpus rather than from someone's memory of LaTeX. The 39 left are almost all
    PromQL and shell fragments that were never maths; the fallback showing their source is
    the right answer for those.

    Frontmatter came along with it and applies to plain markdown too. A leading `---` block
    was previously read as a horizontal rule followed by a paragraph of stray colons, which
    is worse than either rendering or hiding it.

    Vendoring React and the MDX compiler was asked for and refused, and the evidence is what
    settled it rather than the argument. Of 2230 `.mdx` files on the machine this was built
    for, the most-used components are `Callout`, `Quiz`, and a long tail of bespoke
    interactive explorers — `RewardHackingDemo`, `CrossEntropyExplorer`,
    `PerTokenRatioExplorer` — every one of them imported from `@/components/mdx-components`
    or a sibling `.tsx`. Not one file is self-contained. React in the browser would render
    the markdown (already done) and leave every component undefined, or worse, replace it
    with a stand-in Chief invented — a layout that exists nowhere, shown without a hint that
    it is a guess. See #36 for what was built instead.

    One thing worth keeping: a lowercase tag is *not* treated as a component. MDX allows raw
    HTML, and rendering it would undo the guarantee that nothing in an artifact becomes
    markup — so `<script>alert(1)</script>` in an MDX file is text, exactly as it is in a
    markdown one.

36. **Framing a page rather than rendering it.** The thing that can render an MDX file with
    its own components is the project that owns them, and it is usually already running —
    a Next or Astro dev server on another port. So a URL artifact is now *framed* in the
    viewer rather than only linked: the page renders itself, with its real components,
    beside the run that produced it. Nothing is fetched by Chief and nothing is evaluated by
    it. That is the whole feature, and it generalises past MDX — a Storybook story, a
    coverage report, a notebook export.

    The sandbox is the part with a decision in it. `allow-same-origin` is granted, which
    reads alarming and is not: it does not make the frame same-origin with *this* page, it
    only stops the frame being forced into an opaque origin. Without it a dev server loses
    its own storage and every fetch it makes to itself becomes a cross-origin failure — the
    page would frame and then not work. Two different origins stay separated by the browser
    regardless.

    The exception is a URL on Chief's own origin, where `allow-scripts` and
    `allow-same-origin` together *do* let the frame reach out of the sandbox into this page.
    That combination is refused, and the check fails safe: anything it cannot parse gets the
    stricter sandbox.

    Worth recording how nearly that went untested. `smoke_ui.mjs` had replaced
    `globalThis.URL` wholesale to stub `createObjectURL`, which took the constructor with it
    — so `new URL(...)` threw, the origin check fell into its fail-safe branch, and the test
    happily asserted a sandbox the browser would never produce. A stub that replaces a
    built-in instead of extending it does not fail; it answers a different question.

37. **Running a document's own components.** #35 rendered MDX as prose with its components
    named, and that was the wrong place to stop: MDX minus components is markdown with extra
    syntax, so supporting the format at all was close to pointless. The objection that kept
    it there was that components live in a repo behind a bundler — true of a site's MDX, and
    not true of what a workflow produces, which is written by the agent that also writes the
    document.

    Two constraints made it tractable, and both came from the person asking for it. **The
    components sit beside the document**, so the module graph is derived from the file's own
    imports and confined to its directory — the client still never names a path (#34 holds).
    And a document is **agent output**, so it can be asked to use plain `.jsx` rather than
    TypeScript.

    What was refused is vendoring React and a compiler. React 19 ships no UMD build and
    Sucrase no browser bundle, so either means adding a build step to a UI that is
    deliberately static files with none. Neither turned out to be necessary. A survey of
    2230 real component files found exactly one dependency — `react` — and five hooks in use.
    So `mdx-runtime.js` is a hyperscript renderer with those five hooks, and `jsx.js` is a
    scanner rather than a parser: **JSX does not need its expressions parsed**, only
    brace-balanced, because the browser evaluates them. That is why a hand-written transform
    is a few hundred lines instead of a compiler.

    Three things are load-bearing.

    **Execution happens in a sandboxed frame at an opaque origin** — `srcdoc` with
    `allow-scripts` and deliberately without `allow-same-origin`. Everything the frame needs
    is inlined, so it fetches nothing and nothing it runs can reach the page reading the run.
    This is the same boundary #36 established for framing a URL, used for a different reason.

    **A component's children are markdown, not text.** `<Callout>` around three paragraphs
    has to render as three paragraphs, so the children go back through the markdown renderer
    and the result is one tree — which is also what lets a component hold state normally.

    **Failure is loud.** The transform throws on anything it cannot account for, the frame
    shows the compile error, and a document whose components cannot be resolved falls back to
    prose with them named. Nothing renders an approximation of a component that never ran —
    the rule the LaTeX translator set (#33) and the reason the named frame is deliberately
    ugly (#35).

38. **`ArtifactRef.data` does two jobs, and the UI has to tell them apart.** An artifact may
    carry `data` instead of a `ref` — the model requires one or the other — so `data.text` is
    sometimes the document *itself*. It is also where a harness puts facts about a file that
    lives elsewhere: dimensions, a row count, a digest. On one real database the split was
    seven of the first kind to three of the second.

    That conflation is not worth a second field. `ArtifactRef` appears in six places in the
    contract and #30 already refused to split it; two near-synonym dicts would leave both
    conventions live forever and give a harness author a choice with no right answer.

    So the *display* separates them instead. `text` is dropped from the facts, because it is
    already rendered above as the preview and showing it twice reads as a bug. What remains
    is shown inline on the card, the same way an instance's metadata is shown on its row —
    scalars read without a click, structure kept behind a fold. A fold labelled "data" was
    the first attempt and it failed the only test that matters: the person who asked for
    metadata to be visible could not find it.

    A summary needs a route to the rest. Inline scalars answer "which branch is this" at a
    glance and answer nothing else, so every metadata block carries a `{ }` control that
    opens the whole value in the file viewer — the same folding tree a JSON artifact gets,
    at a width that can be dragged. One place to read something, whether it came off the
    disk or out of a run's own record.

    Which is the theme of the three attempts this took. Metadata was stored and rendered
    nowhere; then rendered on steps, where the data was not; then folded on instances and
    artifacts, where nobody would open it. "Present in the DOM" is not "visible", and the
    smoke harness learned the same lesson at the same time — five metadata checks were being
    computed, printed and asserted nowhere, so it reported `artifact folded=false` and called
    the run green.

## 39. Criteria, and a gate Chief cannot actually judge

Goals in this store had grown to a median of 118 characters and a maximum of 902, and
reading the long ones showed why: they were absorbing acceptance conditions. One ends
"Output: one ranked slate, the rejected ideas and why"; another buries "unit-tested against
hand-written correct, incorrect and malformed inputs" three sentences in. Those are criteria
written as prose — conditions that decide whether the step is done, sitting in a field
nothing can enumerate and a reader has to hunt through.

So `WorkflowStep.criteria`, authored as a plain list of strings and numbered `c1`, `c2` on
the way in. Positional ids rather than supplied ones: an id nobody types is an id nobody
gets wrong, and criteria are only ever replaced wholesale by an `update_step` amendment —
there is no operation that inserts one mid-list and leaves the rest addressed as they were.
Attestation is keyed by that id and not by the text, because prose-matching would silently
stop matching the moment an amendment reworded something.

Positional ids do leave one edge, and it is closed rather than tolerated: an amendment may
reword c1 while evidence for the *old* c1 is already recorded, and that evidence would then
stand as an answer to a question nobody asked. So `update_step` drops the recorded evidence
for any criterion whose text changed, and for any that no longer exists — the ones left
alone keep their answers, so adding a fourth criterion does not discard the three already
met. `replay_step` clears the lot for the same reason at a larger scale: an answer belongs
to the attempt that produced it, and carrying it across a replay would let the gate pass
vacuously on exactly the path someone reached for because the first result was wrong. The
snapshot in `history` keeps both.

**Task steps only.** The discriminating question is whether there is an attestation point: a
construct's status is derived from its instances and a checkpoint's outcome is a person's to
give, so neither has anywhere to answer for a criterion. The steps inside a construct's body
do, and that is where a construct's criteria belong.

**No `max_length` on the goal.** The length guidance lives in `Field(description=...)`, which
reaches every harness through the MCP schema, and in the skill. A hard cap would reject
`create_workflow` outright and make the existing 902-character goal unrevisable, and a
rejected plan is a worse failure than a long goal.

**Be honest about what the gate is.** Chief refuses `completed` on a step whose criteria are
not all answered — but it cannot judge whether any of them is *satisfied*. It has no access
to the work and never will. What it enforces is that each criterion was addressed by name,
which is forced enumeration, not verification. That is worth having for exactly the reason
REQ-48 requires a real summary: the cost is one sentence per criterion, and what it catches
is a step called done while a condition someone wrote down was quietly skipped. The refusal
names all three ways out — keep working, report `failed`, propose an amendment — because a
gate with no stated escape hatch is a trap for a harness facing an impossible criterion.

Answers accumulate across updates, like artifacts and metadata: a criterion answered on the
way through does not have to be restated at the end.

Existing workflows are untouched and keep their long goals. Rewriting an approved plan's
steps is `update_step` amendment territory, and `history_edit` where a step has already
completed — a person's call, not a silent migration. A step with no criteria behaves exactly
as it always has, which is both the backwards-compatibility story and the "criteria are
optional" story; they are the same story.

## 40. Instance parameters, and two substitution systems that must not collide

A `parallel` step's branch count is decided at runtime, so what distinguishes a branch can
only arrive at runtime — in the instance's `metadata`. That worked from the start, and a
well-written run uses it exactly as intended: five branches, each carrying its own project
name. What was missing shows up in the runs that did not. A construct fanning out over three
ablation variants, every branch registered with `metadata={}` — and afterwards nothing can
say which was which. Nothing had required it.

So `instance_params` on a construct: the names each instance must supply. Loop and parallel
only — a task runs once and has nothing to tell apart.

**Unknown keys stay welcome.** A checkpoint refuses a response key it did not ask for, and
copying that here would be wrong: instance metadata is load-bearing free-form — seeds,
timings, token counts — and refusing the undeclared would make declaring a parameter cost
more than it gives. The declaration names a *required subset*, not a schema.

**Presence, not truthiness.** `CheckpointResolution.response` is `dict[str, str]`, so a
blank check suffices there. `InstanceCreate.metadata` is `dict[str, Any]`, where `0`,
`False` and `[]` are real answers. Only an absent key — or a string blank once trimmed — is
missing.

**Validation is at registration, once.** An amendment that adds a parameter to a construct
does not retroactively invalidate branches already registered under the looser plan. That is
REQ-14's concern in a different shape: a result was valid when it was recorded.

### The placeholder collision

`{{ paper }}` now has two possible meanings: a template parameter, substituted once when the
template becomes a workflow, and an instance parameter, substituted per branch when the plan
is read. Three rules keep them apart.

**Scoped by the construct, never globally.** A placeholder in a body step resolves against
*its own construct's* declarations. A global exclusion set would mean declaring `paper` on
one construct silently stopped `{{ paper }}` being checked anywhere else in the plan — and
the template would then render a workflow with a placeholder nobody ever fills in.

**A name that is both is an error, not a shadow.** Silent shadowing in a substitution system
is the quieter failure and the worse one; a plan where `{{ paper }}` means two things is a
plan that disagrees with itself.

**`_render_text` leaves unknown placeholders standing** rather than raising. An instance
parameter must survive instantiation intact, because it is filled in long after the template
became a plan. Everything that *should* have been a template parameter was already checked
by `validate_template`, so this loosening costs nothing.

### Rendered at read time, never stored

The plan document keeps `{{ paper }}` literal. The UI substitutes when it draws a branch;
nothing rendered is written back. Storing rendered copies of body-step text inside run state
would put a second copy of the plan in the run, free to drift from the definition it came
from — and `instance.body` already exists precisely so a finished iteration remembers what
it had to do.

A harness needs no rendering at all: `get_run(include_plan=true)` gives it both the plan and
its own instance metadata, which is what it already reads to know which branch it is on.

One consequence to hold on to: **rendering must not reach a criterion's id or the completion
gate.** `criteria_met` is keyed against the criterion *as written*, `{{ paper }}` and all, so
every branch answers `c1` whatever its own value is.

## 41. Deleting a workflow — what the cascade stops at

Chief already had `archive`, and archiving is the right answer for a plan that ran and is
finished. It is the wrong answer for the mis-generated draft, the duplicate, the plan
submitted while testing something else. Those keep asking to be approved forever, in the one
list a person reads to decide what needs them, and "archived" is a lie about what happened.
So `DELETE /v1/workflows/{id}` removes the record and everything it owns: its versions, its
runs and their step states, its amendments, its review notes.

The interesting part is not the cascade but where it stops.

**The audit log stays, and gains an entry.** REQ-20 makes it append-only and the storage
module has no delete path for it at all. It would have been easy to write the cascade as
"every table with a `workflow_id` column", which is exactly the phrasing that would have
taken the trail with it — a deletion that erases the record of itself is not auditable. The
entry records the title, the status, the run ids and the row counts, because after the fact
that is the only remaining description of what was there.

**A template saved from the workflow stays.** It became an independent document the moment
it was saved; keeping the plan for next time is the whole point of saving one.

**Nothing on disk is touched.** Chief records references to artifacts, not their contents
(REQ-46). A tracker that deleted a person's actual work because they tidied their workflow
list would be a tracker nobody could safely tidy, and the confirmation says so out loud.

**A running execution does not block it.** The refusal would be the wrong shape: the run is
not a lock on the record, and someone deleting a workflow mid-run has almost always just
decided the run is the thing they want gone.

### Not on the MCP surface

`approve_workflow` is already a decision the harness may only make on an explicit human
instruction in the turn (MCP-SURFACE.md §3). Permanently erasing a plan and the record of
what it did is strictly further down that road, and there is no agent session that
legitimately needs it. It is REST and the web UI only — the first entry in `REST_ONLY` that
is excluded for being destructive rather than for being self-approval or housekeeping.
