"""Verifying a plan's logic, and lowering it into a workflow.

The tests split in two. Everything that does not need Lean — the source lint, the diagnostic
parser, the compiler — runs everywhere. The rest is skipped where there is no toolchain, which
is the honest behaviour: a machine that cannot check a plan has not found one unsound.

The negative cases are the point of the file. A verifier that only ever says yes is worth
nothing, so each guarantee the design claims has a test that breaks it: an entailment that does
not hold, a contract that excludes nothing, a plan whose contracts say nothing at all.
"""

from __future__ import annotations

import pytest

from chief.lean import attribute_diagnostics, available, compile_plan, verify_source
from chief.lean.verify import (
    LeanUnavailable,
    ensure_built,
    is_built,
    lint_source,
    package_dir,
    parse_output,
)
from chief.models import PlanGraph

needs_lean = pytest.mark.skipif(not available(), reason="no Lean toolchain on this machine")


def example_source() -> str:
    package = package_dir()
    assert package is not None
    return (package / "Examples" / "Pipeline.lean").read_text(encoding="utf-8")


def errors(result) -> list[str]:
    return [d.message for d in result.diagnostics if d.severity == "error"]


# A whole plan in as few lines as a plan can be written, for the cases that need a file rather
# than an edit of the worked example.
MINIMAL = """import ChiefPlan
open ChiefPlan

structure Doc where
  words : Nat
deriving Repr

instance : ArtifactType Doc := ⟨"Doc"⟩

abbrev {first} : Contract Doc :=
  {first_body}
abbrev {second} : Contract Doc :=
  {second_body}

def writeIt : PlanM (Ref Doc {first}) :=
  task "{write_id}" "Write the thing." {first}

def reviseIt (d : Ref Doc {second}) : PlanM (Ref Doc {second}) :=
  task "{revise_id}" "Revise the thing." {second} (inputs := [input "draft" d])

def plan : PlanM Unit := do
  let d ← writeIt
  let _ ← reviseIt (use d)
  pure ()

#eval emitPlan "Minimal" plan
"""


def minimal(
    *,
    first_body: str = '.refine (fun d => d.words ≥ 500) "words ≥ 500" ⟨0⟩ (by decide)',
    second_body: str = '.refine (fun d => d.words ≥ 100) "words ≥ 100" ⟨0⟩ (by decide)',
    write_id: str = "write",
    revise_id: str = "revise",
) -> str:
    return MINIMAL.format(
        first="strong",
        second="weak",
        first_body=first_body,
        second_body=second_body,
        write_id=write_id,
        revise_id=revise_id,
    )


# --------------------------------------------------------------------------- verification


@needs_lean
def test_the_worked_example_verifies() -> None:
    result = verify_source(example_source())

    assert result.status == "verified", errors(result)
    assert result.graph is not None
    assert result.graph.stats.nodes == 5
    # Every contract in it constrains something; nothing was made to compile by claiming
    # nothing.
    assert result.graph.stats.contracts_any == 0
    assert result.graph.stats.contracts_refined == result.graph.stats.contracts_total
    assert not result.graph.stats.vacuous
    # `fit_model` carries its algorithm; the graph counts it.
    assert result.graph.stats.algorithms == 1
    assert result.toolchain


@needs_lean
def test_only_the_axioms_of_ordinary_logic_are_used() -> None:
    result = verify_source(example_source())

    assert result.status == "verified"
    assert set(result.axioms) <= {"propext", "Classical.choice", "Quot.sound"}


@needs_lean
def test_a_promise_that_no_longer_entails_its_demand_is_refused() -> None:
    """The plan's own claim, broken: the model is fitted to a lower bar than review demands."""
    weakened = example_source().replace(
        '(fun m => m.auc ≥ 80) "auc ≥ 80"', '(fun m => m.auc ≥ 70) "auc ≥ 70"'
    )

    result = verify_source(weakened)

    assert result.status == "failed"
    # The message a planner repairs from names both sides of the entailment that failed.
    goals = "\n".join(errors(result))
    assert "auc ≥ 70" in goals and "auc ≥ 75" in goals
    # And it is drawn on the step whose demand went unmet, not on the one that was edited.
    placed = attribute_diagnostics(weakened, result.graph, result.diagnostics)
    assert "review" in {d.step_id for d in placed if d.severity == "error"}


@needs_lean
def test_a_contract_that_excludes_nothing_cannot_be_built() -> None:
    """The guard against making a failing edge compile by promising nothing."""
    vacuous = example_source().replace(
        '.refine (fun m => m.auc ≥ 80) "auc ≥ 80" ⟨0⟩ (by decide)',
        '.refine (fun _ => True) "anything" ⟨0⟩ (by decide)',
    )

    result = verify_source(vacuous)

    assert result.status == "failed"
    assert any("¬True" in message or "False" in message for message in errors(result))


