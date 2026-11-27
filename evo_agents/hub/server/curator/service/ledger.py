"""The Curator's ledger: its one writer (``append``), the lines the hub adds as a proposal moves on, and what GET
.../proposals/{id}/ledger answers; ``evo_agents.hub.server.ledger`` describes them and re-exports them."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import null
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import ledger, review
from evo_agents.hub.server.base.unit_of_work import UnitOfWork
from evo_agents.hub.server.curator.models.ledger import SHA, Ledger, LedgerLine
from evo_agents.hub.server.curator.repository import ledger as repo
from evo_agents.hub.server.projects import project_access
from evo_agents.hub.server.security import Principal

# Writing


def _what(text: str) -> str:
    flat = " ".join(str(text or "").split()) or "-"
    return flat if len(flat) <= ledger.MAX_WHAT_CHARS else flat[: ledger.MAX_WHAT_CHARS - 3].rstrip() + "..."


def _sha(value) -> str | None:
    return value if isinstance(value, str) and SHA.match(value) else None


def _json(value):
    return value if isinstance(value, dict) else null()


async def append(
    conn: AsyncConnection,
    *,
    project_id: int,
    proposal_id: int,
    action: str,
    actor: str,
    what: str,
    change_id: int | None = None,
    actor_id: int | None = None,
    run_id: int | None = None,
    commit_sha: str | None = None,
    before_sha: str | None = None,
    after_sha: str | None = None,
    figures: dict | None = None,
    verdict: dict | None = None,
    pr_url: str | None = None,
    pr_number: int | None = None,
    merged_at=None,
    outcome: str | None = None,
    details: dict | None = None,
) -> int:
    """Add a line to proposal ``proposal_id``'s ledger, in the caller's transaction; its id. The one write of
    curator_ledger: lines are never changed or deleted."""
    if action not in ledger.ACTIONS or actor not in ledger.ACTORS:
        raise ValueError(f"no ledger line is {action!r} by {actor!r}")
    if actor == "agent" and run_id is None:
        raise ValueError("a line of an agent names its run")
    values = {
        "project_id": project_id,
        "proposal_id": proposal_id,
        "change_id": change_id,
        "action": action,
        "actor": actor,
        "actor_id": actor_id if actor == "user" else None,
        "run_id": run_id,
        "what": _what(what),
        "commit_sha": _sha(commit_sha),
        "before_sha": _sha(before_sha),
        "after_sha": _sha(after_sha),
        "figures": _json(figures),
        "verdict": _json(verdict),
        "pr_url": pr_url or None,
        "pr_number": pr_number if isinstance(pr_number, int) and pr_number >= 1 else None,
        "merged_at": merged_at,
        "outcome": outcome,
        "details": _json(details),
    }
    return await repo.insert_line(conn, values)


async def _evidence_texts(conn: AsyncConnection, proposal) -> list[str]:
    """The evidence of a proposal (a row with ``evidence`` and ``finding_ids``) and of its findings, as text."""
    items = [item for item in proposal.evidence or [] if isinstance(item, dict)]
    ids = list(proposal.finding_ids or [])
    if ids:
        for evidence in await repo.finding_evidence(conn, ids):
            items += [item for item in evidence or [] if isinstance(item, dict)]
    return [review.evidence_text(item) for item in items]


async def trigger_for(conn: AsyncConnection, proposal) -> dict:
    """The figures that set off ``proposal`` (a row with ``run_id``, ``lens``, ``evidence`` and ``finding_ids``):
    ``ledger.trigger_of`` over the night's figures of the review run that wrote it."""
    row = await repo.run_figures(conn, proposal.run_id)
    figures = row.figures if row is not None else None
    texts = await _evidence_texts(conn, proposal)
    return ledger.trigger_of(figures, proposal.lens, texts, night=row.night.isoformat() if row is not None else None)


async def cites_corrections(conn: AsyncConnection, proposal) -> bool:
    """Whether the evidence of ``proposal`` (a row with ``run_id``, ``evidence`` and ``finding_ids``, its findings'
    included) points at an entry of ``corrections`` in the night's figures of the review run that wrote it."""
    row = await repo.run_figures(conn, proposal.run_id)
    if row is None or not isinstance(row.figures, dict):
        return False
    return "corrections" in ledger.evidence_metrics(row.figures, await _evidence_texts(conn, proposal))


