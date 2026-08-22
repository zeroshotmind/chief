#!/usr/bin/env python
"""Seed a parallel workflow whose branches are parameterised, one per paper.

    python scripts/seed_fanout.py --base http://127.0.0.1:8080/v1

The shape this demonstrates: the plan does not say how many papers there are, and cannot —
the search step finds them. The construct declares what each branch must supply about
itself (`paper`, `arxiv_id`, `pdf_path`), the body is written once with `{{ paper }}` in it,
and each branch is registered with its own values as it is discovered. The UI fills the body
in per branch; the stored plan keeps the placeholder.

Four branches are registered, one of them left running, so both the finished and the
in-flight rendering are visible. The construct is deliberately left open (`instances_closed`
unset) — a parallel step whose branches are still arriving is the normal state, not an edge.
"""

from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8000/v1"
WORKFLOW_ID = "wf_papers"

PAPERS = [
    {
        "paper": "Attention Is All You Need",
        "arxiv_id": "1706.03762",
        "pdf_path": "papers/1706.03762.pdf",
        "summary": "Read and summarised; the results table is Table 2",
        "evidence": "Table 2 (EN-DE BLEU) cited in the summary's §3",
        "note": "baseline architecture, cited by all three others",
    },
    {
        "paper": "Language Models are Few-Shot Learners",
        "arxiv_id": "2005.14165",
        "pdf_path": "papers/2005.14165.pdf",
        "summary": "Read and summarised; scaling curves are the load-bearing figure",
        "evidence": "Figure 3.1 cited, with the 175B row called out",
        "note": "72 pages, the appendix is most of it",
    },
    {
        "paper": "Training Compute-Optimal Large Language Models",
        "arxiv_id": "2203.15556",
        "pdf_path": "papers/2203.15556.pdf",
        "summary": "Read and summarised; the compute-optimal ratio is the claim to check",
        "evidence": "Table 3 cited; the 20-tokens-per-parameter figure traced to it",
        "note": "contradicts the previous paper's scaling advice, worth flagging",
    },
]

RUNNING = {
    "paper": "Deep Residual Learning for Image Recognition",
    "arxiv_id": "1512.03385",
    "pdf_path": "papers/1512.03385.pdf",
}

STEPS = [
    {
        "id": "find",
        "type": "task",
        "goal": "Find the papers worth reading for this review.",
        "harness": "claude-code",
        "depends_on": [],
        "criteria": [
            "each candidate has an arxiv id and a local PDF path",
            "anything already summarised in a previous review is excluded",
        ],
    },
    {
        "id": "read",
        "type": "parallel",
        "goal": "Read each paper found, one branch per paper.",
        "harness": "claude-code",
        "depends_on": ["find"],
        "body": ["summarise", "check"],
        "on_instance_failure": "continue",
        # The plan cannot say how many branches there will be — `find` decides that. What it
        # can say is what each branch must tell us about itself.
        "instance_params": [
            {"name": "paper", "description": "title, as it appears on the paper"},
            {"name": "arxiv_id", "description": "e.g. 2203.15556"},
            {"name": "pdf_path", "description": "where the PDF was downloaded to"},
            {"name": "note", "description": "anything odd about this one", "required": False},
        ],
    },
    {
        "id": "summarise",
        "type": "task",
        "goal": "Summarise {{ paper }} ({{ arxiv_id }}) from {{ pdf_path }}.",
        "harness": "claude-code",
        "depends_on": [],
        "criteria": [
            "the summary of {{ paper }} cites its own results table or figure",
            "the central claim is stated in one sentence",
        ],
    },
    {
        "id": "check",
        "type": "task",
        "goal": "Check the summary of {{ paper }} against the PDF.",
        "harness": "claude-code",
        "depends_on": ["summarise"],
        "criteria": ["every number in the summary appears in {{ arxiv_id }}"],
    },
    {
        "id": "synth",
        "type": "task",
        "goal": "Write the review across every paper read.",
        "harness": "claude-code",
        "depends_on": ["read"],
        "criteria": ["each paper is either cited or explicitly set aside with a reason"],
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

    existing = None
    try:
        existing = call("GET", f"/workflows/{WORKFLOW_ID}")
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            raise
    if existing and call("GET", f"/runs?workflow_id={WORKFLOW_ID}"):
        print(f"{WORKFLOW_ID} already has a run; nothing to do")
        return 0

    plan = {"title": "Paper review: one branch per paper", "steps": STEPS}
    if existing:
        if existing["status"] == "draft":
            call("PUT", f"/workflows/{WORKFLOW_ID}", plan)
    else:
        call("POST", "/workflows", {
            "workflow_id": WORKFLOW_ID,
            "source": "generated",
            "generated_by": "seed_fanout.py",
            "project": "chief",
            **plan,
        })
    if call("GET", f"/workflows/{WORKFLOW_ID}")["status"] == "draft":
        call("POST", f"/workflows/{WORKFLOW_ID}/approve", {})
    run = call("POST", f"/workflows/{WORKFLOW_ID}/runs", {})["run_id"]

    call("POST", f"/runs/{run}/steps/find/updates", {
        "status": "completed",
        "summary": "Four papers, all with local PDFs; two earlier ones already covered",
        "criteria_met": {
            "c1": "all four have an arxiv id and a downloaded PDF under papers/",
            "c2": "2 dropped — summarised in the January review",
        },
        "artifacts": [{"type": "file", "ref": "papers/candidates.json",
                       "description": "What the search turned up"}],
    })

    for paper in PAPERS:
        metadata = {k: paper[k] for k in ("paper", "arxiv_id", "pdf_path", "note")}
        instance = call("POST", f"/runs/{run}/steps/read/instances", {"metadata": metadata})
        inst = instance["instance_id"]
        base = f"/runs/{run}/state/read/{inst}"
        call("POST", f"{base}/summarise/updates", {
            "status": "completed",
            "summary": paper["summary"],
            "criteria_met": {
                "c1": paper["evidence"],
                "c2": "stated as the opening line of the summary",
            },
            "artifacts": [{"type": "markdown", "ref": f"summaries/{paper['arxiv_id']}.md",
                           "description": f"Summary of {paper['paper']}"}],
        })
        call("POST", f"{base}/check/updates", {
            "status": "completed",
            "summary": "Every figure in the summary traced back to the PDF",
            "criteria_met": {"c1": f"7 numbers checked against {paper['arxiv_id']}, all found"},
        })

    # One still in flight, so the in-progress rendering is visible too.
    running = call("POST", f"/runs/{run}/steps/read/instances", {"metadata": RUNNING})
    call("POST", f"/runs/{run}/state/read/{running['instance_id']}/summarise/updates", {
        "status": "running",
        "summary": "Reading; the PDF is a scan so the tables need OCR",
    })

    print(f"seeded {WORKFLOW_ID} / {run} — 4 branches, 1 still running, construct still open")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
