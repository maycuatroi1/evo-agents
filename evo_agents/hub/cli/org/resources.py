"""The routes under /v1/org that ``evo-agents hub org`` asks (``evo_agents.hub.server.org`` and
``evo_agents.hub.server.worker_shares``), behind one gateway: :class:`OrgChart`.

They belong to the hub and not to a project, so the gateway is not a ``ProjectResource``. Standard library only, like
the rest of the client.
"""

from __future__ import annotations

from typing import cast
from urllib.parse import quote

from evo_agents.hub.cli.base import HubRoutes, call_object


class OrgChart(HubRoutes):
    """The org chart of the hub: GET /v1/org, its units, its seats and whom shared workers are shared with (Gateway)."""

    base = "org"

    def chart(self) -> dict:
        return call_object(self.hub, "GET", self.path)

    def create_unit(self, body: dict) -> dict:
        return call_object(self.hub, "POST", f"{self.path}/units", body)

    def edit_unit(self, slug: str, body: dict) -> dict:
        return call_object(self.hub, "PATCH", self.unit_path(slug), body)

    def archive_unit(self, slug: str) -> dict:
        return call_object(self.hub, "POST", f"{self.unit_path(slug)}/archive")

    def unit_path(self, slug: str) -> str:
        return f"{self.path}/units/{quote(slug, safe='')}"

    def add_seat(self, body: dict) -> dict:
        return call_object(self.hub, "POST", f"{self.path}/seats", body)

    def edit_seat(self, seat_id: int, body: dict) -> dict:
        return call_object(self.hub, "PATCH", f"{self.path}/seats/{seat_id}", body)

    def end_seat(self, seat_id: int) -> dict:
        return call_object(self.hub, "POST", f"{self.path}/seats/{seat_id}/end")

    def workers(self) -> list[dict]:
        return cast("list[dict]", self.hub.call("GET", f"{self.path}/workers"))  # the hub answers a JSON array

    def share(self, worker_id: int, unit: str) -> dict:
        return call_object(self.hub, "PUT", self.worker_unit_path(worker_id, unit))

    def unshare(self, worker_id: int, unit: str) -> dict:
        return call_object(self.hub, "DELETE", self.worker_unit_path(worker_id, unit))

    def worker_unit_path(self, worker_id: int, unit: str) -> str:
        return f"{self.path}/workers/{worker_id}/units/{quote(unit, safe='')}"
