"""How the hub lands a release run that is done (``evo_agents.hub.release_runs``): the pull request of its branch
release/<version> into the repo's default branch, merged with a merge commit, and the tag ``<prefix><version>`` made
on that merge commit, all with the hub's own GitHub App (evo-agents-workers). A run's agent and its worker never push
the default branch or a tag (``gitops.check_push``, kind release); the hub is the only one that merges and tags, so
the version on a tag is always the one the bump wrote.

``land_releases`` is one pass, made by the job ``hub.release_schedules`` after its checks (one pass at a time: the job
holds its lock): each release run that is done, pushed its branch, waits to land and whose cycle is open gets
``land``. The cycle's owner must still hold the admin role on the project (``owner_is_admin``), else nothing of the run
lands. One token of the App's installation on the repo, for that repo alone with contents and pull_requests write
(``pulls.with_token``, use ``merge``), serves the whole landing and is revoked after it. Before the pull request
merges, the hub reads the version VERSION_FILE names on the default branch's tip and on the run's commit and the
release tags (``release_github.read_refs``), and refuses (``release_runs.land_problem``) a version that is not
greater, changes the major, is neither the next patch nor the next minor, whose tag exists, or that the branch does not
name. Then: the pull request opened (or the one open found into the default branch), its number kept, its body naming
the agent's Decision line with nothing GitHub acts on (``release_runs.inert``); its files read, and a change that
shows a sign of test hacking (``release_ci.release_signs``: a test removed, an assertion removed or loosened, a skip
added but for importorskip and a skip on one platform with its reason) is never merged; merged at the run's commit
alone (``pulls.merge_pull``: GitHub refuses once the branch moved), the merge commit kept; and the tag made on it
(``pulls.create_ref``), or found there already. Each part is kept as it is done, so a pass that GitHub failed goes on
from there: a pull request merged already gives its merge commit, one closed without a merge is refused. A pull request
the hub did not merge itself is the run's only when its head is the run's branch at the commit the run pushed and its
base the default branch (``pull_problem``), and its files show no sign of test hacking either: else no tag is made.

A refusal (``land_problem``, a pull request closed, GitHub refusing a call, a tag elsewhere) marks the run refused and
stops the cycle with the reason (``release_cycles.stop_cycle``, cause refused, or hacking for signs of test hacking,
whose pull request stays open); GitHub failing marks a try, and LAND_TRIES in a row mark it failed and stop the cycle
too (cause github). A run tagged is audited as release.tag, with the cycle's owner as actor and no token; its cycle
stays open, and follows the tag (a row of release_tags) for the hub to watch its CI (``release_watch``).
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass

from fastapi import HTTPException
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as upsert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import judge, release_ci, release_runs, releases, tables
from evo_agents.hub.access import has_role
from evo_agents.hub.credentials import github_repo
from evo_agents.hub.server import audit, autonomy_gates, pull_trailers, pulls
from evo_agents.hub.server.github import GitHubRefused, GitHubUnavailable
from evo_agents.hub.server.github_app import GitHubApp
from evo_agents.hub.server.projects import project_access
from evo_agents.hub.server.release_cycles import stop_cycle
from evo_agents.hub.server.release_github import ReadFailed, read_refs
from evo_agents.hub.server.run_state import write_event
from evo_agents.hub.server.security import MACHINE, Principal

log = logging.getLogger(__name__)

TAG = "release.tag"  # the hub tagged a release: "<project> cycle:<id> run:<id> tag=<tag> commit=<sha>"
TAGS = "refs/tags/"


class Refusal(Exception):
    """Why the hub does not land a run, in words safe to show."""

    cause = "refused"


class Hacking(Refusal):
    """The run's change shows signs of test hacking."""

    cause = "hacking"


