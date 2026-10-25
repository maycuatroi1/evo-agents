"""Which provider accounts a run on a gateway model may use (``evo_agents.hub.gateway_runs``), by the rules of any
account (``docs/credentials.md``, sections "Accounts" and "Sharing"), and the check a dispatch makes of them.

The accounts are those of the run's prefix (the catalog's alias of their provider, or evo-<id> for an endpoint a member
named) that are live, not paused and not past their end: the agent's own when the run goes under an agent with a name
that has any bound to its project on the worker (its runs take those, whoever dispatches them, as for env accounts);
else the dispatcher's own, bound to the run's project on any worker or on this one, then those shared with the run's
principal that lend now (``share_leases.shared_secrets``, with its refusals). The run's profile must allow each by
name (its credential axis). An account picked by hand is the only one. Before a worker holds the run, at its dispatch,
the worker is the one it is pinned to, or any.

``check_dispatch`` answers the prefix a dispatch's model goes through the gateway with, or None. On claude-code and
codex a model whose prefix the gateway serves always does; on opencode, or with runtime any, whose own model names
read ``<provider>/<model>`` too, it does when the dispatcher, the agent or a share has an account of that prefix at
all. A run that would go through the gateway with no account it may use is 422, saying why, and so is a plugin's
runtime, which never does; one pinned to a worker whose daemon is older than GATEWAY_RUN_AGENT is 409.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from fastapi import HTTPException
from sqlalchemy import and_, exists, func, or_, select, true
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import gateway_runs, profiles, runs, tables
from evo_agents.hub.credentials import PROVIDER_KIND
from evo_agents.hub.gateway_runs import GATEWAY_RUNTIMES, PICK_PREFIX
from evo_agents.hub.server import share_leases  # a module: it imports worker_placement, which reaches this one

NOTHING = "nothing was dispatched"


@dataclass(frozen=True)
class Asker:
    """What the rules read of a run, or of a dispatch before its run exists (id 0)."""

    id: int
    owner_id: int  # who dispatched it
    principal: int  # who it acts for
    project_id: int
    profile_id: int | None = None
    profile_project_id: int | None = None
    agent_owned: bool = False  # an agent with a name, whose own accounts its runs take
    permissions: dict | None = None  # its profile's, whose credential axis names the accounts it may use

    def allows(self, name: str) -> bool:
        return profiles.allows(self.permissions, "credential", name)


@dataclass(frozen=True)
class Account:
    secret_id: int
    name: str
    share_id: int | None = None
    agent: bool = False


@dataclass
class Usable:
    accounts: list[Account] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)  # why an account of the prefix is left out

    def why(self, prefix: str) -> str:
        found = "; ".join(dict.fromkeys(self.reasons))
        return f"no provider account of {prefix} that this run may use" + (f": {found}" if found else "")


def claim_gate(agent_version: str | None):
    """What a daemon of ``agent_version`` may claim of the gateway's runs: all of them from GATEWAY_RUN_AGENT on, as
    every runtime of the core takes one (``pick_runtime`` turns any into one of them); none before."""
    if gateway_runs.takes_gateway_runs(agent_version):
        return true()
    return tables.runs.c.gateway_provider.is_(None)


def _of_prefix(prefix: str):
    """The provider accounts whose models ``prefix`` names (a condition on provider_accounts)."""
    p = tables.provider_accounts
    clause = p.c.provider.in_(gateway_runs.providers_of(prefix))
    custom = gateway_runs.custom_id(prefix)
    return clause if custom is None else and_(clause, p.c.secret_id == custom)


def _rows(owner, project_id: int, on_worker, prefix: str):
    """The live provider accounts ``owner`` says, of ``prefix``, bound to the project where ``on_worker`` says."""
    s, p, a, b = tables.secrets, tables.provider_accounts, tables.accounts, tables.secret_bindings
    bound = exists().where(b.c.secret_id == s.c.id, b.c.project_id == project_id, on_worker)
    return (
        select(
            s.c.id,
            s.c.name,
            func.coalesce(a.c.paused_at.is_not(None), False).label("paused"),
            func.coalesce(s.c.expires_at <= func.now(), False).label("ended"),
        )
        .join_from(s, p, p.c.secret_id == s.c.id)
        .outerjoin(a, a.c.secret_id == s.c.id)
        .where(owner, s.c.kind == PROVIDER_KIND, s.c.deleted_at.is_(None), bound, _of_prefix(prefix))
        .order_by(s.c.name, s.c.id)
    )


def _sort(asker: Asker, rows, usable: Usable, picked: int | None, agent: bool) -> None:
    for row in rows:
        if picked is not None and row.id != picked:
            continue
        if row.paused:
            usable.reasons.append(f"account {row.id} is paused")
        elif row.ended:
            usable.reasons.append(f"account {row.id} is past its end")
        elif not asker.allows(row.name):
            usable.reasons.append(f"the run's agent does not allow account {row.id}")
        else:
            usable.accounts.append(Account(row.id, row.name, agent=agent))


async def usable_accounts(
    conn: AsyncConnection, asker: Asker, worker_id: int | None, prefix: str, picked: int | None = None
) -> Usable:
    """The provider accounts of ``prefix`` that ``asker`` may use on worker ``worker_id`` (None: any it may go to),
    as the module says; only ``picked`` when one is."""
    b, s = tables.secret_bindings, tables.secrets
    usable = Usable()
    if asker.agent_owned:
        on_worker = true() if worker_id is None else b.c.worker_id == worker_id
        owner = s.c.profile_id == asker.profile_id
        rows = (await conn.execute(_rows(owner, asker.profile_project_id, on_worker, prefix))).all()
        if rows:
            _sort(asker, rows, usable, picked, agent=True)
            return usable
    any_worker = or_(b.c.worker_id.is_(None), true() if worker_id is None else b.c.worker_id == worker_id)
    own = (await conn.execute(_rows(s.c.owner_id == asker.owner_id, asker.project_id, any_worker, prefix))).all()
    _sort(asker, own, usable, picked, agent=False)
    shared, refused = await share_leases.shared_secrets(conn, asker, worker_id or 0, lock=False, kinds=(PROVIDER_KIND,))
    ids = [row.id for row in shared if picked is None or row.id == picked]
    p = tables.provider_accounts
    mine = set((await conn.execute(select(p.c.secret_id).where(p.c.secret_id.in_(ids), _of_prefix(prefix)))).scalars())
    seen = {account.secret_id for account in usable.accounts}
    for row in shared:
        if row.id in mine and row.id not in seen and asker.allows(row.name):
            usable.accounts.append(Account(row.id, row.name, share_id=row.share_id))
            seen.add(row.id)
    usable.reasons += [refusal.text for refusal in refused]
    return usable


async def _known(conn: AsyncConnection, asker: Asker, prefix: str) -> bool:
    """Whether the dispatcher, the run's agent or a share for the project has any provider account of ``prefix``,
    usable now or not: what makes a model of opencode's own naming a gateway model."""
    s, p, cs, csp = tables.secrets, tables.provider_accounts, tables.credential_shares, tables.credential_share_projects
    shared = exists().where(
        cs.c.secret_id == s.c.id,
        cs.c.revoked_at.is_(None),
        exists().where(csp.c.share_id == cs.c.id, csp.c.project_id == asker.project_id),
    )
    whose = [s.c.owner_id == asker.owner_id, shared]
    if asker.agent_owned:
        whose.append(s.c.profile_id == asker.profile_id)
    statement = select(
        exists().where(
            p.c.secret_id == s.c.id,
            s.c.kind == PROVIDER_KIND,
            s.c.deleted_at.is_(None),
            or_(*whose),
            _of_prefix(prefix),
        )
    )
    return bool((await conn.execute(statement)).scalar_one())


