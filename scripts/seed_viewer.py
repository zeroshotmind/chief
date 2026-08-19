#!/usr/bin/env python
"""Seed a workflow whose artifacts are real files, for exercising the file viewer.

Unlike the other seeders this one writes to the disk as well as to the API — it has to, since
the thing under test is Chief reading a file a harness reported. It creates a small tree of
the kinds of file a run actually leaves behind, then reports each of them as an artifact of a
run whose `origin_dir` is that tree.

    python scripts/seed_viewer.py --base http://127.0.0.1:8080/v1

The tree defaults to `human-test/viewer-demo` under the repo, which `.gitignore` already
covers: it is output from exercising Chief, not part of it.

Every branch of the viewer is represented on purpose, the failures included — a missing file,
a path that climbs out of the working directory, and a URL. A demo where everything works
shows you the happy path and hides the four refusals that are most of the design.
"""

from __future__ import annotations

import argparse
import json
import struct
import sys
import urllib.error
import urllib.request
import zlib
from pathlib import Path

BASE = "http://127.0.0.1:8000/v1"
WORKFLOW_ID = "wf_viewer"

SWEEP_MD = r"""## Reward sweep, run 3

The curve is **flat** after step 3k, which rules out the schedule and leaves the advantage
estimator. Three configurations, one of which is in `src/train.py`.

The objective as implemented:

$$
J(\theta) = \mathbb{E}_{x \sim D}\left[ \sum_{t=1}^{T} \log \pi_\theta(a_t \mid s_t) A_t \right]
$$

with $A_t = R_t - b(s_t)$ and $\beta = 0.02$ on the KL term. Note that $\nabla_\theta J$ is
estimated per batch.

### What to try next

1. Whiten the advantages per batch
2. Drop $\beta$ to 0.01 and re-run the audit
3. If neither moves it, the bug is upstream in the reward

> The held-out split is stale. Regenerate it before trusting any of this.

```python
adv = (adv - adv.mean()) / (adv.std() + 1e-8)
loss = -(logp * adv).mean() + beta * kl
```

Maths this renderer does not cover still shows its own source:
$\begin{matrix} a & b \end{matrix}$.
"""

TRAIN_PY = '''"""Training loop for the sweep. Not run from here; kept as the artifact it was."""

import torch


def grpo_step(policy, batch, beta=0.02):
    logp = policy.log_prob(batch.actions, batch.states)
    adv = batch.rewards - batch.baseline
    adv = (adv - adv.mean()) / (adv.std() + 1e-8)
    kl = policy.kl_to_reference(batch.states)
    return -(logp * adv).mean() + beta * kl
'''

RUN_LOG = "\n".join(
    [f"[{i:04d}] step {i * 100:>6}  loss {2.71 - i * 0.03:.4f}  kl {0.004 + i * 0.0002:.4f}"
     for i in range(40)]
) + "\n[done] 4000 steps, 11m32s\n"

METRICS = {
    "run": "sweep-3",
    "steps": 4000,
    "final_loss": 1.53,
    "kl": 0.0118,
    "held_out": {"accuracy": 0.681, "baseline": 0.664, "stale": True},
    "shards": [{"id": i, "rows": 340 + i * 17, "review": i % 3} for i in range(12)],
}

REPORT_HTML = """<!doctype html>
<html><head><title>Report</title></head>
<body>
<h1>Sweep report</h1>
<script>alert('this must never run inside Chief')</script>
<p>Served as source, never as a page.</p>
</body></html>
"""


def png(width: int = 480, height: int = 240) -> bytes:
    """A loss curve, drawn by hand.

    Written out rather than shipped as a fixture, and without Pillow: the repo has four
    runtime dependencies and a demo image is not a reason for a fifth. `zlib` and `struct`
    are enough — a PNG is a header, a deflated bitmap and a CRC per chunk.
    """
    bg, grid, line, axis = (250, 250, 253), (228, 228, 238), (121, 108, 191), (170, 170, 186)
    rows = []
    for y in range(height):
        row = bytearray([0])  # filter byte: none
        for x in range(width):
            colour = bg
            if x % 60 == 0 or y % 40 == 0:
                colour = grid
            if x == 40 or y == height - 40:
                colour = axis
            # A decaying curve with a flat tail, which is what the write-up is about.
            if x > 40:
                t = (x - 40) / (width - 60)
                curve = height - 40 - int((height - 90) * (1 - 2.718 ** (-3.2 * t)) * 0.92)
                if abs(y - curve) <= 1:
                    colour = line
            row += bytes(colour)
        rows.append(bytes(row))

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(b"".join(rows), 9))
        + chunk(b"IEND", b"")
    )


