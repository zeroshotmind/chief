---
name: chief
description: "multi-step work → workflow graph (from a template, new, or a machine-checked proof graph) → tracked execution in Chief, with amendments when the plan stops fitting"
trigger: /chief
---

# Chief

Chief holds the plan and the state of the work. It never executes anything — you do, and
you report what happened. The tools come from the `chief` MCP server; if they are not
available, say so rather than working untracked.

**Loading this skill is not a cue to call anything.** Each tool below is named at the moment
its answer changes what you do next — reach for it then, not on arrival: `list_templates`
when you are about to compose a plan, `list_workflows` when you need a harness name or
project label you are unsure of, `list_runs` when you are picking up existing work. A
question the conversation already answers is not worth a call.

## Check for a template first

When you are about to compose a plan, `list_templates`. A template is a plan someone already
approved the shape of; `create_workflow_from_template` with its parameters beats composing
the same plan again by hand, and the result may not even need approving if a policy covers
it.

`get_template` shows which parameters it needs. Supply every required one — a missing or
misspelled name is refused rather than quietly defaulted.

If a workflow you ran turns out to be worth keeping, `create_template_from_workflow` with
`substitutions` mapping the literals that made it specific to parameter names
(`{"acme/api": "repo"}`). Prefer distinctive literals: substitution is textual, so a short
one over-matches.

## Otherwise plan it

With no template that fits, `create_workflow` with the whole plan as a graph. **Find the
shape before writing steps.** A chain of six tasks is usually a plan that was typed, not
designed — before composing, ask three questions of the work: what is *independent*, what
*repeats*, and what gets *decided*. Each has its own construction:

- One step per unit of work. Every step needs a **goal** (what done looks like, in a
  sentence) and the **harness** that will run it — `claude-code` or `codex`, whichever
  you are. The
  namespace is open strings, so spelling drift fragments it: match what existing workflows
  already use rather than inventing a variant — `list_workflows` shows you, worth a call
  only when the name in use is actually in doubt.
- **Keep the goal to two or three lines, and put what decides "done" in `criteria`.** A
  goal that runs long is nearly always one with acceptance conditions buried in its prose —
  "…unit-tested against hand-written correct, incorrect and malformed completions", three
  sentences in, where nobody can enumerate it. Split those out: `criteria` is a list of
  short checkable statements, written as plain strings, and the goal is left saying what the
  step is for. Task steps only; a construct's criteria go on the steps in its body.
- Order comes from `depends_on`, never from position in the list.
- **Independent work you can name at plan time is a fan-out**: ordinary steps side by side
  (no dependency between them), fanning back in when a later step depends on all of them.
  Not a construct — build + tests + docs is three steps and a join, and drawing them as a
  chain misstates the plan.
- **A `parallel` construct is for branches you cannot count until you are running** — one
  branch per failing test you find, per repo the scan turns up. Give it a `body`; never
  declare how many branches, you register each as it happens.
- **Declare `instance_params` on a construct: what tells one branch or iteration from
  another.** `[{"name": "paper"}, {"name": "pdf_path"}]` for a step that fans out over
  papers. Every instance must then supply a value for each when you register it, so a run
  cannot end up with eight branches nobody can tell apart. Body steps may write
  `{{ paper }}` in a goal or a criterion and it is filled in per branch when the plan is
  read — so write the body once, generically, and let the value arrive with the instance.
- **"Do this, check it, and try again if it isn't good enough" is a `loop`** — so is any
  retry, bounded attempt, or iterate-until-it-passes. The check goes inside the `body` as
  its own step, and the exit condition goes in the loop's **`exit_when`** ("held-out
  accuracy beats baseline and the audit is clean"): the exit is a decision, and naming it
  there puts both arrows on the graph — condition met, continue past the loop; otherwise,
  another iteration. A bound ("at most 3") belongs in `exit_when` too.
