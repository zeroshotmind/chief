/* Headless smoke test for the web UI.

   Run with `node scripts/smoke_ui.mjs`; exits non-zero on failure. It is not part of pytest —
   it needs node, which the package does not otherwise require.

   It renders every screen against canned API responses, driving the app through a stub DOM.
   What it checks is that each screen *builds*: no browser is involved, so it says nothing
   about whether anything looks right. That narrow check is still worth having — the UI has no
   other automated coverage, and the failures it catches (a panel missing a field the
   inspector maps over, a screen wired to state that is not there) are silent until someone
   opens the page. */

const clicks = [];
const handlers = []; // non-click listeners, so the search field can be typed into

function node(tag) {
  const self = {
    tag,
    children: [],
    style: {},
    textContent: "",
    offsetWidth: 900,
    setAttribute(k, v) { this[k] = v; },
    getAttribute(k) { return this[k]; },
    appendChild(c) { this.children.push(c); return c; },
    replaceChildren(...c) { this.children = c; },
    addEventListener(type, fn) {
      if (type === "click") clicks.push({ node: this, fn });
      else handlers.push({ node: this, type, fn });
    },
    querySelector() { return null; },
    querySelectorAll() { return []; },
    replaceWith() {},
    // A real anchor has these; the download path uses both.
    click() { self.clicked = true; },
    remove() {},
  };
  // Real nodes always have one, and code that toggles a class without going through a
  // re-render reaches for it directly. It writes through to `class`, which is what the
  // assertions below read.
  const classes = () => (self.class || "").split(" ").filter(Boolean);
  self.classList = {
    add: (c) => { self.class = [...classes(), c].join(" "); },
    remove: (c) => { self.class = classes().filter((x) => x !== c).join(" "); },
  };
  return self;
}

const roots = { app: node("div"), "dialog-root": node("div") };
// What a browser download needs and the stub DOM does not have. The blob is captured so the
// exported bytes can be asserted rather than only the fact that a click happened.
let exported = null;
// Extended, not replaced. Overwriting `globalThis.URL` wholesale took the constructor with
// it, and every `new URL(...)` in the app then threw into whatever its fallback was — here
// that silently produced a stricter iframe sandbox than the browser would, and the test
// read the fallback as the answer.
globalThis.URL.createObjectURL = (b) => { exported = b; return "blob:x"; };
globalThis.URL.revokeObjectURL = () => {};
// A root element with a working `style.setProperty`, because the drawer insets the page
// through a custom property on it — a stub without one leaves that path untested.
const documentElement = node("html");
const cssVars = {};
documentElement.style.setProperty = (k, v) => { cssVars[k] = v; };
globalThis.document = {
  body: node("body"),
  documentElement,
  createElement: node,
  createElementNS: (_ns, tag) => node(tag),
  getElementById: (id) => roots[id] || node("div"),
  head: node("head"),
  querySelector() { return null; },
  querySelectorAll() { return []; },
  addEventListener() {},
  createTextNode: (t) => ({ text: t }),
};
// Recorded rather than swallowed: a drag listens on the window for pointermove/up, so a
// stub that dropped them could not be driven, and the resize would be untestable.
const winListeners = [];
globalThis.window = {
  innerWidth: 1400,
  addEventListener(type, fn) { winListeners.push({ type, fn }); },
  removeEventListener(type, fn) {
    const i = winListeners.findIndex((l) => l.type === type && l.fn === fn);
    if (i >= 0) winListeners.splice(i, 1);
  },
};
const fireWindow = (type, event) =>
  winListeners.filter((l) => l.type === type).forEach((l) => l.fn(event));
// A real origin, because the frame sandbox is decided by comparing the framed URL against
// it — a stub without one would only ever exercise the fail-safe branch.
globalThis.location = {
  origin: "http://localhost:8080", href: "http://localhost:8080/ui/",
  search: "", hash: "", replace(h) { this.hash = h; },
};
globalThis.Node = Object;
globalThis.setInterval = () => 0;
// The base a relative artifact ref resolves against lives in the browser, not on the server
// (see app.js "local files"). Standing in for it here is what makes the editor link's shape
// assertable — the one part of this that fails silently, because a wrong scheme or a
// mangled path still renders as a perfectly ordinary link.
const stored = { "chief.filesRoot": "/Users/you/work/songs" };
globalThis.localStorage = {
  getItem: (k) => stored[k] ?? null,
  setItem: (k, v) => { stored[k] = v; },
  removeItem: (k) => { delete stored[k]; },
};

const STEPS = [
  // Criteria on a step that ran, so the checklist can be read against what the harness
  // answered; and on one that has not, so the outstanding state is drawn too.
  { id: "a", type: "task", goal: "first", harness: "claude-code", depends_on: [],
    criteria: [{ id: "c1", text: "every persona has a voice note" },
               { id: "c2", text: "the two cut parts are still listed" }] },
  // Declared instance parameters, and a body step that names one: the count is decided at
  // runtime, so what tells one iteration from another can only arrive at runtime too.
  { id: "b", type: "loop", goal: "each thing", harness: "claude-code", depends_on: ["a"], body: ["c", "d"], exit_when: "the check passes",
    instance_params: [{ name: "paper", description: "which paper this iteration reads", required: true }] },
  { id: "c", type: "task", goal: "read {{ paper }} end to end", harness: "claude-code", depends_on: [] },
  // Fixed in the plan rather than reported at runtime, so it has to be readable on a draft
  // — the "checklist" entry is shaped like an ArtifactRef and should draw as one; "threshold"
  // is not, and falls back to plain metadata beside it.
  { id: "d", type: "task", goal: "then check it", harness: "claude-code", depends_on: ["c"],
    criteria: [{ id: "c1", text: "the check is green" }],
    inputs: {
      checklist: { type: "file", ref: "notes/checklist.md", description: "Checklist to verify against" },
      threshold: 0.9,
    } },
  // The step the run stops on. A person decides it, and is asked one thing in writing.
  { id: "e", type: "checkpoint", goal: "ship it?", harness: "human", depends_on: ["a"],
    fields: [{ name: "budget", label: "How much may it spend?", hint: "$", required: true }] },
];
// created_at/updated_at are the store's stamps on the record, not fields a harness sends;
// the list sorts by them, so the fixture carries them in a deliberately un-alphabetical
// order.
// Twelve steps in one layer. The layout floors node width at 170px, so this needs ~2400px
// however wide the window is — which is the case the viewport used to clip silently.
const WIDE_STEPS = [
  { id: "w_open", type: "task", goal: "split it", harness: "claude-code", depends_on: [] },
  ...Array.from({ length: 12 }, (_, i) => ({
    id: `w${String(i).padStart(2, "0")}`, type: "task", goal: `shard ${i}`,
    harness: "claude-code", depends_on: ["w_open"],
  })),
  { id: "w_join", type: "task", goal: "merge it", harness: "claude-code",
    depends_on: Array.from({ length: 12 }, (_, i) => `w${String(i).padStart(2, "0")}`) },
];
const WORKFLOWS = [
  { created_at: "2026-03-02T09:00:00Z", updated_at: "2026-03-02T09:00:00Z", workflow_id: "wf_draft", title: "A draft", source: "generated", generated_by: "claude-code", status: "draft", version: 1, steps: STEPS, project: "songs", origin_dir: "/w/songs" },
  { created_at: "2026-01-05T09:00:00Z", updated_at: "2026-01-06T09:00:00Z", workflow_id: "wf_ok", title: "Approved one", source: "import", generated_by: null, status: "approved", version: 2, steps: STEPS, project: "chief", origin_dir: "/w/chief" },
  { created_at: "2026-02-01T09:00:00Z", updated_at: "2026-02-01T09:00:00Z", workflow_id: "wf_old", title: "Archived one", source: "import", generated_by: null, status: "archived", version: 1, steps: STEPS },
];
const RUN = {
  run_id: "run_1", workflow_id: "wf_ok", base_version: 2, applied_amendment_ids: [],
  status: "waiting_on_human", created_at: new Date().toISOString(), updated_at: new Date().toISOString(),
  step_states: {
    // Two refs of the kind a harness actually reports: a file relative to wherever it was
    // working, and something already on the web. They resolve down different arms.
    a: { step_id: "a", status: "completed", summary: "did it",
      criteria_met: { c1: "all nine recorded", c2: "listed under Cut" },
      metadata: { tokens: 41200, cost_usd: 0.62, model: "claude", nested: { retries: 2 } },
      artifacts: [
      { artifact_id: "art_1", type: "markdown",
        description: "Persona sheet for the whole cast, including the three walk-on parts that only appear in the second act and the two that were cut but are kept for continuity",
        ref: "notes/personas.md",
        // Exercises the renderer end to end: a heading, a hard line break, emphasis, a code
        // span, a list, and inline maths — the shapes a harness actually reports.
        data: { text: "## Cast\nfirst line\nsecond line\n\nThe **lead** is set; see `personas.md`.\n\n- one\n- two\n\nLoss is $x^2 + \\alpha$ per pass." },
        comments: [{ comment_id: "cmt_1", body: "the tone here is the one to match", author: "roy", created_at: new Date().toISOString(), via: "rest" }] },
      { artifact_id: "art_2", type: "pr", description: "The PR", ref: "https://example.com/pr/1", data: { additions: 41, files: 3 }, comments: [] },
      { artifact_id: "art_3", type: "file", description: "Metrics", ref: "data/metrics.json", data: null, comments: [] },
      { artifact_id: "art_4", type: "file", description: "Post", ref: "notes/post.mdx", data: null, comments: [] },
      // The same kind of document with nothing beside it: no modules, so nothing to run, and
      // it falls back to prose with its components named. Both paths matter.
      { artifact_id: "art_5", type: "file", description: "Lone", ref: "notes/named.mdx", data: null, comments: [] },
    ] },
    b: { step_id: "b", status: "running", instances: [{ instance_id: "i0", kind: "iteration", index: 0, status: "completed", summary: "one", step_states: {}, metadata: { paper: "arxiv:2401.11111", seed: 7, deep: { a: 1 } } }] },
    e: { step_id: "e", status: "blocked", summary: "reached the checkpoint", started_at: new Date().toISOString(), artifacts: [] },
  },
};
const TEMPLATES = [
  {
    template_id: "tpl_1", title: "Triage {{ repo }}", description: "nightly triage",
    parameters: [
      { name: "repo", description: "owner/name", required: true, default: null },
      { name: "since", description: null, required: false, default: "24h" },
    ],
    steps: STEPS, status: "active", version: 1, derived_from_workflow_id: null, project: "chief",
    created_at: new Date().toISOString(), updated_at: new Date().toISOString(),
  },
];
// Review notes on the draft: one open on a step that is still there, one whose step the
// plan has since lost, and one already dealt with. The third is what the resolved group is
// for, and the second is the case worth drawing — a note that outlived its node.
const NOTES = [
  { note_id: "rvw_1", workflow_id: "wf_draft", step_id: "a", step_goal: "first", body: "start from the brief, not the outline", author: "roy", created_at: new Date().toISOString(), resolved: false, resolved_at: null, resolved_by: null, via: "rest", orphaned: false },
  { note_id: "rvw_2", workflow_id: "wf_draft", step_id: "zz", step_goal: "the step that went away", body: "this whole stage is premature", author: "roy", created_at: new Date().toISOString(), resolved: false, resolved_at: null, resolved_by: null, via: "rest", orphaned: true },
  { note_id: "rvw_3", workflow_id: "wf_draft", step_id: null, step_goal: null, body: "shape is right now", author: "roy", created_at: new Date().toISOString(), resolved: true, resolved_at: new Date().toISOString(), resolved_by: "roy", via: "rest", orphaned: false },
];
const AUDIT = [
  { seq: 1, at: new Date().toISOString(), event: "workflow.created", workflow_id: "wf_draft", run_id: null, amendment_id: null, detail: { via: "rest" } },
  { seq: 2, at: new Date().toISOString(), event: "workflow.approved", workflow_id: "wf_ok", run_id: null, amendment_id: null, detail: { decided_by: "roy", reason: "scope is right", via: "rest" } },
];