def pdf() -> bytes:
    """A one-page PDF, likewise written out rather than depended on.

    The offsets in the cross-reference table have to be the real byte positions, so the
    objects are assembled first and measured as they go.
    """
    lines = [
        b"BT /F1 18 Tf 60 700 Td (Sweep report - run 3) Tj ET",
        b"BT /F1 11 Tf 60 670 Td (Flat after 3k steps. The estimator, not the schedule.) Tj ET",
        b"BT /F1 11 Tf 60 652 Td (Held-out 0.681 against a 0.664 baseline.) Tj ET",
        b"BT /F1 11 Tf 60 620 Td (This page exists to prove the viewer embeds a PDF.) Tj ET",
    ]
    stream = b"\n".join(lines)
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += str(number).encode() + b" 0 obj\n" + body + b"\nendobj\n"
    start = len(out)
    out += b"xref\n0 " + str(len(objects) + 1).encode() + b"\n0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        b"trailer\n<< /Size " + str(len(objects) + 1).encode() + b" /Root 1 0 R >>\n"
        b"startxref\n" + str(start).encode() + b"\n%%EOF\n"
    )
    return bytes(out)


def write_tree(root: Path) -> None:
    for folder in ("notes", "logs", "src", "data", "figures", "docs", "out"):
        (root / folder).mkdir(parents=True, exist_ok=True)
    (root / "notes" / "sweep.md").write_text(SWEEP_MD)
    (root / "logs" / "run.log").write_text(RUN_LOG)
    (root / "src" / "train.py").write_text(TRAIN_PY)
    (root / "data" / "metrics.json").write_text(json.dumps(METRICS, indent=2) + "\n")
    (root / "figures" / "curve.png").write_bytes(png())
    (root / "docs" / "summary.pdf").write_bytes(pdf())
    (root / "out" / "report.html").write_text(REPORT_HTML)
    (root / "out" / "checkpoint.bin").write_bytes(bytes(range(256)) * 40)


def call(method: str, path: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req) as resp:
        raw = resp.read()
        return json.loads(raw) if raw else {}


COMPONENTS_MDX = """\
---
title: "What the renderer does with MDX"
tags: ["mdx", "viewer"]
---

import { Callout } from "./Callout"
import { Counter } from "./Counter"

## Prose renders

Ordinary markdown is ordinary: **bold**, `code`, a [link](https://example.com), and maths
inline like $\\nabla_\\theta J(\\theta)$ or on its own lines:

$$
J(\\theta) = \\mathbb{E}\\left[ \\sum_{t=1}^{T} \\log \\pi_\\theta(a_t \\mid s_t) A_t \\right]
$$

## Components run

<Callout kind="warn">

This is a real component. Its children are **markdown**, which is what MDX means by
children — and it is styled by its own code, not by Chief.

</Callout>

And they keep their own state. Click it:

<Counter start={3} label="reruns" />

## Nothing becomes markup

<script>alert("this is text, not a script")</script>

```python
# a fenced block is verbatim, including <b>markup</b> and $maths$
loss = -(logp * adv).mean() + beta * kl
```
"""

CALLOUT_JSX = """\
export const Callout = ({ kind, children }) => (
  <aside
    className={"callout " + kind}
    style={{
      borderLeft: "3px solid #796cbf",
      background: "rgba(121,108,191,0.08)",
      padding: "8px 12px",
      borderRadius: "4px",
    }}
  >
    {children}
  </aside>
)
"""

COUNTER_JSX = """\
export function Counter({ start, label }) {
  const [n, setN] = useState(start)
  return (
    <button
      onClick={() => setN(n + 1)}
      style={{
        font: "inherit",
        padding: "4px 10px",
        borderRadius: "4px",
        border: "1px solid #796cbf",
        background: "transparent",
        cursor: "pointer",
      }}
    >
      {label}: {n}
    </button>
  )
}
"""

#: A checkout of a real MDX site, if you have one and want the demo to include a page from
#: it. Nothing here depends on it: pass --site to point at one, and the workflow gains an
#: artifact for a real document alongside the generated ones.
SITE_PAGE = "http://localhost:3000/"


