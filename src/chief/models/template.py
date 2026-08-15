"""WorkflowTemplate — a reusable plan (extension; not in the contract).

A workflow is single-use: it is approved once, it executes once, and its identity is that
execution. Reuse lives here instead. A template is the plan you keep; a workflow is the
plan you are running this time.

Parameters are substituted into the *text* of a plan, never its structure. Step ids,
``depends_on`` and ``body`` are left alone deliberately: ids are permanent (REQ-35) and the
graph is validated by shape, so letting a parameter rewrite either would mean a template
could produce a plan that does not validate — a failure the author could not see coming
when writing the template.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .definition import WorkflowStep

TemplateStatus = Literal["active", "archived"]

# Lowercase and underscored so a placeholder reads as one token in a sentence of prose.
PARAMETER_NAME = r"^[a-z][a-z0-9_]*$"


class TemplateParameter(BaseModel):
    """One value the template needs before it can become a workflow."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=PARAMETER_NAME)
    description: str | None = None
    # A parameter with a default is optional by construction; `required` is about whether
    # the caller must supply one when there is nothing to fall back on.
    required: bool = True
    default: str | None = None

    @property
    def is_optional(self) -> bool:
        return self.default is not None or not self.required


class WorkflowTemplate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    template_id: str
    title: str = Field(min_length=1)
    description: str | None = None
    parameters: list[TemplateParameter] = Field(default_factory=list)
    steps: list[WorkflowStep]
    status: TemplateStatus = "active"
    version: int = 1
    # Set when the template was extracted from a workflow rather than submitted directly.
    derived_from_workflow_id: str | None = None
    created_at: str
    updated_at: str

    @field_validator("parameters")
    @classmethod
    def _unique_names(cls, values: list[TemplateParameter]) -> list[TemplateParameter]:
        names = [p.name for p in values]
        duplicates = {n for n in names if names.count(n) > 1}
        if duplicates:
            raise ValueError(f"duplicate parameter names: {', '.join(sorted(duplicates))}")
        return values

    @property
    def parameter_map(self) -> dict[str, TemplateParameter]:
        return {p.name: p for p in self.parameters}


class TemplateCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    template_id: str | None = None
    title: str = Field(min_length=1)
    description: str | None = None
    parameters: list[TemplateParameter] = Field(default_factory=list)
    steps: list[WorkflowStep]


class TemplateFromWorkflow(BaseModel):
    """Extract a template from a workflow that already exists.

    ``substitutions`` maps a literal string in the plan to a parameter name, so the values
    that made this plan specific become the knobs of the general one. The parameters they
    name are declared automatically if they are not declared explicitly.
    """

    model_config = ConfigDict(extra="forbid")

    template_id: str | None = None
    title: str | None = None
    description: str | None = None
    parameters: list[TemplateParameter] = Field(default_factory=list)
    substitutions: dict[str, str] = Field(default_factory=dict)

    @field_validator("substitutions")
    @classmethod
    def _named_and_non_empty(cls, values: dict[str, str]) -> dict[str, str]:
        import re

        for literal, name in values.items():
            if not literal:
                raise ValueError("a substitution's literal must not be empty")
            if not re.match(PARAMETER_NAME, name):
                raise ValueError(f"'{name}' is not a valid parameter name")
        return values


class TemplateInstantiate(BaseModel):
    """Turn a template into a draft workflow."""

    model_config = ConfigDict(extra="forbid")

    workflow_id: str | None = None
    title: str | None = None
    parameters: dict[str, str] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
