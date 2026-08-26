"""Reading the file an artifact names — the one route that touches the disk.

CONTRACT-NOTES.md #34. The property under test throughout is that the caller never supplies
a path: it names two ids Chief issued, and the path is derived from what a harness already
recorded. Most of what follows is the shape of the refusals, because on a service with no
auth the refusals are the design.
"""

from __future__ import annotations

import base64

import pytest

from .conftest import task

ONE_PIXEL_PNG = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhf"
    "DwAChwGA60e6kgAAAABJRU5ErkJggg=="
)


@pytest.fixture()
def tree(tmp_path):
    """A workflow's working directory, with the kinds of file a run leaves behind."""
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "notes.md").write_text("# Heading\n\nBody with $x^2$ in it.\n")
    (tmp_path / "out" / "run.log").write_text("line one\nline two\n")
    # A one-pixel PNG, so the image path is exercised on real bytes rather than a stub.
    # A real 1x1 PNG, so the image path is exercised on bytes a browser would accept.
    (tmp_path / "out" / "shot.png").write_bytes(base64.b64decode(ONE_PIXEL_PNG))
    (tmp_path / "out" / "page.html").write_text("<script>alert(1)</script>")
    (tmp_path / "out" / "post.mdx").write_text("import X from 'x'\n\n<Callout>hi</Callout>\n")
    (tmp_path / "secret.txt").write_text("not an artifact")
    return tmp_path


def run_with(api, tree, ref: str, *, origin: str | None = "use-tree"):
    """A completed run whose one artifact points at `ref`."""
    origin_dir = str(tree) if origin == "use-tree" else origin
    workflow_id = api.approved_workflow([task("s1")], origin_dir=origin_dir)
    run_id = api.client.post(f"/v1/workflows/{workflow_id}/runs", json={}).json()["run_id"]
    api.update_step(run_id, "s1", status="running")
    api.update_step(
        run_id, "s1", status="completed", artifacts=[{"type": "file", "ref": ref}]
    )
    state = api.client.get(f"/v1/runs/{run_id}").json()
    artifact_id = state["step_states"]["s1"]["artifacts"][0]["artifact_id"]
    return run_id, artifact_id


def fetch(api, run_id, artifact_id):
    return api.client.get(f"/v1/runs/{run_id}/artifacts/{artifact_id}/content")


# --- reading one --------------------------------------------------------------------------


def test_a_relative_ref_resolves_against_where_the_workflow_ran(api, tree):
    run_id, artifact_id = run_with(api, tree, "out/notes.md")
    response = fetch(api, run_id, artifact_id)
    assert response.status_code == 200, response.text
    assert response.content.decode().startswith("# Heading")


def test_an_absolute_ref_is_read_as_given(api, tree):
    run_id, artifact_id = run_with(api, tree, str(tree / "out" / "run.log"))
    assert fetch(api, run_id, artifact_id).content.decode() == "line one\nline two\n"


def test_bytes_come_back_intact(api, tree):
    """Not decoded, not re-encoded: an image has to survive the round trip byte for byte."""
    run_id, artifact_id = run_with(api, tree, "out/shot.png")
    response = fetch(api, run_id, artifact_id)
    assert response.content == (tree / "out" / "shot.png").read_bytes()
    assert response.headers["x-chief-media-type"] == "image/png"


def test_the_response_says_what_it_may_be_rendered_as_without_being_it(api, tree):
    """The type the browser may apply travels in a header; the body is always opaque.

    Serving a file under its own type from Chief's origin would let an artifact script the
    page that is reading the run. The UI re-types the bytes client-side instead.
    """
    run_id, artifact_id = run_with(api, tree, "out/notes.md")
    response = fetch(api, run_id, artifact_id)
    assert response.headers["content-type"] == "application/octet-stream"
    assert response.headers["x-chief-media-type"] == "text/markdown"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "attachment" in response.headers["content-disposition"]


def test_html_is_readable_but_the_wire_type_never_changes(api, tree):
    """HTML is on the allowlist and the UI renders it — in a sandboxed frame with no
    `allow-same-origin`, so a script in it can run but cannot reach Chief's own page. What
    this route promises does not bend for that: the response is still opaque bytes, the
    header is still only a hint, and the markup — script tag included — travels untouched,
    because sanitising it here would just be a second, weaker copy of what the sandbox
    already guarantees."""
    run_id, artifact_id = run_with(api, tree, "out/page.html")
    response = fetch(api, run_id, artifact_id)
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/octet-stream"
    assert response.headers["x-chief-media-type"] == "text/html"
    assert b"<script>" in response.content


def test_mdx_is_its_own_type(api, tree):
    """Markdown's renderer either way, but only MDX has components to be named as such."""
    run_id, artifact_id = run_with(api, tree, "out/post.mdx")
    response = fetch(api, run_id, artifact_id)
    assert response.status_code == 200
    assert response.headers["x-chief-media-type"] == "text/mdx"


