"""ProofGraph — a workflow graph whose every edge is a theorem (extension).

A template is a shape you keep; a workflow is what you are running this time. A *proof
graph* is neither: it is a candidate process nobody has agreed to yet, written so that a
proof assistant can say whether it hangs together. Its steps declare what they need from
the ones before them, and
checking it establishes that every one of those demands is met by what feeds it — before a
person is asked to approve anything, and before a harness executes a step whose inputs were
never going to exist.

The canonical form of a proof graph is its Lean source. The graph below is *derived* from
that source by running it, not written alongside it, which is the property the whole design
rests on: there is no second description to drift out of step with what was proven.
Everything here that is not ``lean_source`` is a record of what checking it concluded.

A verified proof graph compiles into an ordinary :class:`WorkflowDefinition` and from there is
approved, run and amended like any other. Nothing downstream knows it came from here.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .review import ReviewNote

#: A proof graph is ``draft`` until it has been checked, and then says how that went.
#: Deliberately not a workflow status: a graph is never "approved" or "archived", it is
#: compiled into something that can be.
ProofGraphStatus = Literal["draft", "verified", "failed"]

#: The three axioms of Lean's standard logic. A proof depending on nothing outside this set is
#: one the kernel checked; anything else is either a hole or a claim discharged by running
#: compiled code.
SOUND_AXIOMS = frozenset({"propext", "Classical.choice", "Quot.sound"})


class Diagnostic(BaseModel):
    """One thing the checker said, positioned in the graph's own source."""

    model_config = ConfigDict(extra="forbid")

    severity: Literal["error", "warning", "info"]
    line: int | None = None
    column: int | None = None
    message: str
    #: Which step this is about, where that could be worked out. Best-effort and often absent:
    #: it decides which node a failure is drawn on, and nothing else.
    step_id: str | None = None


class SchemaField(BaseModel):
    """One field of an artifact's schema: name, pretty-printed Lean type, and — where the
    field's own type is a structure whose schema was derived — that structure's fields,
    nested. A field with none nested is a leaf or an underived type; the graph does not say
    which, and neither does this."""

    model_config = ConfigDict(extra="forbid")

    name: str
    type: str
    fields: list[SchemaField] = Field(default_factory=list)


class GraphPort(BaseModel):
    """One artifact crossing one edge.

    ``contract`` is the demanding side's condition on an input and the promising side's on an
    output. The two differ exactly where the graph weakened one to the other, and that
    difference is what was proven — showing both is how a reader sees the claim rather than
    just the fact that a claim was made.
    """

    # ``schema`` shadows a BaseModel attribute, same as on ExtractedGraph, and is aliased the
    # same way for the same reason.
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    label: str
    #: The step that produces this artifact. What turns a value dependency into a graph edge.
    source: str
    artifact_type: str
    contract: str
    #: False for a contract that constrains nothing. Per-port rather than only in the totals,
    #: because a graph of nine real contracts and one empty one on the edge that matters must
    #: be able to say *which* edge.
    refined: bool
    #: The artifact type's fields, where the graph derived them with ``artifact_schema``.
    #: Empty means undeclared, never field-free — the UI must not read it as "no fields".
    schema_: list[SchemaField] = Field(default_factory=list, alias="schema")


class AlgLine(BaseModel):
    """One rendered line of a step's algorithm."""

    model_config = ConfigDict(extra="forbid")

    indent: int = 0
    text: str


class AlgExternal(BaseModel):
    """One external dependency an algorithm reaches for — an LLM call, a search API, a
    database query, a library routine. Collected off the checked term, not declared."""

    model_config = ConfigDict(extra="forbid")

    tag: str
    fn: str


class StepAlgorithm(BaseModel):
    """A step's algorithm as pseudocode, rendered from the term Lean elaborated.

    What this carries was checked for scope and shape only — an algorithm whose variables
    do not hold together never gets this far, because its problems fail the graph. Nothing
    here is a claim about what the mathematics means, and the UI must not present it as
    one.
    """

    model_config = ConfigDict(extra="forbid")

    lines: list[AlgLine] = Field(default_factory=list)
    externals: list[AlgExternal] = Field(default_factory=list)