@dataclass(frozen=True)
class Landing:
    """A release run waiting to land, with its cycle, schedule, project and repo."""

    run_id: int
    cycle_id: int
    project_id: int
    project: str
    owner_id: int
    owner: str
    repo: str
    origin: str | None
    tag_pattern: str
    workflow: str
    target_ref: str
    target_commit: str | None
    base_tag: str | None
    version: str
    branch: str
    head_commit: str
    decision: str | None
    pull_number: int | None
    merge_commit: str | None

    @property
    def tag(self) -> str:
        return releases.tag_prefix(self.tag_pattern) + self.version


def _waiting():
    """The release runs that wait to land: done, with their branch, not landed, of an open cycle."""
    rr, r, c, s, p, pr, u = (
        tables.release_runs,
        tables.runs,
        tables.release_cycles,
        tables.release_schedules,
        tables.projects,
        tables.project_repos,
        tables.users,
    )
    return (
        select(
            rr.c.run_id,
            rr.c.cycle_id,
            c.c.project_id,
            p.c.name.label("project"),
            c.c.owner_id,
            u.c.login.label("owner"),
            s.c.repo,
            pr.c.origin,
            s.c.tag_pattern,
            s.c.workflow,
            c.c.target_ref,
            c.c.target_commit,
            c.c.base_tag,
            rr.c.version,
            rr.c.branch,
            rr.c.head_commit,
            rr.c.decision,
            rr.c.pull_number,
            rr.c.merge_commit,
        )
        .join_from(rr, r, r.c.id == rr.c.run_id)
        .join(c, c.c.id == rr.c.cycle_id)
        .join(s, s.c.id == c.c.schedule_id)
        .join(p, p.c.id == c.c.project_id)
        .join(u, u.c.id == c.c.owner_id)
        .outerjoin(pr, (pr.c.project_id == c.c.project_id) & (pr.c.name == s.c.repo))
        .where(r.c.state == "done", rr.c.branch.is_not(None), rr.c.land_state.is_(None), c.c.state == "open")
        .order_by(rr.c.run_id)
    )


async def owner_is_admin(conn: AsyncConnection, owner_id: int, owner: str, project: str) -> bool:
    """Whether ``owner`` (user ``owner_id``), who owns a release schedule, holds the admin role on ``project``, as a
    request of theirs would find."""
    member = Principal(user_id=owner_id, login=owner, admin=False, token_id=0, kind=MACHINE, token_hash="")
    try:
        access = await project_access(conn, member, project)
    except HTTPException:
        return False
    return has_role(access.role, "admin")


async def _save(engine: AsyncEngine, run_id: int, **values) -> None:
    rr = tables.release_runs
    async with engine.begin() as conn:
        await conn.execute(update(rr).where(rr.c.run_id == run_id).values(**values))


async def _event(conn, run_id: int, body: dict) -> None:
    """Write ``body`` in the log of run ``run_id``, its row locked as write_event wants."""
    r = tables.runs
    await conn.execute(select(r.c.id).where(r.c.id == run_id).with_for_update())
    await write_event(conn, run_id, body)


async def _default_branch(app: GitHubApp, token: str, where: tuple[str, str]) -> str:
    """The default branch of the repo, as GitHub names it."""
    owner, name = where
    found = await pulls.repository(app, token, owner, name)
    default = found.get("default_branch")
    if not isinstance(default, str) or not default:
        raise Refusal(f"GitHub names no default branch of {owner}/{name}")
    return default


async def _main_version(app: GitHubApp, token: str, where: tuple[str, str], default: str) -> str | None:
    """The version VERSION_FILE names at the tip of the default branch ``default``."""
    owner, name = where
    tip = await pulls.branch_head(app, token, owner, name, default)
    text = await pulls.file_text(app, token, owner, name, release_runs.VERSION_FILE, tip["sha"])
    return release_runs.file_version(text)


