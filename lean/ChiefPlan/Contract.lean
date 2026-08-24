/-!
# Artifacts, contracts, and the handles that carry them

This is the layer Lean actually checks. A plan is a composition of steps; a step demands
artifacts satisfying some condition and promises an artifact satisfying another; and the
only question Lean is asked is whether each promise *entails* the demand it feeds.

Three things are deliberate here.

**Contracts are proven non-vacuous.** `Contract.refine` cannot be built without a value the
predicate rejects, together with a proof that it rejects it. That closes the failure mode
this whole feature exists to rule out: a planner under repair pressure making a failing edge
compile by weakening the upstream promise to `fun _ => True`. Such a contract is not merely
discouraged, it is unconstructible — `¬ True` has no proof. A plan with no real conditions in
it therefore cannot present itself as verified. `Contract.any` remains available for an
artifact with genuinely nothing to say about it, but it is a separate constructor, it is
counted in the extracted statistics, and it can never be mistaken for a refinement.

**Entailment is proven over all values, not checked on one.** The obligation discharged at
each edge is `∀ x, promise x → demand x`. Nothing is sampled and nothing is run; the claim is
about every artifact the step could ever produce. This is strictly stronger than the runtime
postcondition the plan compiles down to, which is why the runtime check stays cheap: the
shape was settled here, and the runtime only confirms the concrete value.

**The predicate is what is checked; the text beside it is not.** `shown` exists because a
`α → Prop` is erased before extraction can read it, so the string is what reaches the UI.
The two can drift cosmetically — a contract labelled "rows ≥ 1000" whose predicate says
`≥ 100` will verify, and will display the wrong thing. It cannot drift *unsoundly*: the
kernel reads `pred` and nothing else, so a mislabelled contract still constrains exactly what
its predicate says. Generating both from one syntax is the obvious next step and is not done
here.
-/

namespace ChiefPlan

/-- The name an artifact type is known by outside Lean.

Needed because a bare `α : Type` has no recoverable name once extraction is running as
ordinary evaluated code: the type is erased, so the name has to be carried as data. One
instance per artifact structure. -/
class ArtifactType (α : Type) where
  typeName : String

export ArtifactType (typeName)

/-- What is known about an artifact at plan time.

`refine` carries four things: the predicate the kernel checks, the text a reader sees, a
value the predicate rejects, and the proof that it does. The last two are the non-vacuity
guard described above — for a decidable predicate they cost the author a literal and a
`by decide` or `by simp`.

Bind these with `abbrev`, not `def`: see `plan_entails` for why a `def` here breaks every edge
that touches it. Discharge `rejects` with `by decide` — every predicate in the fragment plans
are scoped to is decidable, so it always works and needs no thought. `by simp` is the fallback
for the rare predicate `decide` cannot evaluate.

The rejected value is written as an anonymous constructor, so its fields go in declaration
order — `⟨0, false⟩` for a structure whose first field is a `Nat` and whose second is a `Bool`.
It must be a whole artifact even when the predicate reads one field, and getting the order
wrong surfaces as a type error on the literal, which reads as though the contract is malformed
when only the witness is. -/
inductive Contract (α : Type) where
  /-- Nothing is claimed about this artifact. Honest, and counted as such. -/
  | any : Contract α
  /-- A condition, together with a witness that it excludes something. -/
  | refine (pred : α → Prop) (shown : String) (counter : α) (rejects : ¬ pred counter) :
      Contract α

namespace Contract

variable {α : Type}

/-- The proposition this contract asserts of an artifact. `any` asserts `True`. -/
def pred : Contract α → (α → Prop)
  | .any => fun _ => True
  | .refine p _ _ _ => p

@[simp] theorem pred_any : (Contract.any (α := α)).pred = fun _ => True := rfl

@[simp] theorem pred_refine (p : α → Prop) (s : String) (c : α) (r : ¬ p c) :
    (Contract.refine p s c r).pred = p := rfl

