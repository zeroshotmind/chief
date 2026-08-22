#!/usr/bin/env python
"""Seed a workflow that shows what criteria are for, by showing the before and after.

    python scripts/seed_criteria.py --base http://127.0.0.1:8080/v1

The plan holds two versions of the same piece of work. `t1_old` carries a goal copied in the
shape the store was full of — one 400-character paragraph with its acceptance conditions
buried in the prose. `t1` is the same work, split: a two-line goal, and the conditions as
criteria. Reading them side by side is the argument.

The run then exercises every state the checklist can be in: all criteria answered, some
answered mid-flight, none answered, and a step reported `failed` with one outstanding —
which is what a harness is meant to do rather than reporting completion around it.
"""

from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8000/v1"
WORKFLOW_ID = "wf_crit"

# Representative of what real plans looked like before criteria existed: an acceptance
# condition written as prose, three sentences in, where nothing can enumerate it.
OLD_GOAL = (
    "Build the toy arithmetic dataset (train/held-out split of 'a+b=' style prompts) and the "
    "exact-match reward function, unit-tested against hand-written correct, incorrect and "
    "malformed completions. Write down up front what reward hacking would look like here — "
    "degenerate or empty completions, echoing the prompt, emitting every digit to satisfy a "
    "loose parser, satisfying a format check without answering — so the audit later has a "
    "checklist rather than an impression"
)

STEPS = [
    {
        "id": "t1_old",
        "type": "task",
        "goal": OLD_GOAL,
        "harness": "claude-code",
        "depends_on": [],
    },
    {
        "id": "t1",
        "type": "task",
        "goal": "Build the toy arithmetic dataset and the exact-match reward function.",
        "harness": "claude-code",
        "depends_on": [],
        "criteria": [
            "train/held-out split of 'a+b=' prompts exists and does not overlap",
            "reward function unit-tested on correct, incorrect and malformed completions",
            "the reward-hacking modes are written down before training starts",
        ],
    },
    {
        "id": "t2",
        "type": "task",
        "goal": "Run the signal gate: sample from the untrained model and report the "
                "base rates.",
        "harness": "claude-code",
        "depends_on": ["t1"],
        "criteria": [
            "base-rate accuracy reported on held-out prompts",
            "per-group reward variance reported and non-zero",
        ],
    },
    {
        "id": "t3",
        "type": "task",
        "goal": "Train, then audit the completions for reward hacking.",
        "harness": "claude-code",
        "depends_on": ["t2"],
        "criteria": [
            "held-out accuracy beats the baseline from t2",
            "every hacking mode listed in t1 was checked against real completions",
            "no completion scores full reward without answering",
        ],
    },
    {
        "id": "t4",
        "type": "task",
        "goal": "Write up what the run showed.",
        "harness": "claude-code",
        "depends_on": ["t3"],
        "criteria": ["the write-up states what would have changed the conclusion"],
    },
]


def call(method: str, path: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req) as resp:
        raw = resp.read()
        return json.loads(raw) if raw else {}


def main() -> int:
    global BASE
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base", default=BASE)
    args = ap.parse_args()
    BASE = args.base.rstrip("/")

    # Keyed on the run, not the workflow: a seeder that bails the moment the *draft* exists
    # leaves a half-seeded workflow behind and reports success, which is exactly how
    # seed_viewer.py once shipped a demo whose second half never ran.
    existing = None
    try:
        existing = call("GET", f"/workflows/{WORKFLOW_ID}")
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            raise
    if existing and call("GET", f"/runs?workflow_id={WORKFLOW_ID}"):
        print(f"{WORKFLOW_ID} already has a run; nothing to do")
        return 0
    plan = {"title": "Criteria: a goal split from what decides it", "steps": STEPS}
    if existing:
        # A draft with no run: revise it in place rather than leaving a stale one beside a
        # new id. Workflows have no delete, by design — nothing here gets to remove a record.
        print(f"{WORKFLOW_ID} exists as a draft with no run; finishing the seed")
        if existing["status"] == "draft":
            call("PUT", f"/workflows/{WORKFLOW_ID}", plan)
    else:
        call("POST", "/workflows", {
            "workflow_id": WORKFLOW_ID,
            "source": "generated",
            "generated_by": "seed_criteria.py",
            "project": "chief",
            **plan,
        })
    if call("GET", f"/workflows/{WORKFLOW_ID}")["status"] == "draft":
        call("POST", f"/workflows/{WORKFLOW_ID}/approve", {})
    run = call("POST", f"/workflows/{WORKFLOW_ID}/runs", {})["run_id"]

    call("POST", f"/runs/{run}/steps/t1_old/updates", {
        "status": "completed",
        "summary": "The same work, planned the old way — nothing to check it against",
    })

    # Every criterion answered, each with evidence rather than a tick.
    call("POST", f"/runs/{run}/steps/t1/updates", {
        "status": "completed",
        "summary": "2,000 prompts split 90/10, reward function green on 31 hand-written cases",
        "criteria_met": {
            "c1": "1,800 train / 200 held-out, intersection is empty (asserted in the test)",
            "c2": "31 cases: 12 correct, 12 incorrect, 7 malformed — all expected scores",
            "c3": "four modes written up in notes/hacking.md before any training ran",
        },
    })

    # Answered on the way through rather than all at the end — the accumulating case.
    call("POST", f"/runs/{run}/steps/t2/updates", {
        "status": "running",
        "summary": "Sampling 8 completions per prompt from the untrained model",
        "criteria_met": {"c1": "base rate 4.5% on 200 held-out prompts"},
    })
    call("POST", f"/runs/{run}/steps/t2/updates", {
        "status": "completed",
        "summary": "Base rate 4.5%, group variance 0.21 — enough signal for GRPO to learn from",
        "criteria_met": {"c2": "mean within-group variance 0.21, well clear of zero"},
    })

    # Two of three answered, and the third could not be. Reported failed rather than
    # completed, which is the whole point of the gate.
    call("POST", f"/runs/{run}/steps/t3/updates", {
        "status": "failed",
        "summary": "Accuracy beat the baseline but 3% of full-reward completions echo the "
                   "prompt — the parser is too loose to call this trained",
        "criteria_met": {
            "c1": "38.5% held-out against a 4.5% baseline",
            "c2": "all four modes checked against 500 sampled completions",
        },
    })
    print(f"seeded {WORKFLOW_ID} / {run}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
