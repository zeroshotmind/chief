import ChiefPlan.Contract

/-!
# The algorithm a step carries

A step's contracts say what it consumes and promises. This file is for the *how*: the
algorithm inside the step, written as a term and rendered as the numbered pseudocode a
paper would print. One artifact, two readings — Lean elaborates the term, and extraction
prints it — so the pseudocode a reviewer reads cannot drift from what was checked.

Be precise about what "checked" means here, because it is narrower than the contract layer
and the two must not be conflated. At the edges, entailments are *proven*. Inside an
algorithm nothing is proven about the mathematics: `Σ` is a constructor, `log` is a
constructor, and no kernel obligation says the mean lies in `[0,1]`. What is checked is
scope and shape — every variable is a field of an artifact the step actually holds or a
name an earlier line bound, a projected field must exist on its structure, two vectors
under `cos` have one width, collections bind under `Σ`/`for each`, and text becomes a
number only through a named external call. An algorithm that mentions a variable nothing
bound does not render at all: it is recorded as a problem, and a plan with problems fails
verification. The same rule as everywhere else in this design — what was not checked must
not present the way checked things do.

External dependencies — LLM calls, search, databases, clustering routines, embedding
models — are `oracle` terms: named, tagged, collected off the term, and printed once in a
legend. The algorithm's honesty boundary runs exactly there: everything between two oracle
calls is arithmetic a reader can audit, and everything behind one is the outside world.
-/

namespace ChiefPlan

/-- The shapes algorithm expressions are checked against. Deliberately small: what fits a
benchmark or pipeline specification, not a general mathematics. -/
inductive Ty where
  | scalar
  | bool
  | vec (n : Nat)
  | coll (elem : Ty)
  | text
deriving Repr, DecidableEq

namespace Alg

open Ty

/-- Which shape a Lean field type lands in, for the artifact bridge. One instance per base
type; `List` lifts pointwise. -/
class Reifies (β : Type) where
  ty : Ty
/- Reducible, so a bridged field can meet the arithmetic instances directly: without this,
`x!(t, count) / 100` fails to find `Div` on `Term (Reifies.ty Nat)` even though that *is*
`Term Ty.scalar`, and the author is pushed into an ascription that says nothing. -/
attribute [reducible] Reifies.ty
instance : Reifies Nat := ⟨scalar⟩
instance : Reifies Bool := ⟨Ty.bool⟩
instance : Reifies String := ⟨text⟩
instance [Reifies β] : Reifies (List β) := ⟨coll (Reifies.ty β)⟩

mutual
/-- An expression. `bound` is the plumbing under binders and the statement layer — an
author never writes it, and a `bound` name no statement introduced is caught at emission
and refused. -/
inductive Term : Ty → Type where
  | bound {t} (name : String) : Term t
  /-- A field of an artifact some step produced. Reached only via `x!`. -/
  | proj {t} (step : String) (name : String) : Term t
  /-- A whole artifact, opaque, treated at whatever shape the algorithm needs. Reached
  only via the one-argument `x!`. -/
  | art {t} (step : String) : Term t
  /-- A named constant of the algorithm (a threshold, a width). A knob, never data. -/
  | param {t} (name : String) : Term t
  | lit (x : String) : Term scalar
  | add : Term scalar → Term scalar → Term scalar
  | sub : Term scalar → Term scalar → Term scalar
  | mul : Term scalar → Term scalar → Term scalar
  | div : Term scalar → Term scalar → Term scalar
  | log : Term scalar → Term scalar
  | exp : Term scalar → Term scalar
  | card {t} : Term (coll t) → Term scalar
  | sum {t} (bind : String) (over : Term (coll t)) (body : String → Term scalar) :
      Term scalar
  | argmax {t} (bind : String) (over : Term (coll t)) (body : String → Term scalar) :
      Term t
  /-- The members of a collection a condition keeps: `{c' ∈ g : c' ≠ c}`. -/
  | filter {t} (bind : String) (over : Term (coll t)) (pred : String → Term Ty.bool) :
      Term (coll t)
  | eq {t} : Term t → Term t → Term Ty.bool
  | ne {t} : Term t → Term t → Term Ty.bool
  | ge : Term scalar → Term scalar → Term Ty.bool
  | le : Term scalar → Term scalar → Term Ty.bool
  /-- Cosine similarity; the widths must agree, and Lean is what makes them. -/
  | cos {n} : Term (vec n) → Term (vec n) → Term scalar
  /-- An embedding model: text in, a vector of a stated width out. External, and listed in
  the legend as such. -/
  | embed {n} (model : String) : Term text → Term (vec n)
  /-- An external call: LLM, search API, database, library routine. The result shape is
  whatever the use site demands — annotate when Lean cannot infer it. -/
  | oracle {ts t} (tag : String) (fn : String) : ArgList ts → Term t

