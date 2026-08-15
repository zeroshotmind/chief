#!/usr/bin/env python
"""Seed a Chief instance with demo runs, purely through the public REST API.

Nothing here touches the store directly — the script is a harness impersonator, so
whatever it produces is reachable by any other API client (REQ-4). Point it at a running
server:

    python scripts/seed_demo.py --base http://127.0.0.1:8000/v1

The ids are fixed so the demo is reproducible, which means it wants an empty database: a
second run against the same one skips everything it has already created rather than
duplicating it. It is a development aid for the web UI, not part of the product.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8000/v1"


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
    except urllib.error.HTTPError as exc:  # surface the server's own error payload
        if exc.code not in quiet:
            print(f"{method} {path} -> {exc.code}\n{exc.read().decode()}", file=sys.stderr)
        raise


def md(text: str) -> dict:
    return {"type": "markdown", "data": {"text": text}}


class AlreadySeeded(Exception):
    """The demo ids are fixed, so a second run against the same database collides."""


def workflow(wid: str, title: str, steps: list[dict], generated_by: str | None = None) -> str:
    try:
        call(
            "POST",
            "/workflows",
            {
                "workflow_id": wid,
                "title": title,
                "source": "generated" if generated_by else "import",
                "generated_by": generated_by,
                "steps": steps,
            },
            quiet=(409,),
        )
    except urllib.error.HTTPError as exc:
        if exc.code == 409:
            raise AlreadySeeded(wid) from None
        raise
    call("POST", f"/workflows/{wid}/approve")
    return wid


def task(sid: str, goal: str, harness: str, deps: list[str] | None = None) -> dict:
    return {"id": sid, "type": "task", "goal": goal, "harness": harness,
            "depends_on": deps or []}


def construct(
    sid: str, type_: str, goal: str, harness: str, body: list[str], deps: list[str] | None = None
) -> dict:
    return {"id": sid, "type": type_, "goal": goal, "harness": harness,
            "depends_on": deps or [], "body": body, "on_instance_failure": "continue"}


def step_update(run: str, step: str, **kw) -> None:
    call("POST", f"/runs/{run}/steps/{step}/updates", kw)


def instance(run: str, step: str, kind: str, index: int, summary: str,
             artifacts: list[dict] | None = None, status: str = "completed") -> None:
    inst = call("POST", f"/runs/{run}/steps/{step}/instances", {"kind": kind, "index": index})
    call(
        "POST",
        f"/runs/{run}/steps/{step}/instances/{inst['instance_id']}/updates",
        {"status": status, "summary": summary, "artifacts": artifacts or []},
    )


# --- the five demo runs -----------------------------------------------------------------


def seed_ideas() -> None:
    wid = workflow("wf_ideas", "Research ideas: sparse attention", [
        task("g1", "Survey recent sparse-attention papers and extract open problems",
             "claude-code"),
        construct("g2", "loop", "Generate one research idea per open problem, with a "
                  "feasibility sketch", "claude-code", ["g2a"], ["g1"]),
        task("g2a", "Draft the idea as a one-pager", "claude-code"),
        task("g3", "Rank ideas by novelty and feasibility; shortlist top two",
             "claude-code", ["g2"]),
    ], generated_by="claude-code")
    run = call("POST", f"/workflows/{wid}/runs", {"run_id": "run_a3f1"})["run_id"]

    step_update(run, "g1", status="running", summary="Pulling arXiv listings")
    step_update(run, "g1", status="completed",
                summary="Surveyed 23 papers; 3 open problems extracted",
                artifacts=[{**md(
                    "## Open problems\n"
                    "- KV-cache growth dominates long-context serving cost\n"
                    "- Block-sparse patterns are static; content-aware routing is underexplored\n"
                    "- No accepted benchmark isolates retrieval vs. locality behavior"
                ), "ref": "notes/open-problems.md", "description": "Open problems memo"}])

    for i, (label, text) in enumerate([
        ("Idea 1: learned block router",
         "## Idea 1 — Learned block router\nRoute attention to blocks with a tiny scorer "
         "trained jointly with the model, replacing static sparsity patterns.\n"
         "- Novelty: medium — adjacent to MoE routing\n"
         "- Feasibility: high — single-GPU prototype in ~2 weeks"),
        ("Idea 2: decay-gated KV eviction",
         "## Idea 2 — Decay-gated KV eviction\nEvict KV entries by a learned per-head decay "
         "gate instead of recency heuristics.\n"
         "- Novelty: high — no learned-eviction baseline found in survey\n"
         "- Feasibility: medium — needs a serving-stack fork"),
        ("Idea 3: retrieval-locality benchmark",
         "## Idea 3 — Retrieval–locality benchmark\nA synthetic suite that separates retrieval "
         "ability from locality bias, scored per attention head.\n"
         "- Novelty: medium — benchmark gap is real but crowded space\n"
         "- Feasibility: high — pure data work"),
    ]):
        instance(run, "g2", "iteration", i, label,
                 [{**md(text), "ref": f"ideas/idea-{i + 1}.md", "description": "One-pager"}])
    step_update(run, "g2", instances_closed=True, summary="All three ideas drafted")

    call("POST", f"/runs/{run}/amendments", {
        "proposed_by": "claude-code", "kind": "forward",
        "reason": "Ideas 1 and 2 look close to published work. Insert an arXiv novelty check "
                  "before ranking.",
        "operations": [
            {"op": "insert_after", "target_step_id": "g2",
             "step": task("g2n", "Check each idea against arXiv for prior work; attach the "
                          "closest matches", "claude-code", ["g2"])},
            {"op": "update_step", "target_step_id": "g3",
             "step": task("g3", "Rank ideas by novelty and feasibility; shortlist top two",
                          "claude-code", ["g2n"])},
        ],
    })


def seed_ablation() -> None:
    wid = workflow("wf_ablate", "Ablation study → paper draft", [
        task("e1", "Implement the three ablation variants on a feature branch", "claude-code"),
        construct("e2", "parallel", "Train all variants and log curves", "claude-cowork",
                  ["e2a"], ["e1"]),
        task("e2a", "Train one variant and export its loss curve", "claude-cowork"),
        task("e3", "Write the results section with tables and figures", "claude-code", ["e2"]),
        task("e4", "Assemble the full paper draft (LaTeX)", "claude-code", ["e3"]),
    ])
    run = call("POST", f"/workflows/{wid}/runs", {"run_id": "run_b7c2"})["run_id"]

    step_update(run, "e1", status="completed",
                summary="Three variants implemented on abl/sparse-v2",
                artifacts=[{"type": "url", "ref": "https://github.com/acme/lab/tree/abl/sparse-v2",
                            "description": "Feature branch"}])

    am = call("POST", f"/runs/{run}/amendments", {
        "proposed_by": "claude-cowork", "kind": "forward",
        "reason": "Variant C needs the 13B config; update the training step to allocate two GPUs.",
        "operations": [{"op": "update_step", "target_step_id": "e2a",
                        "step": task("e2a", "Train one variant and export its loss curve "
                                     "(13B on 2×GPU where needed)", "claude-cowork")}],
    })
    call("POST", f"/amendments/{am['amendment_id']}/approve",
         {"decided_by": "human", "reason": "Confirmed with the cluster quota."})

    instance(run, "e2", "branch", 0, "Variant A converges 1.8× faster than baseline",
             [{"type": "image", "ref": "https://placehold.co/640x360/232532/9184d9?text=variant+A",
               "description": "Loss curve — variant A vs baseline",
               "data": {"width": 640, "height": 360}}])
    instance(run, "e2", "branch", 1, "Variant B matches baseline within noise",
             [{"type": "image", "ref": "https://placehold.co/640x360/232532/9184d9?text=variant+B",
               "description": "Loss curve — variant B vs baseline",
               "data": {"width": 640, "height": 360}}])
    instance(run, "e2", "branch", 2, "Variant C at step 42k / 60k", status="running")


def seed_comic() -> None:
    wid = workflow("wf_comic", "Comic issue 42 + teaser video", [
        task("c1", "Write the four-panel script from this week's changelog", "claude-code"),
        construct("c2", "loop", "Illustrate each panel of the script", "imagen-batch",
                  ["c2a"], ["c1"]),
        task("c2a", "Render one panel at 480×480", "imagen-batch"),
        task("c3", "Cut the panels into a 15-second teaser video", "ffmpeg-agent", ["c2"]),
        task("c4", "Publish the strip and teaser to the blog", "claude-code", ["c3"]),
    ])
    run = call("POST", f"/workflows/{wid}/runs", {"run_id": "run_c9d4"})["run_id"]

    step_update(run, "c1", status="completed", summary="Script written: “The Flaky Test”",
                artifacts=[{**md(
                    "## Issue 42 — “The Flaky Test”\n"
                    "- Panel 1: dev celebrates a green CI run\n"
                    "- Panel 2: rerun on the same commit — red\n"
                    "- Panel 3: dev stares into the void\n"
                    "- Panel 4: caption card: “works on my machine” — the machine"
                ), "ref": "comic/script-42.md", "description": "Four-panel script"}])
    for i in range(4):
        instance(run, "c2", "iteration", i, f"Panel {i + 1} rendered",
                 [{"type": "image",
                   "ref": f"https://placehold.co/480x480/232532/9184d9?text=panel+{i + 1}",
                   "description": f"Panel {i + 1}", "data": {"width": 480, "height": 480}}])
    step_update(run, "c2", instances_closed=True, summary="All four panels rendered")
    step_update(run, "c3", status="completed",
                summary="Teaser cut at 19s — pacing needed two extra beats over the 15s brief",
                artifacts=[{"type": "video", "ref": "media/teaser-42.mp4",
                            "description": "Teaser cut",
                            "data": {"duration": "0:19", "width": 1920, "height": 1080}}])

    call("POST", f"/runs/{run}/amendments", {
        "proposed_by": "ffmpeg-agent", "kind": "forward",
        "reason": "The teaser came out at 19s — the brief said 15s, but the pacing needed two "
                  "extra beats. Review the panels and teaser, then approve to publish at 19s.",
        "operations": [{"op": "update_step", "target_step_id": "c4",
                        "step": task("c4", "Publish the strip and the 19-second teaser to the "
                                     "blog", "claude-code", ["c3"])}],
    })


def seed_song() -> None:
    wid = workflow("wf_song", "Translate “Northern Lights” to Spanish", [
        task("m1", "Transcribe the original lyrics and produce a literal gloss", "claude-code"),
        task("m2", "Write singable Spanish lyrics preserving rhyme and meter", "claude-code",
             ["m1"]),
        task("m3", "Synthesize a guide vocal over the instrumental", "voicelab", ["m2"]),
        task("m4", "Master the final mix", "voicelab", ["m3"]),
    ])
    run = call("POST", f"/workflows/{wid}/runs", {"run_id": "run_d2e8"})["run_id"]

    step_update(run, "m1", status="completed",
                summary="Lyrics transcribed and glossed, 3 verses + chorus",
                artifacts=[{**md(
                    "## Verse 1 — literal gloss\n"
                    "Cold sky, green fire overhead → cielo frío, fuego verde en lo alto\n"
                    "We drove north until the road ran out → condujimos al norte hasta que la "
                    "carretera se acabó"
                ), "ref": "song/gloss.md", "description": "Gloss (excerpt)"}])
    step_update(run, "m2", status="completed", summary="Singable Spanish lyrics drafted",
                artifacts=[{**md(
                    "## Coro (ES)\nBajo un cielo que arde en verde\ncanto lo que el frío pierde"
                ), "ref": "song/lyrics-es.md", "description": "Spanish lyrics (excerpt)"}])
    step_update(run, "m3", status="completed", summary="Guide vocal synthesized, 3:12",
                artifacts=[{"type": "audio", "ref": "media/guide-vocal-es.mp3",
                            "description": "Guide vocal (ES)", "data": {"duration": "3:12"}}])

    call("POST", f"/runs/{run}/amendments", {
        "proposed_by": "voicelab", "kind": "history_edit",
        "reason": "Chorus stresses land off the beat in the guide vocal. Replay the lyric step "
                  "with a per-line syllable-count constraint.",
        "operations": [
            {"op": "update_step", "target_step_id": "m2",
             "step": task("m2", "Write singable Spanish lyrics; match syllable count and stress "
                          "per line", "claude-code", ["m1"])},
            {"op": "replay_step", "target_step_id": "m2"},
        ],
    })


def seed_triage() -> None:
    wid = workflow("wf_triage", "Nightly repo triage", [
        task("s1", "Fetch all open issues and PRs updated in the last 24h", "claude-code"),
        construct("s2", "loop", "Triage each fetched issue: classify, then draft a reply",
                  "claude-code", ["s2a"], ["s1"]),
        task("s2a", "Classify and draft a reply", "claude-code"),
        task("s3", "Summarize triage decisions into a report", "claude-code", ["s2"]),
    ])
    run = call("POST", f"/workflows/{wid}/runs", {"run_id": "run_e77d"})["run_id"]

    step_update(run, "s1", status="completed", summary="Pulled 9 open issues",
                artifacts=[{"type": "json", "ref": "artifacts/issues-2026-08-13.json",
                            "description": "Fetched issue list"}])
    instance(run, "s2", "iteration", 0, "#4790 handled — bug; reply drafted")
    instance(run, "s2", "iteration", 1, "#4793 handled — spam; closed with note")
    step_update(run, "s2", instances_closed=True, summary="Two issues triaged")
    step_update(run, "s3", status="completed", summary="Report posted to #triage",
                artifacts=[{"type": "pr", "ref": "https://github.com/acme/repo/pull/4788",
                            "description": "Triage report PR"}])


def main() -> int:
    global BASE
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base", default=BASE, help="API base URL including the /v1 prefix")
    BASE = ap.parse_args().base.rstrip("/")

    seeded = 0
    for seed in (seed_ideas, seed_ablation, seed_comic, seed_song, seed_triage):
        name = seed.__name__.removeprefix("seed_")
        try:
            seed()
        except AlreadySeeded as exc:
            print(f"skipped {name} — workflow '{exc.args[0]}' is already there")
            continue
        seeded += 1
        print(f"seeded {name}")
    if not seeded:
        print("\nNothing new. The demo ids are fixed, so re-seeding needs a fresh database:\n"
              "  stop the server, delete the sqlite file, start it again.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
