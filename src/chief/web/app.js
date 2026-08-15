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
  decideAmendment, getRunDefinition, getRunDetail, getWorkflowAudit, instantiateTemplate,
  listAmendments, listRuns, listTemplates, listWorkflows, resolveCheckpoint,
} from "./api.js";

// ── colour and status vocabulary ─────────────────────────────────────────────────────────
// Colours stay as CSS custom properties rather than literals so the dark palette in
// chief.css applies without a second table here.

const OK = "var(--ok)";
const WARN = "var(--warn)";
const BAD = "var(--bad)";
const ACC = "var(--color-accent)";
const DIM = "var(--dim)";

const STEP_META = {
  pending: { color: "var(--color-neutral-400)" },
  running: { color: ACC, pulse: true },
  completed: { color: OK },
  failed: { color: BAD },
  skipped: { color: "var(--color-neutral-300)" },
  // A checkpoint that has been reached. Warm rather than accent-blue: this one is not the
  // machine working, it is the machine stopped, waiting for you.
  blocked: { color: WARN, pulse: true },
};

const RUN_META = {
  running: { label: "running", color: ACC, pulse: true },
  paused_for_approval: { label: "awaiting approval", color: ACC, pulse: true },
  waiting_on_human: { label: "waiting on you", color: WARN, pulse: true },
  completed: { label: "completed", color: OK },
  failed: { label: "failed", color: BAD },
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
  draft: { label: "awaiting approval", color: ACC, pulse: true },
  ready: { label: "ready to run", color: "var(--color-neutral-500)" },
  archived: { label: "archived", color: "var(--color-neutral-400)" },
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

/** One editor, named once. Not a preference and not a picker: on the machine Chief runs on
    there is a default editor for source, and offering a dropdown would be asking the reader
    to configure something they will answer the same way every time. `cursor://file/` is the
    same shape if that is the one you want; JetBrains is not (`idea://open?file=`). */
const EDITOR_SCHEME = "vscode://file";

// localStorage throws outright in a few configurations (and simply is not there under the
// smoke harness's stub DOM), and none of this is worth a broken render.
const readRoot = () => {
  try {
    return localStorage.getItem(ROOT_KEY) || "";
  } catch {
    return "";
  }
};

const writeRoot = (value) => {
  try {
    if (value) localStorage.setItem(ROOT_KEY, value);
    else localStorage.removeItem(ROOT_KEY);
  } catch {
    /* the path still resolves for this session; it just will not survive a reload */
  }
};

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
  const root = (state.filesRoot || "").replace(/\/+$/, "");
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
function pathRow(ref) {
  if (!ref) return null;
  const web = /^https?:/i.test(ref);
  const absolute = absolutePath(ref);
  // What gets copied is the most useful form of the reference, which for a relative path
  // means the resolved one — that is the version that means something in a terminal. With
  // no folder set there is still the raw ref, and handing that over beats handing over
  // nothing, so the copy control is unconditional.
  const target = web ? ref : absolute || ref;

  return el(
    "span",
    { class: "art-path" },
    web
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

/** The control that names the base, shown with the artifacts rather than parked in a
    settings screen: it only matters when you are looking at a path that needs it. */
function rootRow(arts) {
  if (!arts.some(({ artifact }) => isFileRef(artifact.ref) && !artifact.ref.startsWith("/"))) {
    return null;
  }
  if (state.rootEditing) {
    return el(
      "div",
      { class: "art-root" },
      el("input", {
        class: "input", id: "files-root", type: "text", value: state.rootDraft,
        placeholder: "/Users/you/projects/thing",
        onInput: (e) => setState({ rootDraft: e.target.value }),
        onKeyDown: (e) => e.key === "Enter" && saveRoot(),
      }),
      el("button", { class: "btn btn-primary btn-sm", text: "Save", onClick: saveRoot }),
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
      text: state.filesRoot ? "Project folder" : "Set a project folder to open these",
    }),
    state.filesRoot && el("span", { class: "mono art-root-path", text: state.filesRoot }),
    el("button", {
      class: "btn btn-secondary btn-sm", text: state.filesRoot ? "Change" : "Set…",
      onClick: () => setState({ rootEditing: true, rootDraft: state.filesRoot }),
    }),
  );
}

function saveRoot() {
  const value = (state.rootDraft || "").trim().replace(/\/+$/, "");
  writeRoot(value);
  setState({ filesRoot: value, rootEditing: false });
}

// ── artifacts ────────────────────────────────────────────────────────────────────────────

const ICONS = {
  markdown: "≡", image: "▦", video: "▶", audio: "♪",
  url: "↗", pr: "↗", json: "{}", log: "⌗",
};

/** One ArtifactRef as a card. `type` is an open string (REQ-46), so anything unrecognised
    degrades to its reference rendered as a link. */
function artifactCard(artifact, label) {
  const data = artifact.data || {};
  const title = label || artifact.description || artifact.ref || artifact.type;
  const body = [];
  let meta = artifact.type;

  if (artifact.type === "markdown" && data.text) {
    meta = "markdown";
    body.push(
      el(
        "div",
        { class: "art-md" },
        data.text
          .split("\n")
          .filter((line) => line.trim())
          .map((line) =>
            line.startsWith("## ")
              ? el("span", { class: "h", text: line.slice(3) })
              : line.startsWith("- ")
                ? el("span", { class: "li" }, el("span", { text: line.slice(2) }))
                : el("span", { class: "p", text: line }),
          ),
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
  const path = pathRow(artifact.ref);

  return el(
    "section",
    { class: "card", style: { padding: "var(--space-3)" } },
    el(
      "span",
      { class: "art-head" },
      el("span", { class: "art-icon", text: ICONS[artifact.type] || "⌗" }),
      el("span", { class: "art-label", text: title }),
      el("span", { class: "art-meta", text: meta }),
    ),
    body,
    path,
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
  // What a relative artifact ref is relative to, and the half-typed version of it while it
  // is being changed. Read from localStorage once, here, so every render is a plain field
  // lookup rather than a trip through storage.
  filesRoot: readRoot(),
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
  wfSort: { key: "lifecycle", dir: "asc" },
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
  if (key && s[key]) return `#/${ROUTE_KIND[s.view]}/${encodeURIComponent(s[key])}`;
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
  const [kind, raw] = location.hash.replace(/^#\/?/, "").split("/");
  const id = raw && decodeURIComponent(raw);
  const blank = {
    runId: null, workflowId: null, templateId: null,
    selected: null, detail: null, dialog: null, workflowAudit: null,
  };
  const view = Object.keys(ROUTED).find((v) => ROUTE_KIND[v] === kind);
  if (view && id) return { ...blank, view, [ROUTED[view]]: id };
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
  setState({ view: "workflow", workflowId, selected: null, dialog: null, workflowAudit: null });
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
    el("span", { class: "nav-brand" }, el("span", { class: "nav-dot" }), "Chief"),
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
    dot(life.color, life.pulse ? "chiefpulse 1.6s infinite" : false),
    el(
      "span",
      { class: "title" },
      el("span", { text: workflow.title }),
      el("span", { class: "id text-muted", text: workflow.workflow_id }),
      runs.length > 1 &&
        el("span", { class: "tag", text: `${runs.length} executions` }),
    ),
    el("span", { class: "status", style: { color: life.color }, text: life.label }),
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

    `lifecycle` is the default and sorts by LIFECYCLE_ORDER, not by the status string — the
    whole point of that order is that alphabetical buries the drafts.

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

function sortWorkflows(key, dir) {
  const col = WF_COLUMNS.find((c) => c.key === key) || WF_COLUMNS[1];
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
    !q || r.workflow.title.toLowerCase().includes(q) || r.workflow.workflow_id.includes(q);
  const shown = rows.filter((r) => filter.of(r.workflow, r.life) && matches(r));

  const { key, dir } = state.wfSort;
  const col = WF_COLUMNS.find((c) => c.key === key) || WF_COLUMNS[1];
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
    shown.length > 0 &&
      el(
        "div",
        { class: "list-head" },
        el("span", { class: "dot", style: { visibility: "hidden" } }),
        WF_COLUMNS.map((c) =>
          el("button", {
            class: `col-head col-${c.key} ${c.cls}` + (c.key === key ? " on" : ""),
            text: c.label + (c.key === key ? (dir === "desc" ? " ↓" : " ↑") : ""),
            onClick: () =>
              sortWorkflows(c.key, c.key === key ? (dir === "asc" ? "desc" : "asc") : c.dir),
          }),
        ),
      ),
    el(
      "div",
      { style: { display: "flex", flexDirection: "column" } },
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
    el("p", {
      style: { margin: "0", fontSize: "14px" },
      text: step ? step.goal : `Waiting on a decision at ${stepId}.`,
    }),
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
  const nodeW = Math.max(170, Math.min(250, (width - 32 - (widest - 1) * GAP) / widest));

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
    pos[step.id].x = Math.max(16, Math.min(pos[step.id].x, width - nodeW - 16));
  }

  return { all, pos, nodeW, height: bottom + 24, ghostIds };
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
    kicker:
      step.type === "checkpoint"
        ? `Checkpoint · ${outcome ? outcome.decision : stepState.status}`
        : `Step · ${stepState.status}`,
    title: step.goal,
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
      return {
        label: `${instance.kind === "iteration" ? "Iteration" : "Branch"} ${instance.index + 1}`,
        summary: (instance.status === "failed" && failedBody ? failedBody.summary : instance.summary) || "",
        summaryColor: instance.status === "failed" ? BAD : "var(--color-neutral-500)",
        color: im.color, pulse: pulseOf(im),
      };
    }),
    // The construct's body: the steps every instance runs. Static, so it is readable before
    // the first instance exists — which is when a person is deciding whether to approve it.
    exitLabel: step.exit_when ? `Exits when: ${step.exit_when}` : null,
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
    artsLabel: arts.length ? `All artifacts (${arts.length})` : null,
    arts,
  };
}

function inspector(panel) {
  return el(
    "aside",
    { class: "inspector", "data-screen-label": "Inspector" },
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
      el("p", { style: { margin: "0", fontSize: "13px", lineHeight: "1.45" }, text: panel.title }),
      el("span", {
        class: "mono", style: { fontSize: "11px", color: "var(--color-neutral-500)" },
        text: panel.metaLine,
      }),
      panel.warn && el("div", { class: "accent-note", text: panel.warn }),
      panel.summary &&
        el("span", { style: { fontSize: "12px", color: panel.summaryColor }, text: panel.summary }),
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
      panel.instances.map((instance) =>
        el(
          "span",
          { class: "inst-row" },
          dot(instance.color, instance.pulse, "6px"),
          el("span", { class: "label", text: instance.label }),
          el("span", { class: "summary", style: { color: instance.summaryColor }, text: instance.summary }),
        ),
      ),
      panel.approve &&
        el(
          "div",
          { style: { display: "flex", gap: "var(--space-2)", marginTop: "var(--space-1)" } },
          el("button", { class: "btn btn-primary btn-sm", text: "Approve…", onClick: panel.approve }),
          el("button", { class: "btn btn-secondary btn-sm", text: "Reject…", onClick: panel.reject }),
        ),
    ),
    panel.artsLabel &&
      el("span", { class: "section-label", style: { padding: "0 var(--space-1)" }, text: panel.artsLabel }),
    panel.artsLabel && rootRow(panel.arts || []),
    (panel.arts || []).map(({ artifact, label }) => artifactCard(artifact, label)),
  );
}

// ── templates ────────────────────────────────────────────────────────────────────────────

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
    return el("main", { class: "wide" }, el("p", { class: "text-muted", text: "Loading…" }));
  }
  // Placeholders live in text, never in ids or edges, so the unrendered plan has exactly the
  // shape every workflow made from it will have.
  const { viewport, panel, topSteps } = planGraph({ def: template });
  const active = template.status === "active";

  return el(
    "main",
    { class: "wide", "data-screen-label": "Template detail" },
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
            class: "btn btn-secondary btn-sm", text: "Archive…",
            onClick: () => openTemplateArchiveDialog(template),
          }),
        ),
    ),
    el("div", { class: "graph-split" }, viewport, inspector(panel || templatePanel(template, topSteps))),
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
  const { all, pos, nodeW, height, ghostIds } = layout(display, ghosts, rewires, width, heightFor);

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
    { class: "graph-viewport", style: { height: `${Math.ceil(height * scale)}px` } },
    el(
      "div",
      {
        class: "graph-plane",
        style: {
          width: `${width}px`, height: `${height}px`,
          transform: scale < 1 ? `scale(${scale})` : "none",
        },
      },
      svgEl("svg", { width: String(width), height: String(height) }, edgeDefs(), paths),
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
    return el("main", { class: "wide" }, el("p", { class: "text-muted", text: "Loading…" }));
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
    { class: "wide", "data-screen-label": "Workflow detail" },
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
        el("span", { style: { fontSize: "13px", color: life.color }, text: life.label }),
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
          el("button", {
            class: "btn btn-secondary btn-sm", text: draft ? "Discard…" : "Archive…",
            onClick: () => openWorkflowDialog(workflow, "archive"),
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
                dot(meta.color, pulseOf(meta)),
                el(
                  "span",
                  { class: "title" },
                  el("span", { text: i === 0 ? "latest" : `execution ${runs.length - i}` }),
                  el("span", { class: "id text-muted", text: r.run_id }),
                ),
                el("span", { class: "status", style: { color: meta.color }, text: meta.label }),
                el("span", { class: "when", text: rel(r.updated_at) }),
              );
            }),
          ),
        ),
    ),
    el(
      "div",
      { class: "graph-split" },
      viewport,
      inspector(panel || (detail ? overviewPanel(detail.state, detail, topSteps) : workflowPanel(workflow, topSteps))),
    ),
  );
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
    return el("main", { class: "wide" }, el("p", { class: "text-muted", text: "Loading run…" }));
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
    { class: "wide", "data-screen-label": "Run detail" },
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
        el("span", { style: { fontSize: "13px", color: meta.color }, text: meta.label }),
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
    el("div", { class: "graph-split" }, viewport, inspector(panel || overviewPanel(run, detail, topSteps))),
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
  // replaceChildren has no conditional-child idiom of its own — a skipped branch reaching
  // it would be stringified into the page — so the list is filtered before it gets there.
  root.replaceChildren(
    ...[
      navBar(),
      state.error &&
        el("div", { class: "banner", style: { margin: "0 var(--space-4)" }, text: state.error }),
      screenFor(state.view),
    ].filter((n) => n instanceof Node),
  );

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
