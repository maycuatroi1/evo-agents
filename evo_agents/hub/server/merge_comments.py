"""The hub's comment on a pull request once its merge check settled, so the people who read the pull request on
github.com see what the Judges found without opening the hub.

``post`` runs on each pass of the job hub.merge_queue, after ``merge_check.advance``, from ``merge_ancestry.settle``. Each check that passed or failed
within WINDOW_DAYS (and after FIRST, when the comments came in) and whose verdict names no comment for its head (``verdict.commented``) gets one comment on its pull
request, written with the workers' GitHub App and a token of pull_requests write alone
(``pulls.SERVER_PERMISSIONS["comment"]``); the check's verdict then records it (``commented``: the head, the state,
the comment's id and URL, when). A check judged again at a new head comments again once that settles. GitHub failing
leaves the check for the next pass; GitHub refusing is recorded with why, and not asked again for that head.

The comment (``comment_body``) says the state and the reason, and for each Judge: its run on the hub, its model, its
verdict and reasons, the verdict of each dimension, the hidden checks by number with exit code and time (never what
they run), the plan's verify commands with exit code and time, the signs of score hacking it weighed, and the open
findings. The reasons of the safety, auth and security dimensions, and the findings of the security pass, stay on the hub:
the comment names their verdict and count alone, since a pull request may be public.
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from evo_agents.hub import findings, merge_gate, tables
from evo_agents.hub.server import merge_check as checks
from evo_agents.hub.server import pulls
from evo_agents.hub.server.github import GitHubRefused, GitHubUnavailable
from evo_agents.hub.server.github_app import GitHubApp
from evo_agents.hub.server.protection import github_target

log = logging.getLogger(__name__)

WINDOW_DAYS = 2  # a check settled earlier than this gets no comment
FIRST = datetime(2026, 11, 5, 7, 30, tzinfo=UTC)  # nor one settled before the hub commented at all
MARK = "<!-- evo-agents:merge-check -->"  # the first line of the hub's comments
PRIVATE_DIMENSIONS = frozenset({"safety", "auth", merge_gate.SECURITY})  # their reasons stay on the hub
MAX_REASON_CHARS = 1500
MAX_COMMAND_CHARS = 120
MAX_FINDINGS = 10
MAX_BODY_CHARS = 60_000  # GitHub takes 65536 in a comment


def _clip(value: object, most: int) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= most else text[: most - 3] + "..."


def _seconds(ms) -> str:
    return f"{ms / 1000:.1f} s" if isinstance(ms, int) and not isinstance(ms, bool) else "time not told"


def _run_link(public_url: str | None, project: str, run_id) -> str:
    if not isinstance(run_id, int):
        return "a judge run"
    return f"[run #{run_id}]({public_url}/p/{project}/runs/{run_id})" if public_url else f"run #{run_id}"


def _checks_table(entry: Mapping) -> list[str]:
    rows = [
        f"| Hidden check {item.get('index')} | exit {item.get('exit_code')} | {_seconds(item.get('duration_ms'))} |"
        for item in entry.get("hidden") or []
        if isinstance(item, Mapping)
    ]
    rows += [
        f"| Verify `{_clip(item.get('command'), MAX_COMMAND_CHARS)}` | exit {item.get('exit_code')} | "
        f"{_seconds(item.get('duration_ms'))} |"
        for item in entry.get("verify") or []
        if isinstance(item, Mapping)
    ]
    return ["| Check | Result | Time |", "|---|---|---|", *rows] if rows else []


def _dimensions(entry: Mapping) -> list[str]:
    lines = []
    for name, said in sorted((entry.get("dimensions") or {}).items()):
        if not isinstance(said, Mapping):
            continue
        verdict = said.get("verdict") or "no verdict"
        reason = "" if name in PRIVATE_DIMENSIONS else _clip(said.get("reason") or "", MAX_REASON_CHARS)
        lines.append(f"- **{name}**: {verdict}" + (f": {reason}" if reason else ""))
    return lines


def _judge(entry: Mapping, which: str, public_url: str | None, project: str) -> list[str]:
    agent = entry.get("agent") or "no verdict"
    lines = [f"### {which}: {'passed' if entry.get('passed') else 'failed'}", ""]
    lines.append(
        f"{_run_link(public_url, project, entry.get('run_id'))}, model `{entry.get('model') or 'unknown'}`, "
        f"its agent's verdict: {agent}."
    )
    reasons = _clip(entry.get("reasons") or "", MAX_REASON_CHARS)
    if reasons:
        lines += ["", f"> {reasons}"]
    failures = [str(item) for item in entry.get("failures") or []]
    if failures:
        lines += ["", "Why it did not pass:", *(f"- {_clip(item, MAX_REASON_CHARS)}" for item in failures)]
    table = _checks_table(entry)
    if table:
        lines += ["", *table]
    signs = sorted({str(item.get("kind")) for item in entry.get("signs") or [] if isinstance(item, Mapping)})
    if signs:
        lines += ["", f"Signs of score hacking the Judge weighed against the plan: {', '.join(signs)}."]
    dimensions = _dimensions(entry)
    if dimensions:
        lines += ["", "Dimensions:", *dimensions]
    security = entry.get("security")
    if isinstance(security, Mapping):
        lines += [
            "",
            f"Security pass: {security.get('verdict') or 'no verdict'}, {security.get('candidates') or 0} candidate(s); "
            "its findings are on the hub.",
        ]
    return lines


def comment_body(row, project: str, public_url: str | None) -> str:
    """The comment on the pull request of merge check ``row`` once it settled (the module's docstring)."""
    verdict = row.verdict if isinstance(row.verdict, Mapping) else {}
    state = "passed" if row.state == checks.PASSED else "failed"
    lines = [MARK, f"## evo-agents merge check: {state}", ""]
    lines.append(f"At head `{row.head_sha[:12]}`" + (f": {_clip(row.reason, MAX_REASON_CHARS)}" if row.reason else "."))
    lines.append("The checks ran on this head merged with the current `" + row.base_branch + "`.")
    for key, which in (("first", "Judge"), ("second", "Second Judge")):
        entry = verdict.get(key)
        if isinstance(entry, Mapping) and entry.get("head_sha") in (None, row.head_sha):
            lines += ["", *_judge(entry, which, public_url, project)]
    names = {*merge_gate.gate_names(False), *merge_gate.gate_names(True)}
    open_ones = [
        item
        for item in findings.open_findings(findings.ledger_of(verdict), names)
        if item.get("dimension") not in PRIVATE_DIMENSIONS
    ]
    if open_ones:
        lines += ["", "### Open findings"]
        for item in open_ones[:MAX_FINDINGS]:
            where = f" `{item['path']}`" + (f":{item['line']}" if item.get("line") else "") if item.get("path") else ""
            lines.append(
                f"- {item.get('id')} ({item.get('dimension')}){where}: {_clip(item.get('text'), MAX_REASON_CHARS)}"
            )
        if len(open_ones) > MAX_FINDINGS:
            lines.append(f"- and {len(open_ones) - MAX_FINDINGS} more on the hub.")
    if row.state == checks.PASSED:
        lines += [
            "",
            f"The verdict holds for this head alone: `gh pr merge {row.number} --match-head-commit {row.head_sha}`.",
        ]
    else:
        lines += ["", "A push of a fix asks for the check again at the new head."]
    body = "\n".join(lines) + "\n"
    return body if len(body) <= MAX_BODY_CHARS else body[: MAX_BODY_CHARS - 40] + "\n\n(cut; the rest is on the hub)\n"


async def post(engine: AsyncEngine, app: GitHubApp | None, public_url: str | None) -> dict:
    """Comment on the pull request of each check settled since WINDOW_DAYS with no comment for its head; how many were
    posted (``commented``) and refused (``comment_refused``). Nothing without the workers' App."""
    if app is None:
        return {}
    m = tables.merge_checks
    since = max(datetime.now(UTC) - timedelta(days=WINDOW_DAYS), FIRST)
    base = (public_url or "").rstrip("/") or None
    async with engine.begin() as conn:
        query = select(m).where(m.c.state.in_((checks.PASSED, checks.FAILED)), m.c.updated_at >= since).order_by(m.c.id)
        rows = [row for row in await conn.execute(query) if _due(row)]
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
        record = await _write(app, target, row, comment_body(row, project.name, base))
        if record is None:
            continue
        async with engine.begin() as conn:
            locked = await checks.row_of(conn, row.id, lock=True)
            if locked is None or locked.head_sha != row.head_sha or locked.state != row.state or not _due(locked):
                continue
            await checks.set_row(conn, row.id, verdict={**(locked.verdict or {}), "commented": record})
        outcomes["commented" if record.get("id") else "comment_refused"] += 1
    return dict(outcomes)


def _due(row) -> bool:
    """Whether settled check ``row`` has no comment, or refusal, recorded for its head yet."""
    told = (row.verdict or {}).get("commented") if isinstance(row.verdict, Mapping) else None
    return not (isinstance(told, Mapping) and told.get("head") == row.head_sha and told.get("state") == row.state)


async def _write(app: GitHubApp, target, row, body: str) -> dict | None:
    """The record of the comment written on the pull request of ``row``, or of GitHub's refusal; None when GitHub did
    not answer, for the next pass."""
    where = {"check_id": row.id, "repo": row.repo, "pr": row.number}
    record = {"head": row.head_sha, "state": row.state, "at": datetime.now(UTC).isoformat()}

    async def write(token: str):
        return await pulls.comment_pull(app, token, target.owner, target.name, row.number, body)

    try:
        written = await pulls.with_token(app, target.owner, target.name, "comment", write)
    except GitHubRefused as exc:
        log.warning("the hub's comment on a pull request was refused", extra={**where, "why": str(exc)})
        return {**record, "id": None, "refused": _clip(str(exc), MAX_REASON_CHARS)}
    except GitHubUnavailable as exc:
        log.info("GitHub did not answer the hub's comment; the next pass writes it", extra={**where, "why": str(exc)})
        return None
    return {**record, **written}
