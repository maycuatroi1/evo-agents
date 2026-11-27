"""The canonical byte form every hash of kg/1 is computed over, and the form of a label's principal.

They sit here, below both the graph (``evo_agents.kg.protocol``) and the harness loader, which hashes a plan the same
way and checks a knowledge.yaml's audience, so neither imports the other.
"""

from __future__ import annotations

import hashlib
import json

# A principal of a label's audience: a member of chat <id>, or one with a grant on <project> up to <level> at least.
PRINCIPAL = r"^(chat:[1-9][0-9]*|grant:[a-z0-9][a-z0-9_.-]*:[A-Za-z0-9][A-Za-z0-9_.-]*)$"


def canonical_json(value) -> bytes:
    """The byte form every hash in kg/1 is computed over. Connectors in other languages must match it:
    keys sorted, no insignificant whitespace, UTF-8 without escaping non-ASCII characters."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sha256(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return "sha256:" + hashlib.sha256(data).hexdigest()
