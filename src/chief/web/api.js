/* The REST client. The UI is a pure API client with no private data access (REQ-18):
   every value it shows comes from one of these calls.

   The base URL defaults to the versioned prefix served by the same process. `?api=` points
   the UI at a Chief running elsewhere, which is also how the CORS-free single-origin default
   stays the common case. */

export const API_BASE = new URLSearchParams(location.search).get("api") || "/v1";

export class ApiError extends Error {
  constructor(message, { status = 0, code = null } = {}) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

async function request(path, options) {
  let response;
  try {
    response = await fetch(API_BASE + path, options);
  } catch (cause) {
    throw new ApiError(`cannot reach the Chief API at ${API_BASE}`, { code: "unreachable" });
  }
  const body = response.status === 204 ? null : await response.json().catch(() => null);
  if (!response.ok) {
    // app.py renders domain and validation failures as {error: {code, message}}.
    const err = body && body.error;
    throw new ApiError(
      (err && err.message) || `${options?.method || "GET"} ${path} failed (${response.status})`,
      { status: response.status, code: err && err.code },
    );
  }
  return body;
}

const post = (path, body) =>
  request(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });

export const listWorkflows = () => request("/workflows");
export const listRuns = () => request("/runs");
export const getRun = (runId) => request(`/runs/${runId}`);
export const getRunDefinition = (runId) => request(`/runs/${runId}/definition`);
export const listAmendments = (runId) => request(`/runs/${runId}/amendments`);

/** Decide a checkpoint the run is blocked on. `path` is the state path — ["step_04"] for a
    top-level checkpoint, ["loop_01", "inst_00", "step_04"] for one inside a construct.

    The server validates the answers against the fields the checkpoint declared, so a
    rejection here is real and must reach the person who typed them, not be swallowed. */
export const resolveCheckpoint = (runId, path, body) =>
  post(`/runs/${runId}/resolutions/${path.join("/")}`, body);

/** Say something about an artifact, for whoever picks the work up. Addressed by artifact id
    rather than by state path: artifacts are read as one flat list of everything a run
    produced, and the id is the only handle that survives that flattening. */
export const commentOnArtifact = (runId, artifactId, body) =>
  post(`/runs/${runId}/artifacts/${artifactId}/comments`, body);

/** The bytes of the file an artifact names, for the viewer.

    No path is sent — only the two ids — so there is nothing here that could ask for a file
    other than the one the artifact already points at. The server answers with opaque bytes
    whatever the file is, and names the type it may be rendered as in a header; the caller
    applies that itself, so browsing to the URL can never execute an artifact. */
export async function artifactContent(runId, artifactId) {
  const response = await fetch(
    `${API_BASE}/runs/${runId}/artifacts/${artifactId}/content`,
  ).catch(() => null);
  if (!response) throw new ApiError(`cannot reach the Chief API at ${API_BASE}`, { code: "unreachable" });
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    const err = body && body.error;
    throw new ApiError((err && err.message) || `could not read the file (${response.status})`, {
      status: response.status, code: err && err.code,
    });
  }
  return {
    bytes: await response.arrayBuffer(),
    // The type Chief says this may be shown as, never the response's own content type —
    // that is always octet-stream, deliberately.
    mediaType: response.headers.get("X-Chief-Media-Type") || "application/octet-stream",
    name: response.headers.get("X-Chief-File-Name") || "file",
  };
}

/** An MDX document and the modules sitting beside it, as sources.

    Still no path: two ids, and the server derives the graph from what the files import. */
export const artifactModules = (runId, artifactId) =>
  request(`/runs/${runId}/artifacts/${artifactId}/modules`);

/** Feedback on a plan, for whoever revises it. The other direction of the comment channel:
    a comment is said about work that is done, a note about work that has not started.

    `step_id` is optional — omitted, the note is about the plan as a whole. Both writing a
    note and closing one are REST-only: a harness reads them off the workflow document and
    revises the plan, and a person judges whether that answered them. */
export const listReviewNotes = (workflowId) => request(`/workflows/${workflowId}/notes`);

export const addReviewNote = (workflowId, body) => post(`/workflows/${workflowId}/notes`, body);

