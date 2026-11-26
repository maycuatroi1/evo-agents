"""The routes of the hub ``evo-agents hub admin`` asks, one gateway each: :class:`HubAdmin` for the hub admin's own
(``/v1/admin/...``), :class:`ProjectAdmin` for what the admins of a self-managed project do
(``/v1/projects/{p}/admin/...``). Standard library only, like the rest of the client.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from evo_agents.hub.cli.base import HubRoutes, ProjectResource, call_object


def part(value: Any) -> str:
    """One part of a path, quoted whole."""
    return quote(str(value), safe="")


class HubAdmin(HubRoutes):
    """The hub admin's routes: grants, users, row counts, the Telegram bot, channels, invite links and the
    self-managed state of a project (Gateway)."""

    base = "admin"

    def grant(self, project: str, login: str, body: dict) -> dict:
        return self.call("PUT", self.grant_path(project, login), body)

    def revoke_grant(self, project: str, login: str) -> None:
        self.call("DELETE", self.grant_path(project, login))

    def grant_path(self, project: str, login: str) -> str:
        return f"/projects/{part(project)}/grants/{part(login)}"

    def users(self) -> list[dict]:
        return self.call("GET", "/users")

    def stats(self) -> dict:
        return self.call("GET", "/stats")

    def telegram(self) -> dict:
        return self.call("GET", "/telegram")

    def set_webhook(self) -> dict:
        return self.call("POST", "/telegram/webhook")

    def unlink_telegram(self, login: str) -> dict:
        return self.call("DELETE", f"/users/{part(login)}/telegram")

    def link_channel(self, login: str, kind: str, body: dict) -> dict:
        return call_object(self.hub, "PUT", self.path + self.channel_path(login, kind), body)

    def unlink_channel(self, login: str, kind: str) -> dict:
        return call_object(self.hub, "DELETE", self.path + self.channel_path(login, kind))

    def channel_path(self, login: str, kind: str) -> str:
        return f"/users/{part(login)}/channels/{part(kind)}"

    def invites(self) -> list[dict]:
        return self.call("GET", "/invites")

    def create_invite(self, body: dict) -> dict:
        return self.call("POST", "/invites", body)

    def revoke_invite(self, invite: int) -> dict:
        return self.call("POST", f"/invites/{invite}/revoke")

    def self_managed(self, project: str, body: dict) -> dict:
        return self.call("PUT", f"/projects/{part(project)}/self-managed", body)


class ProjectAdmin(ProjectResource):
    """GET /v1/projects/{p}/admin, and its grants and invite links (Gateway)."""

    collection = "admin"

    def view(self) -> dict:
        return self.call("GET")

    def grant(self, login: str, body: dict) -> dict:
        return self.call("PUT", "grants", login, body=body)

    def revoke(self, login: str) -> None:
        self.call("DELETE", "grants", login)

    def invite(self, body: dict) -> dict:
        return self.call("POST", "invites", body=body)
