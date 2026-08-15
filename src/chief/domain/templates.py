"""Rendering a template into a plan.

Substitution is `{{ name }}`, applied to the text a person reads and the inputs a harness
receives — goals, harness names, string values inside ``inputs``, and the workflow title.
Never to ``id``, ``depends_on`` or ``body``: those are the graph, ids are permanent
(REQ-35), and a parameter that could rewrite them could produce a plan that fails
validation for reasons the template's author never saw.

Both directions are checked, and both at write time rather than at use time:

* every placeholder in the steps must be a declared parameter, or the template would render
  a goal with a literal ``{{ typo }}`` in it and nobody would notice until it was read;
* every parameter supplied at instantiation must be declared, or a caller's typo silently
  does nothing.
"""

from __future__ import annotations

import re
from typing import Any

from ..errors import NotFound, ValidationFailed
from ..models import TemplateParameter, WorkflowStep, WorkflowTemplate

PLACEHOLDER = re.compile(r"\{\{\s*([a-z][a-z0-9_]*)\s*\}\}")

# The fields substitution reaches (exit_when is optional, so a None is passed through
# untouched everywhere these are used). Everything else in a step is structure.
TEXT_FIELDS = ("goal", "harness", "exit_when")


def placeholders_in_text(text: str) -> set[str]:
    return set(PLACEHOLDER.findall(text))


def _walk_inputs(value: Any, found: set[str]) -> None:
    if isinstance(value, str):
        found |= placeholders_in_text(value)
    elif isinstance(value, dict):
        for item in value.values():
            _walk_inputs(item, found)
    elif isinstance(value, list):
        for item in value:
            _walk_inputs(item, found)


def placeholders_in(steps: list[WorkflowStep], *extra_text: str | None) -> set[str]:
    found: set[str] = set()
    for text in extra_text:
        if text:
            found |= placeholders_in_text(text)
    for step in steps:
        for field in TEXT_FIELDS:
            text = getattr(step, field)
            if text:
                found |= placeholders_in_text(text)
        _walk_inputs(step.inputs, found)
    return found


def validate_template(
    steps: list[WorkflowStep], parameters: list[TemplateParameter], *extra_text: str | None
) -> None:
    """Reject a template whose plan names something it does not declare."""
    declared = {p.name for p in parameters}
    used = placeholders_in(steps, *extra_text)
    undeclared = used - declared
    if undeclared:
        raise ValidationFailed(
            "the plan uses parameters the template does not declare: "
            f"{', '.join(sorted(undeclared))}",
            details={"undeclared": sorted(undeclared), "declared": sorted(declared)},
        )


def resolve_values(template: WorkflowTemplate, supplied: dict[str, str]) -> dict[str, str]:
    """Supplied values plus defaults, with both kinds of mistake refused.

    An unknown name is an error rather than an ignored extra: silently dropping it would
    render the plan with the default the caller meant to override.
    """
    declared = template.parameter_map
    unknown = set(supplied) - set(declared)
    if unknown:
        raise ValidationFailed(
            f"unknown parameters for template '{template.template_id}': "
            f"{', '.join(sorted(unknown))}",
            details={"unknown": sorted(unknown), "declared": sorted(declared)},
        )

    values: dict[str, str] = {}
    missing: list[str] = []
    for name, parameter in declared.items():
        if name in supplied:
            values[name] = supplied[name]
        elif parameter.default is not None:
            values[name] = parameter.default
        elif parameter.required:
            missing.append(name)
        else:
            values[name] = ""
    if missing:
        raise ValidationFailed(
            f"missing required parameters: {', '.join(sorted(missing))}",
            details={"missing": sorted(missing)},
        )
    return values


def _render_text(text: str, values: dict[str, str]) -> str:
    return PLACEHOLDER.sub(lambda m: values[m.group(1)], text)


def _render_inputs(value: Any, values: dict[str, str]) -> Any:
    if isinstance(value, str):
        return _render_text(value, values)
    if isinstance(value, dict):
        return {k: _render_inputs(v, values) for k, v in value.items()}
    if isinstance(value, list):
        return [_render_inputs(v, values) for v in value]
    return value


def render_steps(steps: list[WorkflowStep], values: dict[str, str]) -> list[WorkflowStep]:
    rendered = []
    for step in steps:
        patch: dict[str, Any] = {
            f: _render_text(v, values) if (v := getattr(step, f)) else v for f in TEXT_FIELDS
        }
        patch["inputs"] = _render_inputs(step.inputs, values)
        rendered.append(step.model_copy(update=patch))
    return rendered


def render_title(title: str, values: dict[str, str]) -> str:
    return _render_text(title, values)


def parameterise(
    steps: list[WorkflowStep], substitutions: dict[str, str]
) -> list[WorkflowStep]:
    """The inverse, used when extracting a template from a workflow that already ran.

    Replaces each literal with a placeholder for the parameter it maps to. Longest literal
    first, so that substituting "main" does not chew a hole in "maintenance".

    It is textual and unreviewed, and that is a real limit: a short literal over-matches.
    Mapping "api" to a parameter also rewrites "validate the api schema", because nothing
    here knows which occurrences the author meant. Extraction is a starting point for a
    template, not a finished one — prefer distinctive literals ("acme/api", not "api"), and
    read the result before relying on it.
    """
    if not substitutions:
        return list(steps)
    ordered = sorted(substitutions.items(), key=lambda kv: len(kv[0]), reverse=True)

    def swap(text: str) -> str:
        for literal, name in ordered:
            text = text.replace(literal, f"{{{{ {name} }}}}")
        return text

    def swap_inputs(value: Any) -> Any:
        if isinstance(value, str):
            return swap(value)
        if isinstance(value, dict):
            return {k: swap_inputs(v) for k, v in value.items()}
        if isinstance(value, list):
            return [swap_inputs(v) for v in value]
        return value

    out = []
    for step in steps:
        patch: dict[str, Any] = {
            f: swap(v) if (v := getattr(step, f)) else v for f in TEXT_FIELDS
        }
        patch["inputs"] = swap_inputs(step.inputs)
        out.append(step.model_copy(update=patch))
    return out


def declared_for(
    parameters: list[TemplateParameter], substitutions: dict[str, str]
) -> list[TemplateParameter]:
    """Parameters named by substitutions are declared automatically if not spelled out.

    The literal they replaced becomes the default, so a template extracted from a working
    plan renders back to that plan when instantiated with no arguments.
    """
    declared = {p.name: p for p in parameters}
    for literal, name in substitutions.items():
        if name not in declared:
            declared[name] = TemplateParameter(name=name, default=literal)
    return list(declared.values())


def require_active(template: WorkflowTemplate) -> None:
    if template.status != "active":
        raise NotFound(f"template '{template.template_id}' is archived")
