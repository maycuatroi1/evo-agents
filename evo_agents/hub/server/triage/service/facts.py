"""What the first message of a thread says of its run (``triage.service.threads`` is the design): the head (state,
cause, error, plan and step, repo and branch, the last verify, links), why no agent is asked when no account is ready,
who could run it again, the projects its error names, the diagnosis of a run that stopped moving, the chain of
automatic reruns that led to it, and the worker its agent installs a missing program on."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import runs, triage_rules
from evo_agents.hub.accounts import account_state
from evo_agents.hub.server.chats.service.members import label_problems
from evo_agents.hub.server.profile_store import by_id
from evo_agents.hub.server.run_state import RunStep
from evo_agents.hub.server.runs.service.candidates import candidates_of
from evo_agents.hub.server.runs.service.reroute import tried_on
from evo_agents.hub.server.triage.models.threads import ERROR_CHARS, SHOWN, STALE_CAUSE, STALE_MINUTES, line, when
from evo_agents.hub.server.triage.models.threads import event_text as event_said
from evo_agents.hub.server.triage.repository import threads as store

log = logging.getLogger(__name__)


def verify_text(found: RunStep) -> str:
    """The last verify run ``found`` reported, with its exit code; ``none reported`` for none."""
    items = [item for item in found.verify or [] if isinstance(item, dict)]
    if not items:
        return "none reported"
    last = items[-1]
    return f"`{' '.join(str(last.get('command', '')).split())}` exit {last.get('exit_code')}"


def head_lines(found: RunStep, inferred: bool, *, stale: bool = False) -> list[str]:
    """The first lines of the thread of run ``found``: what happened, its cause, error and place, its last verify, the
    links to its page, log and diff."""
    what = {"plan": "Plan run", "author": "Author run"}.get(found.kind, "Run")
    where = (
        f" of step {found.step_key} of {found.plan_id}"
        if found.kind == "step"
        else (f" of {found.plan_id}" if found.plan_id else "")
    )
    if stale:
        lines = [
            f"{what} #{found.run_id}{where} has written no event for {STALE_MINUTES} minutes while its worker still "
            "sends heartbeats.",
            f"Cause: {STALE_CAUSE} (seen by the hub; the run still holds its worker).",
        ]
    else:
        cause = found.failure_cause or "unknown"
        guess = "guessed by the hub from the error" if inferred else "as the worker reported it"
        lines = [f"{what} #{found.run_id}{where} failed.", f"Cause: {cause} ({guess})."]
    error = line(found.error or "")
    if error:
        lines.append("Error: " + runs.clip(error, ERROR_CHARS))
    place = [f"plan {found.plan_id}"] if found.plan_id else []
    place += [f"step {found.step_key}"] if found.step_key else []
    place += [f"repo {found.repo}"] if found.repo else []
    place += [f"branch {found.branch}"] if found.branch else []
    lines.append("Where: " + (", ".join(place) or found.project) + ".")
    lines.append(f"Last verify: {verify_text(found)}.")
    base = f"/v1/projects/{found.project}/runs/{found.run_id}"
    lines.append(f"Run: /p/{found.project}/runs/{found.run_id}, log: {base}/events, diff: {base}/diff")
    return lines


async def no_account(conn: AsyncConnection, found: RunStep) -> str | None:
    """The line that says why no agent is asked, when the hub stopped run ``found`` on the accounts of a variable and
    none of its owner's accounts for it is ready for the project now (expired, paused or cooling); None otherwise."""
    if found.failure_cause not in triage_rules.NEW_ACCOUNT:
        return None
    now = datetime.now(UTC)
    for env_var in await store.stopped_env_vars(conn, found.run_id):
        rows = await store.account_rows(conn, found.dispatcher_id, found.project_id, env_var)  # pyright: ignore[reportArgumentType]
        states = [
            (
                account_state(
                    r.paused_at,
                    r.cooldown_until,
                    None if r.oauth_sealed is not None else r.expires_at,  # an OAuth token is renewed at lease time
                    now,
                ),
                r.cooldown_until,
            )
            for r in rows
        ]
        if not states or any(state == "ready" for state, _ in states):
            continue
        cooling = [until for state, until in states if state == "cooling"]
        until = f"before {min(cooling).isoformat()}" if cooling else "until the owner resumes or renews one"
        return (
            f"No account of the owner for {env_var} is ready {until}: the hub asks no agent, whose run would take "
            "the same accounts. Run it again once one is ready, or on another runtime."
        )
    return None


async def candidates(conn: AsyncConnection, found: RunStep, role: str | None, chat) -> list[str]:
    """The lines that say who could run the run again; one saying why there are none when they cannot be read."""
    try:
        async with conn.begin_nested():
            found_for = await candidates_of(
                conn,
                project=found.project,
                project_id=found.project_id,  # pyright: ignore[reportArgumentType]
                role=role or "reader",
                owner_id=found.dispatcher_id,
                kind=found.kind,
                state="failed",
                run_id=found.run_id,
            )
    except Exception as exc:
        log.warning("triage candidates not read", extra={"run_id": found.run_id, "error": type(exc).__name__})
        return ["Run again: the hub could not read who could."]
    workers = [w for w in found_for.workers if w.fit][:SHOWN]
    agents = [a for a in found_for.agents if a.fit and a.project == found.project][:SHOWN]
    lines = ["Run again with:"]
    lines += [f"- worker {w.worker}" + (" (the run's own)" if w.is_run_worker else "") for w in workers]
    lines += [f"- agent {a.handle}" for a in agents]
    if not workers:
        lines.append(
            "- no worker of the owner is fit: "
            + "; ".join(f"{w.worker} {w.reasons[0]}" for w in found_for.workers[:SHOWN] if w.reasons)
            if found_for.workers
            else "- the owner has no worker that takes this project"
        )
    return lines + await other_projects(conn, found_for, chat)