class FixedArtifact(BaseModel):
    """An input fixed before anything runs — a file, a config, a URL the step starts from.

    Known at graph time rather than produced by an upstream step, so no contract rides on
    it and nothing about it is proven: it is shown beside the contracted inputs, and the
    compiled workflow hands it to the harness as an ordinary input.
    """

    model_config = ConfigDict(extra="forbid")

    label: str
    ref: str
    description: str = ""


class GraphNode(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    type: Literal["task", "checkpoint"]
    goal: str
    harness: str
    #: Which part of the work this step belongs to, if the graph says. A label for a reader.
    group: str | None = None
    criteria: list[str] = Field(default_factory=list)
    fields: list[str] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)
    inputs: list[GraphPort] = Field(default_factory=list)
    #: Inputs fixed at graph time, shown beside the contracted ones.
    fixed: list[FixedArtifact] = Field(default_factory=list)
    produces: GraphPort | None = None
    #: The step's algorithm, if the graph gives one.
    algorithm: StepAlgorithm | None = None


class GraphStats(BaseModel):
    """How much the graph actually claims.

    ``contracts_any`` against ``contracts_refined`` is the measure that matters. A graph whose
    contracts are all unconstrained checks cleanly and has been verified to say nothing; it
    must never present the way one full of real conditions does.
    """

    model_config = ConfigDict(extra="forbid")

    nodes: int = 0
    edges: int = 0
    contracts_total: int = 0
    contracts_refined: int = 0
    contracts_any: int = 0
    #: How many steps carry an algorithm. Absent from graphs an older prelude printed.
    algorithms: int = 0

    @property
    def vacuous(self) -> bool:
        """True when nothing in this graph constrains anything."""
        return self.contracts_total > 0 and self.contracts_refined == 0


class GraphGroup(BaseModel):
    """What one group of steps is for, in a line. Declared in the graph's source with
    ``describeGroup``; a description of a group no step belongs to never gets this far,
    because extraction reports it as a problem instead."""

    model_config = ConfigDict(extra="forbid")

    path: str
    description: str


class ExtractedGraph(BaseModel):
    """What extraction read back out of the graph's source by running it."""

    # ``schema`` would shadow a BaseModel attribute, so the field is named around it and
    # aliased back; populating by either name keeps callers from having to know that.
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    schema_: str = Field(alias="schema")
    title: str
    nodes: list[GraphNode] = Field(default_factory=list)
    #: Group descriptions, where the graph gives any. Keyed by path, nesting on ``/``.
    groups: list[GraphGroup] = Field(default_factory=list)
    #: Things wrong with the graph as a record rather than as logic — a repeated id, a handle
    #: naming a step that was never recorded. Not type errors, so not the kernel's business.
    problems: list[str] = Field(default_factory=list)
    stats: GraphStats = Field(default_factory=GraphStats)

    def node(self, step_id: str) -> GraphNode | None:
        for candidate in self.nodes:
            if candidate.id == step_id:
                return candidate
        return None