async def proposal_recorded(conn: AsyncConnection, project_id: int, proposal_id: int) -> None:
    """The first line of a proposal a review run recorded: ``proposed``, or ``dropped`` as a repeat, by the run's
    agent, with the figures that set it off."""
    row = await repo.recorded_proposal(conn, proposal_id)
    dropped = row.state == "dropped"
    what = (
        f"Review run #{row.run_id} proposed it ({row.kind}, tier {row.tier}): {row.title}"
        if not dropped
        else f"Review run #{row.run_id} proposed it again; dropped as a repeat of rejected proposal #{row.duplicate_of}"
    )
    await append(
        conn,
        project_id=project_id,
        proposal_id=proposal_id,
        action="dropped" if dropped else "proposed",
        actor="agent",
        run_id=row.run_id,
        what=what,
        figures=await trigger_for(conn, row),
        details={
            "kind": row.kind,
            "tier": row.tier,
            "lens": row.lens,
            "evidence": row.evidence_count,
            "duplicate_of": row.duplicate_of,
        },
    )


async def proposal_answered(
    conn: AsyncConnection,
    *,
    project_id: int,
    proposal_id: int,
    user_id: int,
    login: str,
    action: str,
    note: str | None,
    days: int | None,
    via: str | None,
    agent: str | None = None,
    chat_run_id: int | None = None,
) -> None:
    """The line of an admin's answer: accepted, rejected or deferred, by them, or by the project's ``agent`` for them in
    chat run ``chat_run_id`` (``curator.service.threads``), which ``details`` names."""
    state = review.ANSWERED[action]
    what = f"{login} {state} it"
    if action == "defer" and days:
        what += f" for {days} days"
    if via:
        what += f" ({via})"
    if note:
        what += f": {note}"
    await append(
        conn,
        project_id=project_id,
        proposal_id=proposal_id,
        action=state,
        actor="user",
        actor_id=user_id,
        what=what,
        details={
            "note": note,
            "via": via,
            **({"days": days} if days else {}),
            **({"agent": agent, "chat_run": chat_run_id} if agent else {}),
        },
    )


async def change_recorded(conn: AsyncConnection, change_id: int) -> None:
    """The line of what an accepted proposal became: the Curator's plan on its branch, or a change left open."""
    row = await repo.change(conn, change_id)
    if row.plan_id is None:
        await append(
            conn,
            project_id=row.project_id,
            proposal_id=row.proposal_id,
            change_id=row.id,
            action="left_open",
            actor="curator",
            what=row.reason or "it could not become the Curator's plan",
        )
        return
    await append(
        conn,
        project_id=row.project_id,
        proposal_id=row.proposal_id,
        change_id=row.id,
        action="planned",
        actor="curator",
        what=f"It became the Curator's plan {row.plan_id}, on {row.branch} of {row.repo} alone",
        details={"plan_id": row.plan_id, "repo": row.repo, "branch": row.branch, "forge": row.forge},
    )


def _verdict(row) -> dict:
    found = row.verdict if isinstance(row.verdict, dict) else {}
    return {
        "passed": bool(row.passed),
        "source": found.get("source"),
        "agent": found.get("agent"),
        "failures": list(found.get("failures") or []),
        "signs": len(found.get("signs") or []),
        "verify": len(found.get("verify") or []),
        "hidden": len(found.get("hidden") or []),
    }


async def _builder_commit(conn: AsyncConnection, run_id: int | None) -> str | None:
    """The commit the Builder's last step report named, else the run's own."""
    if run_id is None:
        return None
    return await repo.builder_commit(conn, run_id)


