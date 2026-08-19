"""REST routes (contract section 2).

Route paths match the contract exactly. Two families are added on top, both marked as
extensions in the docs:

* ``/runs/{run_id}/state/{path}/...`` — the generalised nested-instance addressing that
  contract 2.2's depth-1 routes are a special case of.
* read-only ``/runs/{run_id}/definition`` and ``/audit`` — the effective plan and the
  audit log (REQ-20) both need to be reachable through the API for the UIs (REQ-4/REQ-18).

No auth (REQ-45), no ownership fields (REQ-44).
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, Header, Query, Request, Response, status

from ..domain import paths as pathlib_
from ..domain.service import Chief
from ..errors import ValidationFailed
from ..models import (
    Amendment,
    AmendmentCreate,
    AmendmentDecision,
    ApprovalPolicy,
    ArtifactRef,
    BodyStepUpdate,
    CheckpointResolution,
    CommentCreate,
    InstanceCreate,
    InstanceUpdate,
    ReviewNote,
    ReviewNoteCreate,
    ReviewNoteDecision,
    RunCreate,
    RunPlan,
    RunState,
    StepInstance,
    StepUpdate,
    TemplateCreate,
    TemplateFromWorkflow,
    TemplateInstantiate,
    WorkflowCreate,
    WorkflowDefinition,
    WorkflowLabel,
    WorkflowRevise,
    WorkflowTemplate,
)

router = APIRouter()


def get_service() -> Chief:  # pragma: no cover - replaced by dependency_overrides
    raise RuntimeError("service dependency was not configured")


Service = Annotated[Chief, Depends(get_service)]


# --- 2.1 workflow lifecycle ---------------------------------------------------------------


@router.post("/workflows", response_model=WorkflowDefinition, status_code=status.HTTP_201_CREATED)
def create_workflow(body: WorkflowCreate, service: Service) -> WorkflowDefinition:
    return service.create_workflow(body)


@router.get("/workflows", response_model=list[WorkflowDefinition])
def list_workflows(service: Service, status_: str | None = Query(None, alias="status")) -> Any:
    return service.list_workflows(status_)


@router.get("/workflows/{workflow_id}", response_model=WorkflowDefinition)
def get_workflow(workflow_id: str, service: Service) -> WorkflowDefinition:
    return service.get_workflow(workflow_id)


@router.put("/workflows/{workflow_id}", response_model=WorkflowDefinition)
def revise_draft(
    workflow_id: str, body: WorkflowRevise, service: Service
) -> WorkflowDefinition:
    """Replace a draft's plan. Only a draft; an approved plan changes by amendment."""
    return service.revise_draft(workflow_id, body)


@router.patch("/workflows/{workflow_id}", response_model=WorkflowDefinition)
def label_workflow(
    workflow_id: str, body: WorkflowLabel, service: Service
) -> WorkflowDefinition:
    """File a workflow under a project, or clear the label. Allowed at any status."""
    return service.label_workflow(workflow_id, body)


@router.get("/projects")
def list_projects(service: Service) -> Any:
    """Every project label in use, with a count. Derived from the workflows, not stored."""
    return service.list_projects()


@router.get("/workflows/{workflow_id}/versions/{version}", response_model=WorkflowDefinition)
def get_workflow_version(workflow_id: str, version: int, service: Service) -> WorkflowDefinition:
    return service.get_workflow_version(workflow_id, version)


@router.post("/workflows/{workflow_id}/approve", response_model=WorkflowDefinition)
def approve_workflow(
    workflow_id: str,
    service: Service,
    # Optional, and it has to stay that way: the contract specifies a bodyless POST, and
    # clients that predate the comment must keep working unchanged.
    body: AmendmentDecision = Body(default_factory=AmendmentDecision),
) -> WorkflowDefinition:
    return service.approve_workflow(workflow_id, body)


@router.post("/workflows/{workflow_id}/archive", response_model=WorkflowDefinition)
def archive_workflow(
    workflow_id: str,
    service: Service,
    body: AmendmentDecision = Body(default_factory=AmendmentDecision),
) -> WorkflowDefinition:
    return service.archive_workflow(workflow_id, body)


# --- extension: review notes on a plan ----------------------------------------------------
#
# Feedback a person leaves on a draft, for whoever revises it. The other direction of the
# artifact-comment channel: a comment is said about work that is done, a note about work
# that has not started. REST-only in both directions — a harness reads notes off the
# workflow document `get_workflow` already returns, and neither writes one nor closes one.


