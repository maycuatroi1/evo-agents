"""The rule ``growth`` of the plugin crm on the Loop (criterion a16 of plan autonomous-oss-growth,
``docs/crm-plugin.md``): the event ``plugin.crm.touch`` that the job ``intake`` records for a new touchpoint from GitHub
gives the agent type @community its work.

- An invite request makes a ``growth_invite`` task: answer in the issue, and when the Policy allows (``tools``), make a
  join link bound to the requester's login and post it there.
- An outsider's new issue, pull request or discussion makes a ``growth_draft`` task: the first reply, through the
  posting gate of the plugin marketing (``marketing__propose``, kind reply, on the project's github account).

The handler reads the contact with its own connection and writes nothing; a contact deleted since makes no work.
"""

from __future__ import annotations

from sqlalchemy import select

from evo_agents.core_plugins.crm import intake
from evo_agents.hub import tables

INVITE, DRAFT = "growth_invite", "growth_draft"
FIRST_REPLY = ("issue", "pull_request", "discussion")
LANGUAGE = "Write in English on GitHub."


def invite_task(login: str, ref: str, url: str | None) -> str:
    """What @community is asked for an invite request."""
    return (
        f"{login} asked for an invite to the project's hub ({ref}, {url or 'no link'}).\n\n"
        f"Call crm__invite with login {login} and ref {ref}. If it gives a link, answer in the request with "
        f"marketing__propose (kind reply, reply_to {ref}, the project's github account): thank them in one line and "
        "give the link, which only their GitHub login can use and which lasts 7 days. If it refuses, answer with the "
        "reason it gives, plainly, and say a maintainer will look. Never promise a link the tool did not make.\n"
        f"{LANGUAGE}"
    )


def first_reply_task(login: str, kind: str, ref: str, url: str | None) -> str:
    """What @community is asked for an outsider's new issue, pull request or discussion."""
    what = kind.replace("_", " ")
    return (
        f"{login}, who is not a member of the repo, opened a {what}: {ref}, {url or 'no link'}.\n\n"
        f"Read it and give the first reply with marketing__propose (kind reply, reply_to {ref}, the project's github "
        "account): thank them, say what happens next, and answer what the docs, releases or changelog answer, with "
        "the link as a source. Do not promise a fix, a date or a merge; do not close, label or assign anything.\n"
        f"{LANGUAGE}"
    )


async def _login(context, contact_id) -> str | None:
    if not isinstance(contact_id, int):
        return None
    c = tables.crm_contacts.c
    async with context.engine.connect() as conn:
        return await conn.scalar(select(c.login).where(c.id == contact_id))


async def growth(params: dict, context) -> dict:
    """The handler of rule ``growth``: one work item of @community for a new touch."""
    event = params["event"] or {}
    payload = event.get("payload") or {}
    if event.get("kind") != intake.TOUCH:
        return {"effects": []}
    kind, ref, url = payload.get("kind"), payload.get("ref"), payload.get("url")
    login = await _login(context, payload.get("contact_id"))
    if login is None or not isinstance(ref, str):
        return {"effects": []}
    if kind == "invite_request":
        detail = {"task": invite_task(login, ref, url), "title": f"Invite request {ref}", "ref": ref}
        return {"effects": [{"effect": "work", "kind": INVITE, "key": f"invite:{ref}", "detail": detail}]}
    if kind in FIRST_REPLY:
        detail = {"task": first_reply_task(login, kind, ref, url), "title": f"First reply to {ref}", "ref": ref}
        return {"effects": [{"effect": "work", "kind": DRAFT, "key": f"first-reply:{ref}", "detail": detail}]}
    return {"effects": []}


__all__ = ["first_reply_task", "growth", "invite_task"]
