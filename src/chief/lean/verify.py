"""Running Lean over a graph and reading the graph back out.

The whole of verification is one subprocess. The file is copied to a temporary directory with
``#print axioms`` appended, ``lake env lean`` is run over it once, and both things we need
come back on the same stream: the diagnostics, and — if it compiled — the JSON the graph's own
``#eval emitGraph`` printed between markers.

Three layers of checking happen, and it is worth being precise about which is load-bearing:

* **The kernel.** Every edge's entailment, and the non-vacuity of every contract. This is the
  part that means something, and it is not something this file implements — it is what Lean
  did before printing anything.
* **The axiom check.** ``#print axioms`` on the graph reports every axiom the graph's term
  transitively depends on. A `sorry` anywhere the graph reaches shows up as ``sorryAx``; a
  `native_decide` shows up as ``Lean.ofReduceBool``. Both would let a graph claim a proof it
  does not have, and both are caught here rather than by grep, which is why this runs even
  though the source lint below also looks for them.
* **The source lint.** Fast, and about the things the kernel has no opinion on: an
  ``@[implemented_by]`` or an ``unsafe`` definition does not weaken a proof, but it can make
  the extracted graph differ from the verified one, which breaks the single-source property
  the whole design rests on.

A graph that fails any of these is ``failed``, and the diagnostics are what a planner reads to
repair it. Lean's own error text is mostly left alone — "unsolved goals, hx : auc ≥ 70,
⊢ auc ≥ 75" names the exact entailment that does not hold, and no wording invented here would
beat it. Three things are done to it, all for the same reason:

* the consequences of an earlier failure are dropped, so a graph does not report a `sorry` its
  author never wrote merely because Lean declined to run `#eval` in a file that failed;
* the duplicate "could not synthesize default value" that accompanies every failed edge is
  dropped, because it names an internal parameter and doubles the error count;
* an entailment that failed only because a contract was bound with `def` is told to say so.
  That message is the one case where Lean's own text actively misleads: the unreduced `match`
  it prints reads as *"the incoming promise is unknown"*, which points a reader straight at a
  contract that is usually perfectly correct.

Everything else is passed through verbatim.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from ..models import Diagnostic, ExtractedGraph, VerifyResult
from ..models.proof_graph import SOUND_AXIOMS

BEGIN_MARKER = "--PROOF-GRAPH-BEGIN--"
END_MARKER = "--PROOF-GRAPH-END--"

#: The name a graph file must bind its graph to, and the symbol the axiom check is run against.
ENTRY_POINT = "graph"

# The severity may carry a category — `error(lean.unknownIdentifier):` — and a parser that
# only knows the bare form drops exactly the errors that name the offending identifier,
# leaving the cascade tail as the whole story.
_DIAGNOSTIC = re.compile(
    r"^(?P<file>[^\s].*?):(?P<line>\d+):(?P<col>\d+): "
    r"(?P<severity>error|warning|info|information)(?:\([^)]*\))?: (?P<message>.*)$"
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
     "raises maxHeartbeats, which is not needed for the decidable fragment graphs are "
     "scoped to"),
)

#: A `Contract` bound with `def` is opaque to `graph_entails`, so every edge touching it fails
#: with an error that looks like the contract is wrong when it is only unreducible. Common
#: enough, and confusing enough, to be worth naming rather than leaving to the goal display.
_DEF_CONTRACT = re.compile(r"^\s*def\s+(?P<name>\w+)\s*:\s*Contract\b", re.MULTILINE)


class LeanUnavailable(RuntimeError):
    """No Lean toolchain, or no ``ProofGraph`` package to check against.

    Raised rather than returned: a graph that could not be checked is not a graph that failed,
    and collapsing the two would let a machine without ``lake`` quietly mark every graph
    unsound.
    """


def package_dir() -> Path | None:
    """Where the ``ProofGraph`` Lean package lives.

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
    is an optional dependency, and an instance without it should say graphs cannot be verified
    here, not that they are unsound.
    """
    return shutil.which("lake") is not None and package_dir() is not None


#: What `lake build` leaves behind. Its absence is the state a fresh checkout is in, and the
#: state that used to make every graph look unsound.
_BUILT = Path(".lake") / "build" / "lib" / "lean" / "ProofGraph.olean"


def is_built(package: Path) -> bool:
    return (package / _BUILT).exists()


def ensure_built(package: Path, *, timeout: float = 600.0) -> None:
    """Build the prelude if it has not been built here.

    A checkout has no `.lake`, so without this the first graph anyone checks fails with Lean
    reporting an unknown module prefix — which reads as *"this graph is broken"* when what is
    actually true is *"nothing has been compiled yet"*. That is precisely the confusion the
    rest of this module exists to prevent, arriving through the back door.

    Built once and then skipped, so it costs a couple of seconds on a new machine and nothing
    afterwards. A build that fails raises rather than returning a verdict: a graph that could
    not be checked has not been found unsound.
    """
    if is_built(package):
        return
    try:
        completed = subprocess.run(
            ["lake", "build"], cwd=package, capture_output=True, text=True, timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise LeanUnavailable(
            f"building the ProofGraph prelude took longer than {timeout:g}s"
        ) from exc
    if completed.returncode != 0 or not is_built(package):
        raise LeanUnavailable(
            "the ProofGraph prelude could not be built, so no graph can be checked here:\n"
            + ((completed.stderr or completed.stdout or "").strip()[-2000:] or "no output")
        )


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
                    message=f"a graph may not be verified when it {what}",
                )
            )

    for match in _DEF_CONTRACT.finditer(stripped):
        out.append(
            Diagnostic(
                severity="warning",
                line=_line_of(source, match.start()),
                message=(
                    f"contract '{match.group('name')}' is bound with `def`; use `abbrev` so "
                    "`graph_entails` can see the predicate underneath the name"
                ),
            )
        )

    if f"def {ENTRY_POINT}" not in stripped:
        out.append(
            Diagnostic(
                severity="error",
                message=f"a graph must define `{ENTRY_POINT} : GraphM Unit`",
            )
        )
    if "emitGraph" not in stripped:
        out.append(
            Diagnostic(
                severity="error",
                message=f'a graph must end with `#eval emitGraph "<title>" {ENTRY_POINT}`',
            )
        )
    return out


def def_bound_contracts(source: str) -> set[str]:
    """Contracts bound with `def`, which `graph_entails` cannot see through."""
    return {m.group("name") for m in _DEF_CONTRACT.finditer(_strip_noise(source))}


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
        diagnostic.message = diagnostic.message.replace(source_name, "graph")
    return diagnostics, ("\n".join(payload).strip() or None), axioms


_DEF = re.compile(r"^\s*def\s+(?P<name>[A-Za-z_][A-Za-z0-9_'!?]*)", re.MULTILINE)
_STEP_CALL = re.compile(r"\b(?:task|checkpoint)\s+\"(?P<id>[^\"]+)\"")


def attribute_diagnostics(
    source: str, graph: ExtractedGraph | None, diagnostics: list[Diagnostic]
) -> list[Diagnostic]:
    """Best-effort: point each diagnostic at the step it is about.

    A failing entailment is reported where the edge is written, which is in the graph's ``do``
    block — a line naming the consuming *function*, not the step id. So the mapping goes
    through the source: each ``def`` is scanned for the ``task``/``checkpoint`` call inside it,
    which gives function name to step id, and a diagnostic is attributed to the definition it
    falls inside, or failing that to whichever known function is named on its own line.

    Heuristic, and labelled as such. It decides which node a failure is drawn on in the UI, and
    nothing else — the message stays exactly as Lean wrote it, and a diagnostic that cannot be
    placed simply has no ``step_id``.

    The step ids come from the source rather than from ``graph``, because the case this exists
    for is the case where there is no graph: a graph that failed to compile printed nothing, and
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
#: terms of the `sorry` axiom — because an errored term becomes one. On a graph whose author
#: wrote no `sorry` that message is a consequence of the real error and reads as a second,
#: mystifying one. Dropped when something else already failed; kept when it stands alone,
#: where it is the only thing saying why no graph came back.
_CASCADE = "Aborting evaluation since the expression depends on the 'sorry' axiom"


