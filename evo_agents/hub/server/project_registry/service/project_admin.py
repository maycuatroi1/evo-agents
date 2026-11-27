"""A self-managed project (``evo_agents.hub.server.project_admin``): what a hub admin turns on, and what the admins of
that project then do themselves, each request in one transaction: who may, up to which role and level, and the grant,
invite, install or removal with its audit row."""

from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub.config import HubConfig
from evo_agents.hub.self_managed import ProjectScope, SelfManaged, normalized, own_signer, state_of
from evo_agents.hub.server import admin as grants
from evo_agents.hub.server import audit, invites
from evo_agents.hub.server.admin import Grant, GrantRequest
from evo_agents.hub.server.base.unit_of_work import UnitOfWork
from evo_agents.hub.server.invites import InviteCreated, InviteRequest
from evo_agents.hub.server.market.models.install import NOTHING_INSTALLED, GitHubInstallIn
from evo_agents.hub.server.plugins.models.store import Plugin
from evo_agents.hub.server.plugins.repository.store import find, version_rows
from evo_agents.hub.server.plugins.service.grants import check_admin
from evo_agents.hub.server.plugins.service.store import from_image, plugin, set_removed
from evo_agents.hub.server.project_registry.models.project_admin import (
    ProjectAdminView,
    ProjectGrant,
    ProjectInstallIn,
    ProjectInviteIn,
    SelfManagedIn,
)
from evo_agents.hub.server.project_registry.models.projects import ProjectAccess
from evo_agents.hub.server.project_registry.repository import project_admin as repository
from evo_agents.hub.server.project_registry.service.access import project_access
from evo_agents.hub.server.security import Principal, holds

# Reading


async def view_of(conn: AsyncConnection, project: str) -> ProjectAdminView:
    """The view of ``project``; 404 when the hub has none."""
    project_id, ladder = await grants.project_ladder(conn, project)
    state = await state_of(conn, project_id)
    rows = await repository.grants_of(conn, project_id)
    return ProjectAdminView(
        name=project,
        self_managed=state.enabled,
        trusted_owners=list(state.owners),
        levels=list(ladder),
        grants=[ProjectGrant(**row._asdict()) for row in rows],
    )


async def show_project(engine: AsyncEngine, project: str) -> ProjectAdminView:
    async with UnitOfWork(engine) as conn:
        return await view_of(conn, project)


async def set_self_managed(engine: AsyncEngine, user: Principal, project: str, body: SelfManagedIn) -> ProjectAdminView:
    """Turn the project's self-managed state on with the GitHub owners hub admins trust, or off."""
    owners = normalized(body.trusted_owners)
    if body.enabled and not owners:
        raise HTTPException(422, "a self-managed project names at least one trusted GitHub owner")
    if not body.enabled and owners:
        raise HTTPException(422, "turning self-managed off takes no trusted owners")
    async with UnitOfWork(engine) as conn:
        project_id, _ = await grants.project_ladder(conn, project)
        await repository.set_self_managed(conn, project_id, body.enabled, list(owners))
        target = (
            f"{project} self_managed=true owners={','.join(owners)}"
            if body.enabled
            else f"{project} self_managed=false"
        )
        await audit.record(
            conn,
            actor_id=user.user_id,
            token_id=user.token_id,
            action=audit.PROJECT_SELF_MANAGED,
            target=target,
            project_id=project_id,
        )
        return await view_of(conn, project)


# What a project's admin may do


async def own_project(
    conn: AsyncConnection, user: Principal, project: str, doing: str
) -> tuple[ProjectAccess, SelfManaged]:
    """The access of ``user`` to a self-managed project they are an admin of; 403 otherwise."""
    access = await project_access(conn, user, project)
    if user.acts_for_run is not None:
        raise HTTPException(403, f"{doing} is for an admin of project {project}, never for the agent of a run")
    check_admin(access, doing)
    state = await state_of(conn, access.project_id)
    if not state.enabled:
        raise HTTPException(
            403,
            f"project {project} is not self-managed, so {doing} is for a hub admin: one turns that on with "
            f"`evo-agents hub admin project self-managed {project} --on --owner OWNER`",
        )
    return access, state


def rank(access: ProjectAccess, level: str) -> int:
    levels = list(access.rules.levels)
    return levels.index(level) if level in levels else len(levels)


def within(access: ProjectAccess, role: str, max_level: str, doing: str) -> None:
    """403 when the role or the max level is above what the admin holds themselves."""
    if not holds(access.role, role):
        raise HTTPException(403, f"{doing}: role {role} is above your own, {access.role}")
    if access.max_level is None or rank(access, max_level) > rank(access, access.max_level):
        raise HTTPException(
            403, f"{doing}: level {max_level} is above your own max level, {access.max_level}; nothing was changed"
        )


