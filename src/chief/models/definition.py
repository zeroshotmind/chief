"""WorkflowDefinition and WorkflowStep — the static plan (contract 1.1, 1.2).

The definition is deliberately free of any run state (REQ-38): a harness reviewing or
amending a plan reads only this document.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .review import ReviewNote

StepType = Literal["task", "loop", "parallel", "checkpoint"]
WorkflowStatus = Literal["draft", "approved", "archived"]
WorkflowSource = Literal["import", "generated"]
InstanceFailurePolicy = Literal["fail_fast", "continue"]


class CheckpointField(BaseModel):
    """One piece of text a checkpoint asks a person for.

    Everything is text: Chief holds what a human said, it does not compute on it, so a type
    system over the answers would buy nothing and would have to be kept in step with a UI
    widget set. ``name`` is the key the answer arrives under and the key the harness reads
    it back by, so it is as permanent as a step id.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    #: What the person sees above the box. Falls back to ``name`` in the UI.
    label: str | None = None
    #: Shown under the box — an example, a unit, the format you want back.
    hint: str | None = None
    required: bool = True


class WorkflowStep(BaseModel):
    """A single node of the plan.

    ``id`` is permanent (REQ-35). ``harness`` is an open string namespace rather than an
    enum so that adding a harness is adding a value, not a schema change (REQ-26). A
    ``checkpoint`` is the one type Chief knows the executor of: a person, so its harness is
    ``human``.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    type: StepType
    goal: str = Field(min_length=1)
    harness: str = Field(min_length=1)
    depends_on: list[str] = Field(default_factory=list)
    inputs: dict[str, Any] = Field(default_factory=dict)
    body: list[str] | None = None
    # Governs whether a failed instance (or a failed step inside an instance body) fails
    # the construct, or is tolerated so the remaining instances still run. Only meaningful
    # for loop/parallel steps, where it is normalised to "fail_fast" if omitted; must stay
    # unset on a task. See CONTRACT-NOTES.md #2.
    on_instance_failure: InstanceFailurePolicy | None = None
    # A loop's exit condition, stated as a condition rather than buried in a body step's
    # goal: "held-out accuracy beats baseline and the audit is clean". The branch out of a
    # loop is a decision — this names what decides it, so the plan's graph can label its two
    # arrows (met -> continue past the loop, not met -> another iteration). Loop-only.
    # Chief records it; the harness judges it — like everything else, it is not enforced.
    exit_when: str | None = None
    # What a checkpoint asks a person for, beyond the approve/reject decision itself. Empty
    # (or absent) is a pure gate: someone has to say go. Checkpoint-only — a task's inputs
    # come from the plan, not from a person at runtime. See CONTRACT-NOTES.md #28.
    fields: list[CheckpointField] | None = None

    @field_validator("goal", "harness")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("must not be blank")
        return v

    @property
    def is_construct(self) -> bool:
        return self.type in ("loop", "parallel")

    @property
    def is_checkpoint(self) -> bool:
        return self.type == "checkpoint"

    @property
    def field_specs(self) -> list[CheckpointField]:
        return list(self.fields or [])

    @property
    def failure_policy(self) -> InstanceFailurePolicy:
        return self.on_instance_failure or "fail_fast"

    @property
    def instance_kind(self) -> Literal["iteration", "branch"]:
        return "iteration" if self.type == "loop" else "branch"

    @property
    def body_ids(self) -> list[str]:
        return list(self.body or [])


class WorkflowDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workflow_id: str
    title: str = Field(min_length=1)
    source: WorkflowSource
    generated_by: str | None = None
    status: WorkflowStatus = "draft"
    version: int = 1
    steps: list[WorkflowStep] = Field(default_factory=list)
    # Which body of work this plan belongs to. An open string namespace like ``harness``
    # (REQ-26): adding a project is adding a value, not a schema change. Deliberately a
    # label rather than a path — a label is stable, and a path rots the moment the tree
    # moves, which is the whole of CONTRACT-NOTES.md #29. Absent on anything planned before
    # projects existed, and the UI must show those rather than filter them away.
    project: str | None = None
    # Where the harness was standing when this plan was made. Provenance, not a base for
    # resolving anything: nothing on the server reads it, and artifact refs still resolve
    # against the folder named in the browser (#29). Recorded because "which checkout was
    # this?" is a real question a month later, and because it makes a good *suggestion* for
    # that browser-side folder — a suggestion the reader can override, which a stale path
    # must always be. See CONTRACT-NOTES.md #32.
    origin_dir: str | None = None
    # Set only when this workflow was instantiated from a template (extension).
    from_template: TemplateOrigin | None = None
    # When the plan was first submitted and when it was last written. These are facts about
    # the record rather than parts of the plan, so they are kept in the store's own columns
    # and filled in on the way out — not carried in the stored document, where a stale copy
    # could disagree with the column, and not accepted from a harness (WorkflowCreate has no
    # such field). They are therefore None on any definition that did not come from the
    # store: a plan being validated, a template's steps, a run's effective definition.
    created_at: str | None = None
    updated_at: str | None = None
    # What a reviewer said about this plan. Server-owned in the same way as the timestamps
    # above: kept in its own table, attached on the way out of ``get_workflow``, and absent
    # from the stored document so a revision cannot overwrite the feedback that asked for
    # it. A harness therefore reads notes without a second call and cannot write one —
    # neither WorkflowCreate nor WorkflowRevise declares the field, and both forbid extras.
    # Empty on any definition that did not come through that read: a version snapshot, a
    # plan being validated, a run's effective definition.
    review_notes: list[ReviewNote] = Field(default_factory=list)

    def step(self, step_id: str) -> WorkflowStep | None:
        for s in self.steps:
            if s.id == step_id:
                return s
        return None

    def steps_by_id(self) -> dict[str, WorkflowStep]:
        return {s.id: s for s in self.steps}


class TemplateOrigin(BaseModel):
    """Where a workflow came from, when it came from a template.

    Kept as lineage on the workflow rather than a live link: the template may be edited or
    archived afterwards, and this has to keep saying what *this* workflow was made from.
    """

    model_config = ConfigDict(extra="forbid")

    template_id: str
    template_version: int
    parameters: dict[str, str] = Field(default_factory=dict)


class WorkflowCreate(BaseModel):
    """Request body for ``POST /workflows``.

    ``workflow_id`` may be supplied by the harness (useful when importing an existing
    plan, REQ-31) and is generated otherwise. ``status`` and ``version`` are not
    accepted: a workflow is always created as ``draft`` at version 1 (REQ-32).
    """

    model_config = ConfigDict(extra="forbid")

    workflow_id: str | None = None
    title: str = Field(min_length=1)
    source: WorkflowSource
    generated_by: str | None = None
    steps: list[WorkflowStep]
    #: Both are the harness's to state at creation: it knows what it is working on and
    #: where it is standing. Neither is accepted on a revision — see ``WorkflowRevise``.
    project: str | None = None
    origin_dir: str | None = None


class WorkflowRevise(BaseModel):
    """Request body for ``PUT /workflows/{workflow_id}``: replace a draft's plan.

    Only a draft may be revised, and revising one is not an amendment. An amendment is the
    protocol for changing a plan a human already approved and a harness is already running,
    which is why it needs its own approval (REQ-13) and why ``propose_amendment`` requires a
    run. A draft has neither: nobody has agreed to it and nothing has executed from it, so
    there is nothing to protect and no run to pause. The alternative — a new workflow each
    time the plan is corrected — leaves the reviewer to work out which of several drafts
    supersedes the others, which is worse than editing the one they are looking at.

    ``version`` stays put deliberately. It counts approved amendments, and making it also
    count pre-approval edits would mean a version number no longer answers "how many times
    has this plan changed under a running harness". Each revision overwrites the draft;
    the audit log records that it happened.

    ``source`` and ``generated_by`` are not accepted: revising a plan does not change where
    it came from. Neither are ``project`` and ``origin_dir``, for the same reason — the
    second especially, which is a record of where the harness stood and would be a lie if a
    later revision from somewhere else could overwrite it. A mislabelled project is
    corrected through ``PATCH /workflows/{id}``, which is a labelling act, not a plan one.
    """

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1)
    steps: list[WorkflowStep]
    reason: str | None = None


class WorkflowLabel(BaseModel):
    """Body for ``PATCH /workflows/{workflow_id}``: which project this belongs to.

    Its own request rather than part of a revision, because it says nothing about the plan
    and so is not refused once the workflow leaves ``draft``. Filing an approved — or long
    finished — workflow under a project is the main use: every workflow that existed before
    projects did has no label, and no amount of care at creation time can fix those.

    ``null`` clears it, which has to be possible: a label applied to the wrong workflow is
    otherwise permanent.
    """

    model_config = ConfigDict(extra="forbid")

    project: str | None = None
