/* Chief web UI (REQ-16).

   There is no separate notion of a "run" here. A workflow has a lifecycle — awaiting
   approval, ready, running, completed — and its detail screen is the same screen throughout,
   drawing the same plan with execution state on it once there is any. The API keeps the
   definition and the run state as separate documents (REQ-38), which is right for a harness
   amending a plan without reading state, but it is not a distinction a person should have to
   hold. `lifecycleOf` collapses the two into one status, and `planGraph` draws either.

   Reuse belongs to templates, not to re-running a workflow, so a workflow is expected to have
   at most one execution; extra ones are surfaced rather than assumed away.

   Reuse is a template's job: a template is the plan you keep, a workflow the plan you are
   running this time. Both draw through planGraph, because a template *is* a plan — it just
   has placeholders where a workflow has values.

   The page and the server it talks to are versioned independently — reloading a page does
   not restart a Chief — so an extension endpoint answering 404 degrades that feature only.
   `state.templates` is `undefined` before it loads and `null` when the server has none.

   Screens: the workflows list, workflow detail, the templates list, template detail, the
   approvals inbox, and run detail — which survives only as the escape hatch for a workflow
   with more than one execution.

   Writes are the decisions only: approve or reject an amendment (REQ-13), approve or retire a
   workflow (REQ-32). Everything else is read through the REST API (REQ-18).

   Ported from the "Chief Runs v5" design. No build step and no CDN: the app is a handful of
   static files the same process serves (REQ-21). `scripts/smoke_ui.mjs` renders every screen
   headlessly.
*/

import {
  ApiError, approveWorkflow, archiveTemplate, archiveWorkflow, createTemplateFromWorkflow,
  decideAmendment, deleteWorkflow, getRunDefinition, getRunDetail, getWorkflowAudit, instantiateTemplate,
  addReviewNote, artifactContent, artifactModules, commentOnArtifact, decideReviewNote, labelWorkflow, listAmendments,
  listReviewNotes, listRuns, listTemplates, listWorkflows, resolveCheckpoint,
} from "./api.js";
import { markdown, inline } from "./markdown.js";

// ── colour and status vocabulary ─────────────────────────────────────────────────────────
// Colours stay as CSS custom properties rather than literals so the dark palette in
// chief.css applies without a second table here.

const OK = "var(--ok)";
const WARN = "var(--warn)";
const BAD = "var(--bad)";
const ACC = "var(--color-accent)";
const DIM = "var(--dim)";

/* `color` is for the small indicators — a dot in an instance cluster, a node's edge in the
   graph — where the hue carries the whole message. `tone` is the same meaning for the places
   that have room to say it in a word; see `badge`. Both live on one record so a status can
   never be green in the graph and amber in the list. */
const STEP_META = {
  pending: { color: "var(--color-neutral-400)", tone: "dim" },
  running: { color: ACC, pulse: true, tone: "acc" },
  completed: { color: OK, tone: "ok" },
  failed: { color: BAD, tone: "bad" },
  skipped: { color: "var(--color-neutral-300)", tone: "dim" },
  // A checkpoint that has been reached. Warm rather than the accent: this one is not the
  // machine working, it is the machine stopped, waiting for you.
  blocked: { color: WARN, pulse: true, tone: "warn" },
};

const RUN_META = {
  running: { label: "running", color: ACC, pulse: true, tone: "acc" },
  paused_for_approval: { label: "awaiting approval", color: ACC, pulse: true, tone: "acc" },
  waiting_on_human: { label: "waiting on you", color: WARN, pulse: true, tone: "warn" },
  completed: { label: "completed", color: OK, tone: "ok" },
  failed: { label: "failed", color: BAD, tone: "bad" },
};

/** One lifecycle, derived from two documents.

    A workflow and its execution are one thing to a person: the plan you approve is the plan
    you then watch run. The API keeps the definition and the run state apart (REQ-38) because
    a harness amending a plan should not have to read execution state — but that is an
    implementation fact, not something a reader of this UI should have to hold.

    Reuse is a template's job, not a workflow's, so a workflow is expected to have at most one
    execution. `register_run` does not enforce that, so extra executions are surfaced rather
    than assumed away — see `executionsOf`. */
const LIFECYCLE = {
  draft: { label: "awaiting approval", color: ACC, pulse: true, tone: "acc" },
  ready: { label: "ready to run", color: "var(--color-neutral-500)", tone: "dim" },
  archived: { label: "archived", color: "var(--color-neutral-400)", tone: "dim" },
};

function lifecycleOf(workflow, runs) {
  if (workflow.status === "draft") return { key: "draft", ...LIFECYCLE.draft };
  if (workflow.status === "archived" && runs.length === 0)
    return { key: "archived", ...LIFECYCLE.archived };
  // The newest execution is the workflow's current state; an archived workflow that ran
  // still reads by what happened when it ran.
  const latest = runs[0];
  if (!latest) return { key: "ready", ...LIFECYCLE.ready };
  return { key: latest.status, ...(RUN_META[latest.status] || RUN_META.running) };
}

/** A workflow's executions, newest first. Normally none or one. */
const executionsOf = (workflow, runs) =>
  (runs || [])
    .filter((r) => r.workflow_id === workflow.workflow_id)
    .sort((a, b) => (a.created_at < b.created_at ? 1 : -1));

const NODE_H = 62;
// A construct is not drawn as a container: its body steps join the main graph as ordinary
// nodes, and the construct itself shrinks to a small gate the cycle passes through — repeat
// or exit for a loop, a join for a parallel. This is the gate's height.
const GATE_H = 30;
const GAP = 32;
const DROP = 60;
const POLL_MS = 15000;

/** `{{ paper }}` in a body step's text, filled in from one instance's own metadata.

    Read-time only: the plan document keeps the placeholder, and nothing rendered here is
    ever stored. That is what stops a run holding a second, drifting copy of the plan — see
    CONTRACT-NOTES.md #40. A name the instance did not supply is left standing rather than
    blanked, so a gap reads as a gap instead of as a sentence with a hole in it. */
function fillParams(text, metadata) {
  if (!text || text.indexOf("{{") === -1) return text;
  const values = metadata || {};
  return text.replace(/\{\{\s*([a-z][a-z0-9_]*)\s*\}\}/g, (whole, name) =>
    Object.prototype.hasOwnProperty.call(values, name) ? String(values[name]) : whole,
  );
}

const hasParams = (text) => !!text && text.indexOf("{{") !== -1;

/** What distinguishes one instance, from the parameters its construct declared. Falls back
    to nothing rather than to a guess: an instance on a construct that declares no
    parameters has no distinguishing value, and inventing one from arbitrary metadata is how
    "Branch 3 · 41200" happens. */
function instanceParamLabel(step, instance) {
  const specs = step.instance_params || [];
  if (!specs.length) return "";
  const values = instance.metadata || {};
  return specs
    .filter((p) => Object.prototype.hasOwnProperty.call(values, p.name))
    .map((p) => String(values[p.name]))
    .join(" · ");
}

/** A step's criteria, and what the harness said about each.

    Rendered as a checklist rather than as prose, because that is the whole point of the
    field: a criterion nobody can enumerate is the criterion that gets skipped, which is how
    goals in this store grew to 900 characters with acceptance conditions buried in them.
    A criterion with evidence reads as met and shows what was claimed; one without reads as
    outstanding — and on a step already reported completed, that combination cannot occur,
    because the server refuses it. */
function criteriaBlock(criteria, met) {
  if (!criteria || !criteria.length) return null;
  const answers = met || {};
  return el(
    "div",
    { class: "criteria" },
    el("span", {
      class: "section-label",
      style: { marginTop: "var(--space-1)" },
      text: `Done when (${criteria.filter((c) => (answers[c.id] || "").trim()).length}/${criteria.length})`,
    }),
    criteria.map((c) => {
      const evidence = (answers[c.id] || "").trim();
      return el(
        "span",
        { class: "criterion" + (evidence ? " met" : "") },
        el("span", {
          class: "criterion-mark",
          text: evidence ? "✓" : "○",
          style: { color: evidence ? OK : "var(--color-neutral-400)" },
        }),
        el(
          "span",
          { class: "criterion-text" },
          el("span", { class: "criterion-what", text: c.text }),
          evidence &&
            el("span", { class: "criterion-evidence md-inline" }, inline(evidence)),
        ),
      );
    }),
  );
}

/** How far a completed step met its goal, when that is knowable. `goal_met` is an optional
    refinement a harness may report; otherwise a failed instance downgrades to partial. */
function outcomeOf(state) {
  if (state.status === "failed") return "none";
  if (state.status !== "completed") return null;
  const declared = (state.metadata || {}).goal_met;
  if (declared) return declared;
  if ((state.instances || []).some((i) => i.status === "failed")) return "partial";
  return "full";
}

const outcomeColor = (o) =>
  o === "full" ? OK : o === "partial" ? WARN : o === "none" ? BAD : null;

const stepMeta = (status) => STEP_META[status] || STEP_META.pending;

/** What one instance of a construct is called. */
const instanceKind = (step) => (step.type === "loop" ? "iteration" : "branch");

/** The steps a construct's `body` names, resolved against the definition.

    This is the *plan* half of a loop or parallel step, and it exists from the moment the
    workflow is written. Instances are the *execution* half and only appear once a run
    registers them. Drawing only the latter — which this UI used to do — left a loop on a
    draft indistinguishable from a task, so the plan a person was asked to approve hid the
    steps inside every construct in it (REQ-32 wants the whole plan visible at that moment). */
function bodyStepsOf(step, def) {
  if (!def || !step || step.ghost) return [];
  const byId = new Map(def.steps.map((s) => [s.id, s]));
  return (step.body || []).map((id) => byId.get(id)).filter(Boolean);
}

/** Inline every construct's body into the flat graph.

    A loop is a cycle, and the picture should say so: the body steps become ordinary nodes
    in the main layout, and the construct itself becomes a small *gate* node the flow passes
    through — its incoming edges are the body's exits, its outgoing edge continues the plan,
    and for a loop a dashed return edge runs back to the body's entry. The gate keeps the
    construct's id, so selecting it inspects the real step, and in a run it is where the
    instance count lives — the "counter" is the instances Chief already records, not a new
    node type, because Chief records what the harness did rather than enforcing a bound.

    Recursive, since a body step may itself be a construct. Display-only: the returned steps
    are copies with synthesised `depends_on`; the definition is never touched. */
function flattenConstructs(steps, ctx) {
  const out = [];
  const expand = (step, inheritedDeps) => {
    const body = ctx.plannedBody(step);
    if (!body.length) {
      out.push({ ...step, depends_on: inheritedDeps ?? step.depends_on });
      return;
    }
    const deps = inheritedDeps ?? step.depends_on ?? [];
    const inBody = new Set(body.map((s) => s.id));
    const entries = body.filter((s) => !(s.depends_on || []).some((d) => inBody.has(d)));
    const exits = body.filter((s) => !body.some((o) => (o.depends_on || []).includes(s.id)));
    for (const child of body) {
      const inner = (child.depends_on || []).filter((d) => inBody.has(d));
      expand(child, entries.includes(child) ? [...deps, ...inner] : inner);
    }
    out.push({
      ...step, gate: true, entryIds: entries.map((s) => s.id),
      depends_on: exits.map((s) => s.id),
    });
  };
  for (const step of steps) expand(step, null);
  return out;
}

/** The gate: a construct rendered as the point the flow passes through, not a box around
    the work. Click inspects the construct itself — goal, body list, instances. */
function gateNode(step, p, w, ctx) {
  const isSelected = ctx.selected === `step:${step.id}`;
  const st = ctx.stateOf(step);
  const sm = stepMeta(st.status);
  const instances = st.instances || [];
  return el(
    "div",
    {
      class: "node gate" + (isSelected ? " sel" : st.status === "failed" ? " fail" : ""),
      style: { left: `${p.x}px`, top: `${p.y}px`, width: `${w}px`, height: `${p.h}px` },
      title: step.goal,
      onClick: () => setState({ selected: isSelected ? "none" : `step:${step.id}` }),
    },
    instances.length > 0 && dot(outcomeColor(outcomeOf(st)) || sm.color, pulseOf(sm), "6px"),
    el("span", {
      class: "gate-label mono",
      text:
        step.type === "loop"
          ? `\u21ba ${step.id} \u00b7 repeat or exit`
          : `\u21c9 ${step.id} \u00b7 join`,
    }),
    instances.length > 0 &&
      el(
        "span",
        { class: "node-cluster" },
        instances.slice(0, 8).map((i) => {
          const im = stepMeta(i.status);
          return dot(im.color, pulseOf(im), "5px");
        }),
        el("span", {
          class: "label",
          text:
            `${instances.length} ${step.type === "loop" ? "iterations" : "branches"}` +
            (st.instances_closed === false ? "\u2026" : ""),
        }),
      ),
  );
}

const pulseOf = (meta) => (meta.pulse ? "chiefpulse 1.6s infinite" : "none");

function rel(iso) {
  if (!iso) return "";
  const minutes = Math.max(0, Math.round((Date.now() - new Date(iso)) / 60000));
  if (minutes < 1) return "now";
  if (minutes < 60) return `${minutes}m`;
  if (minutes < 1440) return `${Math.round(minutes / 60)}h`;
  return `${Math.round(minutes / 1440)}d`;
}

/** A wall-clock stamp: "14 Aug 09:12", with the year when it is not this one.

    Relative time answers "is this current?", which is the right question for a run that is
    moving. When a workflow was added is a fact you sort and compare by, and "31d" cannot be
    compared with anything. */
function stamp(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const day = d.toLocaleDateString(undefined, { day: "numeric", month: "short" });
  const time = d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
  const year = d.getFullYear() === new Date().getFullYear() ? "" : ` ${d.getFullYear()}`;
  return `${day}${year} ${time}`;
}

/** How long the work took, or has been taking: "12m", "1h 40m", "2d 3h".

    Measured on the execution, not on the record — the plan may have sat as a draft for a
    week before anyone ran it, and that wait is not the work. A run still going is measured
    to now, so the cell ticks along with the poll. */
function durationOf(run) {
  if (!run) return "";
  const end = ["completed", "failed"].includes(run.status) ? new Date(run.updated_at) : new Date();
  return Math.max(0, end - new Date(run.created_at));
}

function fmtDuration(ms) {
  if (ms === "" || ms == null) return "";
  const minutes = Math.round(ms / 60000);
  if (minutes < 1) return "<1m";
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return minutes % 60 ? `${hours}h ${minutes % 60}m` : `${hours}h`;
  return Math.round(hours / 24) >= 10 ? `${Math.round(hours / 24)}d` : `${Math.floor(hours / 24)}d ${hours % 24}h`;
}

/** "6m ago", but "just now" rather than the nonsense "now ago". */
function relAgo(iso) {
  const span = rel(iso);
  return span === "now" ? "just now" : span ? `${span} ago` : "";
}

// ── DOM helpers ──────────────────────────────────────────────────────────────────────────

function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  applyProps(node, props);
  append(node, children);
  return node;
}

function svgEl(tag, props = {}, ...children) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
  applyProps(node, props);
  append(node, children);
  return node;
}

function applyProps(node, props) {
  for (const [key, value] of Object.entries(props)) {
    if (value == null || value === false) continue;
    if (key === "class") node.setAttribute("class", value);
    else if (key === "text") node.textContent = value;
    else if (key === "style") Object.assign(node.style, value);
    // Any `onFoo` prop is a listener — onClick, onInput, onKeyDown. Checked before the
    // attribute fallback, or a handler would be stringified into the DOM.
    else if (key.startsWith("on") && typeof value === "function")
      node.addEventListener(key.slice(2).toLowerCase(), value);
    else node.setAttribute(key, value);
  }
}

function append(node, children) {
  for (const child of children.flat(Infinity)) {
    // `cond && node` is the conditional-child idiom, so anything that is not a node or a
    // string is a skipped branch — including the 0 a `list.length &&` guard leaves behind.
    if (typeof child === "string") node.appendChild(document.createTextNode(child));
    else if (child instanceof Node) node.appendChild(child);
  }
}

const dot = (color, pulse, size) =>
  el("span", {
    class: pulse === "none" || !pulse ? "dot" : "dot pulse",
    style: { background: color, ...(size ? { width: size, height: size } : {}) },
  });

/** A status as a word, tinted by its tone — for the places a status has a column to itself.

    A dot asks the reader to hold a legend in their head, and the distinctions here are not
    ones two shades of a hue can carry: "awaiting approval" and "waiting on you" are both
    stopped-and-waiting, and which one it is decides who has to do something. */
const badge = (meta) =>
  el("span", {
    class: `badge b-${meta.tone || "dim"}${meta.pulse ? " pulse" : ""}`,
    text: meta.label,
  });

// ── local files ──────────────────────────────────────────────────────────────────────────

/** An artifact is a reference, not a blob (REQ-46), and a harness reporting one names the
    file the way it saw it — `songs/personas.md`, relative to wherever it was working. Chief
    never recorded that directory and should not start: the plan is not a checkout, and a
    field for it would be one more thing on the harness-facing surface that is wrong the
    moment the tree moves.

    So the base lives in the browser, next to the person who knows it. It is per-machine
    state about *this* reader — the same run opened on another machine resolves against that
    machine's copy — which is exactly what localStorage is for, and it means an old run's
    paths come alive the moment a folder is named, rather than only runs recorded after some
    schema change. */
const ROOT_KEY = "chief.filesRoot";

/** The key a project's folder is stored under.

    Keyed by project, because one folder for everything is wrong the moment Chief is used on
    a second checkout: the artifacts of one project would resolve against another's tree and
    open the wrong file, or none. The bare key stays the home of the unlabelled ones, so a
    folder set before projects existed keeps working. */
const rootKeyFor = (project) => (project ? `${ROOT_KEY}:${project}` : ROOT_KEY);

/** Which project the screen is looking at, for the purposes of resolving a path. */
function currentProject() {
  const wf = (state.workflows || []).find((w) => w.workflow_id === state.workflowId);
  if (wf) return wf.project || null;
  const run = (state.runs || []).find((r) => r.run_id === state.runId);
  const owner = run && (state.workflows || []).find((w) => w.workflow_id === run.workflow_id);
  return (owner && owner.project) || null;
}

/** Where this plan was made, if it says. Only ever offered as a suggestion — see
    CONTRACT-NOTES.md #32: a recorded path is a fact about the past, not a promise about
    where the tree is now, so the browser's own setting stays the one that decides. */
function originDirOf() {
  const wf = (state.workflows || []).find((w) => w.workflow_id === state.workflowId);
  if (wf) return wf.origin_dir || null;
  const run = (state.runs || []).find((r) => r.run_id === state.runId);
  const owner = run && (state.workflows || []).find((w) => w.workflow_id === run.workflow_id);
  return (owner && owner.origin_dir) || null;
}

/** One editor, named once. Not a preference and not a picker: on the machine Chief runs on
    there is a default editor for source, and offering a dropdown would be asking the reader
    to configure something they will answer the same way every time. `cursor://file/` is the
    same shape if that is the one you want; JetBrains is not (`idea://open?file=`). */
const EDITOR_SCHEME = "vscode://file";

// localStorage throws outright in a few configurations (and simply is not there under the
// smoke harness's stub DOM), and none of this is worth a broken render.
const readRoot = (project = null) => {
  try {
    const own = localStorage.getItem(rootKeyFor(project));
    // An empty string is a decision — "this project has no folder" — and is distinct from
    // the key being absent, which is "nobody has said". Only the absent case falls back to
    // the unkeyed value, so a folder set before projects existed keeps resolving while
    // clearing one still clears it.
    if (own !== null) return own;
    return localStorage.getItem(ROOT_KEY) || "";
  } catch {
    return "";
  }
};

const writeRoot = (value, project = null) => {
  try {
    localStorage.setItem(rootKeyFor(project), value || "");
  } catch {
    /* the path still resolves for this session; it just will not survive a reload */
  }
};

// ── the file viewer ──────────────────────────────────────────────────────────────────────
//
// A drawer along the bottom that shows the file an artifact names. The bytes come from the
// server, because the server is the machine that has them — which is the case a browser-side
// file picker cannot serve at all when the UI is reached through a tunnel.
//
// What arrives is opaque bytes plus a separate header saying what they may be shown as. The
// type is applied here, to a blob this page makes, so nothing an artifact contains is ever
// live at Chief's own origin. See CONTRACT-NOTES.md #34.

const VIEWER_WIDTH_KEY = "chief.viewerWidth";
const VIEWER_WIDTH_DEFAULT = 520;
const VIEWER_WIDTH_MIN = 280;

function clampViewerWidth(width) {
  const room = (typeof window !== "undefined" && window.innerWidth) || 1400;
  const max = Math.max(VIEWER_WIDTH_MIN, Math.min(1100, Math.round(room * 0.7)));
  return Math.max(VIEWER_WIDTH_MIN, Math.min(max, Math.round(width)));
}

const readViewerWidth = () => {
  try {
    return clampViewerWidth(Number(localStorage.getItem(VIEWER_WIDTH_KEY)) || VIEWER_WIDTH_DEFAULT);
  } catch {
    return VIEWER_WIDTH_DEFAULT;
  }
};