// Set by the second pass to reproduce an older Chief that has no /templates endpoint.
globalThis.NO_TEMPLATES = process.env.NO_TEMPLATES === "1";

/** What the page sent. A decision is a write, and the only way to assert it went out with
    what was typed is to keep the request. */
const posts = [];

// A 1x1 PNG and a markdown file, answered by the content route. Bytes plus a separate
// header naming what they may be shown as — never the response's own content type.
const PNG = Uint8Array.from(atob(
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
), (c) => c.charCodeAt(0));
const FILE_BODIES = {
  art_1: {
    text: "# From the file\n\nWith $x^2$ in it.\n\n```mermaid\ngraph TD; a-->b;\n```\n",
    type: "text/markdown", name: "personas.md",
  },
  art_3: {
    text: JSON.stringify({
      run: "sweep-3", steps: 4000, ok: true, note: null,
      held_out: { accuracy: 0.681, stale: true },
      shards: [{ id: 0, rows: 340 }, { id: 1, rows: 357 }],
      empty: {},
    }),
    type: "application/json", name: "metrics.json",
  },
  art_5: {
    text: 'import Chart from "./chart"\n\n## Findings\n\n<Callout kind="warn">\n\nThe split is **stale**.\n\n</Callout>\n\n<Chart data={rows} />\n',
    type: "text/mdx", name: "named.mdx",
  },
  art_4: {
    text: 'import { Callout } from "./Callout"\n\n## Findings\n\n<Callout kind="warn">\n\ntext\n\n</Callout>\n',
    type: "text/mdx", name: "post.mdx",
  },
};
// Two checked plans: one that holds up and one that does not, which is what the plans
// screens are for. The verified one carries the graph the server read back out of it, with a
// contract on the edge — a condition proven about an artifact nothing has produced yet.
const TOOLCHAIN = { available: true, toolchain: "leanprover/lean4:v4.33.1" };
const PLAN_GRAPH = {
  schema: "chief.plan/v1",
  title: "Fraud model refresh",
  nodes: [
    {
      id: "harvest", type: "task", goal: "Pull the events.", harness: "claude", group: "Data/Collection",
      criteria: ["count recorded"], fields: [], depends_on: [], inputs: [],
      produces: { label: "out", source: "harvest", artifact_type: "RawEvents", contract: "count ≥ 50000", refined: true },
    },
    {
      id: "fit_model", type: "task", goal: "Fit the classifier.", harness: "claude", group: "Data/Modelling",
      criteria: ["AUC recorded"], fields: [], depends_on: ["harvest"],
      inputs: [{
        label: "events", source: "harvest", artifact_type: "RawEvents",
        contract: "count ≥ 10000", refined: true,
        // The artifact's derived field layout, shown under the condition on both ends. The
        // row type's own fields nest under the field that carries it, as a disclosure.
        schema: [
          { name: "count", type: "Nat", fields: [] },
          {
            name: "events", type: "List Event",
            fields: [
              { name: "amount", type: "Nat", fields: [] },
              { name: "flagged", type: "Bool", fields: [] },
            ],
          },
        ],
      }],
      produces: {
        label: "out", source: "fit_model", artifact_type: "Model",
        contract: "auc ≥ 80", refined: true,
        schema: [{ name: "auc", type: "Nat", fields: [] }],
      },
      // The step's algorithm: rendered lines with indentation, and the external calls the
      // term reached for — pseudocode a reviewer reads, never something presented as proven.
      algorithm: {
        lines: [
          { indent: 0, text: "M ← xgboost(harvest, λ)" },
          { indent: 0, text: "if auc ≥ τ:" },
          { indent: 1, text: "return M" },
        ],
        externals: [{ tag: "algo", fn: "xgboost" }],
      },
    },
    {
      id: "publish", type: "task", goal: "Publish it.", harness: "claude",
      criteria: [], fields: [], depends_on: ["fit_model"], inputs: [], produces: null,
    },
  ],
  // What each group is for, where the plan says — shown as the group panel's summary.
  groups: [{ path: "Data/Modelling", description: "Fit and score the classifier." }],
  problems: [],
  stats: { nodes: 3, edges: 1, contracts_total: 3, contracts_refined: 3, contracts_any: 0 },
};
const PLANS = [
  {
    plan_id: "pln_ok", title: "Fraud model refresh", lean_source: "import ChiefPlan\n-- …\n",
    status: "verified", project: "chief", origin_dir: null, generated_by: null,
    verification: { status: "verified", diagnostics: [], graph: PLAN_GRAPH, toolchain: TOOLCHAIN.toolchain, axioms: ["propext"] },
    verified_at: "2026-08-24T09:00:00.000Z", compiled_to: [], stale: false,
    created_at: "2026-08-24T08:00:00.000Z", updated_at: "2026-08-24T09:00:00.000Z",
  },
  {
    plan_id: "pln_bad", title: "Docs index refresh",
    lean_source: Array.from({ length: 80 }, (_, i) => `-- line ${i + 1}`).join("\n"),
    status: "failed", project: "chief", origin_dir: null, generated_by: null,
    verification: {
      status: "failed",
      diagnostics: [
        { severity: "error", line: 71, column: 21, step_id: "evaluate",
          message: "unsolved goals\nhx : x.vectors ≥ 1000\n⊢ x.vectors ≥ 5000" },
        { severity: "warning", line: 24, column: 0, step_id: null,
          message: "contract 'crawled' is bound with `def`; use `abbrev`" },
      ],
      graph: null, toolchain: TOOLCHAIN.toolchain, axioms: [],
    },
    verified_at: "2026-08-24T09:01:00.000Z", compiled_to: [], stale: false,
    created_at: "2026-08-24T08:00:00.000Z", updated_at: "2026-08-24T09:01:00.000Z",
  },
];
let fileRequests = 0;
let moduleFetches = 0;
let runtimeFetches = 0;
// What the modules endpoint answers for the MDX artifact: the document and one component
// beside it, which is the shape the server derives from the file's own imports.
const MODULES = {
  art_4: {
    "post.mdx": 'import { Callout } from "./Callout"\n\n## Findings\n\n<Callout kind="warn">\n\ntext\n\n</Callout>\n',
    "./Callout": "export const Callout = ({children}) => <aside>{children}</aside>",
  },
};

globalThis.fetch = async (url, options) => {
  // The frame's runtime is inlined from Chief's own files, so opening an MDX document reads
  // them as text. Stubbed rather than read off disk: the smoke run asserts the wiring, and
  // scripts/test_jsx.mjs asserts what those files actually do.
  if (/(markdown|jsx|mdx-runtime)\.js$/.test(String(url))) {
    runtimeFetches += 1;
    return { ok: true, status: 200, text: async () => "/* stub */" };
  }
  const mods = /\/artifacts\/(\w+)\/modules$/.exec(url);
  if (mods) {
    moduleFetches += 1;
    // A document with nothing beside it answers 404, the way the server does when the file
    // is not an .mdx or has no graph — which is the case the named-frame fallback exists for.
    if (!MODULES[mods[1]]) {
      return { ok: false, status: 404, json: async () => ({ error: { message: "no modules" } }) };
    }
    return { ok: true, status: 200, json: async () => ({ modules: MODULES[mods[1]] }) };
  }
  const content = /\/artifacts\/(\w+)\/content$/.exec(url);
  if (content) {
    fileRequests += 1;
    const found = FILE_BODIES[content[1]];
    if (!found) {
      return { ok: false, status: 404, json: async () => ({ error: { message: "no file there" } }) };
    }
    const bytes = found.bytes || new TextEncoder().encode(found.text);
    return {
      ok: true, status: 200,
      headers: { get: (k) => ({ "X-Chief-Media-Type": found.type, "X-Chief-File-Name": found.name })[k] || null },
      arrayBuffer: async () => bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength),
    };
  }
  // Every write, not only the POSTs: resolving a note is a PATCH, and a smoke test that
  // only watched POSTs would call it green without ever seeing the request. A DELETE
  // carries no body at all, so the method alone has to be enough to record one.
  if (options && options.method && options.method !== "GET") {
    posts.push({ url, method: options.method, body: options.body ? JSON.parse(options.body) : null });
  }
  if (NO_TEMPLATES && url.endsWith("/templates")) {
    return { ok: false, status: 404, json: async () => ({ error: { code: "not_found" } }) };
  }
  const body =
    url.endsWith("/plans/toolchain") ? TOOLCHAIN
    : url.endsWith("/plans") ? PLANS
    : /\/plans\/[^/]+$/.test(url) ? PLANS[0]
    : url.includes("/plans/") ? { ...PLANS[0], compiled_to: ["wf_ok"] }
    : url.includes("/notes") ? NOTES
    : url.includes("/audit") ? AUDIT
    : url.endsWith("/templates") ? TEMPLATES
    : url.endsWith("/workflows") ? WORKFLOWS
    : url.endsWith("/runs") ? [RUN]
    : url.includes("/definition") ? { run_id: "run_1", workflow_id: "wf_ok", title: "Approved one", base_version: 2, applied_amendment_ids: [], steps: STEPS }
    : url.includes("/amendments") ? []
    : url.includes("/runs/") ? RUN
    : {};
  return { ok: true, status: 200, json: async () => body };
};

await import("../src/chief/web/app.js");
await new Promise((r) => setTimeout(r, 50));

/** Click the first element whose recorded handler is attached to a node matching `text`. */
function clickByText(text) {
  // Handlers accumulate across renders, so search the newest first: an old render's node is
  // detached and clicking it does nothing.
  const hit = [...clicks].reverse().find((c) => JSON.stringify(c.node).includes(text));
  if (!hit) throw new Error(`no clickable element containing ${text}`);
  hit.fn({ preventDefault() {}, stopPropagation() {} });
}

