"""Amendment and PatchOperation (contract 1.7, 1.8)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .definition import WorkflowStep

AmendmentKind = Literal["forward", "history_edit"]
AmendmentStatus = Literal["pending_approval", "approved", "rejected", "withdrawn"]
PatchOp = Literal["insert_after", "insert_before", "update_step", "remove_step", "replay_step"]

#: Operations that alter or re-run an existing step. Only these force ``history_edit``
#: when their target is already completed/failed. ``insert_*`` does not, because inserting
#: a neighbour neither alters nor re-executes the target (REQ-14). See CONTRACT-NOTES.md #3.
MUTATING_OPS: frozenset[str] = frozenset({"update_step", "remove_step", "replay_step"})

#: Operations that require a ``step`` payload.
STEP_REQUIRED_OPS: frozenset[str] = frozenset({"insert_after", "insert_before", "update_step"})


class PatchOperation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    op: PatchOp
    target_step_id: str = Field(min_length=1)
    #: Scopes the operation to one loop/parallel instance. For nested constructs this is
    #: an instance path, outermost first (see domain/paths.py).
    instance_id: str | None = None
    instance_path: list[str] | None = None
    step: WorkflowStep | None = None

    @model_validator(mode="after")
    def _check_shape(self) -> PatchOperation:
        if self.op in STEP_REQUIRED_OPS and self.step is None:
            raise ValueError(f"op '{self.op}' requires a 'step' payload")
        if self.op not in STEP_REQUIRED_OPS and self.step is not None:
            raise ValueError(f"op '{self.op}' must not carry a 'step' payload")
        if self.instance_id is not None and self.instance_path is not None:
            raise ValueError("supply either 'instance_id' or 'instance_path', not both")
        return self

    def resolved_instance_path(self) -> list[str]:
        if self.instance_path is not None:
            return list(self.instance_path)
        if self.instance_id is not None:
            return [self.instance_id]
        return []


class Amendment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    amendment_id: str
    run_id: str
    workflow_id: str
    proposed_by: str = Field(min_length=1)
    kind: AmendmentKind
    reason: str = Field(min_length=1)
    operations: list[PatchOperation] = Field(min_length=1)
    status: AmendmentStatus = "pending_approval"
    created_at: str
    decided_at: str | None = None
    decided_by: str | None = None
    decision_reason: str | None = None
    #: WorkflowDefinition.version produced by approving this amendment; null until approved.
    resulting_workflow_version: int | None = None


class AmendmentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    proposed_by: str = Field(min_length=1)
    kind: AmendmentKind
    reason: str = Field(min_length=1)
    operations: list[PatchOperation] = Field(min_length=1)


class AmendmentDecision(BaseModel):
    """Body for approve/reject. ``decided_by`` defaults to ``human`` (REQ-43)."""

    model_config = ConfigDict(extra="forbid")

    decided_by: str = "human"
    reason: str | None = None