/** Keep the page out from under the drawer.

    An overlay would cover the artifact list you opened the file from, which is exactly where
    you go next to open another. The page is inset by the drawer's width instead, so both stay
    reachable — set as a custom property so a drag can move it without a re-render. */
function setViewerInset(width) {
  const root = document.documentElement;
  // Guarded on the method, not just the object: a stub DOM has a `style` that is a plain
  // object, and the difference only shows up as a crash on the first file opened.
  if (!root || !root.style || typeof root.style.setProperty !== "function") return;
  root.style.setProperty("--viewer-right", width ? `${width}px` : "0px");
}

/** Blob URLs are held until replaced: revoking one while an <img> or <embed> still points at
    it blanks the frame, and a render can happen at any time. One at a time, so the pages
    a session opens do not accumulate. */
let viewerObjectUrl = null;

/** The live drawer element, so a drag can move it without a re-render — the same
    arrangement the inspector uses, and one fewer DOM lookup per pointermove. */
let viewerNode = null;

function releaseViewerUrl() {
  if (viewerObjectUrl) URL.revokeObjectURL(viewerObjectUrl);
  viewerObjectUrl = null;
}

/** Open the drawer on an artifact and fetch it. */
async function openViewer(artifact, label) {
  const runId = state.detail && state.detail.runId;
  if (!runId || !artifact.artifact_id) return;
  const web = /^https?:/i.test(artifact.ref || "");
  setState({
    viewerPending: null,
    viewer: {
      runId, artifactId: artifact.artifact_id, ref: artifact.ref,
      title: label || artifact.description || artifact.ref,
      // A URL is not Chief's to fetch and never was — it is framed, not read. The page on
      // the other end renders itself, with whatever it is built from.
      url: web ? artifact.ref : null,
      loading: !web, error: null, file: null,
    },
  });
  if (web) return;
  // An MDX document needs its neighbours and a runtime as well as its own bytes.
  try {
    const file = await artifactContent(runId, artifact.artifact_id);
    let extra = {};
    if (file.mediaType === "text/mdx") {
      try {
        const [{ modules }, runtime] = await Promise.all([
          artifactModules(runId, artifact.artifact_id),
          loadRuntime(),
        ]);
        extra = { modules, runtime, entry: file.name };
      } catch {
        // The prose still renders without them, which is better than an error where a
        // document should be. A component with nothing to run it shows as its named frame.
        extra = {};
      }
    }
    // A slow read that lands after the reader moved on must not reopen the drawer or
    // overwrite what they are looking at now.
    if (!state.viewer || state.viewer.artifactId !== artifact.artifact_id) return;
    setState({ viewer: { ...state.viewer, loading: false, file, ...extra } });
  } catch (err) {
    if (!state.viewer || state.viewer.artifactId !== artifact.artifact_id) return;
    setState({
      viewer: {
        ...state.viewer, loading: false,
        error: err instanceof ApiError ? err.message : String(err),
      },
    });
  }
}

/** Open the drawer on a value rather than a file.

    The inline pairs on a card are a glance — enough to tell one branch from another, and
    deliberately not the whole of it. Anything longer than a glance belongs where files
    already go, in the drawer, through the same folding tree: one place to read something,
    at a width you can drag. */
function openJsonViewer(title, value) {
  releaseViewerUrl();
  viewerNode = null;
  setState({
    viewerPending: null,
    viewer: { title, ref: null, json: value, loading: false, error: null, file: null },
  });
}

function closeViewer() {
  releaseViewerUrl();
  viewerNode = null;
  setViewerInset(0);
  setState({ viewer: null, viewerPending: null });
}

/** Open the artifact the URL named, once there is a run loaded to find it in.

    A reload lands with an id and nothing to resolve it against, so this runs after each
    fetch. An id that is not in the run is dropped rather than retried: the artifact may have
    been on a link someone kept from a different run, and a viewer that kept trying would
    re-open on every poll forever. */
function restorePendingViewer() {
  const wanted = state.viewerPending;
  if (!wanted || state.viewer || !state.detail) return;
  const found = runArtifacts(state.detail.def, state.detail.state.step_states || {})
    .find(({ artifact }) => artifact.artifact_id === wanted);
  if (!found) {
    setState({ viewerPending: null });
    return;
  }
  openViewer(found.artifact, found.label);
}

/** JSON as a tree you can fold, rather than as text you have to scroll.

    Native `<details>`, so folding needs no state of its own and the keyboard works for free.
    That only holds because the rendered body is built once and kept — see `viewerBody` — as
    a re-render would otherwise rebuild every node and spring the whole file open again on
    the next poll.

    Two levels open to start: enough to see the shape of a metrics file, not so much that a
    twelve-element array of objects fills the panel before you have read the top of it. */
const JSON_OPEN_DEPTH = 2;

function jsonValue(value, depth) {
  if (value === null) return el("span", { class: "j-null", text: "null" });
  if (typeof value === "string") return el("span", { class: "j-str", text: JSON.stringify(value) });
  if (typeof value === "number") return el("span", { class: "j-num", text: String(value) });
  if (typeof value === "boolean") return el("span", { class: "j-bool", text: String(value) });

  const array = Array.isArray(value);
  const entries = array ? value.map((v, i) => [String(i), v]) : Object.entries(value);
  if (entries.length === 0) {
    return el("span", { class: "j-empty", text: array ? "[]" : "{}" });
  }

  const node = el(
    "details",
    { class: "j-node", ...(depth < JSON_OPEN_DEPTH ? { open: "" } : {}) },
    el("summary", {
      class: "j-summary",
      text: array
        ? `[ ${entries.length} item${entries.length === 1 ? "" : "s"} ]`
        : `{ ${entries.length} key${entries.length === 1 ? "" : "s"} }`,
    }),
    el(
      "div",
      { class: "j-children" },
      entries.map(([key, child]) =>
        el(
          "div",
          { class: "j-row" },
          el("span", { class: array ? "j-index" : "j-key", text: array ? key : `${key}:` }),
          jsonValue(child, depth + 1),
        ),
      ),
    ),
  );
  return node;
}

const isText = (type) =>
  type.startsWith("text/") || type === "application/json" || type === "text/csv";

/** The file itself, rendered by what Chief said it may be shown as. */
/** The frame has no stylesheet of its own, so it carries just enough to be readable. It is
    deliberately plain: a component styled by Chief would look like it had styled itself. */
const MDX_FRAME_CSS = `
  :root { color-scheme: light dark }
  body { margin: 0; padding: 14px 18px; font: 13px/1.55 ui-sans-serif, system-ui, sans-serif }
  h1,h2,h3,h4,h5,h6 { font-size: 1.05em; margin: 1em 0 .4em }
  p { margin: 0 0 .7em }
  pre { overflow-x: auto; padding: 8px; border-radius: 4px; background: rgba(128,128,128,.12) }
  code { font-family: ui-monospace, Menlo, monospace; font-size: .92em }
  table { border-collapse: collapse } td, th { border: 1px solid rgba(128,128,128,.35); padding: 3px 7px }
  img { max-width: 100% }
  .mdx-error { white-space: pre-wrap; color: #b4443a; background: rgba(180,68,58,.1); padding: 10px; border-radius: 4px }
`;

/** Mermaid, loaded once and only if a document turns out to need it — vendored rather than
    fetched from a CDN (REQ-21 applies to the browser side too), and lazy because it is 3+MB
    and most artifacts never use it. A classic script, not a module: the upstream build sets
    `window.mermaid` itself rather than exporting anything import() could take. */
let mermaidReady = null;
function loadMermaid() {
  if (mermaidReady) return mermaidReady;
  mermaidReady = new Promise((resolve, reject) => {
    if (window.mermaid) return resolve(window.mermaid);
    const script = document.createElement("script");
    script.src = new URL("vendor/mermaid.min.js", import.meta.url);
    script.onload = () => {
      window.mermaid.initialize({
        startOnLoad: false,
        securityLevel: "strict",
        // No JS side theme toggle exists yet to react to a change mid-session — matching
        // the CSS-only dark mode this page otherwise has, evaluated once, is the same
        // freshness every other themed thing here already settles for.
        theme: window.matchMedia?.("(prefers-color-scheme: dark)").matches ? "dark" : "default",
      });
      resolve(window.mermaid);
    };
    script.onerror = () => reject(new Error("mermaid failed to load"));
    document.head.appendChild(script);
  });
  return mermaidReady;
}

/** Draw every `<pre class="mermaid">` block markdown() left as source text inside `root`,
    once. A no-op — and no fetch of the runtime — when there is nothing to draw, which is
    the common case. Errors are left as Mermaid's own inline message rather than caught:
    that message names the line the diagram broke on, which a caught-and-hidden failure
    would throw away. */
async function renderMermaid(root) {
  const nodes = root.querySelectorAll(".mermaid");
  if (!nodes.length) return;
  const mermaid = await loadMermaid();
  await mermaid.run({ nodes });
}

/** The compiled sources this page holds, so the frame can be built without a second fetch. */
let runtimeSources = null;

/** Chief's own renderer and runtime, as text, for inlining into the frame.

    Fetched rather than imported: the frame is at an opaque origin and a module fetch from
    there would need CORS Chief does not serve, so the sources are inlined into `srcdoc`
    instead. `export` is stripped because the frame runs them as classic scripts — the one
    place in this codebase where a regex touches JavaScript, and it holds because these two
    files only ever export at the top level. */
async function loadRuntime() {
  if (runtimeSources) return runtimeSources;
  const [markdownSrc, jsxSrc, runtimeSrc] = await Promise.all(
    ["markdown.js", "jsx.js", "mdx-runtime.js"].map((name) =>
      fetch(new URL(name, import.meta.url)).then((r) => r.text()),
    ),
  );
  runtimeSources = {
    markdown: markdownSrc.replace(/^export /gm, ""),
    jsx: jsxSrc,
    runtime: runtimeSrc,
  };
  return runtimeSources;
}

/** The frame that renders an MDX document with its components.

    `srcdoc` with `allow-scripts` and *not* `allow-same-origin`: the document runs at an
    opaque origin, so the components a harness wrote cannot reach this page, its API, or its
    storage. Everything they need is inlined; the frame fetches nothing. */
function mdxFrame(viewer) {
  const frame = el("iframe", {
    class: "viewer-frame", title: viewer.title,
    sandbox: "allow-scripts allow-popups",
    referrerpolicy: "no-referrer",
  });
  const { markdown: md, jsx, runtime } = viewer.runtime;
  const payload = JSON.stringify({ entry: viewer.entry, modules: viewer.modules });
  frame.setAttribute(
    "srcdoc",
    `<!doctype html><meta charset="utf-8">` +
      `<style>${MDX_FRAME_CSS}</style>` +
      `<body><div id="root"></div>` +
      `<script>${md}<\/script><script>${jsx}<\/script><script>${runtime}<\/script>` +
      `<script>(function(){` +
      `  var data = ${payload.replace(/</g, "\\u003c")};` +
      `  try {` +
      `    ChiefMDX.renderMdx({ entry: data.entry, modules: data.modules,` +
      `      host: document.getElementById("root"), markdown: markdown });` +
      `  } catch (err) {` +
      `    var p = document.createElement("pre");` +
      `    p.className = "mdx-error";` +
      `    p.textContent = "This document did not compile:\\n\\n" + (err && err.message || err);` +
      `    document.getElementById("root").appendChild(p);` +
      `  }` +
      `})();<\/script>`,
  );
  return frame;
}

/** Whether a framed page may keep its own origin.

    `allow-same-origin` does not make the frame same-origin with *this* page — the browser
    still separates two different origins. It only stops the frame being forced into an
    opaque one, which a dev server needs: without it the page loses its own storage and
    every fetch it makes to itself becomes a cross-origin failure.

    The exception is a URL on Chief's own origin. There `allow-scripts` and
    `allow-same-origin` together let the frame reach out of the sandbox and into this page,
    so it is refused the second flag — and it would be Chief framing itself, which is not
    what any of this is for. */
function frameSandbox(url) {
  const base = "allow-scripts allow-forms allow-popups allow-modals";
  try {
    if (new URL(url, location.href).origin === location.origin) return base;
  } catch {
    return base;
  }
  return `${base} allow-same-origin`;
}

function viewerBody(viewer) {
  const { file } = viewer;
  // A value rather than a file: metadata opened from a card, through the same tree a JSON
  // artifact gets. Nothing to fetch, so it is checked before the loading state.
  if (viewer.json !== undefined && viewer.json !== null) {
    if (viewer.node) return viewer.node;
    viewer.node = el("div", { class: "viewer-json" }, jsonValue(viewer.json, 2));
    return viewer.node;
  }
  // A page renders itself. This is the only way to see MDX with the components it actually
  // imports — they live in a project Chief has never seen, and the thing that has them is
  // the dev server already serving them. See CONTRACT-NOTES.md #36.
  if (viewer.url) {
    return el("iframe", {
      class: "viewer-frame", src: viewer.url, title: viewer.title,
      sandbox: frameSandbox(viewer.url),
      referrerpolicy: "no-referrer",
    });
  }
  if (viewer.loading) return el("p", { class: "text-muted", text: "Reading…" });
  if (viewer.error) return el("div", { class: "banner", text: viewer.error });
  if (!file) return null;
  // Built once and kept. Every fifteen seconds the poll re-renders the page, and rebuilding
  // this would decode the bytes again and — worse — spring every folded branch of a JSON
  // tree back open under the reader.
  if (viewer.node) return viewer.node;

  const bytes = new Uint8Array(file.bytes);
  const keep = (node) => {
    viewer.node = node;
    return node;
  };

  if (isText(file.mediaType)) {
    const text = new TextDecoder().decode(bytes);
    // The entry has to be in the graph, not merely a graph having been fetched: an empty
    // answer means there was nothing to resolve, and compiling nothing renders nothing.
    if (file.mediaType === "text/mdx" && viewer.modules && viewer.modules[viewer.entry]) {
      // Components to run, so run them — in a frame at an opaque origin, which is the only
      // place a document's own code may execute. See CONTRACT-NOTES.md #37.
      return keep(mdxFrame(viewer));
    }
    if (file.mediaType === "text/markdown" || file.mediaType === "text/mdx") {
      // MDX gets the same renderer with its components named rather than run: evaluating
      // JSX out of an artifact is a build step this has not got and an execution surface it
      // does not want. See CONTRACT-NOTES.md #35.
      const mdx = file.mediaType === "text/mdx";
      const doc = el("div", { class: "viewer-doc md-block" }, markdown(text, { mdx }));
      renderMermaid(doc);
      return keep(doc);
    }
    if (file.mediaType === "application/json") {
      try {
        return keep(el("div", { class: "viewer-json" }, jsonValue(JSON.parse(text), 0)));
      } catch {
        // Malformed JSON is still worth reading, and the text is the only honest way to
        // show what is wrong with it.
        return keep(el("pre", { class: "viewer-pre" }, el("code", { text })));
      }
    }
    // Code and logs, where the shape of the whitespace is the information and reflowing it
    // would destroy it.
    return keep(el("pre", { class: "viewer-pre" }, el("code", { text })));
  }

  releaseViewerUrl();
  viewerObjectUrl = URL.createObjectURL(new Blob([bytes], { type: file.mediaType }));
  if (file.mediaType.startsWith("image/")) {
    return keep(el("div", { class: "viewer-media" }, el("img", { src: viewerObjectUrl, alt: file.name })));
  }
  if (file.mediaType === "application/pdf") {
    // The browser's own viewer, on a blob this page owns.
    return keep(el("iframe", { class: "viewer-frame", src: viewerObjectUrl, title: file.name }));
  }
  return keep(el(
    "div",
    { class: "viewer-binary" },
    el("p", { text: `${file.name} — ${bytes.length.toLocaleString()} bytes, not a previewable type` }),
    el("a", { class: "btn btn-secondary btn-sm", href: viewerObjectUrl, download: file.name, text: "Download" }),
  ));
}

/** The drawer. Absent entirely when nothing is open, so it costs a closed reader nothing. */
/** The drawer, down the right of the window.

    Beside the plan rather than under it: a file is read *against* the run that produced it,
    and a panel along the bottom pushes the graph off the screen to do it. The one thing the
    bottom would have been better for — a wide log — is served instead by the drawer being
    resizable to most of the window. */
function fileViewer() {
  const viewer = state.viewer;
  if (!viewer) return null;
  const file = viewer.file;
  const width = state.viewerWidth;
  setViewerInset(width);

  const aside = el(
    "aside",
    {
      id: "chief-viewer", class: "viewer", "data-screen-label": "File viewer",
      style: { width: `${width}px` },
    },
    el("div", {
      class: "viewer-grip", role: "separator", "aria-orientation": "vertical",
      "aria-label": "Resize the file viewer", tabindex: "0",
      title: "Drag to resize",
      onPointerdown: startViewerResize,
      onKeyDown: (e) => {
        const step = e.shiftKey ? 64 : 16;
        // Leftwards is wider: the grip is the drawer's left edge, and the drawer grows into
        // the space the pointer opens.
        const delta = e.key === "ArrowLeft" ? step : e.key === "ArrowRight" ? -step : 0;
        if (!delta) return;
        if (e.preventDefault) e.preventDefault();
        commitViewerWidth(width + delta);
      },
    }),
    el(
      "div",
      { class: "viewer-panel" },
      el(
      "div",
      { class: "viewer-head" },
      el("span", { class: "viewer-title", text: viewer.title }),
      el("span", { class: "mono viewer-ref", text: viewer.ref || "" }),
      file &&
        el("span", {
          class: "art-meta",
          text: `${file.mediaType} · ${new Uint8Array(file.bytes).length.toLocaleString()} bytes`,
        }),
      el("button", { class: "close-x", text: "✕", title: "Close", onClick: closeViewer }),
      ),
      el("div", { class: "viewer-body" }, viewerBody(viewer)),
    ),
  );
  viewerNode = aside;
  return aside;
}

function commitViewerWidth(next) {
  const width = clampViewerWidth(next);
  try {
    localStorage.setItem(VIEWER_WIDTH_KEY, String(width));
  } catch {
    /* this session only */
  }
  setState({ viewerWidth: width });
}

function startViewerResize(event) {
  if (event.preventDefault) event.preventDefault();
  const startX = event.clientX;
  const from = state.viewerWidth;
  let width = from;
  const onMove = (moveEvent) => {
    width = clampViewerWidth(from + (startX - moveEvent.clientX));
    if (viewerNode) viewerNode.style.width = `${width}px`;
    // The page's inset follows the drag, or the content jumps at the end of it.
    setViewerInset(width);
  };
  const onUp = () => {
    window.removeEventListener("pointermove", onMove);
    window.removeEventListener("pointerup", onUp);
    commitViewerWidth(width);
  };
  window.addEventListener("pointermove", onMove);
  window.addEventListener("pointerup", onUp);
}

// ── the inspector's width ────────────────────────────────────────────────────────────────
//
// Dragged from its left edge, and remembered. A fixed 360px is right for a step's goal and
// wrong for a markdown artifact or a long note thread, and which of those you are reading
// is not something the app can know.

const INSPECTOR_KEY = "chief.inspectorWidth";
const INSPECTOR_DEFAULT = 360;
const INSPECTOR_MIN = 280;

/** Never wider than most of the window, and never so narrow the cards inside it collapse.
    Recomputed on every drag rather than stored, so a width saved on a wide monitor does not
    swallow the graph when the same browser opens on a laptop. */
function clampInspector(width) {
  const room = (typeof window !== "undefined" && window.innerWidth) || 1200;
  const max = Math.max(INSPECTOR_MIN, Math.min(720, Math.round(room * 0.6)));
  return Math.max(INSPECTOR_MIN, Math.min(max, Math.round(width)));
}

const readInspectorWidth = () => {
  try {
    return clampInspector(Number(localStorage.getItem(INSPECTOR_KEY)) || INSPECTOR_DEFAULT);
  } catch {
    return INSPECTOR_DEFAULT;
  }
};

const writeInspectorWidth = (width) => {
  try {
    localStorage.setItem(INSPECTOR_KEY, String(width));
  } catch {
    /* it still applies for this session; it just will not survive a reload */
  }
};

/** The live inspector element, so a drag can move it without a re-render.

    Same reason `copyPath` writes its acknowledgement straight onto the node: `setState`
    rebuilds the whole tree, and doing that per pointermove would drop frames and tear down
    the field someone is typing into. The state is updated once, when the drag ends. */
let inspectorNode = null;

function startInspectorResize(event) {
  // Stops the browser deciding a drag across a panel is a text selection.
  if (event.preventDefault) event.preventDefault();
  const startX = event.clientX;
  const startWidth = state.inspectorWidth;
  let width = startWidth;

  const onMove = (moveEvent) => {
    // Leftwards is wider: the handle is on the panel's left edge, so the panel grows into
    // the space the pointer is moving through.
    width = clampInspector(startWidth + (startX - moveEvent.clientX));
    if (inspectorNode) inspectorNode.style.width = `${width}px`;
  };
  const onUp = () => {
    window.removeEventListener("pointermove", onMove);
    window.removeEventListener("pointerup", onUp);
    writeInspectorWidth(width);
    // One render at the end. `render` re-measures the graph, so the plan relays out into
    // whatever room the panel left it rather than staying at its old width.
    setState({ inspectorWidth: width });
  };
  window.addEventListener("pointermove", onMove);
  window.addEventListener("pointerup", onUp);
}

