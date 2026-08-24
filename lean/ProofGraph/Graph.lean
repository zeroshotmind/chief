import ProofGraph.Contract
import ProofGraph.Alg
import ProofGraph.Schema

/-!
# The plan as a graph

`Contract.lean` makes edges checkable. This file makes them *recoverable*: a plan is written
as a `do` block in `GraphM`, and running that block records the nodes and edges it built.

The single-source property matters more than it looks. There is no second file describing the
graph, no annotation to keep in step with the composition, and so nothing to diff: the thing
Lean type-checks and the thing extraction prints are the same `do` block, read once by the
elaborator and once by the evaluator. A picture drawn from this cannot disagree with the proof
that was checked, because there is only one artifact and one derivation from it.

Dependency edges are not declared. They fall out of data flow — a step that consumes another
step's `Ref` depends on it, and a step that consumes nothing depends on nothing. That is why
a missing edge is not a lint but a type error: there is no way to write a step that reads an
artifact without naming the handle that produces it.

Stage 1 covers `task` and `checkpoint`. `loop` and `parallel` want a combinator that takes a
body as a function of the instance parameter and registers construct-plus-body once, which is
a real design in its own right and is not attempted here.
-/

namespace ProofGraph

/-- One artifact crossing one edge, flattened for extraction.

`source` is the id of the producing step, which is what turns a value dependency into a graph
edge. `contract` is the demanding side's text on an input and the promising side's on an
output — the two differ exactly where a `use` weakened one to the other, and showing both is
how a reader sees what was proven rather than just that something was. -/
structure Port where
  label : String
  source : String
  artifactType : String
  contract : String
  refined : Bool
  /-- The artifact type's fields, where `artifact_schema` derived them, nested where a
  field's own type was derived too. Empty means undeclared, not field-free. -/
  schema : List SchemaField := []
deriving Repr, Inhabited

/-- An artifact fixed before anything runs: a file, a document, a URL the step starts from,
known at graph time rather than produced by an upstream step. No contract rides on it —
nothing upstream promised it into existence — so nothing about it is proven; it is shown,
and the compiled workflow hands it to the harness as an input like any other. -/
structure FixedArtifact where
  label : String
  ref : String
  description : String := ""
deriving Repr, Inhabited

/-- `given "spec" "docs/spec.md" "the product spec"` — name a fixed input. -/
def given (label : String) (ref : String) (description : String := "") : FixedArtifact :=
  { label, ref, description }

/-- A step, in the shape Chief will receive it. -/
structure Node where
  id : String
  /-- `task` or `checkpoint`. -/
  kind : String
  goal : String
  harness : String
  /-- Which part of the work this step belongs to, or empty. Nests on `/`, so
  `"Encoder/Training"` sits inside `"Encoder"`. A label for a reader, never something the
  checking reads: naming a phase says nothing about what any step demands. -/
  group : String
  criteria : List String
  /-- What a checkpoint asks a person for. Empty on a task. -/
  fields : List String
  inputs : List Port
  /-- Inputs fixed before anything runs, shown beside the contracted ones. -/
  fixed : List FixedArtifact
  produces : Option Port
  /-- The step's algorithm, already run to lines and a legend, if the plan gives one. Its
  problems surface with the graph's — a step whose pseudocode names a variable nothing
  bound must fail the plan, not decorate it. -/
  algorithm : Option Alg.AlgRecord
deriving Repr, Inhabited

/-- What a plan accumulates as it is run. -/
structure GraphState where
  nodes : List Node := []
  /-- One line per described group: path, then what that part of the work is for. -/
  groups : List (String × String) := []
deriving Inhabited

/-- The monad a plan is written in: state, and nothing else.

No `IO`, no failure, no nondeterminism — a plan is a description being assembled, and the
narrower this is the less a plan can do besides describe itself. -/
abbrev GraphM := StateM GraphState

/-- Name an artifact being fed to a step.