/-- The human-readable form, for extraction. Never read by the kernel. -/
def shown : Contract α → String
  | .any => "any"
  | .refine _ s _ _ => s

/-- Whether this contract actually constrains anything. Extracted, so that a plan whose
contracts are all `any` cannot look like one that carries real conditions. -/
def refined : Contract α → Bool
  | .any => false
  | .refine .. => true

end Contract

/-- A handle to the artifact a step will produce, indexed by what is known about it.

There is no artifact here — nothing has run, and nothing will run until Chief executes the
compiled plan. What a `Ref` carries at runtime is the id of the step that produces it, which
is what makes the data-dependency edge recoverable by extraction. What it carries at *type*
level is the contract, which is what makes the edge checkable. The graph and the logic are
the same object seen twice, and this is the type that makes that true. -/
structure Ref (α : Type) (_c : Contract α) where
  /-- The id of the step that produces this artifact. -/
  source : String
deriving Repr

/-- Discharge `∀ x, promise x → demand x` for the decidable fragment.

**What it closes.** `≤` and `≥` bounds on `Nat` in either direction, equalities and
disequalities on `Nat` and `Bool`, and conjunctions mixing all of those — a promise of
`p99 ≤ 200 ∧ errors ≤ 50 ∧ sustained = true` entailing a demand of `p99 ≤ 400 ∧ errors ≤ 500 ∧
sustained = true` needs nothing written by hand. It is not limited to the shapes the examples
happen to show. What it will *not* close is anything outside that fragment: quantifiers over
collections, arithmetic on `Int` division, string structure, anything about the world. A
contract that needs more than this has drifted out of what plan-time checking is for, and the
honest move is to state it as a criterion a person or a harness answers for instead.

**What it needs from you.** Contracts must be bound with `abbrev`, never `def`. The tactic has
to see the predicate underneath the name, and `def` makes it opaque — so a `def`-bound contract
fails on every edge that touches it, with an error that looks like the contract is wrong when
it is merely unreducible. This is the single most common way a correct plan fails to compile.

**When it fails.** It leaves the goal standing rather than inventing a message, so Lean prints
the entailment that does not hold, with both sides in view and a counterexample interval where
`omega` can find one. That text is what a planner reads to repair the plan, and it is better
than any wording this file could put in its place.

The ladder is ordered by cost and is entirely Lean core: `trivial` closes an `any` demand,
`assumption` an unchanged contract, `omega` the arithmetic, `simp_all` the structural and
boolean cases. `decide` is deliberately absent — the goal is open under a universally
quantified `x`, so there is nothing to decide. -/
syntax "plan_entails" : tactic

macro_rules
  | `(tactic| plan_entails) =>
    `(tactic| (
        intro x hx
        try simp only [Contract.pred] at hx ⊢
        all_goals (
          first
            | (trivial; done)
            | (assumption; done)
            | (omega; done)
            | (simp_all; done)
            | (simp_all <;> omega)
            | (constructor <;>
                (first | (trivial; done) | (assumption; done) | (omega; done)
                       | (simp_all; done)))
            | (obtain ⟨_, _⟩ := hx; omega)
            | skip)))

/-- Feed an artifact to a step that demands less than is promised.

Every edge in a plan goes through this. The proof obligation is synthesised by
`plan_entails`, so an author writes `use d` and nothing else; if the entailment does not
hold, the plan does not compile, and the error names the two contracts.

`c₂` is implicit and is fixed by unification with the type the consuming step demands, so the
obligation is elaborated with both sides known. That is also why this belongs at the call site
in `plan`, and never inside a step's own `inputs := [input "x" (use r)]` — written there it has
nothing to unify against, and the goal comes out stated against a metavariable, with an error
that names neither `use` nor the fix. Steps take their handles as parameters; `plan` applies
them. -/
def use {α : Type} {c₁ : Contract α} (r : Ref α c₁) {c₂ : Contract α}
    (_entails : ∀ x, c₁.pred x → c₂.pred x := by plan_entails) : Ref α c₂ :=
  ⟨r.source⟩

end ChiefPlan