/** The grip between the plan and the panel. */
function inspectorHandle() {
  return el("div", {
    class: "split-handle",
    // Announced as what it is, and usable without a pointer: a drag is not available to
    // everyone, and the whole control would otherwise be mouse-only.
    role: "separator",
    "aria-orientation": "vertical",
    "aria-label": "Resize the panel",
    tabindex: "0",
    title: "Drag to resize · double-click to reset",
    onPointerdown: startInspectorResize,
    onDblclick: () => {
      writeInspectorWidth(INSPECTOR_DEFAULT);
      setState({ inspectorWidth: INSPECTOR_DEFAULT });
    },
    onKeyDown: (e) => {
      const step = e.shiftKey ? 64 : 16;
      const delta = e.key === "ArrowLeft" ? step : e.key === "ArrowRight" ? -step : 0;
      if (!delta) return;
      if (e.preventDefault) e.preventDefault();
      const width = clampInspector(state.inspectorWidth + delta);
      writeInspectorWidth(width);
      setState({ inspectorWidth: width });
    },
  });
}

/** The plan and the panel, with the grip between them. One helper because all three screens
    that draw a graph compose exactly this, and a handle missing from one of them is the
    kind of difference nobody notices until they are on that screen. */
const splitView = (viewport, panel) =>
  el("div", { class: "graph-split" }, viewport, inspectorHandle(), inspector(panel));

/** Anything with a scheme is somewhere else — http, but also mailto: or a git+ssh remote. */
const isUrlRef = (ref) => /^[a-z][a-z0-9+.-]*:/i.test(ref || "");
const isFileRef = (ref) => !!ref && !isUrlRef(ref);

/** The absolute path a ref names, or null when it is relative and no folder has been set.
    A guess would be worse than nothing here: a `vscode://` link built on the wrong base
    opens a "file not found" in the editor, which reads as the editor failing. */
function absolutePath(ref) {
  if (!isFileRef(ref)) return null;
  if (ref.startsWith("/")) return ref;
  // `~` is the shell's, not the browser's: there is no way to learn $HOME from here, and
  // joining it onto the project folder would build a path with a literal tilde in it. Left
  // unlinked and copyable, which is the same answer as a relative ref with no folder set.
  if (ref.startsWith("~")) return null;
  const root = (readRoot(currentProject()) || "").replace(/\/+$/, "");
  return root ? `${root}/${ref}` : null;
}

// encodeURI, not encodeURIComponent — the separators are the point.
const editorHref = (absolute) => `${EDITOR_SCHEME}${encodeURI(absolute)}`;

/** Put `text` on the clipboard and say so on the button that was pressed.
    The acknowledgement is written straight onto the node rather than through setState: it is
    a fact about one button for one second, and routing it through a full re-render would
    tear down every field being typed into elsewhere on the page. */
function copyPath(button, text) {
  const done = () => {
    button.textContent = "✓";
    button.classList.add("ok");
    setTimeout(() => {
      button.textContent = "⧉";
      button.classList.remove("ok");
    }, 1200);
  };
  // Needs a secure context. http://localhost counts, so the default binding is fine; a
  // Chief served over plain http on a LAN address is not, hence the fallback message.
  try {
    navigator.clipboard.writeText(text).then(done, () => {
      button.textContent = "⌫";
    });
  } catch {
    button.textContent = "⌫";
  }
}

/** Where an artifact is, as a row you can act on: open it, or take the path elsewhere. */
function pathRow(ref, artifact = null, label = null) {
  if (!ref) return null;
  const web = /^https?:/i.test(ref);
  const absolute = absolutePath(ref);
  // What gets copied is the most useful form of the reference, which for a relative path
  // means the resolved one — that is the version that means something in a terminal. With
  // no folder set there is still the raw ref, and handing that over beats handing over
  // nothing, so the copy control is unconditional.
  const target = web ? ref : absolute || ref;
  // Readable here when there is a run behind the screen to ask against. A URL is not ours
  // to fetch, and without a run there is no artifact id to name.
  // A web ref is framed rather than read, so it is openable here too — that is how a page
  // your own dev server renders gets to sit beside the run that produced it.
  const viewable = artifact && artifact.artifact_id && !!state.detail;

  return el(
    "span",
    { class: "art-path" },
    // The path itself opens it, rather than a control beside it: clicking the name of a
    // thing to see the thing is what a reader tries first. Leaving the page is the second
    // choice — the editor for a file, a new tab for a URL — and lives in the ↗.
    viewable
      ? el("button", {
          class: "art-href art-open", text: ref,
          title: web ? `Show ${ref} here` : `Open ${ref} here`,
          onClick: () => openViewer(artifact, label),
        })
      : web
        ? el("a", { class: "art-href", text: ref, href: ref, target: "_blank", rel: "noreferrer" })
        : absolute
          ? el("a", {
              class: "art-href", text: ref, href: editorHref(absolute), title: `Open ${absolute}`,
            })
          // Either no folder has been named, or the ref carries a scheme Chief cannot open.
          // A `vscode://` link built on a guessed base would open a "file not found" in the
          // editor, which reads as the editor being broken rather than the base being unset.
          : el("span", {
              class: "art-href", text: ref,
              title: isFileRef(ref) ? "Set a project folder to open this" : ref,
            }),
    // The editor, kept but demoted: a deep link still beats copying a path and pasting it,
    // and it is the only way out of Chief to the file. Only when a folder is set, since a
    // link built on a guessed base opens a "file not found".
    viewable && (web || absolute) &&
      el("a", {
        class: "art-edit", text: "↗",
        href: web ? ref : editorHref(absolute),
        ...(web ? { target: "_blank", rel: "noreferrer" } : {}),
        title: web ? `Open ${ref} in a tab` : `Open ${absolute} in the editor`,
      }),
    el("button", {
      // Say which path is on offer. A relative one is a real answer — it is what the
      // harness reported — but handing it over without saying so reads as the resolution
      // having silently failed, so the unset case names itself.
      class: absolute || web ? "art-copy" : "art-copy partial",
      text: "⧉",
      title: absolute || web ? `Copy ${target}` : `Copy ${target} — no project folder set`,
      onClick: (e) => copyPath(e.currentTarget, target),
    }),
  );
}

function rootRow(arts) {
  if (!arts.some(({ artifact }) => isFileRef(artifact.ref) && !artifact.ref.startsWith("/"))) {
    return null;
  }
  const project = currentProject();
  const root = readRoot(project);
  const origin = originDirOf();

  if (state.rootEditing) {
    return el(
      "div",
      { class: "art-root" },
      el("input", {
        class: "input", id: "files-root", type: "text", value: state.rootDraft,
        placeholder: origin || "/Users/you/projects/thing",
        onInput: (e) => setState({ rootDraft: e.target.value }),
        onKeyDown: (e) => e.key === "Enter" && saveRoot(),
      }),
      el("button", { class: "btn btn-primary btn-sm", text: "Save", onClick: () => saveRoot() }),
      el("button", {
        class: "btn btn-secondary btn-sm", text: "Cancel",
        onClick: () => setState({ rootEditing: false }),
      }),
    );
  }
  return el(
    "div",
    { class: "art-root" },
    el("span", {
      class: "art-root-label",
      text: root
        ? project ? `Folder for ${project}` : "Project folder"
        : "Set a project folder to open these",
    }),
    root && el("span", { class: "mono art-root-path", text: root }),
    // One click when the plan already says where it was made. It is still only a
    // suggestion — the tree may have moved since, and then this is the wrong answer and
    // typing one is the right one.
    !root && origin &&
      el("button", {
        class: "btn btn-secondary btn-sm",
        text: "Use where it ran",
        title: `Made in ${origin}. If the tree has moved since, set it by hand instead.`,
        onClick: () => saveRoot(origin),
      }),
    el("button", {
      class: "btn btn-secondary btn-sm", text: root ? "Change" : "Set…",
      onClick: () => setState({ rootEditing: true, rootDraft: root }),
    }),
  );
}

function saveRoot(value = state.rootDraft) {
  const cleaned = (value || "").trim().replace(/\/+$/, "");
  writeRoot(cleaned, currentProject());
  // `filesRoot` is only a render trigger now — the value that resolves a path is read back
  // out of storage per project, so keeping a second copy in state could only ever disagree
  // with it.
  setState({ filesRoot: cleaned, rootEditing: false });
}

// ── artifacts ────────────────────────────────────────────────────────────────────────────

const ICONS = {
  markdown: "≡", image: "▦", video: "▶", audio: "♪",
  url: "↗", pr: "↗", json: "{}", log: "⌗",
};

// ── artifact comments ────────────────────────────────────────────────────────────────────

/** What a person wanted said about an output, for whoever picks the work up. A harness
    reports what it produced; this is the other direction, and it reaches the agent through
    the run state it already fetches rather than through anything it has to be told to call. */
const cmtDraftFor = (artifactId) =>
  state.cmtDrafts[artifactId] || { body: "", open: false, error: null };

function setCmtDraft(artifactId, patch) {
  setState({
    cmtDrafts: { ...state.cmtDrafts, [artifactId]: { ...cmtDraftFor(artifactId), ...patch } },
  });
}

async function addComment(runId, artifactId) {
  const body = cmtDraftFor(artifactId).body.trim();
  if (!body) return;
  try {
    await commentOnArtifact(runId, artifactId, { body, author: "human" });
    // Only on success — a refused comment stays on screen to be corrected, exactly as a
    // refused checkpoint answer does.
    const rest = { ...state.cmtDrafts };
    delete rest[artifactId];
    setState({ cmtDrafts: rest });
    await refresh();
  } catch (err) {
    setCmtDraft(artifactId, { error: err instanceof ApiError ? err.message : String(err) });
  }
}

function commentBlock(artifact) {
  const runId = state.detail && state.detail.runId;
  const artifactId = artifact.artifact_id;
  // No run in view, or an artifact the server has not stamped yet: show what is there and
  // offer nothing, rather than a box that posts to nowhere.
  if (!runId || !artifactId) {
    return (artifact.comments || []).map(commentRow);
  }
  const draft = cmtDraftFor(artifactId);
  const inputId = `cmt-${artifactId}`;
  return [
    (artifact.comments || []).map(commentRow),
    draft.error && el("div", { class: "banner", text: draft.error }),
    draft.open
      ? el(
          "div",
          { class: "cmt-compose" },
          // A textarea, not a one-line input — same reasoning as a review note (see
          // note-compose above): "the numbers in here are stale, rerun with last week's
          // data" is a sentence, not eleven visible characters. The body is the element's
          // text rather than a `value` attribute, which a textarea ignores; safe because
          // every render builds a fresh node and the draft itself lives in state.
          el("textarea", {
            class: "note-input", id: inputId, rows: "3", text: draft.body,
            placeholder: "What should whoever picks this up know?",
            onInput: (e) => setCmtDraft(artifactId, { body: e.target.value, error: null }),
            // Enter is a newline here, so sending needs the modifier — same key as every
            // other multi-line box a person types a message into.
            onKeyDown: (e) =>
              e.key === "Enter" && (e.metaKey || e.ctrlKey) && addComment(runId, artifactId),
          }),
          el(
            "div",
            { class: "cmt-compose-actions" },
            el("button", {
              class: "btn btn-primary btn-sm", text: "Add",
              onClick: () => addComment(runId, artifactId),
            }),
            el("button", {
              class: "btn btn-secondary btn-sm", text: "Cancel",
              onClick: () => setCmtDraft(artifactId, { open: false, body: "", error: null }),
            }),
          ),
        )
      : el("button", {
          class: "cmt-add",
          text: (artifact.comments || []).length ? "＋ another comment" : "＋ comment",
          onClick: () => setCmtDraft(artifactId, { open: true }),
        }),
  ];
}

const commentRow = (comment) =>
  el(
    "div",
    { class: "cmt" },
    el("span", { class: "cmt-body md-block" }, markdown(comment.body)),
    el("span", {
      class: "cmt-meta",
      text: `${comment.author}${comment.created_at ? ` · ${relAgo(comment.created_at)}` : ""}`,
    }),
  );

/** The part of an artifact's `data` that describes it, rather than *being* it.

    `ArtifactRef.data` is overloaded: for a markdown artifact `data.text` is the document,
    and for a file somewhere else `data` is facts about it — dimensions, a row count, a
    digest. The keys already rendered as the preview are dropped here so nothing appears
    twice. See CONTRACT-NOTES.md #38. */
function artifactFacts(data) {
  if (!data || typeof data !== "object" || Array.isArray(data)) return null;
  const { text, ...facts } = data;
  return facts;
}

/** Whatever a harness attached to a step, an instance or an artifact.

    Rendered rather than only stored: it was reachable over the API and shown nowhere, which
    made it write-only in practice — a harness could report a token count or a cost every run
    and nobody would ever see one without curl.

    Through the same folding tree the file viewer uses, because it is the same problem: a
    value of unknown shape and unknown depth, which has to be readable when it is three keys
    and survivable when it is thirty. */
function metadataBlock(metadata, variant = "full", label = "Metadata") {
  if (!metadata || typeof metadata !== "object" || !Object.keys(metadata).length) return null;
  // The whole thing, wherever it is shown. What is inline is a summary by design, and a
  // summary with no way to the rest is the fold problem again in a different shape.
  const openAll = el("button", {
    class: "meta-open",
    text: "{ }",
    title: `Open all of ${label.toLowerCase()} as JSON`,
    onClick: (e) => {
      e.stopPropagation?.();
      openJsonViewer(label, metadata);
    },
  });
  if (variant === "compact") {
    // On an instance row, and shown rather than folded. What a harness attaches to a branch
    // is almost always what *distinguishes* that branch — which repo, which variant, which
    // seed — so hiding it behind a disclosure leaves eight rows reading "Branch 1..8" with
    // the one useful field one click away each. Scalars go inline; anything with structure
    // keeps the fold, because that does not fit on a row.
    const entries = Object.entries(metadata);
    const flat = entries.filter(([, v]) => v === null || typeof v !== "object");
    const deep = entries.filter(([, v]) => v !== null && typeof v === "object");
    return [
      el(
        "span",
        { class: "meta-flat" },
        flat.map(([key, value]) =>
          el("span", { class: "meta-pair" },
            el("span", { class: "meta-key", text: `${key} ` }),
            el("span", { text: String(value) })),
        ),
        // Named when there is nothing inline to name it, so a card with only nested
        // metadata still says it has some.
        !flat.length && el("span", { class: "meta-pair meta-key", text: `${entries.length} keys` }),
        openAll,
      ),
    ];
  }
  return [
    el(
      "span",
      { class: "section-label meta-head", style: { marginTop: "var(--space-1)" } },
      el("span", { style: { flex: "1" }, text: label }),
      openAll,
    ),
    el("div", { class: "viewer-json meta-json" }, jsonValue(metadata, 0)),
  ];
}

/** Roughly how many lines this will take at the panel's width.

    Counted rather than measured: measuring needs a laid-out node, and this runs while the
    tree is being built. It only has to be right about *long or not*, and it is generous —
    offering to expand something that already fits costs a click, while not offering costs
    the text. */
const LINE_CHARS = 46;
const looksLong = (text, lines) =>
  (text || "").split("\n").reduce((n, l) => n + Math.max(1, Math.ceil(l.length / LINE_CHARS)), 0) >
  lines;

/** One ArtifactRef as a card. `type` is an open string (REQ-46), so anything unrecognised
    degrades to its reference rendered as a link. */
function artifactCard(artifact, label) {
  const data = artifact.data || {};
  const title = label || artifact.description || artifact.ref || artifact.type;
  const body = [];
  let meta = artifact.type;

  // An artifact with no id cannot be keyed, so it renders open: better a tall card than one
  // whose expander does nothing.
  const key = artifact.artifact_id;
  const clipped =
    !!key && (looksLong(title, 2) || (artifact.type === "markdown" && looksLong(data.text, 9)));
  const toggle = () => setState({ artOpen: { ...state.artOpen, [key]: !state.artOpen[key] } });
  // Nothing is clamped unless there is a control to unclamp it. The line-count guess can
  // only cost a redundant "show more" on something that already fitted, never a clamp with
  // no way past it — which is the failure being fixed, and would be embarrassing to
  // reintroduce two lines below its own fix.
  const open = !clipped || !!state.artOpen[key];

  if (artifact.type === "markdown" && data.text) {
    meta = "markdown";
    body.push(
      el(
        "div",
        {
          class: open ? "art-md" : "art-md clipped",
          // The faded block is itself the control. Clicking cut-off text to see the rest is
          // what a person tries first, and it should not be the one thing that does nothing.
          ...(clipped ? { title: open ? "" : "Show all of it", onClick: toggle } : {}),
        },
        markdown(data.text),
      ),
    );
  } else if (artifact.type === "image" && artifact.ref) {
    meta = data.width ? `${data.width}×${data.height}` : "image";
    const ratio = data.width ? { aspectRatio: `${data.width} / ${data.height}` } : {};
    const img = el("img", {
      class: "art-img", src: artifact.ref, alt: title, loading: "lazy", style: ratio,
    });
    // An artifact is a reference, not a blob (REQ-46) — the image may well be somewhere
    // this browser cannot fetch. Say so instead of leaving an empty frame.
    img.addEventListener("error", () =>
      img.replaceWith(
        el(
          "div",
          { class: "art-img art-missing", style: ratio },
          el("span", { text: "image not reachable from here" }),
          el("span", { class: "art-href", text: artifact.ref }),
        ),
      ),
    );
    body.push(img);
  } else if (artifact.type === "video") {
    meta = "video" + (data.width ? ` · ${data.width}×${data.height}` : "");
    body.push(
      el(
        "div",
        { class: "art-video" },
        el("span", { class: "play", text: "▶" }),
        data.duration && el("span", { class: "dur", text: data.duration }),
      ),
    );
  } else if (artifact.type === "audio") {
    meta = "audio";
    // A deterministic waveform stand-in: the backend stores metadata only, never the blob.
    let h = 0;
    for (const c of artifact.ref || "x") h = (h * 31 + c.charCodeAt(0)) >>> 0;
    const bars = Array.from({ length: 36 }, () => {
      h = (h * 1103515245 + 12345) >>> 0;
      return el("span", { style: { height: `${6 + (h % 20)}px` } });
    });
    body.push(
      el(
        "div",
        { class: "art-audio" },
        el("span", { class: "play", text: "▶" }),
        el("span", { class: "wave" }, bars),
        data.duration && el("span", { class: "art-meta", text: data.duration }),
      ),
    );
  }

  // Deliberately outside the chain above: a ref is a ref whatever the type made of it, so
  // an image and a markdown file both get the same row. It used to be the chain's last
  // branch, which meant the one artifact that rendered a preview was the one whose location
  // you could not read.
  const path = pathRow(artifact.ref, artifact, title);

  return el(
    "section",
    { class: "card", style: { padding: "var(--space-3)" } },
    el(
      "span",
      { class: "art-head" },
      el("span", { class: "art-icon", text: ICONS[artifact.type] || "⌗" }),
      // Wraps rather than ending in an ellipsis. A description is the harness saying what
      // this file is, and a single line of it with the rest cut off is the half that
      // happened to fit, not the half worth reading.
      el("span", {
        class: open || !clipped ? "art-label" : "art-label clipped",
        text: title,
        ...(clipped ? { title: open ? "" : "Show all of it", onClick: toggle } : {}),
      }),
      el("span", { class: "art-meta", text: meta }),
    ),
    body,
    // `data` does two jobs, so it is shown two ways. `text` is the artifact *itself* and is
    // already the preview above — repeating it here would be the same thing twice. Everything
    // else is the harness describing what it produced, and that is worth reading without a
    // click, exactly as an instance's metadata is.
    metadataBlock(artifactFacts(artifact.data), "compact", `${title} · data`),
    // Its own control rather than the comment-link style it borrowed at first: eleven grey
    // pixels under a faded block reads as a caption, and a person looking for the rest of a
    // sentence does not find it.
    clipped &&
      el("button", {
        class: "art-more",
        text: open ? "Show less ▲" : "Show more ▼",
        onClick: toggle,
      }),
    path,
    commentBlock(artifact),
  );
}

/** Every artifact under a step, including the ones its instances produced. */
function stepArtifacts(state, prefix = "") {
  const out = (state.artifacts || []).map((a) => ({ artifact: a, label: prefix + (a.description || a.ref || a.type) }));
  for (const instance of state.instances || []) {
    const name = `${instance.kind === "iteration" ? "Iteration" : "Branch"} ${instance.index + 1}`;
    for (const a of instance.artifacts || []) {
      out.push({ artifact: a, label: `${prefix}${name} · ${a.description || a.ref || a.type}` });
    }
    for (const [bodyId, bodyState] of Object.entries(instance.step_states || {})) {
      out.push(...stepArtifacts(bodyState, `${prefix}${name} · ${bodyId} · `));
    }
  }
  return out;
}

