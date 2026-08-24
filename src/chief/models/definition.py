"""WorkflowDefinition and WorkflowStep — the static plan (contract 1.1, 1.2).

The definition is deliberately free of any run state (REQ-38): a harness reviewing or
amending a plan reads only this document.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .review import ReviewNote

StepType = Literal["task", "loop", "parallel", "checkpoint", "workflow_ref"]
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


class Criterion(BaseModel):
    """One condition that has to hold before a step may be called done.

    Criteria exist because goals were absorbing them. A real one from this store reads
    "... unit-tested against hand-written correct, incorrect and malformed completions",
    buried three sentences into a 467-character goal — an acceptance condition written as
    prose, where nothing can enumerate it and a reader has to hunt for it. Split out, the
    goal says what done looks like and this says how you would know.

    ``id`` is derived from position rather than supplied: criteria are authored as a plain
    list of strings (the validator on ``WorkflowStep`` accepts that and fills these in), and
    an id nobody types is an id nobody gets wrong. Positional is sound because a step's
    criteria are only ever replaced wholesale, by an ``update_step`` amendment — there is no
    operation that inserts one in the middle of a list and leaves the rest addressed as they
    were.
    """

    model_config = ConfigDict(extra="forbid")

    # Defaulted rather than required, so the JSON schema a harness reads agrees with what
    # the validator accepts. Required here, it would advertise an id the caller is told
    # elsewhere not to supply — and a schema that contradicts its own guidance is the way
    # this field gets filled in wrongly.
    id: str = Field(
        default="",
        description="Filled in automatically as c1, c2, … — do not supply one.",
    )
    text: str = Field(
        min_length=1,
        description="The condition, as one short checkable statement.",
    )

    @field_validator("text")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("a criterion must not be blank")
        return v


class InstanceParam(BaseModel):
    """One value each iteration or branch of a construct must supply about itself.

    A `parallel` step's branch count is decided at runtime, so what tells one branch from
    another can only arrive at runtime too — in the instance's ``metadata``. That worked, but
    nothing required it: ``wf_ablate`` in the shipped demo has three branches training three
    different variants, all registered with ``metadata={}``, and no way to tell which was
    which. Declaring the names here makes that plan impossible to write.

    Metadata stays open around them. These are a required subset, not a schema: a harness
    still attaches token counts and timings beside them, and refusing those would make the
    declaration cost more than it gives. See CONTRACT-NOTES.md #40.

    ``name`` is as permanent as a step id — it is the key the value arrives under, the key
    the harness reads back, and the placeholder body steps substitute.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    #: What it is, for whoever reads the plan. Shown in the UI beside the construct.
    description: str | None = None
    #: A required param must be present on every instance; an optional one may be omitted,
    #: and substitutes as empty where a body step names it.
    required: bool = True

    @field_validator("name")
    @classmethod
    def _name_not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("an instance parameter needs a name")
        return v


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
    # Described rather than commented: this reaches a harness through the MCP tool's JSON
    # schema, and length guidance that only lives in a skill file reaches one harness.
    # Deliberately not a `max_length`: a rejected `create_workflow` is a worse failure than
    # a long goal, and this store already holds a 902-character one that must stay
    # revisable. See CONTRACT-NOTES.md #39.
    goal: str = Field(
        min_length=1,
        description=(
            "What done looks like, in two or three lines at most. State the work, not how to "
            "do it, and leave out anything a reader of the plan does not need. Conditions "
            "that decide whether it is finished belong in `criteria`, not here — a goal that "
            "runs long is almost always one with acceptance conditions buried in its prose."
        ),
    )
    harness: str = Field(min_length=1)
    # Task-only, and the reason `goal` can be short. See CONTRACT-NOTES.md #39.
    criteria: list[Criterion] = Field(
        default_factory=list,
        description=(
            "The conditions that decide whether this step is done, one crisp checkable "
            "statement each — 'exit code 0 on the full suite', 'every rejected candidate has "
            "a stated reason'. Write them as plain strings; ids are filled in. Reporting this "
            "step `completed` requires saying how each one was met, so state conditions you "
            "will be able to answer for, and keep vague ones out. Task steps only. Optional: "
            "a step with none behaves as it always has."
        ),
    )
    depends_on: list[str] = Field(default_factory=list)
    # Which part of the work this step belongs to. Purely a label: nothing derives from it,
    # no rule mentions it, and two steps sharing one are not related by it in any way the
    # server knows about. It exists because a plan past about eight steps stops reading as a
    # shape, and the phase a step belongs to is the one thing a reader cannot recover from
    # the graph — "training the encoder" is not a property of any edge. An open namespace
    # like `harness` and `project`: adding a group is writing one, not changing a schema.
    group: str | None = Field(
        default=None,
        description=(
            "Which part of the work this step belongs to, e.g. 'Collection' or 'Evaluation'. "
            "Steps sharing a group are drawn together under that label. Nests on '/', so "
            "'Encoder/Training' sits inside 'Encoder'. Optional, and worth setting only on a "
            "plan big enough that its shape is hard to read; use the same wording across the "
            "steps of one group, since the label is matched literally."
        ),
    )
    inputs: dict[str, Any] = Field(default_factory=dict)
    # What the step promises about what it produces, keyed the way `inputs` is keyed. Open and
    # unvalidated for the same reason `inputs` is: Chief holds it and shows it, and a schema
    # over it would have to be kept in step with every harness that writes one.
    #
    # A step is a function, and a plan that states only what a step demands has written half a
    # signature. The half it leaves out is the one every later step depends on. Distinct from
    # the artifacts a run reports, which are what was *actually* produced: this is the claim
    # made before anything ran.
    outputs: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "What this step promises about what it produces, e.g. "
            "{'model': {'artifact_type': 'Model', 'contract': 'auc >= 80'}}. The counterpart "
            "of `inputs`. Optional; a step that says nothing about its output behaves as it "
            "always has."
        ),
    )
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
    # What each instance of this construct must say about itself. Loop/parallel only. The
    # names are also placeholders: `{{ paper }}` in a body step's goal or criteria is
    # substituted with that branch's value when the step is read. See CONTRACT-NOTES.md #40.
    instance_params: list[InstanceParam] | None = Field(
        default=None,
        description=(
            "What tells one iteration or branch of this construct from another — the names "
            "each one must supply when it is registered, e.g. `paper` and `pdf_path` for a "
            "step that fans out over papers. Every instance is then required to give a value "
            "for each, so a run cannot end up with eight branches nobody can tell apart. "
            "Body steps may write `{{ paper }}` in a goal or a criterion and it is filled in "
            "per branch. Loop and parallel steps only."
        ),
    )
    # What a checkpoint asks a person for, beyond the approve/reject decision itself. Empty
    # (or absent) is a pure gate: someone has to say go. Checkpoint-only — a task's inputs
    # come from the plan, not from a person at runtime. See CONTRACT-NOTES.md #28.
    fields: list[CheckpointField] | None = None
    # The template a workflow_ref step instantiates and runs as a child. workflow_ref-only,
    # required and non-blank there (validated in domain/graph.py alongside the other
    # per-type shape rules) — a sub-workflow step with nothing to instantiate cannot ever
    # move past 'running'.
    ref_template_id: str | None = None
    # Values to substitute when the template is instantiated, in the same shape a harness
    # passes to create_workflow_from_template. workflow_ref-only.
    ref_parameters: dict[str, str] = Field(default_factory=dict)

    @field_validator("criteria", mode="before")
    @classmethod
    def _number_criteria(cls, v: Any) -> Any:
        """Accept ``["a", "b"]`` as well as the stamped form, and number both."""
        if not isinstance(v, list):
            return v
        out = []
        for i, item in enumerate(v, start=1):
            if isinstance(item, str):
                out.append({"id": f"c{i}", "text": item})
            elif isinstance(item, dict):
                out.append({**item, "id": item.get("id") or f"c{i}"})
            else:
                out.append(item)
        return out

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
    def is_workflow_ref(self) -> bool:
        return self.type == "workflow_ref"

    @property
    def instance_param_specs(self) -> list[InstanceParam]:
        return list(self.instance_params or [])

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
    # Set only when this workflow was compiled from a checked plan (extension).
    from_graph: GraphOrigin | None = None
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


