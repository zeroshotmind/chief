import ChiefPlan.Contract
import ChiefPlan.Alg
import ChiefPlan.Graph
import ChiefPlan.Emit

/-!
# ChiefPlan

The vocabulary a Chief plan is written against. Every plan starts:

```lean
import ChiefPlan
open ChiefPlan
```

Both lines. Everything here lives in the `ChiefPlan` namespace, so an import on its own leaves
every name below unresolved.

A plan is an ordinary Lean file. Artifact types are `structure`s, conditions on them are
`Contract`s, steps are functions returning `PlanM (Ref …)`, and the plan is a `do` block
composing them. If it compiles, every step's demands are met by what feeds it — proven for all
values, not sampled. If it runs, the graph it describes can be read off it and handed to Chief.

## The shape a step must have

This is the one rule that is not obvious from the vocabulary, and the one whose failure is
hardest to read. **Write each step as a `def` whose parameters are the handles it consumes, and
put `use` at the call site in `plan` — never inside the step's own `inputs` list.**

```lean
-- Right: the parameter type is what fixes the demand.
def fitModel (d : Ref Dataset enoughToFit) : PlanM (Ref Model accurate) :=
  task "fit_model" "Train the classifier." accurate (inputs := [input "dataset" d])

def plan : PlanM Unit := do
  let ds ← buildDataset
  let _ ← fitModel (use ds)     -- `use` here, where the demand is known
  pure ()
```

```lean
-- Wrong: nothing tells `use` what it is weakening *to*.
task "fit_model" "Train the classifier." accurate
  (inputs := [input "dataset" (use ds)])
```

`use` works out what to prove by unifying with the contract the consuming step demands. Written
inline in `inputs` there is no such type to unify against, so the goal is stated against a
metavariable and cannot be discharged. The error you get names neither `use` nor the fix — it
shows an unreduced `match` on a metavariable — so it is worth getting this right from the
start.

`plan` must be called exactly that, must have type `PlanM Unit`, and therefore ends with
`pure ()`: every step returns a handle, so the `do` block's last line would otherwise give it
the wrong type.

## Everything you get

**Artifacts and conditions** (`ChiefPlan.Contract`)

* `ArtifactType α` — a class carrying the name your artifact type is known by outside Lean.
  One instance per structure: `instance : ArtifactType Dataset := ⟨"Dataset"⟩`.
* `Contract α` — what is known about an artifact. Either `Contract.any`, which claims nothing,
  or `.refine pred shown counter rejects`, which claims `pred` and must name a value it
  rejects. **Bind contracts with `abbrev`, never `def`.**
* `Ref α c` — a handle to the artifact a step will produce, indexed by what is known about it.
* `use r` — feed a handle to a step demanding less than was promised. Every edge goes through
  it; the proof is found for you.
* `plan_entails` — the tactic that finds it. Read its docstring for what it covers.

**Steps and the graph** (`ChiefPlan.Graph`)

* `PlanM` — the monad a plan is written in.
* `task id goal out (harness := "claude") (criteria := []) (inputs := []) (produces := "out")`
  — record a step and return a handle to what it produces. `out` is the contract this step
  *promises*; `produces` names the output port, and is worth setting to something meaningful
  (`"ledger"`, `"model"`) since it is what the artifact is called wherever it is shown.
  Everything after `out` is a named optional argument — pass them by name, not by position.
* `group` on either — which part of the work a step belongs to, e.g. `(group := "Encoder")`.
  Optional, and worth setting only on a plan big enough that its shape is hard to read.
  Groups nest on `/`: `(group := "Encoder/Training")` draws a box inside the `Encoder` one,
  and a group may hold steps of its own as well as sub-groups. Nothing checks it and nothing
  derives from it — naming a phase says nothing about what any step demands.
* `checkpoint id goal (fields := []) (inputs := [])` — record a point where a person decides,
  and return their approval as an artifact.
* `input label r` — name an artifact being fed to a step. This is what creates the edge.
* `Approval` and `granted` — the artifact a checkpoint returns and the contract saying it was
  cleared. A step taking `Ref Approval granted` cannot be written without it, which is what
  makes a gate structural rather than merely an ordering.

**Step algorithms** (`ChiefPlan.Alg`) — optional, and needing `open ChiefPlan Alg`

A task may carry its algorithm: numbered pseudocode rendered from a term Lean checked.
What is checked is scope and shape — every variable is a field of an artifact the step
holds or a name an earlier line bound, and an algorithm that mentions one nothing bound
fails the whole plan. What is *not* checked is the mathematics itself: `Σ` and `log` are
constructors, not claims. External dependencies (LLM calls, search, databases, library
routines) are named oracle terms, collected into a legend, so a reader sees exactly where
the outside world enters.

```lean
def fitModel (d : Ref Dataset enoughToFit) : PlanM (Ref Model accurate) :=
  task "fit_model" "Train the classifier." accurate
    (inputs := [input "dataset" d])
    (algorithm := some do
      let M ← assign "M" (call2 "algo" "xgboost" (x!(d) : Term (Ty.coll Ty.text))
        (Term.param (t := Ty.scalar) "λ") : Term Ty.text)
      let auc ← assign "auc" (call2 "algo" "auc_heldout" M (x!(d) : Term (Ty.coll Ty.text)))
      whenever (Term.ge auc (Term.param "τ")) do
        ret M)
```

* `x!(r, field)` — that field of the artifact `r` refers to; the field must exist, and its
  Lean type fixes the shape (`Nat → scalar`, `String → text`, `List β → coll _`).
  `x!(r)` — the artifact itself, opaque, at whatever shape the use site needs. These are
  the only ways to bring data in; `Term.param "τ"` names a knob, never data.
* `assign`, `gather`, `foreach`, `whenever`, `note`, `ret` — the statement layer. `let m ←
  assign "m" e` binds a variable (same name in string and binder); `gather "R" "g" G fun g
  => e` builds a collection from per-element results, which is how a loop's work becomes a
  value; `foreach` is a loop whose body is lines, whose binder dies with it; `ret` what
  `assign`/`gather` handed you.
* `Σ x ∈ c, body`, `argmax x ∈ c, body`, `filter x ∈ c, cond` — binders over collections.
  Arithmetic on scalars is `+ − * /`, plus `Term.log`, `Term.card`, `Term.cos`,
  `Term.embed`, and comparisons `Term.ge/le/eq/ne`.
* `call1`/`call2`/`call3` — external calls, by arity: tag first (`"llm"`, `"search"`,
  `"db"`, `"algo"`), then the call's name, then arguments. Annotate the result shape
  (`: Term Ty.text`) whenever it feeds something polymorphic — when in doubt, annotate.

**Extraction** (`ChiefPlan.Emit`)

* `emitPlan (title : String) (plan : PlanM Unit) : IO Unit` — print the graph as JSON. Every
  plan file ends with `#eval emitPlan "…" plan`.
* `problems` and `stats` are computed here, when the plan is *run*, not by the kernel: a
  repeated step id or a handle naming a step that was never recorded compiles perfectly and
  shows up only in the extracted JSON.

## Checking a plan

From the repository's `lean/` directory, with an absolute path to your file:

```
lake env lean /path/to/Plan.lean
```

Run it from anywhere else and Lean cannot find `ChiefPlan`, reporting it as an unknown module
prefix — which reads like the plan's import is wrong when only the working directory is.

`Examples/Pipeline.lean` is a complete worked plan; start there.
-/