const runArtifacts = (def, stepStates) =>
  def.steps.flatMap((step) => {
    const state = stepStates[step.id];
    return state ? stepArtifacts(state, `${step.id} · `) : [];
  });

// ── amendment diff (REQ-40, rendered client-side from the stored operations) ──────────────

const quote = (s) => `“${s}”`;

/** The operations as a human-readable diff (REQ-40).

    `def` is the plan the amendment would be applied to. Pass null where it is not loaded —
    the inbox, which lists amendments across runs — and the rows state only what is
    proposed. Without the plan there is no "before" side, and inventing one ("was nothing")
    would assert something false. */
function opRows(amendment, def) {
  return amendment.operations.map((op) => {
    const before = def && def.steps.find((s) => s.id === op.target_step_id);
    const scope = op.instance_id
      ? ` · ${op.instance_id}`
      : op.instance_path
        ? ` · ${op.instance_path.join("/")}`
        : "";

    if (op.op === "insert_after" || op.op === "insert_before") {
      const where = op.op === "insert_after" ? "after" : "before";
      const deps = (op.step.depends_on || []).join(", ");
      return {
        badge: "+ insert", badgeColor: OK,
        text: `${op.step.id} — ${quote(op.step.goal)}`,
        note: `new step · ${where} ${op.target_step_id}` +
          (deps ? ` · runs after ${deps}` : "") + ` · ${op.step.harness}`,
      };
    }
    if (op.op === "remove_step") {
      return {
        badge: "− remove", badgeColor: BAD,
        text: op.target_step_id + (before ? ` — ${quote(before.goal)}` : "") + scope,
      };
    }
    if (op.op === "replay_step") {
      return {
        badge: "↻ replay", badgeColor: WARN,
        text: `re-runs ${op.target_step_id}${scope}`,
        note: "history edit — the prior result is snapshotted",
      };
    }
    // update_step
    const newDeps = (op.step?.depends_on || []).join(", ");
    if (!before) {
      // No plan to compare against: state the proposal, claim no "before".
      return {
        badge: "~ update", badgeColor: ACC,
        text: op.target_step_id + scope,
        now: op.step ? op.step.goal : null,
        note: `depends on ${newDeps || "nothing"} · ${op.step?.harness ?? ""}`.trim(),
      };
    }
    const goalChanged = op.step && before.goal !== op.step.goal;
    const oldDeps = (before.depends_on || []).join(", ");
    const harnessChanged = op.step && before.harness !== op.step.harness;
    return {
      badge: "~ update", badgeColor: ACC,
      text: op.target_step_id + scope,
      was: goalChanged ? before.goal : null,
      now: goalChanged ? op.step.goal : null,
      note:
        oldDeps !== newDeps
          ? `now depends on ${newDeps || "nothing"} (was ${oldDeps || "nothing"})`
          : harnessChanged
            ? `now runs on ${op.step.harness} (was ${before.harness})`
            : null,
    };
  });
}

const opRow = (row) =>
  el(
    "div",
    { class: "op" },
    el("span", {
      class: "op-badge", text: row.badge,
      style: {
        color: row.badgeColor,
        border: `1px solid color-mix(in srgb, ${row.badgeColor} 40%, transparent)`,
      },
    }),
    el(
      "span",
      { class: "op-lines" },
      el("span", { class: "op-text", text: row.text }),
      row.was && el("span", { class: "op-was", text: row.was }),
      row.now && el("span", { class: "op-now", text: row.now }),
      row.note && el("span", { class: "op-note", text: row.note }),
    ),
  );

const kindLabel = (a) => (a.kind === "history_edit" ? "history edit" : "forward");

// ── application state ────────────────────────────────────────────────────────────────────

const state = {
  view: "workflows", // workflows | workflow | approvals | detail
  runId: null,
  workflowId: null,
  templateId: null,
  templates: undefined, // undefined = not loaded yet, null = server has no templates

  workflowAudit: null, // { workflowId, entries }
  // Review notes for the workflow on screen: { workflowId, notes }. Fetched per screen and
  // cleared on the way out, like workflowAudit — a poll landing mid-navigation must not
  // leave one plan's feedback attached to another's.
  workflowNotes: null,
  selected: null, // "step:<id>" | "am:<id>" | "pam:<id>" | "none"
  runs: null, // null until the first load resolves
  workflows: null,
  amendmentsByRun: {},
  // The effective plan, fetched only for runs stopped at a checkpoint — the inbox needs the
  // step's goal and the fields it asks for, and neither is on RunState.
  plansByRun: {},
  // Half-typed checkpoint answers, keyed by run+path. In state for the same reason the list
  // search box is: the poll rebuilds the DOM, and anything held only in the DOM is lost.
  cpDrafts: {},
  // Half-written artifact comments, keyed by artifact id, for the same reason cpDrafts
  // exists: the poll rebuilds the DOM and takes anything held only in it.
  cmtDrafts: {},
  // Which artifact cards have been opened out, keyed by artifact id. Collapsed is the
  // default because the panel is a list of what a run produced, not a reader — but the
  // collapse has to be reversible, which is the whole of this.
  artOpen: {},
  // Half-written review notes and which threads have their resolved history open, both
  // keyed by what the note is about — a step id, or NOTE_PLAN. In state for the reason
  // cmtDrafts is: the poll rebuilds the DOM every 15 seconds and takes anything held only
  // in it, including the box you are typing into.
  noteDrafts: {},
  noteShow: {},
  // What a relative artifact ref is relative to, and the half-typed version of it while it
  // is being changed. Read from localStorage once, here, so every render is a plain field
  // lookup rather than a trip through storage.
  filesRoot: readRoot(),
  // How wide the right-hand panel is. Read from storage once, here, so every render is a
  // field lookup rather than a trip through localStorage.
  inspectorWidth: readInspectorWidth(),
  // The open file, if any: { runId, artifactId, ref, title, loading, error, file }. Null is
  // the closed drawer, and the drawer is not rendered at all when it is null.
  viewer: null,
  // An artifact id from the URL that has not been opened yet, because the run it hangs off
  // was still being fetched when the page loaded. Cleared as soon as it is opened, or as
  // soon as it turns out not to exist.
  viewerPending: null,
  viewerWidth: readViewerWidth(),
  rootEditing: false,
  rootDraft: "",
  detail: null, // { runId, state, def, amendments }
  dialog: null,
  error: null,
  graphWidth: 720,

  // How the workflow list is being read. Not in the URL: the hash addresses *where you
  // are*, and a filter is how you are looking at it. They are also not cleared by `go()` or
  // touched by `refresh()`'s patch, so they survive polling and coming back to the list.
  wfQuery: "",
  wfFilter: "active",
  // Which project the list is narrowed to: null is all of them, UNFILED is the ones nobody
  // has filed. Not in the URL, for the same reason the other filters are not — the hash
  // addresses where you are, and this is how you are looking at it.
  wfProject: null,
  // The workflow whose project label is being edited, and the half-typed name.
  filing: null,
  // Newest activity first: the list is a place you come back to, and what moved since you
  // last looked is what you are coming back for.
  wfSort: { key: "updated", dir: "desc" },
};

function setState(patch) {
  Object.assign(state, patch);
  render();
  writeHash();
}

// ── routing ──────────────────────────────────────────────────────────────────────────────
//
// The URL is the address of what you are looking at. Without one, a reload — or a link sent
// to someone — lands on the workflow list and makes you find the thing again. Only *where
// you are* is in the hash: selection, dialogs and fetched documents are session state, and
// putting them in the URL would make every click a history entry.

const LIST_VIEWS = { workflows: 1, approvals: 1, templates: 1 };
const ROUTED = { workflow: "workflowId", detail: "runId", template: "templateId" };
const ROUTE_KIND = { workflow: "workflow", detail: "run", template: "template" };

function hashFor(s) {
  const key = ROUTED[s.view];
  if (key && s[key]) {
    const base = `#/${ROUTE_KIND[s.view]}/${encodeURIComponent(s[key])}`;
    // The open file is part of where you are, not of how you are looking at it: a link to
    // "this run, this artifact" is worth being able to send, and a reload that dropped it
    // would send you back to a panel you then have to find your way into again. The other
    // browser state — filters, selection, panel widths — stays out of the URL for the
    // opposite reason: it is how, not where.
    const open = s.viewer ? s.viewer.artifactId : s.viewerPending;
    return open ? `${base}/${encodeURIComponent(open)}` : base;
  }
  return `#/${LIST_VIEWS[s.view] ? s.view : "workflows"}`;
}

/** What the address bar last agreed with, so a hash we wrote does not read back as a
    navigation the user asked for. */
let appliedHash = null;

function writeHash() {
  const next = hashFor(state);
  if (next === appliedHash) return;
  appliedHash = next;
  if (location.hash !== next) location.hash = next;
}

