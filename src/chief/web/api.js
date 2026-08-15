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

/** Approve or reject. The server can refuse either (REQ-14 is enforced at approval time),
    so the caller must surface the rejection rather than assume it took. */
export const decideAmendment = (amendmentId, approve, reason) =>
  post(`/amendments/${amendmentId}/${approve ? "approve" : "reject"}`, {
    decided_by: "human",
    reason: reason || null,
  });