def pull_problem(found: dict, row: Landing, default: str) -> str | None:
    """Why the pull request ``found`` is not the one of ``row`` the hub merges or tags: its head is another branch
    than the run's, or another commit than the one the run pushed, or its base is not the default branch ``default``;
    None when it is."""
    head = found.get("head") if isinstance(found.get("head"), dict) else {}
    base = found.get("base") if isinstance(found.get("base"), dict) else {}
    number = found.get("number")
    if head.get("ref") != row.branch:
        return f"pull request #{number} is of {head.get('ref')!r}, not of {row.branch}"
    if head.get("sha") != row.head_commit:
        return (
            f"the head of pull request #{number} of {row.branch} is {releases.short(head.get('sha'))}, not "
            f"{releases.short(row.head_commit)}, the commit release run #{row.run_id} pushed"
        )
    if base.get("ref") != default:
        into = base.get("ref")
        return f"pull request #{number} of {row.branch} is into {into!r}, not into {default}, the default branch"
    return None


async def _tag_commit(app: GitHubApp, token: str, where: tuple[str, str], tag: str) -> str | None:
    """The commit the tag ``tag`` of the repo names (peeled), or None when there is no such tag."""
    try:
        refs = await read_refs(app, token, *where, [TAGS + tag])
    except ReadFailed as exc:
        raise Refusal(str(exc)) from None
    return refs.get(TAGS + tag)


def _pull_text(row: Landing, tag: str) -> str:
    """The body of the pull request of ``row``: what it releases and what the hub does with it, naming no member."""
    since = f" since {row.base_tag}" if row.base_tag else ""
    lines = [
        f"Release {row.version}, cut by release run #{row.run_id} of cycle #{row.cycle_id} from {row.target_ref} at "
        f"{releases.short(row.target_commit)}{since}.",
    ]
    if row.decision:
        lines += ["", release_runs.inert(row.decision)]
    lines += [
        "",
        f"The hub merges this pull request with a merge commit and tags that commit {tag}; {row.workflow} of the "
        "tag runs the full CI and publishes it.",
    ]
    return "\n".join(lines)


async def _no_signs(app: GitHubApp, token: str, where: tuple[str, str], row: Landing, number: int) -> None:
    """Hacking when the files of pull request ``number`` show signs of test hacking."""
    files = judge.from_github_files(await pulls.pull_files(app, token, *where, number))
    signs = release_ci.release_signs(files, row.repo)
    if signs:
        raise Hacking(
            f"the change of {row.branch} (pull request #{number}) shows signs of test hacking: "
            f"{release_ci.signs_text(signs)}; the hub neither merges nor tags it"
        )


async def _merged(engine: AsyncEngine, app: GitHubApp, token: str, where: tuple[str, str], row: Landing) -> str:
    """The merge commit of the pull request of ``row``'s branch: merged now, once nothing refuses it, or found merged
    by an earlier pass or by hand, when it is the run's (``pull_problem``) and shows no sign of test hacking."""
    owner, name = where
    default = await _default_branch(app, token, where)
    number = row.pull_number
    if number is not None:
        found = await pulls.pull(app, token, owner, name, number)
        problem = pull_problem(found, row, default)
        if problem is not None:
            raise Refusal(problem)
        if found.get("merged") is True and isinstance(found.get("merge_commit_sha"), str):
            await _no_signs(app, token, where, row, number)
            return found["merge_commit_sha"]
        if found.get("state") != "open":
            raise Refusal(f"the pull request #{number} of {row.branch} was closed without its merge")
    main_version = await _main_version(app, token, where, default)
    head_text = await pulls.file_text(app, token, owner, name, release_runs.VERSION_FILE, row.head_commit)
    problem = release_runs.land_problem(
        row.version,
        main_version,
        release_runs.file_version(head_text),
        branch=row.branch,
        tag=row.tag,
        tag_exists=await _tag_commit(app, token, where, row.tag) is not None,
    )
    if problem is not None:
        raise Refusal(problem)
    if number is None:
        async with engine.connect() as conn:
            signed = await pull_trailers.block(conn, row.run_id)
        made = await pulls.open_pull(
            app,
            token,
            owner,
            name,
            branch=row.branch,
            base=default,
            title=f"Release {row.version}",
            body=_pull_text(row, row.tag) + signed,
        )
        number, url = made.get("number"), made.get("html_url")
        if type(number) is not int:
            raise GitHubUnavailable(f"GitHub answered without the number of the pull request of {row.branch}")
        problem = pull_problem(made, row, default)
        if problem is not None:
            raise Refusal(problem)
        await _save(engine, row.run_id, pull_number=number, pull_url=url if isinstance(url, str) else None)
    await _no_signs(app, token, where, row, number)
    title = f"Merge pull request #{number} from {owner}/{row.branch}"
    merge = await pulls.merge_pull(app, token, owner, name, number, sha=row.head_commit, title=title)
    await _save(engine, row.run_id, merge_commit=merge)
    return merge


