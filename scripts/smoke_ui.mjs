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
  return {
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
const WORKFLOWS = [
  { created_at: "2026-03-02T09:00:00Z", updated_at: "2026-03-02T09:00:00Z", workflow_id: "wf_draft", title: "A draft", source: "generated", generated_by: "claude-code", status: "draft", version: 1, steps: STEPS },
  { created_at: "2026-01-05T09:00:00Z", updated_at: "2026-01-06T09:00:00Z", workflow_id: "wf_ok", title: "Approved one", source: "import", generated_by: null, status: "approved", version: 2, steps: STEPS },
  { created_at: "2026-02-01T09:00:00Z", updated_at: "2026-02-01T09:00:00Z", workflow_id: "wf_old", title: "Archived one", source: "import", generated_by: null, status: "archived", version: 1, steps: STEPS },
];
const RUN = {
  run_id: "run_1", workflow_id: "wf_ok", base_version: 2, applied_amendment_ids: [],
  status: "waiting_on_human", created_at: new Date().toISOString(), updated_at: new Date().toISOString(),
  step_states: {
    a: { step_id: "a", status: "completed", summary: "did it", artifacts: [] },
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
  if (options && options.method === "POST") posts.push({ url, body: JSON.parse(options.body) });
  if (NO_TEMPLATES && url.endsWith("/templates")) {
    return { ok: false, status: 404, json: async () => ({ error: { code: "not_found" } }) };
  }
  const body =
    url.includes("/audit") ? AUDIT
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

console.log(screens.join("\n"));
console.log(`template graph: ${templateNodes} nodes, ${paramFields} parameter fields`);
console.log("workflow dialog opened:", dialogOpened);
console.log(`draft graph: ${draftNodes} nodes, ${draftGates} loop gates, ${draftEdgeLabels} branch labels`);
console.log(`run graph:   ${runNodes} nodes, ${runClusters} instance clusters`);
console.log(`checkpoint:  ${waitingNodes} node, asked=${asked}, sent=${JSON.stringify(decision && decision.body)}`);

const expected = [
  "Workflows", "Workflow detail", "Workflow detail", "Workflows", "Workflow detail",
  "Approvals inbox", "Templates", "Template detail",
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
  // What was typed is what was sent, addressed by state path, with the decision on it.
  !!decision &&
  decision.url.endsWith("/resolutions/e") &&
  decision.body.decision === "approved" &&
  decision.body.response.budget === "$400" &&
  decision.body.note === "go ahead";
console.log(ok ? "PASS" : `FAIL: expected ${expected.join(", ")}`);
process.exit(ok ? 0 : 1);
