"""The settle chat of a version (criterion a7 of plan srs-conflicts-and-tasks): when @ba's review of a version ends with
findings of level decide of its document still open, the hub opens a chat where members settle them with @ba.

The plugin's events contribution ``reviews`` takes the event task.result of the task runs of its agent type, which the
hub delivers to a core plugin without the grant runs:dispatch, at least once (``evo_agents.hub.plugin_events``), after
the run's end is committed: nothing here can undo that end. ``handlers.review_ended`` opens the chat when the run is the review
of a version's current round (srs_versions.review_run_id), whatever it ended as, while the version is in review, its
document is not archived, the hub's feature chats is on, the agent type ba is on with its profile, and the document has
a finding of level decide open. It asks no model. A version has one chat: srs_settle_chats holds it, and a lock of the
version makes a second delivery, or a second call, find the first's. ``repository.settle`` reads and writes the rows.

The chat is of kind settle (``evo_agents.hub.chats.SETTLE``) with @ba as its agent: every message of a person wakes it
without an @, from a member who only reads the project too, on a worker of the type's run_as as every run of @ba
(``evo_agents.hub.server.chat_runs``), within the chat's budget and hops. Its creator is the version's author, or the
type's run_as when the author holds no grant that clears the chat's label any more. The hub invites by rule
(``_invited``):

- the author of the version;
- the members the plugin's config names in the roles ba, qa and dev;
- the people behind the sources of the open findings: for a code, or the node of a coded item, the author of the
  latest version of the document that holds the item; for the node of a plan or of one of its steps
  (``plan:<id>``), the member who wrote the plan on the hub (its first revision); for a file or a document of the
  graph, the authors of its newest commits, whose emails the finding keeps (``findings.looked_up``) and the hub turns
  into logins (``user_emails.logins_of_emails``);
- the assignees of the tasks linked to the items the findings are about, and to the use case a business rule of them
  sits under.

Someone with no account on the hub, no grant on the project, or a grant that does not clear the chat's label, and
anyone past the chat's limit of people (``chats.MAX_PEOPLE``), is named in the first message with why, and not added.
The first message lists the findings, who was invited and who was not, and ends with the mention of @ba, whose chat
run acts for the type's run_as, as any run a message of the hub wakes; a refusal is said in the thread. Each open finding of level decide then carries the link of
that message as its chat link (``findings.approval_hold`` names it) until the message that settles it replaces it.

In the chat @ba sums the conflicts up, the sources, the choices and whom it asks what (its skill srs-settle). Anyone
in the chat settles: @ba writes the line "Đã chốt: ..." with who settled and when, records the resolution, the member
and the link of the message on the finding (srs__finding), and proposes the next version as the author of the version
the chat is for (``repository.settle.proposer``). An objection within 24 hours of the settling reopens the finding; later, srs__finding
refuses to reopen it.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, replace

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import chats, features, runs
from evo_agents.hub.server import audit, hub_settings
from evo_agents.hub.server.chat_access import ChatAccess, chat_access, projects_by_id, uncleared
from evo_agents.hub.server.chat_members import label_now, put_agent, put_person
from evo_agents.hub.server.chat_notices import notice_added
from evo_agents.hub.server.chat_runs import Hand, hand_mentions, hub_reply
from evo_agents.hub.server.chat_store import add_member, insert_mentions, insert_message, member_event
from evo_agents.hub.server.chat_views import person_author
from evo_agents.hub.server.chats import resolve_mentions
from evo_agents.hub.server.profile_store import Profile, by_id
from evo_agents.hub.server.security import MACHINE, Principal
from evo_agents.hub.server.srs.models.doc import SrsError
from evo_agents.hub.server.srs.repository import revisions, store
from evo_agents.hub.server.srs.repository import settle as rows
from evo_agents.hub.server.srs.repository.settle import SettleVersion
from evo_agents.hub.server.srs.service import findings, notices
from evo_agents.hub.server.user_emails import logins_of_emails
from evo_agents.hub.tables_srs import KG_NODE_KINDS

log = logging.getLogger(__name__)

HUB = "the hub"  # who adds the people of a settle chat, as its notices say
SETTLED = "Đã chốt"  # the line @ba writes once a finding is settled
_PLAN_NODE = re.compile(r"^plan:([^/]+)(?:/step:.+)?$")
_ITEM_NODE = re.compile(rf"^({'|'.join(sorted({kind.lower() for kind in KG_NODE_KINDS.values()}))}):(.+)$")


@dataclass(frozen=True)
class Opened:
    """The settle chat of a version: its chat, its first message and where the web shows it, the run it started."""

    chat_id: int
    root_id: int
    link: str
    agent_run_id: int | None


# Opening the chat


async def tell_reviewers(conn: AsyncConnection, project_id: int, review_run_id: int) -> None:
    """The reviewers of the version whose current round's review was run ``review_run_id`` learn that it waits for them,
    unless an open finding of level decide still holds it (``notices``); a second delivery tells nobody again. For the
    review of a use case sent on its own, the reviewers of that use case, in the latest version."""
    version = await rows.reviewed_version(conn, project_id, review_run_id)
    if version is None:
        reviewed = await revisions.reviewed_by(conn, review_run_id)
        doc = await store.document_by_id(conn, project_id, reviewed.document_id) if reviewed else None
        if reviewed is not None and doc is not None and doc.latest_version is not None:
            latest = await store.version(conn, doc, doc.latest_version)
            await notices.tell_case_reviewers(conn, project_id, doc, latest, reviewed.key)
        return
    if version.state != "in_review" or version.archived:
        return
    doc = await store.document_by_id(conn, project_id, version.document_id)
    if doc is not None:
        await notices.tell_reviewers(conn, project_id, doc, await store.version(conn, doc, version.number))


async def opened(conn: AsyncConnection, version_id: int) -> Opened | None:
    """The settle chat of version ``version_id``, or None."""
    row = await rows.settle_chat(conn, version_id)
    if row is None:
        return None
    return Opened(row.chat_id, row.root_message_id, chats.chat_link(row.chat_id, row.root_message_id), row.agent_run_id)


async def _agent(conn: AsyncConnection, project_id: int):
    """(@ba's profile, the agent type's run_as) while the type is on in the project; (None, None) otherwise. Read
    here rather than through ``hub.server.agent_types``, which the job worker that delivers events cannot import
    first."""
    row = await rows.ba_type(conn, project_id)
    return (None, None) if row is None else (await by_id(conn, row.profile_id), row.run_as)


async def _chats_on(conn: AsyncConnection) -> bool:
    instance, _ = await hub_settings.feature_values(conn)
    return features.enabled(features.BY_ID["chats"], instance)


async def open_chat(conn: AsyncConnection, project_id: int, review_run_id: int) -> Opened | None:
    """Open the settle chat of the version whose current round's review is task run ``review_run_id`` of project
    ``project_id``, in the caller's transaction: the chat it has already, the one made now, or None when none is due
    (see the module)."""
    version = await rows.reviewed_version(conn, project_id, review_run_id)
    if version is None:
        return None
    await rows.lock_version(conn, version.id)
    found = await opened(conn, version.id)
    if found is not None or version.state != "in_review" or version.archived or not await _chats_on(conn):
        return found
    deciding = await rows.deciding(conn, version.document_id, findings.DECIDE)
    profile, run_as = await _agent(conn, project_id)
    if not deciding or profile is None:
        return None
    creator = await _creator(conn, profile, [version.author_id, run_as])
    if creator is None:
        log.warning("srs settle chat not opened: no member to open it", extra={"version_id": version.id})
        return None
    chat = await _make(conn, version, profile, creator)
    invited, left = await _invite(conn, chat, creator, await _invited(conn, version, deciding))
    body = _first_message(version, deciding, invited, left, profile)
    message_id, _ = await insert_message(conn, chat, body, thread_root_id=None)
    agent_run = await _ask(conn, chat, profile, await _principal(conn, run_as), message_id, body)
    link = chats.chat_link(chat.chat_id, message_id)
    values = {"version_id": version.id, "chat_id": chat.chat_id, "root_message_id": message_id}
    values |= {"review_run_id": version.review_run_id, "agent_run_id": agent_run}
    await rows.opened_on(conn, [row.id for row in deciding], link, values)
    log.info("srs settle chat opened", extra={"version_id": version.id, "chat_id": chat.chat_id, "run_id": agent_run})
    return Opened(chat.chat_id, message_id, link, agent_run)


async def _principal(conn: AsyncConnection, user_id: int) -> Principal:
    login = await rows.login(conn, user_id)
    if login is None:
        raise LookupError(f"no user {user_id}")
    return Principal(user_id, login, False, None, MACHINE, "")


async def _creator(conn: AsyncConnection, profile: Profile, candidates: list[int]) -> Principal | None:
    """The first of ``candidates`` who clears the label the chat has with ``profile`` in it; None for none."""
    projects = await projects_by_id(conn, [profile.project_id])
    for user_id in dict.fromkeys(candidates):
        person = await _principal(conn, user_id)
        if person.login.lower() != profile.handle and not await uncleared(conn, {user_id: person.login}, projects):
            return person
    return None


async def _make(conn: AsyncConnection, version: SettleVersion, profile: Profile, creator: Principal) -> ChatAccess:
    """The settle chat of ``version``, made now with its creator and @ba in it; the creator's access to it."""
    title = f"Conflicts of {version.slug} v{version.number}"[: chats.MAX_TITLE_CHARS]
    values = {"title": title, "created_by": creator.user_id, "kind": chats.SETTLE, "direct_profile_id": profile.id}
    chat_id = await rows.insert_chat(conn, values)
    await add_member(conn, chat_id, user_id=creator.user_id, added_by=creator.user_id)
    await member_event(conn, chat_id, "member_added", person_author(creator.login), creator.login)
    chat = await chat_access(conn, creator, chat_id, lock=True)
    await put_agent(conn, creator, chat, profile)
    chat = await label_now(conn, chat)
    await notice_added(conn, replace(chat, login=HUB), creator.user_id)
    target = f"chat:{chat_id} settle {version.slug} v{version.number} agent:@{profile.handle}"
    await audit.record(
        conn,
        actor_id=creator.user_id,
        token_id=None,
        action=audit.CHAT_CREATE,
        target=target,
        project_id=version.project_id,
    )
    return chat


