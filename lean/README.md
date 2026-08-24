# Proof graphs: workflows that compile

A Chief workflow says what the steps are and what order they go in. It does not say what each
step needs from the ones before it, so nothing notices when a step is asked to work from
something the previous step never promised to produce. That is the gap this package closes.

A **proof graph** is a workflow graph whose every edge is a theorem, written as a Lean file.
Each step is a function that demands artifacts satisfying some condition and promises one
satisfying another, and the graph is those functions composed. Lean checks that every promise
entails the demand it feeds — for every possible value, not for a sampled one — and refuses to
build a condition that excludes nothing. What survives is compiled into an ordinary workflow
and run like any other.

```
Graph.lean  ──lake env lean──▶  verified proof graph  ──compile──▶  draft workflow  ──▶  approve, run
```

## Using it

Chief builds the library itself the first time it checks anything, so there is no setup step.
To build it by hand — worth doing after changing the prelude:

```
cd lean && lake build
```

Check a proof graph **from this directory, with an absolute path to the file**:

```
cd lean && lake env lean /path/to/Graph.lean
```

Running it from anywhere else makes Lean report `unknown module prefix 'ProofGraph'`, which
reads like the file's import is wrong when only the working directory is. A check takes well
under a second once the library is built.

A proof graph that holds up prints its graph as JSON between `--PROOF-GRAPH-BEGIN--` and
`--PROOF-GRAPH-END--`. One that does not prints nothing but the reason.

From Python, `chief.lean.verify_source` does both halves — it runs the check and reads the
graph back — and `chief.lean.compile_graph` lowers the result into a `WorkflowCreate`.

## Where to look

| File | What is in it |
| --- | --- |
| `ProofGraph.lean` | The whole vocabulary, listed. Start here. |
| `ProofGraph/Contract.lean` | Artifact types, contracts, handles, and the entailment tactic. |
| `ProofGraph/Alg.lean` | Step algorithms: expressions, statements, and the artifact bridge. |
| `ProofGraph/Schema.lean` | Artifact schemas, derived from the structures by reflection. |
| `ProofGraph/Graph.lean` | `task`, `checkpoint`, `input`, and how the graph is recorded. |
| `ProofGraph/Emit.lean` | Extraction, the structural checks, and the statistics block. |
| `Examples/Pipeline.lean` | A complete five-step plan, one step carrying its algorithm. |

## What is actually guaranteed

Being precise about this matters more than the feature sounds like it needs, because "verified"
is a word that invites more weight than it can carry.

**Proven.** That every step's demands follow from what feeds it, for all values. That every
contract in the graph excludes at least one thing — a contract that says nothing cannot be
constructed, because it would need a proof of `¬ True`. That every step downstream of a
checkpoint depends on the approval it returns, so there is no ordering in which a gate is
skipped.

**Not proven, and not claimed.** Whether a step's work is any good. Whether the artifact a
harness actually produces satisfies the contract its step promised — that is checked at run
time, as a criterion, against the one concrete value. Whether the text beside a contract
describes it correctly: the kernel reads the predicate and never the label, so a mislabelled
contract still constrains exactly what its predicate says, but it will display the wrong
thing.

**Checked, but weaker than proven: a step's algorithm.** A task may carry its algorithm as
pseudocode rendered from a term Lean elaborated. What that buys is scope and shape — every
variable is a field of an artifact the step holds or a name an earlier line bound, vector
widths agree, collections bind properly, and text becomes a number only through a named
external call, all of which are collected into a legend so a reader sees where the outside
world enters. What it does not buy is semantics: `Σ` and `log` are constructors, and nobody
proved the mean lies in `[0,1]`. An algorithm whose variables do not hold together fails the
graph rather than rendering; one whose mathematics is wrong renders faithfully, which is the
point — it is there to be reviewed, and the UI deliberately draws it as a listing rather
than in the colour the proven contracts wear.

**Deliberately outside the fragment.** Anything a check at graph time cannot settle — whether a
document reads well, whether a review was thorough, whether the right people were consulted.
Those stay where they already were: conditions a person or a harness answers for. Pushing them
into Lean would mean either a proof nobody can write or a predicate that quietly says nothing.

**And one thing to watch, which nothing here can catch.** A contract is proven non-vacuous —
it must exclude something — but nothing can check that it excludes the *right* things. A step
that really needs eight webhook handlers can demand `≥ 1`, and that contract is perfectly
constructible, verifies cleanly, and is counted as refined. An author writing under
demonstration pressure — reaching for a demand that visibly exercises the weakening machinery
rather than one that states what the step needs — produces exactly this, and the graph looks
just as good as one that does not. This was found by an agent auditing its own first draft,
and it is the failure mode a reader of a verified proof graph should be looking for: not "is anything
proven", which the statistics answer, but "does each demand say what that step actually needs",
which only reading them does.

## Scope

`task` and `checkpoint` only. `loop` and `parallel` want a combinator that takes a body as a
function of the instance parameter and registers construct-plus-body once, and that is a design
in its own right rather than an omission to be patched.

## Updating the toolchain

`lean-toolchain` pins the version, and `chief.lean` records it beside every verdict — a graph
verified by one toolchain has not been verified by another. After changing it, run
`lake build` and re-check `Examples/Pipeline.lean`; the test suite covers the rest.

There are no dependencies, deliberately. Mathlib would bring richer automation and a
multi-minute cold build, and everything `graph_entails` needs is in Lean core.