/** Click the first element under `root` whose text matches — for the times when the same
    word appears twice on a screen ("Save" is the project filing button *and* "Save as
    template"), and the newest-wins rule would pick the wrong one. */
function clickIn(root, text) {
  const found = [];
  (function walk(n) {
    if ((n.textContent || "") === text) found.push(n);
    for (const c of n.children || []) walk(c);
  })(root);
  for (const n of found) {
    const hit = [...clicks].reverse().find((c) => c.node === n);
    if (hit) return hit.fn({ preventDefault() {}, stopPropagation() {} });
  }
  throw new Error(`no clickable element under that subtree with text ${text}`);
}

/** Click the newest element whose class list contains every one of `classes`. Text is
    ambiguous for a column header ("Workflow" is also the nav link). */
/** The *button* carrying this text, not the first ancestor whose subtree contains it.

    `clickByText` searches whole subtrees, and children are built before their parents — so
    for a dialog it finds the backdrop's dismiss handler rather than the button inside it,
    and a test asserting "confirming deletes" silently asserts "dismissing does nothing". */
function clickButton(text) {
  const hit = [...clicks]
    .reverse()
    .find((c) => c.node.tag === "button" && JSON.stringify(c.node).includes(text));
  if (!hit) throw new Error(`no button containing ${text}`);
  hit.fn({ preventDefault() {}, stopPropagation() {} });
}

/** Returns what the handler did with the event, which is the only way to check a control
    nested inside another one: this DOM stub does not bubble, so asserting "the row did not
    open" would pass whether the handler stopped propagation or not. */
function clickByClass(...classes) {
  const has = (n) => classes.every((c) => (n.class || "").split(" ").includes(c));
  const hit = [...clicks].reverse().find((c) => has(c.node));
  if (!hit) throw new Error(`no clickable element with classes ${classes.join(" ")}`);
  const seen = { stopped: false, prevented: false };
  hit.fn({
    preventDefault() { seen.prevented = true; },
    stopPropagation() { seen.stopped = true; },
  });
  return seen;
}

/** Walk a rendered subtree counting nodes whose class matches — the screen labels below say
    the router works, this says the drawing survived. */
function countClass(root, cls) {
  let n = root.class && root.class.split(" ").includes(cls) ? 1 : 0;
  for (const child of root.children || []) n += countClass(child, cls);
  return n;
}

/** The sandbox the app would put on a frame for this URL. Re-derived rather than exported,
    because the rule is small and the point is to pin both sides of it. */
function frameSandboxOf(url) {
  const base = "allow-scripts allow-forms allow-popups allow-modals";
  return new URL(url, location.href).origin === location.origin ? base : `${base} allow-same-origin`;
}

/** Reload the page at a hash, the way a refresh would: the app rebuilds its state from the
    URL and fetches again, with nothing kept from before. */
async function reloadInto(hash) {
  // Away first, then back. The hashchange handler ignores a hash it already applied, so
  // navigating straight to the current one does nothing at all and would leave the drawer
  // that is already open standing — a test that then "passes" without reloading anything.
  location.hash = "#/workflows";
  fireWindow("hashchange", {});
  await new Promise((r) => setTimeout(r, 60));
  if (findByClass(roots.app, "viewer").length) throw new Error("leaving did not close the file");
  location.hash = hash;
  fireWindow("hashchange", {});
  await new Promise((r) => setTimeout(r, 120));
  const drawer = findByClass(roots.app, "viewer")[0];
  return drawer ? JSON.stringify(drawer).includes("metrics.json") : false;
}

/** Click the artifact path with exactly this text. */
function clickPath(text) {
  const node = findByClass(mainNode(), "art-open").find((n) => n.textContent === text);
  if (!node) throw new Error(`no openable path ${text}`);
  [...clicks].reverse().find((c) => c.node === node).fn({ preventDefault() {}, stopPropagation() {} });
}

/** Every node in a subtree with this tag. */
function findByTag(root, tag, out = []) {
  if (root.tag === tag) out.push(root);
  for (const c of root.children || []) findByTag(c, tag, out);
  return out;
}

/** Force the poll's re-render, to prove what survives one. */
async function refreshTick() {
  clickByText("Workflows");
  await new Promise((r) => setTimeout(r, 30));
  clickByText("Approved one");
  await new Promise((r) => setTimeout(r, 60));
}

/** Every node in a subtree whose class list contains `cls`. */
function findByClass(root, cls, out = []) {
  if ((root.class || "").split(" ").includes(cls)) out.push(root);
  for (const child of root.children || []) findByClass(child, cls, out);
  return out;
}

function countTag(root, tag) {
  let n = root.tag === tag ? 1 : 0;
  for (const child of root.children || []) n += countTag(child, tag);
  return n;
}

const mainNode = () => roots.app.children.find((c) => c.tag === "main");

/** Type into the field whose id is `id` — the checkpoint answers, which are addressed by
    step and field name rather than by the words on screen. */
function typeIntoId(id, value) {
  const hit = [...handlers].reverse().find((h) => h.type === "input" && h.node.id === id);
  if (!hit) throw new Error(`no input field with id ${id}`);
  hit.fn({ target: { value } });
}

/** Type into the field carrying `placeholder` — the list's filter box. */
function typeInto(placeholder, value) {
  const hit = [...handlers]
    .reverse()
    .find((h) => h.type === "input" && String(h.node.placeholder || "").includes(placeholder));
  if (!hit) throw new Error(`no input field with placeholder ${placeholder}`);
  hit.fn({ target: { value } });
}

/** The workflow titles, in the order the list drew them. */
function rowTitles(root = mainNode(), out = []) {
  if (root.class === "title") out.push(root.children[0]?.textContent);
  for (const child of root.children || []) rowTitles(child, out);
  return out;
}

/** Every class string in a subtree whose element carries `cls` — enough to assert that a
    variant was resolved, not merely that the base class was emitted. */
function collectClasses(root, cls, out = []) {
  if (root.class && root.class.split(" ").includes(cls)) out.push(root.class);
  for (const child of root.children || []) collectClasses(child, cls, out);
  return out;
}

const screens = [];
function record(label) {
  const main = roots.app.children.find((c) => c.tag === "main");
  screens.push(`${label} -> ${main ? main["data-screen-label"] : "?"}`);
}

// The brand is the mark itself, served beside the app, not a coloured square standing in
// for it — and the file has to actually be referenced, or the nav shows a broken image.
const navNode = roots.app.children.find((c) => c.tag === "nav");
const mark = navNode && collectClasses(navNode, "nav-mark");
if (!mark || mark.length !== 1) throw new Error("no mark in the nav brand");

// Refresh is a button, not a timer: the nav carries the one way the page reloads its data
// unprompted, and pressing it re-renders without losing the screen you were on.
if (countClass(navNode, "nav-refresh") !== 1) throw new Error("no refresh button in the nav");
clickByText("↻");
await new Promise((r) => setTimeout(r, 60));
if (countClass(mainNode(), "run-row") === 0)
  throw new Error("manual refresh lost the workflow list");

record("initial");
clickByText("A draft");
await new Promise((r) => setTimeout(r, 50));
record("open draft");
// The extraction is the whole risk of this change: assert the graph actually drew, rather
// than only that the route resolved. Three steps, one of them a loop with its own cluster.
const draftNodes = countClass(mainNode(), "node");
const draftClusters = countClass(mainNode(), "node-cluster");
// The loop flattens into the main graph on a draft: body steps as ordinary nodes, the
// construct as a gate the dashed return edge comes back to.
const draftGates = countClass(mainNode(), "gate");
// The gate is a decision: its two arrows are labelled with the exit condition and its
// negation, sourced from the loop's exit_when.
const draftEdgeLabels = countClass(mainNode(), "gate-edge-label");
// Feedback on the draft. It hangs off the node it is about, the way a comment hangs off the
// post it is under — so nothing is on screen until a node is clicked.
// The badge says which nodes have something to read without opening every one.
const noteBadges = countClass(mainNode(), "node-notes");

// The plan-level thread is the panel you get with nothing selected: the orphan lives here
// too, because the step it was left on is gone and there is no node left to open it from.
const planNotes = countClass(mainNode(), "note");
const orphanShown = JSON.stringify(mainNode()).includes("was on zz");

// Click the step that has a note. Its thread comes up beside it, in the inspector.
clickByText("first");
await new Promise((r) => setTimeout(r, 20));
const stepNotes = countClass(mainNode(), "note");
const stepNoteShown = JSON.stringify(mainNode()).includes("start from the brief");

// Leaving one: the box is on the node, so there is nothing to say what it is about.
clickByText("＋ another note");
await new Promise((r) => setTimeout(r, 10));
// A note is a sentence or two, so the box is a resizable textarea rather than a one-line
// field. Asserted by tag: a silent revert to <input> would otherwise keep this test green.
const box = [...handlers].reverse().find((h) => h.type === "input" && h.node.id === "note-a");
const boxIsTextarea = !!box && box.node.tag === "textarea";
typeIntoId("note-a", "check it against last quarter's numbers");
await new Promise((r) => setTimeout(r, 10));
// And Enter puts in a newline instead of sending, which is the point of the change. Only
// the modifier submits.
const before = posts.length;
const keys = [...handlers].reverse().find((h) => h.type === "keydown" && h.node.id === "note-a");
keys.fn({ key: "Enter" });
await new Promise((r) => setTimeout(r, 20));
const plainEnterHeld = posts.length === before;
clickByText("Add");
await new Promise((r) => setTimeout(r, 40));
const noteSent = posts.find((x) => x.url.includes("/notes") && x.method === "POST");

// Closing one is the human's, not the harness's — so it has to be here, in front of them.
// This is the step's own note, because the step's thread is what is open.
clickByText("resolve");
await new Promise((r) => setTimeout(r, 40));
const noteResolved = posts.find((x) => x.method === "PATCH");

// Getting back to the plan's own thread with a node still selected. This is the whole
// reason the button exists: the plan panel is what you get with nothing selected, which is
// true when you arrive and false the moment you click a node — so without a way back,
// feedback about the plan reads as having disappeared after the first click.
clickByText("Feedback on the plan");
await new Promise((r) => setTimeout(r, 20));
const planReachable = JSON.stringify(mainNode()).includes("Feedback on the plan");
const planComposeBack = findByClass(mainNode(), "note-input").length === 0 &&
  JSON.stringify(mainNode()).includes("＋ another note");
const orphanBack = JSON.stringify(mainNode()).includes("was on zz");

clickByText("resolved (1)");
await new Promise((r) => setTimeout(r, 10));
const resolvedShown = JSON.stringify(mainNode()).includes("shape is right now");

clickByText("Approve");
await new Promise((r) => setTimeout(r, 20));
record("approve dialog");

const dialogOpened = roots["dialog-root"].children.length > 0;

// Escape the dialog, then the regression that matters: run detail still renders through the
// extracted graph, with step states colouring it and an instance cluster on the loop.
clickByText("Cancel");
await new Promise((r) => setTimeout(r, 20));
clickByText("Workflows");
await new Promise((r) => setTimeout(r, 30));
record("back to list");