@router.post(
    "/workflows/{workflow_id}/notes",
    response_model=ReviewNote,
    status_code=status.HTTP_201_CREATED,
)
def add_review_note(
    workflow_id: str, body: ReviewNoteCreate, service: Service
) -> ReviewNote:
    """Leave a note on a step, or on the plan as a whole when ``step_id`` is omitted."""
    return service.add_review_note(workflow_id, body)


@router.get("/workflows/{workflow_id}/notes", response_model=list[ReviewNote])
def list_review_notes(
    workflow_id: str, service: Service, resolved: bool | None = Query(None)
) -> Any:
    return service.list_review_notes(workflow_id, resolved)


@router.patch("/workflows/{workflow_id}/notes/{note_id}", response_model=ReviewNote)
def decide_review_note(
    workflow_id: str, note_id: str, body: ReviewNoteDecision, service: Service
) -> ReviewNote:
    """Mark a note resolved, or put a resolved one back."""
    return service.decide_review_note(workflow_id, note_id, body)


# --- extension: templates -----------------------------------------------------------------
#
# Not in the contract. A workflow is single-use — approved once, executed once — so reuse
# lives here instead of in a second run of the same workflow.


@router.post("/templates", response_model=WorkflowTemplate, status_code=status.HTTP_201_CREATED)
def create_template(body: TemplateCreate, service: Service) -> WorkflowTemplate:
    return service.create_template(body)


@router.get("/templates", response_model=list[WorkflowTemplate])
def list_templates(service: Service, status_: str | None = Query(None, alias="status")) -> Any:
    return service.list_templates(status_)


@router.get("/templates/{template_id}", response_model=WorkflowTemplate)
def get_template(template_id: str, service: Service) -> WorkflowTemplate:
    return service.get_template(template_id)


@router.post("/templates/{template_id}/archive", response_model=WorkflowTemplate)
def archive_template(template_id: str, service: Service) -> WorkflowTemplate:
    return service.archive_template(template_id)


@router.post(
    "/templates/{template_id}/workflows",
    response_model=WorkflowDefinition,
    status_code=status.HTTP_201_CREATED,
)
def instantiate_template(
    template_id: str,
    service: Service,
    body: TemplateInstantiate = Body(default_factory=TemplateInstantiate),
) -> WorkflowDefinition:
    """A template plus values becomes a draft workflow. REQ-32 still applies to the result."""
    return service.instantiate_template(template_id, body)


@router.post(
    "/workflows/{workflow_id}/template",
    response_model=WorkflowTemplate,
    status_code=status.HTTP_201_CREATED,
)
def create_template_from_workflow(
    workflow_id: str,
    service: Service,
    body: TemplateFromWorkflow = Body(default_factory=TemplateFromWorkflow),
) -> WorkflowTemplate:
    """Generalise a plan that exists into one that can be reused."""
    return service.create_template_from_workflow(workflow_id, body)


# --- 2.2 runs -----------------------------------------------------------------------------


@router.post(
    "/workflows/{workflow_id}/runs", response_model=RunState, status_code=status.HTTP_201_CREATED
)
def register_run(
    workflow_id: str, service: Service, body: RunCreate = Body(default_factory=RunCreate)
) -> RunState:
    return service.register_run(workflow_id, body)


@router.get("/runs", response_model=list[RunState])
def list_runs(
    service: Service,
    status_: str | None = Query(None, alias="status"),
    workflow_id: str | None = Query(None),
) -> Any:
    return service.list_runs(status_, workflow_id)


@router.get("/runs/{run_id}", response_model=RunState)
def get_run(run_id: str, service: Service) -> RunState:
    return service.get_run(run_id)


@router.get("/runs/{run_id}/definition", response_model=RunPlan)
def get_run_definition(run_id: str, service: Service) -> RunPlan:
    """Extension: the plan this run is executing (base_version + its own amendments)."""
    return service.get_run_plan(run_id)


@router.post("/runs/{run_id}/steps/{step_id}/updates", response_model=RunState)
def report_step_update(run_id: str, step_id: str, body: StepUpdate, service: Service) -> RunState:
    return service.report_step_update(run_id, [step_id], body)


@router.post(
    "/runs/{run_id}/steps/{step_id}/instances",
    response_model=StepInstance,
    status_code=status.HTTP_201_CREATED,
)
def register_instance(
    run_id: str,
    step_id: str,
    service: Service,
    body: InstanceCreate = Body(default_factory=InstanceCreate),
) -> StepInstance:
    _, instance = service.register_instance(run_id, [step_id], body)
    return instance


@router.post(
    "/runs/{run_id}/steps/{step_id}/instances/{instance_id}/updates", response_model=RunState
)
def report_instance_update(
    run_id: str, step_id: str, instance_id: str, body: InstanceUpdate, service: Service
) -> RunState:
    return service.report_instance_update(run_id, [step_id], instance_id, body)


