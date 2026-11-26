"""The errors of a domain: a service raises ``NotFound``, ``Forbidden``, ``Conflict`` or ``Unprocessable`` with its
message, and the app answers it with that status and the ``ErrorBody`` of every error
(``evo_agents.hub.server.errors.domain_error``).

Each is an ``HTTPException`` too, its message the ``detail``: the callers that catch one from a write (the kinds of
@builder, the intake) read ``status_code`` and ``detail`` as they always have.
"""

from __future__ import annotations

from typing import ClassVar

from fastapi import HTTPException


class DomainError(HTTPException):
    """A refusal of a domain, with the status it answers with."""

    status: ClassVar[int] = 400

    def __init__(self, message: str):
        super().__init__(self.status, message)

    @property
    def message(self) -> str:
        return str(self.detail)


class Forbidden(DomainError):
    status = 403


class NotFound(DomainError):
    status = 404


class Conflict(DomainError):
    status = 409


class Unprocessable(DomainError):
    status = 422


def refuse(wrong: list[str]) -> None:
    """422 naming each thing ``wrong`` with a write, when there is any."""
    if wrong:
        raise Unprocessable(f"{'; '.join(wrong)}; nothing changed")