// The list is filtered and sorted in the page. Default view: everything but the archived.
const listRows = countClass(mainNode(), "run-row");
if (listRows !== 2) throw new Error(`expected 2 active rows, got ${listRows}`);
if (countClass(mainNode(), "list-head") !== 1) throw new Error("no sortable column header");

// The list opens on last-updated, newest first. "Approved one" was touched last by its
// running execution; the draft has not moved since it was written.
if (rowTitles().join() !== "Approved one,A draft")
  throw new Error(`default sort: got ${rowTitles()}`);

// Header and rows are one panel, and each status is a word with a tone rather than a dot.
if (countClass(mainNode(), "list-wrap") !== 1) throw new Error("rows are not in one panel");
const badgeTones = collectClasses(mainNode(), "badge").map((c) =>
  (c.match(/\bb-\w+/) || ["?"])[0],
);
// The draft is awaiting approval (accent), the approved one is mid-run (accent) — both
// pulse; what is asserted is that a tone was resolved at all rather than defaulting.
if (badgeTones.length !== 2 || badgeTones.some((t) => t === "?"))
  throw new Error(`status badges: got ${JSON.stringify(badgeTones)}`);
if (countClass(mainNode(), "dot") !== 0)
  throw new Error("a status dot survived in the workflow list");

// "Needs you" is one question, not three: an unapproved draft and a run stopped at a
// checkpoint are both waiting on the same person.
clickByText("Needs you");
await new Promise((r) => setTimeout(r, 10));
if (rowTitles().join() !== "Approved one,A draft") throw new Error(`filter chip: got ${rowTitles()}`);

// Deleting from a row: the control is in the row, its click must not open the workflow
// underneath it, and nothing is sent until the confirmation is accepted.
const rowDelete = clickByClass("row-del");
await new Promise((r) => setTimeout(r, 20));
if (!rowDelete.stopped)
  throw new Error("the row-delete let its click through — the workflow opens underneath");
if (roots["dialog-root"].children.length === 0) throw new Error("no delete confirmation");
if (posts.some((x) => x.method === "DELETE")) throw new Error("deleted before confirming");
clickButton("Cancel");
await new Promise((r) => setTimeout(r, 20));
if (roots["dialog-root"].children.length !== 0) throw new Error("cancel left the dialog up");
if (posts.some((x) => x.method === "DELETE")) throw new Error("cancel still deleted");

clickByClass("row-del");
await new Promise((r) => setTimeout(r, 20));
clickButton("Delete workflow");
await new Promise((r) => setTimeout(r, 30));
const deleted = posts.find((x) => x.method === "DELETE");
if (!deleted) throw new Error("confirming sent no DELETE");
if (!/\/workflows\/wf_\w+$/.test(deleted.url))
  throw new Error(`DELETE went to ${deleted.url}`);

clickByText("Workflows");
await new Promise((r) => setTimeout(r, 30));
clickByText("All");
await new Promise((r) => setTimeout(r, 10));
typeInto("Filter by name", "archived");
await new Promise((r) => setTimeout(r, 10));
if (rowTitles().join() !== "Archived one") throw new Error(`search: got ${rowTitles()}`);
typeInto("Filter by name", "");
await new Promise((r) => setTimeout(r, 10));

// Sorting by name, then the same header again, reverses the list.
clickByClass("col-title");
await new Promise((r) => setTimeout(r, 10));
const byName = rowTitles().join();
clickByClass("col-title");
await new Promise((r) => setTimeout(r, 10));
const reversed = rowTitles().join();
if (byName !== "A draft,Approved one,Archived one" || reversed !== [...byName.split(",")].reverse().join())
  throw new Error(`sort by name: ${byName} then ${reversed}`);
// Added: newest first, and every workflow has one whether or not it has ever run.
clickByClass("col-added");
await new Promise((r) => setTimeout(r, 10));
if (rowTitles().join() !== "A draft,Archived one,Approved one")
  throw new Error(`sort by added: ${rowTitles()}`);
clickByClass("col-added");
await new Promise((r) => setTimeout(r, 10));
if (rowTitles().join() !== "Approved one,Archived one,A draft")
  throw new Error(`sort by added, reversed: ${rowTitles()}`);

// Duration is the execution's, so only the workflow that ran has one — and the ones that
// never ran sort last whichever way the column points.
clickByClass("col-duration");
await new Promise((r) => setTimeout(r, 10));
if (rowTitles()[0] !== "Approved one") throw new Error(`sort by duration: ${rowTitles()}`);
const durations = [];
(function walk(n) {
  if (n.class === "dur") durations.push(n.textContent);
  for (const c of n.children || []) walk(c);
})(mainNode());
if (durations.join() !== "<1m,,") throw new Error(`durations: ${JSON.stringify(durations)}`);

// Projects. One chip per label in use, plus the ones nobody has filed — the bucket that
// has to exist, because every workflow that predates projects is in it and a filter that
// hid them would hide most of the history.
const projectChipRow = findByClass(mainNode(), "chips-project")[0];
const chipLabels = (projectChipRow.children || []).map((c) => c.textContent);
clickByText("Unfiled");
await new Promise((r) => setTimeout(r, 20));
const unfiledOnly = rowTitles().join();
clickByText("chief 1");
await new Promise((r) => setTimeout(r, 20));
const chiefOnly = rowTitles().join();
clickByText("Every project");
await new Promise((r) => setTimeout(r, 20));
const backToAll = rowTitles().length;

clickByText("Active");
await new Promise((r) => setTimeout(r, 10));

// The merge: an executing workflow is the same screen, with state on the plan.
clickByText("Approved one");
await new Promise((r) => setTimeout(r, 60));
record("open running workflow");
// The URL addresses what you are looking at, so a reload comes back to this workflow.
if (!location.hash.startsWith("#/workflow/wf_")) {
  throw new Error(`expected a workflow hash, got ${JSON.stringify(location.hash)}`);
}
const runNodes = countClass(mainNode(), "node");
const runClusters = countClass(mainNode(), "node-cluster");
// The cycle survives execution: body steps stay drawn, the gate carries the instances.
const runGates = countClass(mainNode(), "gate");

// Artifacts: a path you can act on. The relative ref resolves against the stored folder and
// becomes an editor link; the http one is left exactly as the harness reported it. Both get
// a copy button, because copying works even when nothing can open the file.
const paths = findByClass(mainNode(), "art-path");
// The editor link is the small ↗ beside the path now; the path itself opens the file here.
// A web ref has neither — it is an ordinary link, and its href is on the path.
const hrefs = paths.map((p) => {
  const edit = findByClass(p, "art-edit")[0];
  return edit ? edit.href : (findByClass(p, "art-href")[0] || {}).href;
});
const copyButtons = paths.reduce((n, p) => n + findByClass(p, "art-copy").length, 0);
// What lands on the clipboard is the absolute path, not the relative one — the whole point
// is to hand over something that means something outside this page.
let copied = null;
// node ships a real `navigator` and it is getter-only, so the stub goes on the property it
// actually lacks rather than on the object.
Object.defineProperty(globalThis.navigator, "clipboard", {
  configurable: true,
  value: { writeText: async (t) => { copied = t; } },
});
const copyNode = findByClass(mainNode(), "art-copy")[0];
[...clicks].reverse().find((c) => c.node === copyNode)?.fn({ currentTarget: copyNode });
await new Promise((r) => setTimeout(r, 10));

// Naming the folder is the only interactive part of this, so drive it: open the field, type
// a new root with a trailing slash on it, save. What should come back is a stored value with
// the slash gone and links rebuilt underneath it — under this project's own key, because one
// folder for every project resolves the wrong tree the moment there are two.
clickByText("Change");
await new Promise((r) => setTimeout(r, 10));
typeIntoId("files-root", "/elsewhere/tree/");
await new Promise((r) => setTimeout(r, 10));
clickByText("Save");
await new Promise((r) => setTimeout(r, 20));
const rootSaved = stored["chief.filesRoot:chief"];
const rehomed = findByClass(mainNode(), "art-edit")
  .map((a) => a.href)
  .includes("vscode://file/elsewhere/tree/notes/personas.md");

// And with no folder named at all, the path is text. A link built on a guessed base opens a
// "file not found" in the editor, which reads as the editor being broken.
clickByText("Change");
await new Promise((r) => setTimeout(r, 10));
typeIntoId("files-root", "");
await new Promise((r) => setTimeout(r, 10));
clickByText("Save");
await new Promise((r) => setTimeout(r, 20));
// With no folder named there is nothing to build an editor link on, so the ↗ goes from every
// *file* — a `vscode://` URL on a guessed base opens a "file not found", which reads as the
// editor being broken. The web artifact keeps its ↗, because a tab needs no folder. Paths
// stay openable regardless: viewing goes through the server, which resolves against what the
// workflow recorded rather than anything held in here.
const editLinks = findByClass(mainNode(), "art-edit");
const unlinked = editLinks.length === 1 && editLinks[0].href === "https://example.com/pr/1";
const stillOpenable = countClass(mainNode(), "art-open") === 5;
// The copy button does not go away with the link: what the harness reported is still worth
// having on the clipboard.
const stillCopyable = countClass(mainNode(), "art-copy") === 5;

// Markdown, rendered rather than dumped as one run-on line: a heading, a hard break where
// the harness put a newline, emphasis, a code span, a list, and maths as MathML.
const mdParas = countClass(mainNode(), "md-p");
const mdHeads = countClass(mainNode(), "md-h");
const mdCode = countClass(mainNode(), "md-code");
const mdBreaks = countTag(mainNode(), "br");
const mdBold = countTag(mainNode(), "strong");
const mdMath = countTag(mainNode(), "math");
const mdRaw = countClass(mainNode(), "math-raw");

// A long description wraps and is clamped, with a control to open it out. The old
// behaviour was one line ending in an ellipsis and no way past it.
const labelClipped = findByClass(mainNode(), "art-label").some((n) => (n.class || "").includes("clipped"));
// Exactly one of the two: the short one must not be clamped, because nothing would offer
// to unclamp it — which is the bug being fixed, not a smaller version of it.
const onlyLongClipped =
  findByClass(mainNode(), "art-label").filter((n) => (n.class || "").includes("clipped")).length === 1 &&
  countClass(mainNode(), "art-more") === 1;
const moreButton = findByClass(mainNode(), "art-more")[0];
// The cut-off text is itself the control, which is what a person tries before hunting for a
// button. Driving it that way rather than through the button asserts both at once: the
// button exists, and clicking the text does what the button does.
const clippedLabel = findByClass(mainNode(), "art-label").find((n) => (n.class || "").includes("clipped"));
[...clicks].reverse().find((c) => c.node === clippedLabel)?.fn({ preventDefault() {}, stopPropagation() {} });
await new Promise((r) => setTimeout(r, 20));
const labelOpened = findByClass(mainNode(), "art-label").every((n) => !(n.class || "").includes("clipped"));

