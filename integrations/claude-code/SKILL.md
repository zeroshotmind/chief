---
name: chief
description: "multi-step work → workflow graph (from a template, or new) → tracked execution in Chief, with amendments when the plan stops fitting"
trigger: /chief
---

# Chief

Chief holds the plan and the state of the work. It never executes anything — you do, and
you report what happened. The tools come from the `chief` MCP server; if they are not
available, say so rather than working untracked.

## Check for a template first

`list_templates` before planning anything. A template is a plan someone already approved the
shape of; `create_workflow_from_template` with its parameters beats composing the same plan
again by hand, and the result may not even need approving if a policy covers it.

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
  sentence) and the **harness** that will run it — `claude-code` when that is you. The
  namespace is open strings, so spelling drift fragments it: match what existing workflows
  already use (`list_workflows` shows you) rather than inventing a variant.
- Order comes from `depends_on`, never from position in the list.
- **Independent work you can name at plan time is a fan-out**: ordinary steps side by side
  (no dependency between them), fanning back in when a later step depends on all of them.
  Not a construct — build + tests + docs is three steps and a join, and drawing them as a
  chain misstates the plan.
- **A `parallel` construct is for branches you cannot count until you are running** — one
  branch per failing test you find, per repo the scan turns up. Give it a `body`; never
  declare how many branches, you register each as it happens.
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

The workflow comes back as a **draft**. It cannot take a run until it is approved.

## Fixing a draft

While it is still a draft, `revise_draft` — the same workflow, a corrected plan. Do not
create a second workflow to replace one you just made: the reviewer then has two drafts and
nothing saying which counts. It replaces the whole plan, so send every step you want to
keep. Once approved, changes go through an amendment instead.

## Then run it

`approve_workflow` (see below on who decides) → `register_run` → work the steps.

Report each step with `report_step_update`: `running` when you start it, then `completed`
or `failed`. `path` is `["step_03"]` for a top-level step; inside a loop or parallel body
it is `["step_06", "inst_01", "step_09"]`.

**Every update needs a summary, and it must be worth reading.** It is what a person sees to
understand the run without opening the artifacts. "Done" is not a summary; "migrated 14
call sites, 2 needed a manual null check" is.

For a loop or parallel step: `register_step_instance` opens each iteration or branch,
report the body steps inside it by path, and `report_instance_update` when the instance
finishes. Set `instances_closed` on the construct once no more are coming.

Artifacts are JSON metadata — a path, a URL, an id — never file contents.

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

`approve_workflow`, `approve_amendment`, `reject_amendment` and `resolve_checkpoint` are
human decisions. Call them only when the user has asked you to **in this turn** — never to unblock yourself, and
never as a step in a plan you are executing. Chief records which transport a decision
arrived on, so an approval you made is distinguishable afterwards from one made in the UI.

The approval policy is deliberately not a tool. If auto-approval needs to change, that is
the user's to do in the web UI at `/ui`.

## Picking up where you left off

`list_runs` finds the run, `get_run` with `include_plan=true` returns both the state and
the plan — the goals, the harnesses, the dependencies. That is enough to work out what is
done and what is next without asking the user to re-explain.
