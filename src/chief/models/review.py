"""Review notes — what a person wants changed about a plan, before they will approve it.

The other half of the artifact-comment channel (CONTRACT-NOTES.md #30). A comment on an
artifact is said about work that is done; a review note is said about work that has not
started, while the plan is still a draft and the reviewer is deciding whether to let it run.

Notes are kept beside the workflow rather than inside it. ``revise_draft`` replaces the
whole plan — title, steps, everything — so a note stored on a ``WorkflowStep`` would be
destroyed by the very revision it asked for. See CONTRACT-NOTES.md #31.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ReviewNote(BaseModel):
    """One thing a reviewer said about a draft.

    ``step_id`` names the step it is about, or is ``None`` for the plan as a whole ("this
    is three steps in a chain and it should be a loop") — feedback that belongs to no
    single node and would be misfiled on whichever one it was pinned to.

    ``step_goal`` is the step's goal *as it read when the note was written*, kept as a copy
    rather than looked up. A revision may rewrite the goal or drop the step entirely, and
    the note has to keep saying what it was about either way.

    Append-and-resolve, not editable: the body is what was said, and the only thing that
    changes afterwards is whether it still needs attention.
    """

    model_config = ConfigDict(extra="forbid")

    note_id: str
    workflow_id: str
    step_id: str | None = None
    step_goal: str | None = None
    body: str = Field(min_length=1)
    author: str = Field(min_length=1)
    created_at: str
    resolved: bool = False
    resolved_at: str | None = None
    resolved_by: str | None = None
    via: str | None = None
    # True when ``step_id`` names a step the plan no longer has — the harness revised the
    # draft and this note's step went with it. Derived on the way out from the plan as it
    # stands now, never stored: a later revision can bring the id back, and a stored flag
    # would then be a lie. A note is never dropped or auto-resolved for orphaning; whether
    # the feedback was addressed or merely restructured around is the reviewer's call.
    orphaned: bool = False

    @property
    def target_label(self) -> str:
        return self.step_id or "the plan"


class ReviewNoteCreate(BaseModel):
    """Body for leaving a note on a draft.

    ``step_id`` is optional and must name a step the plan currently has — a note on a step
    that is not there could never be read in context. ``step_goal``, ``created_at`` and the
    resolution fields are not accepted: they are the record's, not the caller's.
    """

    model_config = ConfigDict(extra="forbid")

    body: str = Field(min_length=1)
    step_id: str | None = None
    author: str = "human"


class ReviewNoteDecision(BaseModel):
    """Body for closing a note, or putting it back.

    One endpoint in both directions rather than a resolve and an unresolve: resolving by
    mistake is ordinary, and the state is a flag, not an event.
    """

    model_config = ConfigDict(extra="forbid")

    resolved: bool = True
    resolved_by: str = "human"
