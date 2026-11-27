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
release tags (``service.github.read_refs``), and refuses (``release_runs.land_problem``) a version that is not
greater, changes the major, is neither the next patch nor the next minor, whose tag exists, or that the branch does not
name. Then: the pull request opened (or the one open found into the default branch), its number kept, its body naming
the agent's Decision line with nothing GitHub acts on (``release_runs.inert``); its files read for signs of test
hacking (``release_ci.release_signs``): a change with a sign that stops a fix (``release_ci.stopping``: a skip added
but for importorskip and a skip on one platform with its reason, an assertion made always true) is never merged, and
one with only a test or an assertion removed lands flagged (``_flag``: a comment on the pull request, the run's log
and a notice of kind release to the cycle's owner); merged at the run's commit
alone (``pulls.merge_pull``: GitHub refuses once the branch moved), the merge commit kept; and the tag made on it
(``pulls.create_ref``), or found there already. Each part is kept as it is done, so a pass that GitHub failed goes on
from there: a pull request merged already gives its merge commit, one closed without a merge is refused. A pull request
the hub did not merge itself is the run's only when its head is the run's branch at the commit the run pushed and its
base the default branch (``pull_problem``), and its files show no sign of test hacking either: else no tag is made.

A refusal (``land_problem``, a pull request closed, GitHub refusing a call, a tag elsewhere) marks the run refused and
stops the cycle with the reason (``service.cycles.stop_cycle``, cause refused, or hacking for signs of test hacking,
whose pull request stays open); GitHub failing marks a try, and LAND_TRIES in a row mark it failed and stop the cycle
too (cause github). A run tagged is audited as release.tag, with the cycle's owner as actor and no token; its cycle
stays open, and follows the tag (a row of release_tags) for the hub to watch its CI (``service.watch``).
"""

from __future__ import annotations

import logging
from collections import Counter

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import diff_signs, guard, release_ci, release_runs, releases
from evo_agents.hub.credentials import github_repo
from evo_agents.hub.server import audit
from evo_agents.hub.server.autonomy.service import gates as autonomy_gates
from evo_agents.hub.server.forge.client.github_app import GitHubApp
from evo_agents.hub.server.forge.models.github import GitHubRefused, GitHubUnavailable
from evo_agents.hub.server.merge.service import pulls
from evo_agents.hub.server.merge.service import trailers as pull_trailers
from evo_agents.hub.server.projects import project_access
from evo_agents.hub.server.releases.models.github import TAGS, ReadFailed
from evo_agents.hub.server.releases.models.land import TAG, Hacking, Landing, Refusal
from evo_agents.hub.server.releases.repository import runs as runs_repo
from evo_agents.hub.server.releases.repository import tags as tags_repo
from evo_agents.hub.server.releases.repository.cycles import cycle_of
from evo_agents.hub.server.releases.service.cycles import notify_release, stop_cycle
from evo_agents.hub.server.releases.service.github import read_refs
from evo_agents.hub.server.run_state import write_event
from evo_agents.hub.server.security import MACHINE, Principal, holds

log = logging.getLogger(__name__)


async def owner_is_admin(conn: AsyncConnection, owner_id: int, owner: str, project: str) -> bool:
    """Whether ``owner`` (user ``owner_id``), who owns a release schedule, holds the admin role on ``project``, as a
    request of theirs would find."""
    member = Principal(user_id=owner_id, login=owner, admin=False, token_id=0, kind=MACHINE, token_hash="")
    try:
        access = await project_access(conn, member, project)
    except HTTPException:
        return False
    return holds(access.role, "admin")


async def save_run(engine: AsyncEngine, run_id: int, **values) -> None:
    async with engine.begin() as conn:
        await runs_repo.save_release_run(conn, run_id, **values)


async def run_event(conn, run_id: int, body: dict) -> None:
    """Write ``body`` in the log of run ``run_id``, its row locked as write_event wants."""
    await runs_repo.lock_run(conn, run_id)
    await write_event(conn, run_id, body)


async def default_branch(app: GitHubApp, token: str, where: tuple[str, str]) -> str:
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
    if head.get("ref") != row.branch:  # pyright: ignore[reportOptionalMemberAccess]
        return f"pull request #{number} is of {head.get('ref')!r}, not of {row.branch}"  # pyright: ignore[reportOptionalMemberAccess]
    if head.get("sha") != row.head_commit:  # pyright: ignore[reportOptionalMemberAccess]
        return (
            f"the head of pull request #{number} of {row.branch} is {releases.short(head.get('sha'))}, not "  # pyright: ignore[reportOptionalMemberAccess]
            f"{releases.short(row.head_commit)}, the commit release run #{row.run_id} pushed"
        )
    if base.get("ref") != default:  # pyright: ignore[reportOptionalMemberAccess]
        into = base.get("ref")  # pyright: ignore[reportOptionalMemberAccess]
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


async def _no_signs(app: GitHubApp, token: str, where: tuple[str, str], row: Landing, number: int) -> list[dict]:
    """Hacking when the files of pull request ``number`` show a sign that stops a fix (``release_ci.stopping``);
    else the signs it lands flagged with, if any."""
    files = diff_signs.from_github_files(await pulls.pull_files(app, token, *where, number))
    signs = release_ci.release_signs(files, row.repo)
    stops = release_ci.stopping(signs)
    if stops:
        raise Hacking(
            f"the change of {row.branch} (pull request #{number}) shows signs of test hacking: "
            f"{release_ci.signs_text(stops)}; the hub neither merges nor tags it"
        )
    return signs


def _flag_text(row: Landing, number: int, signs: list[dict]) -> str:
    return (
        f"The hub lands {row.branch} (pull request #{number}) of release run #{row.run_id} with {len(signs)} "
        f"sign(s) to read: {release_ci.signs_text(signs)}. A fix forward may bring a test up to the code it tests; "
        "read these lines and revert them on the default branch if they hide a fault."
    )


async def _flag(
    engine: AsyncEngine, app: GitHubApp, token: str, where: tuple[str, str], row: Landing, number: int, signs: list
) -> None:
    """Tell what the hub lands ``row`` with: a comment on pull request ``number``, the run's log and a notice to the
    cycle's owner."""
    text = _flag_text(row, number, signs)
    await pulls.comment_pull(app, token, *where, number, text)
    async with engine.begin() as conn:
        await run_event(conn, row.run_id, {"text": text, "release_signs": signs[:20]})
        found = await cycle_of(conn, row.cycle_id)
        title = f"{row.tag} of {row.repo} lands with {len(signs)} test change(s) to read"
        details = {"tag": row.tag, "pull": number, "signs": signs[:20]}
        await notify_release(conn, found, event="flagged", title=title, body=text, run_id=row.run_id, details=details)
    log.info("release landed flagged", extra={"run_id": row.run_id, "signs": len(signs)})


async def _merged(engine: AsyncEngine, app: GitHubApp, token: str, where: tuple[str, str], row: Landing) -> str:
    """The merge commit of the pull request of ``row``'s branch: merged now, once nothing refuses it, or found merged
    by an earlier pass or by hand, when it is the run's (``pull_problem``) and shows no sign of test hacking."""
    owner, name = where
    default = await default_branch(app, token, where)
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
            policy = await guard.policy(conn)
        title, body = guard.scrub(
            policy, f"github.com/{owner}/{name}", f"Release {row.version}", _pull_text(row, row.tag) + signed
        )
        made = await pulls.open_pull(app, token, owner, name, branch=row.branch, base=default, title=title, body=body)
        number, url = made.get("number"), made.get("html_url")
        if type(number) is not int:
            raise GitHubUnavailable(f"GitHub answered without the number of the pull request of {row.branch}")
        problem = pull_problem(made, row, default)
        if problem is not None:
            raise Refusal(problem)
        await save_run(engine, row.run_id, pull_number=number, pull_url=url if isinstance(url, str) else None)
    signs = await _no_signs(app, token, where, row, number)
    if signs:
        await _flag(engine, app, token, where, row, number, signs)
    title = f"Merge pull request #{number} from {owner}/{row.branch}"
    merge = await pulls.merge_pull(app, token, owner, name, number, sha=row.head_commit, title=title)
    await save_run(engine, row.run_id, merge_commit=merge)
    return merge


async def ensure_tag(app: GitHubApp, token: str, where: tuple[str, str], tag: str, merge: str) -> None:
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
    await ensure_tag(app, token, where, row.tag, merge)
    return merge


async def _refuse(
    engine: AsyncEngine, row: Landing, reason: str, state: str = "refused", cause: str = "refused"
) -> str:
    """Mark ``row`` ``state`` (refused, or failed) for ``reason`` and stop its cycle of ``cause``."""
    async with engine.begin() as conn:
        await runs_repo.save_release_run(conn, row.run_id, land_state=state, land_reason=releases.clip_reason(reason))
        await stop_cycle(
            conn, row.cycle_id, f"the hub did not land {row.branch} of release run #{row.run_id}: {reason}", cause
        )
        await run_event(conn, row.run_id, {"text": f"The hub did not land {row.branch}: {reason}", "land_state": state})
    log.warning("release not landed", extra={"run_id": row.run_id, "state": state, "reason": reason})
    return state


async def _try_failed(engine: AsyncEngine, row: Landing, reason: str) -> str:
    """Count a pass GitHub failed for ``row``: failed, the cycle stopped, once LAND_TRIES did in a row; else waiting."""
    async with engine.begin() as conn:
        tries = await runs_repo.count_try(conn, row.run_id, reason)
    log.warning("release landing tried again later", extra={"run_id": row.run_id, "tries": tries, "why": reason})
    if tries < release_runs.LAND_TRIES:
        return "waiting"
    return await _refuse(engine, row, f"GitHub failed {tries} passes in a row: {reason}", "failed", "github")


async def mark_tagged(engine: AsyncEngine, row: Landing, merge: str, *, by_hand: bool = False) -> str:
    """Keep ``row`` tagged at ``merge``, follow the tag, audit it; ``by_hand`` when a person merged the pull request."""
    async with engine.begin() as conn:
        await runs_repo.mark_tagged(conn, row.run_id, merge, row.tag)
        await tags_repo.follow_tagged(conn, row.cycle_id, row.tag, merge, row.run_id)
        who = "A person merged" if by_hand else "The hub merged"
        text = (
            f"{who} {row.branch} as {releases.short(merge)} and "
            f"{'the hub ' if by_hand else ''}tagged it {row.tag}: {row.workflow} of the "
            "tag publishes the release, and the hub follows its CI."
        )
        await run_event(conn, row.run_id, {"text": text, "tag": row.tag, "merge_commit": merge})
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
    tagged = await mark_tagged(engine, row, merge)
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
            await runs_repo.save_release_run(conn, row.run_id, land_reason=releases.clip_reason(said.reason))  # pyright: ignore[reportArgumentType]
    if said.act:
        return None
    return "waiting" if said.waits else await _refuse(engine, row, said.reason)  # pyright: ignore[reportArgumentType]


async def land_releases(engine: AsyncEngine, app: GitHubApp | None) -> dict:
    """One pass of landing: each release run waiting to land gets ``land``, one failing leaving the others their
    turn; how many ended each way."""
    async with engine.connect() as conn:
        rows = [Landing(**row._mapping) for row in await runs_repo.waiting(conn)]
    outcomes: Counter = Counter()
    for row in rows:
        try:
            outcomes[await land(engine, app, row)] += 1
        except Exception:
            log.exception("a release landing failed", extra={"run_id": row.run_id})
            outcomes["failed"] += 1
    return dict(sorted(outcomes.items()))