// A comment already on an artifact is shown, and leaving a new one posts it addressed by
// artifact id — the only handle that survives artifacts being flattened into one list.
const commentShown = JSON.stringify(mainNode()).includes("the tone here is the one to match");
clickByText("＋ another comment");
await new Promise((r) => setTimeout(r, 10));
typeIntoId("cmt-art_1", "check the second half against the brief");
await new Promise((r) => setTimeout(r, 10));
clickByText("Add");
await new Promise((r) => setTimeout(r, 40));
const comment = posts.find((x) => x.url.includes("/comments"));

// Metadata a harness attached, which used to be stored and shown nowhere at all. On the
// step, on an instance, and on an artifact — three places it can arrive and three places it
// was invisible. Selecting a node changes the inspector, so the selection is put back.
clickByText("did it");
await new Promise((r) => setTimeout(r, 30));
// The criteria checklist, on the step that ran. Both conditions were answered, so both
// read as met and both carry the evidence — a tick with nothing behind it would be the
// metadata problem again: present in the DOM, useless to a reader.
const critRows = findByClass(mainNode(), "criterion");
const critShown = critRows.length === 2 &&
                  critRows.every((n) => JSON.stringify(n).includes("met")) &&
                  JSON.stringify(mainNode()).includes("all nine recorded") &&
                  JSON.stringify(mainNode()).includes("every persona has a voice note") &&
                  JSON.stringify(mainNode()).includes("Done when (2/2)");
// And a criterion nothing has answered reads as outstanding rather than silently absent —
// the state a reader needs before approving a plan.
clickByText("then check it");
await new Promise((r) => setTimeout(r, 30));
const outRows = findByClass(mainNode(), "criterion");
const critOutstanding = outRows.length === 1 &&
                        !JSON.stringify(outRows[0]).includes("criterion met") &&
                        JSON.stringify(mainNode()).includes("Done when (0/1)");
// A fixed input, readable on a step that has never run — no run and no artifact_id behind
// it, so this exercises the degraded (editor-link, no in-page viewer) path through the same
// artifact card an output gets.
const inputArtShown = JSON.stringify(mainNode()).includes("Checklist to verify against") &&
                      JSON.stringify(mainNode()).includes("notes/checklist.md") &&
                      JSON.stringify(mainNode()).includes("Inputs (1)");
const inputMetaShown = findByClass(mainNode(), "meta-json")
  .some((n) => JSON.stringify(n).includes("threshold"));
clickByText("did it");
await new Promise((r) => setTimeout(r, 30));
const stepMetaShown = JSON.stringify(mainNode()).includes("41200") &&
                      countClass(mainNode(), "meta-json") === 1;
const stepMetaFolds = countClass(mainNode(), "j-node") > 0;
// An artifact's descriptive `data` reads inline, like an instance's — it is the harness
// saying what it produced, and a fold labelled "data" is not something anyone finds.
const artFactsInline = findByClass(mainNode(), "meta-pair")
  .some((n) => JSON.stringify(n).includes("additions"));
// An instance's metadata is read without a click: what a harness attaches to a branch is
// what tells that branch from the others, so hiding it leaves eight rows labelled
// "Branch 1..8" with the useful field one click away each.
clickByText("each thing");
await new Promise((r) => setTimeout(r, 30));
// The construct declares what each iteration must supply, and each instance reads its own
// body with its own value in place. Without this a run shows "Iteration 1..8" with nothing
// telling them apart — which is exactly what the wf_ablate demo looks like today.
const instLabelled = JSON.stringify(mainNode()).includes("Iteration 1 · arxiv:2401.11111");
const paramDeclared = JSON.stringify(mainNode()).includes("which paper this iteration reads");
const filled = findByClass(mainNode(), "inst-filled");
const paramsFilled = filled.length === 1 &&
                     JSON.stringify(filled[0]).includes("read arxiv:2401.11111 end to end") &&
                     !JSON.stringify(filled[0]).includes("{{");
const instPairs = findByClass(mainNode(), "meta-pair").map((n) => JSON.stringify(n));
const instInline = instPairs.some((p) => p.includes("seed") && p.includes("7"));
// Scalars go inline; the whole of it — nested values included — is one click away in the
// drawer, through the same tree a JSON artifact gets. A summary with no route to the rest
// is the fold problem in a different shape.
const openAll = findByClass(mainNode(), "meta-open")[0];
[...clicks].reverse().find((c) => c.node === openAll).fn({ preventDefault() {}, stopPropagation() {} });
await new Promise((r) => setTimeout(r, 40));
const metaDrawer = findByClass(roots.app, "viewer")[0];
const instDeepInDrawer = !!metaDrawer && JSON.stringify(metaDrawer).includes("deep") &&
                         countClass(metaDrawer, "viewer-json") === 1;
// Opened from a value, so nothing was fetched for it.
const metaNoFetch = fileRequests === 0;
clickIn(findByClass(roots.app, "viewer")[0], "✕");
await new Promise((r) => setTimeout(r, 20));
clickByText("each thing");
await new Promise((r) => setTimeout(r, 30));
clickByText("did it");
await new Promise((r) => setTimeout(r, 30));


// The file viewer: click the ◱ beside a path and the drawer opens on the file itself. The
// bytes come from the server, which is the only arrangement that works when the UI is
// reached through a tunnel from the machine the files are on.
const viewButtons = countClass(mainNode(), "art-open");
// Clicking the path, not a control beside it. That is the change: a reader clicks the name
// of a thing to see the thing. Named explicitly rather than taken by position — there is
// more than one openable path on this screen, and "the newest" is not a stable target.
clickPath("notes/personas.md");
await new Promise((r) => setTimeout(r, 40));
const drawer = findByClass(roots.app, "viewer")[0];
const viewerOpened = !!drawer;
// Markdown from a file goes through the same renderer as an inline body.
const viewerRendered = drawer && countClass(drawer, "md-p") > 0 && countTag(drawer, "math") === 1;
const viewerTitle = drawer && JSON.stringify(drawer).includes("personas.md");
// A ```mermaid fence is left as a `.mermaid` block for the runtime to draw — this stub DOM
// never loads that runtime, so what is checked here is only that the fence reached the page
// as that block rather than as a plain, unlabelled code dump.
const viewerMermaid = drawer && countClass(drawer, "mermaid") === 1;
// A URL artifact is framed, not fetched: the page renders itself, with the components the
// project that owns them actually builds. Nothing is read off the disk and nothing is
// evaluated by Chief.
clickIn(findByClass(roots.app, "viewer")[0], "✕");
await new Promise((r) => setTimeout(r, 20));
const fetchesBeforeFrame = fileRequests;
clickPath("https://example.com/pr/1");
await new Promise((r) => setTimeout(r, 40));
const frame = findByTag(findByClass(roots.app, "viewer")[0], "iframe")[0];
const framed = !!frame && frame.src === "https://example.com/pr/1";
// The page is inset by the drawer's width rather than covered by it, so the artifact list
// you opened this from is still there to open the next one.
const pageInset = cssVars["--viewer-right"];
const frameNotFetched = fileRequests === fetchesBeforeFrame;
// Its own origin is kept, so a dev server still has its storage and its own API calls —
// which is different from being same-origin with Chief, and the browser still separates
// those two.
const frameSandboxed = !!frame && frame.sandbox.includes("allow-scripts") &&
                       frame.sandbox.includes("allow-same-origin");

// And a URL on Chief's own origin is refused that flag: allow-scripts plus allow-same-origin
// on a same-origin frame lets it reach out of the sandbox and into this page.
const selfFrame = frameSandboxOf("http://localhost:8080/ui/index.html");
const crossFrame = frameSandboxOf("https://example.com/x");

// MDX with components beside it: compiled and run inside a sandboxed frame at an opaque
// origin, so a document's own code executes somewhere it cannot reach this page.
clickIn(findByClass(roots.app, "viewer")[0], "✕");
await new Promise((r) => setTimeout(r, 20));
clickPath("notes/post.mdx");
await new Promise((r) => setTimeout(r, 60));
const mdxV = findByClass(roots.app, "viewer")[0];
const mdxIframe = findByTag(mdxV, "iframe")[0];
const mdxCompiled = !!mdxIframe && !!mdxIframe.srcdoc;
// No allow-same-origin: components a harness wrote must not reach Chief's page or storage.
const mdxSandboxed = !!mdxIframe && mdxIframe.sandbox === "allow-scripts allow-popups";
// srcdoc, not a URL: the frame fetches nothing, so nothing it runs can be swapped underneath.
const mdxSelfContained = !!mdxIframe && !mdxIframe.src &&
  mdxIframe.srcdoc.includes("ChiefMDX.renderMdx") && mdxIframe.srcdoc.includes("post.mdx");
// One graph fetch per MDX document, and the runtime read once for its three files. Captured
// here rather than read at the end, where the totals include the second document.
const mdxModuleFetches = moduleFetches;
const mdxAsked = mdxModuleFetches === 1 && runtimeFetches === 3;
clickIn(findByClass(roots.app, "viewer")[0], "✕");
await new Promise((r) => setTimeout(r, 20));

// The same format with nothing beside it: no modules, so no runtime, and it falls back to
// prose with its components named. Losing that fallback would mean a document with a missing
// sibling showing nothing at all.
clickPath("notes/named.mdx");
await new Promise((r) => setTimeout(r, 40));
const mdxDrawer = findByClass(roots.app, "viewer")[0];
const mdxNodes = countClass(mdxDrawer, "mdx-node");
const mdxImportsFolded = countClass(mdxDrawer, "md-module") === 1;
const mdxProseKept = JSON.stringify(mdxDrawer).includes("The split is") &&
                     countTag(mdxDrawer, "strong") === 1;
const mdxExprShown = JSON.stringify(mdxDrawer).includes("data={rows}");
clickPath("notes/personas.md");
await new Promise((r) => setTimeout(r, 40));

// The open file is in the URL, so it can be linked to and survives a reload.
const hashWithFile = location.hash;
clickIn(findByClass(roots.app, "viewer")[0], "✕");
await new Promise((r) => setTimeout(r, 20));
const hashAfterClose = location.hash;

// JSON folds rather than scrolling. Native <details>, one per object or array, with the
// top two levels open — enough to see the shape without the whole file springing at you.
clickPath("data/metrics.json");
await new Promise((r) => setTimeout(r, 40));
const jsonDrawer = findByClass(roots.app, "viewer")[0];
const jsonNodes = countTag(jsonDrawer, "details");
const jsonOpenAtStart = findByTag(jsonDrawer, "details").filter((d) => d.open !== undefined && d.open !== null).length;
// An empty object is a leaf, not a foldable node with nothing in it.
const jsonEmptyIsLeaf = countClass(jsonDrawer, "j-empty") === 1;
const jsonTyped = countClass(jsonDrawer, "j-str") > 0 && countClass(jsonDrawer, "j-num") > 0 &&
                  countClass(jsonDrawer, "j-bool") > 0 && countClass(jsonDrawer, "j-null") === 1;
