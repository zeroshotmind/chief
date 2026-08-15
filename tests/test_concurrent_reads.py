"""Concurrent reads on the shared connection (REQ-16/REQ-18 fallout).

The web UI fans several GETs out at once — the runs list, the workflow index and one
amendment list per run all leave together. FastAPI runs the sync handlers on a threadpool,
so those land on the one shared sqlite3 connection simultaneously. Before reads took the
store lock this produced ``InterfaceError: bad parameter or other API misuse`` and, worse,
silent cross-talk: a request answering with another request's rows, which surfaced as
spurious 404s for runs that plainly exist.

The failure is load-dependent, so this exercises it directly rather than trusting a single
sequential pass.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient

from .conftest import Api, construct, task


def test_parallel_reads_across_runs_stay_correct(api: Api, client: TestClient) -> None:
    expected: dict[str, str] = {}
    for n in range(6):
        workflow_id, run_id = api.run(
            [task(f"a{n}"), construct(f"b{n}", "loop", [f"c{n}"], depends_on=[f"a{n}"]),
             task(f"c{n}")],
            title=f"workflow {n}",
        )
        api.update_step(run_id, f"a{n}", status="completed")
        api.propose(run_id, [{"op": "update_step", "target_step_id": f"c{n}",
                              "step": task(f"c{n}", harness="other")}])
        expected[run_id] = workflow_id

    paths = ["/v1/runs", "/v1/workflows"]
    for run_id in expected:
        paths += [f"/v1/runs/{run_id}", f"/v1/runs/{run_id}/definition",
                  f"/v1/runs/{run_id}/amendments", "/v1/audit"]

    with ThreadPoolExecutor(max_workers=12) as pool:
        # Several passes: the interleaving that corrupts a cursor is timing-dependent.
        for _ in range(6):
            responses = list(pool.map(lambda p: (p, client.get(p)), paths))
            for path, response in responses:
                assert response.status_code == 200, (
                    f"{path} -> {response.status_code} {response.text}"
                )

    # And the answers are the right ones, not merely successful.
    for run_id, workflow_id in expected.items():
        assert client.get(f"/v1/runs/{run_id}").json()["workflow_id"] == workflow_id
        amendments = client.get(f"/v1/runs/{run_id}/amendments").json()
        assert [a["run_id"] for a in amendments] == [run_id]
