# Using Chief

What each part of Chief is for, and why it behaves the way it does. If you just want it
running, that is in the [README](../README.md).

---

## Tracking a piece of work

Tracking is **opt-in per task**, not a default. Ask for it — "track this in Chief", or `/chief`
— and the agent plans first: one step per unit of work, each with a goal and the harness that
will run it, ordered by explicit `depends_on` edges rather than by position.

That plan arrives as a **draft**, and a draft cannot take a run until you approve it. This is
the point of the tool. Read the graph in the UI, then approve it — or don't, and say what is
wrong (see below). Once approved the agent registers a run and reports each step as it starts
and finishes.

## Review notes — saying what is wrong with a draft

A plan you are not ready to approve is the normal case, and "say what is wrong" should not mean
typing it into a chat window Chief cannot see. Every workflow takes **review notes**.

**Click a node in the graph and the thread for it opens beside the plan**, with a box to add
to it — the same shape as commenting on a post. The box is a resizable textarea, because a
useful note is usually a sentence or two; Enter gives you a newline and ⌘/Ctrl+Enter sends. A node carrying feedback shows a 💬 count, so you can see what
has something to read without opening every one.

For feedback about the plan itself — "this is a chain and it should fan out" — there is a
**Feedback on the plan** button beside Approve, carrying its own count. That is also where a
note goes when the step it was left on is removed by a revision.

The agent reads them off the plan it fetches before revising — no extra call, and nothing to
repeat. Once it has revised, mark the notes it answered **resolved**; they fold away behind a
"resolved (n)" toggle, so what is still open stays readable through several rounds. Resolving
is yours alone, as writing is: a session that could close the feedback it was given could
decide its own work had been accepted.

If a revision removes the step a note was on, the note is **not** dropped and **not** quietly
resolved. It moves to the plan's thread — there is no node left to open it from — reading *was
on step_04: draft the migration script*, the goal that step had when you wrote the note. The
step disappearing might mean you were listened to, or might mean the plan was restructured
around you, and only you can tell those apart.

Nothing here is enforced. A draft with open notes can still be approved, and a revision that
orphans a note is not refused — Chief records the decision, it does not make it.

## When the plan stops fitting

A step the agent cannot execute as written is the case Chief exists for. Instead of improvising
around it, the agent proposes an **amendment** — insert a step, change one, remove one, replay a
failed iteration — and **the run pauses until you decide.** Pending amendments show up in the
approvals inbox, with the proposed changes drawn into the plan graph as dashed ghost nodes, so
you review the plan you are approving rather than a patch document.

Anything touching a step that already finished is a **history edit**: it always needs an explicit
decision, no policy can auto-approve it, and the original result is kept either way.

## Checkpoints — making the agent wait for you

Sometimes you want the run to stop and ask, without waiting for it to hit a problem. A
`checkpoint` is a step type whose harness is a person. The agent reports reaching it, the run
blocks, and it waits. A checkpoint can also declare **fields** — things it asks you for in
writing ("what is the budget?", "which variant?") — and your answers are recorded on the run
where the agent reads them back.

Approving completes the step. Rejecting fails it, which skips everything downstream — a
rejected checkpoint stops that branch of the plan rather than quietly letting it proceed. A
rejection needs a note saying why; approving does not.

## Comments — telling the agent something about work that is done

Every artifact a run produces takes **comments**. "This draft is the one, match its tone."
"The numbers in here are stale." They hang off the artifact and ride on the run state the
agent already fetches when it picks the work up, so nothing has to be repeated and no tool
call is needed to find them.

Comments are yours to write, not the agent's — a harness annotating its own output with its
own opinion of it is what the step summary is already for. They are append-only. Review notes
are the same channel one step earlier: a comment is said about work that is done, a note about
work that has not started.

## Opening the files a run produced

Artifacts are references, not blobs (REQ-46) — a harness reports `songs/personas.md`, relative
to wherever it was working. Chief does not record that directory, so **set a project folder**
in the artifacts panel: paths then resolve into editor links (`vscode://file/...`) and the copy
button hands you the full absolute path. The setting lives in your browser, so the same run
opened on another machine resolves against that machine's checkout.

## When a plan is bigger than the window

A wide fan-out — twelve independent steps side by side — needs more width than any window
has. The graph draws at the width the plan actually needs and the viewport **scrolls
sideways**; it is not scaled down to fit, because a twelve-wide plan shrunk to a third is a
picture of a plan rather than one you can read.

Artifact descriptions wrap rather than ending in an ellipsis, and anything genuinely long —
a description over two lines, a markdown preview over nine — collapses with a **show more**
control. Nothing is ever collapsed without one.

`scripts/seed_stress.py` builds both shapes against a running server if you want to see them:

```bash
python scripts/seed_stress.py --base http://127.0.0.1:8080/v1
```

## Reading an artifact's file