/-- Heterogeneous, typed argument list for `oracle`. -/
inductive ArgList : List Ty → Type where
  | nil : ArgList []
  | cons {t ts} : Term t → ArgList ts → ArgList (t :: ts)
end

open Term

instance : Add (Term scalar) := ⟨add⟩
instance : Sub (Term scalar) := ⟨sub⟩
instance : Mul (Term scalar) := ⟨mul⟩
instance : Div (Term scalar) := ⟨div⟩
instance : OfNat (Term scalar) n := ⟨lit (toString n)⟩

/-- `call1 "llm" "extract" x` — a one-argument external call: tag, then name, then the
argument. -/
def call1 {a t : Ty} (tag fn : String) (x : Term a) : Term t :=
  oracle tag fn (.cons x .nil)

def call2 {a b t : Ty} (tag fn : String) (x : Term a) (y : Term b) : Term t :=
  oracle tag fn (.cons x (.cons y .nil))

def call3 {a b c t : Ty} (tag fn : String) (x : Term a) (y : Term b) (z : Term c) :
    Term t :=
  oracle tag fn (.cons x (.cons y (.cons z .nil)))

/-- The collection fixes the element's shape before the body elaborates — without these, a
body that only hands the binder to a polymorphic oracle leaves the shape unsolvable. The
`Σ`/`argmax`/`filter` notation goes through them. -/
def sumOver {t : Ty} (bind : String) (over : Term (coll t))
    (body : Term t → Term scalar) : Term scalar :=
  .sum bind over (fun n => body (.bound n))

def argmaxOver {t : Ty} (bind : String) (over : Term (coll t))
    (body : Term t → Term scalar) : Term t :=
  .argmax bind over (fun n => body (.bound n))

def filterOver {t : Ty} (bind : String) (over : Term (coll t))
    (pred : Term t → Term Ty.bool) : Term (coll t) :=
  .filter bind over (fun n => pred (.bound n))

end Alg

/-- `Σ c ∈ R, body` — the bound name in the rendering is the ident you wrote. -/
syntax "Σ " ident " ∈ " term ", " term : term
/-- `argmax c ∈ G, body` — the element of `G` maximising `body`. -/
syntax "argmax " ident " ∈ " term ", " term : term
/-- `filter c' ∈ g, cond` — the members of `g` satisfying `cond`. -/
syntax "filter " ident " ∈ " term ", " term : term
macro_rules
  | `(Σ $x ∈ $s, $b) =>
      `(ChiefPlan.Alg.sumOver $(Lean.quote x.getId.toString) $s (fun $x => $b))
  | `(argmax $x ∈ $s, $b) =>
      `(ChiefPlan.Alg.argmaxOver $(Lean.quote x.getId.toString) $s (fun $x => $b))
  | `(filter $x ∈ $s, $b) =>
      `(ChiefPlan.Alg.filterOver $(Lean.quote x.getId.toString) $s (fun $x => $b))

namespace Alg

/-- **The artifact bridge.** `x!(r, field)` is that field of the artifact `r` refers to.
The `Ref` means the step must actually hold the artifact; the projection means the field
must exist on its structure; `Reifies` gives the result its shape. There is no other way
to bring data into an algorithm. -/
def field {α : Type} {c : Contract α} {β : Type} [Reifies β]
    (r : Ref α c) (_p : α → β) (name : String) : Term (Reifies.ty β) :=
  .proj r.source name

/-- `x!(r)` — the artifact itself, opaque. For the step that consumes a dataset rather
than its row count. The shape is whatever the use site treats it as; annotate when Lean
cannot infer it. -/
def whole {α : Type} {c : Contract α} {t : Ty} (r : Ref α c) : Term t :=
  .art r.source

end Alg