def _drop_cascades(diagnostics: list[Diagnostic]) -> list[Diagnostic]:
    errors = [d for d in diagnostics if d.severity == "error"]
    if len(errors) < 2:
        return diagnostics
    return [d for d in diagnostics if _CASCADE not in d.message]


#: Lean reports a failed `use` twice at one position: once to say the default value for the
#: entailment argument could not be synthesised, and once with the goal that was left. Only the
#: second says anything — the first names an internal parameter a graph author never writes, and
#: it doubles the error count on every failing edge.
_SYNTH = "could not synthesize default value for parameter"


def _drop_synthesis_noise(diagnostics: list[Diagnostic]) -> list[Diagnostic]:
    placed = {
        (d.line, d.column) for d in diagnostics if d.severity == "error" and _SYNTH not in d.message
    }
    return [
        d
        for d in diagnostics
        if _SYNTH not in d.message or (d.line, d.column) not in placed
    ]


_OPAQUE_OPEN = "(match (motive := Contract"
_OPAQUE_NAME = re.compile(r"Prop\)\s*(?P<name>[A-Za-z_][A-Za-z0-9_']*)\s+with")


def _collapse_opaque(message: str) -> tuple[str, set[str]]:
    """Replace an unreduced `Contract.pred` match with the name it could not see through.

    A contract bound with `def` does not reduce, so the hypothesis Lean prints is the whole
    `match` term rather than the predicate. Left alone it reads as *"the incoming promise is
    unknown"*, which points a reader at the contract's own definition — an edge that is very
    often perfectly correct and fails only because the name is opaque. Collapsing the term to
    the name says what is actually true and takes the trap out of the message.
    """
    names: set[str] = set()
    out = message
    while True:
        start = out.find(_OPAQUE_OPEN)
        if start == -1:
            return out, names
        depth, end = 0, start
        while end < len(out):
            if out[end] == "(":
                depth += 1
            elif out[end] == ")":
                depth -= 1
                if depth == 0:
                    break
            end += 1
        if end >= len(out):
            return out, names
        found = _OPAQUE_NAME.search(out[start : end + 1])
        name = found.group("name") if found else None
        if name:
            names.add(name)
        label = f"‹{name} — opaque›" if name else "‹opaque contract›"
        out = out[:start] + label + out[end + 1 :]