async def change_moved(conn: AsyncConnection, change_id: int, old: str, new: str, extra: dict | None = None) -> None:
    """The lines a move of change ``change_id`` from ``old`` to ``new`` adds, from the change as it is now and
    ``extra`` (``actor``, ``actor_id``, ``login``, ``before_sha`` of a merge, and ``agent`` and ``chat_run`` of one the
    agent of a proposal's thread made for ``login``; ``run_id`` and ``note`` of a Builder that dropped it, ``actor``
    ``curator`` of the hub's close of a branch that changes nothing; ``merge_request`` and ``into``, its iid and
    target branch, of a merge of the queue on GitLab)."""
    extra = extra or {}
    row = await repo.change(conn, change_id)
    line = {"project_id": row.project_id, "proposal_id": row.proposal_id, "change_id": row.id}
    pull = {"pr_url": row.pr_url, "pr_number": row.pr_number}
    if old == "planned" and new in ("pr_pending", "judge_pending") and row.builder_run_id is not None:
        where = "the hub opens its pull request" if new == "pr_pending" else "its merge request is open"
        await append(
            conn,
            **line,
            action="built",
            actor="agent",
            run_id=row.builder_run_id,
            commit_sha=await _builder_commit(conn, row.builder_run_id),
            what=f"Builder run #{row.builder_run_id} did every step on {row.branch} of {row.repo}: {where}",
            details={"branch": row.branch, "repo": row.repo},
        )
        return
    if old == "pr_pending" and new in ("judge_pending", "judged"):
        await append(
            conn,
            **line,
            **pull,
            action="pull_opened",
            actor="curator",
            commit_sha=row.head_sha,
            what=f"The hub opened pull request #{row.pr_number} of {row.branch} into {row.base_branch}",
        )
    if new == "judged":
        verdict = _verdict(row)
        by_hub = verdict["source"] == "hub" or row.judge_run_id is None
        failures = "; ".join(verdict["failures"])
        if by_hub:
            what = f"The hub failed it before any Judge: {failures or 'signs of score hacking'}"
        else:
            what = f"Judge run #{row.judge_run_id} {'passed' if row.passed else 'failed'} it"
            what += f": {failures}" if failures and not row.passed else ""
        base = (row.verdict or {}).get("base_sha") if isinstance(row.verdict, dict) else None
        await append(
            conn,
            **line,
            **pull,
            action="judged",
            actor="curator" if by_hub else "agent",
            run_id=None if by_hub else row.judge_run_id,
            commit_sha=row.head_sha,
            before_sha=base,
            verdict=verdict,
            what=what,
        )
        return
    if new == "merged":
        await _merged(conn, row, line, pull, extra)
        return
    if new == "open" and old != "open":
        await append(conn, **line, **pull, action="left_open", actor="curator", what=row.reason or "left open")
        return
    if new == "closed" and extra.get("actor") == "curator":  # its branch changes nothing (``changes``)
        await append(
            conn, **line, action="closed", actor="curator", what=f"The hub closed change #{row.id}: {row.reason}"
        )
        return
    if new == "closed" and extra.get("actor") == "agent":  # its Builder dropped it (``changes.routes.drop_change``)
        run_id = extra["run_id"]
        await append(
            conn,
            **line,
            action="closed",
            actor="agent",
            run_id=run_id,
            commit_sha=await _builder_commit(conn, run_id),
            what=f"Builder run #{run_id} dropped change #{row.id} for {extra.get('login')}: {row.reason}",
            details={"note": extra["note"]} if extra.get("note") else None,
        )
        return
    if new == "closed":
        who = extra.get("login") or "A member"
        what = f"{who} closed pull request #{row.pr_number} without a merge"
        details = {"github_login": extra["login"]} if extra.get("login") else None
        if row.pr_number is None:  # an admin closed on the hub a change left open with none
            what, details = f"{who} closed change #{row.id}: {row.reason}", None
        await append(
            conn,
            **line,
            **pull,
            action="closed",
            actor="user",
            actor_id=extra.get("actor_id"),
            what=what,
            details=details,
        )


