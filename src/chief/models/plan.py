"""Plan — a candidate plan whose logic can be machine-checked (extension).

A template is a plan shape you keep; a workflow is the plan you are running this time. A
*plan* is neither: it is a plan nobody has agreed to yet, written so that a proof assistant can
say whether it hangs together. Its steps declare what they need from the ones before them, and
checking it establishes that every one of those demands is met by what feeds it — before a
person is asked to approve anything, and before a harness executes a step whose inputs were
never going to exist.

The canonical form of a plan is its Lean source. The graph below is *derived* from that source
by running it, not written alongside it, which is the property the whole design rests on: there
is no second description to drift out of step with what was proven. Everything here that is not
``lean_source`` is a record of what checking it concluded.

A verified plan compiles into an ordinary :class:`WorkflowDefinition` and from there is
approved, run and amended like any other. Nothing downstream knows it came from here.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

#: A plan is ``draft`` until it has been checked, and then says how that went. Deliberately
#: not a workflow status: a plan is never "approved" or "archived", it is compiled into
#: something that can be.
PlanStatus = Literal["draft", "verified", "failed"]

#: The three axioms of Lean's standard logic. A proof depending on nothing outside this set is
#: one the kernel checked; anything else is either a hole or a claim discharged by running
#: compiled code.
SOUND_AXIOMS = frozenset({"propext", "Classical.choice", "Quot.sound"})


class Diagnostic(BaseModel):
    """One thing the checker said, positioned in the plan's own source."""

    model_config = ConfigDict(extra="forbid")

    severity: Literal["error", "warning", "info"]
    line: int | None = None
    column: int | None = None
    message: str
    #: Which step this is about, where that could be worked out. Best-effort and often absent:
    #: it decides which node a failure is drawn on, and nothing else.
    step_id: str | None = None


class PlanPort(BaseModel):
    """One artifact crossing one edge.

    ``contract`` is the demanding side's condition on an input and the promising side's on an
    output. The two differ exactly where the plan weakened one to the other, and that
    difference is what was proven — showing both is how a reader sees the claim rather than
    just the fact that a claim was made.
    """

    model_config = ConfigDict(extra="forbid")

    label: str
    #: The step that produces this artifact. What turns a value dependency into a graph edge.
    source: str
    artifact_type: str
    contract: str
    #: False for a contract that constrains nothing. Per-port rather than only in the totals,
    #: because a plan of nine real contracts and one empty one on the edge that matters must
    #: be able to say *which* edge.
    refined: bool


class PlanNode(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    type: Literal["task", "checkpoint"]
    goal: str
    harness: str
    #: Which part of the work this step belongs to, if the plan says. A label for a reader.
    group: str | None = None
    criteria: list[str] = Field(default_factory=list)
    fields: list[str] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)
    inputs: list[PlanPort] = Field(default_factory=list)
    produces: PlanPort | None = None


class PlanStats(BaseModel):
    """How much the plan actually claims.

    ``contracts_any`` against ``contracts_refined`` is the measure that matters. A plan whose
    contracts are all unconstrained checks cleanly and has been verified to say nothing; it
    must never present the way one full of real conditions does.
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
    """The graph read back out of a plan by running it."""

    # ``schema`` would shadow a BaseModel attribute, so the field is named around it and
    # aliased back; populating by either name keeps callers from having to know that.
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    schema_: str = Field(alias="schema")
    title: str
    nodes: list[PlanNode] = Field(default_factory=list)
    #: Things wrong with the graph as a record rather than as logic — a repeated id, a handle
    #: naming a step that was never recorded. Not type errors, so not the kernel's business.
    problems: list[str] = Field(default_factory=list)
    stats: PlanStats = Field(default_factory=PlanStats)

    def node(self, step_id: str) -> PlanNode | None:
        for candidate in self.nodes:
            if candidate.id == step_id:
                return candidate
        return None


class VerifyResult(BaseModel):
    """What one check of a plan concluded."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["verified", "failed"]
    diagnostics: list[Diagnostic] = Field(default_factory=list)
    graph: PlanGraph | None = None
    #: Which toolchain built this verdict. Recorded because "verified" is a claim about a
    #: toolchain as much as about a file: a plan checked by one has not been checked by
    #: another, and saying so is the difference between a badge and a fact.
    toolchain: str | None = None
    axioms: list[str] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == "verified"

    @property
    def errors(self) -> list[Diagnostic]:
        return [d for d in self.diagnostics if d.severity == "error"]


class Plan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan_id: str
    title: str = Field(min_length=1)
    #: The plan itself. Everything else on this document is derived from it or is a record of
    #: what checking it said.
    lean_source: str
    status: PlanStatus = "draft"
    #: Which body of work this belongs to, and where the author was standing — the same open
    #: label and the same provenance-not-a-base-path as on a workflow.
    project: str | None = None
    origin_dir: str | None = None
    generated_by: str | None = None
    verification: VerifyResult | None = None
    verified_at: str | None = None
    #: Workflows compiled from this plan, oldest first. Lineage, not a live link: the workflow
    #: is amendable afterwards and this must keep saying what it was made from.
    compiled_to: list[str] = Field(default_factory=list)
    #: Whether the toolchain that produced the stored verdict is still the one installed.
    #: Server-owned and recomputed on every read, never trusted from the stored document — a
    #: plan that goes on displaying "verified" across a toolchain change is exactly the kind of
    #: stale claim this whole feature exists to refuse.
    stale: bool = False
    created_at: str
    updated_at: str

    @property
    def graph(self) -> PlanGraph | None:
        return self.verification.graph if self.verification else None

    @property
    def verified(self) -> bool:
        """Checked, and still checked by the toolchain that is actually installed."""
        return self.status == "verified" and not self.stale


class PlanCreate(BaseModel):
    """Request body for ``POST /plans``."""

    model_config = ConfigDict(extra="forbid")

    plan_id: str | None = None
    title: str = Field(
        min_length=1,
        description="What this plan is for, in a few words. Becomes the workflow's title.",
    )
    lean_source: str = Field(
        min_length=1,
        description=(
            "The whole plan, as a Lean file written against the ChiefPlan prelude. It must "
            "define `plan : PlanM Unit` and end with `#eval emitPlan \"<title>\" plan`."
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


class PlanRevise(BaseModel):
    """Request body for ``PUT /plans/{plan_id}``: replace the source.

    Every revision drops the plan back to ``draft``. A verdict belongs to the text that was
    checked, and carrying one across an edit would let a changed plan wear the badge its
    previous version earned — which is the same lie as letting a toolchain change go unnoticed,
    arriving by a different road.
    """

    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=1)
    lean_source: str = Field(min_length=1)
    reason: str | None = None


class PlanCompile(BaseModel):
    """Request body for ``POST /plans/{plan_id}/workflows``: lower a verified plan.

    The fields are the ones a workflow carries and a plan does not settle: a title it should be
    filed under if not the plan's own, and the labels that say where it belongs.
    """

    model_config = ConfigDict(extra="forbid")

    workflow_id: str | None = None
    title: str | None = None
    project: str | None = None
    origin_dir: str | None = None
