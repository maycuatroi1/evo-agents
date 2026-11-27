"""Which provider accounts a run on a gateway model may use (``evo_agents.hub.gateway_runs``), by the rules of any
account (``docs/credentials.md``, sections "Accounts" and "Sharing"), and the check a dispatch makes of them.

The accounts are those of the run's prefix (the catalog's alias of their provider, or evo-<id> for an endpoint a member
named) that are live, not paused and not past their end: the agent's own when the run goes under an agent with a name
that has any bound to its project on the worker (its runs take those, whoever dispatches them, as for env accounts);
else the dispatcher's own, bound to the run's project on any worker or on this one, then those shared with the run's
principal that lend now (``share_leases.shared_secrets``, with its refusals). The run's profile must allow each by
name (its credential axis). An account picked by hand is the only one. Before a worker holds the run, at its dispatch,
the worker is the one it is pinned to, or any.

Under quota-pacing (the policy of the run's account pool, else the dispatcher's for the project; an agent's own accounts
follow DEFAULT_POLICY), ``paced_accounts`` narrows them to the one ``evo_agents.hub.accounts.pick`` gives, among those
not cooling down when any is not: the run's key of the gateway reaches that account alone (``service.leases``).

``check_dispatch`` answers the prefix a dispatch's model goes through the gateway with, or None. On claude-code and
codex a model whose prefix the gateway serves always does; on opencode, or with runtime any, whose own model names
read ``<provider>/<model>`` too, it does when the dispatcher, the agent or a share has an account of that prefix at
all. A run that would go through the gateway with no account it may use is 422, saying why, and so is a plugin's
runtime, which never does; one pinned to a worker whose daemon is older than GATEWAY_RUN_AGENT is 409.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import gateway_runs, runs
from evo_agents.hub.accounts import FIVE_HOUR, QUOTA_PACING, SEVEN_DAY, Candidate, pick, window_of
from evo_agents.hub.credentials import PROVIDER_KIND
from evo_agents.hub.gateway_runs import GATEWAY_RUNTIMES, PICK_PREFIX
from evo_agents.hub.server import share_leases  # a module: it imports worker_placement, which reaches this one
from evo_agents.hub.server.account_budgets import budget_windows, with_budgets
from evo_agents.hub.server.account_choice import DEFAULT_POLICY, account_policy, pacing_of, pool_policy
from evo_agents.hub.server.base.errors import Conflict, Unprocessable
from evo_agents.hub.server.gateway.models.accounts import NOTHING, Account, Asker, Usable
from evo_agents.hub.server.gateway.repository import accounts as repo


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
    usable = Usable()
    pooled, pool_id = await repo.account_pool(conn, asker.profile_id)
    if pooled:  # an agent: its account pool alone, no account of the dispatcher's or a share's
        if pool_id is not None:
            rows = await repo.pool_rows(conn, pool_id, asker.project_id, prefix)
            _sort(asker, rows, usable, picked, agent=True)
        return usable
    if asker.agent_owned:
        rows = await repo.agent_rows(conn, asker.profile_id, asker.profile_project_id, worker_id, prefix)
        if rows:
            _sort(asker, rows, usable, picked, agent=True)
            return usable
    own = await repo.own_rows(conn, asker.owner_id, asker.project_id, worker_id, prefix)
    _sort(asker, own, usable, picked, agent=False)
    shared, refused = await share_leases.shared_secrets(conn, asker, worker_id or 0, lock=False, kinds=(PROVIDER_KIND,))
    ids = [row.id for row in shared if picked is None or row.id == picked]
    mine = await repo.of_prefix_ids(conn, ids, prefix)
    seen = {account.secret_id for account in usable.accounts}
    for row in shared:
        if row.id in mine and row.id not in seen and asker.allows(row.name):
            usable.accounts.append(Account(row.id, row.name, share_id=row.share_id))
            seen.add(row.id)
    usable.reasons += [refusal.text for refusal in refused]
    return usable


async def _policy(conn: AsyncConnection, asker: Asker, accounts: list[Account]) -> dict:
    pooled, pool_id = await repo.account_pool(conn, asker.profile_id)
    if pooled:
        return await pool_policy(conn, pool_id)
    if any(account.agent for account in accounts):
        return dict(DEFAULT_POLICY)
    return await account_policy(conn, asker.owner_id, asker.project_id)


async def paced_accounts(conn: AsyncConnection, asker: Asker, accounts: list[Account]) -> list[Account]:
    """``accounts`` (``usable_accounts``), narrowed to the one quota-pacing gives when the policy says it, as the
    module says; all of them under any other strategy."""
    if len(accounts) < 2:
        return accounts
    policy = await _policy(conn, asker, accounts)
    if policy["strategy"] != QUOTA_PACING:
        return accounts
    ids = [account.secret_id for account in accounts]
    rows = await repo.pacing_rows(conn, ids)
    usages = with_budgets({i: rows[i].last_usage for i in rows}, await budget_windows(conn, ids))
    ready = [account for account in accounts if not (account.secret_id in rows and rows[account.secret_id].cooling)]
    candidates = [_candidate(account, rows.get(account.secret_id), usages.get(account.secret_id)) for account in ready]
    found = pick(
        candidates or [_candidate(account, None, None) for account in accounts], QUOTA_PACING, pacing_of(policy)
    )
    return [account for account in accounts if found is not None and account.secret_id == found[0].id]


def _candidate(account: Account, row, usage) -> Candidate:
    if row is None:
        return Candidate(account.secret_id, account.name)
    windows = window_of(usage, FIVE_HOUR), window_of(usage, SEVEN_DAY)
    return Candidate(account.secret_id, account.name, row.weight, row.priority, row.rotation, *windows)


async def prefix_for(conn: AsyncConnection, asker: Asker, runtime: str, model: str | None) -> str | None:
    """The prefix ``model`` goes through the gateway with on ``runtime``, as the module says; None when it does not."""
    split = gateway_runs.split(model)
    if split is None:
        return None
    if runtime in ("opencode", "any") and not await repo.known(
        conn, asker.owner_id, asker.project_id, split[0], agent_owned=asker.agent_owned, profile_id=asker.profile_id
    ):
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
        raise Unprocessable(
            f"account {pick.name} is a provider account: it goes with a model {named}/<model> of its provider, which "
            f"the run does not ask for; {NOTHING}",
        )
    return pick.secret_id


async def check_dispatch(conn: AsyncConnection, asker: Asker, asked, model: str | None) -> str | None:
    """The prefix the run's ``model`` goes through the gateway with, or None, as the module says; the domain error
    that says why the dispatch cannot go."""
    prefix = await prefix_for(conn, asker, asked.runtime, model)
    picked = _picked(asked, prefix)
    if prefix is None:
        return None
    if asked.runtime not in (*GATEWAY_RUNTIMES, "any"):
        raise Unprocessable(
            f"model {model} goes through the model gateway, which runtime {asked.runtime} cannot use: dispatch it on "
            f"{', '.join(GATEWAY_RUNTIMES)} or any; {NOTHING}",
        )
    pinned = asked.pinned
    usable = await usable_accounts(conn, asker, None if pinned is None else pinned.id, prefix, picked)
    if not usable.accounts:
        where = "" if pinned is None else f" on worker {pinned.name}"
        raise Unprocessable(f"model {model}: {usable.why(prefix)}{where}; {NOTHING}")
    if pinned is not None and not gateway_runs.takes_gateway_runs(pinned.agent_version):
        version = pinned.agent_version or "of an unknown version"
        needed = runs.version_text(gateway_runs.GATEWAY_RUN_AGENT)
        raise Conflict(
            f"worker {pinned.name} cannot take this run: its daemon (evo-agents {version}) does not run a model through "
            f"the gateway, which takes {needed} or later; upgrade it and restart its daemon; {NOTHING}",
        )
    return prefix
