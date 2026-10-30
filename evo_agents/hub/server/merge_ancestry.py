"""What the hub checks after a pull request that passed the merge check was merged on github.com (plan
super-board-parity-2): that the merge happened at the head the hub judged, and that the merge commit sits on the base.

``settle`` runs on each pass of the job hub.merge_queue, after ``merge_check.advance``. For each check that passed and
has no merge recorded yet (merged within the last WINDOW_DAYS), it reads the pull request with the workers' GitHub App
(a token of pull_requests and contents read). A pull request still open or closed unmerged is left for the next pass. A merged
one gets its merge recorded in the check's verdict (``verdict.merged``: the merge commit, the head GitHub merged, the
head the hub judged, how the base stands to the merge commit, when it was seen), and the project's owner gets one
notice when ``problems`` finds any:

- the pull request merged at another head than the one judged (someone pushed after the verdict, and the merge did
  not pin the head), or
- the merge commit is not on the base branch (GitHub's compare says the base neither contains it nor is it).

A merge with no problem records ``on_base`` true and tells nobody. The check is the hub's own read of GitHub: it never
trusts what the agent says it merged.
"""

from __future__ import annotations

import logging
from collections import Counter
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import runs, tables
from evo_agents.hub.server import merge_check as checks
from evo_agents.hub.server import pulls
from evo_agents.hub.server.github import GitHubRefused, GitHubUnavailable
from evo_agents.hub.server.github_app import GitHubApp
from evo_agents.hub.server.notifications import notify
from evo_agents.hub.server.protection import github_target

log = logging.getLogger(__name__)

WINDOW_DAYS = 14  # a passed check older than this and still unmerged is no longer looked up
ON_BASE = ("ahead", "identical")  # GitHub's compare of merge_commit...base: the base holds the merge commit


def on_base(status: str | None) -> bool:
    """Whether GitHub's compare of the merge commit with the base (``pulls.compare_status``) says the base holds it."""
    return status in ON_BASE


def problems(judged: str, merged_head: str | None, merge_sha: str | None, status: str | None) -> list[str]:
    """What is wrong with a merge of a pull request the hub judged at ``judged``: the head GitHub merged
    (``merged_head``), the merge commit (``merge_sha``) and how the base stands to it (``status``)."""
    found = []
    if merged_head != judged:
        found.append(f"it merged at head {(merged_head or 'unknown')[:12]}, not at the head judged, {judged[:12]}")
    if not merge_sha:
        found.append("GitHub names no merge commit")
    elif not on_base(status):
        found.append(
            f"the merge commit {merge_sha[:12]} is not on the base branch (compare says {status or 'nothing'})"
        )
    return found


def merged_record(row, pull: dict, status: str | None, now: datetime) -> dict:
    """The ``verdict.merged`` entry of check ``row`` for a merged ``pull``."""
    merge_sha = pull.get("merge_commit_sha") if isinstance(pull.get("merge_commit_sha"), str) else None
    head = (pull.get("head") or {}).get("sha")
    found = problems(row.head_sha, head if isinstance(head, str) else None, merge_sha, status)
    return {
        "merge_sha": merge_sha,
        "merged_head": head,
        "judged_head": row.head_sha,
        "base": row.base_branch,
        "compare": status,
        "on_base": on_base(status),
        "problems": found,
        "seen_at": now.isoformat(),
    }


async def _read(app: GitHubApp, target, row) -> tuple[dict, str | None] | None:
    """(the pull request of ``row``, how the base stands to its merge commit) once it is merged; None while it is
    not, or when GitHub cannot tell now."""

    async def look(token: str):
        pull = await pulls.pull(app, token, target.owner, target.name, row.number)
        sha = pull.get("merge_commit_sha")
        if pull.get("merged") is not True and not pull.get("merged_at"):
            return pull, None
        status = None
        if isinstance(sha, str) and sha:
            status = await pulls.compare_status(app, token, target.owner, target.name, sha, row.base_branch)
        return pull, status

    where = {"check_id": row.id, "repo": row.repo, "pr": row.number}
    try:
        pull, status = await pulls.with_token(app, target.owner, target.name, "merged", look)
    except GitHubRefused as exc:
        log.warning("a merged pull request could not be read", extra={**where, "why": str(exc)})
        return None
    except GitHubUnavailable as exc:
        log.info(
            "GitHub did not answer for a merged pull request; the next pass asks", extra={**where, "why": str(exc)}
        )
        return None
    if pull.get("merged") is not True and not pull.get("merged_at"):
        return None
    return pull, status


async def _tell(conn: AsyncConnection, project, row, record: dict) -> None:
    """Open the notice of the project's owner about a merge with problems."""
    what = f"pull request #{row.number} of {row.repo}"
    lines = [f"- {line}" for line in record["problems"]]
    await notify(
        conn,
        user_id=project.owner_id,
        kind="notice",
        notice_kind=runs.AUTONOMY_DECISION_CATEGORY,
        project_id=row.project_id,
        run_id=row.run_id,
        title=f"Merge of {what} does not match the hub's check",
        body="\n".join([f"The hub judged {what} at {row.head_sha[:12]} and read its merge on GitHub:", *lines]),
    )


async def settle(engine: AsyncEngine, app: GitHubApp | None) -> dict:
    """Read the merge of each passed check not read yet; how many were recorded (``merged``) and how many of them
    open a notice (``notified``). Nothing without the workers' App."""
    if app is None:
        return {}
    m = tables.merge_checks
    since = datetime.now(UTC) - timedelta(days=WINDOW_DAYS)
    async with engine.begin() as conn:
        query = select(m).where(m.c.state == checks.PASSED, m.c.created_at >= since).order_by(m.c.id)
        rows = [row for row in await conn.execute(query) if "merged" not in (row.verdict or {})]
        projects = {}
        for row in rows:
            if row.project_id not in projects:
                projects[row.project_id] = await checks.queue._load(conn, row.project_id)
    outcomes: Counter = Counter()
    for row in rows:
        project = projects[row.project_id]
        facts = project.repos.get(row.repo)
        target = github_target(facts.origin) if facts is not None else None
        if target is None:
            continue
        read = await _read(app, target, row)
        if read is None:
            continue
        pull, status = read
        record = merged_record(row, pull, status, datetime.now(UTC))
        async with engine.begin() as conn:
            locked = await checks.row_of(conn, row.id, lock=True)
            if locked is None or "merged" in (locked.verdict or {}):
                continue
            await checks.set_row(conn, row.id, verdict={**(locked.verdict or {}), "merged": record})
            outcomes["merged"] += 1
            if record["problems"]:
                await _tell(conn, project, locked, record)
                outcomes["notified"] += 1
    return dict(outcomes)