// The built body is kept, so a poll cannot spring every folded branch back open.
const keptNode = findByClass(roots.app, "viewer")[0].children.length > 0;
await refreshTick();
const jsonSurvives = countTag(findByClass(roots.app, "viewer")[0], "details") === jsonNodes;
const jsonHash = location.hash;

// And a reload lands on it: the state is rebuilt from the hash alone, with nothing carried
// over, and the artifact reopens once the run it hangs off has been fetched.
// Counted from here, so the number means "what the reload cost" rather than a running total
// that every future edit to this file would have to keep in step.
fileRequests = 0;
const reopened = await reloadInto(jsonHash);
const reloadFetches = fileRequests;
// A link kept from a different run names an artifact this one does not have. It is dropped
// rather than retried — a pending id that survived would re-open on every poll forever —
// and the URL heals itself back to the workflow.
await reloadInto("#/workflow/wf_ok/art_nope");
const staleIgnored = findByClass(roots.app, "viewer").length === 0;
const staleHealed = location.hash === "#/workflow/wf_ok";
// Back to an open file for the resize below.
clickPath("data/metrics.json");
await new Promise((r) => setTimeout(r, 40));

// Resizable from its left edge, like the inspector, and the width is remembered.
const vwBefore = parseInt(findByClass(roots.app, "viewer")[0].style.width, 10);
const vwGrip = findByClass(roots.app, "viewer")[0].children.find((c) => (c.class || "").includes("viewer-grip"));
[...handlers].reverse()
  .find((h) => h.type === "pointerdown" && h.node === vwGrip)
  .fn({ clientX: 900, preventDefault() {} });
fireWindow("pointermove", { clientX: 700 });
const vwDuringDrag = parseInt(findByClass(roots.app, "viewer")[0].style.width, 10);
fireWindow("pointerup", {});
await new Promise((r) => setTimeout(r, 20));
const vwAfter = parseInt(findByClass(roots.app, "viewer")[0].style.width, 10);
const vwStored = Number(stored["chief.viewerWidth"]);

// Closing releases the drawer entirely rather than leaving an empty one in the page.
clickIn(findByClass(roots.app, "viewer")[0], "✕");
await new Promise((r) => setTimeout(r, 20));
const viewerClosed = findByClass(roots.app, "viewer").length === 0;

// The panel is resizable from its left edge. Dragged leftwards it grows, because the handle
// is on that edge and the panel follows the pointer into the space it opens.
const handle = findByClass(mainNode(), "split-handle")[0];
const grab = [...handlers].reverse().find((h) => h.type === "pointerdown" && h.node === handle);
const inspectorBefore = parseInt(findByClass(mainNode(), "inspector")[0].style.width, 10);
grab.fn({ clientX: 1000, preventDefault() {} });
fireWindow("pointermove", { clientX: 880 });
// Mid-drag the node is moved directly, without a render — a setState per pointermove would
// tear down whatever field is being typed into elsewhere on the screen.
const widthDuringDrag = parseInt(findByClass(mainNode(), "inspector")[0].style.width, 10);
fireWindow("pointerup", {});
await new Promise((r) => setTimeout(r, 20));
const inspectorAfter = parseInt(findByClass(mainNode(), "inspector")[0].style.width, 10);
const widthStored = Number(stored["chief.inspectorWidth"]);
// And it cannot be dragged narrower than its contents: clamped, not merely discouraged.
// A fresh handle, because the render after the first drag replaced the one above.
const handle2 = findByClass(mainNode(), "split-handle")[0];
[...handlers].reverse()
  .find((x) => x.type === "pointerdown" && x.node === handle2)
  .fn({ clientX: 1000, preventDefault() {} });
fireWindow("pointermove", { clientX: 3000 });
fireWindow("pointerup", {});
await new Promise((r) => setTimeout(r, 20));
const clamped = parseInt(findByClass(mainNode(), "inspector")[0].style.width, 10);

// Filing a workflow under a project, from the screen you are looking at it on. Allowed at
// any status: the workflows most in need of filing are the ones that already ran.
const originShown = JSON.stringify(mainNode()).includes("made in /w/chief");
clickByText("refile…");
await new Promise((r) => setTimeout(r, 20));
typeIntoId("wf-project", "chief-ui");
await new Promise((r) => setTimeout(r, 10));
// Scoped: "Save as template" is on this screen too, and is rendered later.
clickIn(findByClass(mainNode(), "wf-project")[0], "Save");
await new Promise((r) => setTimeout(r, 30));
// And the directory, separately: setting one must not clear the other, which is the whole
// reason the server tells an omitted field from an explicit null.
clickByText("change…");
await new Promise((r) => setTimeout(r, 20));
typeIntoId("wf-origin_dir", "/w/elsewhere");
await new Promise((r) => setTimeout(r, 10));
clickIn(findByClass(mainNode(), "wf-project")[0], "Save");
await new Promise((r) => setTimeout(r, 40));
const patches = posts.filter((x) => x.method === "PATCH" && x.url.includes("/workflows/wf_ok"));
const filed = patches[0];
const dirSet = patches[1];

if (NO_TEMPLATES) {
  // A page newer than its server must still work: one 404 on an extension endpoint should
  // not leave every screen on "Loading…".
  console.log(screens.join("\n"));
  const reached = screens.map((x) => x.split(" -> ")[1]);
  const ok404 =
    reached[0] === "Workflows" &&
    reached.includes("Workflow detail") &&
    draftNodes === 5;
  console.log(ok404 ? "PASS (no templates: everything else still works)" : "FAIL (stuck)");
  process.exit(ok404 ? 0 : 1);
}

// The checkpoint the run is stopped at: it draws as a node marked waiting, and the inbox
// asks the question, takes the answer, and sends it.
const waitingNodes = countClass(mainNode(), "checkpoint");
// No step here declares a group, so nothing is drawn round anything and the layout is the
// one it always was — grouping must not move a plan that never asked for it.
const ungroupedBoxes = countClass(mainNode(), "group-box");

clickByText("Approvals");
await new Promise((r) => setTimeout(r, 30));
record("nav Approvals");
const asked = JSON.stringify(mainNode()).includes("ship it?");
typeIntoId("cp-run_1:e-budget", "$400");
await new Promise((r) => setTimeout(r, 10));
typeIntoId("cp-run_1:e-note", "go ahead");
await new Promise((r) => setTimeout(r, 10));
clickByText("Approve");
await new Promise((r) => setTimeout(r, 40));
const decision = posts.find((x) => x.url.includes("/resolutions/"));

// Templates: the reusable plan, drawn through the same renderer.
clickByText("Templates");
await new Promise((r) => setTimeout(r, 30));
record("nav Templates");
clickByText("Triage");
await new Promise((r) => setTimeout(r, 40));
record("open template");
const templateNodes = countClass(mainNode(), "node");
clickByText("Use this template");
await new Promise((r) => setTimeout(r, 20));
const paramFields = roots["dialog-root"].children.reduce((n, c) => n + countTag(c, "input"), 0);

// Exporting a template: the browser writes the file, not the server. What comes out has to
// be exactly what POST /templates takes, or the file is a dead end rather than something
// that can be committed and registered again.
clickByText("Export to a file");
await new Promise((r) => setTimeout(r, 20));
const exportedBody = exported ? JSON.parse(await exported.text()) : null;

// A plan wider than the window. Added now rather than up top so the list assertions above
// keep counting the three workflows they were written for.
WORKFLOWS.push({
  created_at: "2026-04-01T09:00:00Z", updated_at: "2026-04-01T09:00:00Z",
  workflow_id: "wf_wide", title: "Wide fan-out", source: "generated",
  generated_by: "claude-code", status: "approved", version: 1, steps: WIDE_STEPS,
});
clickByText("Workflows");
await new Promise((r) => setTimeout(r, 40));
clickByText("Wide fan-out");
await new Promise((r) => setTimeout(r, 60));
record("open wide plan");
const wideNodes = countClass(mainNode(), "node");
// The plane is drawn at the width the plan needs, and the viewport is told to scroll rather
// than clipping it. Without this the right-hand shards are simply not reachable.
const wideViewport = findByClass(mainNode(), "graph-viewport")[0];
const widePlane = findByClass(mainNode(), "graph-plane")[0];
const planeWidth = parseInt(widePlane.style.width, 10);
const viewportScrolls = (wideViewport.class || "").includes("scrolls");

console.log(screens.join("\n"));
console.log(`template graph: ${templateNodes} nodes, ${paramFields} parameter fields`);
console.log("workflow dialog opened:", dialogOpened);
console.log(`draft graph: ${draftNodes} nodes, ${draftGates} loop gates, ${draftEdgeLabels} branch labels`);
console.log(`run graph:   ${runNodes} nodes, ${runClusters} instance clusters`);
console.log(`checkpoint:  ${waitingNodes} node, asked=${asked}, sent=${JSON.stringify(decision && decision.body)}`);
console.log(`artifacts:   ${paths.length} paths, ${copyButtons} copy buttons, ${viewButtons} openable, copied=${copied}`);
console.log(`             ${JSON.stringify(hrefs)}`);
console.log(`instances:   labelled=${instLabelled}, declared=${paramDeclared}, body-filled=${paramsFilled}`);
console.log(`criteria:    met-on-run=${critShown}, outstanding-on-draft=${critOutstanding}`);
console.log(`inputs:      artifact-shown=${inputArtShown}, plain-shown=${inputMetaShown}`);
console.log(`metadata:    step shown=${stepMetaShown}, folds=${stepMetaFolds}, artifact facts inline=${artFactsInline}, instance inline=${instInline}, full json in drawer=${instDeepInDrawer}`);
console.log(`viewer:      ${viewButtons} openable paths, opened=${viewerOpened}, rendered=${viewerRendered}, mermaid=${viewerMermaid}, titled=${viewerTitle}, closed=${viewerClosed}, fetches=${fileRequests}`);
console.log(`url:          open=${hashWithFile} closed=${hashAfterClose} json=${jsonHash}, reload reopens=${reopened} in ${reloadFetches} fetch, stale id dropped=${staleIgnored} and healed=${staleHealed}`);
console.log(`frame:        framed=${framed}, not fetched=${frameNotFetched}, page inset=${pageInset}, sandbox="${frame && frame.sandbox}"`);
console.log(`mdx run:      compiled=${mdxCompiled}, sandbox="${mdxIframe && mdxIframe.sandbox}", self-contained=${mdxSelfContained}, fetches modules=${mdxModuleFetches} runtime=${runtimeFetches}`);
console.log(`mdx:          ${mdxNodes} components named, imports folded=${mdxImportsFolded}, prose kept=${mdxProseKept}, props shown=${mdxExprShown}`);
console.log(`json:         ${jsonNodes} foldable nodes, ${jsonOpenAtStart} open, typed=${jsonTyped}, empty-is-leaf=${jsonEmptyIsLeaf}, survives a poll=${jsonSurvives}`);
console.log(`             width ${vwBefore} -> ${vwDuringDrag} (drag) -> ${vwAfter}, stored=${vwStored}`);
console.log(`markdown:    ${mdParas} paragraphs, ${mdHeads} heading, ${mdBreaks} line break, ${mdBold} bold, ${mdCode} code, ${mdMath} math, ${mdRaw} unrendered`);
console.log(`inspector:   ${inspectorBefore} -> ${widthDuringDrag} (drag) -> ${inspectorAfter} (committed), stored=${widthStored}, floor=${clamped}`);
console.log(`projects:    chips ${JSON.stringify(chipLabels)}`);
console.log(`             unfiled=[${unfiledOnly}] chief=[${chiefOnly}] all=${backToAll}, origin-shown=${originShown}`);
console.log(`             filed=${JSON.stringify(filed && filed.body)} then ${JSON.stringify(dirSet && dirSet.body)}, exported keys=${exportedBody && Object.keys(exportedBody).join(",")}`);
console.log(`wide plan:   ${wideNodes} nodes, plane ${planeWidth}px in a 900px window, scrolls=${viewportScrolls}`);
console.log(`             plan-reachable=${planReachable}, orphan-still-there=${orphanBack}`);
console.log(`             box=${boxIsTextarea ? "textarea" : "INPUT"}, plain-enter-held=${plainEnterHeld}`);
console.log(`notes:       plan=${planNotes} (orphan=${orphanShown}), step=${stepNotes}, badges=${noteBadges}, sent=${JSON.stringify(noteSent && noteSent.body)}, resolved=${JSON.stringify(noteResolved && noteResolved.body)}`);
console.log(`comments:    shown=${commentShown}, sent=${JSON.stringify(comment && comment.body)}`);
console.log(`folder:      saved=${rootSaved}, relinked=${rehomed}, unset-drops-file-editor-links=${unlinked}`);