def seed_mdx(root: Path, page: str, site: Path | None) -> bool:
    """MDX three ways: a file with every construct, a real one, and the page it becomes.

    The third is the point. A `.mdx` file read off the disk renders its prose and names its
    components, because the components live in a project Chief has never seen. The same page
    served by the dev server that *does* have them renders properly — so the workflow carries
    both, and the difference between them is the feature.
    """
    (root / "notes").mkdir(parents=True, exist_ok=True)
    (root / "notes" / "components.mdx").write_text(COMPONENTS_MDX)
    # Beside the document, which is the whole rule: the server derives the graph from what
    # the file imports and will not resolve anything outside this directory.
    (root / "notes" / "Callout.jsx").write_text(CALLOUT_JSX)
    (root / "notes" / "Counter.jsx").write_text(COUNTER_JSX)

    real = next(site.rglob("*.mdx"), None) if site else None
    steps = [
        {"id": "m1", "type": "task", "goal": "Write the page", "harness": "claude-code",
         "depends_on": []},
    ]
    try:
        call("POST", "/workflows", {
            "workflow_id": "wf_mdx", "title": "MDX: rendered, named, and framed",
            "source": "generated", "generated_by": "claude-code", "steps": steps,
            "project": "research",
            "origin_dir": str(real.parent if real else root),
        })
    except urllib.error.HTTPError as exc:
        if exc.code == 409:
            print("'wf_mdx' is already there — nothing to do", file=sys.stderr)
            return False
        print(f"POST /workflows -> {exc.code}\n{exc.read().decode()}", file=sys.stderr)
        return False
    call("POST", "/workflows/wf_mdx/approve", {"decided_by": "roy"})
    run = call("POST", "/workflows/wf_mdx/runs", {})["run_id"]

    artifacts = [
        # Absolute, so it resolves whatever origin_dir the workflow ended up with.
        {"type": "markdown", "ref": str(root / "notes" / "components.mdx"),
         "description": "MDX with its components beside it — they compile and run"},
        {"type": "page", "ref": page,
         "description": "The same kind of page, rendered by the dev server that has the "
                        "components — run `npm run dev` in the site repo to see it"},
    ]
    if real:
        artifacts.insert(1, {
            "type": "markdown", "ref": real.name,
            "description": f"A real write-up from {site.name}: frontmatter, prose, maths",
        })
    call("POST", f"/runs/{run}/steps/m1/updates", {"status": "running", "summary": "writing"})
    call("POST", f"/runs/{run}/steps/m1/updates", {
        "status": "completed",
        "summary": "One MDX file read off the disk, one page framed from the dev server — "
                   "the difference between them is what components cost.",
        "artifacts": artifacts,
    })
    return True


