"""What a call of a provider's endpoint found, and the limits of that call
(``evo_agents.hub.server.accounts.service.probe`` says how the hub calls it)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

TIMEOUT_S = 10.0
MAX_BODY_BYTES = 1 << 20  # what the hub reads of one answer
MAX_ERROR_CHARS = 200
ANTHROPIC_VERSION = "2023-06-01"
NO_MODELS_PATH = (404, 405)  # an endpoint without GET /models
BEARER = re.compile(r"(?i)(bearer\s+)[^\s\"']+")
KEYISH = re.compile(r"(?i)\b(sk|key|api[_-]?key|token)[-_=:\s\"']*[A-Za-z0-9._-]{12,}")


@dataclass
class Probe:
    ok: bool
    models: list[str] = field(default_factory=list)
    status: int | None = None  # the endpoint's HTTP status; None when no answer came
    error: str | None = None