const expected = [
  "Workflows", "Workflow detail", "Workflow detail", "Workflows", "Workflow detail",
  "Approvals inbox", "Templates", "Template detail", "Workflow detail",
];
const actual = screens.map((s) => s.split(" -> ")[1]);
// Plans: the checked-before-approval screens. The list separates a plan that holds up from
// one that does not; the verified one draws its graph through the same renderer everything
// else uses, with the proven condition on the edge shown as a condition rather than dumped
// into a JSON drawer; and the failed one shows the goal that did not follow, verbatim.
clickByText("Plans");
await new Promise((r) => setTimeout(r, 30));
record("nav Plans");
const planRows = countClass(mainNode(), "run-row");
const toolchainShown = JSON.stringify(mainNode()).includes("leanprover/lean4:v4.33.1");

clickByText("Fraud model refresh");
await new Promise((r) => setTimeout(r, 30));
record("nav Plan detail");
const planNodes = countClass(mainNode(), "node");
const claimsShown = JSON.stringify(mainNode()).includes("3 of 3 conditions constrain");
// A step says which part of the work it belongs to, and the graph draws a lane round each.
// Groups nest by path, so "Data/Collection" and "Data/Modelling" give three boxes: one round
// each phase and one round both.
const groupBoxes = countClass(mainNode(), "group-box");
const groupLabels = JSON.stringify(mainNode());
const groupsNamed = ["Data", "Collection", "Modelling"].every((n) => groupLabels.includes(n));

// The invariant the whole lane scheme exists to guarantee, checked numerically rather than
// trusted: every node inside a box belongs to that box's group, and no node of that group
// falls outside it. A box that encloses a foreign node is the failure this replaced.
function boxesAndNodes(root, boxes = [], nodes = []) {
  const cls = (root.class || "").split(" ");
  if (cls.includes("group-box")) {
    // The boundary is a rectilinear outline, not a rectangle, so it is read back as the
    // polygon it is — a bounding box would pass a test the real shape has to earn.
    const nums = (root.d.match(/-?\d+(\.\d+)?/g) || []).map(Number);
    const ring = [];
    for (let i = 0; i + 1 < nums.length; i += 2) ring.push([nums[i], nums[i + 1]]);
    boxes.push(ring);
  }
  if (cls.includes("node") && root.style && root.style.left) {
    nodes.push({
      id: (root.textContent || "") + JSON.stringify(root),
      x: parseFloat(root.style.left), y: parseFloat(root.style.top),
      w: parseFloat(root.style.width), h: parseFloat(root.style.height),
    });
  }
  for (const child of root.children || []) boxesAndNodes(child, boxes, nodes);
  return { boxes, nodes };
}
const drawn = boxesAndNodes(mainNode());
// Ray casting, so containment is judged against the outline rather than against a rectangle
// drawn round it. A node counts as inside only if its whole footprint is.
function pointIn(ring, x, y) {
  let hit = false;
  for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
    const [xi, yi] = ring[i];
    const [xj, yj] = ring[j];
    if (yi > y !== yj > y && x < ((xj - xi) * (y - yi)) / (yj - yi) + xi) hit = !hit;
  }
  return hit;
}
const inside = (ring, n) =>
  [[n.x, n.y], [n.x + n.w, n.y], [n.x, n.y + n.h], [n.x + n.w, n.y + n.h]]
    .every(([x, y]) => pointIn(ring, x, y));
// Two leaf boxes hold one node each; the box round both holds two. No box holds a stray.
const held = drawn.boxes.map((b) => drawn.nodes.filter((n) => inside(b, n)).length).sort();
const containment = JSON.stringify(held) === JSON.stringify([1, 1, 2]);
// The step that declares no group is drawn like any other and belongs to nothing: three
// nodes, three boxes, and the ungrouped one inside none of them. Grouping is per step, so a
// plan may group some of its work and leave the rest plain.
const ungroupedNode = drawn.nodes.filter((n) => !drawn.boxes.some((b) => inside(b, n)));
const optionalPerStep = drawn.nodes.length === 3 && ungroupedNode.length === 1;

// Selecting the step that reads something shows what was proven about what it reads. The
// condition is drawn as a condition — not through artifactCard, which would offer to open a
// file nothing has written, and not swept into the "Other inputs" JSON drawer.
clickByText("Fit the classifier.");
await new Promise((r) => setTimeout(r, 30));
const stepText = JSON.stringify(mainNode());
const contractCards = countClass(mainNode(), "contract");
const contractShown = stepText.includes("count ≥ 10000");
const notInJsonDrawer = !stepText.includes("Other inputs");
// A step is a function: what it demands and what it promises are both shown. And where the
// two sides of an edge differ, the promise sits above the demand — that difference is the
// thing the checking established.
const promisesShown = stepText.includes("Produces") && stepText.includes("auc ≥ 80");
// The derived schema renders under the condition, on the input and the output alike; a
// port without one shows nothing rather than an empty pair of braces. A row type's fields
// sit behind a disclosure named for the field that carries them.
const schemaShown =
  stepText.includes("{ count: Nat }") &&
  stepText.includes("{ auc: Nat }") &&
  !stepText.includes("{  }");
const schemaNested =
  countClass(mainNode(), "schema-nest") === 1 &&
  stepText.includes("events: List Event") &&
  stepText.includes("{ amount: Nat, flagged: Bool }");
const givenAndNeeds = countClass(mainNode(), "contract-given");
const weakeningVisible = stepText.includes("count ≥ 50000") && stepText.includes("count ≥ 10000");
// The step's algorithm renders between the demands and the promise: numbered lines with the
// indentation the term carried, and the external calls printed once as a legend rather than
// tagged inline. It reads as a listing, not a proof — no accent colouring is asserted here
// because none is applied.
const algLines = countClass(mainNode(), "alg-line");
const algShown =
  algLines === 3 &&
  stepText.includes("xgboost(harvest, λ)") &&
  stepText.includes("return M") &&
  stepText.includes("xgboost ⟨algo⟩");
// The loop body's structural indent arrives as an inline margin — one line of the three
// sits inside the `if`, and it is the only one indented.
const algIndented = findByClass(mainNode(), "alg-text").filter(
  (n) => n.style && n.style.marginLeft === "16px",
).length;

// Graph and source are two views of one plan, so they sit behind a toggle rather than
// stacked. The graph is what a verified plan opens on.
const graphFirst = countClass(mainNode(), "node") > 0 && countClass(mainNode(), "src-line") === 0;
const graphTab = `Graph ${countClass(mainNode(), "node")}`;
clickByText("Source");
await new Promise((r) => setTimeout(r, 30));
const sourceShown = countClass(mainNode(), "src-line") > 0;
const graphHidden = countClass(mainNode(), "node") === 0;
clickByText(graphTab);
await new Promise((r) => setTimeout(r, 30));
const backToGraph = countClass(mainNode(), "node") > 0;

// A group's label opens the group as the function it is: what crosses its boundary, and
// the algorithms of the steps inside. The leaf group takes the corpus contract in from
// outside and its product is consumed outside, so both cross.
clickByText("Modelling");
await new Promise((r) => setTimeout(r, 30));
const grpText = JSON.stringify(mainNode());
const grpPanel =
  grpText.includes("Group · 1 step") &&
  grpText.includes("Data/Modelling") &&
  // The plan's own line about what the group is for, not the panel's generic explainer.
  grpText.includes("Fit and score the classifier.") &&
  grpText.includes("Takes in (1)") &&
  grpText.includes("count ≥ 10000") &&
  grpText.includes("auc ≥ 80") &&
  grpText.includes("xgboost(harvest, λ)") &&
  grpText.includes("fit_model");
// The outer group swallows the harvest→fit_model edge: nothing crosses in, the corpus
// contract is plumbing its callers never see, and only the model leaves.
clickByText("Data");
await new Promise((r) => setTimeout(r, 30));
const outerText = JSON.stringify(mainNode());
const outerGrpPanel =
  outerText.includes("Group · 2 steps") &&
  !outerText.includes("Takes in") &&
  outerText.includes("auc ≥ 80") &&
  !outerText.includes("count ≥ 10000") &&
  !outerText.includes("count ≥ 50000") &&
  // Nobody described this group, so the panel explains itself instead of inventing one.
  outerText.includes("What crosses this boundary");

clickByText("Check again");
await new Promise((r) => setTimeout(r, 40));
const verified = posts.find((x) => x.url.includes("/verification"));

clickByText("← Plans");
await new Promise((r) => setTimeout(r, 30));
clickByText("Docs index refresh");
await new Promise((r) => setTimeout(r, 40));
record("nav Plan detail (failed)");
const failedText = JSON.stringify(mainNode());
const diagnostics = countClass(mainNode(), "diagnostic");
const goalShown = failedText.includes("x.vectors ≥ 5000");
const blamedStep = failedText.includes("evaluate");
// A plan that never ran has no graph, so there is nothing to toggle to: the source shows
// plainly, and the diagnostic's line number is the way into it.
const noToggleWithoutGraph = countClass(mainNode(), "chip") === 0;
const sourceIsThere = countClass(mainNode(), "src-line") === 80;
clickByText("line 71");
await new Promise((r) => setTimeout(r, 30));
const lineMarked = collectClasses(mainNode(), "src-line").filter((c) => /\bon\b/.test(c)).length;

