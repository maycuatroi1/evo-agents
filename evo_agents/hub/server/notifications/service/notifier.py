"""The Notifier: what every module that tells a member something goes through. ``NOTIFIER.notify`` stores, in the
caller's transaction, one notification for one member and one delivery of it for each channel of the member's that is
on: the web, which every member has without a row of notification_channels (a delivery without a channel), and each
enabled row. The table is the outbox: nothing is sent while the transaction is open, and a hub that stops between the
commit and the send loses nothing. A notification of a project also goes to the channels of the isolated plugins the
project enabled and granted notifications:send, through the plugins' own outbox (``evo_agents.hub.plugin_events``). A
notification of the interrupt tier for its member (``evo_agents.hub.server.push.service.tiers``) also gets one
delivery on each push device of its member that reaches them (a delivery with a ``device_id``,
``evo_agents.hub.server.push.repository``).

The Notifier also writes what a notification says the way every notification says it: a title on one line
(``one_line``), a body cut to MAX_NOTICE_BODY_BYTES (``body``), and the links to a run's page (``run_link``) and to a
decision's answer form (``decision_link``)."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import bus, jobs, plugin_events, runs
from evo_agents.hub.server.notifications.models import TITLE_CHARS
from evo_agents.hub.server.notifications.repository import outbox
from evo_agents.hub.server.push import repository as push_repository
from evo_agents.hub.server.push.service import tiers


class Notifier:
    """Stores notifications in the outbox, and writes their titles, bodies and links."""

    def run_link(self, project: str, run_id: int) -> str:
        """The web page of a run, as a notification links to it."""
        return f"/p/{project}/runs/{run_id}"

    def decision_link(self, decision_id: int) -> str:
        """Where the web shows a decision and its answer form."""
        return f"/inbox?decision={decision_id}"

    def one_line(self, text: str, limit: int = TITLE_CHARS) -> str:
        """``text`` as one line of at most ``limit`` characters: control characters and runs of blanks become one
        space, and a longer line ends with "..."."""
        line = " ".join("".join(ch if ch.isprintable() else " " for ch in text).split())
        if len(line) <= limit:
            return line
        return line[: limit - 3].rstrip() + "..."

    def body(self, text: str | None) -> str | None:
        """A notification's body as it is stored: None for none or blanks, else cut to MAX_NOTICE_BODY_BYTES."""
        if text is None or not text.strip():
            return None
        return runs.clip(text, runs.MAX_NOTICE_BODY_BYTES)

    async def notify(
        self,
        conn: AsyncConnection,
        *,
        user_id: int,
        kind: str,
        title: str,
        body: str | None = None,
        notice_kind: str | None = None,
        project_id: int | None = None,
        run_id: int | None = None,
        decision_id: int | None = None,
        details: dict | None = None,
        link: str | None = None,
        proposal_id: int | None = None,
    ) -> int:
        """Store a notification for member ``user_id`` and a delivery of it for each of their channels that is on, the
        web included, in the caller's transaction; its id. ``title`` is made one line of at most TITLE_CHARS, ``body``
        cut to MAX_NOTICE_BODY_BYTES. A notice that only reports (``runs.report_only``) is stored read and delivered on
        the web alone: no channel of the member's gets it, nor a push device unless the member chose the interrupt
        tier for its kind, and Today's summary counts it instead."""
        if kind not in runs.NOTIFICATION_KINDS:
            raise ValueError(f"{kind!r} is not a kind of notification")
        report = runs.report_only(kind, notice_kind, details)
        notification_id = await outbox.store(
            conn,
            user_id=user_id,
            kind=kind,
            notice_kind=notice_kind,
            project_id=project_id,
            run_id=run_id,
            decision_id=decision_id,
            proposal_id=proposal_id,
            title=self.one_line(title) or kind,
            body=self.body(body),
            details=details,
            link=link,
            report=report,
        )
        await outbox.add_deliveries(conn, notification_id, user_id, web_only=report)
        # A report's kinds are of the brief tier by default (``tiers``): a device gets one only when its member chose
        # the interrupt tier for its kind themselves.
        tier = {"notice_kind": notice_kind, "run_id": run_id, "decision_id": decision_id, "details": details}
        if await tiers.interrupts(conn, user_id=user_id, kind=kind, **tier):
            await push_repository.add_device_deliveries(conn, notification_id, user_id)
        await plugin_events.emit_notification(conn, notification_id, project_id)  # the channels of isolated plugins
        await bus.soon(jobs.DELIVER_NOTIFICATIONS, conn)  # goes out once the transaction commits
        return notification_id


NOTIFIER = Notifier()