def explain_opaque(diagnostics: list[Diagnostic], opaque: set[str]) -> list[Diagnostic]:
    """Make an entailment failure caused by a `def`-bound contract say so.

    The severity stays `error` — the graph really did fail to check here — but the message now
    names the cause rather than leaving a reader to infer a defect in a contract that may be
    entirely sound. Without this the only thing standing between the reader and editing a
    correct contract is the lint warning several screens up.
    """
    out: list[Diagnostic] = []
    for diagnostic in diagnostics:
        message, names = _collapse_opaque(diagnostic.message)
        blocking = sorted(names & opaque)
        if blocking:
            named = ", ".join(f"'{name}'" for name in blocking)
            message = (
                f"this edge could not be checked because {named} is bound with `def`, so its "
                "predicate cannot be seen through the name. Change it to `abbrev` and verify "
                "again — the entailment itself may well hold.\n\n" + message
            )
        out.append(
            diagnostic if message == diagnostic.message else diagnostic.model_copy(
                update={"message": message}
            )
        )
    return out


def verify_source(source: str, *, timeout: float = 120.0) -> VerifyResult:
    """Check one graph and, if it holds up, read its graph out.

    ``source`` is the whole of a graph file, exactly as its author wrote it. Nothing is
    rewritten except the appended ``#print axioms``, which goes at the end so every line
    number a diagnostic reports still points where the author is looking.
    """
    if not available():
        raise LeanUnavailable(
            "verifying a graph needs the Lean toolchain (`lake` on PATH) and the ProofGraph "
            "package; neither is required to run a workflow that was already compiled"
        )
    package = package_dir()
    assert package is not None  # available() just checked

    lint = lint_source(source)
    if any(d.severity == "error" for d in lint):
        return VerifyResult(status="failed", diagnostics=lint, toolchain=toolchain_version())

    # Before anything is checked, not after: an unbuilt prelude makes every graph look broken.
    ensure_built(package)

    with tempfile.TemporaryDirectory(prefix="chief-graph-") as tmp:
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
                    "the graph depends on "
                    + ", ".join(sorted(unsound))
                    + " — a proof here was not checked by the kernel"
                ),
            )
        )

    graph: ExtractedGraph | None = None
    if payload:
        try:
            graph = ExtractedGraph.model_validate_json(payload)
        except Exception as exc:
            # Not a verdict on the graph. The JSON is printed by the prelude, never by the
            # graph's author, so a shape this cannot read means the prelude and this server
            # disagree about what a graph is — a checkout where one was updated and the other
            # was not, or a server still running the code it was started with. Reporting that
            # as "the graph does not hold up" is the same lie as an unbuilt prelude was, and it
            # is worse here because the graph is fine.
            raise LeanUnavailable(
                "the ProofGraph prelude printed a graph this server cannot read, so nothing "
                "about the graph could be established. The prelude and the server are out of "
                f"step — restarting the server usually settles it. Details: {exc}"
            ) from exc

    if graph is not None:
        diagnostics.extend(
            Diagnostic(severity="error", message=problem) for problem in graph.problems
        )
        if graph.stats.vacuous:
            diagnostics.append(
                Diagnostic(
                    severity="error",
                    message=(
                        "every contract in this graph is `any`, so verification established "
                        "nothing about it; give the artifacts that matter a real condition"
                    ),
                )
            )

    # Placed here rather than left to the caller: a stored result whose diagnostics were never
    # attributed shows every failure on nothing, and the endpoint that stores it has no reason
    # to know it had to ask. Order matters — the consequences of an earlier failure are dropped
    # before what is left is explained and placed.
    diagnostics = _drop_synthesis_noise(_drop_cascades(diagnostics))
    diagnostics = explain_opaque(diagnostics, def_bound_contracts(source))
    diagnostics = attribute_diagnostics(source, graph, diagnostics)

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
