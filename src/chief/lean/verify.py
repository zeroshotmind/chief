"""Running Lean over a plan and reading the graph back out.

The whole of verification is one subprocess. The file is copied to a temporary directory with
``#print axioms`` appended, ``lake env lean`` is run over it once, and both things we need
come back on the same stream: the diagnostics, and — if it compiled — the JSON the plan's own
``#eval emitPlan`` printed between markers.

Three layers of checking happen, and it is worth being precise about which is load-bearing:

* **The kernel.** Every edge's entailment, and the non-vacuity of every contract. This is the
  part that means something, and it is not something this file implements — it is what Lean
  did before printing anything.
* **The axiom check.** ``#print axioms`` on the plan reports every axiom the plan's term
  transitively depends on. A `sorry` anywhere the plan reaches shows up as ``sorryAx``; a
  `native_decide` shows up as ``Lean.ofReduceBool``. Both would let a plan claim a proof it
  does not have, and both are caught here rather than by grep, which is why this runs even
  though the source lint below also looks for them.
* **The source lint.** Fast, and about the things the kernel has no opinion on: an
  ``@[implemented_by]`` or an ``unsafe`` definition does not weaken a proof, but it can make
  the extracted graph differ from the verified one, which breaks the single-source property
  the whole design rests on.

A plan that fails any of these is ``failed``, and the diagnostics are what a planner reads to
repair it. Lean's own error text is left alone: "unsolved goals ⊢ 5000 ≤ x.rows" names the
exact entailment that does not hold, and no wording invented here would beat it.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

BEGIN_MARKER = "--CHIEF-PLAN-BEGIN--"
END_MARKER = "--CHIEF-PLAN-END--"

#: The three axioms of Lean's standard logic. Every ordinary proof may depend on these, and a
#: plan depending on nothing else is a plan whose proofs are real. Anything outside this set
#: is either a hole (``sorryAx``) or a claim discharged by running compiled code rather than
#: by the kernel (``Lean.ofReduceBool``, from ``native_decide``).
SOUND_AXIOMS = frozenset({"propext", "Classical.choice", "Quot.sound"})

#: The name a plan file must bind its plan to, and the symbol the axiom check is run against.
ENTRY_POINT = "plan"

_DIAGNOSTIC = re.compile(
    r"^(?P<file>[^\s].*?):(?P<line>\d+):(?P<col>\d+): "
    r"(?P<severity>error|warning|info|information): (?P<message>.*)$"
)
_AXIOMS = re.compile(r"depends on axioms: \[(?P<axioms>[^\]]*)\]")
_LINE_COMMENT = re.compile(r"--.*$", re.MULTILINE)
_BLOCK_COMMENT = re.compile(r"/-.*?-/", re.DOTALL)
_STRING = re.compile(r'"(?:[^"\\]|\\.)*"')

#: Constructs that would let an extracted graph differ from the verified one, or a proof be
#: discharged outside the kernel. Checked against source with comments and strings removed, so
#: a goal that happens to contain the word is not a finding.
_BANNED = (
    (re.compile(r"^\s*axiom\s", re.MULTILINE), "declares an axiom"),
    (re.compile(r"^\s*unsafe\s", re.MULTILINE), "declares an unsafe definition"),
    (re.compile(r"@\[implemented_by"), "uses @[implemented_by], which can make the "
                                      "extracted graph differ from the verified one"),
    (re.compile(r"\bnative_decide\b"), "uses native_decide, which discharges a goal by "
                                       "running compiled code rather than by the kernel"),
    (re.compile(r"\bsorry\b"), "contains a `sorry`"),
    (re.compile(r"^\s*set_option\s+\S*maxHeartbeats", re.MULTILINE),
     "raises maxHeartbeats, which is not needed for the decidable fragment plans are "
     "scoped to"),
)

#: A `Contract` bound with `def` is opaque to `plan_entails`, so every edge touching it fails
#: with an error that looks like the contract is wrong when it is only unreducible. Common
#: enough, and confusing enough, to be worth naming rather than leaving to the goal display.
_DEF_CONTRACT = re.compile(r"^\s*def\s+(?P<name>\w+)\s*:\s*Contract\b", re.MULTILINE)


class LeanUnavailable(RuntimeError):
    """No Lean toolchain, or no ``ChiefPlan`` package to check against.

    Raised rather than returned: a plan that could not be checked is not a plan that failed,
    and collapsing the two would let a machine without ``lake`` quietly mark every plan
    unsound.
    """


class Diagnostic(BaseModel):
    """One thing Lean said, positioned in the plan's own source."""

    model_config = ConfigDict(extra="forbid")

    severity: Literal["error", "warning", "info"]
    line: int | None = None
    column: int | None = None
    message: str
    #: Filled in by :func:`~chief.lean.compile.attribute_diagnostics` where a line can be
    #: traced to a step, so the UI can show a failure on the node that caused it.
    step_id: str | None = None