async def _tagged(app: GitHubApp, token: str, where: tuple[str, str], tag: str, merge: str) -> None:
    """Tag ``merge`` as ``tag``, or find it tagged so already; Refusal for a tag on another commit."""
    on = await _tag_commit(app, token, where, tag)
    if on is None and await pulls.create_ref(app, token, *where, TAGS + tag, merge):
        return
    on = on or await _tag_commit(app, token, where, tag)
    if on != merge:
        raise Refusal(
            f"the tag {tag} exists already on {releases.short(on)}, not on the merge commit {releases.short(merge)}"
        )


async def _land(engine: AsyncEngine, app: GitHubApp, token: str, where: tuple[str, str], row: Landing) -> str:
    merge = row.merge_commit or await _merged(engine, app, token, where, row)
    await _tagged(app, token, where, row.tag, merge)
    return merge


async def _refuse(
    engine: AsyncEngine, row: Landing, reason: str, state: str = "refused", cause: str = "refused"
) -> str:
    """Mark ``row`` ``state`` (refused, or failed) for ``reason`` and stop its cycle of ``cause``."""
    rr = tables.release_runs
    async with engine.begin() as conn:
        await conn.execute(
            update(rr)
            .where(rr.c.run_id == row.run_id)
            .values(land_state=state, land_reason=releases.clip_reason(reason))
        )
        await stop_cycle(
            conn, row.cycle_id, f"the hub did not land {row.branch} of release run #{row.run_id}: {reason}", cause
        )
        await _event(conn, row.run_id, {"text": f"The hub did not land {row.branch}: {reason}", "land_state": state})
    log.warning("release not landed", extra={"run_id": row.run_id, "state": state, "reason": reason})
    return state


async def _try_failed(engine: AsyncEngine, row: Landing, reason: str) -> str:
    """Count a pass GitHub failed for ``row``: failed, the cycle stopped, once LAND_TRIES did in a row; else waiting."""
    rr = tables.release_runs
    async with engine.begin() as conn:
        tries = (
            await conn.execute(
                update(rr)
                .where(rr.c.run_id == row.run_id)
                .values(land_tries=rr.c.land_tries + 1, land_reason=releases.clip_reason(reason))
                .returning(rr.c.land_tries)
            )
        ).scalar_one()
    log.warning("release landing tried again later", extra={"run_id": row.run_id, "tries": tries, "why": reason})
    if tries < release_runs.LAND_TRIES:
        return "waiting"
    return await _refuse(engine, row, f"GitHub failed {tries} passes in a row: {reason}", "failed", "github")


