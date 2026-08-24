import ProofGraph

/-!
# A worked proof graph

Refreshing a fraud model: harvest events, build a dataset, fit, have someone look at it,
deploy. Five steps, and every arrow between them carries a condition the kernel checked.

The interesting arrows are the ones where the two sides are not the same contract:

* `harvest` promises `count ≥ 50000`; `buildDataset` demands `count ≥ 10000`. Discharged by
  `omega`, and the point is that nobody wrote the implication down — the plan says what each
  step needs, and the fact that one follows from the other is *derived*.
* `buildDataset` promises `rows ≥ 1000 ∧ labelled`; `fitModel` demands `rows ≥ 500 ∧
  labelled`. A conjunction, weakened on one side and carried on the other.
* `fitModel` promises `auc ≥ 80`; `review` demands `auc ≥ 75`. The reviewer is shown models
  that clear a lower bar than the one deployment insists on, which is a real distinction and
  one the plan can now state.
* `deploy` demands `auc ≥ 80` *and* an `Approval` that was granted. It cannot be written
  without the handle `review` returns, so there is no ordering of this plan in which the
  model ships unreviewed.

Change `fitModel` to promise `auc ≥ 70` and the file stops compiling, pointing at `deploy`.
That is the whole feature in one edit.
-/

open ProofGraph Alg

/-! ## Artifacts -/

structure RawEvents where
  count : Nat
deriving Repr

instance : ArtifactType RawEvents := ⟨"RawEvents"⟩
artifact_schema RawEvents

structure Dataset where
  rows : Nat
  labelled : Bool
deriving Repr

instance : ArtifactType Dataset := ⟨"Dataset"⟩
artifact_schema Dataset

structure Model where
  auc : Nat
deriving Repr

instance : ArtifactType Model := ⟨"Model"⟩
artifact_schema Model

structure Deployment where
  live : Bool
deriving Repr

instance : ArtifactType Deployment := ⟨"Deployment"⟩
artifact_schema Deployment

/-! ## Contracts

Every one of these is an `abbrev`, not a `def`. `graph_entails` has to see through the name to
the predicate underneath, and `abbrev` is what makes the definition reducible enough for it
to do so. A `def` here produces entailment failures that look like the contract is wrong when
it is only opaque.

Each also names a value it rejects. That is not decoration: `Contract.refine` will not build
without it, which is what stops a plan from being made to compile by promising nothing. -/

abbrev harvested : Contract RawEvents :=
  .refine (fun e => e.count ≥ 50000) "count ≥ 50000" ⟨0⟩ (by decide)

abbrev usable : Contract RawEvents :=
  .refine (fun e => e.count ≥ 10000) "count ≥ 10000" ⟨0⟩ (by decide)

abbrev trainable : Contract Dataset :=
  .refine (fun d => d.rows ≥ 1000 ∧ d.labelled = true) "rows ≥ 1000, labelled"
    ⟨0, false⟩ (by decide)

abbrev enoughToFit : Contract Dataset :=
  .refine (fun d => d.rows ≥ 500 ∧ d.labelled = true) "rows ≥ 500, labelled"
    ⟨0, false⟩ (by decide)

abbrev accurate : Contract Model :=
  .refine (fun m => m.auc ≥ 80) "auc ≥ 80" ⟨0⟩ (by decide)

abbrev reviewable : Contract Model :=
  .refine (fun m => m.auc ≥ 75) "auc ≥ 75" ⟨0⟩ (by decide)

abbrev shipped : Contract Deployment :=
  .refine (fun d => d.live = true) "live" ⟨false⟩ (by decide)

/-! ## Steps -/

def harvest : GraphM (Ref RawEvents harvested) :=
  task "harvest" "Pull the last 90 days of transaction events into one place." harvested
    (criteria := ["event count recorded in the artifact",
                  "date range covers 90 days ending today"])

def buildDataset (e : Ref RawEvents usable) : GraphM (Ref Dataset trainable) :=
  task "build_dataset" "Join events to chargeback outcomes and write a labelled table."
    trainable
    (criteria := ["row count and label balance recorded",
                  "every row carries a fraud label"])
    (inputs := [input "events" e])

/-- `fit_model` also carries its algorithm — pseudocode rendered from a checked term, with
the training routine and the scorer showing up as the external calls they are. See
`ProofGraph/Alg.lean` for what is and is not established by this. -/
def fitModel (d : Ref Dataset enoughToFit) : GraphM (Ref Model accurate) :=
  task "fit_model" "Train the gradient-boosted classifier and record held-out AUC." accurate
    (criteria := ["held-out AUC recorded", "training config saved beside the model"])
    (inputs := [input "dataset" d])
    (algorithm := some do
      let M ← assign "M"
        (call2 "algo" "xgboost" (x!(d) : Term (Ty.coll Ty.text))
          (Term.param (t := Ty.scalar) "λ") : Term Ty.text)
      let auc ← assign "auc" (call2 "algo" "auc_heldout" M (x!(d) : Term (Ty.coll Ty.text)))
      whenever (Term.ge auc (Term.param "τ")) do
        ret M)

def review (m : Ref Model reviewable) : GraphM (Ref Approval granted) :=
  checkpoint "review" "Decide whether this model is fit to serve live traffic."
    (fields := ["decision", "concerns"])
    (inputs := [input "model" m])

def deploy (m : Ref Model accurate) (a : Ref Approval granted) :
    GraphM (Ref Deployment shipped) :=
  task "deploy" "Roll the approved model out behind the existing scoring endpoint." shipped
    (criteria := ["endpoint returns scores from the new model",
                  "rollback path written down"])
    (inputs := [input "model" m, input "approval" a])

/-! ## The plan -/

def graph : GraphM Unit := do
  let raw ← harvest
  let ds ← buildDataset (use raw)
  let model ← fitModel (use ds)
  let ok ← review (use model)
  let _ ← deploy (use model) (use ok)
  pure ()

#eval emitGraph "Fraud model refresh" graph