function stateFromHash() {
  const [kind, raw, rawArtifact] = location.hash.replace(/^#\/?/, "").split("/");
  const id = raw && decodeURIComponent(raw);
  const blank = {
    runId: null, workflowId: null, templateId: null,
    selected: null, detail: null, dialog: null, workflowAudit: null, workflowNotes: null,
    viewer: null, viewerPending: null,
  };
  const view = Object.keys(ROUTED).find((v) => ROUTE_KIND[v] === kind);
  if (view && id) {
    // Held as "pending" rather than opened here: the run this artifact belongs to has not
    // been fetched yet, so there is nothing to open it from. `refresh` picks it up once the
    // state arrives. See `restorePendingViewer`.
    const pending = rawArtifact ? decodeURIComponent(rawArtifact) : null;
    return { ...blank, view, [ROUTED[view]]: id, viewerPending: pending };
  }
  return { ...blank, view: LIST_VIEWS[kind] ? kind : "workflows" };
}

const pendingOf = (list) => (list || []).filter((a) => a.status === "pending_approval");

function allPending() {
  const out = [];
  for (const run of state.runs || []) {
    for (const a of pendingOf(state.amendmentsByRun[run.run_id])) out.push({ run, amendment: a });
  }
  return out;
}

/** Every checkpoint in one run that is stopped waiting for a person, with the path that
    addresses it and the step that declared it.

    Walks the whole state tree rather than the top level: a checkpoint inside a loop body is
    the interesting case — an iteration that wants a look before it goes round again. */
function blockedIn(run, steps) {
  const byId = new Map((steps || []).map((s) => [s.id, s]));
  const found = [];
  const walk = (container, prefix) => {
    for (const [stepId, st] of Object.entries(container || {})) {
      const path = [...prefix, stepId];
      if (st.status === "blocked") found.push({ run, path, stepId, step: byId.get(stepId), state: st });
      for (const inst of st.instances || []) walk(inst.step_states, [...path, inst.instance_id]);
    }
  };
  walk(run.step_states, []);
  return found;
}

/** Everything waiting on a person, in one list: the amendments, then the checkpoints.

    Both are the same act — the run has stopped and will not move until you decide — so they
    share one inbox rather than making you learn which of two screens a given wait shows up
    on. A checkpoint whose plan has not loaded yet is still listed: knowing something is
    waiting matters more than being able to answer it this second. */
function allWaiting() {
  const out = allPending().map((p) => ({ kind: "amendment", ...p }));
  for (const run of state.runs || []) {
    const plan = state.plansByRun[run.run_id];
    for (const c of blockedIn(run, plan && plan.steps)) out.push({ kind: "checkpoint", ...c });
  }
  return out;
}

/** A run's title lives on its workflow, not on RunState, so the list resolves it through
    the workflow index. A run's *effective* plan may have been amended since; the detail
    screen shows that title, taken from the run's own definition. */
const titles = {};
const titleOf = (run) => titles[run.run_id] || titles[run.workflow_id] || run.workflow_id;

// ── loading ──────────────────────────────────────────────────────────────────────────────

const draftsOf = (workflows) => (workflows || []).filter((w) => w.status === "draft");

async function loadRuns() {
  // Templates are an extension, and an older Chief will 404 them. One missing endpoint must
  // not take the whole UI down with it: the workflows screen has no need of templates, and
  // leaving every screen on "Loading…" because of an unrelated 404 is the worst of both —
  // nothing works and nothing says why.
  const [runs, workflows, templates] = await Promise.all([
    listRuns(),
    listWorkflows(),
    listTemplates().catch((err) => {
      if (err instanceof ApiError && err.status === 404) return null;
      throw err;
    }),
  ]);
  for (const wf of workflows) titles[wf.workflow_id] = wf.title;
  // Amendments are one small request per run and drive the nav badge on every screen.
  // The heavy documents (state + definition) are fetched only for the run being viewed.
  const lists = await Promise.all(runs.map((r) => listAmendments(r.run_id)));
  const amendmentsByRun = {};
  runs.forEach((run, i) => (amendmentsByRun[run.run_id] = lists[i]));

  // A run's own plan, for the runs stopped at a checkpoint. Not the workflow definition: an
  // amendment may have introduced the checkpoint, in which case the base plan has never
  // heard of it. Kept to the runs that need it so the common case stays two requests.
  //
  // Selected by "has a blocked step", not by run status: an amendment pause outranks
  // `waiting_on_human`, so a run that is both would otherwise render a checkpoint card with
  // no plan behind it — no question, no fields, and an Approve button that posts nothing.
  const waiting = runs.filter((r) => blockedIn(r, []).length > 0);
  const plans = await Promise.all(waiting.map((r) => getRunDefinition(r.run_id).catch(() => null)));
  const plansByRun = { ...state.plansByRun };
  waiting.forEach((run, i) => (plansByRun[run.run_id] = plans[i]));
  return { runs, workflows, templates, amendmentsByRun, plansByRun };
}

async function refresh() {
  try {
    const patch = await loadRuns();
    // Fetched per screen, and cleared when you leave it: a poll that lands mid-navigation
    // must not leave one workflow's decisions attached to another's detail.
    patch.workflowAudit =
      state.view === "workflow" && state.workflowId
        ? { workflowId: state.workflowId, entries: await getWorkflowAudit(state.workflowId) }
        : null;
    patch.workflowNotes =
      state.view === "workflow" && state.workflowId
        ? { workflowId: state.workflowId, notes: await listReviewNotes(state.workflowId) }
        : null;

    // A workflow that is executing shows its execution: the same screen, further along. The
    // run carries the *effective* plan, which is the base plan plus any applied amendment,
    // so it is what the graph must be drawn from once a run exists.
    if (state.view === "workflow" && state.workflowId) {
      const workflow = (patch.workflows || []).find((w) => w.workflow_id === state.workflowId);
      const run = workflow && executionsOf(workflow, patch.runs)[0];
      if (run) {
        const detail = await getRunDetail(run.run_id);
        patch.detail = { runId: run.run_id, ...detail };
        patch.amendmentsByRun[run.run_id] = detail.amendments;
      } else {
        patch.detail = null;
      }
    }
    if (state.view === "detail" && state.runId) {
      const detail = await getRunDetail(state.runId);
      titles[state.runId] = detail.def.title;
      patch.detail = { runId: state.runId, ...detail };
      patch.amendmentsByRun[state.runId] = detail.amendments;
    }
    setState({ ...patch, error: null });
    // After the state lands, not before: a URL naming an artifact has nothing to resolve
    // against until the run it belongs to has been fetched.
    restorePendingViewer();
  } catch (err) {
    setState({ error: err instanceof ApiError ? err.message : String(err) });
  }
}

async function openRun(runId) {
  setState({ view: "detail", runId, selected: null, detail: null });
  await refresh();
}

function openTemplate(templateId) {
  setState({ view: "template", templateId, selected: null, dialog: null });
}

function openWorkflow(workflowId) {
  setState({
    view: "workflow", workflowId, selected: null, dialog: null,
    workflowAudit: null, workflowNotes: null, noteDrafts: {}, noteShow: {},
  });
  refresh();
}

function go(view) {
  setState({ view, runId: null, selected: null, detail: null, dialog: null });
  if (view !== "detail") refresh();
}

// ── decisions ────────────────────────────────────────────────────────────────────────────

function openDialog(amendment, approve) {
  setState({
    dialog: {
      subject: "amendment",
      amendment, approve,
      reason: "",
      busy: false,
      error: null,
      key: `${amendment.amendment_id}:${approve}`,
    },
  });
}

/** The workflow-lifecycle decisions (REQ-32). Same dialog, because the thing that matters
    about it is not the wording but that the server's answer is believed rather than
    assumed — the status may have moved under us since the list was loaded. */
/** Asking before erasing a plan and everything it ran.

    Its own dialog rather than a third `action` on the one above, because the question is a
    different shape: archiving asks for a reason to record, and there will be no record here
    to attach one to. What this owes the reader instead is an accurate list of what goes and
    what does not — which is why the copy names the audit trail and the files on disk. */
function openDeleteDialog(workflow) {
  setState({
    dialog: {
      subject: "delete",
      workflow,
      busy: false,
      error: null,
      key: `del:${workflow.workflow_id}`,
    },
  });
}

function deleteDialogNode() {
  const { workflow, busy, error } = state.dialog;
  return el(
    "div",
    { class: "dialog-backdrop", onClick: () => !busy && setState({ dialog: null }) },
    el(
      "div",
      {
        class: "confirm-pop", role: "dialog", "aria-modal": "true",
        onClick: (event) => event.stopPropagation(),
      },
      el("span", { class: "section-label", text: "Delete workflow" }),
      el(
        "p",
        {},
        el("strong", { text: workflow.title }),
        // Said plainly and in full. A confirmation that undersells what it removes is worse
        // than none, because it buys agreement the person did not actually give.
        " and its full history — steps, iterations, artifacts, approvals — will be " +
          "permanently removed. This cannot be undone.",
      ),
      el("p", {
        class: "text-muted", style: { fontSize: "12px" },
        text:
          "Files on disk are not touched, and the audit log keeps a record of the deletion. " +
          "A template saved from this workflow is kept.",
      }),
      error && el("p", { style: { fontSize: "12px", color: "var(--bad)" }, text: error }),
      el(
        "div",
        { class: "dialog-actions" },
        el("button", {
          class: "btn btn-secondary btn-sm", text: "Cancel", disabled: busy,
          onClick: () => setState({ dialog: null }),
        }),
        el("button", {
          class: "btn btn-secondary btn-danger btn-sm",
          text: busy ? "Deleting…" : "Delete workflow", disabled: busy,
          onClick: async () => {
            state.dialog.busy = true;
            render();
            try {
              await deleteWorkflow(workflow.workflow_id);
              setState({ dialog: null });
              // Back to the list either way: the screen behind this one may have been the
              // workflow that no longer exists.
              go("workflows");
              await refresh();
            } catch (err) {
              state.dialog.busy = false;
              state.dialog.error = err instanceof ApiError ? err.message : String(err);
              render();
            }
          },
        }),
      ),
    ),
  );
}

function openWorkflowDialog(workflow, action) {
  setState({
    dialog: {
      subject: "workflow",
      workflow, action,
      reason: "",
      busy: false,
      error: null,
      key: `${workflow.workflow_id}:${action}`,
    },
  });
}

/** Filling in a template's parameters is a form, not a confirmation, so the dialog carries a
    value per parameter rather than one reason string. Defaults are prefilled: the common case
    for an extracted template is to take it as it stands. */
function openTemplateDialog(template) {
  const values = {};
  for (const p of template.parameters) values[p.name] = p.default ?? "";
  setState({
    dialog: {
      subject: "template", action: "use", template, values,
      busy: false, error: null, key: `${template.template_id}:use`,
    },
  });
}

function openTemplateArchiveDialog(template) {
  setState({
    dialog: {
      subject: "template", action: "archive", template,
      busy: false, error: null, key: `${template.template_id}:archive`,
    },
  });
}

async function confirmDialog() {
  const { subject, amendment, approve, reason, workflow, action } = state.dialog;
  setState({ dialog: { ...state.dialog, busy: true, error: null } });
  try {
    if (subject === "template") {
      const { template, action, values } = state.dialog;
      if (action === "archive") {
        await archiveTemplate(template.template_id);
      } else {
        // Straight to the workflow it produced: the point of using a template is the plan
        // now waiting for a decision, not the template you started from.
        const created = await instantiateTemplate(template.template_id, values);
        setState({ dialog: null });
        await refresh();
        openWorkflow(created.workflow_id);
        return;
      }
    } else if (subject === "workflow") {
      await (action === "approve" ? approveWorkflow : archiveWorkflow)(workflow.workflow_id, reason);
    } else {
      await decideAmendment(amendment.amendment_id, approve, reason);
    }
  } catch (err) {
    // The server can legitimately refuse — an immutability violation, or a race with
    // another client. Keep the dialog open and say why rather than showing it as decided.
    setState({
      dialog: {
        ...state.dialog, busy: false,
        error: err instanceof ApiError ? err.message : String(err),
      },
    });
    return;
  }
  setState({ dialog: null, selected: null });
  await refresh();
}

// ── screens ──────────────────────────────────────────────────────────────────────────────

function navBar() {
  const pending = allWaiting().length;
  const drafts = draftsOf(state.workflows).length;
  const link = (label, view, active, extra) =>
    el(
      "a",
      {
        href: "#", "aria-current": active ? "page" : null,
        onClick: (e) => {
          e.preventDefault();
          go(view);
        },
      },
      label,
      extra,
    );

  return el(
    "nav",
    { class: "nav" },
    // The mark itself, not a coloured square standing in for it: it is the same file the
    // tab icon is drawn from, served beside this script.
    el(
      "span", { class: "nav-brand" },
      el("img", { class: "nav-mark", src: "./chief-mark.svg", alt: "" }),
      "Chief",
    ),
    // No Runs entry: an execution is a workflow in the running state, and its detail sits
    // under Workflows. `detail` is the rare second-execution escape hatch, and belongs there
    // too.
    link(
      "Workflows", "workflows",
      state.view !== "approvals" && state.view !== "templates" && state.view !== "template",
      // A draft cannot run until someone approves it (REQ-32), so it is as much "waiting on
      // you" as a pending amendment is. Counting it here is what makes that visible.
      drafts > 0 &&
        el("span", { class: "tag tag-accent", style: { padding: "0 7px" }, text: String(drafts) }),
    ),
    state.templates !== null &&
      link("Templates", "templates", state.view === "templates" || state.view === "template"),
    link(
      "Approvals", "approvals", state.view === "approvals",
      pending > 0 &&
        el("span", { class: "tag tag-accent", style: { padding: "0 7px" }, text: String(pending) }),
    ),
  );
}

/** One row per workflow: its name, where it is in its lifecycle, and how far along.

    There is no separate runs list. A workflow that is executing is a workflow in the running
    state, and clicking it shows the same plan it showed while it was a draft — with progress
    on it. */
function workflowRow({ workflow, runs, life, progress, updated, duration }) {
  return el(
    "button",
    { class: "run-row", onClick: () => openWorkflow(workflow.workflow_id) },
    el(
      "span",
      { class: "title" },
      el("span", { text: workflow.title }),
      el("span", { class: "id text-muted", text: workflow.workflow_id }),
      runs.length > 1 &&
        el("span", { class: "tag", text: `${runs.length} executions` }),
    ),
    el("span", { class: "status" }, badge(life)),
    el("span", {
      class: "when",
      text: progress ? `${progress.done}/${progress.total}` : `${workflow.steps.length} steps`,
    }),
    el("span", {
      class: "stamp", text: stamp(workflow.created_at),
      title: workflow.created_at ? new Date(workflow.created_at).toLocaleString() : null,
    }),
    el("span", {
      class: "stamp", text: stamp(updated),
      title: updated ? `${relAgo(updated)} \u00b7 ${new Date(updated).toLocaleString()}` : null,
    }),
    el("span", { class: "dur", text: fmtDuration(duration) }),
    // A button inside the row button: the click has to be stopped or the workflow opens
    // underneath the confirmation asking whether to delete it.
    el("button", {
      class: "row-del", text: "\u2715", title: `Delete ${workflow.title}…`,
      "aria-label": `Delete ${workflow.title}`,
      onClick: (event) => {
        event.stopPropagation();
        openDeleteDialog(workflow);
      },
    }),
  );
}

/** How much of the plan is finished, counting only top-level steps — body steps belong to
    their construct and would double-count. */
function progressOf(workflow, run) {
  const top = workflow.steps.filter((x) => !workflow.steps.some((p) => (p.body || []).includes(x.id)));
  const states = run.step_states || {};
  return {
    done: top.filter((x) => ["completed", "skipped"].includes((states[x.id] || {}).status)).length,
    total: top.length,
  };
}

/** Ordered so the ones that need something from a person come first, then what is moving,
    then what is finished. Status alone would sort alphabetically and bury the drafts. */
const LIFECYCLE_ORDER = [
  "draft", "waiting_on_human", "paused_for_approval", "running", "ready", "completed", "failed",
];

/** The lifecycles that are stopped on a person: a plan nobody has approved, a run stopped at
    a checkpoint, a run paused on an amendment. All three mean the same thing to whoever is
    reading the list — nothing moves here until you do something. */
const ATTENTION = { draft: 1, waiting_on_human: 1, paused_for_approval: 1 };

/** The list's filters. "Active" is what the screen showed before there were any — archived
    workflows out, everything else in — so the default view has not changed. */
/** The bucket for workflows with no project. A sentinel rather than `null`, because `null`
    already means "no project filter at all" and the two are opposites: one shows
    everything, the other shows only the ones nobody has filed. */
const UNFILED = "\u0000unfiled";

/** One chip per project in use, plus the unfiled, plus an everything chip.

    Derived from the workflows on screen rather than from `GET /projects`: the counts have
    to agree with the list under them, and a second source could only ever disagree. The row
    is absent entirely until something is labelled, so nobody who does not use projects has
    to look at a filter that does nothing. */
function projectChips(rows) {
  const counts = new Map();
  for (const r of rows) counts.set(r.workflow.project || UNFILED, (counts.get(r.workflow.project || UNFILED) || 0) + 1);
  const named = [...counts.keys()].filter((k) => k !== UNFILED).sort((a, b) => a.localeCompare(b));
  if (named.length === 0) return null;

  const chip = (key, label) =>
    el("button", {
      class: "chip" + (state.wfProject === key ? " on" : ""),
      text: `${label} ${counts.get(key) || rows.length}`,
      onClick: () => setState({ wfProject: state.wfProject === key ? null : key }),
    });
  return el(
    "div",
    { class: "chips chips-project" },
    el("button", {
      class: "chip" + (state.wfProject === null ? " on" : ""),
      text: `Every project ${rows.length}`,
      onClick: () => setState({ wfProject: null }),
    }),
    named.map((name) => chip(name, name)),
    counts.has(UNFILED) && chip(UNFILED, "Unfiled"),
  );
}

const WF_FILTERS = [
  { key: "active", label: "Active", of: (w) => w.status !== "archived" },
  // What is waiting on a person: an unapproved draft, or a run paused on an amendment.
  { key: "attention", label: "Needs you", of: (w, life) => ATTENTION[life.key] === 1 },
  { key: "running", label: "Running", of: (w, life) => life.key === "running" },
  { key: "done", label: "Finished", of: (w, life) => life.key === "completed" || life.key === "failed" },
  { key: "archived", label: "Archived", of: (w) => w.status === "archived" },
  { key: "all", label: "All", of: () => true },
];

/** Each sortable column, as the value it sorts on.

    `updated` is the default — the list is a thing you return to, and what has moved since
    you last looked is what you came back for. `lifecycle` sorts by LIFECYCLE_ORDER rather
    than by the status string, because the whole point of that order is that alphabetical
    buries the drafts.

    `updated` is the newest execution's timestamp, and a workflow that has never run simply
    has none. Those sort last in *both* directions rather than pretending to a date: the
    workflow document carries no created_at, so there is nothing honest to fall back on. */
const WF_COLUMNS = [
  { key: "title", label: "Workflow", cls: "title", dir: "asc", of: (w) => w.title.toLowerCase() },
  { key: "lifecycle", label: "Status", cls: "status", dir: "asc", of: (w, ctx) => ctx.rank },
  // One quantity, not two: how much of the plan is done. Never run is 0, and the cell still
  // reads "N steps" because that is the useful thing to know about a plan nobody has run.
  { key: "progress", label: "Progress", cls: "when", dir: "desc", of: (w, ctx) => ctx.fraction },
  // When the plan was submitted — the one column every workflow can be sorted by, run or
  // not. It comes from the store's own record of the row, not from anything a harness said.
  { key: "added", label: "Added", cls: "stamp", dir: "desc", of: (w) => w.created_at || "" },
  // Last touched, whichever touched it: a run reporting a step, or the record itself being
  // approved, revised or archived. Taking only one of the two leaves a column that goes
  // stale while the thing it describes is moving.
  { key: "updated", label: "Last updated", cls: "stamp", dir: "desc", of: (w, ctx) => ctx.updated },
  { key: "duration", label: "Duration", cls: "dur", dir: "desc", of: (w, ctx) => ctx.duration },
];

// The column an unknown sort key falls back to, and the one the list opens on.
const WF_DEFAULT_COLUMN = WF_COLUMNS.find((c) => c.key === "updated");

function sortWorkflows(key, dir) {
  const col = WF_COLUMNS.find((c) => c.key === key) || WF_DEFAULT_COLUMN;
  setState({ wfSort: { key, dir: dir || col.dir } });
}

function workflowsScreen() {
  const workflows = state.workflows;
  if (!workflows || !state.runs)
    return el("main", { class: "narrow" }, el("p", { class: "text-muted", text: "Loading…" }));

  // Everything the list sorts or filters on is derived once per workflow, so a comparator
  // never re-walks the runs.
  const rows = workflows.map((w) => {
    const runs = executionsOf(w, state.runs);
    const life = lifecycleOf(w, runs);
    const rank = LIFECYCLE_ORDER.indexOf(life.key);
    const progress = runs[0] ? progressOf(w, runs[0]) : null;
    return {
      workflow: w, runs, life,
      rank: rank === -1 ? LIFECYCLE_ORDER.length : rank,
      progress,
      fraction: progress && progress.total ? progress.done / progress.total : 0,
      updated: [w.updated_at || "", runs[0] ? runs[0].updated_at : ""].sort().pop() || "",
      duration: durationOf(runs[0]),
    };
  });

  const filter = WF_FILTERS.find((f) => f.key === state.wfFilter) || WF_FILTERS[0];
  const q = state.wfQuery.trim().toLowerCase();
  const matches = (r) =>
    !q ||
    r.workflow.title.toLowerCase().includes(q) ||
    r.workflow.workflow_id.includes(q) ||
    (r.workflow.project || "").toLowerCase().includes(q);
  // `null` is the everything chip; UNFILED is the bucket for workflows with no label, which
  // is every one that predates projects and has to stay reachable rather than filtered away.
  const inProject = (r) =>
    state.wfProject === null ||
    (state.wfProject === UNFILED ? !r.workflow.project : r.workflow.project === state.wfProject);
  const shown = rows.filter((r) => filter.of(r.workflow, r.life) && inProject(r) && matches(r));

  const { key, dir } = state.wfSort;
  const col = WF_COLUMNS.find((c) => c.key === key) || WF_DEFAULT_COLUMN;
  const sign = dir === "desc" ? -1 : 1;
  const cmp = (a, b) => {
    const [x, y] = [col.of(a.workflow, a), col.of(b.workflow, b)];
    // A missing timestamp is not "oldest" — it is unknown, and unknown belongs at the end
    // whichever way the column is pointing.
    if (x === "" || y === "") return x === y ? 0 : x === "" ? 1 : -1;
    return x < y ? -sign : x > y ? sign : 0;
  };
  const sorted = [...shown].sort((a, b) => cmp(a, b) || a.workflow.title.localeCompare(b.workflow.title));

  const live = rows.filter((r) => r.workflow.status !== "archived");
  const waiting =
    live.filter((r) => r.workflow.status === "draft").length + allWaiting().length;
  const running = live.filter((r) => r.life.key === "running").length;

  return el(
    "main",
    // Wider than the other screens: this one is a table, and six columns in a 760px reading
    // column leaves the titles nothing.
    { class: "wide", "data-screen-label": "Workflows" },
    el(
      "header",
      { class: "screen-head" },
      el("h4", { text: "Workflows" }),
      el("span", {
        class: "text-muted", style: { fontSize: "12px" },
        text: `${running} running · ${waiting} awaiting a decision`,
      }),
    ),
    workflows.length > 0 &&
      el(
        "div",
        { class: "list-controls" },
        el(
          "div",
          { class: "chips" },
          WF_FILTERS.map((f) => {
            const count = rows.filter((r) => f.of(r.workflow, r.life)).length;
            return el("button", {
              class: "chip" + (f.key === filter.key ? " on" : ""),
              text: `${f.label} ${count}`,
              onClick: () => setState({ wfFilter: f.key }),
            });
          }),
        ),
        projectChips(rows),
        el("input", {
          // Rebuilt on every render, so its value comes from state and `render` puts the
          // caret back — otherwise the 15s poll would eat what you are typing.
          id: "wf-search", class: "field-search", type: "search",
          placeholder: "Filter by name or id", value: state.wfQuery,
          onInput: (e) => setState({ wfQuery: e.target.value }),
        }),
      ),
    workflows.length === 0 &&
      el("p", {
        class: "text-muted", style: { fontSize: "13px", margin: "0" },
        text: "Nothing yet. A harness submits a plan with POST /workflows.",
      }),
    // Header and rows are one panel, so the columns are a table rather than five things that
    // happen to line up. The trailing spacer stands in for each row's delete control.
    shown.length > 0 &&
      el(
        "div",
        { class: "list-wrap" },
        el(
          "div",
          { class: "list-head" },
          WF_COLUMNS.map((c) =>
            el("button", {
              class: `col-head col-${c.key} ${c.cls}` + (c.key === key ? " on" : ""),
              text: c.label + (c.key === key ? (dir === "desc" ? " ↓" : " ↑") : ""),
              onClick: () =>
                sortWorkflows(c.key, c.key === key ? (dir === "asc" ? "desc" : "asc") : c.dir),
            }),
          ),
          el("span", { class: "row-del-spacer" }),
        ),
        sorted.map((r) => workflowRow(r)),
      ),
    workflows.length > 0 && shown.length === 0 &&
      el("p", {
        class: "text-muted", style: { fontSize: "13px", margin: "var(--space-3) 0 0" },
        text: q
          ? `No ${filter.label.toLowerCase()} workflow matches “${state.wfQuery.trim()}”.`
          : `No ${filter.label.toLowerCase()} workflows.`,
      }),
  );
}

/** The key a checkpoint's half-typed answers are held under, so the 15s poll does not wipe
    them: state, like everything else this UI draws from. */
const cpKey = (runId, path) => `${runId}:${path.join("/")}`;

function cpDraftFor(runId, path) {
  return state.cpDrafts[cpKey(runId, path)] || { response: {}, note: "", error: null };
}

function setCpDraft(runId, path, patch) {
  const key = cpKey(runId, path);
  setState({ cpDrafts: { ...state.cpDrafts, [key]: { ...cpDraftFor(runId, path), ...patch } } });
}

async function decideCheckpoint(runId, path, decision) {
  const draft = cpDraftFor(runId, path);
  try {
    await resolveCheckpoint(runId, path, {
      decision,
      response: draft.response,
      note: draft.note.trim() || null,
      decided_by: "human",
    });
    // Only on success: a refused answer has to stay on screen to be corrected.
    const rest = { ...state.cpDrafts };
    delete rest[cpKey(runId, path)];
    setState({ cpDrafts: rest });
    await refresh();
  } catch (err) {
    setCpDraft(runId, path, { error: err instanceof ApiError ? err.message : String(err) });
  }
}

/** One checkpoint the run is stopped at: what it asks, what it wants written down, and the
    two buttons. Rejecting fails the step, which skips what depended on it — said plainly,
    because it is not obvious and it is not undoable. */
function checkpointCard({ run, path, stepId, step, state: stepState }) {
  const draft = cpDraftFor(run.run_id, path);
  const fields = (step && step.fields) || [];
  const inst = path.length > 1 ? ` · ${path.slice(1).join(" / ")}` : "";
  // Approving is only offered once the plan is here. Without it we do not know what is
  // being asked, and an empty form is not consent — it just posts nothing and gets refused.
  const answered =
    !!step && fields.every((f) => f.required === false || (draft.response[f.name] || "").trim());
  return el(
    "section",
    { class: "card" },
    el("span", { class: "card-kicker", text: `${titleOf(run)} · checkpoint${inst}` }),
    el(
      "p",
      { style: { margin: "0", fontSize: "14px", whiteSpace: "pre-line" } },
      inline(step ? step.goal : `Waiting on a decision at ${stepId}.`),
    ),
    el("span", {
      class: "mono", style: { fontSize: "11px", color: "var(--color-neutral-500)" },
      text: `${stepId} · ${run.run_id}` + (stepState.started_at ? ` · ${relAgo(stepState.started_at)}` : ""),
    }),
    stepState.summary &&
      el("p", { class: "text-muted", style: { margin: "0", fontSize: "13px" }, text: stepState.summary }),
    fields.length > 0 &&
      el(
        "div",
        { class: "cp-fields" },
        fields.map((f) =>
          el(
            "label",
            { class: "cp-field" },
            el("span", {
              class: "cp-label",
              text: (f.label || f.name) + (f.required === false ? " (optional)" : ""),
            }),
            el("input", {
              id: `cp-${cpKey(run.run_id, path)}-${f.name}`,
              class: "field-search", type: "text",
              placeholder: f.hint || "", value: draft.response[f.name] || "",
              onInput: (e) =>
                setCpDraft(run.run_id, path, {
                  response: { ...draft.response, [f.name]: e.target.value },
                }),
            }),
          ),
        ),
      ),
    el("input", {
      id: `cp-${cpKey(run.run_id, path)}-note`,
      class: "field-search", type: "text", style: { marginTop: "var(--space-1)" },
      placeholder: "Note — required if you reject", value: draft.note,
      onInput: (e) => setCpDraft(run.run_id, path, { note: e.target.value }),
    }),
    draft.error &&
      el("span", { style: { fontSize: "12px", color: BAD }, text: draft.error }),
    el(
      "div",
      { style: { display: "flex", alignItems: "center", gap: "var(--space-2)", marginTop: "var(--space-2)" } },
      el("button", {
        class: "btn btn-primary btn-sm", text: "Approve", disabled: answered ? null : "",
        onClick: () => decideCheckpoint(run.run_id, path, "approved"),
      }),
      el("button", {
        class: "btn btn-secondary btn-sm", text: "Reject",
        onClick: () => decideCheckpoint(run.run_id, path, "rejected"),
      }),
      el("span", {
        class: "text-muted", style: { fontSize: "11px", marginLeft: "auto" },
        text: "Rejecting fails this step and skips what depended on it.",
      }),
    ),
  );
}

function approvalsScreen() {
  const waiting = allWaiting();
  return el(
    "main",
    { class: "narrow", "data-screen-label": "Approvals inbox" },
    el(
      "header",
      { class: "screen-head" },
      el("h4", { text: "Approvals" }),
      el("span", {
        class: "text-muted", style: { fontSize: "12px" },
        text: `${waiting.length} awaiting a decision`,
      }),
    ),
    waiting.length === 0 &&
      el("p", {
        class: "text-muted", style: { fontSize: "13px", margin: "0" },
        text: "Nothing awaiting a decision.",
      }),
    waiting.filter((w) => w.kind === "checkpoint").map(checkpointCard),
    waiting.filter((w) => w.kind === "amendment").map(({ run, amendment }) =>
      el(
        "section",
        { class: "card" },
        el("span", {
          class: "card-kicker",
          text: `${titleOf(run)} · ${kindLabel(amendment)} · ${amendment.proposed_by}`,
        }),
        el("p", { style: { margin: "0", fontSize: "14px" }, text: amendment.reason }),
        el("span", {
          class: "mono", style: { fontSize: "11px", color: "var(--color-neutral-500)" },
          text: `${amendment.amendment_id} · ${run.run_id} · ${relAgo(amendment.created_at)}`,
        }),
        // The inbox lists what each amendment proposes; "View in run" opens the run, where
        // the definition is loaded and the same rows gain their before/after side.
        el(
          "div",
          { style: { display: "flex", flexDirection: "column", gap: "var(--space-1)" } },
          opRows(amendment, null).map(opRow),
        ),
        el(
          "div",
          { style: { display: "flex", alignItems: "center", gap: "var(--space-2)", marginTop: "var(--space-2)" } },
          el("button", { class: "btn btn-primary btn-sm", text: "Approve…", onClick: () => openDialog(amendment, true) }),
          el("button", { class: "btn btn-secondary btn-sm", text: "Reject…", onClick: () => openDialog(amendment, false) }),
          el("button", {
            class: "btn btn-ghost", style: { fontSize: "12px", marginLeft: "auto" },
            text: "View in workflow →",
            onClick: async () => {
              openWorkflow(run.workflow_id);
              setState({ selected: `am:${amendment.amendment_id}` });
            },
          }),
        ),
      ),
    ),
  );
}

// ── run detail: layout ───────────────────────────────────────────────────────────────────

/** Lay the plan's top-level steps out in dependency layers, including the ghost steps a
    pending amendment proposes to insert. */
function layout(topSteps, ghosts, rewires, width, heightFor) {
  const all = [...topSteps, ...ghosts];
  const byId = new Map(all.map((s) => [s.id, s]));
  const depth = {};

  const depthOf = (step) => {
    if (depth[step.id] != null) return depth[step.id];
    depth[step.id] = 0; // cycle guard
    const deps = (step.depends_on || []).map((id) => byId.get(id)).filter(Boolean);
    depth[step.id] = deps.length ? Math.max(...deps.map(depthOf)) + 1 : 0;
    return depth[step.id];
  };
  all.forEach(depthOf);

  // An inserted step's placement lives in the operation (insert_after/insert_before +
  // target), not necessarily in its own depends_on — which may be empty and would
  // otherwise strand the ghost on the top row.
  for (const ghost of ghosts) {
    const anchor = depth[ghost.anchorId];
    if (anchor == null) continue;
    const placed = ghost.anchorBefore ? anchor : anchor + 1;
    if (!(ghost.depends_on || []).some((id) => byId.has(id))) depth[ghost.id] = placed;
    else depth[ghost.id] = Math.max(depth[ghost.id], placed);
  }
  // A step whose proposed dependencies include a ghost sinks below it.
  const ghostIds = new Set(ghosts.map((g) => g.id));
  for (const rewire of rewires) {
    for (const depId of rewire.deps) {
      if (ghostIds.has(depId) && depth[rewire.stepId] != null) {
        depth[rewire.stepId] = Math.max(depth[rewire.stepId], depth[depId] + 1);
      }
    }
  }

  const layers = [];
  for (const step of all) (layers[depth[step.id]] ||= []).push(step);

  const widest = Math.max(1, ...layers.map((l) => l.length));
  // 170 is the floor at which a node still reads. Past that the plan does not get narrower,
  // it gets wider than the window — twelve steps side by side need 2400px however big the
  // window is — so the layout reports what it actually needed and the viewport scrolls.
  // Scaling to fit was the alternative and is worse: a twelve-wide fan at 0.37 is a
  // diagram of a plan rather than a plan you can read.
  const nodeW = Math.max(170, Math.min(250, (width - 32 - (widest - 1) * GAP) / widest));
  const needed = 32 + widest * nodeW + (widest - 1) * GAP;
  const planeW = Math.max(width, needed);

  // Node heights vary: a construct grows to hold the steps in its body, so a layer advances
  // by its tallest member rather than by a constant.
  const pos = {};
  let y = 14;
  let bottom = 0;
  for (const layer of layers) {
    let tallest = NODE_H;
    layer.forEach((step, i) => {
      const h = heightFor(step);
      tallest = Math.max(tallest, h);
      pos[step.id] = { x: 16 + i * (nodeW + GAP), y, h };
    });
    bottom = y + tallest;
    y += tallest + DROP;
  }
  // A lone node in a layer centres under its dependencies (or over its dependents).
  for (const layer of layers) {
    if (layer.length !== 1) continue;
    const step = layer[0];
    const deps = (step.depends_on || []).filter((id) => pos[id]);
    if (deps.length) {
      pos[step.id].x = deps.reduce((a, id) => a + pos[id].x, 0) / deps.length;
    } else {
      const kids = all.filter((k) => (k.depends_on || []).includes(step.id));
      if (kids.length) pos[step.id].x = kids.reduce((a, k) => a + pos[k.id].x, 0) / kids.length;
    }
    // Against the plane, not the window: on a wide plan the two differ, and clamping to
    // the window would drag a centred node back on top of the layer above it.
    pos[step.id].x = Math.max(16, Math.min(pos[step.id].x, planeW - nodeW - 16));
  }

  return { all, pos, nodeW, height: bottom + 24, ghostIds, planeW };
}

const MARKERS = { ok: OK, warn: WARN, bad: BAD, dim: DIM, acc: ACC };

function edgeDefs() {
  return svgEl(
    "defs",
    {},
    Object.entries(MARKERS).map(([name, color]) =>
      svgEl(
        "marker",
        { id: `arr-${name}`, markerWidth: "7", markerHeight: "7", refX: "5.5", refY: "3.5", orient: "auto" },
        svgEl("path", { d: "M0 0 L6 3.5 L0 7 z", style: { fill: color } }),
      ),
    ),
  );
}

// ── run detail: inspector panels ─────────────────────────────────────────────────────────

function stepPanel(step, stepState, def) {
  const arts = stepArtifacts(stepState);
  const instances = stepState.instances || [];
  const body = bodyStepsOf(step, def);
  const meta =
    `${step.id} · ${step.harness}` +
    (step.type !== "task"
      ? ` · ${step.type}${stepState.instances_closed === false ? " (more instances may come)" : ""}`
      : "") +
    (stepState.started_at ? ` · started ${relAgo(stepState.started_at)}` : "");

  const summary =
    stepState.status === "skipped"
      ? stepState.skip_cause === "dependency"
        ? "skipped after an upstream failure"
        : "skipped: removed"
      : stepState.summary;

  // A checkpoint reads as what it asks while it waits, and as what was said once it is
  // decided. Both go through the `body` rows the constructs already use, so the panel gains
  // no fourth way of laying out a list of labelled lines.
  const outcome = step.type === "checkpoint" ? stepState.checkpoint : null;
  const asks = step.type === "checkpoint" && !outcome
    ? (step.fields || []).map((f) => ({ id: f.name, goal: f.label || f.hint || "free text" }))
    : [];
  const said = outcome
    ? Object.entries(outcome.response || {}).map(([name, value]) => ({ id: name, goal: value }))
    : [];

  return {
    // Which node this panel is about. The review-note thread hangs off it, the way a
    // comment thread hangs off the post it is under.
    stepId: step.id,
    kicker:
      step.type === "checkpoint"
        ? `Checkpoint · ${outcome ? outcome.decision : stepState.status}`
        : `Step · ${stepState.status}`,
    title: step.goal,
    criteria: step.criteria || [],
    criteriaMet: stepState.criteria_met || {},
    metaLine:
      outcome
        ? `${step.id} · ${outcome.decision} by ${outcome.decided_by} · ${relAgo(outcome.decided_at)}` +
          (outcome.via === "mcp" ? " · relayed by a harness" : "")
        : meta,
    warn:
      stepState.status === "blocked"
        ? "Waiting on you. This run does not move until it is decided — answer it in Approvals."
        : null,
    summary,
    summaryColor: stepState.status === "failed" ? BAD : "var(--color-neutral-600)",
    instances: instances.map((instance) => {
      const im = stepMeta(instance.status);
      const failedBody = Object.values(instance.step_states || {}).find((b) => b.status === "failed");
      const which = instanceParamLabel(step, instance);
      return {
        // The declared parameters come first when there are any: "Branch 3 · sparsify" is
        // what a person is looking for, and "Branch 3" alone is what made wf_ablate's three
        // variants indistinguishable.
        label: `${instance.kind === "iteration" ? "Iteration" : "Branch"} ${instance.index + 1}`
          + (which ? ` · ${which}` : ""),
        summary: (instance.status === "failed" && failedBody ? failedBody.summary : instance.summary) || "",
        summaryColor: instance.status === "failed" ? BAD : "var(--color-neutral-500)",
        color: im.color, pulse: pulseOf(im),
        metadata: instance.metadata,
        // Only the body steps that actually name a parameter: rendering the rest would
        // repeat the shared body under every branch for no gain.
        filled: body
          .filter((b) => hasParams(b.goal) || (b.criteria || []).some((c) => hasParams(c.text)))
          .map((b) => ({
            id: b.id,
            goal: fillParams(b.goal, instance.metadata),
            criteria: (b.criteria || []).map((c) => fillParams(c.text, instance.metadata)),
          })),
      };
    }),
    // Whatever the harness attached to this step. Merged across updates rather than
    // replaced, so what is here is everything it has ever said, not just the last thing.
    metadata: stepState.metadata,
    // The construct's body: the steps every instance runs. Static, so it is readable before
    // the first instance exists — which is when a person is deciding whether to approve it.
    exitLabel: step.exit_when ? `Exits when: ${step.exit_when}` : null,
    // What every instance must say about itself, readable before the first one exists.
    paramsLabel: (step.instance_params || []).length
      ? `Each ${instanceKind(step)} supplies`
      : null,
    params: (step.instance_params || []).map((p) => ({
      id: p.name,
      goal: (p.description || "") + (p.required ? "" : " (optional)"),
    })),
    bodyLabel: asks.length
      ? "Asks you for"
      : said.length
        ? "You said"
        : body.length
          ? `Each ${instanceKind(step)} runs`
          : null,
    body: asks.length ? asks : said.length ? said : body,
    artsLabel: arts.length ? `Artifacts (${arts.length})` : null,
    arts,
  };
}

function amendmentPanel(amendment, isPending, def, stepStates) {
  const arts = runArtifacts(def, stepStates);
  return {
    kicker:
      (isPending ? "Decision needed · " : `Decided · ${amendment.status} · `) +
      `${kindLabel(amendment)} · ${amendment.proposed_by}`,
    title: amendment.reason,
    metaLine:
      `${amendment.amendment_id} · ${relAgo(amendment.created_at)}` +
      (amendment.decided_by ? ` · by ${amendment.decided_by}` : "") +
      (amendment.decision_reason ? ` — ${quote(amendment.decision_reason)}` : ""),
    warn:
      isPending && amendment.kind === "history_edit"
        ? "History edit: approving re-runs a step that already finished."
        : null,
    opsLabel: "Proposed plan changes",
    ops: opRows(amendment, def),
    instances: [],
    artsLabel: arts.length ? `Review artifacts (${arts.length})` : null,
    arts,
    approve: isPending ? () => openDialog(amendment, true) : null,
    reject: isPending ? () => openDialog(amendment, false) : null,
  };
}

function overviewPanel(run, detail, topSteps) {
  const arts = runArtifacts(detail.def, detail.state.step_states);
  const done = topSteps.filter(
    (s) => (detail.state.step_states[s.id] || {}).status === "completed",
  ).length;
  return {
    kicker: "Run overview",
    title: detail.def.title,
    metaLine: `${run.run_id} · ${done} of ${topSteps.length} steps completed`,
    summary: "Select a step or a proposed change to inspect it.",
    summaryColor: "var(--color-neutral-500)",
    instances: [],
    // What the harness said when it registered the run: what triggered it, which commit.
    metadata: run.metadata,
    artsLabel: arts.length ? `All artifacts (${arts.length})` : null,
    arts,
  };
}

function inspector(panel) {
  const aside = el(
    "aside",
    {
      class: "inspector", "data-screen-label": "Inspector",
      style: { width: `${state.inspectorWidth}px` },
    },
    el(
      "section",
      { class: "card" },
      el(
        "span",
        { style: { display: "flex", alignItems: "baseline", gap: "var(--space-2)" } },
        el("span", { class: "card-kicker", style: { flex: "1" }, text: panel.kicker }),
        panel.close &&
          el("button", { class: "close-x", text: "✕", title: "Close", onClick: panel.close }),
      ),
      // `inline()`, not plain text: a goal is short prose ("state the work"), not markup,
      // but the same bold/code/math a summary or a comment may use reads just as well here
      // — and inline() alone does not turn a literal newline into a visible one, hence
      // `pre-line` staying on top of it. Block markup (headings, lists) stays out on
      // purpose: a goal is one to three lines, not a document.
      el(
        "p",
        { style: { margin: "0", fontSize: "13px", lineHeight: "1.45", whiteSpace: "pre-line" } },
        inline(panel.title),
      ),
      el("span", {
        class: "mono", style: { fontSize: "11px", color: "var(--color-neutral-500)" },
        text: panel.metaLine,
      }),
      panel.warn && el("div", { class: "accent-note", text: panel.warn }),
      panel.summary &&
        el(
          "span",
          { class: "md-inline", style: { fontSize: "12px", color: panel.summaryColor } },
          inline(panel.summary),
        ),
      criteriaBlock(panel.criteria, panel.criteriaMet),
      panel.opsLabel &&
        el("span", { class: "section-label", style: { marginTop: "var(--space-1)" }, text: panel.opsLabel }),
      (panel.ops || []).map(opRow),
      panel.exitLabel && el("div", { class: "accent-note", text: panel.exitLabel }),
      panel.bodyLabel &&
        el("span", { class: "section-label", style: { marginTop: "var(--space-1)" }, text: panel.bodyLabel }),
      (panel.body || []).map((step) =>
        el(
          "span",
          { class: "inst-row" },
          el("span", { class: "label mono", style: { fontSize: "11px" }, text: step.id }),
          el("span", { class: "summary", text: step.goal }),
        ),
      ),
      panel.paramsLabel &&
        el("span", { class: "section-label", style: { marginTop: "var(--space-1)" }, text: panel.paramsLabel }),
      (panel.params || []).map((p) =>
        el(
          "span",
          { class: "inst-row" },
          el("span", { class: "label mono", style: { fontSize: "11px" }, text: p.id }),
          el("span", { class: "summary", text: p.goal }),
        ),
      ),
      panel.instances.map((instance) =>
        el(
          "div",
          { class: "inst-block" },
          el(
            "span",
            { class: "inst-row" },
            dot(instance.color, instance.pulse, "6px"),
            el("span", { class: "label", text: instance.label }),
            el("span", { class: "summary", style: { color: instance.summaryColor }, text: instance.summary }),
            metadataBlock(instance.metadata, "compact", `${instance.label} · metadata`),
          ),
          // The body as this instance actually reads it, with its own values in place —
          // the whole point of declaring the parameters. Only shown for the steps that name
          // one, so a construct whose body is generic looks exactly as it did.
          (instance.filled || []).map((b) =>
            el(
              "div",
              { class: "inst-filled" },
              el("span", { class: "criterion-what", text: b.goal }),
              b.criteria.map((text) =>
                el("span", { class: "criterion-evidence", text: `— ${text}` }),
              ),
            ),
          ),
        ),
      ),
      metadataBlock(panel.metadata),
      panel.approve &&
        el(
          "div",
          { style: { display: "flex", gap: "var(--space-2)", marginTop: "var(--space-1)" } },
          el("button", { class: "btn btn-primary btn-sm", text: "Approve…", onClick: panel.approve }),
          el("button", { class: "btn btn-secondary btn-sm", text: "Reject…", onClick: panel.reject }),
        ),
      // The thread on this node, under everything the panel says about it. `noteWorkflow`
      // is set only by the workflow screen, so the run and template screens — which share
      // this inspector — render nothing here.
      panel.noteWorkflow && noteBlock(panel.noteWorkflow, panel.stepId || null),
    ),
    panel.artsLabel &&
      el("span", { class: "section-label", style: { padding: "0 var(--space-1)" }, text: panel.artsLabel }),
    panel.artsLabel && rootRow(panel.arts || []),
    (panel.arts || []).map(({ artifact, label }) => artifactCard(artifact, label)),
  );
  inspectorNode = aside;
  return aside;
}

// ── templates ────────────────────────────────────────────────────────────────────────────

/** Write a template out as a file, so a project can keep its own plan shapes beside its code.

    A download from the browser, not a write from the server: Chief reads nothing off disk
    and writes nothing to it, which is what keeps it a tracker rather than a file server
    (CONTRACT-NOTES.md #29). The browser is already the thing with a filesystem the person
    chose.

    What comes out is exactly what `POST /templates` takes — the same field names, the same
    shape — so the file is a request body at rest. Commit it, and registering it on another
    machine is posting it back. The id travels with it, which is what makes re-registering
    the same file idempotent rather than a second copy under a new name. */
function exportTemplate(template) {
  const body = {
    template_id: template.template_id,
    title: template.title,
    description: template.description,
    parameters: template.parameters,
    steps: template.steps,
    project: template.project,
  };
  const slug = (template.title || template.template_id)
    .toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "").slice(0, 60);
  const url = URL.createObjectURL(
    new Blob([JSON.stringify(body, null, 2) + "\n"], { type: "application/json" }),
  );
  const link = el("a", { href: url, download: `${slug || "template"}.json` });
  document.body.appendChild(link);
  link.click();
  link.remove();
  // Freed on the next tick rather than immediately: revoking it before the click has been
  // dispatched cancels the download in some browsers.
  setTimeout(() => URL.revokeObjectURL(url), 0);
}

