"""The draft pull requests the hub opens for the branch of a chat run (``chat_agents.pushed``) or a task run
(``task_dispatch.pushed``), from the push that opens one to the end that closes it. Nothing here merges.

Opening (``open_for``, with one token of the workers' App, ``pulls.SERVER_PERMISSIONS["open"]``). The base is the one
the run's task names, else the one the worker names (the branch of origin the run's branch was made from,
``worker.runner.pull_base``), else, from a worker that names none, whichever of the project's default branch and the
repo's default branch on GitHub the head has fewer commits beyond (``pick_base``). Before it opens one, the hub
compares base...head on GitHub (``pulls.compare_paths``): a branch with no commit the base lacks, or whose changes are
only paths a run makes beside the work (``runs.WORKER_MADE``: a ``.venv``), gets no pull request (``Made.nothing`` says
why), and the one open of it already is closed with a comment that says so. One open of the branch into another base
than the one the worker or the task names is moved to it. The run keeps each pull request it pushed to (runs.pulls,
``kept``): repo, number, url, branch, base, head and state ``open``.

Settling (``settle_pass``, in each pass of the reaper). The drafts kept open on runs queued in the last WATCH_DAYS, the
newest run's copy of each, are read at most every CHECK_MINUTES, SETTLE_BATCH a pass, the one read longest ago first,
GitHub outside any transaction:

1. GitHub says it is merged or closed: it is kept so (state merged or closed), nothing else.
2. Its head is in its base (GitHub compares base...head as behind or identical): closed, with a comment naming the
   merged pull request that holds its head (GET .../commits/{sha}/pulls), else saying its commits are in the base.
3. It changes no file, or only paths a run makes beside the work: closed, with a comment saying so.
4. Once the trace of its run ended (``settled``: no run of the trace, or of the run's session for a run of no trace,
   still to end, the last of them ended SETTLE_MINUTES ago), a merged pull request of the same repo into the same base
   that a message of those runs links (``linked``) and that changes a path the draft changes replaced it: closed, with a
   comment naming it. Else the hub cannot tell whether the work still stands, and opens the thread of rule draft to the
   Orchestrator of the run's agent (``orchestrator.on_draft``), once; the draft keeps ``asked_at`` and is read again
   for 1 to 3 only. An agent no Orchestrator orchestrates gets no thread, its draft left as it is.

A close GitHub refuses leaves the draft open, read again later, its comment not written twice (``commented``).
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import runs, tables
from evo_agents.hub.server import pulls
from evo_agents.hub.server.github import GitHubRefused, GitHubUnavailable

log = logging.getLogger(__name__)

KINDS = ("chat", "task")  # the kinds of run whose branch gets a draft pull request
WATCH_DAYS = 30  # a draft kept on a run queued longer ago is read no more
CHECK_MINUTES = 10  # a draft is read at most this often
SETTLE_MINUTES = 30  # a trace whose last run ended this long ago, none still to end, has ended
SETTLE_BATCH = 20  # drafts one pass reads
MAX_LINKED = 5  # pull requests of a trace's messages one draft reads
MAX_PULLS = 50  # the pull requests a run keeps (runs.pulls), one for each repo of its project at most
SHOWN_PATHS = 5  # paths a reason names
DRAFT_TITLE = "[Draft]"  # chat_runs.DRAFT_TITLE: the title of a pull request on a repo that takes no draft
PULL_URL = re.compile(r"^https://github\.com/(?P<owner>[^/\s]+)/(?P<repo>[^/\s]+)/pull/(?P<number>\d+)$")
IN_BASE = ("behind", "identical")  # how GitHub compares base...head when every commit of head is in base
# (connection, the draft's run, the lines of the thread, its key): opens the thread of rule draft (``orchestrator.on_draft``)
Ask = Callable[[AsyncConnection, int, list[str], str], Awaitable[object]]


@dataclass(frozen=True)
class Made:
    """What ``open_for`` did with the push of a branch: the pull request opened (``opened``) or found, the base, and
    when it opened none, why (``nothing``) and the one of the branch it closed for that (``closed``)."""

    pull: dict | None
    opened: bool
    base: str | None
    nothing: str | None = None
    closed: dict | None = None


@dataclass(frozen=True)
class Draft:
    """A draft kept on a run: the run, its project, kind and branch, and the pull request (repo of the project, owner
    and name on GitHub, number, base, url) with what the hub noted of it."""

    run_id: int
    project_id: int
    branch: str
    repo: str
    owner: str
    name: str
    number: int
    base: str | None
    url: str
    noted: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Verdict:
    """What GitHub says of a draft: ``merged``, ``closed``, ``close`` (with the comment and the pull request that
    replaced it) or ``open`` (its base, head and paths)."""

    state: str
    comment: str | None = None
    replaced_by: int | None = None
    base: str | None = None
    head: str | None = None
    paths: tuple[str, ...] = ()


# Opening


def nothing_to_open(compared: dict | None, branch: str, base: str) -> str | None:
    """Why the branch, as GitHub compared it with ``base`` (``pulls.compare_paths``), needs no pull request: it has no
    commit the base lacks, changes no file, or changes only paths of runs.WORKER_MADE; None when it does, or GitHub
    could not say."""
    if compared is None:
        return None
    if compared.get("ahead_by") == 0 or compared.get("status") in IN_BASE:
        return f"{branch} has no commit that {base} lacks"
    paths = compared.get("paths")
    if paths is None or compared.get("truncated"):
        return None
    if not paths:
        return f"{branch} changes no file against {base}"
    if all(runs.worker_made(path) for path in paths):
        shown = ", ".join(paths[:SHOWN_PATHS]) + (
            f" and {len(paths) - SHOWN_PATHS} more" if len(paths) > SHOWN_PATHS else ""
        )
        return f"{branch} changes only {shown}, which a run makes beside the work and never commits"
    return None


async def compared(app, token: str, owner: str, name: str, base: str, head: str) -> dict | None:
    """``pulls.compare_paths`` of base...head, None when GitHub refuses it: a comparison never stops a pull request."""
    try:
        return await pulls.compare_paths(app, token, owner, name, base, head)
    except GitHubRefused as exc:
        log.info("comparison refused", extra={"repo": f"{owner}/{name}", "base": base, "why": str(exc)})
        return None


async def pick_base(app, token: str, owner: str, name: str, head: str, defaults: tuple[str | None, ...]) -> str:
    """The base of a push whose worker names none: of ``defaults`` (the project's default branch) and the repo's
    default branch on GitHub, the one ``head`` has fewer commits beyond, ``defaults`` first on a tie; the repo's default
    branch when GitHub compares none."""
    default = (await pulls.repository(app, token, owner, name)).get("default_branch")
    found = list(dict.fromkeys(item for item in (*defaults, default) if isinstance(item, str) and item))
    if not found:
        raise GitHubRefused(f"GitHub names no default branch of {owner}/{name}")
    best: tuple[tuple[int, int], str] | None = None
    for index, branch in enumerate(found if len(found) > 1 else ()):
        found_one = await compared(app, token, owner, name, branch, head)
        ahead = found_one.get("ahead_by") if found_one else None
        if isinstance(ahead, int) and (best is None or (ahead, index) < best[0]):
            best = ((ahead, index), branch)
    if best is not None:
        return best[1]
    return default if isinstance(default, str) and default else found[0]


async def _opened(app, token: str, owner: str, name: str, pull: dict) -> dict:
    """``pull`` (branch, base, title, body) opened as a draft, or titled DRAFT_TITLE on a repo that takes none."""
    title = pull.pop("title")
    try:
        return await pulls.open_pull(app, token, owner, name, title=title, draft=True, **pull)
    except pulls.Refused as exc:
        if not pulls.drafts_refused(exc.errors):
            raise
        log.info("no draft pull request on this repo", extra={"repo": f"{owner}/{name}", "why": str(exc)})
    return await pulls.open_pull(app, token, owner, name, title=f"{DRAFT_TITLE} {title}"[:240], draft=False, **pull)


async def close_pull(app, token: str, owner: str, name: str, number: int, comment: str | None) -> None:
    """Comment ``comment`` on pull request ``number`` (unless None), then close it."""
    if comment:
        await pulls.comment_pull(app, token, owner, name, number, comment)
    await pulls.update_pull(app, token, owner, name, number, state="closed")


async def open_for(
    app,
    target: tuple[str, str],
    branch: str,
    head: str,
    title: str,
    text: str,
    *,
    base: str | None = None,
    defaults: tuple[str | None, ...] = (),
) -> Made:
    """The draft pull request of ``branch`` at ``head`` on ``target`` (owner, name) into ``base``, else a base
    ``pick_base`` picks from ``defaults``: found or opened, or none when the branch has nothing for one, the one open of
    it then closed; see the module's docstring."""
    owner, name = target

    async def made(token: str) -> Made:
        into = base or await pick_base(app, token, owner, name, head, defaults)
        found = await pulls.find_pull(app, token, owner, name, branch)
        why = nothing_to_open(await compared(app, token, owner, name, into, head), branch, into)
        if why is not None:
            if found is not None:
                said = f"Closed by the hub: {why}, so this draft has nothing to merge."
                await close_pull(app, token, owner, name, found["number"], said)
            return Made(None, False, into, nothing=why, closed=found)
        if found is not None:
            if base is not None and (found.get("base") or {}).get("ref") != base:
                found = await pulls.update_pull(app, token, owner, name, found["number"], base=base)
            return Made(found, False, into)
        return Made(
            await _opened(app, token, owner, name, {"branch": branch, "base": into, "title": title, "body": text}),
            True,
            into,
        )

    return await pulls.with_token(app, owner, name, "open", made)


def kept(repo: str, branch: str, head: str, made: Made) -> dict | None:
    """What the run keeps of the pull request ``made`` found or opened (runs.pulls); None when there is none."""
    pull = made.pull
    if pull is None or type(pull.get("number")) is not int:
        return None
    url = pull.get("html_url") if isinstance(pull.get("html_url"), str) else None
    return {
        "repo": repo,
        "url": url,
        "number": pull["number"],
        "branch": branch,
        "base": made.base,
        "head": head,
        "state": "open",
    }


async def keep(conn: AsyncConnection, run_id: int, entry: dict) -> None:
    """Keep ``entry`` in runs.pulls of run ``run_id``, in place of the one of its repo."""
    r = tables.runs
    found = (await conn.execute(select(r.c.pulls).where(r.c.id == run_id).with_for_update())).scalar_one() or []
    found = [item for item in found if isinstance(item, dict) and item.get("repo") != entry["repo"]] + [entry]
    await conn.execute(update(r).where(r.c.id == run_id).values(pulls=found[-MAX_PULLS:]))


async def mark(conn: AsyncConnection, project_id: int, repo: str, number: int, **noted) -> None:
    """Note ``noted`` (a state, when it was read, ...) on every copy of pull request ``number`` of ``repo`` that the
    runs of project ``project_id`` keep."""
    r = tables.runs
    held = r.c.pulls.contains([{"repo": repo, "number": number}])
    rows = (await conn.execute(select(r.c.id, r.c.pulls).where(r.c.project_id == project_id, held))).all()
    for row in rows:
        items = [
            {**item, **noted}
            if isinstance(item, dict) and (item.get("repo"), item.get("number")) == (repo, number)
            else item
            for item in row.pulls or []
        ]
        await conn.execute(update(r).where(r.c.id == row.id).values(pulls=items))


# Settling


def _when(value) -> datetime | None:
    try:
        return datetime.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        return None


def _draft(row, item) -> Draft | None:
    match = PULL_URL.match(item.get("url") or "") if isinstance(item, dict) else None
    if match is None or item.get("state") != "open" or type(item.get("number")) is not int:
        return None
    base = item.get("base") if isinstance(item.get("base"), str) else None
    repo = item.get("repo")
    if not isinstance(repo, str):
        return None
    owner, name = match["owner"], match["repo"]
    return Draft(row.id, row.project_id, row.branch or "", repo, owner, name, item["number"], base, item["url"], item)


async def watched(conn: AsyncConnection, batch: int = SETTLE_BATCH, now: datetime | None = None) -> list[Draft]:
    """The drafts a pass reads: the newest run's copy of each draft kept open, not read in CHECK_MINUTES, on a run of
    KINDS queued in WATCH_DAYS; the one read longest ago first, ``batch`` of them."""
    r = tables.runs
    now = now or datetime.now(UTC)
    query = select(r.c.id, r.c.project_id, r.c.branch, r.c.pulls).where(
        r.c.kind.in_(KINDS),
        r.c.pulls.contains([{"state": "open"}]),
        r.c.queued_at > func.now() - timedelta(days=WATCH_DAYS),
    )
    seen: set[tuple[int, str, int]] = set()
    found: list[tuple[datetime, Draft]] = []
    for row in await conn.execute(query.order_by(r.c.id.desc())):
        for item in row.pulls or []:
            draft = _draft(row, item)
            if draft is None or (draft.project_id, draft.repo, draft.number) in seen:
                continue
            seen.add((draft.project_id, draft.repo, draft.number))
            read = _when(item.get("checked_at"))
            if read is None or now - read >= timedelta(minutes=CHECK_MINUTES):
                found.append((read or datetime.min.replace(tzinfo=UTC), draft))
    return [draft for _, draft in sorted(found, key=lambda pair: pair[0])[:batch]]


async def _holder(app, token: str, draft: Draft, head: str) -> int | None:
    """The merged pull request other than the draft that holds ``head``, which GitHub lists of the commit."""
    for item in await pulls.commit_pulls(app, token, draft.owner, draft.name, head):
        if item.get("number") != draft.number and item.get("merged_at"):
            number = item.get("number")
            return number if type(number) is int else None
    return None


async def read_draft(app, token: str, draft: Draft) -> Verdict:
    """What GitHub says of ``draft`` now (steps 1 to 3 of the module's docstring)."""
    pull = await pulls.pull(app, token, draft.owner, draft.name, draft.number)
    if pull.get("merged") is True:
        return Verdict("merged")
    if pull.get("state") != "open":
        return Verdict("closed")
    base = (pull.get("base") or {}).get("ref") or draft.base or ""
    head = (pull.get("head") or {}).get("sha") or ""
    branch = (pull.get("head") or {}).get("ref") or draft.branch
    found = await compared(app, token, draft.owner, draft.name, base, head)
    if found is not None and found.get("status") in IN_BASE:
        holder = await _holder(app, token, draft, head)
        through = f", merged through #{holder}" if holder is not None else ""
        said = f"Closed by the hub: the head of this draft ({head[:12]}) is in {base} already{through}."
        return Verdict("close", said, holder)
    why = nothing_to_open(found, branch, base)
    if why is not None:
        return Verdict("close", f"Closed by the hub: {why}, so this draft has nothing to merge.")
    paths = tuple(found.get("paths") or ()) if found else ()
    return Verdict("open", base=base, head=head, paths=paths)


async def _trace_runs(conn: AsyncConnection, draft: Draft):
    """The ids of the runs of the trace of the draft's run, or of its session (its project and branch) for a run of
    no trace, as a subquery."""
    s, r = tables.system_spans, tables.runs
    trace = await conn.scalar(select(s.c.trace_id).where(s.c.run_id == draft.run_id).limit(1))
    if trace is not None:
        return select(s.c.run_id).where(s.c.trace_id == trace, s.c.run_id.is_not(None))
    return select(r.c.id).where(r.c.project_id == draft.project_id, r.c.branch == draft.branch)


async def settled(conn: AsyncConnection, draft: Draft) -> bool:
    """Whether the trace of the draft's run ended: none of its runs is still to end, and the last ended SETTLE_MINUTES
    ago."""
    r = tables.runs
    ids = await _trace_runs(conn, draft)
    going = await conn.scalar(select(func.count()).where(r.c.id.in_(ids), r.c.state.not_in(runs.TERMINAL_STATES)))
    if going:
        return False
    since = func.now() - timedelta(minutes=SETTLE_MINUTES)
    last = select(func.max(func.coalesce(r.c.finished_at, r.c.queued_at))).where(r.c.id.in_(ids)).scalar_subquery()
    return bool(await conn.scalar(select(last < since)))


async def linked(conn: AsyncConnection, draft: Draft) -> list[int]:
    """The pull requests of the draft's repo, other than it, that the messages and evidence of its trace's runs link
    (a URL on github.com, or ``<repo>#<n>``), first seen first, MAX_LINKED at most."""
    m, r = tables.chat_messages, tables.runs
    ids = await _trace_runs(conn, draft)
    texts = list((await conn.execute(select(m.c.body).where(m.c.run_id.in_(ids)).order_by(m.c.id))).scalars())
    texts += [text for text in (await conn.execute(select(r.c.evidence).where(r.c.id.in_(ids)))).scalars() if text]
    owner, name = re.escape(draft.owner), re.escape(draft.name)
    pattern = re.compile(rf"github\.com/{owner}/{name}/pull/(\d+)|(?<![\w-]){name}#(\d+)", re.IGNORECASE)
    found: list[int] = []
    for text in texts:
        for url_number, short_number in pattern.findall(text):
            number = int(url_number or short_number)
            if number != draft.number and number not in found:
                found.append(number)
    return found[:MAX_LINKED]


async def replacement(app, token: str, draft: Draft, verdict: Verdict, numbers: list[int]) -> dict | None:
    """Of the pull requests ``numbers`` the trace links, the first merged into the draft's base that changes a path the
    draft changes."""
    if not verdict.paths:
        return None
    ours = set(verdict.paths)
    for number in numbers:
        try:
            other = await pulls.pull(app, token, draft.owner, draft.name, number)
        except pulls.Refused:
            continue
        if other.get("merged") is not True or (other.get("base") or {}).get("ref") != verdict.base:
            continue
        files = await pulls.pull_files(app, token, draft.owner, draft.name, number)
        if ours & {item.get("filename") for item in files}:
            return other
    return None


def _lines(draft: Draft, verdict: Verdict, numbers: list[int]) -> list[str]:
    linked_text = ", ".join(f"#{number}" for number in numbers) or "none"
    return [
        f"Draft: {draft.url} of {draft.repo} from {draft.branch} into {verdict.base}, head {(verdict.head or '')[:12]}, "
        f"{len(verdict.paths)} file(s) changed; still open.",
        f"Pull requests of {draft.repo} the trace links: {linked_text}; none merged into {verdict.base} with a file "
        "of this draft.",
        "Close it with a comment naming the pull request that holds its work, mark it ready for review when its work "
        "still stands, or give the work to the agent with `evo-agents worker triage reassign`.",
    ]


async def _closing(app, token: str, draft: Draft, comment: str, replaced_by: int | None, outcome: str):
    """Close the draft with ``comment``, written once (``commented``): (``outcome``, what to note) when GitHub closed
    it, else (open, what to note)."""
    try:
        if not draft.noted.get("commented"):
            await pulls.comment_pull(app, token, draft.owner, draft.name, draft.number, comment)
    except GitHubRefused as exc:
        log.warning("draft pull request not commented on", extra={"pull": draft.url, "why": str(exc)})
        return "open", {}
    try:
        await pulls.update_pull(app, token, draft.owner, draft.name, draft.number, state="closed")
    except GitHubRefused as exc:
        log.warning("draft pull request not closed", extra={"pull": draft.url, "why": str(exc)})
        return "open", {"commented": True}
    return outcome, {"state": "closed", "replaced_by": replaced_by, "commented": True}


async def _decide(engine: AsyncEngine, app, token: str, draft: Draft, ask: Ask | None, stamp: str):
    """(what happened, what to note) of ``draft``, with a token of the workers' App on its repo."""
    verdict = await read_draft(app, token, draft)
    if verdict.state in ("merged", "closed"):
        return verdict.state, {"state": verdict.state}
    if verdict.state == "close":
        return await _closing(app, token, draft, verdict.comment or "Closed by the hub.", verdict.replaced_by, "closed")
    async with engine.connect() as conn:
        if draft.noted.get("asked_at") or not await settled(conn, draft):
            return "waiting", {}
        numbers = await linked(conn, draft)
    other = await replacement(app, token, draft, verdict, numbers)
    if other is not None:
        said = (
            f"Closed by the hub: the work of this draft went into {verdict.base} through #{other['number']}, which "
            "the agents' trace links and which changes a file this draft changes."
        )
        return await _closing(app, token, draft, said, other["number"], "replaced")
    if ask is not None:
        async with engine.begin() as conn:
            await ask(
                conn,
                draft.run_id,
                _lines(draft, verdict, numbers),
                f"draft:{draft.project_id}:{draft.repo}#{draft.number}",
            )
    return "asked", {"asked_at": stamp}


async def settle_one(engine: AsyncEngine, app, draft: Draft, ask: Ask | None = None) -> str:
    """Read ``draft`` and act on it as the module's docstring says, ``ask`` opening the thread of rule draft
    (``orchestrator.on_draft``); what happened: merged, closed, replaced, asked, open (GitHub refused the close),
    waiting (its trace has not ended, or its Orchestrator was asked) or unread."""
    stamp = datetime.now(UTC).isoformat()

    async def act(token: str):
        return await _decide(engine, app, token, draft, ask, stamp)

    try:
        what, noted = await pulls.with_token(app, draft.owner, draft.name, "settle", act)
    except (GitHubRefused, GitHubUnavailable) as exc:
        log.info("draft pull request not read", extra={"pull": draft.url, "why": str(exc)})
        what, noted = "unread", {}
    async with engine.begin() as conn:
        await mark(conn, draft.project_id, draft.repo, draft.number, checked_at=stamp, **noted)
    return what


async def settle_pass(engine: AsyncEngine, app, ask: Ask | None = None, batch: int = SETTLE_BATCH) -> Counter[str]:
    """One pass, of the reaper: each draft ``watched`` gives, read and acted on (``settle_one``); how many of each
    outcome. Nothing without the workers' App."""
    done: Counter[str] = Counter()
    if app is None:
        return done
    async with engine.connect() as conn:
        drafts = await watched(conn, batch)
    for draft in drafts:
        try:
            done[await settle_one(engine, app, draft, ask)] += 1
        except Exception as exc:  # one draft never stops the reaper's pass
            error = f"{type(exc).__name__}: {exc}"
            log.warning("draft pull request not settled", extra={"pull": draft.url, "error": error})
            done["error"] += 1
    if any(done[key] for key in ("closed", "replaced", "asked", "error")):
        log.info("draft pull requests", extra=dict(done))
    return done
