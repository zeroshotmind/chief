"""Checking a plan's logic before it becomes a workflow.

A plan is written in Lean against the `ChiefPlan` prelude in the repository's ``lean/``
directory, where each step is a function demanding artifacts that satisfy some condition and
promising one that satisfies another. Lean checks that every promise entails the demand it
feeds — for all values, not for a sample — and refuses to build a condition that excludes
nothing. What survives that is a graph whose edges are known to line up, which is then
compiled into an ordinary :class:`~chief.models.WorkflowDefinition` and run the way any other
plan is run.

Nothing in here is on the server's hot path, and nothing downstream of ``compile_plan``
depends on Lean having been involved: a compiled plan is a plan.
"""

from __future__ import annotations

from .compile import compile_plan
from .verify import (
    LeanUnavailable,
    PlanGraph,
    VerifyResult,
    available,
    package_dir,
    toolchain_version,
    verify_source,
)

__all__ = [
    "LeanUnavailable",
    "PlanGraph",
    "VerifyResult",
    "available",
    "compile_plan",
    "package_dir",
    "toolchain_version",
    "verify_source",
]
