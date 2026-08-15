"""SQLite document store.

The contract leaves the storage engine open (Open Item 2) but assumes lookups by
workflow_id / run_id / step_id. SQLite fits a local single-user tool (REQ-44, REQ-21): one
file, no daemon, transactional, and shipped with Python.

Documents are stored as JSON with the identifying columns lifted out for indexing. Step
state is nested inside its run document, which is what the contract's shape describes and
what every read path wants anyway — a run is always read whole.

All writes go through :meth:`Store.transaction`, which holds a lock for the duration. That
is enough for a single-user local service and gives the amendment engine the atomicity the
contract demands without a second consistency mechanism.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from ..errors import NotFound
from ..ids import now
from ..models import Amendment, ApprovalPolicy, RunState, WorkflowDefinition, WorkflowTemplate
from ..transport import current_transport

SCHEMA = """
CREATE TABLE IF NOT EXISTS workflows (
    workflow_id      TEXT PRIMARY KEY,
    status           TEXT NOT NULL,
    version          INTEGER NOT NULL,
    retired_step_ids TEXT NOT NULL DEFAULT '[]',
    document         TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS workflow_versions (
    workflow_id  TEXT NOT NULL,
    version      INTEGER NOT NULL,
    amendment_id TEXT,
    document     TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    PRIMARY KEY (workflow_id, version)
);

CREATE TABLE IF NOT EXISTS runs (
    run_id      TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL,
    status      TEXT NOT NULL,
    document    TEXT NOT NULL,
    -- The plan this run is actually executing: base_version plus only its own approved
    -- amendments (contract 1.3). Deliberately not the workflow's current definition.
    effective   TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS runs_by_workflow ON runs (workflow_id);

CREATE TABLE IF NOT EXISTS amendments (
    amendment_id TEXT PRIMARY KEY,
    run_id       TEXT NOT NULL,
    workflow_id  TEXT NOT NULL,
    status       TEXT NOT NULL,
    document     TEXT NOT NULL,
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS amendments_by_run ON amendments (run_id, status);

CREATE TABLE IF NOT EXISTS templates (
    template_id TEXT PRIMARY KEY,
    status      TEXT NOT NULL,
    document    TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS config (
    key      TEXT PRIMARY KEY,
    document TEXT NOT NULL
);

-- Append-only; no update or delete path exists in this module (REQ-20).
CREATE TABLE IF NOT EXISTS audit_log (
    seq          INTEGER PRIMARY KEY AUTOINCREMENT,
    at           TEXT NOT NULL,
    event        TEXT NOT NULL,
    workflow_id  TEXT,
    run_id       TEXT,
    amendment_id TEXT,
    detail       TEXT NOT NULL
);
"""


class Store:
    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        with self._conn:
            self._conn.executescript(SCHEMA)

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock, self._conn:
            yield self._conn

    # One connection is shared across threads (``check_same_thread=False``), and FastAPI
    # runs sync endpoints on a threadpool — so a client that issues several reads at once
    # lands them on the same connection concurrently. Cursors are not connection-safe
    # under that: interleaved execute/fetch raises InterfaceError or hands one caller
    # another's rows. Reads therefore take the same lock writes do, and hold it until the
    # rows are materialised. The lock is reentrant, so a read inside a transaction is fine.

    def _one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def _all(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    # --- workflows ----------------------------------------------------------------------

    # Timestamps live in the table's own columns, so the stored document must not carry a
    # second copy that could drift from them.
    _STAMPS = {"created_at", "updated_at"}

    @staticmethod
    def _stamped(row: sqlite3.Row) -> WorkflowDefinition:
        """The document as stored, with the record's timestamps put back on it."""
        defn = WorkflowDefinition.model_validate_json(row["document"])
        return defn.model_copy(
            update={"created_at": row["created_at"], "updated_at": row["updated_at"]}
        )

    def create_workflow(self, defn: WorkflowDefinition) -> None:
        stamp = now()
        payload = defn.model_dump_json(exclude=self._STAMPS)
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO workflows (workflow_id, status, version, retired_step_ids, "
                "document, created_at, updated_at) VALUES (?, ?, ?, '[]', ?, ?, ?)",
                (defn.workflow_id, defn.status, defn.version, payload, stamp, stamp),
            )
            conn.execute(
                "INSERT INTO workflow_versions (workflow_id, version, amendment_id, document, "
                "created_at) VALUES (?, ?, NULL, ?, ?)",
                (defn.workflow_id, defn.version, payload, stamp),
            )
        # The caller holds the object it just handed us and will return it to a client, so
        # it gets the stamps the row was written with rather than a None or a stale pair.
        defn.created_at = defn.updated_at = stamp

    def workflow_exists(self, workflow_id: str) -> bool:
        row = self._one("SELECT 1 FROM workflows WHERE workflow_id = ?", (workflow_id,))
        return row is not None

    def get_workflow(self, workflow_id: str) -> WorkflowDefinition:
        row = self._one(
            "SELECT document, created_at, updated_at FROM workflows WHERE workflow_id = ?",
            (workflow_id,),
        )
        if row is None:
            raise NotFound(f"workflow '{workflow_id}' not found")
        return self._stamped(row)

    def get_workflow_version(self, workflow_id: str, version: int) -> WorkflowDefinition:
        row = self._one(
            "SELECT document, created_at, created_at AS updated_at FROM workflow_versions "
            "WHERE workflow_id = ? AND version = ?",
            (workflow_id, version),
        )
        if row is None:
            if not self.workflow_exists(workflow_id):
                raise NotFound(f"workflow '{workflow_id}' not found")
            raise NotFound(f"workflow '{workflow_id}' has no version {version}")
        return self._stamped(row)

    def retired_step_ids(self, workflow_id: str) -> set[str]:
        row = self._one(
            "SELECT retired_step_ids FROM workflows WHERE workflow_id = ?", (workflow_id,)
        )
        if row is None:
            raise NotFound(f"workflow '{workflow_id}' not found")
        return set(json.loads(row["retired_step_ids"]))

    def save_workflow(
        self,
        conn: sqlite3.Connection,
        defn: WorkflowDefinition,
        *,
        retired: set[str] | None = None,
        new_version_from: str | None = None,
    ) -> None:
        stamp = now()
        payload = defn.model_dump_json(exclude=self._STAMPS)
        if retired is None:
            conn.execute(
                "UPDATE workflows SET status = ?, version = ?, document = ?, updated_at = ? "
                "WHERE workflow_id = ?",
                (defn.status, defn.version, payload, stamp, defn.workflow_id),
            )
        else:
            conn.execute(
                "UPDATE workflows SET status = ?, version = ?, document = ?, "
                "retired_step_ids = ?, updated_at = ? WHERE workflow_id = ?",
                (
                    defn.status,
                    defn.version,
                    payload,
                    json.dumps(sorted(retired)),
                    stamp,
                    defn.workflow_id,
                ),
            )
        if new_version_from is not None:
            conn.execute(
                "INSERT OR REPLACE INTO workflow_versions (workflow_id, version, amendment_id, "
                "document, created_at) VALUES (?, ?, ?, ?, ?)",
                (defn.workflow_id, defn.version, new_version_from, payload, stamp),
            )
        defn.updated_at = stamp

    def list_workflows(self, status: str | None = None) -> list[WorkflowDefinition]:
        if status:
            rows = self._all(
                "SELECT document, created_at, updated_at FROM workflows WHERE status = ? "
                "ORDER BY created_at, workflow_id",
                (status,),
            )
        else:
            rows = self._all(
                "SELECT document, created_at, updated_at FROM workflows "
                "ORDER BY created_at, workflow_id"
            )
        return [self._stamped(r) for r in rows]

    # --- runs ---------------------------------------------------------------------------

    def create_run(
        self, conn: sqlite3.Connection, run: RunState, effective: WorkflowDefinition
    ) -> None:
        conn.execute(
            "INSERT INTO runs (run_id, workflow_id, status, document, effective, created_at, "
            "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                run.run_id,
                run.workflow_id,
                run.status,
                run.model_dump_json(),
                effective.model_dump_json(),
                run.created_at,
                run.updated_at,
            ),
        )

    def run_exists(self, run_id: str) -> bool:
        return (
            self._one("SELECT 1 FROM runs WHERE run_id = ?", (run_id,))
            is not None
        )

    def get_run(self, run_id: str) -> tuple[RunState, WorkflowDefinition]:
        row = self._one(
            "SELECT document, effective FROM runs WHERE run_id = ?", (run_id,)
        )
        if row is None:
            raise NotFound(f"run '{run_id}' not found")
        return (
            RunState.model_validate_json(row["document"]),
            WorkflowDefinition.model_validate_json(row["effective"]),
        )

    def save_run(
        self,
        conn: sqlite3.Connection,
        run: RunState,
        effective: WorkflowDefinition | None = None,
    ) -> None:
        if effective is None:
            conn.execute(
                "UPDATE runs SET status = ?, document = ?, updated_at = ? WHERE run_id = ?",
                (run.status, run.model_dump_json(), run.updated_at, run.run_id),
            )
        else:
            conn.execute(
                "UPDATE runs SET status = ?, document = ?, effective = ?, updated_at = ? "
                "WHERE run_id = ?",
                (
                    run.status,
                    run.model_dump_json(),
                    effective.model_dump_json(),
                    run.updated_at,
                    run.run_id,
                ),
            )

    def list_runs(
        self, status: str | None = None, workflow_id: str | None = None
    ) -> list[RunState]:
        clauses, params = [], []
        if status:
            clauses.append("status = ?")
            params.append(status)
        if workflow_id:
            clauses.append("workflow_id = ?")
            params.append(workflow_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._all(
            f"SELECT document FROM runs {where} ORDER BY created_at, run_id", params
        )
        return [RunState.model_validate_json(r["document"]) for r in rows]

    # --- amendments ---------------------------------------------------------------------

    def create_amendment(self, conn: sqlite3.Connection, amendment: Amendment) -> None:
        conn.execute(
            "INSERT INTO amendments (amendment_id, run_id, workflow_id, status, document, "
            "created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (
                amendment.amendment_id,
                amendment.run_id,
                amendment.workflow_id,
                amendment.status,
                amendment.model_dump_json(),
                amendment.created_at,
            ),
        )

    def get_amendment(self, amendment_id: str) -> Amendment:
        row = self._one(
            "SELECT document FROM amendments WHERE amendment_id = ?", (amendment_id,)
        )
        if row is None:
            raise NotFound(f"amendment '{amendment_id}' not found")
        return Amendment.model_validate_json(row["document"])

    def save_amendment(self, conn: sqlite3.Connection, amendment: Amendment) -> None:
        conn.execute(
            "UPDATE amendments SET status = ?, document = ? WHERE amendment_id = ?",
            (amendment.status, amendment.model_dump_json(), amendment.amendment_id),
        )

    def list_amendments(
        self, run_id: str | None = None, status: str | None = None
    ) -> list[Amendment]:
        # ``run_id`` is optional so "is anything of mine waiting?" is one query rather than
        # one per run — the approvals inbox and any client waiting on a decision both need
        # it (STATUS.md section 4 gap 1, MCP-SURFACE.md 2).
        clauses, params = [], []
        for column, value in (("run_id", run_id), ("status", status)):
            if value is not None:
                clauses.append(f"{column} = ?")
                params.append(value)
        where = f"WHERE {' AND '.join(clauses)} " if clauses else ""
        rows = self._all(
            f"SELECT document FROM amendments {where}ORDER BY created_at, amendment_id",
            params,
        )
        return [Amendment.model_validate_json(r["document"]) for r in rows]

    def pending_amendment(self, run_id: str) -> Amendment | None:
        found = self.list_amendments(run_id, status="pending_approval")
        return found[0] if found else None

    # --- config -------------------------------------------------------------------------

    def get_approval_policy(self) -> ApprovalPolicy:
        row = self._one(
            "SELECT document FROM config WHERE key = 'approval_policy'"
        )
        if row is None:
            return ApprovalPolicy()
        return ApprovalPolicy.model_validate_json(row["document"])

    def put_approval_policy(self, conn: sqlite3.Connection, policy: ApprovalPolicy) -> None:
        conn.execute(
            "INSERT OR REPLACE INTO config (key, document) VALUES ('approval_policy', ?)",
            (policy.model_dump_json(),),
        )

    def get_workflow_approval_policy(self) -> ApprovalPolicy:
        row = self._one("SELECT document FROM config WHERE key = 'workflow_approval_policy'")
        if row is None:
            return ApprovalPolicy()
        return ApprovalPolicy.model_validate_json(row["document"])

    def put_workflow_approval_policy(
        self, conn: sqlite3.Connection, policy: ApprovalPolicy
    ) -> None:
        conn.execute(
            "INSERT OR REPLACE INTO config (key, document) "
            "VALUES ('workflow_approval_policy', ?)",
            (policy.model_dump_json(),),
        )

    # --- templates ------------------------------------------------------------------------

    def create_template(self, conn: sqlite3.Connection, template: WorkflowTemplate) -> None:
        conn.execute(
            "INSERT INTO templates (template_id, status, document, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                template.template_id,
                template.status,
                template.model_dump_json(),
                template.created_at,
                template.updated_at,
            ),
        )

    def save_template(self, conn: sqlite3.Connection, template: WorkflowTemplate) -> None:
        conn.execute(
            "UPDATE templates SET status = ?, document = ?, updated_at = ? WHERE template_id = ?",
            (
                template.status,
                template.model_dump_json(),
                template.updated_at,
                template.template_id,
            ),
        )

    def template_exists(self, template_id: str) -> bool:
        row = self._one("SELECT 1 FROM templates WHERE template_id = ?", (template_id,))
        return row is not None

    def get_template(self, template_id: str) -> WorkflowTemplate:
        row = self._one("SELECT document FROM templates WHERE template_id = ?", (template_id,))
        if row is None:
            raise NotFound(f"template '{template_id}' not found")
        return WorkflowTemplate.model_validate_json(row["document"])

    def list_templates(self, status: str | None = None) -> list[WorkflowTemplate]:
        if status:
            rows = self._all(
                "SELECT document FROM templates WHERE status = ? ORDER BY created_at, template_id",
                (status,),
            )
        else:
            rows = self._all("SELECT document FROM templates ORDER BY created_at, template_id", ())
        return [WorkflowTemplate.model_validate_json(r["document"]) for r in rows]

    # --- audit --------------------------------------------------------------------------

    def audit(
        self,
        conn: sqlite3.Connection,
        event: str,
        *,
        workflow_id: str | None = None,
        run_id: str | None = None,
        amendment_id: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        # The transport goes inside ``detail`` rather than in a column of its own: the table
        # is append-only with no migration path, and existing databases would need one for a
        # value nothing queries by. Promote it if that ever changes (MCP-SURFACE.md 4).
        entry = dict(detail or {})
        entry["via"] = current_transport.get()
        conn.execute(
            "INSERT INTO audit_log (at, event, workflow_id, run_id, amendment_id, detail) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (now(), event, workflow_id, run_id, amendment_id, json.dumps(entry)),
        )

    def audit_entries(
        self,
        *,
        workflow_id: str | None = None,
        run_id: str | None = None,
        amendment_id: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses, params = [], []
        for column, value in (
            ("workflow_id", workflow_id),
            ("run_id", run_id),
            ("amendment_id", amendment_id),
        ):
            if value is not None:
                clauses.append(f"{column} = ?")
                params.append(value)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._all(
            f"SELECT seq, at, event, workflow_id, run_id, amendment_id, detail FROM audit_log "
            f"{where} ORDER BY seq",
            params,
        )
        return [
            {
                "seq": r["seq"],
                "at": r["at"],
                "event": r["event"],
                "workflow_id": r["workflow_id"],
                "run_id": r["run_id"],
                "amendment_id": r["amendment_id"],
                "detail": json.loads(r["detail"]),
            }
            for r in rows
        ]