async def prefix_for(conn: AsyncConnection, asker: Asker, runtime: str, model: str | None) -> str | None:
    """The prefix ``model`` goes through the gateway with on ``runtime``, as the module says; None when it does not."""
    split = gateway_runs.split(model)
    if split is None:
        return None
    if runtime in ("opencode", "any") and not await _known(conn, asker, split[0]):
        return None  # opencode's own name for a model of its providers
    return split[0]


def _picked(asked, prefix: str | None) -> int | None:
    """The provider account picked by hand for the run; 422 for one of another prefix, or for a model the gateway does
    not serve."""
    found = [picked for picked in asked.picked if picked.env_var.startswith(PICK_PREFIX)]
    if not found:
        return None
    pick = found[0]
    if prefix is None or pick.env_var != PICK_PREFIX + prefix:
        named = pick.env_var.removeprefix(PICK_PREFIX)
        raise HTTPException(
            422,
            f"account {pick.name} is a provider account: it goes with a model {named}/<model> of its provider, which "
            f"the run does not ask for; {NOTHING}",
        )
    return pick.secret_id


async def check_dispatch(conn: AsyncConnection, asker: Asker, asked, model: str | None) -> str | None:
    """The prefix the run's ``model`` goes through the gateway with, or None, as the module says; the HTTPException
    that says why the dispatch cannot go."""
    prefix = await prefix_for(conn, asker, asked.runtime, model)
    picked = _picked(asked, prefix)
    if prefix is None:
        return None
    if asked.runtime not in (*GATEWAY_RUNTIMES, "any"):
        raise HTTPException(
            422,
            f"model {model} goes through the model gateway, which runtime {asked.runtime} cannot use: dispatch it on "
            f"{', '.join(GATEWAY_RUNTIMES)} or any; {NOTHING}",
        )
    pinned = asked.pinned
    usable = await usable_accounts(conn, asker, None if pinned is None else pinned.id, prefix, picked)
    if not usable.accounts:
        where = "" if pinned is None else f" on worker {pinned.name}"
        raise HTTPException(422, f"model {model}: {usable.why(prefix)}{where}; {NOTHING}")
    if pinned is not None and not gateway_runs.takes_gateway_runs(pinned.agent_version):
        version = pinned.agent_version or "of an unknown version"
        needed = runs.version_text(gateway_runs.GATEWAY_RUN_AGENT)
        raise HTTPException(
            409,
            f"worker {pinned.name} cannot take this run: its daemon (evo-agents {version}) does not run a model through "
            f"the gateway, which takes {needed} or later; upgrade it and restart its daemon; {NOTHING}",
        )
    return prefix