- On a loop or parallel, **`on_instance_failure`** says what one failed instance means:
  `fail_fast` (default) fails the construct, `continue` lets the remaining instances run. A
  sweep usually wants `continue`; a pipeline does not.
- **Somewhere a person has to say go, that is a `checkpoint`** — sign-off before anything
  irreversible or costly, a judgement call you should not be making, or a value only they
  can supply. Its `harness` is `human`. It can also *ask* for something: `fields` is a list
  of `{name, label, hint, required}`, one per thing you need in writing (an API key belongs
  in their environment, not here). Plan it in whenever you would otherwise have stopped to
  ask in chat — the wait becomes part of the record instead of something the transcript
  alone remembers.
- **An either/or fork has no edge type** — Chief's graph cannot draw "if X then A else B"
  between steps. Plan through the step that decides, state both expected outcomes in its
  goal, and bring the chosen branch in as an amendment once the answer lands. That is a
  *foreseen* amendment, and saying so in the goal is what makes it legible; anything
  foreseeable that is *retry-shaped* still belongs in the graph as a loop.

**Say what it belongs to.** `create_workflow` takes `project` — a short label for the body
of work ("chief", "songs"), which is how a person finds this later among everything else —
and `origin_dir`, the directory you are working in. Match a label already in use rather than
inventing a variant of it — `list_workflows` shows what is in use, when the conversation
does not already say; a project is not a directory, so two
checkouts can share one label and one checkout can carry work for two. Leave `project` out if
the work does not belong to anything in particular. `origin_dir` is a record of where you
stood, not a path anything resolves against, so give it as you see it.

The workflow comes back as a **draft**. It cannot take a run until it is approved.

## Or prove the plan first

When the steps have real preconditions between them — each one depending on conditions an
earlier artifact must satisfy, wrong expensively and discovered halfway through — write the
plan as a **proof graph** instead: a Lean file where each step declares what it demands of
its inputs and what it promises about its output, and the server checks that every promise
actually satisfies the demand it feeds, for every possible value, before anyone is asked to
approve anything. A short errand does not need this.

The flow: `create_proof_graph` with the whole file as `lean_source` (it takes `project` and
`origin_dir` like `create_workflow`) → `verify_proof_graph` → read
`verification.diagnostics` — they name the exact condition that does not follow, with both
sides in view — → `revise_proof_graph` → verify again until it holds → `compile_proof_graph`.
What comes out is an ordinary draft workflow: it still needs approval and a run, and the
proven conditions travel with it as the steps' inputs and criteria. When a verification
fails, fix whichever side is untrue of the work — strengthen the upstream promise or weaken
the downstream demand — never whichever makes the message go away.

The vocabulary lives in the server repo's `lean/` directory: `ProofGraph.lean` documents all
of it and `Examples/Pipeline.lean` is a complete worked graph — read them before writing
your first one. The rules that cost the most to learn from diagnostics alone:

- Write each step as a `def` whose parameters are the handles it consumes, and put `use` at
  the call site in `graph` — never inline in the step's own `inputs` list. Inline, the proof
  goal is stated against a metavariable, and the error names neither `use` nor the fix.
- The graph must be named `graph`, have type `GraphM Unit`, and end with `pure ()`; the file
  must end with `#eval emitGraph "<title>" graph`.
- Bind contracts with `abbrev`, never `def`.
- A graph whose contracts are all `Contract.any` verifies and claims nothing. The stats
  count how many conditions actually constrain something, and a reviewer reads that number —
  refine the conditions that matter rather than shipping a green badge on empty claims.
- Inputs fixed before anything runs — a config, a spec, a URL — are `given` fixed artifacts
  on the step, not steps of their own. Groups (`group :=`, `describeGroup`), derived
  artifact schemas (`artifact_schema`), and per-step `algorithm` pseudocode are all
  optional: reach for them when the plan is big enough to be hard to read without them.

A reviewer leaves `review_notes` on a proof graph exactly as on a workflow draft:
`get_proof_graph` returns them, addressing them is `revise_proof_graph` with the `reason`
saying which note each change answers, and marking one resolved is theirs, not yours.