def seed_metadata() -> bool:
    """Metadata in every place it can be attached, so each one can be seen.

    There are four, and they are not the same thing. The run's is what triggered it. A
    step's accumulates across its updates. An instance's is what tells one branch from
    another. An artifact's `data` is the harness describing its own output. A plan has none
    — `inputs` is a value the plan states, not a record of what happened.
    """
    steps = [
        {"id": "m1", "type": "task", "goal": "Measure the baseline", "harness": "claude-code",
         "depends_on": [], "inputs": {"dataset": "held-out-v3", "note": "this is plan-time"}},
        {"id": "m2", "type": "parallel", "goal": "Sweep the variants", "harness": "claude-code",
         "depends_on": ["m1"], "body": ["m2a"], "on_instance_failure": "continue"},
        {"id": "m2a", "type": "task", "goal": "Train one variant", "harness": "claude-code",
         "depends_on": []},
    ]
    try:
        call("POST", "/workflows", {
            "workflow_id": "wf_meta", "title": "Metadata, everywhere it can be attached",
            "source": "generated", "generated_by": "claude-code", "steps": steps,
            "project": "research",
        })
    except urllib.error.HTTPError as exc:
        if exc.code == 409:
            print("'wf_meta' is already there — nothing to do", file=sys.stderr)
            return False
        raise
    call("POST", "/workflows/wf_meta/approve", {"decided_by": "roy"})
    # On the run: what set it going.
    run = call("POST", "/workflows/wf_meta/runs", {
        "metadata": {"trigger": "nightly", "commit": "9f2c1ab", "host": "workstation"},
    })["run_id"]

    # On a step, over two updates, to show that it merges rather than replaces.
    call("POST", f"/runs/{run}/steps/m1/updates", {
        "status": "running", "summary": "measuring", "metadata": {"started_by": "cron"},
    })
    call("POST", f"/runs/{run}/steps/m1/updates", {
        "status": "completed",
        "summary": "Baseline 0.664 on the held-out split",
        "metadata": {"accuracy": 0.664, "tokens": 41200, "cost_usd": 0.62,
                     "timing": {"wall_s": 91.4, "gpu_s": 88.1}},
        "artifacts": [
            # One here, because this workflow is about the four *levels* metadata attaches
            # to. The shapes `data` itself takes are wf_shapes' job.
            {"type": "file", "ref": "data/metrics.json", "description": "Baseline metrics",
             "data": {"rows": 4096, "sha256": "3b1f…9ac2", "schema": "metrics/v2"}},
        ],
    })

    # On each instance: what tells one branch from another.
    variants = [
        {"lr": 3e-4, "beta": 0.02, "seed": 7, "result": "flat after 3k"},
        {"lr": 1e-4, "beta": 0.02, "seed": 8, "result": "best: 0.681"},
        {"lr": 3e-4, "beta": 0.01, "seed": 9, "result": "unstable"},
    ]
    for index, variant in enumerate(variants):
        inst = call("POST", f"/runs/{run}/steps/m2/instances",
                    {"kind": "branch", "index": index})["instance_id"]
        call("POST", f"/runs/{run}/steps/m2/instances/{inst}/updates", {
            "status": "completed", "summary": variant.pop("result"), "metadata": variant,
        })
    call("POST", f"/runs/{run}/steps/m2/updates",
         {"instances_closed": True, "summary": "Three variants swept"})
    return True


def seed_shapes(root: Path) -> bool:
    """Every shape an artifact can take, side by side.

    `ArtifactRef` requires one of `ref` or `data`, and `data` is used for two different
    things — the artifact's own content, and facts about a file that lives elsewhere. The
    difference is invisible in the model and obvious on screen only when the cases sit
    together, which is what this is for. See CONTRACT-NOTES.md #38.
    """
    steps = [
        {"id": "a1", "type": "task", "goal": "Produce one of everything",
         "harness": "claude-code", "depends_on": []},
    ]
    try:
        call("POST", "/workflows", {
            "workflow_id": "wf_shapes", "title": "Artifact shapes: a ref, some facts, or both",
            "source": "generated", "generated_by": "claude-code", "steps": steps,
            # Set, so the artifacts that name a file can actually be opened. A shape demo
            # where the files do not resolve demonstrates half of each case.
            "project": "research", "origin_dir": str(root),
        })
    except urllib.error.HTTPError as exc:
        if exc.code == 409:
            print("'wf_shapes' is already there — nothing to do", file=sys.stderr)
            return False
        raise
    call("POST", "/workflows/wf_shapes/approve", {"decided_by": "roy"})
    run = call("POST", "/workflows/wf_shapes/runs", {})["run_id"]
    call("POST", f"/runs/{run}/steps/a1/updates", {"status": "running", "summary": "producing"})
    call("POST", f"/runs/{run}/steps/a1/updates", {
        "status": "completed",
        "summary": "Six artifacts, one of each shape an ArtifactRef can take",
        "artifacts": [
            # 1. A ref and nothing else — the plainest case, and the most common.
            {"type": "log", "ref": "logs/run.log", "description": "Just a file, no metadata"},
            # 2. A markdown file you can open, *and* facts about it. This is the case worth
            #    seeing: the card opens the document and reads its metadata at once, and
            #    neither is in the other's way.
            {"type": "markdown", "ref": "notes/sweep.md",
             "description": "A document you can open, with facts beside it",
             "data": {"words": 412, "sections": 3, "reviewed_by": "roy", "sha256": "9ac2…3b1f"}},
            # 3. The same for a binary the browser renders but Chief cannot read into text.
            {"type": "file", "ref": "docs/summary.pdf",
             "description": "A PDF, with what the harness knows about it",
             "data": {"pages": 1, "generated_by": "reportlab", "bytes": 853}},
            {"type": "file", "ref": "data/metrics.json",
             "description": "And a data file, described the same way",
             "data": {"rows": 4096, "sha256": "3b1f…9ac2", "schema": "metrics/v2"}},
            # 3. Content only: `data.text` is the artifact, and no such file exists.
            {"type": "markdown", "description": "The artifact IS the text — there is no file",
             "data": {"text": "## Baseline\n\nHeld-out accuracy **0.664**, the number every "
                              "variant has to beat. Inline maths works too: $A_t = R_t - "
                              "b(s_t)$.\n"}},
            # 4. Content and facts in the same dict, which is the conflation itself: `text`
            #    renders as the preview, `words` and `read_seconds` as facts beside it.
            {"type": "markdown", "description": "Content and facts in one field",
             "data": {"text": "## Next\n\n- Whiten the advantages\n- Drop the KL weight\n",
                      "words": 9, "read_seconds": 4}},
            # 5. No ref, no content: an artifact that is purely metadata. Legal because the
            #    model requires one of ref or data, not both.
            {"type": "measurement", "description": "Nothing to open — the facts are the point",
             "data": {"device": "M2 Max", "vram_gb": 32, "backend": "mps", "wall_s": 91.4}},
            # 6. Structure rather than scalars, which is what the fold is for.
            {"type": "report", "description": "Nested, so it folds instead of going inline",
             "data": {"per_shard": [{"id": i, "rows": 340 + i * 17} for i in range(4)],
                      "totals": {"rows": 1462, "review": 3}}},
        ],
    })
    return True


