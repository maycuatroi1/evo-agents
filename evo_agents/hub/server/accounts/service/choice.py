"""Which of its owner's accounts a run gets for each variable (``docs/credentials.md``, section "Accounts"), chosen in
the transaction of ``credentials.lease_credentials`` that leases them, while the run's row is locked.

``choose_accounts`` takes the env secrets the run may be leased (``share_leases``: the owner's, bound to the run's
project on any of their workers or on this one, live and not past their end, and those other members shared with the
owner that their shares lend now; for a run of the Curator only those its charter lists) and gives back one for each
variable:

0. the secret a share for this run alone lends (``share_leases.for_run``): the value an admin of the project gave for
   it in answer to its credential request, which comes before any other for its variable;
1. the account the run took already (run_accounts), the one its owner picked when dispatching it among them, or the
   one the run it resumes took, while it is still one of those secrets: a run keeps its account on every ask for its
   leases, whatever happened to the account since; else the secret of a lease the run holds for that variable. A
   shared account picked by hand is one of them only while its share lends it: meanwhile the next tiers choose;
2. else among the accounts bound to this worker (for a run of the Curator: the accounts its charter lists), then
   among the others bound to the project, by the strategy of the owner's policy for the project
   (``evo_agents.hub.accounts.pick``), skipping each account its owner paused or that cools down; then, the same way,
   among the accounts shared with the owner (how ``shared``): the owner's own come first;
3. else, when every account of the owner's left cools down, the one whose cooldown ends first, and a ``system`` event
   of the run says so; when every account is paused, none, and an event says that.

The accounts of an agent profile (``agent_accounts``) come in the same list, and ``credentials.survey_run`` leaves out
of it every secret of the dispatcher for a variable the agent has an account for on this worker, so a variable's group
is the agent's or the member's, never both. An agent's accounts are chosen by DEFAULT_POLICY (its strategy, cooldown,
failover and ``max_failovers``), and those of an account pool by the pool's strategy and quota-pacing thresholds
(``pools``, DEFAULT_POLICY for the rest), by the same tiers (all of them are bound to this worker), and fail over among
the agent's accounts alone. Every account of the agent for a variable past its end (``ended``): the run gets none for
it, and an event says so.

Quota-pacing weighs the usage windows of each account (``accounts.last_usage``), its weekly budget's window replacing
the ``seven_day`` one where the owner set one (``budgets``).

Each event names an account by its secret's id, never its name: the run's log is read by every reader of the project.

Two runs asking at once cannot take the same turn of a rotation: before it reads them, the ask creates the accounts
rows of the secrets it weighs (none existing) and locks them, in the order of their ids, until it commits.

``fail_over`` is what the hub does when a run's account hits its usage limit (``rate_limits``): the account cools down
until the reset its runtime gave, else for the policy's ``cooldown_seconds``; then, unless the account was picked by
hand, the policy turns failover off or the run failed over ``max_failovers`` times already, the run takes the next
ready account by the same tiers and strategy, its run_accounts row moves to it and counts one more failover. With no
account ready it stays and stops, and the answer says when the first one is ready again.

An OAuth account whose refresh the token endpoint refused (``refresh``: the refresh token was revoked or has ended)
needs a new sign-in: ``mark_relogin`` puts it in a cooldown that lasts ``RELOGIN_SPAN``, with the reason
``RELOGIN_REASON``, and ``fail_over_relogin`` moves the runs that held it to another account as ``fail_over`` does.
The member ends the cooldown by running ``evo-agents hub account oauth-login`` again
(``repository.choice.clear_relogin``).

Any other cooldown ends when the owner resumes the account (``evo-agents hub account resume``,
``repository.choice.end_cooldown``): a usage limit that reset early, or a token renewed after a refused one, needs no
wait and no new sign-in.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub.accounts import (
    DEFAULT_POLICY,
    FIVE_HOUR,
    SEVEN_DAY,
    Candidate,
    Pacing,
    pick,
    usable,
    window_of,
)
from evo_agents.hub.server.accounts.models.choice import (
    RELOGIN_REASON,
    RELOGIN_SPAN,
    AccountState,
    Choice,
    Failover,
    Run,
    Secret,
)
from evo_agents.hub.server.accounts.repository import choice as repository
from evo_agents.hub.server.accounts.service.budgets import budget_windows, with_budgets


def pacing_of(policy: dict) -> Pacing:
    """The thresholds of quota-pacing ``policy`` holds."""
    return Pacing(
        policy.get("hard_reserve_pct", DEFAULT_POLICY["hard_reserve_pct"]),
        policy.get("five_hour_ceiling_pct", DEFAULT_POLICY["five_hour_ceiling_pct"]),
    )


async def _lock_accounts(conn: AsyncConnection, ids: list[int]) -> dict[int, AccountState]:
    """The accounts rows of ``ids``, made when missing and locked until the transaction ends, by secret id."""
    ordered = sorted(set(ids))
    rows = await repository.lock_accounts(conn, ordered)
    usages = with_budgets({row.secret_id: row.last_usage for row in rows}, await budget_windows(conn, ordered))
    return {
        row.secret_id: AccountState(
            weight=row.weight,
            priority=row.priority,
            paused=row.paused_at is not None,
            cooling_until=row.cooldown_until if row.cooling else None,
            rotation=row.rotation,
            five_hour=window_of(usages[row.secret_id], FIVE_HOUR),
            seven_day=window_of(usages[row.secret_id], SEVEN_DAY),
        )
        for row in rows
    }


def _candidate(secret: Secret, state: AccountState) -> Candidate:
    return Candidate(
        secret.id, secret.name, state.weight, state.priority, state.rotation, state.five_hour, state.seven_day
    )


def _tiers(secrets: list[Secret], curator: bool) -> list[tuple[str, list[Secret]]]:
    if curator:
        return [("charter", secrets)]
    own = [s for s in secrets if s.share_id is None]
    return [
        ("worker", [s for s in own if s.on_worker]),
        ("pool", [s for s in own if not s.on_worker]),
        ("shared", [s for s in secrets if s.share_id is not None]),
    ]


def _fallback(env_var: str, secrets: list[Secret], states: dict[int, AccountState], strategy: str) -> Choice:
    """Nothing is ready: the owner's own account whose cooldown ends first, else none since every one is paused. An
    account shared with the owner is never taken cooling: its owner's runs go first (``share_leases``)."""
    secrets = [s for s in secrets if s.share_id is None]
    if not secrets:
        return Choice(None, None, note=f"no account for {env_var} is ready: the run gets none")
    allowed = {c.id for c in usable([_candidate(s, states[s.id]) for s in secrets], strategy)}
    cooling = [s for s in secrets if s.id in allowed and not states[s.id].paused and states[s.id].cooling_until]
    if not cooling:
        return Choice(None, None, note=f"every account for {env_var} is paused: the run gets none")
    secret = min(cooling, key=lambda s: (states[s.id].cooling_until, s.id))  # pyright: ignore[reportArgumentType]
    until = states[secret.id].cooling_until.isoformat()  # pyright: ignore[reportOptionalMemberAccess]
    note = f"every account for {env_var} is cooling down: the run took secret {secret.id}, ready first, at {until}"
    return Choice(secret, "cooling", note=note)