# Whom it invites


@dataclass
class _Invitees:
    """Who the rules name, each once, with why: by login, and by an email no login was found for."""

    logins: dict[str, str]
    emails: dict[str, str]

    def add(self, login: str | None, why: str) -> None:
        if login and login.lower() not in {name.lower() for name in self.logins}:
            self.logins[login] = why


async def _invited(conn: AsyncConnection, version: SettleVersion, deciding: list) -> _Invitees:
    """The people the rules of the module name for the chat of ``version``, whose open findings are ``deciding``."""
    found = _Invitees({}, {})
    found.add(await _login(conn, version.author_id), f"the author of version {version.number}")
    try:
        roles = (await store.project_settings(conn, version.project_id, version.project)).roles
    except SrsError:  # a config whose code patterns do not compile names no one the chat can use
        roles = {}
    for role in ("ba", "qa", "dev"):
        for login in roles.get(role) or []:
            found.add(login, f"role {role}")
    items = {row.item_id for row in deciding if row.item_id}
    for row in deciding:
        for source in row.sources:
            items |= await _source_people(conn, version, source, found)
        emails = [email for email in row.commit_authors or [] if isinstance(email, str)]
        for email, login in (await logins_of_emails(conn, emails)).items():
            why = f"an author of the newest commits of a file finding #{row.id} names"
            if login is None:
                found.emails.setdefault(email, why)
            else:
                found.add(login, why)
    items |= await rows.use_cases_above(conn, deciding)
    for login, code in await rows.assignees(conn, items):
        found.add(login, f"assigned a task of {code}")
    return found


