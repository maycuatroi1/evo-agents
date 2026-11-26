"""The member's memories on the hub, ``/v1/memories``, which ``evo_agents.hub.memory.MemorySync`` pushes and pulls."""

from __future__ import annotations

from typing import Any

from evo_agents.hub.api.resource import Resource


class Memories(Resource):
    """``/v1/memories/<id>?<query>``."""

    base = "memories"

    def __init__(self, hub: Any = None):
        super().__init__(hub)
