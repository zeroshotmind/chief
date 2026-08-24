import ProofGraph.Graph

/-!
# Extraction

Printing the graph the plan built, as JSON, on stdout, between markers the verifier looks for.

Two kinds of checking meet here. The kernel has already settled everything about *contracts*
by the time this runs — a file that reaches extraction is one where every edge's entailment
was proven. What is left is the handful of properties that are about the graph as a record
rather than as logic: ids that repeat, ids that are blank, a handle naming a step that was
never recorded. Those cannot be type errors, because nothing about them is ill-typed, so they
are collected as `problems` and the verifier refuses a plan that reports any.

The statistics block exists for one reason. A plan whose contracts are all `Contract.any` is
well-typed, extracts cleanly, and has been checked — and it has been checked to say nothing.
It must not present to a reader the way a plan full of real refinements does, so the counts
travel with the graph and the UI is expected to show them.

JSON is written by hand rather than by importing `Lean.Data.Json`, which would pull the whole
compiler frontend into every plan check for one escaper. Non-ASCII passes through as UTF-8,
which is valid JSON; only the characters that must be escaped are.
-/

namespace ProofGraph
namespace Json

private def hexDigit (n : Nat) : Char :=
  if n < 10 then Char.ofNat (n + 48) else Char.ofNat (n - 10 + 87)

private def uEscape (c : Char) : String :=
  let n := c.val.toNat
  "\\u" ++ String.ofList
    [hexDigit ((n >>> 12) % 16), hexDigit ((n >>> 8) % 16),
     hexDigit ((n >>> 4) % 16), hexDigit (n % 16)]

def escape (s : String) : String :=
  s.foldl (init := "") fun acc c =>
    acc ++ match c with
      | '"' => "\\\""
      | '\\' => "\\\\"
      | '\n' => "\\n"
      | '\r' => "\\r"
      | '\t' => "\\t"
      | c => if c.val < 0x20 then uEscape c else c.toString

def str (s : String) : String := "\"" ++ escape s ++ "\""
def num (n : Nat) : String := toString n
def bool (b : Bool) : String := if b then "true" else "false"
def arr (xs : List String) : String := "[" ++ String.intercalate "," xs ++ "]"

def obj (kvs : List (String × String)) : String :=
  "{" ++ String.intercalate "," (kvs.map fun (k, v) => str k ++ ":" ++ v) ++ "}"

end Json

open Json

/-- Whitespace-only, including empty. Spelled out rather than via `trim`, which now yields a
slice and would drag a conversion into every call. -/
private def isBlank (s : String) : Bool :=
  s.all fun c => c == ' ' || c == '\t' || c == '\n' || c == '\r'

partial def SchemaField.toJson : SchemaField → String
  | .mk n t nested =>
      obj [("name", str n), ("type", str t),
           ("fields", arr (nested.map SchemaField.toJson))]

def Port.toJson (p : Port) : String :=
  obj [("label", str p.label), ("source", str p.source),
       ("artifact_type", str p.artifactType), ("contract", str p.contract),
       ("refined", bool p.refined),
       ("schema", arr (p.schema.map SchemaField.toJson))]

def Alg.AlgLine.toJson (l : Alg.AlgLine) : String :=
  obj [("indent", num l.indent), ("text", str l.text)]

def Alg.AlgRecord.toJson (a : Alg.AlgRecord) : String :=
  obj [("lines", arr (a.lines.map Alg.AlgLine.toJson)),
       ("externals", arr (a.externals.map fun (tag, fn) =>
          obj [("tag", str tag), ("fn", str fn)]))]

def Node.toJson (n : Node) : String :=
  obj [("id", str n.id), ("type", str n.kind), ("goal", str n.goal),
       ("harness", str n.harness),
       ("group", if isBlank n.group then "null" else str n.group),
       ("criteria", arr (n.criteria.map str)),
       ("fields", arr (n.fields.map str)),
       ("depends_on", arr ((n.inputs.map (·.source)).eraseDups.map str)),
       ("inputs", arr (n.inputs.map Port.toJson)),
       ("fixed", arr (n.fixed.map fun f =>
          obj [("label", str f.label), ("ref", str f.ref),
               ("description", str f.description)])),
       ("produces", match n.produces with
                    | none => "null"
                    | some p => Port.toJson p),
       ("algorithm", match n.algorithm with
                     | none => "null"
                     | some a => a.toJson)]

