"""How the hub shows a credential share (``evo_agents.hub.credential_shares``): the model of one, and the shares of
some secrets read in one query, which GET /v1/secrets, GET /v1/accounts and the share routes
(``evo_agents.hub.server.credential_shares``) give their owner. A share names its secret, grantee, projects and the
logins of who created and revoked it; never a value.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field
from sqlalchemy import Text, cast, func, select
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.credential_shares import CREDENTIAL_CLASSES, GRANTEE_KINDS, credential_class
from evo_agents.hub.org import LEVELS


class CredentialShare(BaseModel):
    """A share of a secret or an account, as its owner sees it."""

    id: int
    secret: str = Field(description="the name of the secret or account shared")
    credential: Literal[CREDENTIAL_CLASSES] = Field(
        description="subscription (a personal Claude account, shared only after its owner acknowledged the terms "
        "warning), api_key, git or env"
    )
    grantee_kind: Literal[GRANTEE_KINDS] = Field(description="user: one member; unit: a unit and below; all: everyone")
    grantee: str | None = Field(description="the member's login or the unit's slug; null for all")
    projects: list[str] = Field(description="the projects whose runs may use it")
    level: Literal[LEVELS] | None = Field(description="the autonomy level a run using it is held to; null for none")
    budget_usd: float | None = Field(description="the most the runs using it may cost a month, in USD; null for none")
    usage_ceiling_pct: int | None = Field(
        description="1 to 100: no run takes the account while its usage is at this percent or above; null for none"
    )
    max_running: int | None = Field(description="the most runs that may hold it at once; null for no limit")
    expires_at: datetime | None = Field(description="when the share ends; null for no end")
    terms_ack_at: datetime | None = Field(description="when its owner acknowledged the terms warning; null for none")
    created_by: str
    created_at: datetime
    revoked_at: datetime | None
    revoked_by: str | None


def share_query():
    """The shares with every column of CredentialShare, plus the secret's id as ``secret_id``."""
    cs, s, units = tables.credential_shares, tables.secrets, tables.org_units
    grantee, creator, revoker = (tables.users.alias(name) for name in ("grantee", "creator", "revoker"))
    csp, projects = tables.credential_share_projects, tables.projects
    names = (
        select(projects.c.name)
        .join_from(csp, projects, projects.c.id == csp.c.project_id)
        .where(csp.c.share_id == cs.c.id)
        .order_by(projects.c.name)
    )
    return (
        select(
            cs.c.id,
            cs.c.secret_id,
            s.c.name.label("secret"),
            s.c.kind,
            s.c.env_var,
            cs.c.grantee_kind,
            func.coalesce(grantee.c.login, units.c.slug).label("grantee"),
            func.array(names.scalar_subquery(), type_=ARRAY(Text)).label("projects"),
            cs.c.level,
            cast(cs.c.budget_usd, Text).label("budget_usd"),
            cs.c.usage_ceiling_pct,
            cs.c.max_running,
            cs.c.expires_at,
            cs.c.terms_ack_at,
            creator.c.login.label("created_by"),
            cs.c.created_at,
            cs.c.revoked_at,
            revoker.c.login.label("revoked_by"),
        )
        .join_from(cs, s, s.c.id == cs.c.secret_id)
        .outerjoin(grantee, grantee.c.id == cs.c.grantee_user_id)
        .outerjoin(units, units.c.id == cs.c.grantee_unit_id)
        .join(creator, creator.c.id == cs.c.created_by)
        .outerjoin(revoker, revoker.c.id == cs.c.revoked_by)
        .order_by(cs.c.id.desc())
    )


def share_of(row) -> CredentialShare:
    values = dict(row._mapping)
    kind, env_var = values.pop("kind"), values.pop("env_var")
    del values["secret_id"]
    budget = values.pop("budget_usd")
    return CredentialShare(
        **values, credential=credential_class(kind, env_var), budget_usd=None if budget is None else float(budget)
    )


async def shares_of(
    conn: AsyncConnection, secret_ids: list[int], *, live_only: bool = True
) -> dict[int, list[CredentialShare]]:
    """The shares of each of ``secret_ids``, newest first: those not revoked, or all of them."""
    if not secret_ids:
        return {}
    cs = tables.credential_shares
    statement = share_query().where(cs.c.secret_id.in_(secret_ids))
    if live_only:
        statement = statement.where(cs.c.revoked_at.is_(None))
    found: dict[int, list[CredentialShare]] = defaultdict(list)
    for row in await conn.execute(statement):
        found[row.secret_id].append(share_of(row))
    return found
