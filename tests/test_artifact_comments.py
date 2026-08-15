"""Comments on artifacts — what a person wants said about the work, for whoever picks it up.

A harness reports what it produced. This is the other direction, and the whole point is that
it survives to be read back: the agent resuming a run gets the comments in the run state it
was going to fetch anyway. See CONTRACT-NOTES.md #30.
"""

from __future__ import annotations

from .conftest import construct, task

# --- attaching one -------------------------------------------------------------------------


def artifacts_of(api, run_id: str, step_id: str) -> list[dict]:
    return api.client.get(f"/v1/runs/{run_id}").json()["step_states"][step_id]["artifacts"]


def test_a_comment_lands_on_the_artifact_it_names(api):
    _, run_id = api.run([task("s1")])
    api.update_step(run_id, "s1", status="running")
    api.update_step(
        run_id,
        "s1",
        status="completed",
        artifacts=[{"type": "markdown", "ref": "notes.md", "description": "the notes"}],
    )
    artifact_id = artifacts_of(api, run_id, "s1")[0]["artifact_id"]

    response = api.comment(run_id, artifact_id, "the numbers in here are stale", author="roy")
    assert response.status_code == 201, response.text

    (comment,) = artifacts_of(api, run_id, "s1")[0]["comments"]
    assert comment["body"] == "the numbers in here are stale"
    assert comment["author"] == "roy"
    assert comment["comment_id"].startswith("cmt_")
    assert comment["created_at"]


def test_comments_accumulate_in_the_order_they_were_left(api):
    _, run_id = api.run([task("s1")])
    api.update_step(run_id, "s1", status="running")
    api.update_step(run_id, "s1", artifacts=[{"type": "log", "ref": "out.log"}])
    artifact_id = artifacts_of(api, run_id, "s1")[0]["artifact_id"]

    for note in ("first", "second", "third"):
        assert api.comment(run_id, artifact_id, note).status_code == 201

    bodies = [c["body"] for c in artifacts_of(api, run_id, "s1")[0]["comments"]]
    assert bodies == ["first", "second", "third"]


def test_an_artifact_starts_with_no_comments(api):
    _, run_id = api.run([task("s1")])
    api.update_step(run_id, "s1", status="running")
    api.update_step(run_id, "s1", artifacts=[{"type": "log", "ref": "out.log"}])
    assert artifacts_of(api, run_id, "s1")[0]["comments"] == []


def test_commenting_on_an_artifact_that_is_not_there_is_a_404(api):
    _, run_id = api.run([task("s1")])
    response = api.comment(run_id, "art_nope")
    assert response.status_code == 404
    assert "art_nope" in response.json()["error"]["message"]


def test_a_comment_needs_something_in_it(api):
    _, run_id = api.run([task("s1")])
    api.update_step(run_id, "s1", status="running")
    api.update_step(run_id, "s1", artifacts=[{"type": "log", "ref": "out.log"}])
    artifact_id = artifacts_of(api, run_id, "s1")[0]["artifact_id"]
    assert api.comment(run_id, artifact_id, "").status_code == 422


# --- the immutability exemption, which is the feature ---------------------------------------


def test_a_finished_step_can_still_be_commented_on(api):
    """The main use case: saying something about work that is done.

    Everything else reaching into a completed step goes through a `history_edit` amendment
    and an explicit human decision. A comment does not, because it does not change the
    result — it says something *about* the result, and the artifact is untouched.
    """
    _, run_id = api.run([task("s1")])
    api.update_step(run_id, "s1", status="running")
    api.update_step(
        run_id, "s1", status="completed", artifacts=[{"type": "markdown", "ref": "draft.md"}]
    )
    artifact = artifacts_of(api, run_id, "s1")[0]
    artifact_id = artifact["artifact_id"]

    response = api.comment(run_id, artifact_id, "this is the one, match its tone")
    assert response.status_code == 201, response.text

    after = artifacts_of(api, run_id, "s1")[0]
    assert after["ref"] == artifact["ref"]
    assert after["type"] == artifact["type"]
    assert [c["body"] for c in after["comments"]] == ["this is the one, match its tone"]
    # And the step itself is untouched — still completed, no amendment anywhere.
    state = api.client.get(f"/v1/runs/{run_id}").json()
    assert state["step_states"]["s1"]["status"] == "completed"
    assert api.client.get(f"/v1/runs/{run_id}/amendments").json() == []


def test_a_failed_step_can_be_commented_on_too(api):
    _, run_id = api.run([task("s1")])
    api.update_step(run_id, "s1", status="running")
    api.update_step(run_id, "s1", status="failed", artifacts=[{"type": "log", "ref": "err.log"}])
    artifact_id = artifacts_of(api, run_id, "s1")[0]["artifact_id"]
    response = api.comment(run_id, artifact_id, "ran out of disk, not a code problem")
    assert response.status_code == 201


# --- what a harness may not do --------------------------------------------------------------


