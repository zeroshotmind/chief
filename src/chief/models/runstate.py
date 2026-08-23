"""RunState, StepState, StepInstance and ArtifactRef — the execution record (contract 1.3-1.6).

Kept as a document separate from the definition and joined by step id (REQ-38).

StepState and StepInstance are mutually recursive: a StepInstance carries a map of
StepState-shaped entries for the steps in its parent's body, and any of those body steps
may itself be a loop/parallel construct with instances of its own (contract 1.2, nesting).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .definition import WorkflowStep

StepStatus = Literal["pending", "running", "completed", "failed", "skipped", "blocked"]
RunStatus = Literal["running", "paused_for_approval", "waiting_on_human", "completed", "failed"]
InstanceKind = Literal["iteration", "branch"]

TERMINAL_OK: frozenset[str] = frozenset({"completed", "skipped"})
HISTORY_LOCKED: frozenset[str] = frozenset({"completed", "failed"})


class ArtifactComment(BaseModel):
    """Something a person wanted said about an artifact, for whoever picks the work up.

    A harness reports what it produced; this is the other direction — "this draft is the
    one, match its tone", "the numbers in here are stale". It rides on the run state the
    harness already fetches, so reading it costs no new call and no new tool.

    Append-only, like the artifact list it hangs off. There is no edit and no delete: a
    comment is a thing someone said at a point in the run, and letting it be rewritten
    afterwards would make the record of what the harness was told disagree with what it
    acted on.
    """

    model_config = ConfigDict(extra="forbid")

    comment_id: str
    body: str = Field(min_length=1)
    author: str = Field(min_length=1)
    created_at: str
    #: Which transport it arrived on, as with a decision — see REQ-43.
    via: str | None = None


class ArtifactRef(BaseModel):
    """JSON metadata only; no blob storage (REQ-46)."""

    model_config = ConfigDict(extra="forbid")

    artifact_id: str | None = None
    type: str = Field(min_length=1)
    description: str | None = Field(
        default=None,
        description="One line saying what this is, for a person scanning a list of outputs.",
    )
    ref: str | None = Field(
        default=None,
        description=(
            "Where it is: a path, or a URL. A path relative to the workflow's origin_dir is "
            "resolved and the file can be opened in the web UI, so give the path you "
            "actually wrote to rather than an approximation of it. A URL is framed rather "
            "than read."
        ),
    )
    data: Any = Field(
        default=None,
        description=(
            "Metadata about the artifact — dimensions, a row count, a digest, how it was "
            "produced — shown beside it in the web UI. `data.text` is special: it holds the "
            "artifact's own content for something with no file, and is rendered as the "
            "preview rather than as metadata. One of `ref` or `data` is required."
        ),
    )
    # Server-owned, in the same sense as a derived status: this shape is also what a harness
    # submits in a StepUpdate, and a harness reporting its own comments would be reporting
    # what it was told rather than what it did. `_stamp_artifacts` refuses them on the way
    # in. See CONTRACT-NOTES.md #30.
    comments: list[ArtifactComment] = Field(default_factory=list)

    @model_validator(mode="after")
    def _ref_or_data(self) -> ArtifactRef:
        if self.ref is None and self.data is None:
            raise ValueError("artifact must carry at least one of 'ref' or 'data'")
        return self


class CheckpointOutcome(BaseModel):
    """What a person decided at a checkpoint, and what they typed.

    Nested rather than flattened onto StepState for the same reason ``instances`` is: it is
    present only for one step type, and keeping it in one object means ``snapshot()`` carries
    the whole decision into history as a unit when a replay supersedes it.
    """

    model_config = ConfigDict(extra="forbid")

    decision: Literal["approved", "rejected"]
    #: The answers, keyed by CheckpointField.name. Empty for a pure gate.
    response: dict[str, str] = Field(default_factory=dict)
    #: Free text the person added alongside the decision — why they said no, a caveat.
    note: str | None = None
    decided_by: str = "human"
    decided_at: str
    #: Which transport the decision arrived on, so a decision an agent relayed stays
    #: distinguishable from one made by hand in the UI (REQ-43).
    via: str | None = None


class StepInstance(BaseModel):
    """One loop iteration or parallel branch (REQ-10, REQ-11)."""

    model_config = ConfigDict(extra="forbid")

    instance_id: str
    parent_step_id: str
    kind: InstanceKind
    index: int
    status: StepStatus = "pending"
    summary: str | None = None
    artifacts: list[ArtifactRef] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    step_states: dict[str, StepState] = Field(default_factory=dict)
    started_at: str | None = None
    completed_at: str | None = None
    # The parent's body as it stood when this instance was registered. An instance executes
    # the body it was spawned with, so an amendment that edits the body cannot retroactively
    # change what an already-finished iteration was expected to do (REQ-14).
    body: list[str] = Field(default_factory=list)
    # Prior snapshots of this instance, appended when an approved history-edit replays it
    # (REQ-42). Mirrors StepState.history; the contract defines history only on StepState,
    # which leaves instance-scoped replays with nowhere to preserve the old result.
    history: list[dict[str, Any]] = Field(default_factory=list)


class StepState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    step_id: str
    status: StepStatus = "pending"
    summary: str | None = None
    artifacts: list[ArtifactRef] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    started_at: str | None = None
    completed_at: str | None = None
    # Present only for loop/parallel steps.
    instances: list[StepInstance] | None = None
    # Present only for a checkpoint step, once a person has decided it.
    checkpoint: CheckpointOutcome | None = None
    # Present only for a workflow_ref step, once its child run has been registered. The
    # child run's terminal status is cascaded onto this step automatically — there is no
    # human decision to record, unlike checkpoint.
    child_run_id: str | None = None
    # Set by the harness once no further instances will be registered, so the server can
    # tell "no instances yet" from "all instances done". See CONTRACT-NOTES.md #1.
    instances_closed: bool = False
    # What the harness said about each of the step's criteria when it reported completion,
    # keyed by criterion id. Empty on a step that declares none, which is most of them.
    criteria_met: dict[str, str] = Field(default_factory=dict)
    # Prior snapshots, appended when superseded by an approved history-edit (REQ-42).
    history: list[dict[str, Any]] = Field(default_factory=list)
    # Why the server set ``skipped``. A dependency skip is retracted if the blocking
    # failure is later replayed away; a removal skip is permanent. Without this the two
    # are indistinguishable and a replayed dependency leaves the chain wedged.
    skip_cause: Literal["dependency", "removed"] | None = None

    def instance(self, instance_id: str) -> StepInstance | None:
        for inst in self.instances or []:
            if inst.instance_id == instance_id:
                return inst
        return None

    def snapshot(self) -> dict[str, Any]:
        """A frozen copy of the live state, minus ``history`` itself (no nesting of history)."""
        data = self.model_dump(mode="json", exclude={"history"})
        return data


StepInstance.model_rebuild()
StepState.model_rebuild()


class RunState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    workflow_id: str
    base_version: int
    applied_amendment_ids: list[str] = Field(default_factory=list)
    status: RunStatus = "running"
    # Whatever the harness said when it registered this run — what triggered it, which
    # commit, which machine. `RunCreate` has always accepted it; until it was declared here
    # it was accepted and dropped, which is worse than refusing it.
    metadata: dict[str, Any] = Field(default_factory=dict)
    step_states: dict[str, StepState] = Field(default_factory=dict)
    created_at: str
    updated_at: str


class RunCreate(BaseModel):
    """Request body for ``POST /workflows/{workflow_id}/runs``."""

    model_config = ConfigDict(extra="forbid")

    run_id: str | None = None
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "What set this run going: a trigger, a commit, a host, a ticket. Recorded once "
            "and shown on the run overview; per-step facts belong on the step update."
        ),
    )


class RunPlan(BaseModel):
    """The plan a run is executing, with the lineage that identifies it (contract 1.3).

    Deliberately not a WorkflowDefinition: ``WorkflowDefinition.version`` names a document
    in the workflow's shared history, and a run's effective plan is base_version plus only
    that run's own approved amendments — no shared version number names it.
    """

    model_config = ConfigDict(extra="forbid")

    run_id: str
    workflow_id: str
    title: str
    base_version: int
    applied_amendment_ids: list[str]
    steps: list[WorkflowStep]