class PlanPort(BaseModel):
    """One artifact crossing one edge."""

    model_config = ConfigDict(extra="forbid")

    label: str
    source: str
    artifact_type: str
    contract: str
    refined: bool


class PlanNode(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    type: Literal["task", "checkpoint"]
    goal: str
    harness: str
    criteria: list[str] = Field(default_factory=list)
    fields: list[str] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)
    inputs: list[PlanPort] = Field(default_factory=list)
    produces: PlanPort | None = None


class PlanStats(BaseModel):
    """How much the plan actually claims.

    ``contracts_any`` against ``contracts_refined`` is the measure that matters. A plan whose
    contracts are all ``any`` type-checks, extracts cleanly, and has been verified to say
    nothing — it must never present the way one full of real refinements does, and these
    counts are what let a reader tell them apart.
    """

    model_config = ConfigDict(extra="forbid")

    nodes: int = 0
    edges: int = 0
    contracts_total: int = 0
    contracts_refined: int = 0
    contracts_any: int = 0

    @property
    def vacuous(self) -> bool:
        """True when nothing in this plan constrains anything."""
        return self.contracts_total > 0 and self.contracts_refined == 0


class PlanGraph(BaseModel):
    # ``schema`` would shadow a BaseModel attribute, so the field is named around it and
    # aliased back; populating by either name keeps callers from having to know that.
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    schema_: str = Field(alias="schema")
    title: str
    nodes: list[PlanNode] = Field(default_factory=list)
    problems: list[str] = Field(default_factory=list)
    stats: PlanStats = Field(default_factory=PlanStats)