**Click the path** and it opens in a drawer down the right. A URL artifact is **framed** —
your dev server renders it, Chief only shows it — and a file — markdown through the
is read by the server: markdown
through the renderer, images inline, PDFs in the browser's own viewer, **JSON as a tree you
can fold**, code and logs as text, anything else as a size and a download. Clicking the name of a thing to
see the thing is what a reader tries first, so that is what it does; the editor deep link is
the small **↗** beside it, since most of the time the question is "what is in this" rather
than "let me change it".

**The open file is in the URL** — `#/workflow/wf_wide/art_1957` — so a refresh lands back on
it and the link can be sent to someone. Nothing else about how you are looking at the page is:
filters, the selected node and the panel widths stay out, because they are *how* you are
looking rather than *where* you are. An id that names an artifact the run does not have is
dropped rather than retried, and the URL heals itself back to the workflow.

It opens down the **right** of the window, because a file is read *against* the run that
produced it and a panel along the bottom pushes the plan off the screen to do that. Drag its
left edge to resize — up to 70% of the window, so a wide log still has room — and the width is
remembered. The page is inset rather than covered, so the artifact list you opened it from
stays reachable for the next one.

**Chief's server reads the file**, which is the point: it works when you reach the UI through
an SSH tunnel from the machine the files are actually on. A browser-side file picker cannot
do that — it would read your laptop, not the host.

That is the one place Chief touches the filesystem, and it is deliberately narrow:

- **You never send a path.** The URL names a run and an artifact, both ids Chief issued, and
  the path comes from the artifact's own `ref` resolved against the workflow's `origin_dir`.
  There is no `?path=` to traverse. The only readable files are ones a harness already
  reported.
- **A relative ref cannot climb out** of the directory the workflow ran in.
- **Nothing is served under its own type.** The response is always opaque bytes; the type the
  browser may apply travels in a header and the page applies it itself. An `.html` or `.svg`
  artifact served under its own type from Chief's origin would be script running next to the
  run you are reading.
- **A `Host` header that is not loopback is refused**, which blocks DNS rebinding — a page on
  the open web pointing its own domain at `127.0.0.1` so a fetch looks same-origin. If you
  reach the UI under a name rather than `localhost`, allow it explicitly:

  ```bash
  chief --port 8080 --allow-host chief.internal
  ```
- **25MB cap**, and directories, devices and missing files are refused with a reason.

A workflow with no `origin_dir` cannot resolve a relative ref, and says so rather than
guessing. New plans record it themselves — the agent passes the directory it is working in
when it calls `create_workflow`. For anything planned before Chief asked for one, **set the
directory on the workflow detail screen** and its files become readable; that is the only
route, since a revision is deliberately not allowed to rewrite where the work happened.

## Markdown and maths

Artifact bodies, step summaries, comments and review notes are rendered rather than dumped
as one run-on line. **Newlines are kept** — a single one is a line break, not a space, because
a harness reporting a summary and a person typing in a textarea both mean the break they
typed. On top of that: headings, bold and italic, `code` and fenced blocks, lists,
blockquotes, links, and **LaTeX** between `$…$` or `$$…$$`.

The maths is translated to MathML and typeset by the browser. That is what makes it possible
without a dependency — no CDN tag on a loopback tool, and no 300KB of vendored KaTeX for text
that is usually three lines of prose.

**MDX** files render **with their components**, when the components sit beside them. Put
`Callout.jsx` next to `post.mdx`, import it as `./Callout`, and it compiles and runs —
hooks, state, event handlers and all. Chief ships a JSX transform and a small component
runtime (`jsx.js`, `mdx-runtime.js`) rather than React itself: what agent-written components
use is function components and five hooks, and that is a few hundred lines rather than a
vendored framework and a build step.

Two rules make it safe and bounded. The document's code runs in a **sandboxed frame at an
opaque origin** — it cannot reach the page reading the run, its API or its storage. And the
module graph is derived server-side from the file's own imports, confined to **its own
directory**: `./Chart` resolves, `../secret` and `/etc/passwd` are not resolved at all, and a
bare `react` is left to the runtime. The client still never names a path.

What it does not do: TypeScript (`.tsx`), npm packages, or components living elsewhere in a
repo behind a bundler alias. For an MDX page that belongs to a built site — one importing
`@/components/…` — report the URL your dev server serves it at instead; Chief frames that
page and you get the real thing. A document whose components cannot be found falls back to
prose with them named, rather than showing nothing.
Evaluating JSX out of an artifact would need a build step this project does not have and an
execution surface it does not want. So `<Callout>…</Callout>` becomes a framed block labelled
with its tag and props, with the prose inside it rendered as the markdown it is; a
self-closing `<Chart data={rows} />` becomes the same frame with nothing in it; `import` and
`export` lines fold away into one line you can open. You lose the styling and keep the
content, and you can see exactly what was there. **Frontmatter** is read as metadata rather
than as a horizontal rule followed by stray colons — in `.md` as well as `.mdx`.

The maths is a **subset**, and it says so when it meets something outside it: unrenderable maths is
shown as its own source on a marked background, so `$\begin{matrix}…$` reads as *not
rendered* rather than disappearing. Covered: fractions, roots, sub- and superscripts, sums and
integrals with limits, Greek, the usual relations and arrows, `\left…\right` fences and
manual sizing, `\text`/`\operatorname`, font commands, accents, `\underbrace`, and
environments — `matrix`, `cases`, `aligned` and friends — as tables. Not covered: macros, and
alignment within an environment.