async def not_above(conn: AsyncConnection, access: ProjectAccess, login: str, doing: str) -> None:
    """403 when ``login`` holds a grant above the admin's own: the admin cannot change or take back what they could not
    give."""
    held = await repository.held_grant(conn, access.project_id, login)
    if held is not None:
        within(access, held.role, held.max_level, doing)


async def show_own_project(engine: AsyncEngine, user: Principal, project: str) -> ProjectAdminView:
    """The self-managed state, trusted owners, ladder and grants of a project, for its admins and hub admins."""
    async with UnitOfWork(engine) as conn:
        access = await project_access(conn, user, project)
        if not user.admin or access.role is not None:
            check_admin(access, "reading the grants of the project")
        return await view_of(conn, project)


async def put_grant(
    engine: AsyncEngine, user: Principal, project: str, login: str, body: GrantRequest
) -> tuple[Grant, list[int]]:
    """Give ``login`` a role on the project, or change the one it has, up to the admin's own role and max level; the
    grant and the runs whose leases were taken (``admin.revoke_taken``)."""
    doing = f"giving {login} a grant on project {project}"
    async with UnitOfWork(engine) as conn:
        access, _ = await own_project(conn, user, project, doing)
        within(access, body.role, body.max_level, doing)
        await not_above(conn, access, login, doing)
        return await grants.give_grant(conn, user, project, login, body)


async def delete_grant(engine: AsyncEngine, user: Principal, project: str, login: str) -> list[int]:
    """Take back the grant of ``login`` on the project, if it is not above the admin's own role and max level; the runs
    whose leases were taken (``admin.revoke_taken``)."""
    doing = f"taking back the grant of {login} on project {project}"
    async with UnitOfWork(engine) as conn:
        access, _ = await own_project(conn, user, project, doing)
        await not_above(conn, access, login, doing)
        return await grants.take_grant(conn, user, project, login)


async def make_invite(
    engine: AsyncEngine, config: HubConfig, user: Principal, project: str, body: ProjectInviteIn
) -> InviteCreated:
    """Make a one-time link that joins the project with a role and max level up to the admin's own."""
    doing = f"making an invite link to project {project}"
    async with UnitOfWork(engine) as conn:
        access, _ = await own_project(conn, user, project, doing)
        within(access, body.role, body.max_level, doing)
        asked = InviteRequest(kind="join", project=project, **body.model_dump())
        invite, token = await invites.make_invite(conn, user, asked)
    return invites.created(config, invite, token)


# Plugins


def signer_of(repo: str, signer: str | None) -> str:
    """The signer for ``repo``: a workflow of that same repository, its ci.yml by default; 422 for any other."""
    named = own_signer(repo, signer)
    if named is None:
        raise HTTPException(
            422,
            f"a self-managed project installs plugins signed by a workflow of the repository itself "
            f"(sigstore:{repo}/.github/workflows/FILE.yml), not {signer}; a hub admin trusts any other signer; "
            f"{NOTHING_INSTALLED}",
        )
    return named


async def install_asked(
    engine: AsyncEngine, user: Principal, project: str, body: ProjectInstallIn
) -> tuple[GitHubInstallIn, ProjectScope]:
    """What installing, or upgrading, the plugin of the release of a trusted owner's repository for this project alone
    asks of ``market.service.install.install_github``, and the project it installs into; 403 or 422 before anything
    is fetched."""
    doing = f"installing a plugin from {body.repo}"
    async with UnitOfWork(engine) as conn:
        access, state = await own_project(conn, user, project, doing)
    if not state.trusts_repo(body.repo):
        owner = body.repo.partition("/")[0]
        raise HTTPException(
            403,
            f"{owner} is not a trusted owner of project {project} ({', '.join(state.owners)}): a hub admin installs "
            f"the plugin, or adds the owner; {NOTHING_INSTALLED}",
        )
    asked = GitHubInstallIn(repo=body.repo, tag=body.tag, signer=signer_of(body.repo, body.signer))
    return asked, ProjectScope(access.project_id, access.name)


async def remove(engine: AsyncEngine, hub_version: str, user: Principal, project: str, name: str) -> Plugin:
    """Remove the installed version of a plugin this project installed; every version stays in the store."""
    doing = f"removing plugin {name}"
    async with UnitOfWork(engine) as conn:
        access, _ = await own_project(conn, user, project, doing)
        row = await find(conn, name, lock=True)
        if row is None or row.project_id != access.project_id:
            raise HTTPException(404, f"project {project} installed no plugin {name}: see its plugins page")
        if row.installed_version is None:
            raise HTTPException(409, f"plugin {name} is not installed")
        if from_image(await version_rows(conn, row.id)):
            raise HTTPException(409, f"plugin {name} ships in the hub's image")
        await set_removed(conn, user, row, project_id=access.project_id)
        return await plugin(conn, name, hub_version)