class VerifyResult(BaseModel):
    """What one check of a proof graph concluded."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["verified", "failed"]
    diagnostics: list[Diagnostic] = Field(default_factory=list)
    graph: ExtractedGraph | None = None
    #: Which toolchain built this verdict. Recorded because "verified" is a claim about a
    #: toolchain as much as about a file: a graph checked by one has not been checked by
    #: another, and saying so is the difference between a badge and a fact.
    toolchain: str | None = None
    axioms: list[str] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == "verified"

    @property
    def errors(self) -> list[Diagnostic]:
        return [d for d in self.diagnostics if d.severity == "error"]


class ProofGraph(BaseModel):
    model_config = ConfigDict(extra="forbid")

    graph_id: str
    title: str = Field(min_length=1)
    #: The proof graph itself. Everything else on this document is derived from it or is a record of
    #: what checking it said.
    lean_source: str
    status: ProofGraphStatus = "draft"
    #: Which body of work this belongs to, and where the author was standing — the same open
    #: label and the same provenance-not-a-base-path as on a workflow.
    project: str | None = None
    origin_dir: str | None = None
    generated_by: str | None = None
    verification: VerifyResult | None = None
    verified_at: str | None = None
    #: Workflows compiled from this graph, oldest first. Lineage, not a live link: the workflow
    #: is amendable afterwards and this must keep saying what it was made from.
    compiled_to: list[str] = Field(default_factory=list)
    #: Feedback a reviewer left while reading the graph, each note on a step or on the graph
    #: as a whole — the same conversation as review notes on a workflow draft, attached the
    #: same way: on the single read, from their own table, never stored in this document.
    review_notes: list[ReviewNote] = Field(default_factory=list)
    #: Whether the toolchain that produced the stored verdict is still the one installed.
    #: Server-owned and recomputed on every read, never trusted from the stored document — a
    #: graph that goes on displaying "verified" across a toolchain change is exactly the kind
    #: of stale claim this whole feature exists to refuse.
    stale: bool = False
    created_at: str
    updated_at: str

    @property
    def graph(self) -> ExtractedGraph | None:
        return self.verification.graph if self.verification else None

    @property
    def verified(self) -> bool:
        """Checked, and still checked by the toolchain that is actually installed."""
        return self.status == "verified" and not self.stale


class ProofGraphCreate(BaseModel):
    """Request body for ``POST /proof-graphs``."""

    model_config = ConfigDict(extra="forbid")

    graph_id: str | None = None
    title: str = Field(
        min_length=1,
        description="What this proof graph is for, in a few words. Becomes the workflow's title.",
    )
    lean_source: str = Field(
        min_length=1,
        description=(
            "The whole proof graph, as a Lean file written against the ProofGraph prelude. It "
            "must define `graph : GraphM Unit` and end with `#eval emitGraph \"<title>\" graph`."
        ),
    )
    project: str | None = Field(
        default=None,
        description=(
            "Short label for the body of work this belongs to. Match a label already in use "
            "rather than inventing a variant."
        ),
    )
    origin_dir: str | None = Field(
        default=None,
        description="Absolute path of the directory you are working in, if you know it.",
    )
    generated_by: str | None = None


class ProofGraphRevise(BaseModel):
    """Request body for ``PUT /proof-graphs/{graph_id}``: replace the source.

    Every revision drops the graph back to ``draft``. A verdict belongs to the text that was
    checked, and carrying one across an edit would let a changed graph wear the badge its
    previous version earned — which is the same lie as letting a toolchain change go unnoticed,
    arriving by a different road.
    """

    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=1)
    lean_source: str = Field(min_length=1)
    reason: str | None = None


class ProofGraphLabel(BaseModel):
    """Body for ``PATCH /proof-graphs/{graph_id}``: correcting the record, not the graph.

    The same request :class:`WorkflowLabel` is for a workflow, for the same reasons — and
    with one more thing at stake here. Revision is deliberately the only door that touches
    the source, and it drops the graph back to ``draft`` because a verdict belongs to the
    text that was checked. The title is not part of that text: what was proven about a graph
    called one thing is exactly as proven when it is called another, so a rename must ride a
    request of its own or every correction of a name would cost a verification run.

    ``null`` clears a label and an omitted field is left alone, told apart by
    ``model_fields_set``. The title alone cannot be cleared — a graph without one is not a
    record of anything — so blank is refused rather than read as "remove".
    """

    model_config = ConfigDict(extra="forbid")

    title: str | None = None
    project: str | None = None
    origin_dir: str | None = None


class ProofGraphCompile(BaseModel):
    """Request body for ``POST /proof-graphs/{graph_id}/workflows``: lower a verified graph.

    The fields are the ones a workflow carries and a proof graph does not settle: a title
    it should be filed under if not the graph's own, and the labels that say where it belongs.
    """

    model_config = ConfigDict(extra="forbid")

    workflow_id: str | None = None
    title: str | None = None
    project: str | None = None
    origin_dir: str | None = None