@router.post(
    "/runs/{run_id}/steps/{step_id}/instances/{instance_id}/steps/{body_step_id}/updates",
    response_model=RunState,
)
def report_body_step_update(
    run_id: str,
    step_id: str,
    instance_id: str,
    body_step_id: str,
    body: BodyStepUpdate,
    service: Service,
) -> RunState:
    return service.report_step_update(run_id, [step_id, instance_id, body_step_id], body)


@router.post("/runs/{run_id}/steps/{step_id}/resolution", response_model=RunState)
def resolve_checkpoint(
    run_id: str, step_id: str, body: CheckpointResolution, service: Service
) -> RunState:
    """Record a person's decision at a blocked checkpoint."""
    return service.resolve_checkpoint(run_id, [step_id], body)


# --- 2.2 (extension) generalised addressing for nested constructs --------------------------


@router.post("/runs/{run_id}/state/{state_path:path}/updates", response_model=RunState)
def report_nested_step_update(
    run_id: str, state_path: str, body: BodyStepUpdate, service: Service
) -> RunState:
    return service.report_step_update(run_id, pathlib_.parse_path(state_path), body)


@router.post(
    "/runs/{run_id}/state/{state_path:path}/instances",
    response_model=StepInstance,
    status_code=status.HTTP_201_CREATED,
)
def register_nested_instance(
    run_id: str,
    state_path: str,
    service: Service,
    body: InstanceCreate = Body(default_factory=InstanceCreate),
) -> StepInstance:
    _, instance = service.register_instance(run_id, pathlib_.parse_path(state_path), body)
    return instance


@router.post("/runs/{run_id}/resolutions/{state_path:path}", response_model=RunState)
def resolve_nested_checkpoint(
    run_id: str, state_path: str, body: CheckpointResolution, service: Service
) -> RunState:
    """A checkpoint inside a loop or parallel body, addressed by state path."""
    return service.resolve_checkpoint(run_id, pathlib_.parse_path(state_path), body)


#: Hosts this API answers file content on. A `Host` header naming anything else is a
#: DNS-rebinding attempt: a page on the open web resolving its own domain to 127.0.0.1 so
#: that a fetch reaches a loopback service the browser thinks is same-origin. The MCP
#: transport already carries this check; it matters more here, because this is the one route
#: that returns something off the disk. Applied to this route alone rather than to the whole
#: API, so nothing that works today can stop working.
def _check_host(request: Request, host: str | None) -> None:
    from ..app import allowed_hosts

    if host is None:
        return
    name = host.rsplit(":", 1)[0].strip("[]").lower()
    if name not in allowed_hosts():
        raise ValidationFailed(
            f"file content is not served to host '{name}'",
            details={"host": name},
        )


@router.get("/runs/{run_id}/artifacts/{artifact_id}/content")
def artifact_content(
    run_id: str,
    artifact_id: str,
    service: Service,
    request: Request,
    host: Annotated[str | None, Header()] = None,
) -> Response:
    """The file this artifact names, for the viewer to render.

    The caller supplies no path — only the two ids — so there is nothing to traverse. See
    ``domain/files.py``.

    Always ``application/octet-stream``, whatever the file is. The type the browser may
    render it as travels in ``X-Chief-Media-Type`` and the UI applies it client-side, so
    browsing straight to this URL can never execute an artifact: an SVG or an HTML file
    served under its own type from Chief's origin would be script running next to the run
    you are reading.
    """
    _check_host(request, host)
    found = service.artifact_content(run_id, artifact_id)
    return Response(
        content=found.data,
        media_type="application/octet-stream",
        headers={
            "X-Chief-Media-Type": found.media_type,
            "X-Chief-File-Name": found.name,
            "Content-Disposition": f'attachment; filename="{found.name}"',
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "no-store",
        },
    )


@router.get("/runs/{run_id}/artifacts/{artifact_id}/modules")
def artifact_modules(
    run_id: str,
    artifact_id: str,
    service: Service,
    request: Request,
    host: Annotated[str | None, Header()] = None,
) -> Any:
    """An MDX document and the co-located modules it imports, as sources.

    Text rather than bytes, because the caller is a compiler rather than a viewer. The same
    two ids and no path — see ``domain/files.py`` for why the graph can be derived without
    the client ever naming one.
    """
    _check_host(request, host)
    return {"modules": service.artifact_modules(run_id, artifact_id)}