The coverage is measured rather than asserted: every `$…$` and `$$…$$` in a corpus of 2230
real `.mdx` write-ups — 10,838 expressions — renders at **99.6%**, and the commands in
`scripts/test_markdown.mjs` were chosen by reading what came back as source, in frequency
order. Of the 39 that still do not, 38 are PromQL and shell snippets that were never maths.

Nothing is ever built as an HTML string — every node is created and its text set through
`textContent`. Artifact bodies come from outside, so that is a security property rather than a
style preference, and `scripts/test_markdown.mjs` asserts it directly.

## Sizing the panel

The panel on the right — run overview, step detail, artifacts, feedback — is dragged from
its **left edge**, and the width is remembered per browser. Double-click the grip to go back
to the default; with it focused, arrow keys move it (hold shift for bigger steps), because a
drag is not available to everyone.

It has a floor, so it cannot be collapsed to a sliver its cards cannot render in, and a
ceiling relative to the window, so a width saved on a large monitor does not swallow the
graph when the same browser opens on a laptop.

## Projects

Every workflow can carry a **project** — a short label like `chief` or `songs`. The agent
sets it when it plans, and the workflow list grows a row of chips to narrow by. Workflows
with no label are not hidden: they collect under **Unfiled**, which is where everything
planned before projects existed lives until you file it.

A project is deliberately **not a directory**. One product spans several checkouts and one
checkout carries work for more than one, so tying the two together would only ever be wrong
for somebody. What *is* recorded per workflow is `origin_dir` — where the agent was standing
when it planned — shown on the detail screen as *made in …*. It is a memory, not a live path:
nothing on the server resolves against it, and if the tree has moved it is simply wrong. Its
one job beyond the record is to offer itself as the **project folder** for opening artifacts,
in one click, which you can override.

File or refile a workflow from its detail screen, at any status — including ones that
finished long ago, which are exactly the ones worth filing.

Templates carry a project too, and a workflow made from one inherits it.

## Exporting a template to a project

A template detail screen has **Export to a file**. What comes out is exactly the body
`POST /templates` takes, id included, so you can commit it beside the code it describes and
register it again — on this machine or another — by posting it back. The file is written by
your browser, not by Chief: the server reads nothing off disk and writes nothing to it, which
is what keeps it a tracker rather than a file server.

## Templates

A workflow is single-use — approved once, executed once — so reuse lives in **templates**: a
plan with `{{ parameters }}` in it. Instantiating one produces a draft workflow, which still
needs approving. You can also turn a workflow you already ran into a template.

## Checked plans — catching a broken plan before it runs

A workflow says what the steps are and what order they go in. It does not say what each step
needs from the ones before it, so nothing notices when a plan asks a step to work from
something the previous step was never going to produce. You find out halfway through.

A **plan** closes that gap. It is written as a Lean file (see `lean/README.md`) where each step
declares what it demands of its inputs and what it promises about its output, and the server
checks that every promise actually satisfies the demand it feeds — for every possible value,
not a sampled one. A plan that holds up compiles into an ordinary draft workflow and is
approved and run like any other.

```
create_plan → verify_plan → (read the diagnostics, revise_plan, verify again) → compile_plan
```

What that buys you, precisely: a step cannot be written that reads an artifact without naming
the step producing it, so a missing dependency is impossible rather than merely discouraged; a
condition that excludes nothing cannot be written down at all, so a plan cannot be made to pass
by promising nothing; and anything downstream of a checkpoint depends on the approval it
returns, so no ordering of the plan skips the gate.

What it does not buy you, and does not pretend to: whether a step's work is any *good*. That
stays exactly where it was — criteria a person or a harness answers for. The checking is about
whether the plan hangs together, not whether it is a good idea.

Worth the extra round-trip when steps have real preconditions that would be expensive to
discover late. Not worth it for a short errand.

Lean is optional. `GET /v1/plans/toolchain` says whether this instance can check anything; if
it cannot, verification is refused outright rather than reported as a plan that failed, and
everything already compiled goes on running untouched.

## Approval policy

If approving every routine plan by hand gets tiring, the approval policy can auto-approve
matching workflows and forward amendments. It is edited in the UI at `/ui`, deliberately not
through any agent-facing tool: a session that could edit the policy governing its own approvals
could approve its own work. History edits can never be auto-approved, and that is checked when
the policy is written rather than when a decision is made.

---

## The web UI

`src/chief/web/` — five static files, no build step and no CDN, served by the same process
(REQ-21). It is a pure API client (REQ-18): a workflow list, a detail screen that draws the
plan as a dependency graph with per-instance state and artifacts, an approvals inbox covering
both pending amendments and blocked checkpoints, and the decision controls.

`/ui/?api=http://other-host:8080/v1` points it at a Chief running elsewhere.

---
