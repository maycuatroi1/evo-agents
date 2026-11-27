"""What an admin of a project does with another member's run of it, which only the member who dispatched the run did
before: cancel it, approve it, run it again (``controls``), list the candidates to run it again (``candidates``), and
answer its decisions, a credential request among them (``evo_agents.hub.server.decisions``,
``evo_agents.hub.server.credentials.service.answers``).

An admin here is a member signed in as themself (never a run's token: ``Principal.acts_for_run``) who holds the admin
role on the run's project, or a hub admin (EVO_HUB_ADMINS) who holds any grant on it: one without a grant sees none of
its runs, as before, and a rerun, which dispatches a run, needs the writer role as every dispatch does. Any other member
keeps the 403 of the member who dispatched the run alone. A run of the hub's own machinery stays out of reach as it was (a local run, a plan run rerun, and the
other refusals of each route): the admin does what the dispatcher could, no more.

Each act of an admin on another member's run, but a read, writes a ``system`` event into the run's log that names who
did it, as what, and who dispatched the run (``admin_action``, ``by``, ``as``, ``dispatched_by``), and tells the
dispatcher with a notice of kind ADMIN_NOTICE that links to the run. The state event of a move the act makes (a cancel,
an approval) keeps the actor ``owner`` of the table of moves, with a reason that names the admin.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub.server.projects import ProjectAccess
from evo_agents.hub.server.run_state import write_event
from evo_agents.hub.server.security import Principal, holds

ADMIN_NOTICE = "run_admin_action"  # runs.NOTICE_KINDS: an admin acted on a run the member dispatched
PROJECT_ADMIN, HUB_ADMIN = "project_admin", "hub_admin"  # what the admin acted as, in the event's ``as``


@dataclass(frozen=True)
class OnBehalf:
    """An admin acting on a run another member dispatched: that member, and what the admin acts as."""

    owner_id: int
    owner: str  # the login of the member who dispatched the run
    standing: str  # PROJECT_ADMIN or HUB_ADMIN
    project: str

    @property
    def as_what(self) -> str:
        if self.standing == PROJECT_ADMIN:
            return f"an admin of project {self.project}"
        return "a hub admin"


def standing(user: Principal, access: ProjectAccess) -> str | None:
    """PROJECT_ADMIN or HUB_ADMIN when ``user``, signed in as themself, may act on another member's run of the project
    ``access`` reads; None otherwise."""
    if user.acts_for_run is not None:
        return None
    if holds(access.role, "admin"):
        return PROJECT_ADMIN
    return HUB_ADMIN if user.admin else None


def on_behalf(user: Principal, access: ProjectAccess, owner_id: int, owner: str) -> OnBehalf | None:
    """How ``user`` acts on a run that member ``owner_id`` (login ``owner``) dispatched: None when they dispatched it
    themself, else as an admin; raises LookupError for a member who is neither, whom the caller refuses."""
    if owner_id == user.user_id:
        return None
    held = standing(user, access)
    if held is None:
        raise LookupError(owner)
    return OnBehalf(owner_id, owner, held, access.name)


def acted(user: Principal, behalf: OnBehalf | None, did: str) -> str:
    """The reason a move names: ``did`` by ``user``, and for an admin, for whom."""
    if behalf is None:
        return f"{user.login} {did}"
    return f"{user.login}, {behalf.as_what}, {did} for {behalf.owner}, who dispatched it"


async def record_act(
    conn: AsyncConnection,
    user: Principal,
    access: ProjectAccess,
    run_id: int,
    behalf: OnBehalf,
    action: str,
    did: str,
    *,
    decision_id: int | None = None,
    extra: dict | None = None,
) -> None:
    """Write the event of an admin's act ``action`` (``did``, such as "cancelled it") into the log of run ``run_id``,
    whose row the caller holds locked, and tell the member who dispatched it, in the caller's transaction."""
    from evo_agents.hub.server.notifications.service.notifier import (
        NOTIFIER,  # it imports the service of runs, which imports this module
    )

    text = acted(user, behalf, did)
    body = {
        "text": text,
        "admin_action": action,
        "by": user.login,
        "as": behalf.standing,
        "dispatched_by": behalf.owner,
        **(extra or {}),
    }
    if decision_id is not None:
        body["decision_id"] = decision_id
    await write_event(conn, run_id, body)
    await NOTIFIER.notify(
        conn,
        user_id=behalf.owner_id,
        kind="notice",
        notice_kind=ADMIN_NOTICE,
        project_id=access.project_id,
        run_id=run_id,
        title=f"Run #{run_id}: {text}",
        body=text,
        details={
            "action": action,
            "by": user.login,
            "as": behalf.standing,
            "decision_id": decision_id,
            **(extra or {}),
        },
        link=NOTIFIER.run_link(access.name, run_id),
    )