async def _merged(conn: AsyncConnection, row, line: dict, pull: dict, extra: dict) -> None:
    """The line of a merge of change ``row``, by the hub, a member or the agent of its proposal's thread, after the
    playbook entries that held until its proposal landed are retired."""
    actor = extra.get("actor") or "curator"
    who = "The hub" if actor == "curator" else (extra.get("login") or "A member")
    what = f"{who} merged pull request #{row.pr_number} into {row.base_branch} as {row.merge_sha}"
    if extra.get("merge_request"):  # its merge queue on GitLab merged it
        what = f"{who}'s merge queue merged !{extra['merge_request']} into {extra.get('into')} as {row.merge_sha}"
    details = {"github_login": extra["login"]} if extra.get("login") else {}
    if extra.get("agent"):  # the agent of the proposal's thread, for its owner (``curator.service.threads``)
        what = f"{extra['agent']} merged pull request #{row.pr_number} into {row.base_branch} as {row.merge_sha}"
        what += f" for {extra.get('login')} (chat run {extra.get('chat_run')})"
        details = {"agent": extra["agent"], "chat_run": extra.get("chat_run")}
    if actor == "curator":
        details, what = await _replayed_merge(conn, row, details, what)
    # it reads the proposals of the routes that import this module
    from evo_agents.hub.server.playbook import service as playbook

    await playbook.retire_due(conn, row.project_id)  # the entries that held until this proposal landed
    await append(
        conn,
        **line,
        **pull,
        action="merged",
        actor=actor,
        actor_id=extra.get("actor_id"),
        commit_sha=row.head_sha,
        before_sha=extra.get("before_sha"),
        after_sha=row.merge_sha,
        merged_at=row.merged_at,
        what=what,
        details=details or None,
    )


async def _replayed_merge(conn: AsyncConnection, row, details: dict, what: str) -> tuple[dict, str]:
    """The details and the words of the hub's merge of change ``row`` once its replays won at the head it merged
    (``change_tiers.replay_verdict``): the verdict under ``replay`` and, when the merge releases a global skill
    (``skill_release.wanted``), the skill under ``release``; as they were otherwise."""
    from evo_agents.hub.server.changes.service import tiers as change_tiers
    from evo_agents.hub.server.releases.service import skill_release  # skill_release writes through this module

    found = await change_tiers.replay_verdict(conn, row.id, row.head_sha)
    if not found["won"]:
        return details, what
    details = {**details, "replay": found}
    what += f"; its replays won: {found['reason']}"
    release = await skill_release.wanted(conn, row, found)
    if release is not None:
        details["release"] = release
        what += f"; the hub publishes global skill {release['skill']} from it"
    return details, what


async def build_failed(conn: AsyncConnection, change, run_id: int, state: str, why: str) -> None:
    """The line of a Builder of ``change`` (a row of curator_changes) that ended ``state`` without its plan done."""
    await append(
        conn,
        project_id=change.project_id,
        proposal_id=change.proposal_id,
        change_id=change.id,
        action="build_failed",
        actor="agent",
        run_id=run_id,
        what=f"Builder run #{run_id} ended {state}: {why}",
    )


# Reading


async def outcome_due_at(conn: AsyncConnection, proposal_id: int, project_id: int):
    """When the hub counts the figures of proposal ``proposal_id``'s merged change again; None when it is not merged
    or its outcome is in."""
    row = await repo.merged_change(conn, proposal_id)
    if row is None:
        return None
    measured = await repo.outcome_in(conn, row.id)
    if measured:
        return None
    from evo_agents.hub.server.outcomes import outcome_days

    return row.merged_at + timedelta(days=await outcome_days(conn, project_id))


async def show_ledger(engine: AsyncEngine, user: Principal, project: str, proposal_id: int, sink: str | None) -> Ledger:
    """The ledger of proposal ``proposal_id`` of ``project`` for ``user`` (with the hub sink header ``sink``), as GET
    .../proposals/{id}/ledger answers."""
    from evo_agents.hub.server.proposals.service.reads import readable_proposal  # it writes lines through this module
    from evo_agents.hub.server.proposals.service.record import require_reader

    async with UnitOfWork(engine) as conn:
        access = await project_access(conn, user, project)
        require_reader(access)
        await readable_proposal(conn, access, proposal_id, sink, user)
        rows = await repo.lines(conn, proposal_id)
        due = await outcome_due_at(conn, proposal_id, access.project_id)
    return Ledger(
        project=access.name,
        proposal_id=proposal_id,
        lines=[LedgerLine(**row._mapping) for row in rows],
        outcome_due_at=due,
    )