def _given_for_run(secrets: list[Secret]) -> Secret | None:
    """The secret a share for this run alone lends (the first by id), given for it in answer to its request."""
    given = [secret for secret in secrets if getattr(secret, "for_run", False)]
    return min(given, key=lambda secret: secret.id) if given else None


def _decide(
    env_var: str,
    secrets: list[Secret],
    states: dict[int, AccountState],
    kept: int | None,
    held: set[int],
    context: tuple,
) -> Choice:
    policy, curator = context
    strategy = policy["strategy"]
    by_id = {secret.id: secret for secret in secrets}
    given = _given_for_run(secrets)
    if given is not None:
        return Choice(given, None if kept == given.id else "shared")
    if kept in by_id:
        return Choice(by_id[kept], None)
    tiers = _tiers(secrets, curator)
    for how, tier in tiers:
        for secret in tier:
            if secret.id in held:
                return Choice(secret, how)
    for how, tier in tiers:
        ready = [s for s in tier if not states[s.id].paused and states[s.id].cooling_until is None]
        found = pick([_candidate(s, states[s.id]) for s in ready], strategy, pacing_of(policy))
        if found is not None:
            rotation = found[1] if strategy != "fill-first" else None
            return Choice(by_id[found[0].id], how, rotation)
    return _fallback(env_var, secrets, states, strategy)