@needs_lean
def test_a_plan_whose_contracts_all_say_nothing_is_refused() -> None:
    """`any` is honest and allowed, but a plan made only of it has verified nothing."""
    source = minimal(first_body=".any", second_body=".any")

    result = verify_source(source)

    assert result.status == "failed"
    assert any("every contract in this plan is `any`" in m for m in errors(result))
    assert result.graph is not None
    assert result.graph.stats.vacuous


@needs_lean
def test_a_weakening_edge_between_two_real_contracts_is_accepted() -> None:
    result = verify_source(minimal())

    assert result.status == "verified", errors(result)
    assert result.graph is not None
    assert result.graph.stats.contracts_any == 0


@needs_lean
def test_a_repeated_step_id_is_reported() -> None:
    """Not ill-typed, so not the kernel's business — caught at extraction instead."""
    result = verify_source(minimal(write_id="same", revise_id="same"))

    assert result.status == "failed"
    assert any("duplicate step id 'same'" in message for message in errors(result))


@needs_lean
def test_a_plan_that_cannot_be_checked_is_not_a_plan_that_failed() -> None:
    """`sorry` is refused before Lean is started, and the message says which line."""
    result = verify_source(example_source().replace("(by decide)", "(by sorry)", 1))

    assert result.status == "failed"
    assert any("contains a `sorry`" in message for message in errors(result))


# --------------------------------------------------------------------------- source lint


def test_lint_refuses_the_constructs_that_would_let_extraction_lie() -> None:
    for snippet, expected in [
        ("axiom cheat : False", "declares an axiom"),
        ("unsafe def loop : Nat := loop", "unsafe"),
        ("@[implemented_by other] def x := 1", "implemented_by"),
        ("example : True := by native_decide", "native_decide"),
        ("example : True := by sorry", "sorry"),
    ]:
        found = lint_source(f"import ChiefPlan\ndef plan := 1\nemitPlan\n{snippet}\n")
        assert any(expected in d.message for d in found if d.severity == "error"), snippet


def test_lint_does_not_fire_on_words_inside_goals_and_comments() -> None:
    """A step whose goal mentions an axiom is a step, not a proof."""
    source = (
        "import ChiefPlan\n"
        "def plan := 1\n"
        "emitPlan\n"
        '-- we are sorry about the axiom here\n'
        'def goal := "say sorry to the reviewer and state the axiom"\n'
    )

    assert [d for d in lint_source(source) if d.severity == "error"] == []


def test_lint_names_a_def_bound_contract() -> None:
    """The most common way a correct plan fails to compile, called out by name."""
    source = "import ChiefPlan\ndef plan := 1\nemitPlan\ndef trainable : Contract Dataset := x\n"

    warnings = [d for d in lint_source(source) if d.severity == "warning"]

    assert len(warnings) == 1
    assert "trainable" in warnings[0].message
    assert "abbrev" in warnings[0].message


def test_lint_requires_a_plan_and_an_emit() -> None:
    found = [d.message for d in lint_source("import ChiefPlan\n") if d.severity == "error"]

    assert any("must define `plan : PlanM Unit`" in message for message in found)
    assert any("emitPlan" in message for message in found)


# --------------------------------------------------------------------------- output parsing


def test_parse_output_keeps_a_wrapped_message_in_one_piece() -> None:
    stream = (
        "/tmp/Plan.lean:12:4: error: unsolved goals\n"
        "hx : x.auc ≥ 70\n"
        "⊢ x.auc ≥ 75\n"
        "/tmp/Plan.lean:20:0: warning: unused variable\n"
    )

    diagnostics, payload, axioms = parse_output(stream, source_name="/tmp/Plan.lean")

    assert payload is None and axioms == []
    assert len(diagnostics) == 2
    assert diagnostics[0].line == 12
    assert "⊢ x.auc ≥ 75" in diagnostics[0].message
    assert diagnostics[1].severity == "warning"


def test_parse_output_reads_the_payload_and_the_axioms() -> None:
    stream = (
        "--CHIEF-PLAN-BEGIN--\n"
        '{"schema":"chief.plan/v1"}\n'
        "--CHIEF-PLAN-END--\n"
        "/tmp/Plan.lean:30:0: info: 'plan' depends on axioms: [propext, Quot.sound]\n"
    )

    diagnostics, payload, axioms = parse_output(stream, source_name="/tmp/Plan.lean")

    assert payload == '{"schema":"chief.plan/v1"}'
    assert axioms == ["propext", "Quot.sound"]
    assert diagnostics == []


