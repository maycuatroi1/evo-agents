"""What the choice of an account for a run reads and decides (``evo_agents.hub.server.accounts.service.choice``): a
secret and a run as it reads them, the state of an account, a choice, a failover, and the cooldown of an OAuth account
that needs a new sign-in."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from evo_agents.hub.accounts import Window

RELOGIN_REASON = "needs-relogin: the OAuth refresh token was refused; run evo-agents hub account oauth-login again"
RELOGIN_SPAN = timedelta(days=3650)  # a cooldown only a new sign-in ends


class Secret(Protocol):
    """What ``choose_accounts`` reads of a secret (``credentials._Secret``)."""

    id: int
    name: str
    kind: str
    env_var: str | None
    on_worker: bool
    share_id: int | None  # the share another member lent it under; None for the owner's own
    for_run: bool  # that share is for this run alone (``share_leases.for_run``): it comes first for its variable
    agent: str | None  # the handle of the agent profile that owns it; None for a member's


class Run(Protocol):
    id: int
    owner_id: int
    project_id: int
    curator: object | None


@dataclass(frozen=True)
class AccountState:
    weight: int
    priority: int
    paused: bool
    cooling_until: datetime | None  # set while it cools down
    rotation: float
    five_hour: Window | None = None  # its usage windows, for quota-pacing
    seven_day: Window | None = None


@dataclass(frozen=True)
class Choice:
    secret: Secret | None
    how: str | None  # None: the run keeps what it took, nothing to record
    rotation: float | None = None  # the account's place once taken, for a rotating strategy
    note: str | None = None  # the text of the run's event


@dataclass(frozen=True)
class Failover:
    """What ``fail_over`` decided: the account the run moves to, or why it stays and when one is ready first."""

    secret: Secret | None  # None: the run cannot move
    failovers: int  # the times the run moved, this one included
    cooling_until: datetime  # until when the limited account cools down
    reason: str | None = None  # why the run cannot move
    ready_at: datetime | None = None  # when it cannot: the earliest end of a cooldown among its accounts