/** A template is the plan you keep; a workflow is the plan you are running this time. It
    draws through the same renderer as everything else — a template *is* a plan, it just has
    placeholders where a workflow has values. */
function templateRow(template) {
  const archived = template.status === "archived";
  return el(
    "button",
    { class: "run-row", onClick: () => openTemplate(template.template_id) },
    dot(archived ? "var(--color-neutral-400)" : OK, false),
    el(
      "span",
      { class: "title" },
      el("span", { text: template.title }),
      el("span", { class: "id text-muted", text: template.template_id }),
    ),
    el("span", {
      class: "status text-muted",
      text: template.parameters.length
        ? template.parameters.map((p) => p.name).join(", ")
        : "no parameters",
    }),
    el("span", { class: "when", text: `${template.steps.length} steps` }),
  );
}

function templatesScreen() {
  const templates = state.templates;
  if (templates === undefined)
    return el("main", { class: "narrow" }, el("p", { class: "text-muted", text: "Loading…" }));
  if (templates === null) {
    return el(
      "main",
      { class: "narrow", "data-screen-label": "Templates" },
      el("header", { class: "screen-head" }, el("h4", { text: "Templates" })),
      el("p", {
        class: "text-muted", style: { fontSize: "13px", margin: "0" },
        text:
          "This Chief has no /templates endpoint — it is running a build from before " +
          "templates existed. Restart it to pick them up.",
      }),
    );
  }
  const active = templates.filter((t) => t.status === "active");
  const archived = templates.filter((t) => t.status === "archived");

  return el(
    "main",
    { class: "narrow", "data-screen-label": "Templates" },
    el(
      "header",
      { class: "screen-head" },
      el("h4", { text: "Templates" }),
      el("span", {
        class: "text-muted", style: { fontSize: "12px" },
        text: `${active.length} available`,
      }),
    ),
    templates.length === 0 &&
      el("p", {
        class: "text-muted", style: { fontSize: "13px", margin: "0" },
        text: "No templates yet. Save one from a workflow that worked, or POST /templates.",
      }),
    el("div", { style: { display: "flex", flexDirection: "column" } }, active.map(templateRow)),
    archived.length > 0 &&
      el(
        "section",
        { style: { display: "flex", flexDirection: "column" } },
        el("h5", {
          style: {
            margin: "var(--space-4) 0 var(--space-1)", fontSize: "11px", textTransform: "uppercase",
            letterSpacing: "0.06em", color: "var(--color-neutral-500)",
          },
          text: `Archived · ${archived.length}`,
        }),
        ...archived.map(templateRow),
      ),
  );
}

function templateDetailScreen() {
  const template = (state.templates || []).find((t) => t.template_id === state.templateId);
  if (!template) {
    return el("main", { class: "wide graph" }, el("p", { class: "text-muted", text: "Loading…" }));
  }
  // Placeholders live in text, never in ids or edges, so the unrendered plan has exactly the
  // shape every workflow made from it will have.
  const { viewport, panel, topSteps } = planGraph({ def: template });
  const active = template.status === "active";

  return el(
    "main",
    { class: "wide graph", "data-screen-label": "Template detail" },
    el(
      "div",
      {},
      el("button", {
        class: "btn btn-ghost",
        style: { fontSize: "13px", marginLeft: "calc(-1 * var(--space-1))" },
        text: "← Templates", onClick: () => go("templates"),
      }),
      el(
        "div",
        { class: "screen-head", style: { marginTop: "var(--space-3)" } },
        el("h4", { text: template.title }),
        !active && el("span", { class: "text-muted", style: { fontSize: "13px" }, text: "archived" }),
      ),
      el("p", {
        class: "text-muted mono",
        style: { fontSize: "11px", margin: "var(--space-2) 0 0" },
        text:
          `${template.template_id} · ${template.steps.length} steps` +
          (template.derived_from_workflow_id
            ? ` · from ${template.derived_from_workflow_id}`
            : ""),
      }),
      template.description &&
        el("p", { style: { fontSize: "13px", margin: "var(--space-2) 0 0" }, text: template.description }),
      template.parameters.length > 0 &&
        el(
          "div",
          { style: { marginTop: "var(--space-3)" } },
          el("span", { class: "section-label", text: "Parameters" }),
          el(
            "div",
            { style: { display: "flex", flexDirection: "column", gap: "var(--space-1)" } },
            template.parameters.map((p) =>
              el(
                "div",
                { style: { display: "flex", gap: "var(--space-2)", alignItems: "baseline", fontSize: "13px" } },
                el("span", { class: "mono", style: { fontSize: "12px" }, text: p.name }),
                el("span", {
                  class: "text-muted", style: { flex: "1" },
                  text: p.description || "",
                }),
                el("span", {
                  class: "text-muted mono", style: { fontSize: "11px" },
                  text: p.default !== null && p.default !== undefined ? `default ${p.default}` : "required",
                }),
              ),
            ),
          ),
        ),
      active &&
        el(
          "div",
          { style: { display: "flex", gap: "var(--space-2)", marginTop: "var(--space-3)" } },
          el("button", {
            class: "btn btn-primary btn-sm", text: "Use this template…",
            onClick: () => openTemplateDialog(template),
          }),
          el("button", {
            class: "btn btn-ghost btn-sm", style: { fontSize: "12px" },
            text: "Export to a file",
            title: "Downloads the template as JSON — the same body POST /templates takes, "
                 + "so it can be committed alongside the project and registered anywhere",
            onClick: () => exportTemplate(template),
          }),
          el("button", {
            class: "btn btn-secondary btn-sm", text: "Archive…",
            onClick: () => openTemplateArchiveDialog(template),
          }),
        ),
    ),
    splitView(viewport, panel || templatePanel(template, topSteps)),
  );
}

