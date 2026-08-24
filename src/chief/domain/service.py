"""The Chief's business logic.

Every server-side invariant from contract section 4 is enforced here rather than in the
API layer, so the MCP surface (contract section 3, next task) gets the same guarantees for
free by calling the same methods.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from .. import lean
from ..errors import (
    InvalidTransition,
    InvariantViolation,
    NotAvailable,
    NotFound,
    ValidationFailed,
)
from ..ids import amendment_id as new_amendment_id
from ..ids import artifact_id as new_artifact_id
from ..ids import comment_id as new_comment_id
from ..ids import instance_id as format_instance_id
from ..ids import note_id as new_note_id
from ..ids import now
from ..ids import proof_graph_id as new_graph_id
from ..ids import run_id as new_run_id
from ..ids import template_id as new_template_id
from ..ids import workflow_id as new_workflow_id
from ..models import (
    Amendment,
    AmendmentCreate,
    AmendmentDecision,
    ApprovalPolicy,
    ArtifactComment,
    ArtifactRef,
    BodyStepUpdate,
    CheckpointOutcome,
    CheckpointResolution,
    CommentCreate,
    GraphOrigin,
    InstanceCreate,
    InstanceUpdate,
    PatchOperation,
    ProofGraph,
    ProofGraphCompile,
    ProofGraphCreate,
    ProofGraphRevise,
    ReviewNote,
    ReviewNoteCreate,
    ReviewNoteDecision,
    RunCreate,
    RunPlan,
    RunState,
    StepInstance,
    StepState,
    StepUpdate,
    TemplateCreate,
    TemplateFromWorkflow,
    TemplateInstantiate,
    TemplateOrigin,
    WorkflowCreate,
    WorkflowDefinition,
    WorkflowLabel,
    WorkflowRevise,
    WorkflowStep,
    WorkflowTemplate,
)
from ..storage import Store
from ..transport import current_transport
from . import files, patch, paths, policy_eval
from . import templates as tmpl
from .derive import recompute, set_status
from .graph import top_level_ids, validate_definition

_SERVER_ONLY_STATUSES = frozenset({"skipped"})
_TERMINAL = frozenset({"completed", "failed", "skipped"})


class Chief:
    def __init__(self, store: Store) -> None:
        self.store = store

    # --- workflows ----------------------------------------------------------------------

    def create_workflow(
        self,
        body: WorkflowCreate,
        origin: TemplateOrigin | None = None,
        graph_origin: GraphOrigin | None = None,
    ) -> WorkflowDefinition:
        workflow_id = body.workflow_id or new_workflow_id()
        if self.store.workflow_exists(workflow_id):
            raise InvalidTransition(f"workflow '{workflow_id}' already exists")
        defn = WorkflowDefinition(
            from_template=origin,
            from_graph=graph_origin,
            workflow_id=workflow_id,
            title=body.title,
            source=body.source,
            generated_by=body.generated_by,
            status="draft",
            version=1,
            steps=body.steps,
            project=body.project,
            origin_dir=body.origin_dir,
        )
        validate_definition(defn)
        self.store.create_workflow(defn)
        with self.store.transaction() as conn:
            self.store.audit(
                conn,
                "workflow.created",
                workflow_id=workflow_id,
                detail={"source": defn.source, "generated_by": defn.generated_by},
            )
        return defn

    def revise_draft(self, workflow_id: str, body: WorkflowRevise) -> WorkflowDefinition:
        """draft -> draft, with a different plan. See ``WorkflowRevise`` for why this is not
        an amendment.

        Refused once the workflow leaves ``draft``: an approved plan may have a run against
        it, and rewriting it underneath that run would change work already in flight without
        anyone deciding to. That is what amendments are for, and they pause the run to do it.
        """
        defn = self.store.get_workflow(workflow_id)
        if defn.status != "draft":
            raise InvalidTransition(
                f"workflow '{workflow_id}' is '{defn.status}', not a draft; a plan that has "
                "been approved is changed by proposing an amendment against its run"
            )
        before = len(defn.steps)
        defn.title = body.title
        defn.steps = body.steps
        # The same validation the plan passed on creation: a revision is a whole plan, not a
        # patch, so nothing carries over that could make an invalid graph acceptable.
        validate_definition(defn)
        with self.store.transaction() as conn:
            self.store.save_workflow(conn, defn)
            self.store.audit(
                conn,
                "workflow.revised",
                workflow_id=workflow_id,
                detail={
                    "reason": body.reason,
                    "steps_before": before,
                    "steps_after": len(defn.steps),
                },
            )
        return defn

    def label_workflow(self, workflow_id: str, body: WorkflowLabel) -> WorkflowDefinition:
        """Which project a workflow belongs to. Not a plan change, so not a revision.

        Allowed at any status, including archived. The workflows that most need labelling
        are the ones that already ran — nothing about a finished plan is altered by filing
        it, and a rule that only drafts could be labelled would make the feature useless for
        every workflow that predates it.
        """
        defn = self.store.get_workflow(workflow_id)
        # Only what the caller actually sent. `project=None` clears the project; omitting it
        # leaves it as it was, and the two arrive looking identical without this.
        sent = body.model_fields_set
        detail: dict[str, Any] = {}
        for field in ("project", "origin_dir"):
            if field not in sent:
                continue
            detail[field] = getattr(body, field)
            detail[f"{field}_was"] = getattr(defn, field)
            setattr(defn, field, getattr(body, field))
        if not detail:
            raise ValidationFailed("nothing to change: send a project or an origin_dir")
        with self.store.transaction() as conn:
            self.store.save_workflow(conn, defn)
            self.store.audit(
                conn, "workflow.labelled", workflow_id=workflow_id, detail=detail
            )
        return self.get_workflow(workflow_id)

    def list_projects(self) -> list[dict[str, Any]]:
        """Every project label in use, with how many workflows carry it.

        Derived rather than stored. A project is not an object with a lifecycle — it is
        whatever labels happen to be on the workflows — so there is nothing to create,
        rename or delete, and nothing that can disagree with the workflows it claims. The
        unlabelled ones are counted too, under a null name: they are the majority on any
        database that predates this, and a list that omitted them would be a list that
        quietly hid most of the history.
        """
        counts: dict[str | None, int] = {}
        for defn in self.store.list_workflows(None):
            counts[defn.project] = counts.get(defn.project, 0) + 1
        return [
            {"project": name, "workflows": counts[name]}
            for name in sorted(counts, key=lambda n: (n is None, (n or "").lower()))
        ]

    def get_workflow(self, workflow_id: str) -> WorkflowDefinition:
        defn = self.store.get_workflow(workflow_id)
        # The one read that carries the review notes. A harness picking a draft back up gets
        # the feedback in the document it was going to fetch anyway, exactly as it gets
        # artifact comments in the run state — no new call, and nothing to remember to ask
        # for. The workflow list leaves them off: it is a list of plans, not of feedback.
        defn.review_notes = self._attach_orphaning(defn, self.store.list_review_notes(workflow_id))
        return defn

    # --- review notes on a draft --------------------------------------------------------

    @staticmethod
    def _attach_orphaning(defn: WorkflowDefinition, notes: list[ReviewNote]) -> list[ReviewNote]:
        """Mark the notes whose step the plan no longer has.

        Computed against the plan as it stands rather than stored, because a revision can
        drop a step id and a later one can bring it back. An orphan is shown, never dropped
        and never auto-resolved: the step vanishing may mean the feedback was acted on, or
        may mean the harness restructured around it without addressing it, and telling those
        apart is the reviewer's job.
        """
        present = defn.steps_by_id()
        return [
            note.model_copy(
                update={"orphaned": note.step_id is not None and note.step_id not in present}
            )
            for note in notes
        ]

    def add_review_note(self, workflow_id: str, body: ReviewNoteCreate) -> ReviewNote:
        """Leave feedback on a plan, for whoever revises it.

        The step's goal is copied onto the note as it reads now. If the step is later
        rewritten or dropped, the note still says what it was about — "was on step_04:
        draft the migration script" — which is the difference between an orphan a person
        can act on and an id that means nothing.
        """
        defn = self.store.get_workflow(workflow_id)
        step = None
        if body.step_id is not None:
            step = defn.step(body.step_id)
            if step is None:
                raise ValidationFailed(
                    f"workflow '{workflow_id}' has no step '{body.step_id}'",
                    details={"workflow_id": workflow_id, "step_id": body.step_id},
                )
        note = ReviewNote(
            note_id=new_note_id(),
            workflow_id=workflow_id,
            step_id=body.step_id,
            step_goal=step.goal if step else None,
            body=body.body,
            author=body.author,
            created_at=now(),
            via=current_transport.get(),
        )
        with self.store.transaction() as conn:
            self.store.add_review_note(conn, note)
            self.store.audit(
                conn,
                "workflow.note_added",
                workflow_id=workflow_id,
                detail={
                    "note_id": note.note_id,
                    "step_id": note.step_id,
                    "author": note.author,
                    "status": defn.status,
                },
            )
        return note

    def list_review_notes(self, workflow_id: str, resolved: bool | None = None) -> list[ReviewNote]:
        defn = self.store.get_workflow(workflow_id)
        notes = self._attach_orphaning(defn, self.store.list_review_notes(workflow_id))
        if resolved is None:
            return notes
        return [n for n in notes if n.resolved is resolved]

    def decide_review_note(
        self, workflow_id: str, note_id: str, decision: ReviewNoteDecision
    ) -> ReviewNote:
        """Close a note, or put it back.

        Not a harness's to call, for the same reason it cannot approve the workflow it
        proposed: a session that can close the feedback it was given can decide its own work
        is finished. It reads the notes and revises the plan; a person judges whether that
        answered them. Hence no MCP tool — see MCP-SURFACE.md.
        """
        defn = self.store.get_workflow(workflow_id)
        note = self.store.get_review_note(workflow_id, note_id)
        if note.resolved is decision.resolved:
            raise InvalidTransition(
                f"review note '{note_id}' is already "
                f"{'resolved' if note.resolved else 'open'}"
            )
        note.resolved = decision.resolved
        note.resolved_at = now() if decision.resolved else None
        note.resolved_by = decision.resolved_by if decision.resolved else None
        with self.store.transaction() as conn:
            self.store.save_review_note(conn, note)
            self.store.audit(
                conn,
                "workflow.note_resolved" if decision.resolved else "workflow.note_reopened",
                workflow_id=workflow_id,
                detail={"note_id": note_id, "decided_by": decision.resolved_by},
            )
        return self._attach_orphaning(defn, [note])[0]

    def get_workflow_version(self, workflow_id: str, version: int) -> WorkflowDefinition:
        return self.store.get_workflow_version(workflow_id, version)

    def list_workflows(self, status: str | None = None) -> list[WorkflowDefinition]:
        return self.store.list_workflows(status)

    @staticmethod
    def _decision_detail(decision: AmendmentDecision | None, **extra: str) -> dict[str, Any]:
        """What a lifecycle decision leaves behind.

        The same shape as an amendment decision, deliberately: a person reading the audit log
        should not have to learn two vocabularies for "who decided this, and why". The reason
        is optional — most approvals need no explanation, and demanding one would train people
        to type nothing useful.
        """
        decision = decision or AmendmentDecision()
        detail: dict[str, Any] = {"decided_by": decision.decided_by, **extra}
        if decision.reason:
            detail["reason"] = decision.reason
        return detail

    def approve_workflow(
        self, workflow_id: str, decision: AmendmentDecision | None = None
    ) -> WorkflowDefinition:
        """draft -> approved (REQ-32). Does not create a run; the harness registers that."""
        defn = self.store.get_workflow(workflow_id)
        if defn.status != "draft":
            raise InvalidTransition(
                f"workflow '{workflow_id}' is '{defn.status}'; only a draft can be approved"
            )
        defn.status = "approved"
        with self.store.transaction() as conn:
            self.store.save_workflow(conn, defn)
            self.store.audit(
                conn,
                "workflow.approved",
                workflow_id=workflow_id,
                detail=self._decision_detail(decision),
            )
        return defn

    def archive_workflow(
        self, workflow_id: str, decision: AmendmentDecision | None = None
    ) -> WorkflowDefinition:
        """draft | approved -> archived. Blocks new runs; runs in progress are unaffected.

        A draft archives too, and must: a superseded draft is otherwise immortal. It cannot
        be deleted, and refusing to archive it leaves it asking to be approved forever, in a
        list a human reads to decide what needs them. Archiving one is not the same act as
        archiving an approved workflow — nothing ever ran from it — so the audit entry keeps
        the state it came from.
        """
        defn = self.store.get_workflow(workflow_id)
        if defn.status == "archived":
            raise InvalidTransition(f"workflow '{workflow_id}' is already archived")
        previous = defn.status
        defn.status = "archived"
        with self.store.transaction() as conn:
            self.store.save_workflow(conn, defn)
            self.store.audit(
                conn,
                "workflow.archived",
                workflow_id=workflow_id,
                detail=self._decision_detail(decision, **{"from": previous}),
            )
        return defn

    def delete_workflow(self, workflow_id: str) -> dict[str, Any]:
        """Remove a workflow and its whole history, permanently.

        Distinct from archiving, and not a replacement for it. Archiving keeps the record and
        stops it asking for anything; this is for the plan that should never have been
        submitted — a mis-generated draft, a duplicate, a test — where keeping it is not
        prudence but clutter in the one list a person reads to decide what needs them.

        Everything the workflow owns goes with it: its versions, its runs and their step
        states, its amendments, its review notes. Two things stay. **The audit trail stays**,
        including a new entry for this deletion — a delete that erases the record of itself
        is not auditable, and REQ-20 makes that log append-only in the first place. **A
        template saved from this workflow stays**, because it stopped being part of this
        workflow the moment it was saved.

        Files on disk are never touched. Chief records references to artifacts, not their
        contents (REQ-46), and a tracker that deletes a person's work because they tidied
        their workflow list would be a tracker nobody could safely tidy.

        A running execution does not block this. The refusal would be the wrong shape: the
        run is not a lock on the record, and someone deleting a workflow mid-run has almost
        always just decided the run is the thing they want gone. What the count in the
        response is for is saying plainly what went.
        """
        defn = self.store.get_workflow(workflow_id)
        runs = self.store.list_runs(workflow_id=workflow_id)
        with self.store.transaction() as conn:
            removed = self.store.delete_workflow(conn, workflow_id)
            # Written after the rows are gone but inside the same transaction, so the trail
            # and the deletion cannot disagree.
            self.store.audit(
                conn,
                "workflow.deleted",
                workflow_id=workflow_id,
                detail={
                    "title": defn.title,
                    "status": defn.status,
                    "version": defn.version,
                    "runs": [r.run_id for r in runs],
                    "removed": removed,
                },
            )
        return {"workflow_id": workflow_id, "title": defn.title, "removed": removed}

    # --- templates ----------------------------------------------------------------------

    def create_template(
        self, body: TemplateCreate, derived_from_workflow_id: str | None = None
    ) -> WorkflowTemplate:
        template_id = body.template_id or new_template_id()
        if self.store.template_exists(template_id):
            raise InvalidTransition(f"template '{template_id}' already exists")
        # The plan has to be a valid graph *before* substitution, not only after: parameters
        # never touch structure, so a template that cannot render a valid plan is broken at
        # authoring time and should be refused then.
        tmpl.validate_template(body.steps, body.parameters, body.title)
        self._validate_template_graph(body.steps, template_id)

        stamp = now()
        template = WorkflowTemplate(
            template_id=template_id,
            title=body.title,
            description=body.description,
            parameters=body.parameters,
            steps=body.steps,
            project=body.project,
            derived_from_workflow_id=derived_from_workflow_id,
            created_at=stamp,
            updated_at=stamp,
        )
        with self.store.transaction() as conn:
            self.store.create_template(conn, template)
            self.store.audit(
                conn,
                "template.created",
                detail={
                    "template_id": template_id,
                    "parameters": [p.name for p in template.parameters],
                },
            )
        return template

    def create_template_from_workflow(
        self, workflow_id: str, body: TemplateFromWorkflow
    ) -> WorkflowTemplate:
        """Turn a plan that exists into the general form of itself."""
        defn = self.store.get_workflow(workflow_id)
        parameters = tmpl.declared_for(body.parameters, body.substitutions)
        steps = tmpl.parameterise(defn.steps, body.substitutions)
        # One write: a second transaction to attach the lineage could fail in between and
        # leave a template indistinguishable from one authored by hand.
        return self.create_template(
            TemplateCreate(
                template_id=body.template_id,
                title=body.title or defn.title,
                description=body.description,
                parameters=parameters,
                steps=steps,
                # Keeping a plan that worked keeps what it was worked on, unless the caller
                # is deliberately generalising it away from that project.
                project=(body.project or None) if body.project is not None else defn.project,
            ),
            derived_from_workflow_id=workflow_id,
        )

    def get_template(self, template_id: str) -> WorkflowTemplate:
        return self.store.get_template(template_id)

    def list_templates(self, status: str | None = None) -> list[WorkflowTemplate]:
        return self.store.list_templates(status)

    def archive_template(self, template_id: str) -> WorkflowTemplate:
        template = self.store.get_template(template_id)
        if template.status == "archived":
            raise InvalidTransition(f"template '{template_id}' is already archived")
        template.status = "archived"
        template.updated_at = now()
        with self.store.transaction() as conn:
            self.store.save_template(conn, template)
            self.store.audit(conn, "template.archived", detail={"template_id": template_id})
        return template

    def instantiate_template(
        self, template_id: str, body: TemplateInstantiate
    ) -> WorkflowDefinition:
        """A template plus values becomes a draft workflow (REQ-32 still applies)."""
        template = self.store.get_template(template_id)
        tmpl.require_active(template)
        values = tmpl.resolve_values(template, body.parameters)

        defn = self.create_workflow(
            WorkflowCreate(
                workflow_id=body.workflow_id,
                title=body.title or tmpl.render_title(template.title, values),
                source="import",
                generated_by=f"template:{template_id}",
                steps=tmpl.render_steps(template.steps, values),
                # The template's label unless this call names one. A project-specific
                # template making unlabelled workflows would be the feature not working.
                project=body.project or template.project,
                origin_dir=body.origin_dir,
            ),
            origin=TemplateOrigin(
                template_id=template_id, template_version=template.version, parameters=values
            ),
        )
        return self._maybe_auto_approve(defn)

    def _maybe_auto_approve(self, defn: WorkflowDefinition) -> WorkflowDefinition:
        """REQ-32 still holds: the workflow is a draft, and a policy may then approve it.

        The gate is not skipped — it is answered by a rule a human wrote and the server
        proved could only ever fire for a template instance. The audit entry names the rule,
        so an auto-approval is never mistaken for someone's decision.
        """
        auto, rule_id = policy_eval.decide_workflow(
            self.store.get_workflow_approval_policy(), defn
        )
        if not auto:
            return defn
        return self.approve_workflow(
            defn.workflow_id,
            AmendmentDecision(
                decided_by=f"policy:{rule_id}", reason="auto-approved by workflow approval policy"
            ),
        )

    def get_workflow_approval_policy(self) -> ApprovalPolicy:
        return self.store.get_workflow_approval_policy()

    def put_workflow_approval_policy(self, policy: ApprovalPolicy) -> ApprovalPolicy:
        policy_eval.validate_workflow_policy(policy)
        with self.store.transaction() as conn:
            self.store.put_workflow_approval_policy(conn, policy)
            self.store.audit(conn, "config.updated", detail={"key": "workflow_approval_policy"})
        return policy

    # --- proof graphs ------------------------------------------------------------------------

    @staticmethod
    def _freshen(graph: ProofGraph) -> ProofGraph:
        """Say whether the stored verdict was reached by the toolchain that is installed now.

        Recomputed on the way out of the store rather than written into the document, for the
        same reason a workflow's timestamps live in columns: a copy inside the document could
        disagree with the world and would go on asserting whatever it said when it was written.
        A graph checked under one toolchain and read under another is not verified — saying so
        is the whole difference between a fact and a badge.
        """
        stored = graph.verification.toolchain if graph.verification else None
        graph.stale = bool(stored and stored != lean.toolchain_version())
        return graph

    def create_proof_graph(self, body: ProofGraphCreate) -> ProofGraph:
        graph_id = body.graph_id or new_graph_id()
        if self.store.proof_graph_exists(graph_id):
            raise InvalidTransition(f"graph '{graph_id}' already exists")
        stamp = now()
        graph = ProofGraph(
            graph_id=graph_id,
            title=body.title,
            lean_source=body.lean_source,
            project=body.project,
            origin_dir=body.origin_dir,
            generated_by=body.generated_by,
            created_at=stamp,
            updated_at=stamp,
        )
        with self.store.transaction() as conn:
            self.store.create_proof_graph(conn, graph)
            self.store.audit(conn, "graph.created", detail={"graph_id": graph_id})
        return graph

    def get_proof_graph(self, graph_id: str) -> ProofGraph:
        return self._freshen(self.store.get_proof_graph(graph_id))

    def list_proof_graphs(
        self, *, status: str | None = None, project: str | None = None
    ) -> list[ProofGraph]:
        stored = self.store.list_proof_graphs(status=status, project=project)
        return [self._freshen(p) for p in stored]

    def revise_proof_graph(self, graph_id: str, body: ProofGraphRevise) -> ProofGraph:
        """Replace the source. The verdict does not survive it.

        A graph is not versioned the way a workflow is, because there is nothing to protect:
        nobody has approved it and nothing has executed from it. What must not survive an edit
        is the verdict — it belongs to the text that was checked, and carrying it across would
        let changed source wear a badge earned by source that no longer exists.
        """
        graph = self.store.get_proof_graph(graph_id)
        graph.title = body.title or graph.title
        graph.lean_source = body.lean_source
        graph.status = "draft"
        graph.verification = None
        graph.verified_at = None
        graph.updated_at = now()
        with self.store.transaction() as conn:
            self.store.save_proof_graph(conn, graph)
            self.store.audit(
                conn, "graph.revised", detail={"graph_id": graph_id, "reason": body.reason}
            )
        return self._freshen(graph)

    def verify_proof_graph(self, graph_id: str) -> ProofGraph:
        """Check the graph's logic and record what came back.

        The server does the checking rather than trusting a client's word for it, which is what
        makes the verdict worth storing: a green graph means this process ran the kernel over
        that source, not that something told it so.
        """
        graph = self.store.get_proof_graph(graph_id)
        try:
            result = lean.verify_source(graph.lean_source)
        except lean.LeanUnavailable as exc:
            raise NotAvailable(str(exc)) from exc

        graph.verification = result
        graph.status = result.status
        graph.verified_at = now()
        graph.updated_at = graph.verified_at
        with self.store.transaction() as conn:
            self.store.save_proof_graph(conn, graph)
            self.store.audit(
                conn,
                "graph.verified",
                detail={
                    "graph_id": graph_id,
                    "status": result.status,
                    "toolchain": result.toolchain,
                    "errors": len(result.errors),
                },
            )
        return self._freshen(graph)

    def compile_proof_graph(self, graph_id: str, body: ProofGraphCompile) -> WorkflowDefinition:
        """Lower a checked graph into a draft workflow.

        Refused unless the graph is verified *by the toolchain that is installed now* — a graph
        whose verdict predates a toolchain change has to be checked again before it can become
        something a person is asked to approve.
        """
        doc = self._freshen(self.store.get_proof_graph(graph_id))
        if doc.status != "verified":
            raise InvalidTransition(
                f"graph '{graph_id}' is {doc.status}; only a verified graph can be compiled"
            )
        if doc.stale:
            raise InvalidTransition(
                f"graph '{graph_id}' was verified by {doc.verification.toolchain} and this "
                f"instance runs {lean.toolchain_version()}; verify it again before compiling"
            )
        extracted = doc.graph
        if extracted is None:  # pragma: no cover - a verified graph carries its extraction
            raise InvariantViolation(f"graph '{graph_id}' is verified but carries no graph")

        create = lean.compile_graph(
            extracted,
            title=body.title or doc.title,
            project=body.project or doc.project,
            origin_dir=body.origin_dir or doc.origin_dir,
            generated_by=doc.generated_by or f"graph:{graph_id}",
        )
        create.workflow_id = body.workflow_id
        defn = self.create_workflow(
            create,
            graph_origin=GraphOrigin(
                graph_id=graph_id,
                contracts_refined=extracted.stats.contracts_refined,
                contracts_any=extracted.stats.contracts_any,
                toolchain=doc.verification.toolchain if doc.verification else None,
                verified_at=doc.verified_at,
            ),
        )

        doc.compiled_to = [*doc.compiled_to, defn.workflow_id]
        doc.updated_at = now()
        with self.store.transaction() as conn:
            self.store.save_proof_graph(conn, doc)
            self.store.audit(
                conn,
                "graph.compiled",
                workflow_id=defn.workflow_id,
                detail={"graph_id": graph_id},
            )
        return defn

    def delete_proof_graph(self, graph_id: str) -> dict[str, Any]:
        """Remove a graph. The workflows compiled from it are untouched.

        A compiled workflow is a graph in its own right from the moment it exists — it carries
        its own steps and its own lineage record — so deleting what it was made from does not
        take anything it needs with it.
        """
        graph = self.store.get_proof_graph(graph_id)
        with self.store.transaction() as conn:
            self.store.delete_proof_graph(conn, graph_id)
            self.store.audit(
                conn,
                "graph.deleted",
                detail={"graph_id": graph_id, "compiled_to": graph.compiled_to},
            )
        return {"graph_id": graph_id, "deleted": True, "compiled_to": graph.compiled_to}

    def lean_available(self) -> dict[str, Any]:
        """Whether this instance can check a graph, and with what."""
        return {"available": lean.available(), "toolchain": lean.toolchain_version()}

    def _validate_template_graph(self, steps, template_id: str) -> None:
        """Validate the graph's shape by borrowing the workflow validator.

        Placeholders live in text, never in ids or edges, so the unrendered graph has exactly
        the same graph as every graph it will ever produce.
        """
        probe = WorkflowDefinition(
            workflow_id=template_id, title="probe", source="generated", steps=steps
        )
        validate_definition(probe)

    # --- runs ---------------------------------------------------------------------------

    def register_run(
        self,
        workflow_id: str,
        body: RunCreate,
        *,
        parent_run_id: str | None = None,
        parent_step_path: list[str] | None = None,
    ) -> RunState:
        defn = self.store.get_workflow(workflow_id)
        if defn.status == "draft":
            raise InvalidTransition(
                f"workflow '{workflow_id}' is still a draft and needs human approval before "
                "a run can be registered (REQ-32)"
            )
        if defn.status == "archived":
            raise InvalidTransition(f"workflow '{workflow_id}' is archived; no new runs")

        run_id = body.run_id or new_run_id()
        if self.store.run_exists(run_id):
            raise InvalidTransition(f"run '{run_id}' already exists")

        stamp = now()
        run = RunState(
            metadata=body.metadata,
            run_id=run_id,
            workflow_id=workflow_id,
            base_version=defn.version,
            applied_amendment_ids=[],
            status="running",
            step_states={
                step_id: StepState(step_id=step_id) for step_id in top_level_ids(defn.steps)
            },
            created_at=stamp,
            updated_at=stamp,
        )
        effective = deepcopy(defn)
        recompute(run, effective)
        with self.store.transaction() as conn:
            self.store.create_run(
                conn,
                run,
                effective,
                parent_run_id=parent_run_id,
                parent_step_path=parent_step_path,
            )
            self.store.audit(
                conn,
                "run.registered",
                workflow_id=workflow_id,
                run_id=run_id,
                detail={"base_version": defn.version, "metadata": body.metadata},
            )
        return run

    def _spawn_child_run(self, step: WorkflowStep, parent_run_id: str, path: list[str]) -> RunState:
        """Instantiate a workflow_ref step's template and register a run of it as a child.

        Auto-approved unconditionally, unlike ``instantiate_template``'s policy-gated path:
        there is no one to hand a draft to here, so a workflow_ref step that could only ever
        sit as an unapproved draft would be a step that can never move past 'running'.
        """
        assert step.ref_template_id is not None
        defn = self.instantiate_template(
            step.ref_template_id,
            TemplateInstantiate(parameters=step.ref_parameters),
        )
        if defn.status == "draft":
            defn = self.approve_workflow(
                defn.workflow_id,
                AmendmentDecision(
                    decided_by=f"workflow_ref:{step.id}",
                    reason=f"auto-approved as the child of workflow_ref step '{step.id}'",
                ),
            )
        return self.register_run(
            defn.workflow_id,
            RunCreate(metadata={"spawned_by": f"{parent_run_id}:{'.'.join(path)}"}),
            parent_run_id=parent_run_id,
            parent_step_path=path,
        )

    def _cascade_child_completion(self, run: RunState) -> None:
        """Once a run reaches a terminal status, push that onto the step that spawned it.

        Only runs registered by a workflow_ref step (``_spawn_child_run``) carry a parent
        link; an ordinary run's is None and this is a no-op. Recurses, since the parent run
        this just completed may itself be someone's child.
        """
        if run.status not in ("completed", "failed"):
            return
        link = self.store.get_run_parent_link(run.run_id)
        if link is None:
            return
        parent_run_id, parent_path = link
        parent, parent_effective = self.store.get_run(parent_run_id)
        _, parent_state, _ = paths.resolve(parent, parent_path, create=True)
        if parent_state.status != "blocked":
            # Already resolved (e.g. this cascade already ran, or a history-edit replay
            # cleared it) — nothing left to push.
            return
        set_status(parent_state, run.status)
        parent_paused = self.store.pending_amendment(parent_run_id) is not None
        recompute(parent, parent_effective, paused=parent_paused)
        with self.store.transaction() as conn:
            self.store.save_run(conn, parent)
            self.store.audit(
                conn,
                "step.updated",
                workflow_id=parent.workflow_id,
                run_id=parent_run_id,
                detail={
                    "path": parent_path,
                    "status": run.status,
                    "cause": f"child run '{run.run_id}' finished",
                },
            )
        self._cascade_child_completion(parent)

    def get_run(self, run_id: str) -> RunState:
        run, _ = self.store.get_run(run_id)
        return run

    def get_run_plan(self, run_id: str) -> RunPlan:
        """The plan this run is actually executing, with the lineage that identifies it.

        Returned as its own object rather than a WorkflowDefinition: a materialised
        effective plan has no meaningful ``version``, since it is base_version plus this
        run's own amendments and no shared version number names that document.
        """
        run, effective = self.store.get_run(run_id)
        return RunPlan(
            run_id=run.run_id,
            workflow_id=run.workflow_id,
            title=effective.title,
            base_version=run.base_version,
            applied_amendment_ids=list(run.applied_amendment_ids),
            steps=effective.steps,
        )

    def list_runs(
        self, status: str | None = None, workflow_id: str | None = None
    ) -> list[RunState]:
        return self.store.list_runs(status, workflow_id)

    # --- execution reporting ------------------------------------------------------------

    def _load_for_update(self, run_id: str) -> tuple[RunState, WorkflowDefinition, bool]:
        run, effective = self.store.get_run(run_id)
        paused = self.store.pending_amendment(run_id) is not None
        return run, effective, paused

    @staticmethod
    def _stamp_artifacts(artifacts: list[ArtifactRef]) -> list[ArtifactRef]:
        stamped = []
        for artifact in artifacts:
            # ArtifactRef is both the stored shape and the shape a harness submits, so the
            # field is declared and `extra="forbid"` will not catch this. A harness sending
            # comments would be reporting back what it was told rather than what it did.
            if artifact.comments:
                raise InvariantViolation(
                    "artifact comments are not a harness's to report — they are what a "
                    "person said about the work, added through the comment endpoint",
                    details={"artifact_id": artifact.artifact_id},
                )
            if artifact.artifact_id is None:
                artifact = artifact.model_copy(update={"artifact_id": new_artifact_id()})
            stamped.append(artifact)
        return stamped

    @staticmethod
    def _find_artifact(run: RunState, artifact_id: str) -> ArtifactRef | None:
        """The artifact with this id, wherever in the run it hangs.

        Addressed by id rather than by state path, unlike everything else that reaches into
        a run. An artifact's id is unique within the run and already stamped by the time
        anyone can see it, whereas its path is not something the reader has: the UI
        flattens artifacts out of the tree to list them, and a nested one would otherwise
        have to carry a path back through the flattening just to be commented on.
        """

        def walk(container: dict[str, StepState]) -> ArtifactRef | None:
            for state in container.values():
                for artifact in state.artifacts:
                    if artifact.artifact_id == artifact_id:
                        return artifact
                # `instances` is None on a task, not empty: a task has no instances rather
                # than none yet, and the model keeps that distinction.
                for instance in state.instances or []:
                    for artifact in instance.artifacts:
                        if artifact.artifact_id == artifact_id:
                            return artifact
                    found = walk(instance.step_states or {})
                    if found is not None:
                        return found
            return None

        return walk(run.step_states)

    def comment_on_artifact(
        self, run_id: str, artifact_id: str, body: CommentCreate
    ) -> ArtifactRef:
        """Attach a person's note to an artifact.

        Deliberately not blocked by the completed-step rule that governs everything else
        reaching into a finished step. Commenting on a finished step's output is the point —
        "this draft is the one, match its tone" is said about work that is done — and a
        comment annotates a result rather than changing one, so it is not a `history_edit`
        and needs no amendment. The artifact, its `ref` and its `data` are untouched; the
        recorded result still says exactly what the harness reported.
        """
        run, _, _ = self._load_for_update(run_id)
        artifact = self._find_artifact(run, artifact_id)
        if artifact is None:
            raise NotFound(
                f"run '{run_id}' has no artifact '{artifact_id}'",
                details={"run_id": run_id, "artifact_id": artifact_id},
            )
        artifact.comments.append(
            ArtifactComment(
                comment_id=new_comment_id(),
                body=body.body,
                author=body.author,
                created_at=now(),
                via=current_transport.get(),
            )
        )
        with self.store.transaction() as conn:
            self.store.save_run(conn, run)
            self.store.audit(
                conn,
                "artifact.commented",
                workflow_id=run.workflow_id,
                run_id=run_id,
                detail={"artifact_id": artifact_id, "author": body.author},
            )
        return artifact

    def artifact_content(self, run_id: str, artifact_id: str) -> files.ArtifactFile:
        """The bytes of the file an artifact names.

        The only path Chief ever reads, and it never comes from the caller: run id plus
        artifact id in, the artifact's own ``ref`` resolved against the workflow's recorded
        ``origin_dir`` out. See ``domain/files.py`` for why that is narrower than any root a
        flag could have named.
        """
        run, _, _ = self._load_for_update(run_id)
        artifact = self._find_artifact(run, artifact_id)
        if artifact is None:
            raise NotFound(
                f"run '{run_id}' has no artifact '{artifact_id}'",
                details={"run_id": run_id, "artifact_id": artifact_id},
            )
        # The workflow as it stands, not the run's effective plan: `origin_dir` is a property
        # of the record rather than of the plan, so it is not carried on the run's copy.
        workflow = self.store.get_workflow(run.workflow_id)
        path = files.resolve(artifact.ref, workflow.origin_dir)
        data, media = files.read(path)
        return files.ArtifactFile(path=path, data=data, media_type=media, name=path.name)

    def artifact_modules(self, run_id: str, artifact_id: str) -> dict[str, str]:
        """An MDX artifact and the components sitting beside it, as sources.

        Same addressing as ``artifact_content``: two ids in, and every path derived from
        what the files themselves import. The runtime that renders this executes in a
        sandbox at an opaque origin, so what is served here never runs anywhere it could
        reach Chief. See CONTRACT-NOTES.md #37.
        """
        found = self.artifact_content(run_id, artifact_id)
        if found.path.suffix.lower() not in (".mdx", ".md"):
            raise ValidationFailed(
                "only an .mdx document has components to resolve",
                details={"path": str(found.path)},
            )
        return files.module_graph(found.path)

    def _check_addressable(self, defn: WorkflowDefinition, path: list[str]) -> None:
        paths.validate_against_definition(defn.steps_by_id(), path)
        if len(path) == 1 and path[0] not in top_level_ids(defn.steps):
            raise ValidationFailed(
                f"step '{path[0]}' lives inside a loop/parallel body; address it through the "
                "instance it runs in",
                details={"step_id": path[0]},
            )

    def report_step_update(
        self, run_id: str, path: list[str], update: StepUpdate | BodyStepUpdate
    ) -> RunState:
        run, effective, paused = self._load_for_update(run_id)
        self._check_addressable(effective, path)
        step = effective.step(path[-1])
        assert step is not None

        if step.is_construct:
            if update.status is not None:
                raise InvariantViolation(
                    f"step '{step.id}' is type '{step.type}'; its status is derived from its "
                    "instances and cannot be reported directly",
                    details={"step_id": step.id},
                )
        elif update.instances_closed is not None:
            raise ValidationFailed(
                f"step '{step.id}' is type '{step.type}' and has no instances to close",
                details={"step_id": step.id},
            )

        if step.is_checkpoint and update.status not in (None, "running"):
            raise InvariantViolation(
                f"step '{step.id}' is a checkpoint; a harness can report reaching it "
                f"('running') but not how it turned out — '{update.status}' is a person's "
                "to give, through resolve_checkpoint",
                details={"step_id": step.id, "status": update.status},
            )
        if step.is_workflow_ref and update.status not in (None, "running"):
            raise InvariantViolation(
                f"step '{step.id}' is a workflow_ref; a harness can report reaching it "
                f"('running') but not how it turned out — '{update.status}' is cascaded "
                "automatically from its child run",
                details={"step_id": step.id, "status": update.status},
            )

        _, state, enclosing = paths.resolve(run, path, create=True)
        if enclosing is not None and path[-1] not in (enclosing.body or []):
            raise ValidationFailed(
                f"step '{path[-1]}' is not part of instance '{enclosing.instance_id}'",
                details={"step_id": path[-1], "instance_id": enclosing.instance_id},
            )
        if state.status in _TERMINAL:
            # REQ-14: a recorded result is immutable. Re-running or rewriting one goes
            # through a human-approved history_edit amendment, not a plain update.
            raise InvariantViolation(
                f"step '{path[-1]}' is already '{state.status}' and its result is immutable; "
                "propose a history_edit amendment with replay_step to re-run it (REQ-14, "
                "REQ-41)",
                details={"step_id": path[-1], "status": state.status},
            )

        self._check_criteria(step, update, state.criteria_met)

        state.summary = update.summary
        if update.criteria_met:
            state.criteria_met.update(update.criteria_met)
        if update.artifacts:
            state.artifacts.extend(self._stamp_artifacts(update.artifacts))
        if update.metadata:
            state.metadata.update(update.metadata)
        if update.instances_closed is not None:
            if state.instances_closed and not update.instances_closed:
                raise InvariantViolation(
                    f"step '{step.id}' has already been closed to new instances; reopening it "
                    "would make the derived completion of the construct unstable",
                    details={"step_id": step.id},
                )
            state.instances_closed = update.instances_closed
            if state.instances is None:
                state.instances = []
        if update.status is not None:
            if update.status in _SERVER_ONLY_STATUSES:
                raise InvariantViolation(f"'{update.status}' is set by the server only")
            if step.is_workflow_ref and update.status == "running" and state.child_run_id is None:
                state.child_run_id = self._spawn_child_run(step, run.run_id, path).run_id
            # Reaching a checkpoint or a workflow_ref is all a harness gets to say about
            # one. Reporting `running` hands it to a person (or, for workflow_ref, to the
            # child run just spawned) and the server records `blocked`; the outcome is
            # theirs to give — through resolve_checkpoint, or automatically once the child
            # run finishes.
            set_status(
                state, "blocked" if (step.is_checkpoint or step.is_workflow_ref) else update.status
            )

        recompute(run, effective, paused=paused)
        with self.store.transaction() as conn:
            self.store.save_run(conn, run)
            self.store.audit(
                conn,
                "step.updated",
                workflow_id=run.workflow_id,
                run_id=run_id,
                detail={
                    "path": path,
                    "status": update.status,
                    "instances_closed": update.instances_closed,
                    "summary": update.summary,
                    "artifact_count": len(update.artifacts),
                },
            )
        self._cascade_child_completion(run)
        return run

    @staticmethod
    def _check_instance_params(step: WorkflowStep, metadata: dict[str, Any]) -> None:
        """Every declared parameter must arrive with the instance that is being registered.

        Unknown keys are *allowed*, unlike a checkpoint's response. Instance metadata is
        load-bearing free-form — timings, seeds, token counts — and refusing anything
        undeclared would make declaring a parameter cost more than it gives. The declaration
        names a required subset, not a schema.

        Presence, not truthiness: the value is ``Any``, so a parameter legitimately supplied
        as ``0``, ``False`` or an empty list must count as given. Only an absent key, or a
        string that is blank once trimmed, is missing. See CONTRACT-NOTES.md #40.
        """
        missing = sorted(
            spec.name
            for spec in step.instance_param_specs
            if spec.required
            and (
                spec.name not in metadata
                or metadata[spec.name] is None
                or (isinstance(metadata[spec.name], str) and not metadata[spec.name].strip())
            )
        )
        if missing:
            raise ValidationFailed(
                f"step '{step.id}' requires {missing} on every {step.instance_kind} — it is "
                "what tells this one from the others; pass them in the instance's metadata",
                details={"step_id": step.id, "missing": missing},
            )

    @staticmethod
    def _check_criteria(
        step: WorkflowStep,
        update: StepUpdate | BodyStepUpdate,
        recorded: dict[str, str],
    ) -> None:
        """A step with criteria cannot be reported done until each one has been answered for.

        Chief cannot judge whether a criterion is satisfied — it has no access to the work
        and never will. What it can do is refuse a completion that has not addressed each
        criterion *by name*, which is forced enumeration rather than verification. That is
        worth having for the reason REQ-48 requires a real summary: the cost of the gate is
        one sentence per criterion, and the thing it catches is a step called done while a
        condition someone wrote down was quietly skipped. See CONTRACT-NOTES.md #39.
        """
        by_id = {c.id: c for c in step.criteria}
        unknown = sorted(set(update.criteria_met) - set(by_id))
        if unknown:
            raise ValidationFailed(
                f"step '{step.id}' has no criteria {', '.join(unknown)}",
                details={"step_id": step.id, "unknown": unknown, "criteria": sorted(by_id)},
            )
        if not by_id or update.status != "completed":
            return

        # Against everything answered so far, not just this update: evidence accumulates the
        # way artifacts and metadata do, so a criterion answered on the way through does not
        # have to be restated at the end.
        answered = {**recorded, **update.criteria_met}
        missing = [c for c in step.criteria if not answered.get(c.id, "").strip()]
        if missing:
            raise InvariantViolation(
                f"step '{step.id}' cannot be completed yet: "
                + "; ".join(f"{c.id} ({c.text}) is unanswered" for c in missing)
                + ". Say in criteria_met how each was met, or — if one cannot be — keep "
                "working and report again, report 'failed', or propose an amendment "
                "changing the criteria. Do not report completion around an unmet criterion",
                details={
                    "step_id": step.id,
                    "unmet": [{"id": c.id, "text": c.text} for c in missing],
                },
            )

    @staticmethod
    def _check_response(step: WorkflowStep, response: dict[str, str], *, approved: bool) -> None:
        """The answers must match what the checkpoint asked for.

        Validated rather than kept as-is because the harness reads these back by name: an
        unnoticed typo in a key would surface as a missing answer at the point the harness
        acts on it, long after the person who typed it has gone.

        A required field is only required to say *yes*. Saying no is exactly the case where
        you do not have the answers — a person rejecting "which maintenance window?" is
        rejecting because there isn't one — and demanding them to decline would make the
        cheapest way out of a checkpoint be to fill it in with anything. A typo'd key is
        still refused either way: that is a mistake, not an answer withheld.
        """
        declared = {f.name: f for f in step.field_specs}
        unknown = sorted(set(response) - set(declared))
        if unknown:
            raise ValidationFailed(
                f"checkpoint '{step.id}' did not ask for {unknown}",
                details={"step_id": step.id, "unknown": unknown, "declared": sorted(declared)},
            )
        missing = sorted(
            name
            for name, spec in declared.items()
            if spec.required and not response.get(name, "").strip()
        )
        if missing and approved:
            raise ValidationFailed(
                f"checkpoint '{step.id}' requires {missing}",
                details={"step_id": step.id, "missing": missing},
            )

    def resolve_checkpoint(
        self, run_id: str, path: list[str], resolution: CheckpointResolution
    ) -> RunState:
        """Record a person's decision at a blocked checkpoint and let the run move again.

        Approving completes the step; rejecting fails it, which cascades ``skipped`` to
        everything downstream through the ordinary dependency rule — a rejected checkpoint
        stops that branch of the plan rather than quietly letting it proceed.

        A rejection is only accepted with a note. "No" without a reason leaves the harness
        nothing to propose an amendment from, and it is the one field a person is always in
        a position to fill in.
        """
        run, effective, paused = self._load_for_update(run_id)
        self._check_addressable(effective, path)
        step = effective.step(path[-1])
        assert step is not None
        if not step.is_checkpoint:
            raise ValidationFailed(
                f"step '{step.id}' is type '{step.type}', not a checkpoint",
                details={"step_id": step.id},
            )

        _, state, _ = paths.resolve(run, path, create=True)
        if state.status != "blocked":
            raise InvalidTransition(
                f"checkpoint '{step.id}' is '{state.status}', not 'blocked'; there is nothing "
                "waiting on a decision here"
                + (" — it has already been decided" if state.checkpoint else ""),
                details={"step_id": step.id, "status": state.status},
            )
        if resolution.decision == "rejected" and not (resolution.note or "").strip():
            raise ValidationFailed(
                f"rejecting checkpoint '{step.id}' needs a note saying why",
                details={"step_id": step.id},
            )
        approved = resolution.decision == "approved"
        self._check_response(step, resolution.response, approved=approved)

        state.checkpoint = CheckpointOutcome(
            decision=resolution.decision,
            response=dict(resolution.response),
            note=resolution.note,
            decided_by=resolution.decided_by,
            decided_at=now(),
            via=current_transport.get(),
        )
        verb = "Approved" if approved else "Rejected"
        state.summary = f"{verb} by {resolution.decided_by}" + (
            f": {resolution.note}" if resolution.note else "."
        )
        set_status(state, "completed" if approved else "failed")

        recompute(run, effective, paused=paused)
        with self.store.transaction() as conn:
            self.store.save_run(conn, run)
            self.store.audit(
                conn,
                "checkpoint.resolved",
                workflow_id=run.workflow_id,
                run_id=run_id,
                detail={
                    "path": path,
                    "decision": resolution.decision,
                    "decided_by": resolution.decided_by,
                    "note": resolution.note,
                    "fields": sorted(resolution.response),
                },
            )
        self._cascade_child_completion(run)
        return run

    def register_instance(
        self, run_id: str, path: list[str], body: InstanceCreate
    ) -> tuple[RunState, StepInstance]:
        run, effective, paused = self._load_for_update(run_id)
        self._check_addressable(effective, path)
        step = effective.step(path[-1])
        assert step is not None
        if not step.is_construct:
            raise ValidationFailed(
                f"step '{step.id}' is type 'task' and has no instances",
                details={"step_id": step.id},
            )
        if body.kind is not None and body.kind != step.instance_kind:
            raise ValidationFailed(
                f"step '{step.id}' is a {step.type}; its instances are of kind "
                f"'{step.instance_kind}', not '{body.kind}'",
                details={"step_id": step.id},
            )

        self._check_instance_params(step, body.metadata)

        _, state, _ = paths.resolve(run, path, create=True)
        if state.instances is None:
            state.instances = []
        if state.instances_closed:
            raise InvariantViolation(
                f"step '{step.id}' is closed to new instances",
                details={"step_id": step.id},
            )
        if state.status in _TERMINAL:
            raise InvariantViolation(
                f"step '{step.id}' is '{state.status}' and cannot take new instances",
                details={"step_id": step.id, "status": state.status},
            )

        index = body.index if body.index is not None else len(state.instances)
        if any(i.index == index for i in state.instances):
            raise InvalidTransition(f"step '{step.id}' already has an instance at index {index}")
        instance_id = body.instance_id or format_instance_id(index)
        if state.instance(instance_id) is not None:
            raise InvalidTransition(f"instance '{instance_id}' already exists on '{step.id}'")

        instance = StepInstance(
            instance_id=instance_id,
            parent_step_id=step.id,
            kind=step.instance_kind,
            index=index,
            status="pending",
            summary=body.summary,
            metadata=body.metadata,
            body=list(step.body_ids),
            step_states={b: StepState(step_id=b) for b in step.body_ids},
        )
        state.instances.append(instance)
        if state.started_at is None:
            state.started_at = now()

        recompute(run, effective, paused=paused)
        with self.store.transaction() as conn:
            self.store.save_run(conn, run)
            self.store.audit(
                conn,
                "instance.registered",
                workflow_id=run.workflow_id,
                run_id=run_id,
                detail={"path": path, "instance_id": instance_id, "index": index},
            )
        refreshed = paths.resolve_instance(run, path, instance_id)
        return run, refreshed

    def report_instance_update(
        self, run_id: str, path: list[str], instance_id: str, update: InstanceUpdate
    ) -> RunState:
        run, effective, paused = self._load_for_update(run_id)
        self._check_addressable(effective, path)
        step = effective.step(path[-1])
        assert step is not None
        instance = paths.resolve_instance(run, path, instance_id)
        if instance.status in _TERMINAL:
            raise InvariantViolation(
                f"instance '{instance_id}' is already '{instance.status}' and its result is "
                "immutable; propose a history_edit amendment with replay_step to re-run it",
                details={"instance_id": instance_id, "status": instance.status},
            )
        body = instance.body or step.body_ids

        if update.status is not None:
            if update.status in _SERVER_ONLY_STATUSES:
                raise InvariantViolation(f"'{update.status}' is set by the server only")
            if len(body) > 1:
                raise InvariantViolation(
                    f"instance '{instance_id}' has a {len(body)}-step body; its status is "
                    "derived from those steps. Report against the body step instead",
                    details={"instance_id": instance_id, "body": body},
                )

        instance.summary = update.summary
        if update.artifacts:
            instance.artifacts.extend(self._stamp_artifacts(update.artifacts))
        if update.metadata:
            instance.metadata.update(update.metadata)
        if update.status is not None:
            # Single-step body: write through to the body step so instance status stays
            # derived from exactly one rule (contract 1.5).
            target = instance.step_states.setdefault(body[0], StepState(step_id=body[0]))
            target.summary = update.summary
            set_status(target, update.status)
            set_status(instance, update.status)

        recompute(run, effective, paused=paused)
        with self.store.transaction() as conn:
            self.store.save_run(conn, run)
            self.store.audit(
                conn,
                "instance.updated",
                workflow_id=run.workflow_id,
                run_id=run_id,
                detail={
                    "path": path,
                    "instance_id": instance_id,
                    "status": update.status,
                    "summary": update.summary,
                },
            )
        return run

    # --- amendments ---------------------------------------------------------------------

    def _validate_operations(
        self,
        run: RunState,
        effective: WorkflowDefinition,
        kind: str,
        operations: list[PatchOperation],
    ) -> None:
        """Every check an amendment must pass, run at both proposal and approval.

        Re-running these at approval is not belt-and-braces: a run keeps executing while an
        amendment waits for a human, so a target that was `pending` when the amendment was
        classified can be `completed` by the time it is approved. Checking only at proposal
        time leaves a window where a forward amendment silently destroys a completed result.
        """
        patch.validate_targets(run, effective, operations)
        required = patch.classify_required_kind(run, effective, operations)
        if required == "history_edit" and kind == "forward":
            raise InvariantViolation(
                "this amendment alters or replays a step or instance that is already "
                "completed or failed, so it must be submitted with kind 'history_edit' "
                "(REQ-14, REQ-41)",
                details={"required_kind": "history_edit", "submitted_kind": kind},
            )
        patch.check_type_changes(run, effective, operations)
        retired = self.store.retired_step_ids(run.workflow_id)
        # Dry run: raises if the operation set cannot apply cleanly as a whole.
        candidate = patch.preview_definition(effective, operations, retired=retired)
        patch.check_scope_moves(run, effective, candidate)
        patch.dry_run_state_effects(run, effective, candidate, operations)

    def propose_amendment(self, run_id: str, body: AmendmentCreate) -> Amendment:
        run, effective = self.store.get_run(run_id)
        if self.store.pending_amendment(run_id) is not None:
            raise InvariantViolation(
                f"run '{run_id}' already has an amendment awaiting a decision; a run may only "
                "have one pending amendment at a time"
            )

        self._validate_operations(run, effective, body.kind, body.operations)

        amendment = Amendment(
            amendment_id=new_amendment_id(),
            run_id=run_id,
            workflow_id=run.workflow_id,
            proposed_by=body.proposed_by,
            kind=body.kind,
            reason=body.reason,
            operations=body.operations,
            status="pending_approval",
            created_at=now(),
        )

        recompute(run, effective, paused=True)
        with self.store.transaction() as conn:
            self.store.create_amendment(conn, amendment)
            self.store.save_run(conn, run)
            self.store.audit(
                conn,
                "amendment.proposed",
                workflow_id=run.workflow_id,
                run_id=run_id,
                amendment_id=amendment.amendment_id,
                detail={
                    "kind": amendment.kind,
                    "reason": amendment.reason,
                    "operations": [op.model_dump(mode="json") for op in amendment.operations],
                },
            )

        auto, rule_id = policy_eval.decide(self.store.get_approval_policy(), amendment)
        if auto:
            return self.approve_amendment(
                amendment.amendment_id,
                AmendmentDecision(
                    decided_by=f"policy:{rule_id}", reason="auto-approved by approval policy"
                ),
            )
        return amendment

    def get_amendment(self, amendment_id: str) -> Amendment:
        return self.store.get_amendment(amendment_id)

    def list_amendments(
        self, run_id: str | None = None, status: str | None = None
    ) -> list[Amendment]:
        # Scoped to a run, an unknown id is a 404 rather than an empty list, so a typo is not
        # mistaken for "nothing pending". Unscoped there is nothing to check: the empty list
        # is the honest answer.
        if run_id is not None and not self.store.run_exists(run_id):
            raise NotFound(f"run '{run_id}' not found")
        return self.store.list_amendments(run_id, status)

    def approve_amendment(self, amendment_id: str, decision: AmendmentDecision) -> Amendment:
        amendment = self.store.get_amendment(amendment_id)
        self._require_pending(amendment)
        if amendment.kind == "history_edit" and decision.decided_by.startswith("policy"):
            raise InvariantViolation(
                "a history_edit amendment always requires an explicit human decision and can "
                "never be approved by policy (contract 1.9, section 4)"
            )

        run, effective = self.store.get_run(amendment.run_id)
        workflow = self.store.get_workflow(amendment.workflow_id)
        # The run kept executing while this waited for a decision.
        self._validate_operations(run, effective, amendment.kind, amendment.operations)
        retired = self.store.retired_step_ids(amendment.workflow_id)

        before = deepcopy(effective)
        effective.steps = patch.apply_to_steps(
            effective.steps, amendment.operations, retired_step_ids=retired
        )
        try:
            workflow.steps = patch.apply_to_steps(
                workflow.steps, amendment.operations, retired_step_ids=retired
            )
        except (NotFound, ValidationFailed) as exc:
            raise InvariantViolation(
                "the amendment applies to this run's plan but not to the workflow's current "
                f"definition (version {workflow.version}), which a sibling run has already "
                f"amended: {exc}",
                details={"workflow_version": workflow.version},
            ) from exc

        workflow.version += 1
        retired |= {op.target_step_id for op in amendment.operations if op.op == "remove_step"}

        patch.apply_state_effects(run, before, effective, amendment.operations)
        run.applied_amendment_ids.append(amendment.amendment_id)
        recompute(run, effective, paused=False)

        amendment.status = "approved"
        amendment.decided_at = now()
        amendment.decided_by = decision.decided_by
        amendment.decision_reason = decision.reason
        amendment.resulting_workflow_version = workflow.version

        with self.store.transaction() as conn:
            self.store.save_workflow(
                conn, workflow, retired=retired, new_version_from=amendment.amendment_id
            )
            self.store.save_run(conn, run, effective)
            self.store.save_amendment(conn, amendment)
            self.store.audit(
                conn,
                "amendment.approved",
                workflow_id=amendment.workflow_id,
                run_id=amendment.run_id,
                amendment_id=amendment.amendment_id,
                detail={
                    "decided_by": amendment.decided_by,
                    "reason": decision.reason,
                    "resulting_workflow_version": workflow.version,
                    "run_status": run.status,
                },
            )
        return amendment

    def reject_amendment(self, amendment_id: str, decision: AmendmentDecision) -> Amendment:
        return self._close_amendment(
            amendment_id,
            status="rejected",
            decided_by=decision.decided_by,
            reason=decision.reason,
            event="amendment.rejected",
        )

    def withdraw_amendment(self, amendment_id: str, reason: str | None = None) -> Amendment:
        return self._close_amendment(
            amendment_id,
            status="withdrawn",
            decided_by=None,
            reason=reason,
            event="amendment.withdrawn",
        )

    def _close_amendment(
        self,
        amendment_id: str,
        *,
        status: str,
        decided_by: str | None,
        reason: str | None,
        event: str,
    ) -> Amendment:
        amendment = self.store.get_amendment(amendment_id)
        self._require_pending(amendment)
        amendment.status = status  # type: ignore[assignment]
        amendment.decided_at = now()
        amendment.decided_by = decided_by
        amendment.decision_reason = reason

        run, effective = self.store.get_run(amendment.run_id)
        recompute(run, effective, paused=False)

        with self.store.transaction() as conn:
            self.store.save_amendment(conn, amendment)
            self.store.save_run(conn, run)
            self.store.audit(
                conn,
                event,
                workflow_id=amendment.workflow_id,
                run_id=amendment.run_id,
                amendment_id=amendment_id,
                detail={"decided_by": decided_by, "reason": reason, "run_status": run.status},
            )
        return amendment

    @staticmethod
    def _require_pending(amendment: Amendment) -> None:
        if amendment.status != "pending_approval":
            raise InvalidTransition(
                f"amendment '{amendment.amendment_id}' is '{amendment.status}'; only a "
                "pending_approval amendment can be decided"
            )

    # --- config -------------------------------------------------------------------------

    def get_approval_policy(self) -> ApprovalPolicy:
        return self.store.get_approval_policy()

    def put_approval_policy(self, policy: ApprovalPolicy) -> ApprovalPolicy:
        policy_eval.validate_policy(policy)
        with self.store.transaction() as conn:
            self.store.put_approval_policy(conn, policy)
            self.store.audit(
                conn,
                "config.approval_policy_updated",
                detail={"rules": [r.model_dump(mode="json") for r in policy.rules]},
            )
        return policy

    # --- audit --------------------------------------------------------------------------

    def audit_entries(self, **filters: str | None) -> list[dict]:
        return self.store.audit_entries(**filters)  # type: ignore[arg-type]