/-- Ids that appear more than once, in first-seen order. -/
private def duplicateIds (ids : List String) : List String :=
  (ids.foldl (init := (([] : List String), ([] : List String))) fun (seen, dups) x =>
    if seen.contains x then (seen, if dups.contains x then dups else dups ++ [x])
    else (seen ++ [x], dups)).2

/-- Group descriptions that do not hold together: a described group no step belongs to is
stale text about nothing, and a group described twice is two lines fighting over one box. -/
def groupProblems (nodes : List Node) (groups : List (String × String)) : List String :=
  let keys := nodes.map (·.group)
  let belongs := fun (path : String) =>
    keys.any fun k => k == path || (k.startsWith (path ++ "/") : Bool)
  let dangling := groups.filterMap fun (path, _) =>
    if belongs path then none
    else some s!"'{path}' is described but no step belongs to it"
  let dups := (duplicateIds (groups.map (·.1))).map fun p =>
    s!"group '{p}' is described twice"
  dangling ++ dups

/-- Everything wrong with the graph that is not a matter of logic. -/
def problems (nodes : List Node) : List String :=
  let ids := nodes.map (·.id)
  let dups := (duplicateIds ids).map fun d => s!"duplicate step id '{d}'"
  let blank := nodes.filterMap fun n =>
    if isBlank n.id then some "a step has a blank id"
    else if isBlank n.goal then some s!"step '{n.id}' has a blank goal"
    else if isBlank n.harness then some s!"step '{n.id}' has a blank harness"
    else none
  let dangling := nodes.flatMap fun n =>
    n.inputs.filterMap fun p =>
      if ids.contains p.source then none
      else some s!"step '{n.id}' reads '{p.label}' from unknown step '{p.source}'"
  let selfDep := nodes.flatMap fun n =>
    n.inputs.filterMap fun p =>
      if p.source == n.id then some s!"step '{n.id}' depends on itself" else none
  let algProblems := nodes.flatMap fun n =>
    match n.algorithm with
    | none => []
    | some a => a.problems.map fun p => s!"step '{n.id}' algorithm: {p}"
  dups ++ blank ++ dangling ++ selfDep ++ algProblems

/-- How much this plan actually claims.

`contracts_any` against `contracts_refined` is the measure that separates a plan carrying real
conditions from one that type-checks because it asserts nothing. -/
def stats (nodes : List Node) : String :=
  let ports := nodes.flatMap fun n => n.inputs ++ n.produces.toList
  obj [("nodes", num nodes.length),
       ("edges", num (nodes.flatMap (·.inputs)).length),
       ("contracts_total", num ports.length),
       ("contracts_refined", num (ports.filter (·.refined)).length),
       ("contracts_any", num (ports.filter (!·.refined)).length),
       ("algorithms", num (nodes.filter (·.algorithm.isSome)).length)]

def graphJson (title : String) (st : GraphState) : String :=
  let nodes := st.nodes
  obj [("schema", str "chief.proofgraph/v1"),
       ("title", str title),
       ("nodes", arr (nodes.map Node.toJson)),
       ("groups", arr (st.groups.map fun (path, description) =>
          obj [("path", str path), ("description", str description)])),
       ("problems", arr ((problems nodes ++ groupProblems nodes st.groups).map str)),
       ("stats", stats nodes)]

/-- The markers the verifier scans for. Lean writes warnings and hints to the same stream,
so the payload has to be delimited rather than assumed to be the whole of stdout. -/
def beginMarker : String := "--PROOF-GRAPH-BEGIN--"
def endMarker : String := "--PROOF-GRAPH-END--"

/-- Print a plan's graph. Every proof-graph file ends with `#eval emitGraph "…" graph`. -/
def emitGraph (title : String) (p : GraphM Unit) : IO Unit := do
  IO.println beginMarker
  IO.println (graphJson title p.final)
  IO.println endMarker

end ProofGraph
