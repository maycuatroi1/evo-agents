"""The error of a proposal of @builder, apart so the modules that hold one kind's logic can raise it without importing
``proposals``."""

from __future__ import annotations

NOTHING = "nothing changed"


class ProposalError(Exception):
    """Why a proposal cannot be made, decided or rolled back, with the HTTP status a route answers; nothing changed."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message
