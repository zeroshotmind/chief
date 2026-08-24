import ChiefPlan.Contract
import ChiefPlan.Graph
import ChiefPlan.Emit

/-!
# ChiefPlan

The vocabulary a Chief plan is written against. `import ChiefPlan` gets all of it.

A plan is an ordinary Lean file. Artifact types are `structure`s, conditions on them are
`Contract`s, steps are functions returning `PlanM (Ref …)`, and the plan is a `do` block
composing them. If it compiles, every step's demands are met by what feeds it — proven for all
values, not sampled. If it runs, the graph it describes can be read off it and handed to Chief.

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
* `task id goal out (harness := "claude") (criteria := []) (inputs := [])` — record a step and
  return a handle to what it produces.
* `checkpoint id goal (fields := []) (inputs := [])` — record a point where a person decides,
  and return their approval as an artifact.
* `input label r` — name an artifact being fed to a step. This is what creates the edge.
* `Approval` and `granted` — the artifact a checkpoint returns and the contract saying it was
  cleared. A step taking `Ref Approval granted` cannot be written without it, which is what
  makes a gate structural rather than merely an ordering.

**Extraction** (`ChiefPlan.Emit`)

* `emitPlan title plan` — print the graph as JSON. Every plan file ends with
  `#eval emitPlan "…" plan`, and the plan itself must be called `plan`.

## Checking a plan

From the repository's `lean/` directory, with an absolute path to your file:

```
lake env lean /path/to/Plan.lean
```

Run it from anywhere else and Lean cannot find `ChiefPlan`, reporting it as an unknown module
prefix — which reads like the plan's import is wrong when only the working directory is.

`Examples/Pipeline.lean` is a complete worked plan; start there.
-/
