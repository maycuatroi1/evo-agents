"""How the audit trail names a run, and the row an action on a run writes there (``evo_agents.hub.server.audit``)."""

from __future__ import annotations

from evo_agents.hub.server import audit
from evo_agents.hub.server.projects import ProjectAccess
from evo_agents.hub.server.security import Principal


def run_target(project: str, plan_id: str | None, key: str | None, run_id: int) -> str:
    """How an audit row names a run: its project, plan and step, or no step for a plan run; a review run, which has
    no plan, as the project's review."""
    if not plan_id:
        return f"{project}/review run:{run_id}"
    return f"{project}/{plan_id}{'' if key is None else f'#{key}'} run:{run_id}"


async def audit_run(conn, user: Principal, access: ProjectAccess, action: str, target: str) -> None:
    await audit.record(
        conn, actor_id=user.user_id, token_id=user.token_id, action=action, target=target, project_id=access.project_id
    )
