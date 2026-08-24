import Lean

/-!
# Artifact schemas, derived rather than declared

An artifact's schema is its Lean structure — that is already the design: two steps agree
about an artifact because both name the same structure, and the type checker refuses an
edge whose ends disagree. What this file adds is the way that schema *leaves* Lean: a
reflection command reads the structure's fields and their types and records them as data,
so extraction can print them, the UI can show them on both ends of an edge, and the
compiled workflow can tell a harness what the produced document must look like.

Derived, never written by hand, because a hand-declared schema is a second description of
the same structure and second descriptions drift. `artifact_schema Corpus` reads the
fields off the structure at elaboration time; edit the structure and the schema follows,
or the command fails. There is nothing to keep in step.

Compatibility needs no new check. Producer and consumer name one structure, so there is
one schema, not two to reconcile; and an algorithm that projects a field with `x!` is
checked against the structure, so a field removed at the producer breaks every consumer
that reads it — at plan time, in the consumer's own step. What travels here is a record of
what was already enforced, in the same way the drawn graph is a record of the checked one.
-/

namespace ChiefPlan

/-- The fields an artifact type carries, as data: name and pretty-printed type, in
declaration order. The blanket instance says nothing, honestly, for types nobody derived a
schema for — an empty schema is "undeclared", never "no fields". -/
class ArtifactSchema (α : Type) where
  fields : List (String × String)

instance (priority := 0) : ArtifactSchema α := ⟨[]⟩

open Lean Elab Command in
/-- `artifact_schema Corpus` — derive `ArtifactSchema Corpus` from the structure itself.

One line per artifact type, next to its `ArtifactType` instance. The fields and their
types are read off the environment, so the schema cannot disagree with the structure it
describes. -/
elab "artifact_schema " id:ident : command => do
  let name ← liftCoreM <| realizeGlobalConstNoOverload id
  let env ← getEnv
  unless isStructure env name do
    throwErrorAt id "'{name}' is not a structure, so it has no fields to derive"
  let fields := getStructureFields env name
  let entries ← liftTermElabM do
    fields.mapM fun f => do
      let some ci := env.find? (name ++ f)
        | throwError "no projection found for field '{f}'"
      Meta.forallTelescope ci.type fun _ body => do
        return (f.toString, toString (← Meta.ppExpr body))
  let mut listStx ← `([])
  for (n, t) in entries.reverse do
    listStx ← `(($(Syntax.mkStrLit n), $(Syntax.mkStrLit t)) :: $listStx)
  elabCommand (← `(instance : ArtifactSchema $id := ⟨$listStx⟩))

end ChiefPlan
