"""The audit trail: one row per command that changes something, saying who did it (the user and the token they
used), the action, its target, the project it happened in and when. A target names things, such as a project, a
login, a host or a token id; it never holds content or a credential. Rows are inserted in the same transaction as
the change they describe, so a change that rolls back leaves no row, and never updated (schema 0001 rejects updates).
The hub's worker deletes the rows older than EVO_HUB_AUDIT_DAYS, 365 by default, once a day (hub.prune_audit).

The project (schema 0007) comes from the action and the target unless the caller names it: the first name of the
target for grant, project, plan, kg and blob actions, the project of ``skill:project/<project>/<name>``, and the
project of the memory a memory action names. Actions outside a project (signing in and out, tokens, global skills,
personal memories) have none.
"""

from __future__ import annotations

import re

from sqlalchemy import BigInteger, Insert, String, bindparam, insert, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables

LOGIN = "auth.login"
LOGOUT = "auth.logout"
TOKEN_REVOKE = "token.revoke"
GRANT_PUT = "grant.put"
GRANT_DELETE = "grant.delete"
PROJECT_REGISTER = "project.register"
PROJECT_UPDATE = "project.update"
BLOB_COMMIT = "blob.commit"  # the target is the project, never the hashes
KG_CONFIG = "kg.config"  # the knowledge config a project's graph is built with changed; the target is the project
KG_INGEST = "kg.ingest"  # a pushed run became part of the project's corpus; the target is the project, never the run
KG_BUILD = "kg.build"  # a member queued a build; the target is the project
KG_PRUNE = "kg.prune"  # the retention deleted artifacts of a project's older graphs; the target is "<project> keep=<n>"
# An admin deleted old graphs and source blobs of a project that hold a secret (evo_agents.hub.server.kg_scrub); the
# target counts them, "<project> artifacts=<n> blobs=<n>", never a hash or a value.
KG_SCRUB = "kg.scrub"
# Workers belong to no project; a target names the pairing or the worker by id and name, never a code or a token.
WORKER_PAIR = "worker.pair"  # a member created a pairing code: "pairing:<id> name=<worker name>"
WORKER_JOIN = "worker.join"  # a machine joined with a code, as its owner with the new worker token
WORKER_REGISTER = "worker.register"  # a machine registered itself with its machine token
WORKER_DRAIN = "worker.drain"
WORKER_UNDRAIN = "worker.undrain"
WORKER_REVOKE = "worker.revoke"  # the worker and its token are revoked, its runs released
# The owner set, on the web, who may hand the worker runs: "worker:<id> name=<n> owner=<login> dispatch_from=<any|web>"
WORKER_DISPATCH_FROM = "worker.dispatch_from"
# The owner changed the worker's settings; the target names each field that changed, "<field>=<was>-><is>", after
# "worker:<id> name=<n> owner=<login>": "slots=1->4 labels=[]->[gpu,mac] name=old->new", never a token.
WORKER_UPDATE = "worker.update"
# Runs happen in a project, which the caller names; a target is "<project>/<plan>#<step> run:<id>" ("<project>/<plan>
# run:<id>" for a plan run), never a prompt or evidence.
RUN_DISPATCH = "run.dispatch"  # a member queued a run of a ready step
RUN_DISPATCH_PLAN = "run.dispatch_plan"  # a member queued a plan run: "<project>/<plan> run:<id>", no step
RUN_DISPATCH_AUTHOR = "run.dispatch_author"  # a member queued an author run: "<project>/author run:<id>", no request
RUN_STEP_REPORT = "run.step_report"  # a plan run's worker wrote a step, as the dispatcher: "... run:<id> status=<s>"
RUN_CANCEL = "run.cancel"  # the owner cancelled a run, or asked its worker to stop it
RUN_APPROVE = "run.approve"  # the owner approved a run in review, and the step is done
RUN_RERUN = "run.rerun"  # the owner queued the step again after a run that ended
RUN_MESSAGE = "run.message"  # the owner sent the run's agent a message; the target names the message, not its text
RUN_TAKEOVER = "run.takeover"  # the owner asked to drive the run's agent in a terminal
RUN_HANDBACK = "run.handback"  # the owner asked to let the run's agent go on headless
RUN_FINISH = "run.finish"  # the owner ended the chat of an author run: "<project>/author run:<id>"
# Decisions and notifications; a target names the decision or the notifications by id, and an answer's option, never
# the question, the answer's text or a notification's title.
DECISION_ANSWER = "decision.answer"  # the owner answered: "<project>/<plan>[#<step>] decision:<id> run:<id> option=<k>"
NOTIFICATION_READ = "notification.read"  # a member marked notifications read: "notifications:<id>,<id>" or ":all"
# The web terminal of a run; the target names the run as above, and on close the bytes relayed each way and how it
# ended ("... to_worker=<n> to_browser=<n> end=<idle|timeout|browser|worker|protocol|shutdown>"), never the bytes.
TERMINAL_OPEN = "terminal.open"  # the owner opened the run's terminal in a browser
TERMINAL_CLOSE = "terminal.close"
# A member's secrets belong to no one project; the target is the secret's name, never its value.
SECRET_PUT = "secret.put"  # the owner created a secret or replaced it, value included
SECRET_DELETE = "secret.delete"  # the owner deleted it: its sealed value and bindings went, its leases were revoked
# A member's accounts (docs/credentials.md, section Accounts): an env secret's settings, by its id ("secret:<id>
# weight=<n> priority=<n> paused=<yes|no>"), and the member's policy for a project, in that project ("<project>
# strategy=<s> cooldown=<seconds> failover=<yes|no> max_failovers=<n>", or the project alone when it was dropped).
ACCOUNT_SET = "account.set"  # the owner changed the weight or priority of an account, or paused or resumed it
ACCOUNT_POLICY_PUT = "account.policy-put"  # the member set their policy for the project
ACCOUNT_POLICY_DELETE = "account.policy-delete"  # the member dropped it: the defaults hold again
# A run's account hit its usage limit and the run moved to another one, as its worker (the actor) reported it, in the
# run's project: "<run target> env=<variable> from=secret:<id> to=secret:<id>".
ACCOUNT_FAILOVER = "account.failover"
# A project's agent profiles (evo_agents.hub.server.agent_profiles), in the project; the target is
# "<project>/@<handle> v<version>", with " fields=<names>" for an edit, never a description, instructions or an image.
AGENT_CREATE = "agent.create"  # an admin of the project created an agent profile, at version 1
AGENT_UPDATE = "agent.update"  # an admin changed it, its avatar included: the version it made and the fields changed
AGENT_DELETE = "agent.delete"  # an admin soft-deleted it: it takes no new run
# Group chats (evo_agents.hub.server.chats); the target is "chat:<id>", then "person:<login>", "agent:@<handle>" or
# "message:<id>", never a title or a message's text. Adding or removing an agent is in the agent's project.
CHAT_CREATE = "chat.create"  # a member created a chat, with the people and agents it started with
CHAT_ADD = "chat.add"  # a member added a person or an agent
CHAT_REMOVE = "chat.remove"  # a member removed one, or left
CHAT_MESSAGE = "chat.message"  # a member sent a message
# The leases of a run, in the run's project; the target names the run as above, then the secrets by name, the GitHub
# App's accounts and the repos, never a value: "... run:<id> secrets=<names> github-app=<accounts> repos=<repos>".
CREDENTIAL_LEASE = "credential.lease"  # the worker holding the run asked for its leases
# Leases given back: "... run:<id> leases=<n> secrets=<names> github-app=<tokens> by=<worker|run-<state>|...>",
# by the worker (its actor), or by the hub itself when the run left the held states or its worker was revoked.
CREDENTIAL_REVOKE = "credential.revoke"

