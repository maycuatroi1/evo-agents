"""Where the message of each decision stands on the channels of core plugins: ``Decision.channel_deliveries``, which
every view of a decision shows (``decision_reads.with_outputs``). ``docs/notifications.md`` (Decisions on channels) is the
design.

A decision the hub told the person asked of has one entry for each kind of channel: each channel that got a delivery
of its notification, and each kind a core plugin contributes to the channels that did not. A delivery reads as sent,
with the id the service gave the message, and, when the message went without the decision's content, why
(``withheld``: its label keeps it off the channel); pending, while it is tried again, with its last error; failed, with
its last error, once its tries or DECISION_WINDOW ran out (``notifications``). A kind without a delivery is not sent,
with the reason: the person asked has not linked it, turned it off, or linked it after the decision was asked. The edit
of a sent message once the decision closed (``notifications.queue_edits``) shows under ``edit``. A decision nobody was
told of (a wait, a credential request the hub answered itself) has none.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.plugin_calls import core_registry
from evo_agents.hub.server.decision_models import ChannelDelivery, ChannelEdit, Decision

NOT_LINKED = "{asked} has not linked {kind}"
TURNED_OFF = "{asked} turned {kind} off"
LINKED_LATER = "{asked} linked {kind} after the decision was asked"
WITHHELD_REASONS = {
    "label": "its label {label} keeps its content off {kind}: the message links to the hub",
    "default_label": "it has no label of its own, and its project keeps its content off {kind}: the message links "
    "to the hub",
    "project": "its project keeps its content off {kind}: the message links to the hub",
}
STATES = {"delivered": "sent", "pending": "pending", "failed": "failed"}


def contributed_kinds() -> list[str]:
    """The kinds of channel the core plugins of this hub contribute, in their order."""
    return list(dict.fromkeys(item["name"] for _, item in core_registry().contributions("channels")))


def _edit(row) -> ChannelEdit | None:
    if row.edit_state is None:
        return None
    state = STATES.get(row.edit_state, "pending")
    return ChannelEdit(state=state, error=None if state == "sent" else row.edit_error, sent_at=row.edit_at)


def _entry(row, label: str | None) -> ChannelDelivery:
    state = STATES.get(row.state, "pending")
    reason = None
    if state == "sent" and row.withheld in WITHHELD_REASONS:
        reason = WITHHELD_REASONS[row.withheld].format(label=label or "-", kind=row.kind)
    return ChannelDelivery(
        channel=row.kind,
        state=state,
        external_id=row.external_id,
        reason=reason,
        withheld=row.withheld if state == "sent" else None,
        error=None if state == "sent" else row.last_error,
        attempts=row.attempts,
        first_tried_at=row.created_at,
        sent_at=row.delivered_at,
        edit=_edit(row),
    )


def _not_sent(kind: str, asked: str, channel) -> ChannelDelivery:
    if channel is None:
        reason = NOT_LINKED
    elif not channel.enabled:
        reason = TURNED_OFF
    else:
        reason = LINKED_LATER
    return ChannelDelivery(channel=kind, state="not_sent", reason=reason.format(asked=asked, kind=kind))


async def fill(conn: AsyncConnection, decisions: list[Decision]) -> None:
    """Give each of ``decisions`` its ``channel_deliveries``."""
    from evo_agents.hub.server.decision_reads import asked_person  # it fills its views through this module

    if not decisions:
        return
    d, r, n = tables.decisions, tables.runs, tables.notifications
    dl, c = tables.notification_deliveries, tables.notification_channels
    edit = dl.alias("edit")
    ids = [decision.id for decision in decisions]
    told_query = (
        select(n.c.decision_id, n.c.id.label("notification_id"), n.c.user_id)
        .select_from(n.join(d, d.c.id == n.c.decision_id).join(r, r.c.id == d.c.run_id))
        .where(n.c.decision_id.in_(ids), n.c.user_id == asked_person())
    )
    told = {row.decision_id: row for row in (await conn.execute(told_query)).all()}
    if not told:
        return
    sent_query = (
        select(
            dl.c.notification_id,
            c.c.kind,
            dl.c.state,
            dl.c.external_id,
            dl.c.last_error,
            dl.c.attempts,
            dl.c.created_at,
            dl.c.delivered_at,
            dl.c.withheld,
            edit.c.state.label("edit_state"),
            edit.c.last_error.label("edit_error"),
            edit.c.delivered_at.label("edit_at"),
        )
        .select_from(dl.join(c, c.c.id == dl.c.channel_id).outerjoin(edit, edit.c.edit_of == dl.c.id))
        .where(dl.c.notification_id.in_([row.notification_id for row in told.values()]), dl.c.edit_of.is_(None))
        .order_by(dl.c.id)
    )
    by_notification: dict[int, list] = {}
    for row in (await conn.execute(sent_query)).all():
        by_notification.setdefault(row.notification_id, []).append(row)
    kinds = contributed_kinds()
    channels: dict[tuple[int, str], object] = {}
    if kinds:
        users = {row.user_id for row in told.values()}
        linked = select(c.c.user_id, c.c.kind, c.c.enabled).where(c.c.user_id.in_(users), c.c.kind.in_(kinds))
        channels = {(row.user_id, row.kind): row for row in (await conn.execute(linked)).all()}
    for decision in decisions:
        found = told.get(decision.id)
        if found is None:
            continue
        rows = by_notification.get(found.notification_id, [])
        entries = [_entry(row, decision.label) for row in rows]
        sent_kinds = {row.kind for row in rows}
        entries += [
            _not_sent(kind, decision.asked_of, channels.get((found.user_id, kind)))
            for kind in kinds
            if kind not in sent_kinds
        ]
        decision.channel_deliveries = entries