## Fixing a draft

While it is still a draft, `revise_draft` — the same workflow, a corrected plan. Do not
create a second workflow to replace one you just made: the reviewer then has two drafts and
nothing saying which counts. It replaces the whole plan, so send every step you want to
keep. Once approved, changes go through an amendment instead.

**`get_workflow` before you revise, and read `review_notes`.** That is where the reviewer
put what they want changed — each note on a step, or on the plan as a whole. Address every
one with `resolved: false`, and say in the revision's `reason` which note each change
answers. A note whose step you removed comes back marked `orphaned`: it is still open, and
`step_goal` says what it was about. If you disagree with a note, say so and leave the plan
alone — do not revise it into something the note did not ask for.

Marking a note resolved is theirs, not yours; so is writing one. There is no tool for
either, deliberately.

## Then run it

`approve_workflow` (see below on who decides) → `register_run` → work the steps.

Report each step with `report_step_update`: `running` when you start it, then `completed`
or `failed`. `path` is `["step_03"]` for a top-level step; inside a loop or parallel body
it is `["step_06", "inst_01", "step_09"]`.

**Before reporting a step `completed`, go through its criteria yourself and check each one
actually holds.** Do the checking — run the suite, open the file, look at the output — and
pass `criteria_met` keyed by criterion id (`c1`, `c2`, …), each with a sentence of what
satisfied it: "all 314 tests pass, see the log artifact", not "yes". Answers accumulate, so
record each as you go rather than all at the end.

If one does not hold, **that step is not finished**. Keep working and report again. If it
cannot be made to hold, report `failed` or propose an amendment changing the criterion —
never report completion around one, and never write an answer for a criterion you have not
actually checked.

Chief will refuse `completed` while any criterion is unanswered and name the ones it is
waiting on. Treat that refusal as a backstop you should rarely see, not the thing that tells
you what is left: it can only detect a criterion you said nothing about, never one you
answered carelessly. Chief cannot check whether a criterion truly holds — you can.

**Every update needs a summary, and it must be worth reading.** It is what a person reads to
know what happened and whether they need to open anything — not a replacement for the
artifacts. "Done" is not a summary; "migrated 14 call sites, 2 needed a manual null check"
is.

**Two or three sentences.** If the step produced more than that — a list of findings, a
table, a comparison — write it to a file, register it as an artifact, and let the summary
say what it found and point at it. Long prose in a summary is unreadable at a glance and
unsearchable afterwards, and it is the one place in Chief that cannot be reopened as a
document. Shortening without writing the detail somewhere is losing it, not summarising.

For a loop or parallel step: pass each instance's `instance_params` in its `metadata` when
you register it — it is refused without them. Read your own instance's metadata to know
which branch you are on; the body step's `{{ paper }}` is display, and the value in metadata
is the source. `register_step_instance` opens each iteration or branch,
report the body steps inside it by path, and `report_instance_update` when the instance
finishes. Set `instances_closed` on the construct once no more are coming.

**Artifacts are references, never file contents** — a path, a URL, an id. Two things follow
from the web UI being able to open them.

Give the **path you actually wrote to**, relative to the `origin_dir` you set when you
planned. The UI resolves it and shows the file: markdown rendered, images and PDFs inline,
code and logs as text. A path that is close but not right reads as a missing file.

Put what you know about the output in **`data`** — a row count, dimensions, a digest, how it
was produced. It shows beside the artifact. `data.text` is the exception: it holds the
content itself for an artifact with no file, and is rendered as the document rather than as
metadata. An artifact needs one of `ref` or `data`, so "no file, just facts" is a legitimate
artifact.

**`metadata` on an update is worth filling in.** Token counts, cost, timings, a commit, a
seed — it is shown in the UI and merged across updates, so a later one adds to it rather than
replacing it. On a loop or parallel instance it is the only thing that distinguishes one
branch from another: without it a person sees "Branch 1 … Branch 8" and cannot tell which was
which. `register_run` takes the same field for what set the whole run going.

