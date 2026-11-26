"""The members' channels of core plugins: linking and unlinking a channel; the one-time codes a channel is linked with
(channel_link_codes, schema 0063); what a member may answer from a channel; and what a channel's class reads of a
notification. ``notifications.repository.channels`` holds their SQL (the member a channel of a kind speaks for, found
by its config, and the state a channel keeps among it). The API of core plugins
(``evo_agents.hub.server.core_plugin_api``) builds on both, and ``docs/plugins.md`` (The core plugin API) is the
design.

A channel is a row of notification_channels: one member, one kind, the config its plugin stores (a chat's ids, say),
and the web session that linked it (``token_id``). It speaks for its member only while that session lives: the same
member's web session, neither revoked nor expired (``live_session``). Linking takes a live session and replaces the
member's channel of that kind; ``unique_on`` names the config keys that identify a chat, so a chat speaks for one
member and another member's link to it goes. Unlinking deletes the channel, whose deliveries go with it.

A hub admin may link a member's channel instead (``admin_link``), for a member who does not reach the hub's web: the
row keeps no session and names the admin (``linked_by``, schema 0268), and it speaks for its member until it is
unlinked or linked again (``channel_live``). The member's own link from the web takes its place.

These rules came from the Telegram channel (the core plugin telegram, ``evo_core_plugins.telegram`` of
evo-agents-core-plugins), which reaches them through the API like any other core plugin.
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Mapping
from datetime import datetime

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import telegram
from evo_agents.hub.redact import redact
from evo_agents.hub.server import audit, output_notices
from evo_agents.hub.server.notifications.models import (
    ANSWERABLE,
    BY_DEFAULT_LABEL,
    BY_LABEL,
    BY_PROJECT,
    LABELLED_ANSWER,
    MAX_CONFIG_BYTES,
    NO_USER,
    NOT_YOURS,
    RESTRICTED_ANSWER,
    SESSION_ENDED,
    Found,
    Outgoing,
)
from evo_agents.hub.server.notifications.repository import channels
from evo_agents.hub.server.notifications.service.channels import SENDING


def json_object(value, what: str, limit: int = MAX_CONFIG_BYTES) -> dict:
    """``value`` as a JSON object of at most ``limit`` bytes; ValueError naming ``what`` otherwise."""
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise ValueError(f"{what} is a JSON object with string keys")
    try:
        text = json.dumps(dict(value), separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{what} is not JSON: {exc}") from None
    if len(text.encode()) > limit:
        raise ValueError(f"{what} is at most {limit // 1024} KiB of JSON")
    return json.loads(text)


async def user_named(conn: AsyncConnection, login: str):
    """The user (id, login) of ``login`` in any case; 404 when the hub has none."""
    found = await channels.user_named(conn, login)
    if found is None:
        raise HTTPException(404, NO_USER.format(login=login))
    return found


async def link(
    conn: AsyncConnection,
    *,
    user_id: int,
    login: str,
    token_id: int,
    kind: str,
    config: dict,
    unique_on: tuple[str, ...] = (),
) -> Found:
    """Link member ``user_id``'s channel of ``kind`` with ``config``, bound to web session ``token_id``, in place of
    the one they had; another member's channel of the kind whose config has the same values at ``unique_on`` goes.
    403 SESSION_ENDED when the session does not live now. Audited as ``<kind>.link``."""
    if not await channels.session_live(conn, user_id, token_id):
        raise HTTPException(403, SESSION_ENDED)
    bound = {"config": config, "enabled": True, "token_id": token_id, "linked_by": None}
    channel_id = await channels.bind(conn, user_id, kind, bound, unique_on)
    await audit.record(conn, actor_id=user_id, token_id=token_id, action=f"{kind}.link", target=f"{kind} user:{login}")
    return Found(user_id, login, channel_id, dict(config))


async def admin_link(
    conn: AsyncConnection,
    *,
    login: str,
    kind: str,
    config: dict,
    unique_on: tuple[str, ...] = (),
    actor_id: int,
    actor_token_id: int | None,
) -> Found:
    """A hub admin, ``actor_id``, links the channel of ``kind`` of the member named ``login`` (in any case) with
    ``config``, bound to no session: it speaks for the member until it is unlinked or linked again, the member's own
    link from the web among them. In place of the member's channel of the kind; another member's channel of the kind
    with the same values at ``unique_on`` goes. 404 when the hub has no such user. Audited as ``<kind>.link`` by the
    admin, ``by=admin``."""
    found = await user_named(conn, login)
    bound = {"config": config, "enabled": True, "token_id": None, "linked_by": actor_id}
    channel_id = await channels.bind(conn, found.id, kind, bound, unique_on)
    await audit.record(
        conn,
        actor_id=actor_id,
        token_id=actor_token_id,
        action=f"{kind}.link",
        target=f"{kind} user:{found.login} by=admin",
    )
    return Found(found.id, found.login, channel_id, dict(config))


async def unlink(
    conn: AsyncConnection,
    *,
    user_id: int,
    kind: str,
    by: str,
    actor_id: int | None = None,
    actor_token_id: int | None = None,
) -> bool:
    """Delete member ``user_id``'s channel of ``kind`` and the deliveries waiting for it; whether there was one.
    Audited as ``<kind>.unlink`` done ``by`` the member (web or bot), or by ``actor_id``, a hub admin (admin)."""
    login = await channels.delete_channel(conn, user_id, kind)
    if login is not None:
        await audit.record(
            conn,
            actor_id=user_id if actor_id is None else actor_id,
            token_id=actor_token_id,
            action=f"{kind}.unlink",
            target=f"{kind} user:{login} by={by}",
        )
    return login is not None


# The one-time codes


async def new_link_code(conn: AsyncConnection, *, user_id: int, token_id: int, kind: str) -> tuple[str, datetime]:
    """A one-time code that links member ``user_id``'s channel of ``kind``, made by web session ``token_id``, and when
    it expires: it lives ``telegram.LINK_SECONDS``, the hub keeps its SHA-256 alone, and it replaces the member's codes
    of the kind not used yet. 403 SESSION_ENDED when the session does not live now."""
    if not await channels.session_live(conn, user_id, token_id):
        raise HTTPException(403, SESSION_ENDED)
    code = telegram.new_code()
    expires_at = await channels.new_link_code(
        conn,
        user_id=user_id,
        token_id=token_id,
        kind=kind,
        code_hash=telegram.code_hash(code),
        seconds=telegram.LINK_SECONDS,
    )
    return code, expires_at


async def redeem_link_code(conn: AsyncConnection, kind: str, code: str):
    """The member (user_id, login, token_id) code ``code`` of ``kind`` was made for, now used; None when it is not one
    of the kind, was used, expired, or the web session that made it does not live any more."""
    if not isinstance(code, str) or not telegram.LINK_CODE.fullmatch(code):
        return None
    return await channels.redeem_link_code(conn, kind, telegram.code_hash(code))


# What a channel may answer and read


def kept_from(row, level: str, location: str | None, below: bool = False) -> bool:
    """Whether the project of ``row`` (of ``channels.project_rules``) is kept from a channel cleared for level and
    location, or for what sits below them (``telegram.restricted_for``); no project is."""
    if row is None:
        return True
    return telegram.restricted_for(row.clearance, row.levels, row.locations, level, location, below=below)


def effective_label(label: str | None, default_label, levels) -> str | None:
    """A decision's label, else its project's default label's level, else the top of its ladder (what
    ``access.ProjectRules`` reads a project without a default label as)."""
    if label is not None:
        return label
    if isinstance(default_label, Mapping) and isinstance(default_label.get("level"), str):
        return default_label["level"]
    levels = list(levels or [])
    return levels[-1] if levels else None


def labelled_kept(row, level: str, below: bool = False) -> bool:
    """Whether a decision with a label (``row`` has its label, its project's levels and ``channel_clearance``, the
    clearance of the sink of the channel it would go to) is kept from that channel, whose plugin clears it for
    ``level`` (``telegram.label_kept_from``)."""
    return telegram.label_kept_from(row.label, row.levels, row.channel_clearance, level, below=below)


async def answerable_project(
    conn: AsyncConnection,
    user_id: int,
    kind: str,
    item_id: int,
    level: str,
    location: str | None,
    below: bool = False,
    *,
    channel: str | None = None,
) -> str:
    """The name of the project of decision or proposal ``item_id``, once member ``user_id`` may answer it from a
    channel of kind ``channel`` cleared for ``level`` and ``location``: 404 NOT_YOURS without a grant on it, the same
    as for nothing by that id; 403 LABELLED_ANSWER for a decision whose label keeps it off the channel
    (``labelled_kept``, against the sink of kind channel named ``channel``), and 403 RESTRICTED_ANSWER for a decision
    without a label, or a proposal, of a project kept from the channel. Checked before anything else of the decision
    or proposal is read."""
    row = await channels.answer_rules(conn, user_id, kind, item_id, channel)
    if row is None:
        raise HTTPException(404, NOT_YOURS)
    if row.label is not None:
        if labelled_kept(row, level, below):
            raise HTTPException(403, LABELLED_ANSWER.format(label=row.label, decision_id=item_id))
    elif kept_from(row, level, location, below):
        raise HTTPException(403, RESTRICTED_ANSWER)
    return row.name


def _redacted(value):
    """``value``, a text of a decision a channel sends, through ``evo_agents.hub.redact``; None stays None."""
    return redact(value) if isinstance(value, str) else value


def _channel_decision(row) -> dict:
    """What a channel reads of a decision (``extra["decision"]``), every text of it redacted."""
    found = dict(row._mapping)
    options = []
    for option in found.pop("options") or []:
        shown = {**option, "label": _redacted(option.get("label"))}
        if option.get("description") is not None:
            shown["description"] = _redacted(option["description"])
        options.append(shown)
    chosen = next((item for item in options if item.get("key") == found.get("answer_option")), None)
    texts = ("question", "context", "run_title", "recommended_reason", "undo")
    return {
        **found,
        **{key: _redacted(found[key]) for key in texts},
        "options": options,
        "evidence": [_redacted(ref) for ref in found["evidence"] or []],
        "answer_label": None if chosen is None else chosen["label"],
    }


async def _restriction(
    conn: AsyncConnection, notification: Outgoing, told: dict, level: str, location: str | None, below: bool
) -> tuple[bool, str]:
    """Whether ``notification`` is kept from a channel cleared for ``level`` and ``location``, and by what (BY_LABEL,
    BY_DEFAULT_LABEL, BY_PROJECT); a decision's label, and its state for an edit, put in ``told``."""
    sending = SENDING.get()
    restricted = notification.kind in ANSWERABLE
    by = BY_PROJECT
    rules = None
    if notification.kind == "decision" and notification.decision_id is not None:
        channel = None if sending is None else sending.channel
        rules = await channels.decision_rules(conn, notification.decision_id, channel)
        if "edit" in told and rules is not None:
            told["edit"]["state"] = rules.state
    if rules is not None:
        told["label"] = effective_label(rules.label, rules.default_label, rules.levels)
        told["label_given"] = rules.label is not None
        if rules.label is not None:
            restricted, by = labelled_kept(rules, level, below), BY_LABEL
        else:
            restricted, by = kept_from(rules, level, location, below), BY_DEFAULT_LABEL
    elif notification.project is not None:
        restricted = kept_from(await channels.rules_of(conn, notification.project), level, location, below)
    return restricted, by


