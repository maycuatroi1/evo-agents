"""Which secrets a run may be leased (``docs/credentials.md``, sections "Accounts" and "Sharing"): its principal's
own (``own_secrets``) and those another member shared with them (``shared_secrets``), read in the transactions of
``credentials.lease_credentials`` and ``rate_limits``; and what the hub takes back when a share ends.

The principal of a run is the member who dispatched it (runs.dispatched_by): runs.principal_id of plan
autonomy-levels is not in the schema yet. Each lease records the principal it was made for
(credential_leases.principal_id), so the cost of the runs that took an account splits by principal
(``account_views``).

A share covers a run when it is not revoked, its end has not passed, it is for the run's project, and its grantee is
the principal (``user``), a live unit holding the principal's live primary seat or a unit above that seat
(``unit``), or anyone (``all``). Its secret is live, of a kind the hub leases, not the principal's own, and still bound
to the project on any worker of its owner; it counts as on this worker when it is bound to this one. A run of the
Curator and a release run never take a shared secret: their charter and their runtime's login are their own.

A run that holds a lease under a share keeps being lent it. Otherwise the share lends only when each of these holds,
and the first that does not is the share's refusal, which goes to the run's events naming the share by its id, never
the secret's name or value (``note_refusals``), when the run got nothing else for the variable or origin:

- its owner has not paused the account and it does not cool down: while it waits, the owner's runs go first (they
  take a cooling account as a last resort, ``account_choice._fallback``; a run under a share never does);
- the highest usage among the windows of accounts.last_usage whose reset has not passed is under the share's
  usage_ceiling_pct (``peak_usage``); an account whose usage is unknown is lent;
- fewer than max_running runs hold a lease under the share now;
- the cost of the runs that took a lease under the share this month (UTC, as ``account_views`` counts a run's cost),
  plus an estimate for each run holding one that reported no cost yet and for this run, is within budget_usd. The
  estimate is the mean cost of the share's runs this month that reported one, else ESTIMATE_USD, and never under
  MIN_ESTIMATE_USD: a run is never taken to cost nothing.

The principal's own secrets come first: ``account_choice`` takes a shared account only when none of the principal's
own for the variable is ready, and ``credentials`` a shared git secret for an origin only when none of the
principal's own covers it.

A share revoked ends the leases still out under it at once (``credential_shares.revoke_share``, with
``end_share_leases``). A share that ended, or no longer covers a run, ends that run's leases under it at the run's next
ask (``end_unlent``), and on every pass of the reaper for any run (``end_lapsed``).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import and_, distinct, exists, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import runs, tables
from evo_agents.hub.credential_shares import ALL, UNIT, USER
from evo_agents.hub.credentials import LEASED_KINDS
from evo_agents.hub.server import org_store
from evo_agents.hub.server.account_oauth import OAUTH_ENV_VAR
from evo_agents.hub.server.account_views import month_start, usage_cost

ESTIMATE_USD = Decimal("1.00")  # what a run under a share is taken to cost before any of its runs reported a cost
MIN_ESTIMATE_USD = Decimal("0.01")


@dataclass(frozen=True)
class Refusal:
    """Why a share lent its secret to no run this time."""

    share_id: int
    kind: str  # the secret's: env or git
    env_var: str | None
    url_prefix: str | None
    text: str


# What a run may be leased


def _secret_columns():
    s = tables.secrets
    return (
        s.c.id,
        s.c.name,
        s.c.kind,
        s.c.env_var,
        s.c.url_prefix,
        s.c.username,
        s.c.sealed,
        s.c.nonce,
        s.c.key_id,
        s.c.expires_at,
        s.c.owner_id,
    )


def _live(refreshable: bool):
    """A secret not past its end; with ``refreshable``, an OAuth account past it too, whose token a lease renews."""
    s, a = tables.secrets, tables.accounts.alias("signed_in")  # apart from the accounts row ``shared_secrets`` joins
    live = s.c.expires_at > func.now()
    if refreshable:
        signed_in = exists().where(a.c.secret_id == s.c.id, a.c.oauth_sealed.is_not(None))
        live = or_(live, and_(s.c.kind == "env", s.c.env_var == OAUTH_ENV_VAR, signed_in))
    return or_(s.c.expires_at.is_(None), live)


def _on_worker(project_id: int, worker_id: int):
    s, b = tables.secrets, tables.secret_bindings
    return exists().where(b.c.secret_id == s.c.id, b.c.project_id == project_id, b.c.worker_id == worker_id)


def own_secrets(owner_id: int, project_id: int, worker_id: int, *, lock: bool, refreshable: bool = False):
    """The owner's secrets bound to the run's project, on any of their workers or on this one, live and not past their
    end, by name; whether each is bound to this worker. With ``lock`` their rows are shared-locked."""
    s, b = tables.secrets, tables.secret_bindings
    bound = exists().where(
        b.c.secret_id == s.c.id,
        b.c.project_id == project_id,
        or_(b.c.worker_id.is_(None), b.c.worker_id == worker_id),
    )
    statement = (
        select(*_secret_columns(), _on_worker(project_id, worker_id).label("on_worker"))
        .where(
            s.c.owner_id == owner_id,
            s.c.kind.in_(LEASED_KINDS),
            s.c.deleted_at.is_(None),
            _live(refreshable),
            bound,
        )
        .order_by(s.c.name)
    )
    return statement.with_for_update(read=True, of=s) if lock else statement


async def grantee_clause(conn: AsyncConnection, principal_id: int):
    """The shares whose grantee is ``principal_id``: theirs, those of a live unit holding their live primary seat or
    above it, and those for all."""
    chart, today = await org_store.load(conn), await org_store.today(conn)
    seat = chart.primary_seat(principal_id, today)
    units = []
    if seat is not None and not chart.ended(seat, today):
        units = [
            unit.id
            for unit in chart.ancestors(seat.unit_id)
            if unit.archived_at is None and (unit.ends_on is None or unit.ends_on >= today)
        ]
    cs = tables.credential_shares
    return or_(
        cs.c.grantee_kind == ALL,
        and_(cs.c.grantee_kind == USER, cs.c.grantee_user_id == principal_id),
        and_(cs.c.grantee_kind == UNIT, cs.c.grantee_unit_id.in_(units)),
    )


def live_share():
    """A share neither revoked nor past its end."""
    cs = tables.credential_shares
    return and_(cs.c.revoked_at.is_(None), or_(cs.c.expires_at.is_(None), cs.c.expires_at > func.now()))


async def shared_secrets(
    conn: AsyncConnection, run, worker_id: int, *, lock: bool, refreshable: bool = False
) -> tuple[list, list[Refusal]]:
    """(the rows of the secrets shared with the principal of ``run`` that the hub lends it now, each with its
    share_id; the refusals of the shares that lend nothing), as the module says. With ``lock`` the rows of the
    secrets and of their shares are shared-locked, so a share revoked meanwhile waits for these leases and ends them."""
    s, b, cs, csp, a = (
        tables.secrets,
        tables.secret_bindings,
        tables.credential_shares,
        tables.credential_share_projects,
        tables.accounts,
    )
    statement = (
        select(
            *_secret_columns(),
            _on_worker(run.project_id, worker_id).label("on_worker"),
            cs.c.id.label("share_id"),
            cs.c.usage_ceiling_pct,
            cs.c.max_running,
            cs.c.budget_usd,
            a.c.last_usage,
            a.c.paused_at.is_not(None).label("paused"),
            func.coalesce(a.c.cooldown_until > func.now(), False).label("cooling"),
        )
        .join_from(cs, s, s.c.id == cs.c.secret_id)
        .outerjoin(a, a.c.secret_id == s.c.id)
        .where(
            live_share(),
            exists().where(csp.c.share_id == cs.c.id, csp.c.project_id == run.project_id),
            await grantee_clause(conn, run.owner_id),
            s.c.owner_id != run.owner_id,
            s.c.kind.in_(LEASED_KINDS),
            s.c.deleted_at.is_(None),
            _live(refreshable),
            exists().where(b.c.secret_id == s.c.id, b.c.project_id == run.project_id),
        )
        .order_by(s.c.name, s.c.id, cs.c.id)
    )
    rows = (await conn.execute(statement.with_for_update(read=True, of=(s, cs)) if lock else statement)).all()
    if not rows:
        return [], []
    cl = tables.credential_leases
    held = select(cl.c.share_id).where(cl.c.run_id == run.id, cl.c.share_id.is_not(None), cl.c.revoked_at.is_(None))
    kept = set((await conn.execute(held)).scalars())
    lent, refused, seen = [], [], set()
    for row in rows:
        if row.id in seen:  # lent already under another share
            continue
        why = None if row.share_id in kept else await _refusal(conn, run.id, row)
        if why is None:
            seen.add(row.id)
            lent.append(row)
        else:
            refused.append(Refusal(row.share_id, row.kind, row.env_var, row.url_prefix, why))
    return lent, [refusal for refusal in refused if refusal.share_id not in {row.share_id for row in lent}]


# Whether a share lends now


def _when(value) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        at = datetime.fromisoformat(value)
    except ValueError:
        return None
    return at if at.tzinfo is not None else at.replace(tzinfo=UTC)


def peak_usage(usage, now: datetime) -> float | None:
    """The highest utilization in ``usage`` (accounts.last_usage) among its windows whose reset has not passed, the
    limit it reported first among them; None when it gives none."""
    if not isinstance(usage, dict):
        return None
    found: list[float] = []
    windows = usage.get("windows")
    for window in [usage, *(windows.values() if isinstance(windows, dict) else [])]:
        if not isinstance(window, dict):
            continue
        value, resets = window.get("utilization"), _when(window.get("resets_at"))
        if isinstance(value, int | float) and not isinstance(value, bool) and (resets is None or resets > now):
            found.append(float(value))
    return max(found, default=None)


def _holders(share_id: int, run_id: int):
    """How many other runs a worker holds now with a lease under the share."""
    cl, r = tables.credential_leases, tables.runs
    return (
        select(func.count(distinct(cl.c.run_id)))
        .join_from(cl, r, r.c.id == cl.c.run_id)
        .where(
            cl.c.share_id == share_id,
            cl.c.revoked_at.is_(None),
            cl.c.run_id != run_id,
            r.c.state.in_(runs.HELD_STATES),
        )
    )


def _spent(share_id: int, run_id: int):
    """What the other runs that took a lease under the share this month cost: (spent, runs that reported a cost,
    runs held now that reported none)."""
    cl, r = tables.credential_leases, tables.runs
    took = (
        select(cl.c.run_id)
        .where(cl.c.share_id == share_id, cl.c.run_id != run_id)
        .group_by(cl.c.run_id)
        .having(func.min(cl.c.issued_at) >= month_start())
        .subquery()
    )
    cost = usage_cost(r.c.usage)
    return select(
        func.coalesce(func.sum(cost), 0).label("spent"),
        func.count().filter(cost > 0).label("costed"),
        func.count().filter(cost <= 0, r.c.state.in_(runs.HELD_STATES)).label("pending"),
    ).join_from(took, r, r.c.id == took.c.run_id)


def estimate(spent: Decimal, costed: int) -> Decimal:
    """What a run under a share is taken to cost: the mean of its runs this month that reported a cost, else
    ESTIMATE_USD; never under MIN_ESTIMATE_USD."""
    mean = spent / costed if costed else ESTIMATE_USD
    return max(mean, MIN_ESTIMATE_USD)


def _money(value: Decimal) -> str:
    return f"{value.quantize(Decimal('0.01'))} USD"


async def _refusal(conn: AsyncConnection, run_id: int, row) -> str | None:
    """Why the share of ``row`` lends its secret to run ``run_id`` not now; None when it lends."""
    share = f"share {row.share_id}"
    if row.paused:
        return f"{share} lends nothing now: its owner paused the account"
    if row.cooling:
        return f"{share} lends nothing now: the account cools down, and its owner's runs go first"
    peak = peak_usage(row.last_usage, datetime.now(UTC))
    if row.usage_ceiling_pct is not None and peak is not None and peak >= row.usage_ceiling_pct:
        return (
            f"{share} lends nothing now: the account's usage is {peak:g}%, at or above the share's ceiling of "
            f"{row.usage_ceiling_pct}%"
        )
    if row.max_running is not None:
        holding = (await conn.execute(_holders(row.share_id, run_id))).scalar_one()
        if holding >= row.max_running:
            return f"{share} lends nothing now: {holding} runs hold it, the most it allows"
    if row.budget_usd is not None:
        spent, costed, pending = (await conn.execute(_spent(row.share_id, run_id))).one()
        each = estimate(Decimal(spent), costed)
        total = Decimal(spent) + each * (pending + 1)
        if total > row.budget_usd:
            return (
                f"{share} lends nothing now: its runs this month cost {_money(Decimal(spent))}, with "
                f"{pending + 1} run(s) estimated at {_money(each)} each that makes {_money(total)}, over its budget "
                f"of {_money(row.budget_usd)}"
            )
    return None


async def note_refusals(conn: AsyncConnection, run_id: int, refused: list[Refusal], env_vars, missing) -> None:
    """Write to the run's events why each share in ``refused`` lent nothing, when the run got no secret for the
    variable (``env_vars``: those it got) or the share's secret was for an origin it got nothing for (``missing``)."""
    from evo_agents.hub.credentials import matches
    from evo_agents.hub.server.run_state import write_event  # it imports the routes that import this module

    for refusal in refused:
        if refusal.kind == "env" and refusal.env_var in env_vars:
            continue
        if refusal.kind == "git" and not any(origin and matches(refusal.url_prefix, origin) for origin in missing):
            continue
        body = {"text": refusal.text, "share_id": refusal.share_id}
        if refusal.env_var is not None:
            body["env_var"] = refusal.env_var
        await write_event(conn, run_id, body)


