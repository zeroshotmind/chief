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
from typing import Any

from ..models import Diagnostic, PlanGraph, VerifyResult
from ..models.plan import SOUND_AXIOMS

BEGIN_MARKER = "--CHIEF-PLAN-BEGIN--"
END_MARKER = "--CHIEF-PLAN-END--"

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


_DEF = re.compile(r"^\s*def\s+(?P<name>[A-Za-z_][A-Za-z0-9_'!?]*)", re.MULTILINE)
_STEP_CALL = re.compile(r"\b(?:task|checkpoint)\s+\"(?P<id>[^\"]+)\"")


def attribute_diagnostics(
    source: str, graph: PlanGraph | None, diagnostics: list[Diagnostic]
) -> list[Diagnostic]:
    """Best-effort: point each diagnostic at the step it is about.

    A failing entailment is reported where the edge is written, which is in the plan's ``do``
    block — a line naming the consuming *function*, not the step id. So the mapping goes
    through the source: each ``def`` is scanned for the ``task``/``checkpoint`` call inside it,
    which gives function name to step id, and a diagnostic is attributed to the definition it
    falls inside, or failing that to whichever known function is named on its own line.

    Heuristic, and labelled as such. It decides which node a failure is drawn on in the UI, and
    nothing else — the message stays exactly as Lean wrote it, and a diagnostic that cannot be
    placed simply has no ``step_id``.

    The step ids come from the source rather than from ``graph``, because the case this exists
    for is the case where there is no graph: a plan that failed to compile printed nothing, and
    that is precisely when a reader needs to know which node broke.
    """
    lines = source.splitlines()
    known = (
        {node.id for node in graph.nodes}
        if graph is not None
        else {m.group("id") for m in _STEP_CALL.finditer(source)}
    )

    spans: list[tuple[int, str]] = [
        (source.count("\n", 0, m.start()) + 1, m.group("name")) for m in _DEF.finditer(source)
    ]
    owner: dict[str, str] = {}
    for index, (start, name) in enumerate(spans):
        end = spans[index + 1][0] if index + 1 < len(spans) else len(lines) + 1
        body = "\n".join(lines[start - 1 : end - 1])
        call = _STEP_CALL.search(body)
        if call and call.group("id") in known:
            owner[name] = call.group("id")

    def enclosing(line: int) -> str | None:
        found = None
        for start, name in spans:
            if start <= line:
                found = name
            else:
                break
        return found

    out: list[Diagnostic] = []
    for diagnostic in diagnostics:
        step_id = None
        if diagnostic.line is not None:
            name = enclosing(diagnostic.line)
            if name is not None and name in owner:
                step_id = owner[name]
            elif 1 <= diagnostic.line <= len(lines):
                text = lines[diagnostic.line - 1]
                for candidate, mapped in owner.items():
                    if re.search(rf"\b{re.escape(candidate)}\b", text):
                        step_id = mapped
                        break
        out.append(
            diagnostic if step_id is None else diagnostic.model_copy(update={"step_id": step_id})
        )
    return out


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

    # Placed here rather than left to the caller: a stored result whose diagnostics were never
    # attributed shows every failure on nothing, and the endpoint that stores it has no reason
    # to know it had to ask.
    diagnostics = attribute_diagnostics(source, graph, _drop_cascades(diagnostics))

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
