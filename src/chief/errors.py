"""Domain error types.

Every rejection the Chief makes is one of these, so the API layer can map a
domain failure onto an HTTP status without the domain code importing FastAPI.
"""

from __future__ import annotations

from typing import Any


class ChiefError(Exception):
    """Base class for every rejection raised by the domain layer."""

    status_code: int = 400
    code: str = "bad_request"

    def __init__(self, message: str, *, details: Any = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details is not None:
            payload["details"] = self.details
        return {"error": payload}


class NotFound(ChiefError):
    status_code = 404
    code = "not_found"


class ValidationFailed(ChiefError):
    """The submitted document is structurally invalid (REQ-34)."""

    status_code = 422
    code = "validation_failed"


class InvalidTransition(ChiefError):
    """The operation is well-formed but not legal in the current state."""

    status_code = 409
    code = "invalid_transition"


class InvariantViolation(ChiefError):
    """A server-side invariant from contract section 4 blocks the operation."""

    status_code = 409
    code = "invariant_violation"


class Conflict(ChiefError):
    status_code = 409
    code = "conflict"


class NotAvailable(ChiefError):
    """The operation needs something this instance does not have.

    Distinct from a validation failure on purpose: a plan that could not be checked because
    there is no Lean toolchain here has not been found unsound, and answering 422 would say it
    had. 503 says the same thing to a client that only reads status codes.
    """

    status_code = 503
    code = "not_available"