async def prepare(
    conn: AsyncConnection, notification: Outgoing, level: str, location: str | None, below: bool = False
) -> Outgoing:
    """``notification`` with what a channel cleared for ``level`` and ``location`` may send of it in ``extra``.

    A decision with a label is judged by that label against the clearance of its project's sink of kind channel named
    as the channel the job's pass delivers to (``notifications.service.channels.SENDING``), and against ``level``
    (``labelled_kept``): no such sink, or no delivery being prepared, keeps it off. A decision without a label, a
    proposal and a notice keep the rule of the project's hub sink (``kept_from``). One kept off gets ``{"restricted":
    true, "restricted_by": ...}`` alone: ``label`` (BY_LABEL), ``default_label`` for a decision without a label,
    ``project`` for the rest and for a decision or proposal of no project.

    Else ``restricted`` is false, with its decision (question, category, plan_id, step_key, options, state, run_kind,
    run_title, the recommended_reason, reversible, undo and evidence of ``decision_models``, an empty list for no
    evidence, and context, answer_option, answer_label, answered_by, answered_via, label and label_given), each text
    through ``evo_agents.hub.redact``, and its proposal (title, tier, kind, lens, evidence_count, state), as Telegram's
    channel reads them, and for a decision or a notice of a run's files that carries files a channel may send, those
    files (``outputs``: at most ``output_notices.CHANNEL_OUTPUTS``, none of source tool, each with a presigned GET of
    its bytes) and how many there are (``outputs_count``).

    A decision's ``extra`` has ``label``, its effective label (its own, else its project's default label's level),
    and ``label_given`` whether it has its own, restricted or not, and its title and body are redacted. A delivery
    that edits the message of an earlier one (``edit_of``, ``notifications.service.deliveries.queue_edits``) adds
    ``edit``: the ``delivery_id`` and ``external_id`` of that message and the decision's ``state``, so the channel
    edits it in place of sending a new one."""
    sending = SENDING.get()
    told: dict = {}
    if sending is not None and sending.edit_of is not None:
        told["edit"] = {"delivery_id": sending.edit_of, "external_id": sending.edit_external_id, "state": None}
    restricted, by = await _restriction(conn, notification, told, level, location, below)
    if notification.kind == "decision":  # the title and the body repeat the question
        notification = dataclasses.replace(
            notification, title=redact(notification.title), body=_redacted(notification.body)
        )
    if restricted:
        return dataclasses.replace(notification, extra={"restricted": True, "restricted_by": by, **told})
    extra: dict = {"restricted": False, **told}
    if notification.decision_id is not None:
        row = await channels.channel_decision(conn, notification.decision_id)
        labelled = {"label": told.get("label"), "label_given": told.get("label_given", False)}
        extra["decision"] = None if row is None else {**_channel_decision(row), **labelled}
    if notification.proposal_id is not None:
        extra["proposal"] = await channels.channel_proposal(conn, notification.proposal_id)
    carried = await output_notices.channel_outputs(conn, notification)
    if carried is not None and carried[1]:
        extra["outputs"], extra["outputs_count"] = carried
    return dataclasses.replace(notification, extra=extra)
