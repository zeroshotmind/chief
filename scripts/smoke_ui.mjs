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
    replaceWith() {},
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
globalThis.document = {
  createElement: node,
  createElementNS: (_ns, tag) => node(tag),
  getElementById: (id) => roots[id] || node("div"),
  addEventListener() {},
  createTextNode: (t) => ({ text: t }),
};
globalThis.window = { addEventListener() {} };
globalThis.location = { search: "", hash: "", replace(h) { this.hash = h; } };
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
  { id: "a", type: "task", goal: "first", harness: "claude-code", depends_on: [] },
  { id: "b", type: "loop", goal: "each thing", harness: "claude-code", depends_on: ["a"], body: ["c", "d"], exit_when: "the check passes" },
  { id: "c", type: "task", goal: "one thing", harness: "claude-code", depends_on: [] },
  { id: "d", type: "task", goal: "then check it", harness: "claude-code", depends_on: ["c"] },
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
  { created_at: "2026-03-02T09:00:00Z", updated_at: "2026-03-02T09:00:00Z", workflow_id: "wf_draft", title: "A draft", source: "generated", generated_by: "claude-code", status: "draft", version: 1, steps: STEPS },
  { created_at: "2026-01-05T09:00:00Z", updated_at: "2026-01-06T09:00:00Z", workflow_id: "wf_ok", title: "Approved one", source: "import", generated_by: null, status: "approved", version: 2, steps: STEPS },
  { created_at: "2026-02-01T09:00:00Z", updated_at: "2026-02-01T09:00:00Z", workflow_id: "wf_old", title: "Archived one", source: "import", generated_by: null, status: "archived", version: 1, steps: STEPS },
];
const RUN = {
  run_id: "run_1", workflow_id: "wf_ok", base_version: 2, applied_amendment_ids: [],
  status: "waiting_on_human", created_at: new Date().toISOString(), updated_at: new Date().toISOString(),
  step_states: {
    // Two refs of the kind a harness actually reports: a file relative to wherever it was
    // working, and something already on the web. They resolve down different arms.
    a: { step_id: "a", status: "completed", summary: "did it", artifacts: [
      { artifact_id: "art_1", type: "markdown",
        description: "Persona sheet for the whole cast, including the three walk-on parts that only appear in the second act and the two that were cut but are kept for continuity",
        ref: "notes/personas.md", data: null,
        comments: [{ comment_id: "cmt_1", body: "the tone here is the one to match", author: "roy", created_at: new Date().toISOString(), via: "rest" }] },
      { artifact_id: "art_2", type: "pr", description: "The PR", ref: "https://example.com/pr/1", data: null, comments: [] },
    ] },
    b: { step_id: "b", status: "running", instances: [{ instance_id: "i0", kind: "iteration", index: 0, status: "completed", summary: "one", step_states: {} }] },
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
    steps: STEPS, status: "active", version: 1, derived_from_workflow_id: null,
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

globalThis.fetch = async (url, options) => {
  // Every write, not only the POSTs: resolving a note is a PATCH, and a smoke test that
  // only watched POSTs would call it green without ever seeing the request.
  if (options && options.body) {
    posts.push({ url, method: options.method, body: JSON.parse(options.body) });
  }
  if (NO_TEMPLATES && url.endsWith("/templates")) {
    return { ok: false, status: 404, json: async () => ({ error: { code: "not_found" } }) };
  }
  const body =
    url.includes("/notes") ? NOTES
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

/** Click the newest element whose class list contains every one of `classes`. Text is
    ambiguous for a column header ("Workflow" is also the nav link). */
function clickByClass(...classes) {
  const has = (n) => classes.every((c) => (n.class || "").split(" ").includes(c));
  const hit = [...clicks].reverse().find((c) => has(c.node));
  if (!hit) throw new Error(`no clickable element with classes ${classes.join(" ")}`);
  hit.fn({ preventDefault() {}, stopPropagation() {} });
}

/** Walk a rendered subtree counting nodes whose class matches — the screen labels below say
    the router works, this says the drawing survived. */
function countClass(root, cls) {
  let n = root.class && root.class.split(" ").includes(cls) ? 1 : 0;
  for (const child of root.children || []) n += countClass(child, cls);
  return n;
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

const screens = [];
function record(label) {
  const main = roots.app.children.find((c) => c.tag === "main");
  screens.push(`${label} -> ${main ? main["data-screen-label"] : "?"}`);
}

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

// "Needs you" is one question, not three: an unapproved draft and a run stopped at a
// checkpoint are both waiting on the same person.
clickByText("Needs you");
await new Promise((r) => setTimeout(r, 10));
if (rowTitles().join() !== "A draft,Approved one") throw new Error(`filter chip: got ${rowTitles()}`);

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
const hrefs = paths.map((p) => findByClass(p, "art-href")[0]).map((a) => a && a.href);
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
// the slash gone and links rebuilt underneath it.
clickByText("Change");
await new Promise((r) => setTimeout(r, 10));
typeIntoId("files-root", "/elsewhere/tree/");
await new Promise((r) => setTimeout(r, 10));
clickByText("Save");
await new Promise((r) => setTimeout(r, 20));
const rootSaved = stored["chief.filesRoot"];
const rehomed = findByClass(mainNode(), "art-path")
  .map((p) => findByClass(p, "art-href")[0].href)
  .includes("vscode://file/elsewhere/tree/notes/personas.md");

// And with no folder named at all, the path is text. A link built on a guessed base opens a
// "file not found" in the editor, which reads as the editor being broken.
clickByText("Change");
await new Promise((r) => setTimeout(r, 10));
typeIntoId("files-root", "");
await new Promise((r) => setTimeout(r, 10));
clickByText("Save");
await new Promise((r) => setTimeout(r, 20));
const unresolved = findByClass(mainNode(), "art-path")
  .map((p) => findByClass(p, "art-href")[0])
  .find((a) => a.textContent === "notes/personas.md");
const unlinked = !!unresolved && unresolved.tag === "span" && !unresolved.href;
// The copy button does not go away with the link: what the harness reported is still worth
// having on the clipboard.
const stillCopyable = countClass(mainNode(), "art-copy") === 2;

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
console.log(`artifacts:   ${paths.length} paths, ${copyButtons} copy buttons, copied=${copied}`);
console.log(`             ${JSON.stringify(hrefs)}`);
console.log(`wide plan:   ${wideNodes} nodes, plane ${planeWidth}px in a 900px window, scrolls=${viewportScrolls}`);
console.log(`             plan-reachable=${planReachable}, orphan-still-there=${orphanBack}`);
console.log(`             box=${boxIsTextarea ? "textarea" : "INPUT"}, plain-enter-held=${plainEnterHeld}`);
console.log(`notes:       plan=${planNotes} (orphan=${orphanShown}), step=${stepNotes}, badges=${noteBadges}, sent=${JSON.stringify(noteSent && noteSent.body)}, resolved=${JSON.stringify(noteResolved && noteResolved.body)}`);
console.log(`comments:    shown=${commentShown}, sent=${JSON.stringify(comment && comment.body)}`);
console.log(`folder:      saved=${rootSaved}, relinked=${rehomed}, unset-is-text=${unlinked}`);

const expected = [
  "Workflows", "Workflow detail", "Workflow detail", "Workflows", "Workflow detail",
  "Approvals inbox", "Templates", "Template detail", "Workflow detail",
];
const actual = screens.map((s) => s.split(" -> ")[1]);
const ok =
  dialogOpened &&
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
  // Two artifacts, two acted-on paths, a copy control on each.
  paths.length === 2 &&
  copyButtons === 2 &&
  hrefs.includes("vscode://file/Users/you/work/songs/notes/personas.md") &&
  hrefs.includes("https://example.com/pr/1") &&
  copied === "/Users/you/work/songs/notes/personas.md" &&
  // Changing the folder: stored without its trailing slash, and the links follow it.
  rootSaved === "/elsewhere/tree" &&
  rehomed &&
  // Clearing it: readable text, no link, copy button still there.
  unlinked &&
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
  decision.body.note === "go ahead";
console.log(ok ? "PASS" : `FAIL: expected ${expected.join(", ")}`);
process.exit(ok ? 0 : 1);