def test_the_file_name_travels_with_it(api, tree):
    run_id, artifact_id = run_with(api, tree, "out/notes.md")
    assert fetch(api, run_id, artifact_id).headers["x-chief-file-name"] == "notes.md"


# --- what it refuses ----------------------------------------------------------------------


def test_a_ref_that_climbs_out_of_the_working_directory_is_refused(api, tree):
    """The one containment this has to enforce itself, since a relative ref is joined to a
    base rather than used as given."""
    run_id, artifact_id = run_with(api, tree, "out/../../etc/passwd")
    response = fetch(api, run_id, artifact_id)
    assert response.status_code == 422
    assert "climbs out" in response.json()["error"]["message"]


def test_a_ref_that_stays_inside_after_climbing_is_allowed(api, tree):
    """`out/../secret.txt` is still under the directory the workflow ran in. Refusing it
    would be refusing a path that resolves somewhere legitimate."""
    run_id, artifact_id = run_with(api, tree, "out/../secret.txt")
    assert fetch(api, run_id, artifact_id).status_code == 200


def test_a_relative_ref_with_no_recorded_directory_says_so(api, tree):
    run_id, artifact_id = run_with(api, tree, "out/notes.md", origin=None)
    response = fetch(api, run_id, artifact_id)
    assert response.status_code == 409
    assert "does not record where it ran" in response.json()["error"]["message"]


def test_a_url_artifact_is_not_a_file(api, tree):
    run_id, artifact_id = run_with(api, tree, "https://example.com/pr/1")
    response = fetch(api, run_id, artifact_id)
    assert response.status_code == 422
    assert "URL" in response.json()["error"]["message"]


def test_a_missing_file_is_a_404(api, tree):
    run_id, artifact_id = run_with(api, tree, "out/gone.md")
    assert fetch(api, run_id, artifact_id).status_code == 404


def test_a_directory_is_refused(api, tree):
    run_id, artifact_id = run_with(api, tree, "out")
    response = fetch(api, run_id, artifact_id)
    assert response.status_code == 422
    assert "directory" in response.json()["error"]["message"]


def test_a_file_over_the_limit_is_refused_with_its_size(api, tree, monkeypatch):
    from chief.domain import files

    monkeypatch.setattr(files, "MAX_BYTES", 8)
    run_id, artifact_id = run_with(api, tree, "out/run.log")
    response = fetch(api, run_id, artifact_id)
    assert response.status_code == 422
    detail = response.json()["error"]["details"]
    assert detail["size"] == 18
    assert detail["limit"] == 8


def test_an_artifact_that_is_not_there_is_a_404(api, tree):
    run_id, _ = run_with(api, tree, "out/notes.md")
    assert fetch(api, run_id, "art_nope").status_code == 404


# --- the module graph ---------------------------------------------------------------------


@pytest.fixture()
def mdx_tree(tree):
    (tree / "docs").mkdir()
    (tree / "docs" / "post.mdx").write_text(
        'import { Callout } from "./Callout"\n'
        'import Chart from "./Chart"\n'
        'import React from "react"\n\n'
        "## Findings\n\n<Callout>text</Callout>\n"
    )
    (tree / "docs" / "Callout.jsx").write_text(
        'import { tint } from "./tint"\n'
        "export const Callout = ({children}) => <aside>{children}</aside>"
    )
    (tree / "docs" / "Chart.jsx").write_text("export default () => null")
    (tree / "docs" / "tint.js").write_text("export const tint = 1")
    (tree / "docs" / "secret.env").write_text("TOKEN=hunter2")
    return tree


def modules(api, run_id, artifact_id):
    return api.client.get(f"/v1/runs/{run_id}/artifacts/{artifact_id}/modules")


def test_the_graph_is_the_document_and_what_it_imports(api, mdx_tree):
    run_id, artifact_id = run_with(api, mdx_tree, "docs/post.mdx")
    response = modules(api, run_id, artifact_id)
    assert response.status_code == 200, response.text
    graph = response.json()["modules"]
    # The entry under its own name, each import under the specifier that named it, and the
    # transitive one the component itself imports.
    assert set(graph) == {"post.mdx", "./Callout", "./Chart", "./tint"}
    assert "Findings" in graph["post.mdx"]
    assert "aside" in graph["./Callout"]


def test_a_bare_specifier_is_left_for_the_runtime(api, mdx_tree):
    """`react` is not a file beside the document, and not an error either — the runtime
    supplies what a component needs."""
    run_id, artifact_id = run_with(api, mdx_tree, "docs/post.mdx")
    assert "react" not in modules(api, run_id, artifact_id).json()["modules"]


def test_nothing_outside_the_document_s_own_directory_is_reachable(api, mdx_tree):
    """The whole containment. `../` and nested paths are not resolved at all, so a document
    cannot reach a sibling directory, let alone the tree above it."""
    (mdx_tree / "docs" / "escape.mdx").write_text(
        'import a from "../secret"\nimport b from "./sub/deep"\nimport c from "/etc/passwd"\n'
    )
    run_id, artifact_id = run_with(api, mdx_tree, "docs/escape.mdx")
    assert set(modules(api, run_id, artifact_id).json()["modules"]) == {"escape.mdx"}