# Taking back


async def end_unlent(conn: AsyncConnection, run_id: int, worker_id: int, lent: set[int]) -> int:
    """Mark revoked the leases run ``run_id`` holds on this worker under a share not in ``lent``, the shares it was
    lent this time; how many."""
    cl = tables.credential_leases
    statement = (
        update(cl)
        .where(
            cl.c.run_id == run_id,
            cl.c.worker_id == worker_id,
            cl.c.share_id.is_not(None),
            cl.c.share_id.not_in(lent) if lent else cl.c.share_id.is_not(None),
            cl.c.revoked_at.is_(None),
        )
        .values(revoked_at=func.now())
    )
    return (await conn.execute(statement)).rowcount


async def end_share_leases(conn: AsyncConnection, share_id: int) -> int:
    """Mark revoked every lease still out under share ``share_id``; how many."""
    cl = tables.credential_leases
    statement = update(cl).where(cl.c.share_id == share_id, cl.c.revoked_at.is_(None)).values(revoked_at=func.now())
    return (await conn.execute(statement)).rowcount


async def end_lapsed(conn: AsyncConnection) -> int:
    """Mark revoked every lease still out under a share that was revoked or has ended; how many."""
    cl, cs = tables.credential_leases, tables.credential_shares
    lapsed = select(cs.c.id).where(~live_share())
    statement = update(cl).where(cl.c.share_id.in_(lapsed), cl.c.revoked_at.is_(None)).values(revoked_at=func.now())
    return (await conn.execute(statement)).rowcount