async def _login(conn: AsyncConnection, user_id: int | None) -> str | None:
    if user_id is None:
        return None
    return await rows.login(conn, user_id)


async def _source_people(conn: AsyncConnection, version: SettleVersion, source: dict, found: _Invitees) -> set[int]:
    """Add the person behind ``source`` to ``found``; the item it names, as a set, when it names one."""
    kind, ref = source.get("kind"), str(source.get("ref") or "")
    plan = _PLAN_NODE.match(ref) if kind == "node" else None
    if plan is not None:
        found.add(
            await rows.plan_author(conn, version.project_id, plan.group(1)), f"the author of plan {plan.group(1)}"
        )
        return set()
    item = _ITEM_NODE.match(ref) if kind == "node" else None
    code = ref if kind == "code" else item.group(2) if item is not None else None
    if code is None:
        return set()
    held = await rows.holder(conn, version.project_id, code)
    if held is None:
        return set()
    found.add(held.login, f"the author of version {held.number} of {held.slug}, which holds {code}")
    return {held.item_id}


async def _invite(
    conn: AsyncConnection, chat: ChatAccess, creator: Principal, named: _Invitees
) -> tuple[list[str], list[str]]:
    """(the lines of who was added, the lines of who was not and why): each person ``named`` names who has an account,
    a grant and clears the chat's label, while the chat has room."""
    invited, left = [f"{creator.login} ({named.logins.get(creator.login) or 'who the chat is opened for'})"], []
    room = chats.MAX_PEOPLE - 1
    by_hub = replace(chat, login=HUB)
    for login, why in named.logins.items():
        row = await rows.user_by_login(conn, login)
        if row is not None and row.id == creator.user_id:
            continue
        if row is None:
            left.append(f"{login} ({why}): no account on this hub")
            continue
        problems = await uncleared(conn, {row.id: row.login}, chat.projects)
        if problems:
            left.append(f"{row.login} ({why}): {problems[0].removeprefix(f'{row.login} (').removesuffix(')')}")
        elif room <= 0:
            left.append(f"{row.login} ({why}): the chat holds at most {chats.MAX_PEOPLE} people")
        else:
            try:
                await put_person(conn, creator, by_hub, row.id, row.login)
            except HTTPException as exc:  # refused before anything is written: a login that is the agent's handle
                left.append(f"{row.login} ({why}): {exc.detail}")
                continue
            invited.append(f"{row.login} ({why})")
            room -= 1
    left += [f"{email} ({why}): no member of this hub lists this address" for email, why in named.emails.items()]
    return invited, left


