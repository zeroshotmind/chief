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

The schema is a tree. A field whose type is itself a structure with a derived schema —
directly, or through `List`/`Array`/`Option` — carries that structure's fields nested
under it, so a corpus of `List WritingSample` shows the sample's own layout, not just its
name. Nesting follows *derivation*, deliberately: a row type the author never ran
`artifact_schema` on stays a bare name, which keeps `String` from exploding into its
internals and makes "what expands" the author's own statement of which types are data.
Derive the row type before the artifact that contains it.

Compatibility needs no new check. Producer and consumer name one structure, so there is
one schema, not two to reconcile; and an algorithm that projects a field with `x!` is
checked against the structure, so a field removed at the producer breaks every consumer
that reads it — at plan time, in the consumer's own step. What travels here is a record of
what was already enforced, in the same way the drawn graph is a record of the checked one.
-/

namespace ProofGraph

/-- One field of an artifact's schema: name, pretty-printed type, and — where the field's
type is itself a structure whose schema was derived — that structure's fields, nested. -/
inductive SchemaField where
  | mk (name : String) (type : String) (nested : List SchemaField)
deriving Repr, Inhabited

/-- The fields an artifact type carries, as data, in declaration order. The blanket
instance says nothing, honestly, for types nobody derived a schema for — an empty schema
is "undeclared", never "no fields". -/
class ArtifactSchema (α : Type) where
  fields : List SchemaField

instance (priority := 0) : ArtifactSchema α := ⟨[]⟩

namespace Schema
open Lean Elab Command

/-- The marker suffix the derive command declares its instance under, which is also how
recursion decides whether a field's type expands: derived means expandable. -/
private def markName (n : Name) : Name := n ++ `chiefArtifactSchema

/-- Peel container types down to the element whose structure might expand. -/
private partial def elementOf (e : Expr) : Expr :=
  match e.getAppFn.constName? with
  | some n =>
      if n == ``List || n == ``Array || n == ``Option then
        elementOf e.appArg!
      else e
  | none => e

private partial def fieldsOf (structName : Name) (visited : List Name) :
    TermElabM (List SchemaField) := do
  let env ← getEnv
  (getStructureFields env structName).toList.mapM fun f => do
    let some ci := env.find? (structName ++ f)
      | throwError "no projection found for field '{f}'"
    Meta.forallTelescope ci.type fun _ body => do
      let shown := toString (← Meta.ppExpr body)
      let nested ← match (elementOf body).getAppFn.constName? with
        | some head =>
            -- Expand only what was itself derived, and never back into a type already on
            -- this path — a self-referential structure ends at its own name.
            if env.contains (markName head) && !visited.contains head then
              fieldsOf head (head :: visited)
            else pure []
        | none => pure []
      return .mk f.toString shown nested

private partial def quoteFields (fields : List SchemaField) :
    CommandElabM (TSyntax `term) := do
  let mut listStx ← `([])
  for field in fields.reverse do
    let .mk n t nested := field
    let nestedStx ← quoteFields nested
    listStx ←
      `(SchemaField.mk $(Syntax.mkStrLit n) $(Syntax.mkStrLit t) $nestedStx :: $listStx)
  return listStx

/-- `artifact_schema Corpus` — derive `ArtifactSchema Corpus` from the structure itself.

One line per artifact type, next to its `ArtifactType` instance, row types before the
artifacts that contain them. The fields and their types are read off the environment, so
the schema cannot disagree with the structure it describes. -/
elab "artifact_schema " id:ident : command => do
  let name ← liftCoreM <| realizeGlobalConstNoOverload id
  unless isStructure (← getEnv) name do
    throwErrorAt id "'{name}' is not a structure, so it has no fields to derive"
  let fields ← liftTermElabM <| fieldsOf name [name]
  let listStx ← quoteFields fields
  let instName := Lean.mkIdent <| `_root_ ++ markName name
  elabCommand (← `(instance $instName:ident : ArtifactSchema $id := ⟨$listStx⟩))

end Schema
end ProofGraph