def seed_docs(root: Path) -> bool:
    """A document you can open, carrying facts about itself.

    The case wf_shapes could not be corrected into: artifacts cannot be appended to a step
    that has finished, so the markdown-and-PDF version of "a file *and* its metadata" needed
    a workflow of its own.
    """
    steps = [
        {"id": "d1", "type": "task", "goal": "Write it up and export it",
         "harness": "claude-code", "depends_on": []},
    ]
    try:
        call("POST", "/workflows", {
            "workflow_id": "wf_docs", "title": "Documents that carry their own metadata",
            "source": "generated", "generated_by": "claude-code", "steps": steps,
            "project": "research", "origin_dir": str(root),
        })
    except urllib.error.HTTPError as exc:
        if exc.code == 409:
            print("'wf_docs' is already there — nothing to do", file=sys.stderr)
            return False
        raise
    call("POST", "/workflows/wf_docs/approve", {"decided_by": "roy"})
    run = call("POST", "/workflows/wf_docs/runs", {})["run_id"]
    call("POST", f"/runs/{run}/steps/d1/updates", {"status": "running", "summary": "writing"})
    call("POST", f"/runs/{run}/steps/d1/updates", {
        "status": "completed",
        "summary": "The write-up, a PDF of it, and a figure — each with what the harness "
                   "knows about it",
        "artifacts": [
            {"type": "markdown", "ref": "notes/sweep.md",
             "description": "The write-up — opens here, and says what it is",
             "data": {"words": 412, "sections": 3, "reviewed_by": "roy",
                      "sha256": "9ac2…3b1f", "sources": ["run-3", "held-out-v3"],
                      "checks": {"spelling": "clean", "links": 4, "maths_rendered": 17}}},
            {"type": "file", "ref": "docs/summary.pdf",
             "description": "The same, exported — opens in the browser's own viewer",
             "data": {"pages": 1, "bytes": 853, "generated_by": "hand-rolled writer",
                      "fonts": ["Helvetica"]}},
            {"type": "image", "ref": "figures/curve.png",
             "description": "The loss curve, with how it was drawn",
             "data": {"width": 480, "height": 240, "drawn_from": "logs/run.log",
                      "palette": {"line": "#796cbf", "grid": "#e4e4ee"}}},
        ],
    })
    return True