syntax "x!(" term ", " ident ")" : term
syntax "x!(" term ")" : term
macro_rules
  | `(x!($r, $f)) =>
      `(ChiefPlan.Alg.field $r (fun a => a.$f) $(Lean.quote f.getId.toString))
  | `(x!($r)) => `(ChiefPlan.Alg.whole $r)

namespace Alg
open Ty

/-! ## Rendering and scanning

Two walks over the same term: `render` prints it, `scan` collects the free `bound` names
(binder bodies subtract their own) and the external calls. Emission uses both — the
rendered text is what a reader reviews, and the scan is what refuses an algorithm whose
variables do not hold together. -/

mutual
partial def renderArgs : {ts : List Ty} → ArgList ts → List String
  | _, .nil => []
  | _, .cons a rest => render a :: renderArgs rest

partial def render : {t : Ty} → Term t → String
  | _, .bound n => n
  | _, .proj st n => s!"{st}.{n}"
  | _, .art st => st
  | _, .param n => n
  | _, .lit x => x
  | _, .add a b => s!"({render a} + {render b})"
  | _, .sub a b => s!"({render a} − {render b})"
  | _, .mul a b => s!"{render a}·{render b}"
  | _, .div a b => s!"{render a}/{render b}"
  | _, .log a => s!"log {render a}"
  | _, .exp a => s!"exp({render a})"
  | _, .card s => s!"|{render s}|"
  | _, .sum x s f => s!"Σ_({x} ∈ {render s}) {render (f x)}"
  | _, .argmax x s f => s!"argmax_({x} ∈ {render s}) {render (f x)}"
  | _, .filter x s p => s!"\{{x} ∈ {render s} : {render (p x)}}"
  | _, .eq a b => s!"{render a} = {render b}"
  | _, .ne a b => s!"{render a} ≠ {render b}"
  | _, .ge a b => s!"{render a} ≥ {render b}"
  | _, .le a b => s!"{render a} ≤ {render b}"
  | _, .cos a b => s!"cos({render a}, {render b})"
  | _, .embed m x => s!"{m}({render x})"
  | _, .oracle _ fn args =>
      let rendered := String.intercalate ", " (renderArgs args)
      s!"{fn}({rendered})"
end

mutual
partial def scanArgs : {ts : List Ty} → ArgList ts →
    (List String × List (String × String))
  | _, .nil => ([], [])
  | _, .cons a rest =>
      let (v, o) := scan a
      let (vs, os) := scanArgs rest
      (v ++ vs, o ++ os)

partial def scan : {t : Ty} → Term t → (List String × List (String × String))
  | _, .bound n => ([n], [])
  | _, .proj .. => ([], [])
  | _, .art _ => ([], [])
  | _, .param _ => ([], [])
  | _, .lit _ => ([], [])
  | _, .add a b | _, .sub a b | _, .mul a b | _, .div a b
  | _, .eq a b | _, .ne a b | _, .ge a b | _, .le a b =>
      let (v₁, o₁) := scan a
      let (v₂, o₂) := scan b
      (v₁ ++ v₂, o₁ ++ o₂)
  | _, .cos a b =>
      let (v₁, o₁) := scan a
      let (v₂, o₂) := scan b
      (v₁ ++ v₂, o₁ ++ o₂)
  | _, .log a => scan a
  | _, .exp a => scan a
  | _, .card s => scan s
  | _, .sum x s f | _, .argmax x s f =>
      let (v₁, o₁) := scan s
      let (v₂, o₂) := scan (f x)
      (v₁ ++ v₂.filter (· ≠ x), o₁ ++ o₂)
  | _, .filter x s p =>
      let (v₁, o₁) := scan s
      let (v₂, o₂) := scan (p x)
      (v₁ ++ v₂.filter (· ≠ x), o₁ ++ o₂)
  | _, .embed m x =>
      let (v, o) := scan x
      (v, o ++ [("embed", m)])
  | _, .oracle tag fn args =>
      let (v, o) := scanArgs args
      (v, o ++ [(tag, fn)])
end

/-! ## The statement layer

An algorithm is a `do` block in `AlgM`. `assign` names an intermediate and returns the
variable; `gather` builds a collection from per-element results, which is how a loop's
work becomes a value; `foreach` is a loop whose body is lines, not a value, and whose
binder dies with it. The lines print in the order written. -/

/-- One rendered line of an algorithm. -/
structure AlgLine where
  indent : Nat
  text : String
deriving Repr, Inhabited

