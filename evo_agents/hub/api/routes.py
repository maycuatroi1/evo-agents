"""How a path of the hub's API is written: ``/v1/<base>/<part>...?<query>``.

``base`` is a constant of the gateway, written as the hub's route is (``worker/runs``, ``x/tasks``); each part is a
value (a project, a run id, a name) and is quoted whole, so a ``/`` in it stays inside its segment. A query value of
None is left out. Standard library only, like the rest of the client.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any
from urllib.parse import quote, urlencode

V1 = "/v1"

Query = Mapping[str, Any] | Iterable[tuple[str, Any]]


def quoted(*parts: Any, safe: str = "") -> str:
    """``/<part>...``, each part quoted whole but for the characters in ``safe``."""
    return "".join(f"/{quote(str(part), safe=safe)}" for part in parts)


def query_string(query: Query = ()) -> str:
    """``?<query>`` of a mapping, or of pairs a key may repeat in, with the values of None left out; ``""`` for none."""
    pairs = query.items() if isinstance(query, Mapping) else query
    given = [(key, value) for key, value in pairs if value is not None]
    return f"?{urlencode(given)}" if given else ""


def route(base: str, *parts: Any, safe: str = "", **query: Any) -> str:
    """``/v1/<base>/<part>...?<query>``: ``base`` as written, each part quoted whole (but for ``safe``)."""
    return f"{V1}/{base}" + quoted(*parts, safe=safe) + query_string(query)


def is_route(path: str, base: str) -> bool:
    """Whether ``path`` (its query left out) is ``/v1/<base>`` or below it."""
    head = f"{V1}/{base}"
    bare = path.split("?", 1)[0]
    return bare == head or bare.startswith(head + "/")