# --------------------------------------------------------------------------- compilation


def graph(**overrides) -> PlanGraph:
    base = {
        "schema": "chief.plan/v1",
        "title": "Fraud model refresh",
        "nodes": [
            {
                "id": "fit",
                "type": "task",
                "goal": "Fit the classifier.",
                "harness": "claude",
                "criteria": ["AUC recorded"],
                "fields": [],
                "depends_on": [],
                "inputs": [],
                "produces": {
                    "label": "out",
                    "source": "fit",
                    "artifact_type": "Model",
                    "contract": "auc ≥ 80",
                    "refined": True,
                },
            },
            {
                "id": "review",
                "type": "checkpoint",
                "goal": "Decide whether it ships.",
                "harness": "human",
                "criteria": [],
                "fields": ["decision"],
                "depends_on": ["fit"],
                "inputs": [
                    {
                        "label": "model",
                        "source": "fit",
                        "artifact_type": "Model",
                        "contract": "auc ≥ 75",
                        "refined": True,
                    }
                ],
                "produces": None,
            },
        ],
        "problems": [],
        "stats": {
            "nodes": 2,
            "edges": 1,
            "contracts_total": 3,
            "contracts_refined": 3,
            "contracts_any": 0,
        },
    }
    base.update(overrides)
    return PlanGraph.model_validate(base)


def test_compile_turns_data_flow_into_dependencies_and_inputs() -> None:
    workflow = compile_plan(graph())

    review = next(step for step in workflow.steps if step.id == "review")
    assert review.depends_on == ["fit"]
    # The input says what was proven about the artifact, and why the dependency exists.
    assert review.inputs["model"] == {
        "artifact_type": "Model",
        "contract": "auc ≥ 75",
        "from_step": "fit",
        "proven": True,
    }


def test_compile_restates_a_promise_as_a_criterion() -> None:
    """Proven at plan time for all values; confirmed at run time for the one produced."""
    workflow = compile_plan(graph())

    fit = next(step for step in workflow.steps if step.id == "fit")
    texts = [criterion.text for criterion in fit.criteria]
    assert "AUC recorded" in texts
    assert any("auc ≥ 80" in text and "Model" in text for text in texts)


def test_compile_leaves_criteria_off_a_checkpoint() -> None:
    """A person's decision is not a condition a harness answers for."""
    workflow = compile_plan(graph())

    review = next(step for step in workflow.steps if step.id == "review")
    assert review.criteria == []
    assert [field.name for field in review.fields or []] == ["decision"]


def test_compile_takes_its_title_from_the_plan_unless_told_otherwise() -> None:
    assert compile_plan(graph()).title == "Fraud model refresh"
    assert compile_plan(graph(), title="Something else").title == "Something else"


