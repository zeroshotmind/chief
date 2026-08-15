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