export const decideReviewNote = (workflowId, noteId, resolved) =>
  request(`/workflows/${workflowId}/notes/${noteId}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ resolved, resolved_by: "human" }),
  });

/** File a workflow under a project, or clear the label with null. Not a revision: it says
    nothing about the plan, so it is allowed at any status — which matters, because the
    workflows most in need of filing are the ones that already ran. */
export const labelWorkflow = (workflowId, patch) =>
  request(`/workflows/${workflowId}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    // Only the keys the caller passed. The server tells an omitted field from an explicit
    // null, and sending both every time would clear whichever one was not being edited.
    body: JSON.stringify(patch),
  });

/** Templates: the reusable plan. A workflow is single-use, so reuse lives here. */
export const listTemplates = () => request("/templates");
export const archiveTemplate = (templateId) => post(`/templates/${templateId}/archive`, {});

/** Build a draft workflow from a template. The server refuses a missing required parameter
    or an unknown name, so the caller must surface the error rather than assume it took. */
export const instantiateTemplate = (templateId, parameters, title) =>
  post(`/templates/${templateId}/workflows`, { parameters, title: title || null });

/** Generalise an existing plan into a template. */
export const createTemplateFromWorkflow = (workflowId, body) =>
  post(`/workflows/${workflowId}/template`, body);

/** Proof graphs: a workflow graph whose every edge is a theorem, checked before anyone
    approves anything.

    Older than the feature and newer than the UI both happen, so every one of these is
    allowed to 404 at the caller — `listProofGraphs` is the one the shell asks for on load,
    and it treats a 404 as "this Chief has no proof-graphs endpoint" rather than as an error
    worth stopping for, exactly as templates do. */
export const listProofGraphs = () => request("/proof-graphs");
export const getProofGraph = (graphId) => request(`/proof-graphs/${graphId}`);

/** Whether this instance can check a graph at all. Asked before offering the button: an
    instance without a Lean toolchain must say so, not report every graph as unsound. */
export const proofGraphToolchain = () => request("/proof-graphs/toolchain");

/** Run the check and store what came back. A graph that does not hold up is a 200 carrying
    `status: "failed"` and the diagnostics — the check reaching a verdict is the request
    succeeding, so the caller reads the body rather than catching. */
export const verifyProofGraph = (graphId) => post(`/proof-graphs/${graphId}/verification`, {});

/** Replace the source. The server drops the verdict: it belonged to the text that earned it. */
export const reviseProofGraph = (graphId, leanSource, reason) =>
  request(`/proof-graphs/${graphId}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ lean_source: leanSource, reason: reason || null }),
  });

/** Lower a verified graph into a draft workflow. Refused unless it is verified by the
    toolchain running now. */
export const compileProofGraph = (graphId, body) =>
  post(`/proof-graphs/${graphId}/workflows`, body || {});

export const deleteProofGraph = (graphId) =>
  request(`/proof-graphs/${graphId}`, { method: "DELETE" });

/** The audit log for one workflow (REQ-20). How a decision comment is read back after the
    moment it was typed: the decision is an event, not a field on the workflow. */
export const getWorkflowAudit = (workflowId) => request(`/audit?workflow_id=${workflowId}`);

/** Everything the run-detail screen renders, in one round trip's worth of parallelism. */
export async function getRunDetail(runId) {
  const [state, def, amendments] = await Promise.all([
    getRun(runId),
    getRunDefinition(runId),
    listAmendments(runId),
  ]);
  return { state, def, amendments };
}

/** The two workflow-lifecycle decisions (REQ-32). Neither takes a body: approving a draft
    and retiring a workflow are the whole payload. Both can be refused — the status may have
    moved under us — so callers surface the error rather than assuming it took. */
const decision = (reason) => ({ decided_by: "human", reason: reason || null });

export const approveWorkflow = (workflowId, reason) =>
  post(`/workflows/${workflowId}/approve`, decision(reason));
export const archiveWorkflow = (workflowId, reason) =>
  post(`/workflows/${workflowId}/archive`, decision(reason));

/** Permanent, and not the same act as archiving: the workflow, its versions, its runs, its
    amendments and its review notes all go. The audit trail and any template saved from it
    stay. Nothing on disk is touched. There is no MCP tool for this — see the route. */
export const deleteWorkflow = (workflowId) =>
  request(`/workflows/${workflowId}`, { method: "DELETE" });

/** Approve or reject. The server can refuse either (REQ-14 is enforced at approval time),
    so the caller must surface the rejection rather than assume it took. */
export const decideAmendment = (amendmentId, approve, reason) =>
  post(`/amendments/${amendmentId}/${approve ? "approve" : "reject"}`, {
    decided_by: "human",
    reason: reason || null,
  });
