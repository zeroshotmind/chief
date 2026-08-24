"""Identifier generation.

Ids are short and prefixed so they are readable in logs and diffs. They are opaque to
clients; nothing in the contract depends on their internal structure.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime


def _token(n: int = 4) -> str:
    return secrets.token_hex(n)[:n]


def workflow_id() -> str:
    return f"wf_{_token()}"


def run_id() -> str:
    return f"run_{_token()}"


def template_id() -> str:
    return f"tpl_{_token()}"


def proof_graph_id() -> str:
    return f"pg_{_token()}"


def amendment_id() -> str:
    return f"amd_{_token()}"


def artifact_id() -> str:
    return f"art_{_token()}"


def comment_id() -> str:
    return f"cmt_{_token()}"


def note_id() -> str:
    return f"rvw_{_token()}"


def instance_id(index: int) -> str:
    return f"inst_{index:02d}"


def now() -> str:
    """Current UTC time as an iso8601 string, which is the contract's timestamp format."""
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
