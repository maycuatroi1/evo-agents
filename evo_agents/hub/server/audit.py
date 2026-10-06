"""The audit trail: one row per command that changes something, saying who did it (the user and the token they
used), the action, its target, the project it happened in and when. A target names things, such as a project, a
login, a host or a token id; it never holds content or a credential. Rows are only ever inserted (schema 0001
rejects updates), in the same transaction as the change they describe, so a change that rolls back leaves no row.

The project (schema 0007) comes from the action and the target unless the caller names it: the first name of the
target for grant, project, plan, kg and blob actions, the project of ``skill:project/<project>/<name>``, and the
project of the memory a memory action names. Actions outside a project (signing in and out, tokens, global skills,
personal memories) have none.
"""

from __future__ import annotations

import re

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
# Workers belong to no project; a target names the pairing or the worker by id and name, never a code or a token.
WORKER_PAIR = "worker.pair"  # a member created a pairing code: "pairing:<id> name=<worker name>"
WORKER_JOIN = "worker.join"  # a machine joined with a code, as its owner with the new worker token
WORKER_REGISTER = "worker.register"  # a machine registered itself with its machine token
WORKER_DRAIN = "worker.drain"
WORKER_UNDRAIN = "worker.undrain"
WORKER_REVOKE = "worker.revoke"  # the worker and its token are revoked, its runs released
# Runs happen in a project, which the caller names; a target is "<project>/<plan>#<step> run:<id>" ("<project>/<plan>
# run:<id>" for a plan run), never a prompt or evidence.
RUN_DISPATCH = "run.dispatch"  # a member queued a run of a ready step
RUN_DISPATCH_PLAN = "run.dispatch_plan"  # a member queued a plan run: "<project>/<plan> run:<id>", no step
RUN_STEP_REPORT = "run.step_report"  # a plan run's worker wrote a step, as the dispatcher: "... run:<id> status=<s>"
RUN_CANCEL = "run.cancel"  # the owner cancelled a run, or asked its worker to stop it
RUN_APPROVE = "run.approve"  # the owner approved a run in review, and the step is done
RUN_RERUN = "run.rerun"  # the owner queued the step again after a run that ended
RUN_MESSAGE = "run.message"  # the owner sent the run's agent a message; the target names the message, not its text
RUN_TAKEOVER = "run.takeover"  # the owner asked to drive the run's agent in a terminal
RUN_HANDBACK = "run.handback"  # the owner asked to let the run's agent go on headless
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

NAMED_FAMILIES = frozenset({"grant", "project", "plan", "kg", "blob"})  # targets that start with the project's name
_FIRST_NAME = re.compile(r"([a-z0-9][a-z0-9-]{0,99})(?:[/ @]|$)")
_PROJECT_SKILL = re.compile(r"skill:project/([a-z0-9][a-z0-9-]{0,99})/")
_MEMORY = re.compile(r"memory:([0-9]{1,18})")

INSERT = """
INSERT INTO audit (actor_id, token_id, action, target, project_id)
VALUES (%(actor_id)s, %(token_id)s, %(action)s, %(target)s,
        coalesce(%(project_id)s::bigint,
                 (SELECT id FROM projects WHERE name = %(project)s::text),
                 (SELECT project_id FROM memories WHERE id = %(memory_id)s::bigint)))
"""


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


async def record(
    conn, *, actor_id: int | None, token_id: int | None, action: str, target: str, project_id: int | None = None
) -> None:
    project, memory_id = (None, None) if project_id is not None else subject(action, target)
    params = {
        "actor_id": actor_id,
        "token_id": token_id,
        "action": action,
        "target": target,
        "project_id": project_id,
        "project": project,
        "memory_id": memory_id,
    }
    await conn.execute(INSERT, params)


def token_target(token_id: int) -> str:
    return f"token:{token_id}"
