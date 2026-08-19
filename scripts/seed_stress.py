#!/usr/bin/env python
"""Seed the two shapes the UI gets wrong when a plan is bigger than the window.

Not a demo — a fixture for the failures below, kept because both are the kind of thing
that reads fine at three steps and breaks at twelve, so nothing in `seed_demo.py` was ever
going to catch them.

    python scripts/seed_stress.py --base http://127.0.0.1:8000/v1

**A wide fan-out.** Twelve independent steps side by side. The graph lays out at a fixed
node width once it runs out of room, so the plane grows past the viewport — and the
viewport clipped it, which put the right-hand nodes behind the inspector with no way to
reach them.

**Artifacts with more to say than fits.** A long description, and a markdown body of
twenty-odd lines. Both were cut off with nothing offering the rest.

Like `seed_demo.py` this goes through the public API only, so whatever it produces is
reachable by any other client (REQ-4). The ids are fixed, so it wants a database it has not
already been run against.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8000/v1"

FAN = 12
LONG_DESCRIPTION = (
    "Persona sheet for the whole cast, including the three walk-on parts that only appear "
    "in the second act and the two that were cut but are kept here because the continuity "
    "notes still reference them"
)
LONG_MARKDOWN = "\n".join(
    [
        "## What this run produced",
        "",
        "A pass over every shard, with the per-shard notes below. The interesting part is "
        "that shards 4 and 9 disagree about the same row, which is either a clock skew "
        "problem or a genuine double-write, and this run cannot tell which.",
        "",
        "## Per shard",
        "",
        *[
            f"- shard {i:02d}: {340 + i * 17} rows reconciled, {i % 3} left for review"
            for i in range(12)
        ],
        "",
        "## What to do next",
        "",
        "- Re-run 4 and 9 with the clock check on",
        "- Leave the rest; they are clean",
        "- The reviewer wants the row counts in the summary, not only in here",
    ]
)


def call(
    method: str, path: str, body: dict | None = None, quiet: tuple[int, ...] = ()
) -> dict | list:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        if exc.code not in quiet:
            print(f"{method} {path} -> {exc.code}\n{exc.read().decode()}", file=sys.stderr)
        raise


class AlreadySeeded(Exception):
    pass


def task(sid: str, goal: str, deps: list[str] | None = None) -> dict:
    return {
        "id": sid,
        "type": "task",
        "goal": goal,
        "harness": "claude-code",
        "depends_on": deps or [],
    }


def seed_wide() -> None:
    """Twelve steps in one layer, fanning out from one and back into one.

    A fan-out, not a `parallel` construct: these are branches you can name at plan time, so
    they are ordinary steps side by side. That is also what makes the layout hard — twelve
    real nodes in a single layer, none of them foldable into a gate.
    """
    fan = [
        task(f"w{i:02d}", f"Reconcile shard {i:02d} against the ledger", ["w_open"])
        for i in range(FAN)
    ]
    steps = [
        task("w_open", "Split the ledger into shards and hand one to each worker"),
        *fan,
        task("w_join", "Merge every shard's result and report the disagreements",
             [s["id"] for s in fan]),
    ]
    try:
        call("POST", "/workflows", {
            "workflow_id": "wf_wide", "title": "Wide fan-out (12 parallel shards)",
            "source": "generated", "generated_by": "claude-code", "steps": steps,
        }, quiet=(409,))
    except urllib.error.HTTPError as exc:
        if exc.code == 409:
            raise AlreadySeeded("wf_wide") from None
        raise
    call("POST", "/workflows/wf_wide/approve", {"decided_by": "roy"})
    run = call("POST", "/workflows/wf_wide/runs", {})["run_id"]

    call("POST", f"/runs/{run}/steps/w_open/updates",
         {"status": "completed", "summary": "12 shards, 4.1M rows"})
    for i, step in enumerate(fan):
        # One of them carries the artifacts, so the wide graph and the long text are on the
        # same screen: the inspector has to hold both at once.
        artifacts = []
        if i == 4:
            artifacts = [
                {"type": "markdown", "ref": f"out/shard-{i:02d}.md",
                 "description": LONG_DESCRIPTION, "data": {"text": LONG_MARKDOWN}},
                {"type": "log", "ref": f"out/shard-{i:02d}.log",
                 "description": "Full reconciliation log, including the two rows that "
                                "disagree and every row that was checked before them"},
            ]
        call("POST", f"/runs/{run}/steps/{step['id']}/updates", {
            "status": "completed",
            "summary": f"shard {i:02d}: reconciled, {i % 3} rows left for review",
            "artifacts": artifacts,
        })
    call("POST", f"/runs/{run}/steps/w_join/updates",
         {"status": "running", "summary": "Merging; shards 4 and 9 disagree"})


def seed_wordy() -> None:
    """A small plan whose artifacts have more to say than the panel gives them.

    Separated from the wide one deliberately: if the two failures only ever appear
    together, the fix for one can hide the other.
    """
    steps = [task("t1", "Draft the piece"), task("t2", "Review it", ["t1"])]
    try:
        call("POST", "/workflows", {
            "workflow_id": "wf_wordy", "title": "Artifacts with a lot to say",
            "source": "generated", "generated_by": "claude-code", "steps": steps,
        }, quiet=(409,))
    except urllib.error.HTTPError as exc:
        if exc.code == 409:
            raise AlreadySeeded("wf_wordy") from None
        raise
    call("POST", "/workflows/wf_wordy/approve", {"decided_by": "roy"})
    run = call("POST", "/workflows/wf_wordy/runs", {})["run_id"]

    call("POST", f"/runs/{run}/steps/t1/updates", {
        "status": "completed",
        "summary": "First draft done",
        "artifacts": [
            {"type": "markdown", "ref": "drafts/piece.md", "description": LONG_DESCRIPTION,
             "data": {"text": LONG_MARKDOWN}},
            # Short ones too: the expander must not appear where there is nothing to expand.
            {"type": "markdown", "ref": "drafts/note.md", "description": "One line",
             "data": {"text": "Nothing much to say about this one."}},
        ],
    })


def main() -> int:
    global BASE
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base", default=BASE, help="API base URL including the /v1 prefix")
    BASE = ap.parse_args().base.rstrip("/")

    seeded = 0
    for seed in (seed_wide, seed_wordy):
        name = seed.__name__.removeprefix("seed_")
        try:
            seed()
        except AlreadySeeded as exc:
            print(f"skipped {name} — workflow '{exc.args[0]}' is already there")
            continue
        seeded += 1
        print(f"seeded {name}")
    if not seeded:
        print("\nNothing new — the ids are fixed. Use a fresh database to re-seed.",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