def test_a_compiled_plan_is_accepted_by_the_ordinary_workflow_rules(api) -> None:
    """Nothing downstream can tell it from a hand-written plan, which is the point."""
    workflow = compile_plan(graph(), project="chief")

    response = api.create_workflow(
        [step.model_dump(exclude_none=True) for step in workflow.steps],
        title=workflow.title,
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert [step["id"] for step in body["steps"]] == ["fit", "review"]


@needs_lean
def test_the_worked_example_compiles_into_a_workflow_chief_accepts(api) -> None:
    result = verify_source(example_source())
    assert result.graph is not None

    workflow = compile_plan(result.graph, project="chief", generated_by="lean")
    response = api.create_workflow(
        [step.model_dump(exclude_none=True) for step in workflow.steps], title=workflow.title
    )

    assert response.status_code == 201, response.text
    steps = {step["id"]: step for step in response.json()["steps"]}
    # The gate is structural: deploy reads the approval, so it depends on the checkpoint.
    assert sorted(steps["deploy"]["depends_on"]) == ["fit_model", "review"]
    assert steps["review"]["type"] == "checkpoint"


# --------------------------------------------------------------------------- attribution


def test_a_failure_is_placed_even_when_no_graph_came_back() -> None:
    """The case attribution exists for: a plan that did not compile printed nothing."""
    source = (
        "import ChiefPlan\n"
        "open ChiefPlan\n"
        "\n"
        "def fitModel (d : Ref Dataset trainable) : PlanM (Ref Model accurate) :=\n"
        '  task "fit_model" "Fit it." accurate\n'
        "\n"
        "def plan : PlanM Unit := do\n"
        "  let m ← fitModel (use ds)\n"
        "  pure ()\n"
    )
    diagnostics, _, _ = parse_output(
        "/tmp/Plan.lean:8:20: error: unsolved goals\n", source_name="/tmp/Plan.lean"
    )

    placed = attribute_diagnostics(source, None, diagnostics)

    assert placed[0].step_id == "fit_model"


# --------------------------------------------------------------------------- message quality


DEF_BOUND = MINIMAL.replace("abbrev {first}", "def {first}")


@needs_lean
def test_a_def_bound_contract_says_why_the_edge_could_not_be_checked() -> None:
    """Lean's own text points at a contract that is usually correct. This one does not."""
    source = DEF_BOUND.format(
        first="strong",
        second="weak",
        first_body='.refine (fun d => d.words ≥ 500) "words ≥ 500" ⟨0⟩ (by decide)',
        second_body='.refine (fun d => d.words ≥ 100) "words ≥ 100" ⟨0⟩ (by decide)',
        write_id="write",
        revise_id="revise",
    )

    result = verify_source(source)

    assert result.status == "failed"
    blamed = "\n".join(errors(result))
    assert "'strong' is bound with `def`" in blamed
    assert "Change it to `abbrev`" in blamed
    # And the unreadable match term is collapsed to the name it could not see through.
    assert "motive := Contract" not in blamed
    assert "‹strong — opaque›" in blamed


@needs_lean
def test_a_failed_edge_is_reported_once_not_twice() -> None:
    """The synthesis message names an internal parameter and says nothing a reader can use."""
    weakened = example_source().replace(
        '(fun m => m.auc ≥ 80) "auc ≥ 80"', '(fun m => m.auc ≥ 70) "auc ≥ 70"'
    )

    result = verify_source(weakened)

    assert result.status == "failed"
    assert not any("could not synthesize default value" in m for m in errors(result))
    assert len(errors(result)) == 1
    assert "auc ≥ 75" in errors(result)[0]


# --------------------------------------------------------------------------- the prelude


@needs_lean
def test_the_prelude_is_built_before_anything_is_checked() -> None:
    """A checkout has no build, and an unbuilt prelude made every plan look broken.

    Lean reports an unknown module prefix, which reads as "this plan does not hold up" when
    what is true is "nothing has been compiled yet" — the exact confusion the rest of this
    module exists to prevent, arriving through the back door. Verifying anything at all is
    enough to leave it built.
    """
    package = package_dir()
    assert package is not None

    verify_source(example_source())

    assert is_built(package)
    # And a second call is a no-op rather than a rebuild.
    ensure_built(package)


def test_a_prelude_that_cannot_be_built_is_unavailable_not_unsound(tmp_path) -> None:
    (tmp_path / "lakefile.toml").write_text("name = \"broken\"\n", encoding="utf-8")

    with pytest.raises(LeanUnavailable):
        ensure_built(tmp_path, timeout=120)


@needs_lean
def test_a_graph_this_server_cannot_read_is_not_a_plan_that_failed(monkeypatch) -> None:
    """The prelude prints the graph, never the author — so an unreadable one is version skew.

    Reporting it as a verdict tells someone their plan is broken when the plan is fine and the
    two halves of the checker are out of step. The same distinction the whole module keeps:
    "could not look" is not "looked and found it wanting".
    """
    import chief.lean.verify as verify

    class Refuses:
        @staticmethod
        def model_validate_json(_payload: str):
            raise ValueError("nodes.0.group: extra inputs are not permitted")

    monkeypatch.setattr(verify, "PlanGraph", Refuses)

    with pytest.raises(LeanUnavailable) as raised:
        verify_source(example_source())

    assert "out of step" in str(raised.value)


# ---------------------------------------------------------------------------- algorithms


# The worked example's `fit_model` carries an algorithm; the assertions on it live with the
# example tests above the fold, so these focus on the guarantees: an algorithm's variables
# must hold together, its externals are derived rather than declared, and a checkpoint never
# carries one.
ALG_STEP = """
    (algorithm := some do
      let m ← assign "m" (call1 "algo" "count_words" (x!(d) : Term (Ty.coll Ty.text))
        : Term Ty.scalar)
      let ok ← assign "ok" (Term.ge m (Term.param "θ"))
      ret ok)"""


def with_algorithm(body: str = ALG_STEP) -> str:
    source = minimal().replace("open ChiefPlan", "open ChiefPlan Alg")
    return source.replace(
        'task "revise" "Revise the thing." weak (inputs := [input "draft" d])',
        'task "revise" "Revise the thing." weak (inputs := [input "draft" d])' + body,
    )


@needs_lean
def test_a_step_algorithm_is_rendered_and_its_externals_derived() -> None:
    result = verify_source(with_algorithm())

    assert result.status == "verified", errors(result)
    assert result.graph is not None
    node = result.graph.node("revise")
    assert node is not None and node.algorithm is not None
    texts = [line.text for line in node.algorithm.lines]
    assert texts == [
        "m ← count_words(write)",
        "ok ← m ≥ θ",
        "return ok",
    ]
    # The external call was collected off the term, not declared beside it.
    assert [(e.tag, e.fn) for e in node.algorithm.externals] == [("algo", "count_words")]
    assert result.graph.stats.algorithms == 1
    # Steps without one say so rather than carrying something empty.
    write = result.graph.node("write")
    assert write is not None and write.algorithm is None


@needs_lean
def test_an_algorithm_variable_nothing_bound_fails_the_plan() -> None:
    leaky = """
    (algorithm := some do
      foreach "w" (x!(d) : Term (Ty.coll Ty.text)) fun _ => do
        let _ ← assign "n" (Term.lit "1")
        pure ()
      ret (Term.bound (t := Ty.scalar) "n"))"""

    result = verify_source(with_algorithm(leaky))

    assert result.status == "failed"
    assert any(
        "algorithm" in message and "'n'" in message for message in errors(result)
    ), errors(result)


@needs_lean
def test_an_algorithm_cannot_reach_an_artifact_the_step_does_not_hold() -> None:
    """The bridge is the claim: x! needs the Ref, and the Ref is a parameter the step was
    given. An algorithm on `revise` naming a handle that is not among its parameters is not
    a lint — it does not elaborate."""
    grabby = ALG_STEP.replace("x!(d)", "x!(nothere)")

    result = verify_source(with_algorithm(grabby))

    assert result.status == "failed"
    assert any("nothere" in message for message in errors(result)), errors(result)


@needs_lean
def test_a_group_description_travels_and_a_dangling_one_is_refused() -> None:
    grouped = minimal().replace(
        'task "revise" "Revise the thing." weak (inputs := [input "draft" d])',
        'task "revise" "Revise the thing." weak (inputs := [input "draft" d]) '
        '(group := "Polish")',
    ).replace(
        "def plan : PlanM Unit := do",
        "def plan : PlanM Unit := do\n"
        '  describeGroup "Polish" "Everything after the first draft."',
    )

    result = verify_source(grouped)

    assert result.status == "verified", errors(result)
    assert result.graph is not None
    assert [(g.path, g.description) for g in result.graph.groups] == [
        ("Polish", "Everything after the first draft.")
    ]

    # A description of a group no step belongs to is stale text about nothing, refused
    # rather than displayed.
    dangling = verify_source(
        minimal().replace(
            "def plan : PlanM Unit := do",
            "def plan : PlanM Unit := do\n  describeGroup \"Ghost\" \"A part nobody is in.\"",
        )
    )
    assert dangling.status == "failed"
    assert any("Ghost" in message for message in errors(dangling)), errors(dangling)


@needs_lean
def test_a_derived_schema_travels_on_both_ends_of_an_edge() -> None:
    """`artifact_schema` reads the structure by reflection, so the emitted fields are the
    structure's own — and they appear on the input side and the output side alike, since
    both name the same type."""
    result = verify_source(example_source())

    assert result.status == "verified", errors(result)
    assert result.graph is not None
    fit = result.graph.node("fit_model")
    assert fit is not None
    assert [(f.name, f.type) for f in fit.inputs[0].schema_] == [
        ("rows", "Nat"),
        ("labelled", "Bool"),
    ]
    assert fit.produces is not None
    assert [(f.name, f.type) for f in fit.produces.schema_] == [("auc", "Nat")]
    # A plan that derives no schema shows none — undeclared, not field-free.
    bare = verify_source(minimal())
    assert bare.status == "verified", errors(bare)
    assert bare.graph is not None
    write = bare.graph.node("write")
    assert write is not None and write.produces is not None
    assert write.produces.schema_ == []


def test_compile_restates_a_schema_as_a_validation_criterion() -> None:
    from chief.models import PlanField

    node = graph().nodes[0]
    node.produces.schema_ = [PlanField(name="rows", type="Nat")]
    compiled = compile_plan(graph(nodes=[node]))

    step = compiled.steps[0]
    texts = [c.text for c in step.criteria]
    assert any("document with fields: rows (Nat)" in t for t in texts), texts
    (output,) = step.outputs.values()
    assert output["schema"] == {"rows": "Nat"}
