"""Request bodies for the execution-reporting endpoints (contract 2.2)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .runstate import ArtifactRef, InstanceKind

#: ``skipped`` is deliberately absent: it is server-produced only (contract 1.4, 4).
ReportableStatus = Literal["pending", "running", "completed", "failed"]


class _UpdateBase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Required on every update, non-empty (REQ-48).
    summary: str = Field(min_length=1)
    artifacts: list[ArtifactRef] = Field(default_factory=list)
    # Described rather than commented: this reaches a harness through the MCP tool's JSON
    # schema, and an undescribed dict is a dict nobody fills in.
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Anything worth recording about this step that is not prose: token counts, "
            "cost, timings, model or commit ids, a seed. Shown in the web UI. Merged across "
            "updates rather than replaced, so a later one can add a key or correct a key "
            "without losing the others. On a loop or parallel instance, this is what tells "
            "one branch from another — put what distinguishes it here."
        ),
    )

    @field_validator("summary")
    @classmethod
    def _summary_not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("summary must not be blank (REQ-48)")
        return v


class StepUpdate(_UpdateBase):
    """Payload for a step update.

    ``status`` is rejected for loop/parallel steps and ``instances_closed`` is rejected for
    task steps; both checks need the definition, so they live in the service layer.
    """

    status: ReportableStatus | None = None
    instances_closed: bool | None = None
    # Keyed by criterion id (`c1`, `c2`, …) rather than by its text: an amendment may reword
    # a criterion, and matching on prose would silently stop matching. Required in full when
    # reporting `completed` on a step that declares criteria — see the gate in the service.
    criteria_met: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "How each of this step's criteria was met, keyed by criterion id (`c1`, `c2`, …). "
            "Required to report `completed` on a step that declares any: every id, each with "
            "a sentence of evidence — 'all 314 tests pass, see the log artifact' — not 'yes' "
            "or 'done'. If one cannot be met, keep working, report `failed`, or propose an "
            "amendment changing the criteria; do not report completion around it."
        ),
    )


class InstanceUpdate(_UpdateBase):
    """Payload for a loop-iteration / parallel-branch update.

    ``status`` is only accepted when the parent's body is a single step; for a multi-step
    body the instance status is derived from ``step_states`` (contract 1.5).
    """

    status: ReportableStatus | None = None


class BodyStepUpdate(StepUpdate):
    """Payload for a step *inside* one instance's body. Same shape as a step update, because
    a body step may itself be a loop/parallel construct."""


class CheckpointResolution(BaseModel):
    """Body for resolving a blocked checkpoint: what the person decided, and what they typed.

    No ``summary``, unlike every other update here: the summary is Chief's to write, because
    a checkpoint's result *is* the decision and letting the caller narrate it separately
    invites a summary that disagrees with the outcome recorded beside it.

    ``response`` is validated against the step's declared ``fields`` — an unknown key or a
    missing required one is a rejection, not a silently kept blob.
    """

    model_config = ConfigDict(extra="forbid")

    decision: Literal["approved", "rejected"]
    response: dict[str, str] = Field(default_factory=dict)
    note: str | None = None
    decided_by: str = "human"


class CommentCreate(BaseModel):
    """Body for commenting on an artifact.

    ``comment_id`` and ``created_at`` are not accepted — they are the record's, not the
    caller's, in the same way an artifact's id is stamped rather than supplied.
    """

    model_config = ConfigDict(extra="forbid")

    body: str = Field(min_length=1)
    author: str = "human"


class InstanceCreate(BaseModel):
    """Payload for registering a new instance. Instances are created on demand at runtime
    rather than predeclared (REQ-19)."""

    model_config = ConfigDict(extra="forbid")

    instance_id: str | None = None
    kind: InstanceKind | None = None
    index: int | None = None
    summary: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