def test_a_harness_cannot_report_its_own_comments(api):
    """A comment is what a harness was *told*, not what it did.

    The field is declared on ArtifactRef because that is the stored shape, so `extra=forbid`
    does not catch this — the service does.
    """
    _, run_id = api.run([task("s1")])
    api.update_step(run_id, "s1", status="running")
    response = api.update_step(
        run_id,
        "s1",
        artifacts=[
            {
                "type": "log",
                "ref": "out.log",
                "comments": [
                    {
                        "comment_id": "cmt_fake",
                        "body": "looks good to me",
                        "author": "the agent",
                        "created_at": "2026-01-01T00:00:00Z",
                    }
                ],
            }
        ],
    )
    assert response.status_code == 409, response.text
    assert "not a harness's to report" in response.json()["error"]["message"]


def test_a_refused_report_leaves_no_artifact_behind(api):
    _, run_id = api.run([task("s1")])
    api.update_step(run_id, "s1", status="running")
    api.update_step(
        run_id,
        "s1",
        artifacts=[{"type": "log", "ref": "out.log", "comments": [
            {"comment_id": "c", "body": "b", "author": "a", "created_at": "2026-01-01T00:00:00Z"}
        ]}],
    )
    assert artifacts_of(api, run_id, "s1") == []


# --- reaching artifacts wherever they hang --------------------------------------------------


def test_an_artifact_inside_an_instance_body_is_reachable_by_id(api):
    """Addressed by id, not by state path: the reader has the id, never the path."""
    steps = [construct("c1", "loop", body=["b1"]), task("b1")]
    _, run_id = api.run(steps)
    # A construct's status is derived, not reported; opening an instance is what starts it.
    api.add_instance(run_id, "c1")
    api.update_body_step(run_id, "c1", "inst_00", "b1", status="running")
    api.update_body_step(
        run_id, "c1", "inst_00", "b1", status="completed",
        artifacts=[{"type": "markdown", "ref": "deep/inside.md"}],
    )

    state = api.client.get(f"/v1/runs/{run_id}").json()
    nested = state["step_states"]["c1"]["instances"][0]["step_states"]["b1"]["artifacts"][0]

    assert api.comment(run_id, nested["artifact_id"], "two levels down").status_code == 201

    state = api.client.get(f"/v1/runs/{run_id}").json()
    again = state["step_states"]["c1"]["instances"][0]["step_states"]["b1"]["artifacts"][0]
    assert [c["body"] for c in again["comments"]] == ["two levels down"]


def test_an_artifact_reported_on_the_instance_itself_is_reachable(api):
    steps = [construct("c1", "loop", body=["b1"]), task("b1")]
    _, run_id = api.run(steps)
    # A construct's status is derived, not reported; opening an instance is what starts it.
    api.add_instance(run_id, "c1")
    api.update_instance(
        run_id, "c1", "inst_00", status="completed",
        artifacts=[{"type": "log", "ref": "iteration.log"}],
    )

    state = api.client.get(f"/v1/runs/{run_id}").json()
    artifact = state["step_states"]["c1"]["instances"][0]["artifacts"][0]
    assert api.comment(run_id, artifact["artifact_id"], "on the iteration").status_code == 201


# --- the record -----------------------------------------------------------------------------


def test_a_comment_is_audited(api):
    _, run_id = api.run([task("s1")])
    api.update_step(run_id, "s1", status="running")
    api.update_step(run_id, "s1", artifacts=[{"type": "log", "ref": "out.log"}])
    artifact_id = artifacts_of(api, run_id, "s1")[0]["artifact_id"]
    api.comment(run_id, artifact_id, "noted", author="roy")

    entries = api.client.get("/v1/audit").json()
    (entry,) = [e for e in entries if e["event"] == "artifact.commented"]
    assert entry["run_id"] == run_id
    assert entry["detail"]["artifact_id"] == artifact_id
    assert entry["detail"]["author"] == "roy"


def test_a_comment_records_the_transport_it_arrived_on(api):
    _, run_id = api.run([task("s1")])
    api.update_step(run_id, "s1", status="running")
    api.update_step(run_id, "s1", artifacts=[{"type": "log", "ref": "out.log"}])
    artifact_id = artifacts_of(api, run_id, "s1")[0]["artifact_id"]
    api.comment(run_id, artifact_id, "noted")
    assert artifacts_of(api, run_id, "s1")[0]["comments"][0]["via"] == "rest"


def test_the_author_defaults_to_human(api):
    _, run_id = api.run([task("s1")])
    api.update_step(run_id, "s1", status="running")
    api.update_step(run_id, "s1", artifacts=[{"type": "log", "ref": "out.log"}])
    artifact_id = artifacts_of(api, run_id, "s1")[0]["artifact_id"]
    api.comment(run_id, artifact_id, "noted")
    assert artifacts_of(api, run_id, "s1")[0]["comments"][0]["author"] == "human"