The label is what the edge is called on the consuming side (`"dataset"`, `"approval"`), and
becomes the key under which Chief records the input. -/
def input {α : Type} [ArtifactType α] [ArtifactSchema α] {c : Contract α}
    (label : String) (r : Ref α c) : Port :=
  { label
    source := r.source
    artifactType := typeName α
    contract := c.shown
    refined := c.refined
    schema := ArtifactSchema.fields (α := α) }

/-- Record a step and return a handle to what it produces.

`out` is the contract the step *promises*. It is an explicit argument rather than an inferred
one because it appears in the result type — `GraphM (Ref β out)` — so writing it is how the
author states the promise, and every later consumer is checked against it. -/
def task {β : Type} [ArtifactType β] [ArtifactSchema β] (id : String) (goal : String)
    (out : Contract β)
    (harness : String := "claude")
    (criteria : List String := [])
    (inputs : List Port := [])
    (produces : String := "out")
    (group : String := "")
    (fixed : List FixedArtifact := [])
    (algorithm : Option (Alg.AlgM Unit) := none) : GraphM (Ref β out) := do
  modify fun s =>
    { s with nodes := s.nodes ++ [{
        id, kind := "task", goal, harness, group, criteria, fields := [], inputs, fixed,
        produces := some {
          label := produces
          source := id
          artifactType := typeName β
          contract := out.shown
          refined := out.refined
          schema := ArtifactSchema.fields (α := β) }
        algorithm := algorithm.map (·.record) }] }
  return ⟨id⟩

/-- What a person hands back when they clear a checkpoint. -/
structure Approval where
  approved : Bool
deriving Repr, Inhabited

instance : ArtifactType Approval := ⟨"Approval"⟩
artifact_schema Approval

/-- A checkpoint that was actually cleared, as opposed to merely reached.

This is the contract that makes a gate structural. A step placed after a checkpoint in
wall-clock order is just a step; a step that takes `Ref Approval granted` as an input cannot
be written without the handle the checkpoint returns, so the dependency is in the type and
there is no plan shape in which the approval is skipped. -/
abbrev granted : Contract Approval :=
  .refine (fun a => a.approved = true) "approved" ⟨false⟩ (by decide)

/-- Record a point where a person decides, and return their approval as an artifact.

Lean has no opinion on the decision itself — whether a reviewer *should* approve is not the
kind of claim a proof assistant makes, and pretending otherwise is exactly the overreach this
design avoids. What is checked is that everything downstream of the decision depends on it. -/
def checkpoint (id : String) (goal : String)
    (fields : List String := [])
    (inputs : List Port := [])
    (group : String := "")
    (fixed : List FixedArtifact := []) : GraphM (Ref Approval granted) := do
  modify fun s =>
    { s with nodes := s.nodes ++ [{
        id, kind := "checkpoint", goal, harness := "human", group, criteria := [], fields,
        inputs, fixed,
        produces := some {
          label := "approval"
          source := id
          artifactType := "Approval"
          contract := granted.shown
          refined := granted.refined
          schema := ArtifactSchema.fields (α := Approval) }
        algorithm := none }] }
  return ⟨id⟩

/-- Say what a group is for, in a line.

Optional, like the grouping itself, and checked only structurally: describing a group no
step belongs to is a problem at extraction, since a description of nothing is exactly the
kind of stale text this design refuses to display. Nesting works by path —
`describeGroup "Encoder" "…"` describes the box that `group := "Encoder/Training"` steps
sit inside. -/
def describeGroup (path : String) (description : String) : GraphM Unit :=
  modify fun s => { s with groups := s.groups ++ [(path, description)] }

/-- Run a plan and hand back the nodes it recorded, in the order they were written. -/
def GraphM.nodes (p : GraphM Unit) : List Node := (p.run {}).2.nodes

/-- Run a plan and hand back everything it recorded. -/
def GraphM.final (p : GraphM Unit) : GraphState := (p.run {}).2

end ProofGraph
