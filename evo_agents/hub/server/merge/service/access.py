"""Who may answer the hub's merge queue: an admin of the project, never a run acting for one (``service.post_merge``,
``service.breaker``)."""

from __future__ import annotations

from fastapi import HTTPException

from evo_agents.hub.server.security import holds


def admin_only(user, access) -> None:
    if user.acts_for_run is not None or not holds(access.role, "admin"):
        raise HTTPException(403, f"this needs the admin role on project {access.name}")
