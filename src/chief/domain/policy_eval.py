"""Approval-policy matching (contract Open Item 1).

The contract leaves ``ApprovalPolicy.rules[].match`` as a placeholder string. This is the
grammar it resolves to — small enough to be obviously safe (no eval, no attribute walking),
big enough to express the rules REQ-43 is actually about::

    amendment.kind == 'forward'
    amendment.kind == 'forward' && amendment.proposed_by in ['planner', 'claude_cli']
    amendment.kind == 'forward' && amendment.ops subset_of ['insert_after', 'insert_before']
    true

Rules are evaluated first-match-wins, which is the other half of Open Item 1.

Expressions are evaluated with three-valued (Kleene) logic. That is what makes the
"a history_edit can never be auto-approved" invariant checkable at *write* time: bind
``amendment.kind`` to ``history_edit``, leave every other field UNKNOWN, and demand the
expression evaluate to a definite FALSE. If it can't be proven false, the rule is rejected.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from ..errors import ValidationFailed
from ..models import Amendment, ApprovalPolicy

SCALAR_FIELDS = {"kind", "proposed_by", "run_id", "workflow_id"}
LIST_FIELDS = {"ops"}
UNKNOWN = None

# A second subject, for the policy that governs approving a *workflow* (REQ-32/REQ-43).
# Same grammar, same Kleene evaluation, different fields — and the same trick for proving a
# rule safe at write time.
WORKFLOW_SCALARS = {"template_id", "source", "generated_by", "title"}

SUBJECTS = {
    "amendment": (SCALAR_FIELDS, LIST_FIELDS),
    "workflow": (WORKFLOW_SCALARS, set()),
}

_TOKEN_RE = re.compile(
    r"""
    \s*(?:
        (?P<lparen>\()
      | (?P<rparen>\))
      | (?P<lbrack>\[)
      | (?P<rbrack>\])
      | (?P<comma>,)
      | (?P<and>&&)
      | (?P<or>\|\|)
      | (?P<eq>==)
      | (?P<ne>!=)
      | (?P<not>!)
      | (?P<string>'[^']*'|"[^"]*")
      | (?P<ident>[A-Za-z_][A-Za-z0-9_.]*)
    )
    """,
    re.VERBOSE,
)


@dataclass(frozen=True)
class Token:
    kind: str
    value: str


def tokenize(source: str) -> list[Token]:
    tokens: list[Token] = []
    pos = 0
    while pos < len(source):
        if source[pos].isspace():
            pos += 1
            continue
        match = _TOKEN_RE.match(source, pos)
        if not match or match.end() == match.start():
            raise ValidationFailed(
                f"cannot parse approval-policy expression at offset {pos}",
                details={"expression": source, "offset": pos},
            )
        kind = match.lastgroup or ""
        tokens.append(Token(kind, match.group(kind)))
        pos = match.end()
    return tokens


# --- AST -------------------------------------------------------------------------------


@dataclass(frozen=True)
class Literal:
    value: bool


@dataclass(frozen=True)
class Comparison:
    field: str
    op: str
    value: Any


@dataclass(frozen=True)
class Not:
    operand: Any


@dataclass(frozen=True)
class And:
    parts: tuple[Any, ...]


@dataclass(frozen=True)
class Or:
    parts: tuple[Any, ...]


class _Parser:
    def __init__(self, tokens: list[Token], source: str, subject: str = "amendment") -> None:
        self.tokens = tokens
        self.pos = 0
        self.source = source
        self.subject = subject
        self.scalars, self.lists = SUBJECTS[subject]

    def fail(self, message: str) -> ValidationFailed:
        return ValidationFailed(
            f"invalid approval-policy expression: {message}", details={"expression": self.source}
        )

    def peek(self) -> Token | None:
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def take(self, kind: str) -> Token:
        token = self.peek()
        if token is None or token.kind != kind:
            raise self.fail(f"expected {kind}, got {token.value if token else 'end of input'}")
        self.pos += 1
        return token

    def parse(self) -> Any:
        node = self.parse_or()
        if self.peek() is not None:
            raise self.fail(f"unexpected trailing input near '{self.tokens[self.pos].value}'")
        return node

    def parse_or(self) -> Any:
        parts = [self.parse_and()]
        while (token := self.peek()) and token.kind == "or":
            self.pos += 1
            parts.append(self.parse_and())
        return parts[0] if len(parts) == 1 else Or(tuple(parts))

    def parse_and(self) -> Any:
        parts = [self.parse_factor()]
        while (token := self.peek()) and token.kind == "and":
            self.pos += 1
            parts.append(self.parse_factor())
        return parts[0] if len(parts) == 1 else And(tuple(parts))

    def parse_factor(self) -> Any:
        token = self.peek()
        if token is None:
            raise self.fail("unexpected end of input")
        if token.kind == "not":
            self.pos += 1
            return Not(self.parse_factor())
        if token.kind == "lparen":
            self.pos += 1
            node = self.parse_or()
            self.take("rparen")
            return node
        if token.kind == "ident" and token.value in ("true", "false"):
            self.pos += 1
            return Literal(token.value == "true")
        return self.parse_comparison()

    def parse_comparison(self) -> Any:
        field_token = self.take("ident")
        field = field_token.value
        prefix = f"{self.subject}."
        if not field.startswith(prefix):
            raise self.fail(f"unknown reference '{field}'; only {prefix}<field> is available")
        name = field[len(prefix) :]
        if name not in self.scalars | self.lists:
            raise self.fail(
                f"unknown field '{name}'; available: "
                f"{', '.join(sorted(self.scalars | self.lists))}"
            )
        token = self.peek()
        if token is None:
            raise self.fail(f"expected an operator after '{field}'")
        if token.kind in ("eq", "ne"):
            self.pos += 1
            if name in self.lists:
                raise self.fail(f"'{name}' is a list; use 'subset_of'")
            return Comparison(name, token.kind, self.parse_string())
        if token.kind == "ident" and token.value in ("in", "subset_of"):
            self.pos += 1
            if token.value == "in" and name in self.lists:
                raise self.fail(f"'{name}' is a list; use 'subset_of'")
            if token.value == "subset_of" and name in self.scalars:
                raise self.fail(f"'{name}' is a scalar; use '==', '!=' or 'in'")
            return Comparison(name, token.value, self.parse_string_list())
        raise self.fail(f"unexpected operator '{token.value}'")

    def parse_string(self) -> str:
        return self.take("string").value[1:-1]

    def parse_string_list(self) -> tuple[str, ...]:
        self.take("lbrack")
        values: list[str] = []
        if (token := self.peek()) and token.kind == "rbrack":
            self.pos += 1
            return ()
        values.append(self.parse_string())
        while (token := self.peek()) and token.kind == "comma":
            self.pos += 1
            values.append(self.parse_string())
        self.take("rbrack")
        return tuple(values)


def parse(expression: str, subject: str = "amendment") -> Any:
    return _Parser(tokenize(expression), expression, subject).parse()


# --- Kleene evaluation -----------------------------------------------------------------


def _eval(node: Any, bindings: dict[str, Any]) -> bool | None:
    if isinstance(node, Literal):
        return node.value
    if isinstance(node, Not):
        inner = _eval(node.operand, bindings)
        return UNKNOWN if inner is UNKNOWN else not inner
    if isinstance(node, And):
        results = [_eval(p, bindings) for p in node.parts]
        if any(r is False for r in results):
            return False
        return UNKNOWN if any(r is UNKNOWN for r in results) else True
    if isinstance(node, Or):
        results = [_eval(p, bindings) for p in node.parts]
        if any(r is True for r in results):
            return True
        return UNKNOWN if any(r is UNKNOWN for r in results) else False
    if isinstance(node, Comparison):
        actual = bindings.get(node.field, UNKNOWN)
        if actual is UNKNOWN:
            return UNKNOWN
        if node.op == "eq":
            return actual == node.value
        if node.op == "ne":
            return actual != node.value
        if node.op == "in":
            return actual in node.value
        if node.op == "subset_of":
            return set(actual).issubset(set(node.value))
    raise AssertionError(f"unhandled node {node!r}")


def bindings_for(amendment: Amendment) -> dict[str, Any]:
    return {
        "kind": amendment.kind,
        "proposed_by": amendment.proposed_by,
        "run_id": amendment.run_id,
        "workflow_id": amendment.workflow_id,
        "ops": tuple(op.op for op in amendment.operations),
    }


def matches(expression: str, amendment: Amendment) -> bool:
    """Evaluate a rule against a concrete amendment. UNKNOWN cannot occur here."""
    return _eval(parse(expression), bindings_for(amendment)) is True


def provably_excludes_history_edit(expression: str) -> bool:
    """True when the rule cannot possibly match a ``history_edit`` amendment.

    Every field other than ``kind`` is left UNKNOWN, so the answer holds for *all* possible
    amendments, not just some sampled ones.
    """
    return _eval(parse(expression), {"kind": "history_edit"}) is False


def validate_policy(policy: ApprovalPolicy) -> None:
    """Reject a policy at write time rather than silently ignoring it at decision time.

    A ``history_edit`` amendment can never be auto-approved (contract 1.9, section 4), so any
    rule granting auto-approval must be provably restricted to ``forward`` amendments.
    """
    for index, rule in enumerate(policy.rules):
        parse(rule.match)  # syntax check regardless of auto_approve
        if rule.auto_approve and not provably_excludes_history_edit(rule.match):
            raise ValidationFailed(
                f"rules[{index}] grants auto_approve but is not restricted to forward "
                "amendments; a history_edit amendment can never be auto-approved. Add "
                "\"amendment.kind == 'forward'\" to the match expression.",
                details={"rule_index": index, "match": rule.match},
            )


# --- workflow approval (REQ-32 + REQ-43) ------------------------------------------------


def workflow_bindings(defn: Any) -> dict[str, Any]:
    origin = defn.from_template
    return {
        "template_id": origin.template_id if origin else None,
        "source": defn.source,
        "generated_by": defn.generated_by,
        "title": defn.title,
    }


# A workflow that came from no template. Distinct from UNKNOWN, which is also None: binding
# template_id to None would read as "unknown", and every comparison against it would go
# UNKNOWN rather than FALSE — so nothing could ever be proven safe.
ABSENT = object()


def provably_requires_a_template(expression: str) -> bool:
    """True when the rule cannot match a workflow that did not come from a template.

    The workflow half of REQ-32 is the same shape as the history_edit invariant: a plan a
    person wrote by hand must always be approved by a person. Auto-approval is only
    defensible for a plan whose *template* a human already approved, so a rule that grants it
    has to be provably restricted to template instances. Binding ``template_id`` to None and
    demanding a definite FALSE proves it for every possible workflow, not a sampled few.
    """
    return _eval(parse(expression, "workflow"), {"template_id": ABSENT}) is False


def validate_workflow_policy(policy: ApprovalPolicy) -> None:
    for index, rule in enumerate(policy.rules):
        parse(rule.match, "workflow")
        if rule.auto_approve and not provably_requires_a_template(rule.match):
            raise ValidationFailed(
                f"rules[{index}] grants auto_approve but is not restricted to workflows made "
                "from a template; a hand-written plan always needs a human (REQ-32). Add a "
                "condition on workflow.template_id.",
                details={"rule_index": index, "match": rule.match},
            )


def decide_workflow(policy: ApprovalPolicy, defn: Any) -> tuple[bool, str | None]:
    bindings = workflow_bindings(defn)
    if bindings["template_id"] is None:
        return False, None
    for index, rule in enumerate(policy.rules):
        if _eval(parse(rule.match, "workflow"), bindings) is True:
            return rule.auto_approve, rule.id or f"rules[{index}]"
    return False, None


def decide(policy: ApprovalPolicy, amendment: Amendment) -> tuple[bool, str | None]:
    """First matching rule wins. Returns ``(auto_approve, rule_id)``.

    The ``history_edit`` short-circuit is belt-and-braces: :func:`validate_policy` already
    makes it impossible to store a rule that could match one.
    """
    if amendment.kind == "history_edit":
        return False, None
    for index, rule in enumerate(policy.rules):
        if matches(rule.match, amendment):
            return rule.auto_approve, rule.id or f"rules[{index}]"
    return False, None