class GraphOrigin(BaseModel):
    """Where a workflow came from, when it was compiled from a checked plan.

    Lineage rather than a live link, for the same reason as ``TemplateOrigin``: the plan may be
    revised or re-checked afterwards, and this has to keep saying what *this* workflow was made
    from. ``toolchain`` is part of that record — "verified" names the thing that did the
    verifying, and a verdict without it cannot be re-examined later.

    Nothing on the server reads this to decide anything. A compiled workflow is approved, run
    and amended by exactly the rules that govern a hand-written one; the version counter
    already says whether it has been amended since, so a plan's badge needs no help to stop
    meaning "and it still matches".
    """

    model_config = ConfigDict(extra="forbid")

    graph_id: str
    #: What the plan's own statistics said at the moment it was compiled — how many contracts
    #: it carried and how many of them constrained anything. Copied rather than looked up so
    #: that a workflow can be read without the plan, and so a later revision of the plan cannot
    #: quietly restate what this workflow was built on.
    contracts_refined: int = 0
    contracts_any: int = 0
    toolchain: str | None = None
    verified_at: str | None = None


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
    # Described rather than commented: these two reach a harness through the MCP tool's
    # JSON schema, and a `#:` comment does not travel there. A field an agent sees as a
    # bare nullable string is a field it leaves null.
    project: str | None = Field(
        default=None,
        description=(
            "Short label for the body of work this belongs to, e.g. 'chief' or 'songs'. "
            "Match a label already in use rather than inventing a variant. Not a directory: "
            "one project spans several checkouts. Omit if it belongs to nothing in "
            "particular."
        ),
    )
    origin_dir: str | None = Field(
        default=None,
        description=(
            "Absolute path of the directory you are working in. Record it whenever you "
            "know it: it is how a person later tells which checkout this was, and it is "
            "what lets the web UI open the files this run reports. Nothing resolves "
            "against it on the server, so give it as you see it."
        ),
    )


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

    ``null`` clears a field and omitting it leaves that field alone — the two are told apart
    by ``model_fields_set``, not by the value. Without that distinction a request setting the
    project would silently erase the directory beside it, which is the sort of data loss
    nobody notices until they look for something that used to be there.

    ``origin_dir`` is here as well as on creation, and that is not a contradiction of
    ``WorkflowRevise`` refusing it. A revision is a harness rewriting the plan, and one made
    somewhere else overwriting where the work happened would turn a record into a lie. This
    is a person correcting the record by hand, which is the only way a workflow planned
    before Chief asked for a directory can ever have one — and without it those workflows can
    never show their files.
    """

    model_config = ConfigDict(extra="forbid")

    #: A new title, allowed at any status for the same reason the labels are: renaming says
    #: nothing about the plan, and the workflows most in need of a better name are often the
    #: ones already running or finished. Unlike the labels it cannot be cleared — a workflow
    #: without a title is not a record of anything — so blank is refused rather than treated
    #: as "remove".
    title: str | None = None
    project: str | None = None
    origin_dir: str | None = None
