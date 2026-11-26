"""Routes of the hub several modules ask, each written once."""

from __future__ import annotations

from typing import Any

from evo_agents.hub.api.routes import route

PROJECTS = route("projects")
WHOAMI = route("auth/whoami")
AUTH_CONFIG = route("auth/config")
AUTH_GITHUB = route("auth/github")
AUTH_LOGOUT = route("auth/logout")
BLOB_UPLOADS = route("blobs/uploads")
BLOB_COMMIT = route("blobs/commit")
BLOBS = route("blobs")
WORKERS = route("workers")
WORKER_POOLS = route("worker-pools")
SKILLS = route("skills")
NOTIFICATIONS = route("me/notifications")
MEMORIES = route("memories")
CLI_PLUGINS = route("cli/plugins")
GUARD_POLICY = route("guard/policy")
SELLER_API = route("seller-api")


def project(name: str) -> str:
    """``/v1/projects/<name>``: one project."""
    return route("projects", name)


def worker(worker_id: Any, *parts: Any) -> str:
    """``/v1/workers/<id>/<part>...``: one worker of the member's."""
    return route("workers", worker_id, *parts)