function templatePanel(template, topSteps) {
  const harnesses = [...new Set(template.steps.map((s) => s.harness))];
  return {
    kicker: "Template",
    title: template.title,
    metaLine: `${topSteps.length} top-level of ${template.steps.length} steps · ${harnesses.join(", ")}`,
    summary:
      "Placeholders are filled in when a workflow is made from this. Select a step to read it.",
    summaryColor: "var(--color-neutral-500)",
    instances: [],
  };
}

// ── run detail ───────────────────────────────────────────────────────────────────────────

/** The plan, drawn.

    Takes a definition and whatever is known about executing it, so the same picture serves a
    run — where step states colour the nodes and pending amendments add ghosts — and a
    workflow that has never run, where there are neither and every node reads as pending.
    Returns the pieces rather than a screen: the two callers frame it differently. */
function planGraph({ def, stepStates = {}, pending = [], past = [] }) {
  // Body steps are drawn inside their construct's node, not as top-level nodes.
  const topSteps = def.steps.filter((s) => !def.steps.some((p) => (p.body || []).includes(s.id)));
  const selected = state.selected ?? (pending.length ? `am:${pending[0].amendment_id}` : null);

  // Ghost nodes for proposed insertions, and the dependency rewires that accompany them.
  const ghosts = [];
  const rewires = [];
  for (const amendment of pending) {
    for (const op of amendment.operations) {
      if (op.op === "insert_after" || op.op === "insert_before") {
        ghosts.push({
          ...op.step, ghost: true, amendment,
          anchorId: op.target_step_id, anchorBefore: op.op === "insert_before",
        });
      } else if (op.op === "update_step" && op.step) {
        rewires.push({ stepId: op.target_step_id, deps: op.step.depends_on || [], amendment });
      }
    }
  }

  // The cycle *is* the plan, so a construct stays flattened while it runs — execution
  // colours the body nodes instead of collapsing them. A body step's state lives inside the
  // construct's instances, so overlay the newest iteration's states onto the flat graph;
  // earlier iterations are history, carried by the gate's cluster and the inspector.
  const plannedBody = (step) => (step.ghost ? [] : bodyStepsOf(step, def));
  const effective = {};
  const overlay = (states) => {
    for (const [id, st] of Object.entries(states || {})) {
      effective[id] = st;
      const instances = st.instances || [];
      if (instances.length) {
        const latest = instances.reduce((a, b) => (b.index > a.index ? b : a));
        overlay(latest.step_states);
      }
    }
  };
  overlay(stepStates);
  const stateOf = (step) =>
    step.ghost ? { status: "pending" } : effective[step.id] || { status: "pending" };
  // What every depth of the recursive renderer needs.
  const ctx = { plannedBody, stateOf, pending, selected };

  const width = Math.max(state.graphWidth, 480);
  const scale = Math.min(1, state.graphWidth / width);
  // Constructs flatten into the display graph: body steps inline, the construct as a gate.
  const display = flattenConstructs(topSteps, ctx);
  const heightFor = (step) => (step.gate ? GATE_H : NODE_H);
  const { all, pos, nodeW, height, ghostIds, planeW } = layout(display, ghosts, rewires, width, heightFor);
  // Room for the horizontal scrollbar, but only when there is one to make room for.
  const scrolls = planeW > width;

  // ── edges ──
  const paths = [];
  const addEdge = (fromId, toId, isGhost) => {
    const from = pos[fromId];
    const to = pos[toId];
    if (!from || !to) return;
    const cx = from.x + nodeW / 2;
    const yb = from.y + (from.h ?? NODE_H);
    const tx = to.x + nodeW / 2;
    let stroke = ACC;
    let opacity = 0.75;
    let marker = "acc";
    if (!isGhost) {
      const source = all.find((s) => s.id === fromId);
      const sourceState = stateOf(source);
      const outcome = outcomeOf(sourceState);
      const color = outcomeColor(outcome);
      const running = sourceState.status === "running";
      stroke = color || (running ? ACC : DIM);
      opacity = color || running ? 0.85 : 0.35;
      marker = outcome === "full" ? "ok" : outcome === "partial" ? "warn" : outcome === "none" ? "bad" : running ? "acc" : "dim";
    }
    paths.push(
      svgEl("path", {
        d: `M ${cx} ${yb} C ${cx} ${yb + 26}, ${tx} ${to.y - 26}, ${tx} ${to.y - 2}`,
        "stroke-width": "1.4",
        "stroke-dasharray": isGhost ? "4 4" : "none",
        "marker-end": `url(#arr-${marker})`,
        style: { fill: "none", stroke, opacity: String(opacity) },
      }),
    );
  };
  for (const step of all) {
    for (const depId of step.depends_on || []) {
      if (pos[depId]) addEdge(depId, step.id, step.ghost || ghostIds.has(depId));
    }
  }
  for (const ghost of ghosts) {
    // The anchor edge for an insertion whose own depends_on does not name it.
    if (!(ghost.depends_on || []).includes(ghost.anchorId) && pos[ghost.anchorId]) {
      if (ghost.anchorBefore) addEdge(ghost.id, ghost.anchorId, true);
      else addEdge(ghost.anchorId, ghost.id, true);
    }
  }
  for (const rewire of rewires) {
    for (const depId of rewire.deps) if (ghostIds.has(depId)) addEdge(depId, rewire.stepId, true);
  }
  // A loop's return edges: from the gate back up to the body's entries, dashed, along the
  // right-hand side — "this comes back around" drawn rather than described. The gate is a
  // decision with exactly two arrows, and when the loop declares `exit_when` they are
  // labelled: ✓ the condition (continue past the loop), ✗ otherwise (another iteration).
  for (const gate of all.filter((s) => s.gate && s.type === "loop")) {
    const from = pos[gate.id];
    if (!from) continue;
    const fy = from.y + (from.h ?? GATE_H) / 2;
    for (const entryId of gate.entryIds || []) {
      const to = pos[entryId];
      if (!to) continue;
      const rail = Math.min(width - 6, Math.max(from.x, to.x) + nodeW + 20);
      const ty = to.y + NODE_H / 2;
      paths.push(
        svgEl("path", {
          d: `M ${from.x + nodeW} ${fy} C ${rail} ${fy}, ${rail} ${ty}, ${to.x + nodeW + 2} ${ty}`,
          "stroke-width": "1.3", "stroke-dasharray": "4 4", "marker-end": "url(#arr-acc)",
          style: { fill: "none", stroke: ACC, opacity: "0.55" },
        }),
      );
    }
    if (gate.exit_when) {
      paths.push(
        svgEl("text", {
          class: "gate-edge-label",
          x: String(from.x + nodeW + 8), y: String(fy - 6),
          style: { fill: ACC, opacity: "0.8" },
          text: `✗ otherwise, another iteration`,
        }),
        svgEl("text", {
          class: "gate-edge-label",
          x: String(from.x + nodeW / 2 + 10), y: String(from.y + (from.h ?? GATE_H) + 18),
          style: { fill: "var(--ok)", opacity: "0.9" },
          text: `✓ ${gate.exit_when}`,
        }),
      );
    }
  }

  // ── nodes ──
  const nodes = all.map((step) => {
    if (step.gate) return gateNode(step, pos[step.id], nodeW, ctx);
    const stepState = stateOf(step);
    const sm = stepMeta(stepState.status);
    const isSelected =
      selected === `step:${step.id}` ||
      (step.ghost && selected === `am:${step.amendment.amendment_id}`);
    const pendingHere =
      !step.ghost &&
      pending.find((a) => a.operations.some((o) => o.target_step_id === step.id));
    const pastHere = !step.ghost && past.find((a) => a.operations.some((o) => o.target_step_id === step.id));
    const outcome = outcomeOf(stepState);
    const artCount = stepArtifacts(stepState).length;

    const classes = ["node"];
    if (step.type === "checkpoint") classes.push("checkpoint");
    if (step.type === "workflow_ref") classes.push("workflow-ref");
    if (step.ghost) classes.push("ghost");
    else if (isSelected) classes.push("sel");
    else if (pendingHere) classes.push("pend");
    else if (stepState.status === "blocked") classes.push("waiting");
    else if (stepState.status === "failed") classes.push("fail");

    const showSummary =
      !step.ghost && ["completed", "failed", "running"].includes(stepState.status) && stepState.summary;

    const node = el(
      "div",
      {
        class: classes.join(" "),
        style: {
          left: `${pos[step.id].x}px`, top: `${pos[step.id].y}px`,
          width: `${nodeW}px`, height: `${pos[step.id].h}px`,
        },
        title: step.goal,
        onClick: () =>
          step.ghost
            ? setState({ selected: `am:${step.amendment.amendment_id}` })
            : setState({ selected: isSelected ? "none" : `step:${step.id}` }),
      },
      el(
        "span",
        { class: "node-head" },
        dot(
          step.ghost ? "var(--color-accent-300)" : outcomeColor(outcome) || sm.color,
          step.ghost ? "none" : pulseOf(sm),
          "7px",
        ),
        el("span", {
          class:
            "node-goal" +
            (step.ghost ? " ghosted" : ["pending", "skipped"].includes(stepState.status) ? " dimmed" : ""),
          text: showSummary ? stepState.summary : step.goal,
        }),
      ),
      artCount > 0 && el("span", { class: "node-arts", text: `⌗ ${artCount}` }),
      // Feedback waiting on this node, so you can see which steps have something to read
      // without opening every one of them. Click goes through to the node as usual — the
      // thread is in the inspector the click opens.
      !step.ghost &&
        openNoteCount(step.id) > 0 &&
        el("span", { class: "node-notes", text: `💬 ${openNoteCount(step.id)}` }),
      // The one node a person can act on from here. It says who it is waiting for, and the
      // inbox is where it gets answered — the graph shows the plan, it is not a form.
      step.type === "checkpoint" &&
        !step.ghost &&
        el("span", {
          class: "node-tag" + (stepState.status === "blocked" ? " waiting" : " quiet"),
          text: stepState.status === "blocked" ? "waiting on you →" : "checkpoint",
          onClick:
            stepState.status === "blocked"
              ? (e) => {
                  e.stopPropagation();
                  go("approvals");
                }
              : null,
        }),
      // A sub-workflow node: what it's waiting on is a child run finishing, not a person,
      // so the tag goes straight to that run rather than to the approvals inbox.
      step.type === "workflow_ref" &&
        !step.ghost &&
        el("span", {
          class: "node-tag" + (stepState.status === "blocked" ? " waiting" : " quiet"),
          text: stepState.status === "blocked" ? "sub-workflow running →" : "sub-workflow",
          onClick: stepState.child_run_id
            ? (e) => {
                e.stopPropagation();
                openRun(stepState.child_run_id);
              }
            : null,
        }),
      step.ghost && el("span", { class: "node-tag ghost", text: "proposed" }),
      !step.ghost &&
        (pendingHere || pastHere) &&
        el("button", {
          class: pendingHere ? "node-tag" : "node-tag past",
          text: pendingHere ? "proposed change" : "amended earlier",
          onClick: (e) => {
            e.stopPropagation();
            setState({
              selected: pendingHere
                ? `am:${pendingHere.amendment_id}`
                : `pam:${pastHere.amendment_id}`,
            });
          },
        }),
    );
    return node;
  });

  // ── inspector ──
  let panel = null;
  if (selected?.startsWith("step:")) {
    // def.steps, not topSteps: a body node inside a construct is selectable too.
    const step = def.steps.find((s) => `step:${s.id}` === selected);
    if (step) panel = stepPanel(step, stateOf(step), def);
  } else if (selected?.startsWith("am:")) {
    const amendment = pending.find((a) => `am:${a.amendment_id}` === selected);
    if (amendment) panel = amendmentPanel(amendment, true, def, stepStates);
  } else if (selected?.startsWith("pam:")) {
    const amendment = past.find((a) => `pam:${a.amendment_id}` === selected);
    if (amendment) panel = amendmentPanel(amendment, false, def, stepStates);
  }
  if (panel) panel.close = () => setState({ selected: "none" });

  const viewport = el(
    "div",
    {
      class: scrolls ? "graph-viewport scrolls" : "graph-viewport",
      style: { height: `${Math.ceil(height * scale) + (scrolls ? 14 : 0)}px` },
      // Says so out loud, because a plan running off the right edge otherwise reads as a
      // plan with fewer steps in it than it has.
      title: scrolls ? "Wider than the window — scroll sideways for the rest" : null,
    },
    el(
      "div",
      {
        class: "graph-plane",
        style: {
          width: `${planeW}px`, height: `${height}px`,
          transform: scale < 1 ? `scale(${scale})` : "none",
        },
      },
      svgEl("svg", { width: String(planeW), height: String(height) }, edgeDefs(), paths),
      nodes,
    ),
  );
  measured = viewport;

  return { viewport, panel, topSteps };
}

/** A workflow, at whatever point in its life it has reached.

    Draft, ready, running or finished, it is the same screen showing the same plan — with
    execution state on it once there is any. That is the whole merge: there was never a second
    kind of object to look at, only a second document behind the same one. */
function workflowDetailScreen() {
  const workflow = (state.workflows || []).find((w) => w.workflow_id === state.workflowId);
  if (!workflow) {
    return el("main", { class: "wide graph" }, el("p", { class: "text-muted", text: "Loading…" }));
  }
  const runs = executionsOf(workflow, state.runs);
  const life = lifecycleOf(workflow, runs);
  const run = runs[0];
  // Only trust the loaded detail if it belongs to this workflow's execution.
  const detail = state.detail && run && state.detail.runId === run.run_id ? state.detail : null;
  const draft = workflow.status === "draft";

  const amendments = detail ? detail.amendments : [];
  const { viewport, panel, topSteps } = planGraph({
    // The run's own definition once it exists: it carries any amendment already applied.
    def: detail ? detail.def : workflow,
    stepStates: detail ? detail.state.step_states || {} : {},
    pending: pendingOf(amendments),
    past: amendments.filter((a) => a.status !== "pending_approval"),
  });
  const progress = detail ? progressOf(workflow, detail.state) : null;

  return el(
    "main",
    { class: "wide graph", "data-screen-label": "Workflow detail" },
    el(
      "div",
      {},
      el("button", {
        class: "btn btn-ghost",
        style: { fontSize: "13px", marginLeft: "calc(-1 * var(--space-1))" },
        text: "← Workflows", onClick: () => go("workflows"),
      }),
      el(
        "div",
        { class: "screen-head", style: { marginTop: "var(--space-3)" } },
        el("h4", { text: workflow.title }),
        badge(life),
        progress &&
          el("span", {
            class: "text-muted", style: { fontSize: "12px" },
            text: `${progress.done} of ${progress.total} steps`,
          }),
      ),
      el("p", {
        class: "text-muted mono",
        style: { fontSize: "11px", margin: "var(--space-2) 0 0" },
        text:
          `${workflow.workflow_id} · v${workflow.version} · ` +
          `${workflow.source}${workflow.generated_by ? ` by ${workflow.generated_by}` : ""}` +
          (detail && detail.state.applied_amendment_ids.length
            ? ` · +${detail.state.applied_amendment_ids.length} amendment` +
              (detail.state.applied_amendment_ids.length > 1 ? "s" : "")
            : ""),
      }),
      projectLine(workflow),
      decisionNote(workflow),
      workflow.status !== "archived" &&
        el(
          "div",
          { style: { display: "flex", gap: "var(--space-2)", marginTop: "var(--space-3)" } },
          draft &&
            el("button", {
              class: "btn btn-primary btn-sm", text: "Approve…",
              onClick: () => openWorkflowDialog(workflow, "approve"),
            }),
          // The way back to the plan's own thread. Without it, feedback about the plan is
          // reachable only by having nothing selected — which is true when you arrive and
          // false the moment you click a node, so it reads as having disappeared.
          planNoteButton(workflow),
          el("button", {
            class: "btn btn-secondary btn-sm", text: draft ? "Discard…" : "Archive…",
            onClick: () => openWorkflowDialog(workflow, "archive"),
          }),
          el("button", {
            class: "btn btn-secondary btn-danger btn-sm", text: "Delete…",
            onClick: () => openDeleteDialog(workflow),
          }),
          // Keeping a plan that worked. Parameterising it means saying which literals become
          // knobs, which this screen has no way to ask — so the copy is unparameterised and
          // the harness (or the API) adds substitutions. See MCP create_template_from_workflow.
          !draft &&
            el("button", {
              class: "btn btn-ghost", style: { fontSize: "12px" }, text: "Save as template",
              onClick: async () => {
                try {
                  const created = await createTemplateFromWorkflow(workflow.workflow_id, {});
                  await refresh();
                  openTemplate(created.template_id);
                } catch (err) {
                  setState({ error: err instanceof ApiError ? err.message : String(err) });
                }
              },
            }),
        ),
      // Reuse is a template's job, so more than one execution is unusual. When it happens,
      // say so plainly rather than silently showing only the newest.
      runs.length > 1 &&
        el(
          "div",
          { style: { marginTop: "var(--space-3)" } },
          el("span", { class: "section-label", text: `${runs.length} executions` }),
          el(
            "div",
            { style: { display: "flex", flexDirection: "column" } },
            runs.map((r, i) => {
              const meta = RUN_META[r.status] || RUN_META.running;
              return el(
                "button",
                { class: "run-row", onClick: () => openRun(r.run_id) },
                el(
                  "span",
                  { class: "title" },
                  el("span", { text: i === 0 ? "latest" : `execution ${runs.length - i}` }),
                  el("span", { class: "id text-muted", text: r.run_id }),
                ),
                el("span", { class: "status" }, badge(meta)),
                el("span", { class: "when", text: rel(r.updated_at) }),
              );
            }),
          ),
        ),
    ),
    // Whichever panel is showing gets the workflow, and with it the note thread. With no
    // node selected that is the plan overview, which is exactly the right home for a note
    // about the plan rather than about any one step.
    splitView(
      viewport,
      Object.assign(
        panel || (detail ? overviewPanel(detail.state, detail, topSteps) : workflowPanel(workflow, topSteps)),
        { noteWorkflow: workflow },
      ),
    ),
  );
}


// ── review notes on a plan ───────────────────────────────────────────────────────────────
//
// Feedback left while deciding whether to approve a draft. It hangs off the node it is
// about, in the inspector that opens when you click one — the same place, and the same
// shape, as a comment on an artifact. A note about the plan rather than any one step goes
// on the panel you get with nothing selected, which is the plan overview.
//
// The harness reads these off the workflow document and cannot write one or close one; see
// MCP-SURFACE.md. Closing is here, in front of the person who asked for the change.

/** The key a thread is filed under. A step id cannot be empty, so nothing collides. */
const NOTE_PLAN = "";

const notesHeld = (workflow) => {
  const held = state.workflowNotes;
  // Nothing while the fetch is in flight, and nothing on a screen that is not this
  // workflow — a poll landing mid-navigation must not hang one plan's feedback off another.
  if (!held || !workflow || held.workflowId !== workflow.workflow_id) return null;
  return held.notes || [];
};

/** The notes on one node. Orphans are filed under the plan: their step is gone, so there is
    no node left to open them from, and dropping them off the screen would quietly lose the
    feedback the revision was supposed to answer. */
function notesFor(workflow, stepId) {
  const all = notesHeld(workflow);
  if (!all) return null;
  return stepId
    ? all.filter((n) => n.step_id === stepId && !n.orphaned)
    : all.filter((n) => !n.step_id || n.orphaned);
}

/** How many open notes a node is carrying, for the badge on the graph. Zero everywhere but
    the workflow screen, because that is the only screen that loads them. */
function openNoteCount(stepId) {
  const held = state.workflowNotes;
  if (!held) return 0;
  return (held.notes || []).filter((n) => n.step_id === stepId && !n.resolved && !n.orphaned)
    .length;
}

const noteDraftFor = (key) =>
  state.noteDrafts[key] || { body: "", open: false, error: null };

function setNoteDraft(key, patch) {
  setState({ noteDrafts: { ...state.noteDrafts, [key]: { ...noteDraftFor(key), ...patch } } });
}

async function postNote(workflow, stepId) {
  const key = stepId || NOTE_PLAN;
  const body = noteDraftFor(key).body.trim();
  if (!body) return;
  try {
    await addReviewNote(workflow.workflow_id, {
      body,
      step_id: stepId || null,
      author: "human",
    });
    // Only on success — a refused note stays on screen to be corrected, exactly as a
    // refused artifact comment does.
    const rest = { ...state.noteDrafts };
    delete rest[key];
    setState({ noteDrafts: rest });
    await refresh();
  } catch (err) {
    setNoteDraft(key, { error: err instanceof ApiError ? err.message : String(err) });
  }
}

async function decideNote(workflow, note, resolved) {
  try {
    await decideReviewNote(workflow.workflow_id, note.note_id, resolved);
    await refresh();
  } catch (err) {
    setState({ error: err instanceof ApiError ? err.message : String(err) });
  }
}

/** One note, drawn like a comment because it is one. What it adds is the resolve control
    and, on an orphan, what the note used to be about — by then the step id names nothing,
    which is why the goal is copied onto the note when it is written. */