async def _ended_event(conn: AsyncConnection, run: Run, env_var: str) -> None:
    from evo_agents.hub.server.run_state import write_event  # it imports the routes that import this module

    handle = getattr(run, "profile", None)
    text = f"every account of agent @{handle} for {env_var} has ended: the run gets none, and not its dispatcher's"
    await write_event(conn, run.id, {"text": text, "env_var": env_var, "secret_id": None})


def follows_default(secret: Secret) -> bool:
    """Whether the account is one of an agent or of an account pool: chosen by DEFAULT_POLICY or the pool's, never a
    member's."""
    return secret.agent is not None or getattr(secret, "pool", None) is not None


async def _policy_of(conn: AsyncConnection, run: Run, secret: Secret, member: dict | None = None) -> dict:
    """The policy ``secret`` is chosen by for ``run``: its account pool's, DEFAULT_POLICY for an agent's own, else
    the member's (``member`` when read already)."""
    if getattr(secret, "pool", None) is not None:
        return await repository.pool_policy(conn, getattr(run, "account_pool_id", None))
    if secret.agent is not None:
        return dict(DEFAULT_POLICY)
    return member if member is not None else await repository.account_policy(conn, run.owner_id, run.project_id)


async def choose_accounts(
    conn: AsyncConnection, run: Run, secrets: list[Secret], held: set[int], ended: tuple[str, ...] = ()
) -> list[Secret]:
    """One env secret of ``secrets`` for each variable, by the variable's name, as the module says; ``held`` holds the
    ids of the env secrets the run holds a lease of, ``ended`` the variables whose accounts of the agent all ended.
    Records each new choice and writes the run's events."""
    by_var: dict[str, list[Secret]] = {}
    for secret in sorted((s for s in secrets if s.kind == "env"), key=lambda s: s.name):
        by_var.setdefault(secret.env_var, []).append(secret)  # pyright: ignore[reportArgumentType]
    if not by_var and not ended:
        return []
    ids = [secret.id for group in by_var.values() for secret in group]
    states = await _lock_accounts(conn, ids) if ids else {}
    kept = await repository.kept(conn, run.id)
    member = await repository.account_policy(conn, run.owner_id, run.project_id)
    chosen: list[Secret] = []
    for env_var in sorted(set(by_var) | set(ended)):
        if env_var not in by_var:  # every account of the agent for it has ended, and the dispatcher's is not leased
            await _ended_event(conn, run, env_var)
            continue
        owned = follows_default(by_var[env_var][0])  # an agent's or a pool's accounts, never by a member's policy
        context = (await _policy_of(conn, run, by_var[env_var][0], member), run.curator is not None and not owned)
        choice = _decide(env_var, by_var[env_var], states, kept.get(env_var), held, context)
        if choice.note is not None:
            from evo_agents.hub.server.run_state import write_event  # it imports the routes that import this module

            await write_event(conn, run.id, {"text": choice.note, "env_var": env_var, "secret_id": _id(choice)})
        if choice.secret is None:
            continue
        if choice.how is not None:
            await repository.record(conn, run.id, env_var, choice)
        chosen.append(choice.secret)
    return chosen


def _id(choice: Choice) -> int | None:
    return choice.secret.id if choice.secret is not None else None


# When a run's account hits its usage limit