@router.post(
    "/runs/{run_id}/artifacts/{artifact_id}/comments",
    response_model=ArtifactRef,
    status_code=status.HTTP_201_CREATED,
)
def comment_on_artifact(
    run_id: str, artifact_id: str, body: CommentCreate, service: Service
) -> ArtifactRef:
    """Attach a person's note to an artifact, wherever in the run it hangs.

    Addressed by artifact id rather than by state path, unlike its neighbours here. An
    artifact's id is unique within the run and is stamped before anyone can see the
    artifact, whereas its path is not something a reader holds: artifacts are read as a
    flat list of everything a run produced, and requiring a path would mean carrying one
    back through that flattening for no gain.
    """
    return service.comment_on_artifact(run_id, artifact_id, body)


@router.post(
    "/runs/{run_id}/instance-updates/{state_path:path}",
    response_model=RunState,
)
def report_nested_instance_update(
    run_id: str, state_path: str, body: InstanceUpdate, service: Service
) -> RunState:
    """``state_path`` ends on the instance id, e.g. ``step_06/inst_01/step_09/inst_00``."""
    tokens = pathlib_.parse_path(state_path)
    return service.report_instance_update(run_id, tokens[:-1], tokens[-1], body)


# --- 2.3 amendments -----------------------------------------------------------------------


@router.post(
    "/runs/{run_id}/amendments", response_model=Amendment, status_code=status.HTTP_201_CREATED
)
def propose_amendment(run_id: str, body: AmendmentCreate, service: Service) -> Amendment:
    return service.propose_amendment(run_id, body)


@router.get("/runs/{run_id}/amendments", response_model=list[Amendment])
def list_amendments(
    run_id: str, service: Service, status_: str | None = Query(None, alias="status")
) -> Any:
    return service.list_amendments(run_id, status_)


@router.get("/amendments", response_model=list[Amendment])
def list_all_amendments(
    service: Service,
    run_id: str | None = Query(None),
    status_: str | None = Query(None, alias="status"),
) -> Any:
    """Amendments across every run. ``?status=pending_approval`` is the approvals inbox.

    Extension. The contract has only the per-run route, which cannot answer "is anything
    waiting?" without one request per run — the question every client waiting on a decision
    actually asks (MCP-SURFACE.md 2).
    """
    return service.list_amendments(run_id, status_)


@router.get("/amendments/{amendment_id}", response_model=Amendment)
def get_amendment(amendment_id: str, service: Service) -> Amendment:
    return service.get_amendment(amendment_id)


@router.post("/amendments/{amendment_id}/approve", response_model=Amendment)
def approve_amendment(
    amendment_id: str,
    service: Service,
    body: AmendmentDecision = Body(default_factory=AmendmentDecision),
) -> Amendment:
    return service.approve_amendment(amendment_id, body)


@router.post("/amendments/{amendment_id}/reject", response_model=Amendment)
def reject_amendment(
    amendment_id: str,
    service: Service,
    body: AmendmentDecision = Body(default_factory=AmendmentDecision),
) -> Amendment:
    return service.reject_amendment(amendment_id, body)


@router.post("/amendments/{amendment_id}/withdraw", response_model=Amendment)
def withdraw_amendment(
    amendment_id: str,
    service: Service,
    body: AmendmentDecision = Body(default_factory=AmendmentDecision),
) -> Amendment:
    return service.withdraw_amendment(amendment_id, body.reason)


# --- 2.4 config ---------------------------------------------------------------------------


@router.get("/config/workflow-approval-policy", response_model=ApprovalPolicy)
def get_workflow_approval_policy(service: Service) -> ApprovalPolicy:
    """Extension: which template instances may be approved without a person (REQ-32/43)."""
    return service.get_workflow_approval_policy()


@router.put("/config/workflow-approval-policy", response_model=ApprovalPolicy)
def put_workflow_approval_policy(body: ApprovalPolicy, service: Service) -> ApprovalPolicy:
    return service.put_workflow_approval_policy(body)


@router.get("/config/approval-policy", response_model=ApprovalPolicy)
def get_approval_policy(service: Service) -> ApprovalPolicy:
    return service.get_approval_policy()


@router.put("/config/approval-policy", response_model=ApprovalPolicy)
def put_approval_policy(body: ApprovalPolicy, service: Service) -> ApprovalPolicy:
    return service.put_approval_policy(body)


# --- extension: audit ---------------------------------------------------------------------


@router.get("/audit")
def get_audit(
    service: Service,
    workflow_id: str | None = Query(None),
    run_id: str | None = Query(None),
    amendment_id: str | None = Query(None),
) -> list[dict]:
    return service.audit_entries(workflow_id=workflow_id, run_id=run_id, amendment_id=amendment_id)


@router.get("/healthz", include_in_schema=False)
def healthz() -> Response:
    return Response(status_code=status.HTTP_204_NO_CONTENT)