If you write an `.mdx` document, components **beside it** in the same directory are compiled
and run when someone opens it — plain `.jsx`, no TypeScript, and imports resolved only from
that directory. Components imported from elsewhere in a repo cannot be resolved and are shown
named but not run.

## When you reach a checkpoint

Report it `running` — that is you saying you have arrived, and Chief marks the step
**blocked**. Then **stop and tell the user**: what is being asked, and what you will do with
each answer. Do not report it `completed` or `failed`; the server refuses that, because how
a checkpoint turned out is not yours to say.

When they answer, `resolve_checkpoint` with `decision` (`approved` / `rejected`), their
`response` keyed by the field names the step declared, and a `note` — required to reject.
They may also answer in the UI, in which case the decision is simply there the next time you
`get_run`: it is on the step's state as `checkpoint`, with what they typed under `response`.
Read it from there rather than from memory of the conversation.

A rejection fails the step, and everything downstream is skipped. That is the point — it
stops the branch. If the work should continue down a different route, that is an
amendment.

## When an ordinary step needs something only a person knows

A checkpoint is a person-decision the plan named in advance. Most of the time you will hit
something the plan did not — a step you have already reported `running`, partway through,
turns out to need a name, a preference, a "which one" only the user can give. `ask_question`
with `text` saying what you need, and `fields` if the answer should be more than one
free-text sentence (same shape as a checkpoint's — omit it for free text). This **blocks the
step without ending it**: unlike a checkpoint, nothing about the step's outcome is decided
here.

Then **stop and tell the user what you asked**, the same as reaching a checkpoint. When they
answer — in the UI, or by telling you and you relaying it with `answer_question` — the step
goes back to `running` and you keep working. Read the answer off `get_run`: it is on the
step's `questions`, the entry whose `question_id` you asked with, under `response` (a single
`text` key for free text, the declared field names otherwise). Do not guess at an answer or
carry on without it while a question is open.

## When the plan stops fitting

A step you cannot execute as written is the whole reason Chief exists. **Propose an
amendment. Do not improvise around it, and do not report success on something you worked
around.**

`propose_amendment` with the operations you want (`insert_after`, `insert_before`,
`update_step`, `remove_step`, `replay_step`) and a reason that explains what you hit.

Then **the run pauses and you wait.** Nothing is applied until a human approves it. Poll
`get_amendment`, or `list_amendments` with `status="pending_approval"`. Tell the user the
run is waiting on them and what you are asking for — do not sit silently in a poll loop,
and do not carry on with the old plan. If you change your mind, `withdraw_amendment`.

A completed step is immutable. Changing one is a `history_edit` amendment, it always needs
an explicit human decision, and no policy can auto-approve it. The original result is kept
either way.

## What is not yours to decide

`approve_workflow`, `approve_amendment`, `reject_amendment`, `resolve_checkpoint` and
`answer_question` are human decisions. Call them only when the user has asked you to **in
this turn** — never to unblock yourself, and
never as a step in a plan you are executing. Chief records which transport a decision
arrived on, so an approval you made is distinguishable afterwards from one made in the UI.

The approval policy is deliberately not a tool. If auto-approval needs to change, that is
the user's to do in the web UI at `/ui`.

## Picking up where you left off

`list_runs` finds the run, `get_run` with `include_plan=true` returns both the state and
the plan — the goals, the harnesses, the dependencies. That is enough to work out what is
done and what is next without asking the user to re-explain.

**Read the `comments` on the artifacts while you are there.** They are what a person said
about an output after you reported it — "this draft is the one, match its tone", "the
numbers in here are stale". Nobody will repeat them to you, and building on an artifact
someone has commented on without reading the comment is how you redo rejected work. They
are yours to read, not to write: a comment is what you were *told*, so there is no tool to
add one.
