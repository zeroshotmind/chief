"""Checking a plan's logic before it becomes a workflow.

A proof graph is written in Lean against the `ProofGraph` prelude in the repository's ``lean/``
directory, where each step is a function demanding artifacts that satisfy some condition and
promising one that satisfies another. Lean checks that every promise entails the demand it
feeds — for all values, not for a sample — and refuses to build a condition that excludes
nothing. What survives that is a graph whose edges are known to line up, which is then
compiled into an ordinary :class:`~chief.models.WorkflowDefinition` and run the way any other
plan is run.

Nothing in here is on the server's hot path, and nothing downstream of ``compile_graph``
depends on Lean having been involved: a compiled plan is a plan.
"""

from __future__ import annotations

from .compile import compile_graph
from .verify import (
    LeanUnavailable,
    attribute_diagnostics,
    available,
    lint_source,
    package_dir,
    toolchain_version,
    verify_source,
)

__all__ = [
    "LeanUnavailable",
    "attribute_diagnostics",
    "available",
    "compile_graph",
    "lint_source",
    "package_dir",
    "toolchain_version",
    "verify_source",
]
