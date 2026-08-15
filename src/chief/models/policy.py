"""ApprovalPolicy (contract 1.9).

The contract leaves the ``match`` grammar as Open Item 1. This module pins it down:
a small three-valued boolean expression language over ``amendment.<field>``, evaluated
first-match-wins. See CONTRACT-NOTES.md #5 for why this shape was chosen.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ApprovalRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    match: str = Field(min_length=1)
    auto_approve: bool
    # Optional stable label so an auto-approval can record *which* rule decided it. The
    # contract's Amendment.decided_by is "human | policy_id | null" but nothing in the
    # ApprovalPolicy schema supplied a policy id; this is that id.
    id: str | None = None


class ApprovalPolicy(BaseModel):
    """Default is empty: everything requires a human (REQ-43)."""

    model_config = ConfigDict(extra="forbid")

    rules: list[ApprovalRule] = Field(default_factory=list)