NAMED_FAMILIES = frozenset({"grant", "project", "plan", "kg", "blob"})  # targets that start with the project's name
_FIRST_NAME = re.compile(r"([a-z0-9][a-z0-9-]{0,99})(?:[/ @]|$)")
_PROJECT_SKILL = re.compile(r"skill:project/([a-z0-9][a-z0-9-]{0,99})/")
_MEMORY = re.compile(r"memory:([0-9]{1,18})")


def subject(action: str, target: str) -> tuple[str | None, int | None]:
    """The project an action happened in, as a project name or the id of the memory it changed; (None, None) for
    an action outside any project."""
    family = action.partition(".")[0]
    if family == "memory":
        found = _MEMORY.fullmatch(target)
        return None, int(found[1]) if found else None
    if family == "skill":
        found = _PROJECT_SKILL.match(target)
        return (found[1] if found else None), None
    if family in NAMED_FAMILIES:
        found = _FIRST_NAME.match(target)
        return (found[1] if found else None), None
    return None, None


def _insert(project_id) -> Insert:
    """An audit row of :audit_actor, :audit_token, :audit_action and :audit_target in the project ``project_id``
    gives. The parameters name no column of audit: an INSERT would take a parameter named after a column as the
    value of that column."""
    return insert(tables.audit).values(
        actor_id=bindparam("audit_actor", type_=BigInteger),
        token_id=bindparam("audit_token", type_=BigInteger),
        action=bindparam("audit_action", type_=String),
        target=bindparam("audit_target", type_=String),
        project_id=project_id,
    )


# Built once (docs/hub.md, Data access): the project given by id, by name, as the memory's, or none.
_IN_PROJECT = _insert(bindparam("audit_project_id", type_=BigInteger))
_IN_NAMED = _insert(
    select(tables.projects.c.id).where(tables.projects.c.name == bindparam("audit_project")).scalar_subquery()
)
_OF_MEMORY = _insert(
    select(tables.memories.c.project_id).where(tables.memories.c.id == bindparam("audit_memory")).scalar_subquery()
)


async def record(
    conn: AsyncConnection,
    *,
    actor_id: int | None,
    token_id: int | None,
    action: str,
    target: str,
    project_id: int | None = None,
) -> None:
    row = {"audit_actor": actor_id, "audit_token": token_id, "audit_action": action, "audit_target": target}
    statement = _IN_PROJECT
    if project_id is None:
        project, memory_id = subject(action, target)
        if project is not None:
            statement, row["audit_project"] = _IN_NAMED, project
        elif memory_id is not None:
            statement, row["audit_memory"] = _OF_MEMORY, memory_id
    row["audit_project_id"] = project_id
    await conn.execute(statement, row)


def token_target(token_id: int) -> str:
    return f"token:{token_id}"