# The first message


def _source_text(source: dict) -> str:
    note = f" ({' '.join(str(source['note']).split())})" if source.get("note") else ""
    return f"{source.get('kind')} {source.get('ref')}{note}"


def _first_message(
    version: SettleVersion, deciding: list, invited: list[str], left: list[str], profile: Profile
) -> str:
    lines = [
        f"@{profile.handle}'s review of version {version.number} of {version.slug} ({version.title}) ended with "
        f"{len(deciding)} finding(s) of level decide open. No version of {version.slug} is approved until each is "
        "settled here or waived:",
    ]
    for row in deciding:
        about = row.item_code or row.item or "the whole document"
        sources = "; ".join(_source_text(source) for source in row.sources)
        found_on = f"found on version {row.version}"
        lines.append(f"- #{row.id} {row.kind}, {about}, {found_on}: {row.summary} Sources: {sources}")
    lines += ["", "Invited: " + "; ".join(invited) + "."]
    if left:
        lines.append("Not invited: " + "; ".join(left) + ".")
    lines += [
        "",
        f"Anyone here settles a finding: say what you choose. @{profile.handle} sums each conflict up, writes a line "
        f'"{SETTLED}: ..." with who settled it and when, records it on the finding, and proposes the next version of '
        f"{version.slug} as the author of version {version.number}. An objection within "
        f"{findings.REOPEN_HOURS} hours of that line opens the finding again.",
    ]
    return runs.clip("\n".join(lines), chats.MAX_MESSAGE_BYTES - 200) + f"\n\n@{profile.handle}"


async def _ask(
    conn: AsyncConnection, chat: ChatAccess, profile: Profile, run_as: Principal, message_id: int, body: str
) -> int | None:
    """Hand the first message, the hub's, to @ba for the type's run_as (hop 1), whom a run woken by a message of the
    hub acts for (``chat_runs.chat_run``); the chat run it made, if any. A refusal (the budget, a worker) is said in
    the thread."""
    kept, _, _ = await resolve_mentions(conn, chat, run_as, [profile.handle])
    hand = Hand(chat, message_id, message_id, profile, run_as.user_id, run_as.login, MACHINE, 1, HUB, body)
    await hand_mentions(conn, hand, kept)
    await insert_mentions(conn, message_id, kept)
    refusals = [mention["refusal"] for mention in kept if mention["refusal"]]
    if refusals:
        await hub_reply(conn, chat.chat_id, message_id, "\n".join(refusals))
    return next((mention.get("run_id") for mention in kept if mention.get("run_id")), None)


__all__ = ["SETTLED", "Opened", "open_chat", "opened", "tell_reviewers"]