async def other_projects(conn: AsyncConnection, found_for, chat) -> list[str]:
    """The lines about the projects the error names: whose agent can be invited, and what stops it for the others (no
    writer role, a label of the chat someone does not clear). Nobody is invited here."""
    if not found_for.other_projects:
        return []
    lines = ["The error names another project:"]
    for other in found_for.other_projects:
        fit = [a for a in found_for.agents if a.project == other.project and a.fit]
        if not other.fit or not fit:
            why = (
                other.reasons[0]
                if not other.fit
                else f"no agent of {other.project} has a worker of the owner to run on"
            )
            lines.append(f"- {other.project} (the error names {other.named}): no one is invited: {why}.")
            continue
        profile = await by_id(conn, fit[0].profile_id)
        try:
            async with conn.begin_nested():
                problems = await label_problems(conn, chat, profile) if profile is not None else []
        except Exception as exc:
            log.warning("triage label not read", extra={"error": type(exc).__name__})
            lines.append(f"- {other.project}: the hub could not read the chat's label. No one is invited.")
            continue
        if problems:
            lines.append(
                f"- {other.project} (the error names {other.named}): agent {fit[0].handle} cannot join, the chat's label "
                f"would pass someone in it: {'; '.join(problems)}. No one is invited."
            )
        else:
            lines.append(
                f"- {other.project} (the error names {other.named}): the owner is a writer there; the agent of this "
                f"thread may invite one of its agents ({', '.join(a.handle for a in fit[:SHOWN])}) with `evo-agents worker triage invite`."
            )
    return lines


async def diagnosis(conn: AsyncConnection, found: RunStep) -> list[str]:
    """The lines of a run that stopped moving, stale or lost on its last attempt: its last event (the hub's own move to
    the end it is in left out), its worker and that worker's last heartbeat, its lease and its attempt."""
    row = await store.lease_row(conn, found.run_id)
    last = await store.last_event(conn, found.run_id, ended=row.state in runs.TERMINAL_STATES)
    if last is not None:
        said = event_said(last.kind, last.body)
        lines = [f"Last event: #{last.seq} {last.kind} at {when(last.at, row.now)}" + (f": {said}" if said else "")]
    else:
        lines = ["Last event: none."]
    if found.worker:
        lines.append(f"Worker: {found.worker}, last heartbeat {when(row.last_heartbeat_at, row.now)}.")
    else:
        lines.append("Worker: none.")
    if row.state in runs.HELD_STATES:
        until = (
            "no end" if row.lease_expires_at is None else f"{row.lease_expires_at.astimezone(UTC):%Y-%m-%d %H:%M} UTC"
        )
        lines.append(f"Lease: held until {until}; the worker still extends it.")
    else:
        lines.append(f"Lease: ended with the run at {when(row.finished_at, row.now)}; the worker stopped extending it.")
    last_try = " (the last)" if found.attempt >= found.max_attempts else ""
    lines.append(f"Attempt: {found.attempt} of {found.max_attempts}{last_try}.")
    return lines


async def chain_of(conn: AsyncConnection, run_id: int) -> tuple[int, list[str]]:
    """(how many automatic reruns led to run ``run_id``, the cause of each): the runs it reruns, by ``parent_run_id``,
    for as long as the triage of that run reran it (``run_triage.auto_reruns``). A rerun by hand starts a chain."""
    causes: list[str] = []
    current = run_id
    while len(causes) <= triage_rules.RERUN_CAP:
        parent = await store.parent_of(conn, current)
        if parent is None:
            break
        row = await store.reruns_of(conn, parent)
        if row is None or row.auto_reruns < 1:
            break
        causes.append(row.cause or "unknown")
        current = parent
    return len(causes), causes


async def install_worker(conn: AsyncConnection, found: RunStep) -> tuple[int, str, list[str]] | None:
    """(the id and name of the worker the agent of failed run ``found`` installs what is missing on, the names of the
    workers the run was tried on) when the run lacks a program: the first of them that is its owner's own, so another
    member's machine is changed only when no other is left, else the one it failed on; None for any other failure."""
    if not triage_rules.lacks_program(found.failure_cause, found.error):
        return None
    tried, _ = await tried_on(conn, found.run_id)
    by_worker = {row.id: row for row in await store.workers_of(conn, tried)}
    held = [by_worker[worker_id] for worker_id in tried if worker_id in by_worker]
    if not held:
        return None
    own = [row for row in reversed(held) if row.owner_id == found.dispatcher_id]
    chosen = own[0] if own else held[0]
    return chosen.id, chosen.name, [row.name for row in reversed(held)]