console.log(`plans:       ${planRows} rows, toolchain=${toolchainShown}, graph=${planNodes} nodes, claims=${claimsShown}`);
console.log(`             contracts=${contractCards} shown=${contractShown}, produces=${promisesShown}, given/needs=${givenAndNeeds}, weakening=${weakeningVisible}, not-json=${notInJsonDrawer}`);
console.log(`             algorithm lines=${algLines}, rendered+legend=${algShown}, indented=${algIndented}, schema=${schemaShown} nested=${schemaNested}`);
console.log(`             group panel: leaf=${grpPanel}, outer=${outerGrpPanel}`);
console.log(`             groups=${groupBoxes} boxes (nested), named=${groupsNamed}, contains=${JSON.stringify(held)} ok=${containment}`);
console.log(`             optional: ${ungroupedNode.length} of ${drawn.nodes.length} nodes in no box=${optionalPerStep}, whole plan ungrouped -> ${ungroupedBoxes} boxes`);
console.log(`             pane graph-first=${graphFirst}, source=${sourceShown} (graph hidden=${graphHidden}), back=${backToGraph}`);
console.log(`             diagnostics=${diagnostics}, goal=${goalShown}, blamed=${blamedStep}, no-toggle=${noToggleWithoutGraph}, lines=${sourceIsThere}, jumped=${lineMarked}`);

const ok =
  dialogOpened &&
  // Metadata, in the four places it can be attached. These were computed and printed and
  // — for a while — asserted nowhere, which is a report rather than a test: the harness
  // said "artifact folded=false" and still called the run green.
  instLabelled &&
  paramDeclared &&
  paramsFilled &&
  critShown &&
  critOutstanding &&
  inputArtShown &&
  inputMetaShown &&
  stepMetaShown &&
  stepMetaFolds &&
  artFactsInline &&
  instInline &&
  instDeepInDrawer &&
  metaNoFetch &&
  expected.every((e, i) => e === actual[i]) &&
  // a + c + d + e + the loop's gate, all ordinary nodes in one flat graph — drafted or
  // running. The checkpoint is a node like any other; what marks it is its class.
  draftNodes === 5 &&
  runNodes === 5 &&
  draftGates === 1 &&
  draftEdgeLabels === 2 &&
  draftClusters === 0 &&
  // The run's iteration history lives on the gate: one cluster, on the gate node.
  runClusters === 1 &&
  runGates === 1 &&
  // A template draws like any other plan, and its dialog asks for one field per parameter.
  templateNodes === 5 &&
  paramFields === 2 &&
  // The plan says a person decides this one, and the graph says so without being asked.
  waitingNodes === 1 &&
  asked &&
  // Three artifacts, three paths, a copy control on each — copying works even for the ones
  // that cannot be opened or viewed.
  paths.length === 5 &&
  copyButtons === 5 &&
  hrefs.includes("vscode://file/Users/you/work/songs/notes/personas.md") &&
  hrefs.includes("https://example.com/pr/1") &&
  copied === "/Users/you/work/songs/notes/personas.md" &&
  // Changing the folder: stored without its trailing slash, and the links follow it.
  rootSaved === "/elsewhere/tree" &&
  rehomed &&
  // Clearing it: no editor link built on a guess, but the file is still readable in Chief
  // and the path is still copyable.
  unlinked &&
  stillOpenable &&
  stillCopyable &&
  // Notes hang off nodes. Nothing selected shows the plan thread — which here is the
  // orphan, whose step no node can stand in for. The plan-level note that is already
  // resolved stays folded away until asked for.
  planNotes === 1 &&
  orphanShown &&
  noteBadges === 1 &&
  resolvedShown &&
  // Selecting the step that has one shows that one, and only that one.
  stepNotes === 1 &&
  stepNoteShown &&
  boxIsTextarea &&
  plainEnterHeld &&
  // The plan's thread is reachable with a node selected, and still carries the orphan.
  planReachable &&
  planComposeBack &&
  orphanBack &&
  // A new note goes out against the node whose thread it was typed into.
  !!noteSent &&
  noteSent.url.endsWith("/workflows/wf_draft/notes") &&
  noteSent.body.step_id === "a" &&
  noteSent.body.body === "check it against last quarter's numbers" &&
  noteSent.body.author === "human" &&
  // And closing one is a write from this page, not something the harness did.
  !!noteResolved &&
  noteResolved.url.endsWith("/workflows/wf_draft/notes/rvw_1") &&
  noteResolved.body.resolved === true &&
  // All four are openable: the three files are read by the server, and the URL is framed —
  // which is the only way to see a page rendered by the project that owns its components.
  viewButtons === 5 &&
  viewerOpened &&
  viewerRendered &&
  viewerMermaid &&
  viewerTitle &&
  viewerClosed &&
  // One fetch per open, and a reload is an open: the file is read once, not on every poll.
  reloadFetches === 1 &&
  // The URL is framed rather than read, and the frame keeps its own origin without
  // gaining Chief's.
  framed &&
  frameNotFetched &&
  pageInset === "520px" &&
  frameSandboxed &&
  !selfFrame.includes("allow-same-origin") &&
  crossFrame.includes("allow-same-origin") &&
  // MDX with co-located components: compiled into a sandboxed frame that fetches nothing.
  mdxCompiled &&
  mdxSandboxed &&
  mdxSelfContained &&
  mdxAsked &&
  // MDX: two components named, imports folded, prose rendered, props visible. Nothing run.
  mdxNodes === 2 &&
  mdxImportsFolded &&
  mdxProseKept &&
  mdxExprShown &&
  // The open file is addressable: it goes into the hash, comes back out of it, and a
  // reload at that URL reopens it.
  /\/art_1$/.test(hashWithFile) &&
  /\/art_3$/.test(jsonHash) &&
  hashAfterClose === "#/workflow/wf_ok" &&
  reopened &&
  staleIgnored &&
  staleHealed &&
  // JSON folds: one node per object or array, the top two levels open, values typed, and
  // the built tree kept across a re-render rather than springing open every fifteen seconds.
  jsonNodes === 5 &&
  jsonOpenAtStart === 3 &&
  jsonTyped &&
  jsonEmptyIsLeaf &&
  keptNode &&
  jsonSurvives &&
  // Grows leftwards from its own edge, moves during the drag without a re-render, and the
  // width it lands on is remembered.
  vwBefore === 520 &&
  vwDuringDrag === 720 &&
  vwAfter === 720 &&
  vwStored === 720 &&
  // Markdown reaches the screen as elements, not as one line of text.
  mdParas >= 2 &&
  mdHeads === 1 &&
  mdBreaks === 1 &&
  mdBold === 1 &&
  mdCode === 1 &&
  mdMath === 1 &&
  mdRaw === 0 &&
  // The panel resizes from its left edge, moves during the drag without a re-render, and
  // the width it lands on is remembered.
  inspectorBefore === 360 &&
  widthDuringDrag === 480 &&
  inspectorAfter === 480 &&
  widthStored === 480 &&
  // Dragged far past its floor it stops at the floor, rather than collapsing to nothing.
  clamped === 280 &&
  // One chip per project in use, an Unfiled bucket, and an everything chip.
  chipLabels.join("|") === "Every project 3|chief 1|songs 1|Unfiled 1" &&
  unfiledOnly === "Archived one" &&
  chiefOnly === "Approved one" &&
  backToAll === 3 &&
  // The plan says where it was made, as a record rather than a live path.
  originShown &&
  // Filing goes out as a PATCH, and is allowed on a workflow that has already run.
  !!filed &&
  filed.body.project === "chief-ui" &&
  // One key per request, not both: sending both every time would clear whichever field the
  // person was not editing.
  !("origin_dir" in filed.body) &&
  !!dirSet &&
  dirSet.body.origin_dir === "/w/elsewhere" &&
  !("project" in dirSet.body) &&
  // The exported template is a POST /templates body at rest — same names, same shape, id
  // included so re-registering the same file is idempotent rather than a second copy.
  !!exportedBody &&
  exportedBody.template_id === "tpl_1" &&
  exportedBody.project === "chief" &&
  exportedBody.steps.length === STEPS.length &&
  exportedBody.parameters.length === 2 &&
  // Fourteen nodes, twelve of them in one layer: the plane is far wider than the window,
  // and the viewport scrolls to it instead of hiding the difference.
  wideNodes === 14 &&
  planeWidth > 2000 &&
  viewportScrolls &&
  // A long artifact description wraps and is clamped, with a way to open it out.
  labelClipped &&
  onlyLongClipped &&
  !!moreButton &&
  labelOpened &&
  // Comments: the existing one is read back, and a new one goes out by artifact id.
  commentShown &&
  !!comment &&
  comment.url.endsWith("/runs/run_1/artifacts/art_1/comments") &&
  comment.body.body === "check the second half against the brief" &&
  comment.body.author === "human" &&
  // What was typed is what was sent, addressed by state path, with the decision on it.
  !!decision &&
  decision.url.endsWith("/resolutions/e") &&
  decision.body.decision === "approved" &&
  decision.body.response.budget === "$400" &&
  decision.body.note === "go ahead" &&
  // Plans: both rows listed, the verified one drawn, its proven condition shown as a
  // condition, and the failed one showing the goal that did not follow.
  planRows === 2 &&
  toolchainShown &&
  planNodes === 3 &&
  // One lane per declared group, labelled; and a plan that declares none is laid out exactly
  // as it was, with nothing drawn round it.
  groupBoxes === 3 &&
  groupsNamed &&
  containment &&
  optionalPerStep &&
  ungroupedBoxes === 0 &&
  contractCards === 2 &&
  contractShown &&
  promisesShown &&
  givenAndNeeds === 2 &&
  weakeningVisible &&
  schemaShown &&
  schemaNested &&
  // The step's algorithm: numbered pseudocode with its indentation, externals as a legend.
  algShown &&
  algIndented === 1 &&
  // Clicking a group label inspects the group: boundary contracts and member algorithms.
  grpPanel &&
  outerGrpPanel &&
  notInJsonDrawer &&
  claimsShown &&
  !!verified &&
  verified.url.endsWith("/plans/pln_ok/verification") &&
  diagnostics === 2 &&
  goalShown &&
  blamedStep &&
  // The toggle: a verified plan opens on the graph, switches to the source and back.
  graphFirst &&
  sourceShown &&
  graphHidden &&
  backToGraph &&
  // A plan with no graph has no toggle, and its diagnostics jump into the source.
  noToggleWithoutGraph &&
  sourceIsThere &&
  lineMarked === 1;
console.log(ok ? "PASS" : `FAIL: expected ${expected.join(", ")}`);
process.exit(ok ? 0 : 1);