def main() -> int:
    global BASE
    here = Path(__file__).resolve().parent.parent
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base", default=BASE, help="API base URL including the /v1 prefix")
    ap.add_argument(
        "--dir", default=str(here / "human-test" / "viewer-demo"),
        help="where to write the sample files; becomes the workflow's origin_dir",
    )
    ap.add_argument(
        "--site", default=None,
        help="a checkout containing .mdx files; the first one found is added as an artifact",
    )
    ap.add_argument(
        "--page", default=SITE_PAGE,
        help="a URL your dev server renders, framed in the viewer as a page artifact",
    )
    args = ap.parse_args()
    BASE = args.base.rstrip("/")
    root = Path(args.dir).expanduser().resolve()

    write_tree(root)
    print(f"wrote sample files under {root}")

    if seed_files(root):
        print(f"seeded {WORKFLOW_ID}")
        print(f"open: {BASE.removesuffix('/v1')}/ui/#/workflow/{WORKFLOW_ID}")
    # Deliberately not chained to the one above. Each workflow here has a fixed id, so one of
    # them already existing is the ordinary case on a second run — and it must not stop the
    # others being seeded, which is exactly what an early return did.
    if seed_mdx(root, args.page, Path(args.site).expanduser() if args.site else None):
        print("seeded wf_mdx")
        print(f"open: {BASE.removesuffix('/v1')}/ui/#/workflow/wf_mdx")
    if seed_metadata():
        print("seeded wf_meta")
        print(f"open: {BASE.removesuffix('/v1')}/ui/#/workflow/wf_meta")
    if seed_shapes(root):
        print("seeded wf_shapes")
        print(f"open: {BASE.removesuffix('/v1')}/ui/#/workflow/wf_shapes")
    if seed_docs(root):
        print("seeded wf_docs")
        print(f"open: {BASE.removesuffix('/v1')}/ui/#/workflow/wf_docs")
    return 0


def seed_files(root: Path) -> bool:
    """The viewer's own demo: every kind of artifact, and the refusals."""
    steps = [
        {"id": "v1", "type": "task", "goal": "Run the reward sweep and keep what it produced",
         "harness": "claude-code", "depends_on": []},
        {"id": "v2", "type": "task", "goal": "Write it up", "harness": "claude-code",
         "depends_on": ["v1"]},
    ]
    try:
        call("POST", "/workflows", {
            "workflow_id": WORKFLOW_ID, "title": "File viewer: every kind of artifact",
            "source": "generated", "generated_by": "claude-code", "steps": steps,
            "project": "research", "origin_dir": str(root),
        })
    except urllib.error.HTTPError as exc:
        if exc.code == 409:
            print(f"'{WORKFLOW_ID}' is already there — skipping it", file=sys.stderr)
            return False
        print(f"POST /workflows -> {exc.code}\n{exc.read().decode()}", file=sys.stderr)
        return False

    call("POST", f"/workflows/{WORKFLOW_ID}/approve", {"decided_by": "roy"})
    run = call("POST", f"/workflows/{WORKFLOW_ID}/runs", {})["run_id"]

    call("POST", f"/runs/{run}/steps/v1/updates", {"status": "running", "summary": "sweeping"})
    call("POST", f"/runs/{run}/steps/v1/updates", {
        "status": "completed",
        "summary": "4000 steps, flat after 3k — the estimator, not the schedule",
        "artifacts": [
            {"type": "log", "ref": "logs/run.log", "description": "Training log, 40 lines"},
            {"type": "image", "ref": "figures/curve.png",
             "description": "Loss curve, drawn from the run"},
            {"type": "file", "ref": "data/metrics.json",
             "description": "Final metrics and the per-shard breakdown"},
            {"type": "file", "ref": "src/train.py", "description": "The step function as run"},
            {"type": "file", "ref": "out/checkpoint.bin",
             "description": "Checkpoint — binary, nothing to preview"},
        ],
    })
    call("POST", f"/runs/{run}/steps/v2/updates", {"status": "running", "summary": "writing up"})
    call("POST", f"/runs/{run}/steps/v2/updates", {
        "status": "completed",
        "summary": "Write-up, a PDF of it, and three artifacts that cannot be previewed",
        "artifacts": [
            {"type": "markdown", "ref": "notes/sweep.md",
             "description": "The write-up, with maths in it"},
            {"type": "file", "ref": "docs/summary.pdf", "description": "The same, as a PDF"},
            {"type": "file", "ref": "out/report.html",
             "description": "HTML — shown as source, never as a page"},
            # The three refusals, on purpose. A demo where everything works hides most of
            # what the design is.
            {"type": "file", "ref": "out/never-written.md",
             "description": "A file the run did not actually leave behind"},
            {"type": "file", "ref": "../../../../etc/passwd",
             "description": "A path climbing out of the working directory"},
            {"type": "pr", "ref": "https://github.com/zeroshotmind/chief/pull/1",
             "description": "A URL, which is not a file at all"},
        ],
    })

    return True


if __name__ == "__main__":
    raise SystemExit(main())
