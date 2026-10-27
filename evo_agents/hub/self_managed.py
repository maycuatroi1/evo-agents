"""A self-managed project: the state a hub admin gives it (``projects.self_managed``, ``projects.trusted_owners``,
0170) and the question every route and @builder asks of it.

A hub admin turns the state on for a project with the GitHub owners they trust. The admins of that project then install,
upgrade and remove plugins from the releases of those owners, approve the plugin_install proposals of @builder that name
a repository of one of them, and give grants and invite links on the project, never above their own role and max level.
Nothing here grants a reader anything: what a member sees is still their grant and the labels.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.plugins.market import default_signer


@dataclass(frozen=True)
class SelfManaged:
    """Whether a project runs itself, and the GitHub owners (lower case) whose releases its admins install."""

    enabled: bool = False
    owners: tuple[str, ...] = ()

    def trusts_repo(self, repo: str | None) -> bool:
        """Whether ``repo`` (OWNER/REPO) belongs to an owner this project trusts, the project being self-managed."""
        return self.enabled and bool(repo) and repo.partition("/")[0].lower() in self.owners


def own_signer(repo: str, signer: str | None) -> str | None:
    """The signer to check a release of ``repo`` against for a self-managed project: the one named, or the repo's ci.yml;
    None when it is not a workflow of that same repository (only a hub admin trusts any other signer)."""
    named = signer or default_signer(repo)
    return named if named.lower().startswith(f"sigstore:{repo}/.github/workflows/".lower()) else None


@dataclass(frozen=True)
class ProjectScope:
    """The self-managed project a plugin is installed for."""

    project_id: int
    name: str


def normalized(owners: list[str]) -> tuple[str, ...]:
    """``owners`` in lower case, each once, in the order given (GitHub treats owner names without case)."""
    return tuple(dict.fromkeys(owner.strip().lower() for owner in owners if owner.strip()))


async def state_of(conn: AsyncConnection, project_id: int) -> SelfManaged:
    """The state of project ``project_id``."""
    p = tables.projects
    row = (await conn.execute(select(p.c.self_managed, p.c.trusted_owners).where(p.c.id == project_id))).first()
    if row is None:
        return SelfManaged()
    return SelfManaged(row.self_managed, normalized(list(row.trusted_owners)))
