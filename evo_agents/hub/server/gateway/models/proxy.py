"""What the hub's gateway route passes and answers (``routes.proxy``, ``service.proxy``): the headers it lets through
each way, its refusal of a token, and the run a token is of."""

from __future__ import annotations

from dataclasses import dataclass

PASSED = ("content-type", "accept", "anthropic-version", "anthropic-beta", "openai-beta", "user-agent")
RETURNED = ("content-type", "cache-control", "x-request-id", "retry-after")
REFUSED = "the run's token to the gateway route is missing, revoked, or of a run that is no longer held"


@dataclass(frozen=True)
class Holder:
    run_id: int
    prefix: str
    key: str
    project_id: int