async def _done(engine: AsyncEngine, row: Landing, merge: str) -> str:
    rr = tables.release_runs
    async with engine.begin() as conn:
        await conn.execute(
            update(rr)
            .where(rr.c.run_id == row.run_id)
            .values(merge_commit=merge, tagged=row.tag, tagged_at=func.now(), land_state="tagged", land_reason=None)
        )
        t = tables.release_tags
        followed = upsert(t).values(cycle_id=row.cycle_id, tag=row.tag, commit=merge, run_id=row.run_id)
        await conn.execute(  # a watch that saw the tag before this pass followed it as a person's: it is the run's
            followed.on_conflict_do_update(constraint="release_tags_cycle_id_tag_key", set_={"run_id": row.run_id})
        )
        text = (
            f"The hub merged {row.branch} as {releases.short(merge)} and tagged it {row.tag}: {row.workflow} of the "
            "tag publishes the release, and the hub follows its CI."
        )
        await _event(conn, row.run_id, {"text": text, "tag": row.tag, "merge_commit": merge})
        await audit.record(
            conn,
            actor_id=row.owner_id,
            token_id=None,
            action=TAG,
            target=f"{row.project} cycle:{row.cycle_id} run:{row.run_id} tag={row.tag} commit={merge}",
            project_id=row.project_id,
        )
    log.info("release tagged", extra={"run_id": row.run_id, "tag": row.tag, "commit": merge})
    return "tagged"


async def land(engine: AsyncEngine, app: GitHubApp | None, row: Landing) -> str:
    """Land release run ``row`` (see the module): tagged, refused, failed, or waiting for the next pass."""
    async with engine.connect() as conn:
        admin = await owner_is_admin(conn, row.owner_id, row.owner, row.project)
    if not admin:
        return await _refuse(
            engine,
            row,
            f"{row.owner}, the owner of the schedule, holds no admin role on {row.project} any more: the hub lands "
            "nothing in their name",
        )
    where = github_repo(row.origin) if row.origin else None
    if app is None:
        return await _refuse(engine, row, "this hub has no GitHub App: it opens, merges and tags nothing")
    if where is None:
        return await _refuse(engine, row, f"repo {row.repo} of project {row.project} has no origin on GitHub")
    held = await _autonomy(engine, row)
    if held is not None:
        return held
    try:
        merge = await pulls.with_token(app, *where, "merge", lambda token: _land(engine, app, token, where, row))
    except Refusal as exc:
        return await _refuse(engine, row, str(exc), cause=exc.cause)
    except GitHubUnavailable as exc:
        return await _try_failed(engine, row, str(exc))
    except GitHubRefused as exc:
        return await _refuse(engine, row, str(exc))
    tagged = await _done(engine, row, merge)
    async with engine.begin() as conn:
        title = f"The hub merged {row.branch} and tagged {row.tag} of {row.repo} for release run {row.run_id}"
        await autonomy_gates.tell_after(conn, row.run_id, "K6", key=f"release {row.tag}", title=title, repo=row.repo)
    return tagged


async def _autonomy(engine: AsyncEngine, row: Landing) -> str | None:
    """K6 of the principal of release run ``row`` (``autonomy_gates``): None to land it; waiting while the principal
    is asked, the reason kept; refused on a deny or a refusal, its cycle stopped."""
    what = f"merge {row.branch} into {row.target_ref} and tag {row.tag} of {row.repo}"
    async with engine.begin() as conn:
        said = await autonomy_gates.check(conn, row.run_id, "K6", key=f"release {row.tag}", what=what, repo=row.repo)
        if said.waits:
            rr = tables.release_runs
            reason = releases.clip_reason(said.reason)
            await conn.execute(update(rr).where(rr.c.run_id == row.run_id).values(land_reason=reason))
    if said.act:
        return None
    return "waiting" if said.waits else await _refuse(engine, row, said.reason)


async def land_releases(engine: AsyncEngine, app: GitHubApp | None) -> dict:
    """One pass of landing: each release run waiting to land gets ``land``, one failing leaving the others their
    turn; how many ended each way."""
    async with engine.connect() as conn:
        rows = [Landing(**row._mapping) for row in (await conn.execute(_waiting())).all()]
    outcomes: Counter = Counter()
    for row in rows:
        try:
            outcomes[await land(engine, app, row)] += 1
        except Exception:
            log.exception("a release landing failed", extra={"run_id": row.run_id})
            outcomes["failed"] += 1
    return dict(sorted(outcomes.items()))
