"""The owner's view of a run's leases: GET /v1/projects/{p}/runs/{id}/credentials, the leases ``credentials`` handed
out, given back or not (``docs/credentials.md``, section "What a run got").

GET /v1/projects/{p}/runs/{id}/credentials lists every lease the run got, given back or not, for the member who
dispatched it: the secret's name or ``github-app:<account>``, provider, kind, target (the variable, or the origins it
answered for), the worker, and when it was issued, ends and was revoked; never a value, sealed or not. The run must be
one the caller may read (``runs.readable_run``: 404 otherwise); another member, a hub admin included, gets 403, since
the leases name the owner's secrets as GET /v1/secrets does to the owner alone. A lease of an account of the run's
agent (``agent_accounts``) is named ``@<handle>:<name>``, which no secret of the owner can be. Each lease names the agent profile and
version it was handed out under (``evo_agents.hub.profiles``), null for one from before schema 0030.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import aliased

from evo_agents.hub import tables
from evo_agents.hub.credentials import LEASED_KINDS, PROVIDERS, agent_lease_name, github_repo
from evo_agents.hub.server import plans as plan_routes
from evo_agents.hub.server.admin import ProjectName
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.runs.models import RunId
from evo_agents.hub.server.runs.service.views import readable_run
from evo_agents.hub.server.security import CurrentUser

router = APIRouter(prefix="/v1/projects", tags=["runs"], responses={401: {"model": ErrorBody}})
REFUSALS = {403: {"model": ErrorBody}, 404: {"model": ErrorBody}}


class RunLease(BaseModel):
    """A lease a run got, as its owner sees it: everything but the value."""

    id: int
    name: str = Field(
        description="the secret's name, github-app:<account>, or @<handle>:<name> for an account of an agent"
    )

    provider: Literal[PROVIDERS] = Field(description="secret: the owner's; github-app: a token the hub's App made")
    kind: Literal[LEASED_KINDS] = Field(description="env: a variable of the agent; git: what git's helper answered")
    target: str = Field(
        description="kind env: the variable it set; kind git: the origins of the run's repos it answered for, "
        "space-separated, in the form of normalize_origin"
    )
    worker: str = Field(description="the worker it was leased to")
    issued_at: datetime
    expires_at: datetime | None = Field(description="when it stops working: a GitHub token's hour, a secret's end")
    revoked_at: datetime | None = Field(description="when the run gave it back or the hub took it; null while out")
    profile: str | None = Field(
        None, description="the handle of the agent profile it was handed out under; null for a lease before schema 0030"
    )
    profile_id: int | None = Field(None, description="that profile's id")
    profile_version: int | None = Field(None, description="the version of that profile")


def _run_owner(run_id: int):
    r, users = tables.runs, tables.users
    return (
        select(r.c.dispatched_by, users.c.login)
        .join_from(r, users, users.c.id == r.c.dispatched_by)
        .where(r.c.id == run_id)
    )


def _run_leases(run_id: int):
    """Every lease of run ``run_id``, with the worker's name and the secret's name and kind (none for a GitHub token),
    in the order they were made."""
    cl, w, s, ap = tables.credential_leases, tables.workers, tables.secrets, tables.agent_profiles
    owner = aliased(tables.agent_profiles)  # the agent that owns the secret, when one does
    return (
        select(
            cl.c.id,
            s.c.name.label("secret"),
            cl.c.provider,
            s.c.kind.label("secret_kind"),
            cl.c.target,
            w.c.name.label("worker"),
            cl.c.issued_at,
            cl.c.expires_at,
            cl.c.revoked_at,
            ap.c.handle.label("profile"),
            owner.c.handle.label("agent"),
            cl.c.profile_id,
            cl.c.profile_version,
        )
        .join_from(cl, w, w.c.id == cl.c.worker_id)
        .outerjoin(s, s.c.id == cl.c.secret_id)
        .outerjoin(ap, ap.c.id == cl.c.profile_id)
        .outerjoin(owner, owner.c.id == s.c.profile_id)
        .where(cl.c.run_id == run_id)
        .order_by(cl.c.id)
    )


def _token_name(target: str) -> str:
    """The name a GitHub token's lease was handed out with, github-app:<owner>, from the origins it answered for."""
    origins = target.split()
    found = github_repo(origins[0]) if origins else None
    return f"github-app:{found[0]}" if found else "github-app"


def _lease_name(row) -> str:
    if row.provider != "secret":
        return _token_name(row.target)
    return row.secret if row.agent is None else agent_lease_name(row.agent, row.secret)


@router.get("/{project}/runs/{run_id}/credentials", response_model=list[RunLease], responses=REFUSALS)
async def run_credentials(
    request: Request,
    project: ProjectName,
    run_id: RunId,
    user: CurrentUser,
    sink: Annotated[str | None, Header(alias=plan_routes.SINK_HEADER)] = None,
) -> list[RunLease]:
    """Every lease the run got, given back or not, without a value; for the member who dispatched it."""
    async with request.app.state.engine.begin() as conn:
        await readable_run(conn, user, project, run_id, sink)
        owner_id, owner = (await conn.execute(_run_owner(run_id))).one()
        if owner_id != user.user_id:
            raise HTTPException(403, f"only {owner}, who dispatched run {run_id}, sees the credentials it got")
        rows = (await conn.execute(_run_leases(run_id))).all()
    return [
        RunLease(
            id=row.id,
            name=_lease_name(row),
            provider=row.provider,
            kind=row.secret_kind if row.provider == "secret" else "git",
            target=row.target,
            worker=row.worker,
            issued_at=row.issued_at,
            expires_at=row.expires_at,
            revoked_at=row.revoked_at,
            profile=row.profile,
            profile_id=row.profile_id,
            profile_version=row.profile_version,
        )
        for row in rows
    ]