/-- What running an algorithm leaves behind: the lines a reader reviews, the external
calls it makes, and everything wrong with its variables. A record with problems is not
rendered anywhere — it fails the plan. -/
structure AlgRecord where
  lines : List AlgLine := []
  externals : List (String × String) := []
  problems : List String := []
deriving Repr, Inhabited

structure AlgState where
  lines : List AlgLine := []
  indent : Nat := 0
  /-- Innermost scope first; a loop's binder lives only as long as its body. -/
  scopes : List (List String) := [[]]
  oracles : List (String × String) := []
  problems : List String := []
deriving Inhabited

/-- The monad an algorithm is written in: state, and nothing else — the same austerity as
`PlanM`, for the same reason. -/
abbrev AlgM := StateM AlgState

private def emit (text : String) : AlgM Unit :=
  modify fun s => { s with lines := s.lines ++ [{ indent := s.indent, text }] }

private def declare (name : String) : AlgM Unit :=
  modify fun s =>
    { s with scopes := match s.scopes with
        | top :: rest => (top ++ [name]) :: rest
        | [] => [[name]] }

/-- Record what a term reaches for; a variable no statement bound becomes a problem, with
`extraBinds` for names the calling statement itself introduces. -/
private def noteTerm {t : Ty} (e : Term t) (extraBinds : List String := []) :
    AlgM Unit := do
  let (vars, os) := scan e
  modify fun s =>
    let known := extraBinds ++ s.scopes.flatten
    let missing := (vars.filter fun v => !known.contains v).eraseDups
    { s with
        oracles := s.oracles ++ os
        problems := s.problems ++ missing.map fun v =>
          s!"variable '{v}' is used before anything binds it" }

private def indented (header : String) (binds : List String) (body : AlgM Unit) :
    AlgM Unit := do
  emit header
  modify fun s => { s with indent := s.indent + 1, scopes := binds :: s.scopes }
  body
  modify fun s => { s with indent := s.indent - 1, scopes := s.scopes.tail }

/-- `let m ← assign "m" e` — one assignment line; the returned term is the variable. Use
the same name in the string and the binder. -/
def assign {t : Ty} (name : String) (e : Term t) : AlgM (Term t) := do
  noteTerm e
  emit s!"{name} ← {render e}"
  declare name
  return .bound name

/-- `let R ← gather "R" "g" G fun g => e` — build a collection from per-element results:
renders `R ← \{ e : g ∈ G }` and returns the collection as a variable. -/
def gather {t u : Ty} (name bind : String) (over : Term (coll t))
    (body : Term t → Term u) : AlgM (Term (coll u)) := do
  noteTerm over
  let inner := body (.bound bind)
  noteTerm inner (extraBinds := [bind])
  emit s!"{name} ← \{ {render inner} : {bind} ∈ {render over} }"
  declare name
  return .bound name

/-- A line in prose, for the odd step that is genuinely not an expression. Nothing in it
is checked, and nothing in it can bind a variable. -/
def note (text : String) : AlgM Unit := emit text

/-- `foreach "g" G fun g => do …` — a loop; its binder, and anything assigned inside, is
out of scope after it. A loop that should produce a value is a `gather`. -/
def foreach {t : Ty} (bind : String) (over : Term (coll t))
    (body : Term t → AlgM Unit) : AlgM Unit := do
  noteTerm over
  indented s!"for each {bind} ∈ {render over}:" [bind] (body (.bound bind))

/-- `whenever cond do …` — a guarded block. -/
def whenever (cond : Term Ty.bool) (body : AlgM Unit) : AlgM Unit := do
  noteTerm cond
  indented s!"if {render cond}:" [] body

/-- The algorithm's result. `ret` what `assign`/`gather` handed you; a name you did not
bind is a problem, and a plan whose algorithm has problems fails. -/
def ret {t : Ty} (e : Term t) : AlgM Unit := do
  noteTerm e
  emit s!"return {render e}"

private def dedupPairs (xs : List (String × String)) : List (String × String) :=
  xs.foldl (init := []) fun acc x => if acc.contains x then acc else acc ++ [x]

/-- Run an algorithm and package what it left behind. -/
def AlgM.record (a : AlgM Unit) : AlgRecord :=
  let st := (a.run {}).2
  { lines := st.lines, externals := dedupPairs st.oracles, problems := st.problems }

end Alg
end ChiefPlan
