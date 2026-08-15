"""Which transport the current call arrived on.

REQ-32 and REQ-13 both say a *human* approves. A tool call made by an agent on a human's
instruction is genuinely such a decision — the human did decide — but the record would
otherwise not distinguish it from one the agent made on its own initiative. Stamping the
transport onto every audit entry keeps that distinguishable after the fact
(MCP-SURFACE.md section 4).

A context variable rather than a parameter on every ``Chief`` method: the value is
ambient to the call, no domain method has any business branching on it, and threading it
through twenty signatures would invite exactly that. ``anyio`` copies the context into the
threadpool, so a value set in an async MCP tool reaches the synchronous store call.
"""

from __future__ import annotations

from contextvars import ContextVar

# REST is the default because it is the surface everything else is a guest on: the UIs,
# curl, and any client that predates the MCP surface all arrive without setting this.
current_transport: ContextVar[str] = ContextVar("current_transport", default="rest")