def _why_not(how: str | None, policy: dict, failovers: int, whose: str = "its owner's") -> str | None:
    if how == "manual":
        return "its account was picked by hand"
    if not policy["failover"]:
        policy_of = "the owner's" if whose == "its owner's" else whose  # the words a member's run always had
        return f"{policy_of} policy turns failover off"
    if failovers >= policy["max_failovers"]:
        return f"it failed over {failovers} times, the most {whose} policy allows"
    return None


async def fail_over(
    conn: AsyncConnection, run: Run, secrets: list[Secret], env_var: str, current: int, limit: tuple
) -> Failover:
    """Cool account ``current`` down, which run ``run`` holds for ``env_var`` and which hit its usage limit, and move
    the run to another one of ``secrets`` when it may, as the module says. ``limit`` is (the reset the runtime gave or
    None, the cooldown's reason). A run that moved already answers the account it moved to, as a retry would."""
    resets_at, reason = limit
    own = [s for s in secrets if s.kind == "env" and s.env_var == env_var]
    held = next((s for s in own if s.id == current), None)
    owned = held is not None and follows_default(held)  # an agent's accounts: DEFAULT_POLICY, a pool's: its own
    policy = (
        await _policy_of(conn, run, held)
        if held is not None and owned
        else await repository.account_policy(conn, run.owner_id, run.project_id)
    )
    states = await _lock_accounts(conn, [s.id for s in own] + [current])
    now = datetime.now(UTC)
    until = (
        resets_at if resets_at is not None and resets_at > now else now + timedelta(seconds=policy["cooldown_seconds"])
    )
    until = await repository.cool(conn, current, until, reason)
    taken, how, failovers = await repository.taken(conn, run.id, env_var)
    others = [s for s in own if s.id != current]
    if failovers and taken != current and taken in {s.id for s in others}:
        return Failover(next(s for s in others if s.id == taken), failovers, until)
    why = _why_not(how, policy, failovers, "the default" if owned else "its owner's")
    if why is None:
        for tier_how, tier in _tiers(others, run.curator is not None and not owned):
            ready = [s for s in tier if not states[s.id].paused and states[s.id].cooling_until is None]
            found = pick([_candidate(s, states[s.id]) for s in ready], policy["strategy"], pacing_of(policy))
            if found is not None:
                rotation = found[1] if policy["strategy"] != "fill-first" else None
                secret = next(s for s in ready if s.id == found[0].id)
                await repository.record(conn, run.id, env_var, Choice(secret, tier_how, rotation), failovers + 1)
                return Failover(secret, failovers + 1, until)
        why = f"no other account for {env_var} is ready"
    cooling = [states[s.id].cooling_until for s in others if not states[s.id].paused and states[s.id].cooling_until]
    return Failover(None, failovers, until, why, min([until, *cooling]))  # pyright: ignore[reportArgumentType]


# An OAuth account whose refresh token is dead


async def mark_relogin(conn: AsyncConnection, secret_id: int) -> None:
    """Account ``secret_id`` needs a new sign-in: no run is given it until the member logs in again."""
    await repository.cool(conn, secret_id, datetime.now(UTC) + RELOGIN_SPAN, RELOGIN_REASON)


async def fail_over_relogin(conn: AsyncConnection, run: Run, secrets: list[Secret], rejected: set[int]) -> None:
    """Move run ``run`` off each account in ``rejected`` that it holds, as ``fail_over`` does for a usage limit; its
    lease of that account is revoked. A run that cannot move (picked by hand, failover off, no other account ready)
    keeps the account, whose token the lease leaves out once it has ended."""
    for secret in sorted((s for s in secrets if s.id in rejected and s.kind == "env"), key=lambda s: s.name):
        taken, _, _ = await repository.taken(conn, run.id, secret.env_var)  # pyright: ignore[reportArgumentType]
        if taken != secret.id:
            continue
        limit = (datetime.now(UTC) + RELOGIN_SPAN, RELOGIN_REASON)
        moved = await fail_over(conn, run, secrets, secret.env_var, secret.id, limit)  # pyright: ignore[reportArgumentType]
        if moved.secret is not None:
            await repository.revoke_secret_lease(conn, run.id, secret.id)