class VerifyResult(BaseModel):
    """What one verification run concluded."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["verified", "failed"]
    diagnostics: list[Diagnostic] = Field(default_factory=list)
    graph: PlanGraph | None = None
    #: Which Lean built this verdict. Stored beside a plan because "verified" is a claim about
    #: a toolchain as much as about a file, and a plan verified two toolchains ago has not
    #: been verified by the one running now.
    toolchain: str | None = None
    axioms: list[str] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == "verified"

    @property
    def errors(self) -> list[Diagnostic]:
        return [d for d in self.diagnostics if d.severity == "error"]


def package_dir() -> Path | None:
    """Where the ``ChiefPlan`` Lean package lives.

    ``CHIEF_LEAN_DIR`` wins, so a deployment that ships the package elsewhere can say so.
    Otherwise it is found by walking up from this file, which is what makes a checkout work
    with no configuration at all.
    """
    override = os.environ.get("CHIEF_LEAN_DIR")
    if override:
        candidate = Path(override).expanduser()
        return candidate if (candidate / "lakefile.toml").exists() else None
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "lean"
        if (candidate / "lakefile.toml").exists():
            return candidate
    return None


def available() -> bool:
    """Whether this machine can verify anything at all.

    Checked before offering verification rather than discovered by a failing subprocess: Lean
    is an optional dependency, and an instance without it should say plans cannot be verified
    here, not that they are unsound.
    """
    return shutil.which("lake") is not None and package_dir() is not None


def toolchain_version() -> str | None:
    package = package_dir()
    if package is None:
        return None
    toolchain = package / "lean-toolchain"
    return toolchain.read_text(encoding="utf-8").strip() if toolchain.exists() else None


def _strip_noise(source: str) -> str:
    """Source with comments and string literals blanked, for lint matching.

    Blanked rather than deleted so line numbers survive — a finding reported on the wrong line
    is worse than one reported without a line.
    """
    def blank(match: re.Match[str]) -> str:
        return re.sub(r"\S", " ", match.group(0))

    return _LINE_COMMENT.sub(blank, _BLOCK_COMMENT.sub(blank, _STRING.sub(blank, source)))


def _line_of(source: str, index: int) -> int:
    return source.count("\n", 0, index) + 1


def lint_source(source: str) -> list[Diagnostic]:
    """Everything worth refusing before Lean is even started."""
    stripped = _strip_noise(source)
    out: list[Diagnostic] = []

    for pattern, what in _BANNED:
        for match in pattern.finditer(stripped):
            out.append(
                Diagnostic(
                    severity="error",
                    line=_line_of(source, match.start()),
                    message=f"a plan may not be verified when it {what}",
                )
            )

    for match in _DEF_CONTRACT.finditer(stripped):
        out.append(
            Diagnostic(
                severity="warning",
                line=_line_of(source, match.start()),
                message=(
                    f"contract '{match.group('name')}' is bound with `def`; use `abbrev` so "
                    "`plan_entails` can see the predicate underneath the name"
                ),
            )
        )

    if f"def {ENTRY_POINT}" not in stripped:
        out.append(
            Diagnostic(
                severity="error",
                message=f"a plan must define `{ENTRY_POINT} : PlanM Unit`",
            )
        )
    if "emitPlan" not in stripped:
        out.append(
            Diagnostic(
                severity="error",
                message=f'a plan must end with `#eval emitPlan "<title>" {ENTRY_POINT}`',
            )
        )
    return out


def parse_output(
    output: str, *, source_name: str
) -> tuple[list[Diagnostic], str | None, list[str]]:
    """Split Lean's stream into diagnostics, the emitted JSON, and the axiom list.

    Diagnostics wrap: a header line carries the position and the first line of the message,
    and everything up to the next header belongs to it. Joining those back together is what
    keeps "unsolved goals" attached to the goal it is about, which is the single most useful
    thing a planner reads.
    """
    payload: list[str] = []
    diagnostics: list[Diagnostic] = []
    axioms: list[str] = []
    current: Diagnostic | None = None
    body: list[str] = []
    in_payload = False

    def flush() -> None:
        nonlocal current, body
        if current is not None:
            text = "\n".join([current.message, *body]).strip()
            diagnostics.append(current.model_copy(update={"message": text}))
        current, body = None, []

    for raw in output.splitlines():
        if raw.strip() == BEGIN_MARKER:
            flush()
            in_payload = True
            continue
        if raw.strip() == END_MARKER:
            in_payload = False
            continue
        if in_payload:
            payload.append(raw)
            continue

        match = _DIAGNOSTIC.match(raw)
        if match:
            flush()
            found = _AXIOMS.search(match.group("message"))
            if found:
                axioms = [a.strip() for a in found.group("axioms").split(",") if a.strip()]
                continue
            severity = match.group("severity")
            current = Diagnostic(
                severity="info" if severity == "information" else severity,  # type: ignore[arg-type]
                line=int(match.group("line")),
                column=int(match.group("col")),
                message=match.group("message"),
            )
        elif current is not None:
            body.append(raw)
        else:
            found = _AXIOMS.search(raw)
            if found:
                axioms = [a.strip() for a in found.group("axioms").split(",") if a.strip()]

    flush()
    for diagnostic in diagnostics:
        diagnostic.message = diagnostic.message.replace(source_name, "plan")
    return diagnostics, ("\n".join(payload).strip() or None), axioms


#: Lean refuses to run `#eval` in a file that already failed to elaborate, and says so in
#: terms of the `sorry` axiom — because an errored term becomes one. On a plan whose author
#: wrote no `sorry` that message is a consequence of the real error and reads as a second,
#: mystifying one. Dropped when something else already failed; kept when it stands alone,
#: where it is the only thing saying why no graph came back.
_CASCADE = "Aborting evaluation since the expression depends on the 'sorry' axiom"


def _drop_cascades(diagnostics: list[Diagnostic]) -> list[Diagnostic]:
    errors = [d for d in diagnostics if d.severity == "error"]
    if len(errors) < 2:
        return diagnostics
    return [d for d in diagnostics if _CASCADE not in d.message]


def verify_source(source: str, *, timeout: float = 120.0) -> VerifyResult:
    """Check one plan and, if it holds up, read its graph out.

    ``source`` is the whole of a plan file, exactly as its author wrote it. Nothing is
    rewritten except the appended ``#print axioms``, which goes at the end so every line
    number a diagnostic reports still points where the author is looking.
    """
    if not available():
        raise LeanUnavailable(
            "verifying a plan needs the Lean toolchain (`lake` on PATH) and the ChiefPlan "
            "package; neither is required to run a workflow that was already compiled"
        )
    package = package_dir()
    assert package is not None  # available() just checked

    lint = lint_source(source)
    if any(d.severity == "error" for d in lint):
        return VerifyResult(status="failed", diagnostics=lint, toolchain=toolchain_version())

    with tempfile.TemporaryDirectory(prefix="chief-plan-") as tmp:
        path = Path(tmp) / "Plan.lean"
        body = source if source.endswith("\n") else source + "\n"
        path.write_text(f"{body}\n#print axioms {ENTRY_POINT}\n", encoding="utf-8")
        try:
            completed = subprocess.run(
                ["lake", "env", "lean", str(path)],
                cwd=package,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return VerifyResult(
                status="failed",
                diagnostics=[
                    *lint,
                    Diagnostic(
                        severity="error",
                        message=(
                            f"Lean did not finish within {timeout:g}s. Contracts in the "
                            "decidable fragment check in well under a second, so this usually "
                            "means a predicate that `omega` and `simp` cannot settle."
                        ),
                    ),
                ],
                toolchain=toolchain_version(),
            )

        stream = (completed.stdout or "") + (completed.stderr or "")
        diagnostics, payload, axioms = parse_output(stream, source_name=str(path))
        diagnostics = [*lint, *diagnostics]

    unsound = [a for a in axioms if a not in SOUND_AXIOMS]
    if unsound:
        diagnostics.append(
            Diagnostic(
                severity="error",
                message=(
                    "the plan depends on "
                    + ", ".join(sorted(unsound))
                    + " — a proof here was not checked by the kernel"
                ),
            )
        )

    graph: PlanGraph | None = None
    if payload:
        try:
            graph = PlanGraph.model_validate_json(payload)
        except Exception as exc:  # noqa: BLE001 - reported, not raised
            diagnostics.append(
                Diagnostic(
                    severity="error",
                    message=f"the plan printed a graph that could not be read: {exc}",
                )
            )

    if graph is not None:
        diagnostics.extend(
            Diagnostic(severity="error", message=problem) for problem in graph.problems
        )
        if graph.stats.vacuous:
            diagnostics.append(
                Diagnostic(
                    severity="error",
                    message=(
                        "every contract in this plan is `any`, so verification established "
                        "nothing about it; give the artifacts that matter a real condition"
                    ),
                )
            )

    diagnostics = _drop_cascades(diagnostics)

    failed = completed.returncode != 0 or graph is None
    failed = failed or any(d.severity == "error" for d in diagnostics)

    return VerifyResult(
        status="failed" if failed else "verified",
        diagnostics=diagnostics,
        graph=graph,
        toolchain=toolchain_version(),
        axioms=axioms,
    )


def verify_file(path: str | os.PathLike[str], **kwargs: Any) -> VerifyResult:
    return verify_source(Path(path).read_text(encoding="utf-8"), **kwargs)