function noteRow(workflow, note) {
  const meta =
    `${note.author}${note.created_at ? ` · ${relAgo(note.created_at)}` : ""}` +
    (note.resolved
      ? ` · resolved${note.resolved_by ? ` by ${note.resolved_by}` : ""}` +
        (note.resolved_at ? ` ${relAgo(note.resolved_at)}` : "")
      : "");
  return el(
    "div",
    { class: note.resolved ? "note note-done" : "note" },
    note.orphaned &&
      el("span", {
        class: "note-orphan",
        text: `was on ${note.step_id}${note.step_goal ? `: ${note.step_goal}` : ""}`,
        title:
          "A revision removed that step. Whether that answered this note, or worked around " +
          "it, is yours to say — so it stays open until you close it.",
      }),
    el("span", { class: "cmt-body md-block" }, markdown(note.body)),
    el(
      "span",
      { class: "note-foot" },
      el("span", { class: "cmt-meta", text: meta }),
      workflow.status !== "archived" &&
        el("button", {
          class: "cmt-add note-act",
          text: note.resolved ? "reopen" : "resolve",
          onClick: () => decideNote(workflow, note, !note.resolved),
        }),
    ),
  );
}

/** The thread on one node: what is open, a way to see what has been dealt with, and a box.

    Rendered inside the inspector, under whatever it is about. On a draft the box is always
    offered, empty thread or not — that emptiness is the invitation, and it is where you say
    what you want changed instead of typing it somewhere Chief cannot see. */
function noteBlock(workflow, stepId) {
  const notes = notesFor(workflow, stepId);
  if (!notes) return null;

  const key = stepId || NOTE_PLAN;
  const open = notes.filter((n) => !n.resolved);
  const done = notes.filter((n) => n.resolved);
  const draft = workflow.status === "draft";
  const writable = workflow.status !== "archived";
  if (!draft && notes.length === 0) return null;

  const showing = !!state.noteShow[key];
  const label = stepId ? "Feedback on this step" : "Feedback on the plan";
  return [
    el(
      "span",
      { class: "section-label note-label" },
      el("span", { style: { flex: "1" }, text: `${label}${open.length ? ` (${open.length})` : ""}` }),
      done.length > 0 &&
        el("button", {
          class: "cmt-add note-act",
          text: showing ? `hide resolved (${done.length})` : `resolved (${done.length})`,
          onClick: () => setState({ noteShow: { ...state.noteShow, [key]: !showing } }),
        }),
    ),
    open.map((note) => noteRow(workflow, note)),
    showing && done.map((note) => noteRow(workflow, note)),
    noteDraftFor(key).error && el("div", { class: "banner", text: noteDraftFor(key).error }),
    writable &&
      (noteDraftFor(key).open
        ? el(
            "div",
            { class: "note-compose" },
            // A textarea, not a one-line input: "this should be a loop, and the check
            // belongs inside it rather than after" is a sentence, and a field that shows
            // eight characters of it invites the kind of note nobody can act on. It starts
            // three lines tall and the browser's own grip resizes it from there.
            //
            // The body is the element's text rather than a `value` attribute, which a
            // textarea ignores. Safe because every render builds a fresh node — the draft
            // lives in state, which is what survives the 15-second poll.
            el("textarea", {
              class: "note-input", id: `note-${key || "plan"}`, rows: "3",
              text: noteDraftFor(key).body,
              placeholder: stepId
                ? "What should change about this step?"
                : "What should change about the plan?",
              onInput: (e) => setNoteDraft(key, { body: e.target.value, error: null }),
              // Enter is a newline here, so sending needs the modifier. Same key as every
              // other multi-line box a person types a message into.
              onKeyDown: (e) =>
                e.key === "Enter" && (e.metaKey || e.ctrlKey) && postNote(workflow, stepId),
            }),
            el(
              "div",
              { class: "note-compose-actions" },
              el("button", {
                class: "btn btn-primary btn-sm", text: "Add",
                onClick: () => postNote(workflow, stepId),
              }),
              el("button", {
                class: "btn btn-secondary btn-sm", text: "Cancel",
                onClick: () => setNoteDraft(key, { open: false, body: "", error: null }),
              }),
            ),
          )
        : el("button", {
            class: "cmt-add",
            text: open.length || done.length ? "＋ another note" : "＋ leave a note",
            onClick: () => setNoteDraft(key, { open: true }),
          })),
  ];
}

/** Which project this belongs to, and where it was made.

    Both on one line because they answer the same question from two sides — what is this
    part of, and which checkout was it. The label is editable here because filing is a
    person's housekeeping and there is nowhere else it would go; the folder is not, because
    it is a record of where the harness stood and not a field anyone should be able to
    revise afterwards. */
function projectLine(workflow) {
  const filing = state.filing && state.filing.workflowId === workflow.workflow_id;
  const known = [...new Set((state.workflows || []).map((w) => w.project).filter(Boolean))];
  const editing = (field) => filing && state.filing.field === field;

  const editor = (field, placeholder, list) =>
    el(
      "span",
      { class: "wf-edit" },
      el("input", {
        class: "input", id: `wf-${field}`, type: "text", value: state.filing.draft,
        placeholder, ...(list ? { list } : {}),
        onInput: (e) => setState({ filing: { ...state.filing, draft: e.target.value } }),
        onKeyDown: (e) => e.key === "Enter" && saveFiling(workflow),
      }),
      el("button", {
        class: "btn btn-primary btn-sm", text: "Save", onClick: () => saveFiling(workflow),
      }),
      el("button", {
        class: "btn btn-secondary btn-sm", text: "Cancel",
        onClick: () => setState({ filing: null }),
      }),
    );

  const begin = (field, value) => () =>
    setState({ filing: { workflowId: workflow.workflow_id, field, draft: value || "" } });

  return el(
    "div",
    { class: "wf-project" },
    // The project.
    editing("project")
      ? [
          editor("project", "Project name", "wf-projects"),
          el("datalist", { id: "wf-projects" }, known.map((p) => el("option", { value: p }))),
        ]
      : [
          workflow.project
            ? el("button", {
                class: "chip on", text: workflow.project,
                title: "Show everything in this project",
                onClick: () => setState({ view: "workflows", wfProject: workflow.project }),
              })
            : el("span", { class: "text-muted", style: { fontSize: "12px" }, text: "Unfiled" }),
          el("button", {
            class: "cmt-add", style: { marginTop: "0" },
            text: workflow.project ? "refile…" : "file under a project…",
            onClick: begin("project", workflow.project),
          }),
        ],
    // Where it ran. Editable, unlike on a revision: a harness rewriting where the work
    // happened would be rewriting history, but a person correcting the record is the only
    // way a workflow planned before Chief asked for a directory can ever have one — and
    // without one the viewer cannot open a single file it reported.
    editing("origin_dir")
      ? editor("origin_dir", "/Users/you/projects/thing")
      : el(
          "span",
          { class: "wf-origin-line" },
          workflow.origin_dir
            ? el("span", {
                class: "mono wf-origin", text: `made in ${workflow.origin_dir}`,
                title: "Where the harness was when this plan was made, and what its "
                     + "relative artifact paths resolve against.",
              })
            : el("span", {
                class: "wf-origin",
                text: "no directory recorded — files cannot be opened",
                title: "A relative artifact path has nothing to resolve against until this "
                     + "is set. New plans record it themselves.",
              }),
          el("button", {
            class: "cmt-add", style: { marginTop: "0" },
            text: workflow.origin_dir ? "change…" : "set the directory…",
            onClick: begin("origin_dir", workflow.origin_dir),
          }),
        ),
  );
}

async function saveFiling(workflow) {
  const { field, draft } = state.filing;
  try {
    await labelWorkflow(workflow.workflow_id, { [field]: (draft || "").trim() || null });
    setState({ filing: null });
    await refresh();
  } catch (err) {
    setState({ error: err instanceof ApiError ? err.message : String(err), filing: null });
  }
}

/** Open the plan's own thread — the panel you get with no node selected.

    Shown as a count so it also answers "is there anything on the plan I have not read", and
    marked as current while that panel is the one showing, so it reads as a place you are
    rather than only a button you press. */
function planNoteButton(workflow) {
  const notes = notesFor(workflow, null);
  if (!notes) return null;
  const draft = workflow.status === "draft";
  if (!draft && notes.length === 0) return null;

  const open = notes.filter((n) => !n.resolved).length;
  const here = !state.selected || state.selected === "none";
  return el("button", {
    class: here ? "btn btn-secondary btn-sm is-here" : "btn btn-ghost btn-sm",
    style: { fontSize: "12px" },
    text: `Feedback on the plan${open ? ` · ${open}` : ""}`,
    title: "Notes about the plan as a whole, and any left on a step that has since gone",
    onClick: () => setState({ selected: "none" }),
  });
}

/** The decisions already taken on this workflow, read back out of the audit log.

    Without this a comment is written and never seen again, which is the same as not asking
    for it. Silent while the log is still loading rather than claiming there was no comment. */
function decisionNote(workflow) {
  const audit = state.workflowAudit;
  if (!audit || audit.workflowId !== workflow.workflow_id) return null;

  const decisions = audit.entries.filter(
    (e) => e.event === "workflow.approved" || e.event === "workflow.archived",
  );
  if (decisions.length === 0) return null;

  return el(
    "div",
    { style: { display: "flex", flexDirection: "column", gap: "var(--space-1)", marginTop: "var(--space-3)" } },
    decisions.map((entry) => {
      const detail = entry.detail || {};
      const what =
        entry.event === "workflow.approved"
          ? "Approved"
          : detail.from === "draft"
            ? "Discarded"
            : "Archived";
      return el(
        "div",
        { class: "accent-note", style: { fontSize: "13px" } },
        el("span", { text: `${what} by ${detail.decided_by || "unknown"} · ${relAgo(entry.at)}` }),
        detail.reason &&
          el("span", {
            style: { display: "block", marginTop: "4px", color: "var(--color-neutral-600)" },
            text: detail.reason,
          }),
      );
    }),
  );
}

/** The inspector's resting state for a workflow: what the plan is, before a node is picked.

    Same shape the run overview returns — `instances` included, because inspector() maps it
    unconditionally. */
function workflowPanel(workflow, topSteps) {
  const harnesses = [...new Set(workflow.steps.map((s) => s.harness))];
  const constructs = workflow.steps.filter((s) => s.type !== "task");
  return {
    kicker: "Plan overview",
    title: workflow.title,
    metaLine:
      `${topSteps.length} top-level of ${workflow.steps.length} steps · ` +
      `${harnesses.join(", ")}` +
      (constructs.length ? ` · ${constructs.map((c) => `${c.id} ${c.type}`).join(", ")}` : "") +
      (workflow.created_at ? ` · added ${stamp(workflow.created_at)}` : ""),
    summary:
      workflow.status === "draft"
        ? "Nothing has run. Select a step to read its goal, then approve or discard the plan."
        : "Select a step to inspect it.",
    summaryColor: "var(--color-neutral-500)",
    instances: [],
  };
}

function detailScreen() {
  const detail = state.detail;
  if (!detail || detail.runId !== state.runId) {
    return el("main", { class: "wide graph" }, el("p", { class: "text-muted", text: "Loading run…" }));
  }
  const run = detail.state;
  const def = detail.def;
  const meta = RUN_META[run.status] || RUN_META.running;
  const { viewport, panel, topSteps } = planGraph({
    def,
    stepStates: run.step_states || {},
    pending: pendingOf(detail.amendments),
    past: detail.amendments.filter((a) => a.status !== "pending_approval"),
  });

  return el(
    "main",
    { class: "wide graph", "data-screen-label": "Run detail" },
    el(
      "div",
      {},
      el("button", {
        class: "btn btn-ghost",
        style: { fontSize: "13px", marginLeft: "calc(-1 * var(--space-1))" },
        text: "← Workflow", onClick: () => openWorkflow(run.workflow_id),
      }),
      el(
        "div",
        { class: "screen-head", style: { marginTop: "var(--space-3)" } },
        el("h4", { text: def.title }),
        badge(meta),
      ),
      el("p", {
        class: "text-muted mono",
        style: { fontSize: "11px", margin: "var(--space-2) 0 0" },
        text:
          `${run.run_id} · base v${run.base_version}` +
          (run.applied_amendment_ids.length
            ? ` · +${run.applied_amendment_ids.length} amendment` +
              (run.applied_amendment_ids.length > 1 ? "s" : "")
            : ""),
      }),
    ),
    splitView(viewport, panel || overviewPanel(run, detail, topSteps)),
  );
}

// ── dialog ───────────────────────────────────────────────────────────────────────────────

/** The comment is optional and is kept: both endpoints take a decision body, and it lands in
    the audit entry beside who decided. */
function workflowDialogNode() {
  const { workflow, action, busy, error, reason } = state.dialog;
  const input = el("input", {
    class: "input", id: "decision-reason", value: reason,
    placeholder: "Recorded in the audit log",
  });
  input.addEventListener("input", () => (state.dialog.reason = input.value));
  const approving = action === "approve";
  const discarding = !approving && workflow.status === "draft";

  return el(
    "div",
    { class: "dialog-backdrop" },
    el(
      "div",
      { class: "dialog", role: "dialog", "aria-modal": "true" },
      el("div", {
        class: "dialog-title",
        text: `${approving ? "Approve" : discarding ? "Discard" : "Archive"} ${workflow.workflow_id}`,
      }),
      el("div", {
        class: "dialog-body",
        text: approving
          ? "Runs can register against this plan from now on. The approval lands in the audit log."
          : discarding
            ? "The draft is retired without ever having run. It cannot be approved afterwards."
            : "No new runs can register. Runs already in progress are unaffected.",
      }),
      el("div", {
        class: "accent-note", style: { fontSize: "13px" },
        text: workflow.title,
      }),
      error && el("div", { class: "banner", text: error }),
      el(
        "div",
        { class: "field" },
        el("label", { for: "decision-reason", text: `Comment (optional)` }),
        input,
      ),
      el(
        "div",
        { class: "dialog-actions" },
        el("button", {
          class: "btn btn-secondary", text: "Cancel", disabled: busy || null,
          onClick: () => setState({ dialog: null }),
        }),
        el("button", {
          class: approving ? "btn btn-primary" : "btn btn-secondary", disabled: busy || null,
          text: busy ? "Working…" : approving ? "Approve" : discarding ? "Discard" : "Archive",
          onClick: confirmDialog,
        }),
      ),
    ),
  );
}

function templateDialogNode() {
  const { template, action, busy, error, values } = state.dialog;
  const using = action === "use";

  const fields = using
    ? template.parameters.map((parameter) => {
        const id = `param-${parameter.name}`;
        const input = el("input", {
          class: "input", id, value: values[parameter.name] ?? "",
          placeholder: parameter.default ?? "required",
        });
        input.addEventListener("input", () => (state.dialog.values[parameter.name] = input.value));
        return el(
          "div",
          { class: "field" },
          el("label", { for: id, text: parameter.description || parameter.name }),
          input,
        );
      })
    : [];

  return el(
    "div",
    { class: "dialog-backdrop" },
    el(
      "div",
      { class: "dialog", role: "dialog", "aria-modal": "true" },
      el("div", {
        class: "dialog-title",
        text: using ? `Use ${template.template_id}` : `Archive ${template.template_id}`,
      }),
      el("div", {
        class: "dialog-body",
        text: using
          ? "This builds a draft workflow from the template. It still needs approving before it can run, unless a policy covers it."
          : "The template stays readable but no new workflows can be made from it.",
      }),
      el("div", { class: "accent-note", style: { fontSize: "13px" }, text: template.title }),
      error && el("div", { class: "banner", text: error }),
      ...fields,
      el(
        "div",
        { class: "dialog-actions" },
        el("button", {
          class: "btn btn-secondary", text: "Cancel", disabled: busy || null,
          onClick: () => setState({ dialog: null }),
        }),
        el("button", {
          class: using ? "btn btn-primary" : "btn btn-secondary", disabled: busy || null,
          text: busy ? "Working…" : using ? "Create workflow" : "Archive",
          onClick: confirmDialog,
        }),
      ),
    ),
  );
}

function dialogNode() {
  if (state.dialog.subject === "delete") return deleteDialogNode();
  if (state.dialog.subject === "template") return templateDialogNode();
  if (state.dialog.subject === "workflow") return workflowDialogNode();
  const { amendment, approve, busy, error, reason } = state.dialog;
  const input = el("input", {
    class: "input", id: "decision-reason", value: reason,
    placeholder: "Recorded in the audit log",
  });
  input.addEventListener("input", () => (state.dialog.reason = input.value));

  return el(
    "div",
    { class: "dialog-backdrop" },
    el(
      "div",
      { class: "dialog", role: "dialog", "aria-modal": "true" },
      el("div", { class: "dialog-title", text: `${approve ? "Approve" : "Reject"} ${amendment.amendment_id}` }),
      el("div", {
        class: "dialog-body",
        text: approve
          ? "The run resumes on the amended plan; the decision lands in the audit log."
          : "The proposal is closed; the run resumes unchanged.",
      }),
      approve &&
        amendment.kind === "history_edit" &&
        el("div", {
          class: "accent-note", style: { fontSize: "13px" },
          text: "History edit: this re-runs a step that already finished. The prior result is snapshotted first.",
        }),
      error && el("div", { class: "banner", text: error }),
      el(
        "div",
        { class: "field" },
        el("label", { for: "decision-reason", text: "Reason (optional)" }),
        input,
      ),
      el(
        "div",
        { class: "dialog-actions" },
        el("button", {
          class: "btn btn-secondary", text: "Cancel", disabled: busy || null,
          onClick: () => setState({ dialog: null }),
        }),
        el("button", {
          class: "btn btn-primary", disabled: busy || null,
          text: busy ? "Working…" : approve ? "Approve" : "Reject",
          onClick: confirmDialog,
        }),
      ),
    ),
  );
}

// ── render ───────────────────────────────────────────────────────────────────────────────

const SCREENS = {
  approvals: approvalsScreen,
  templates: templatesScreen,
  template: templateDetailScreen,
  workflows: workflowsScreen,
  workflow: workflowDetailScreen,
  // A run's own screen survives only for a workflow with more than one execution.
  detail: detailScreen,
};

const screenFor = (view) => (SCREENS[view] || workflowsScreen)();

let measured = null; // the graph viewport of the most recent render, for width measurement
let dialogKey = null;

function render() {
  const root = document.getElementById("app");
  measured = null;
  // The whole tree is replaced on every render, including the poll's, so a field being
  // typed into is destroyed mid-word. Its identity and selection are carried across.
  const active = document.activeElement;
  const keepFocus = active && active.id ? { id: active.id, start: active.selectionStart, end: active.selectionEnd } : null;
  // Same problem as focus, for the file viewer: its body is rebuilt fresh every render,
  // including the 15s poll, so a scroll position held only in the old (about-to-be-
  // discarded) DOM node would otherwise snap back to the top mid-read.
  const viewerScroll = document.querySelector(".viewer-body")?.scrollTop;
  // replaceChildren has no conditional-child idiom of its own — a skipped branch reaching
  // it would be stringified into the page — so the list is filtered before it gets there.
  root.replaceChildren(
    ...[
      navBar(),
      state.error &&
        el("div", { class: "banner", style: { margin: "0 var(--space-4)" }, text: state.error }),
      screenFor(state.view),
      fileViewer(),
    ].filter((n) => n instanceof Node),
  );
  if (viewerScroll) {
    const body = document.querySelector(".viewer-body");
    if (body) body.scrollTop = viewerScroll;
  }

  // The dialog is a sibling of the app tree and is rebuilt only when it changes identity,
  // so re-renders driven by polling do not wipe what has been typed into the reason field.
  const host = document.getElementById("dialog-root");
  const key = state.dialog && `${state.dialog.key}:${state.dialog.busy}:${state.dialog.error}`;
  if (key !== dialogKey) {
    dialogKey = key;
    host.replaceChildren(...(state.dialog ? [dialogNode()] : []));
    host.querySelector("input")?.focus();
  }

  if (keepFocus) {
    const back = document.getElementById(keepFocus.id);
    if (back && back !== document.activeElement) {
      back.focus?.();
      if (keepFocus.start != null) back.setSelectionRange?.(keepFocus.start, keepFocus.end);
    }
  }

  measureGraph();
}

/** The graph lays out against its own measured width; one re-render settles it. */
function measureGraph() {
  if (!measured) return;
  const width = measured.offsetWidth;
  if (width > 0 && Math.abs(width - state.graphWidth) > 2) {
    state.graphWidth = width;
    render();
  }
}

window.addEventListener("resize", measureGraph);
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && state.dialog && !state.dialog.busy) setState({ dialog: null });
});

// Back, forward, or a hash typed by hand: the URL is the source of truth for *where*, so
// the view follows it rather than the other way round.
window.addEventListener("hashchange", () => {
  if (location.hash === appliedHash) return;
  appliedHash = location.hash;
  setState(stateFromHash());
  refresh();
});

Object.assign(state, stateFromHash());
appliedHash = hashFor(state);
if (location.hash !== appliedHash) location.replace(appliedHash);
render();
refresh();
setInterval(refresh, POLL_MS);