def test_only_module_suffixes_are_served(api, mdx_tree):
    """A `.env` beside a document is not a module, and asking for it as one is refused
    rather than quietly served as text."""
    (mdx_tree / "docs" / "bad.mdx").write_text('import s from "./secret.env"\n')
    run_id, artifact_id = run_with(api, mdx_tree, "docs/bad.mdx")
    response = modules(api, run_id, artifact_id)
    assert response.status_code == 422
    assert "not a module" in response.json()["error"]["message"]


def test_a_missing_sibling_is_not_a_server_error(api, mdx_tree):
    """The runtime says which import it could not satisfy; the server has nothing to add."""
    (mdx_tree / "docs" / "gap.mdx").write_text('import x from "./nope"\n')
    run_id, artifact_id = run_with(api, mdx_tree, "docs/gap.mdx")
    assert set(modules(api, run_id, artifact_id).json()["modules"]) == {"gap.mdx"}


def test_a_cycle_terminates(api, mdx_tree):
    (mdx_tree / "docs" / "a.jsx").write_text('import b from "./b"\nexport const a = 1')
    (mdx_tree / "docs" / "b.jsx").write_text('import a from "./a"\nexport const b = 2')
    (mdx_tree / "docs" / "cycle.mdx").write_text('import a from "./a"\n')
    run_id, artifact_id = run_with(api, mdx_tree, "docs/cycle.mdx")
    assert set(modules(api, run_id, artifact_id).json()["modules"]) == {"cycle.mdx", "./a", "./b"}


def test_only_a_document_has_a_graph(api, mdx_tree):
    run_id, artifact_id = run_with(api, mdx_tree, "out/run.log")
    response = modules(api, run_id, artifact_id)
    assert response.status_code == 422
    assert "only an .mdx" in response.json()["error"]["message"]


def test_the_graph_is_behind_the_same_host_guard(api, mdx_tree):
    run_id, artifact_id = run_with(api, mdx_tree, "docs/post.mdx")
    response = api.client.get(
        f"/v1/runs/{run_id}/artifacts/{artifact_id}/modules",
        headers={"host": "evil.example.com"},
    )
    assert response.status_code == 422


# --- the rebinding guard ------------------------------------------------------------------


def test_a_foreign_host_header_is_refused(api, tree):
    """A page on the open web pointing its own domain at 127.0.0.1 so a fetch looks
    same-origin. The MCP transport already blocks this; it matters more on the one route
    that returns bytes off the disk.
    """
    run_id, artifact_id = run_with(api, tree, "out/notes.md")
    response = api.client.get(
        f"/v1/runs/{run_id}/artifacts/{artifact_id}/content",
        headers={"host": "evil.example.com"},
    )
    assert response.status_code == 422
    assert "not served to host" in response.json()["error"]["message"]


def test_loopback_hosts_are_served(api, tree):
    run_id, artifact_id = run_with(api, tree, "out/notes.md")
    for host in ("localhost:8080", "127.0.0.1:8080", "localhost"):
        response = api.client.get(
            f"/v1/runs/{run_id}/artifacts/{artifact_id}/content", headers={"host": host}
        )
        assert response.status_code == 200, f"{host}: {response.text}"


def test_an_extra_host_can_be_allowed(api, tree, monkeypatch):
    """For a UI reached under a name — a tunnel endpoint, a container alias."""
    monkeypatch.setenv("CHIEF_ALLOW_HOSTS", "chief.internal")
    run_id, artifact_id = run_with(api, tree, "out/notes.md")
    response = api.client.get(
        f"/v1/runs/{run_id}/artifacts/{artifact_id}/content",
        headers={"host": "chief.internal:8080"},
    )
    assert response.status_code == 200, response.text


# --- what is reachable at all -------------------------------------------------------------


def test_only_files_an_artifact_names_are_reachable(api, tree):
    """The property the whole design turns on.

    There is no path parameter, so `secret.txt` is not addressable — not because a check
    refuses it, but because there is no way to ask for it. The only way in is an artifact id
    for an artifact whose ref already points there.
    """
    run_id, artifact_id = run_with(api, tree, "out/notes.md")
    # Every shape of "give me that other file instead" is a 404 on the id, not a read.
    for attempt in (
        f"/v1/runs/{run_id}/artifacts/{artifact_id}/content?path=/etc/passwd",
        f"/v1/runs/{run_id}/artifacts/../../etc/passwd/content",
    ):
        response = api.client.get(attempt)
        assert response.status_code in (200, 404), response.status_code
        if response.status_code == 200:
            # The query parameter is ignored, not honoured.
            assert response.content.decode().startswith("# Heading")
