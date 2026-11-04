"""The Curator's changes on the hub: an accepted proposal's plan, its Builder, its pull request, its Judge and its
merge; and the job that moves the changes on. ``evo_agents.hub.judge`` holds the model,
``evo_agents.hub.server.proposal_plans`` how an accepted proposal becomes the Curator's plan and its change, and
``evo_agents.hub.server.protection`` the repos whose ruleset keeps the Curator off their default branch. This module
holds the job, its pull requests and merges; ``change_views`` the models and the reading of a change, ``change_shift``
the night shift's side, ``change_routes`` the routes and ``change_watch`` what the job reads again.

Accepting a proposal of a tier the charter's ``planned_tiers`` names (tiers 0 to 2 unless it names others,
``judge.planned_tiers``) makes its draft the Curator's plan ``curator-<proposal>-<slug>``, on the branch
``curator/<proposal>-<slug>`` of its one repo, and a row of curator_changes, state ``planned``; a draft that cannot
become one leaves its change ``open``, with the reason, and nothing of it runs (``proposal_plans.plan_from_proposal``).
A write of a Curator's plan that names another repo or branch for it, or changes anything but the progress of its steps
(status, done_at, evidence and note) and its status, is refused (``plan_write_refusal``, from ``plans._store``): its
what, goal, context, verify and acceptance stay as the hub made them. A plan whose id starts with ``curator-`` is made
this way alone, never by hand. No member dispatches a run of a Curator's plan, step or plan
(``refuse_manual_dispatch``): the night shift alone runs it, at any hour.

The night shift (``curator.gate_of`` and ``_fire``) queues, after the night's review run and outside its window too
(``shift_hours``), the judge run of a change waiting for one (``queue_judge``), then the Builder of a planned change
(``queue_builder``): a plan run of the Curator's plan, pinned to the worker on duty, as the schedule's owner, within the
night's caps. A change on GitHub gets its Builder only once the hub checked the repo's ruleset within PROTECTION_DAYS
and found it keeps the Curator's App off the default branch (curator_repo_checks); one on GitLab only once the charter
names the owner's git secret (``git_secret``) and the secret is there. A Builder never runs while a judge run of its
change is active, nor a judge run while a run of the plan is. Each planned change ``queue_builder`` passes over keeps
why as its reason, written only when it says something new, and null again once its Builder is queued: its plan gone,
the repo's ruleset not checked or not keeping the Curator off (``protection.ruleset_wait``), the owner's git secret
missing, a run of the plan still active, no step ready, or a worker on duty that cannot build it.

When the Builder ends done with every step of the plan done (``builder_ended``), the change waits for its pull
request (``pr_pending``) on GitHub, or for its Judge (``judge_pending``) on GitLab, where the push opened the merge
request; judged there, it stays open for its owner, or goes to the merge queue when the project's autonomy policy has
its repo in mode auto (``merge_gate.curator_judged``; the hub's worker reaches the queue's job through this module).
The job ``curator.changes`` (``advance``), one pass at a time, opens the pull request with the workers' App, reads its
files and runs ``diff_signs.hack_signs`` on them: a sign fails the change at once and puts its proposal at tier 3. Those
files also give the proposal its tier again (``change_tiers.retier``: ``tiers.tier_of`` over the paths the pull request
really changes, what the knowledge graph says of them and, for kind kg_source, what it does to knowledge.yaml key by
key, which only ever raises it), so a proposal of docs whose pull request changes code is tier 1 from then on. A list of
files GitHub cuts short is a sign too. A judge run reads what it needs with GET /v1/worker/runs/{id}/judge, the hidden
checks of the charter and the checks of knowledge.yaml included (``change_tiers.judge_knowledge``), and posts its
verdict with POST /v1/worker/runs/{id}/verdict, those checks' results among its verify results. Both take, besides the
worker's token, the run's own key (``judge.JUDGE_KEY_HEADER``), which the hub makes when the run is claimed, hands the
daemon in the claim alone and keeps as a SHA-256 (``issue_judge_key``): the worker token, which code on the worker's
machine can read, neither reads a hidden check nor writes a verdict. The hub passes the change only as
``judge.final_verdict`` says, and a sign of the worker's detector puts the proposal at tier 3 too; the paths the worker
read in the diff give the proposal its tier again. A judge run that ends without a verdict queues another,
JUDGE_ATTEMPTS in all, then leaves the change open. The job then writes the Judge's check run on the pull request with
the workers' App, and merges it when ``judge.merge_decision`` says so, reading everything again from GitHub first (the
ruleset with the Curator's App, the files and the tier they give, the checks the ruleset requires and the head commit's
message), and merging a change of a tier the charter's ``auto_merge`` names (tier 0 alone unless it names more, up to 3)
at the head the Judge passed; anything else, a tier the charter does not name, a sign or a protected path, stays open
for its owner with the reason.
Before it merges or leaves a change open it reads the change again under its row lock (``_unchanged``): one a person
closed or an agent merged while the pass read GitHub is left as it is (MOVED).
CI must be green, or, when the charter has ``merge_without_ci``, may be none at all or, with a ruleset that requires no
check, what ran: a check that ran and failed still leaves the change open, and one still running makes it wait. A
change of a harness alone whose replays won merges as tier 0 (``change_tiers.merge_tier``), and the merge of one
releases its global skill (``evo_agents.hub.server.skill_release``, part of each pass).
A change left open for its owner is told in its proposal's thread when the charter has the agent answer, and the agent
may merge it there (``curator_threads``, through ``merge_read``, ``merge_at_head`` and ``record_merge``, which the
hub's own merge uses too). While the project's night shift is paused the hub merges nothing: a judged change waits for
it. Every WATCH_EVERY it reads such a pull request again: merged by hand, its change is ``merged`` (when, by whom, at
which commit), closed without a merge, ``closed`` (``_settle``). A branch merged by hand outside the hub is read the same way
(``settle_by_branch``): when GitHub refuses to open the pull request of a change (no commits between the default
branch and its branch, say), and every WATCH_EVERY for a planned change whose plan has no step ready to run, the job
looks for a pull request of the change's branch in any state, and a merged one, else a closed one, settles the change,
which names it from then on. It also checks rulesets (``protection.check_rulesets``): the one of a repo a planned
change waits for at its next pass, and every RECHECK_HOURS each one checked before. Each move of a change adds its
lines to its proposal's ledger (``evo_agents.hub.server.ledger``), and the job curator.outcomes counts the figures of
a merged change again after the charter's outcome_days (``evo_agents.hub.server.outcomes``).

GET /v1/projects/{p}/curator/changes (reader) lists the project's changes the caller may read (its proposal, and its
plan when it has one, through the project's hub sink), newest first, each with its reason. POST
.../curator/changes/{id}/close (admin), with a reason, closes a change left ``open`` with no pull request, such as one
whose proposal named no repo: the reason becomes the change's, and its proposal's ledger gets the line. A change with a
pull request closes when its pull request does.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import partial

from sqlalchemy import exists, func, select, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import approval_heads, diff_signs, guard, judge, tables
from evo_agents.hub.server import audit, autonomy_gates, change_tiers, merge_gate, skill_release
from evo_agents.hub.server.change_views import change_of_plan, charter_of, raise_tier, set_change
from evo_agents.hub.server.change_watch import settle_by_branch, watch_pass
from evo_agents.hub.server.github import GitHubRefused, GitHubUnavailable
from evo_agents.hub.server.github_app import GitHubApp
from evo_agents.hub.server.protection import (
    Target,
    check_rulesets,
    github_target,
    reason_line,
    store_check,
)
from evo_agents.hub.server.run_state import write_event

log = logging.getLogger(__name__)

PULL = "curator.pull"  # the hub opened a change's pull request: "<project>/<plan> change:<id> pr=<url>"
MERGE = "curator.merge"  # the hub merged a change's pull request: "<project>/<plan> change:<id> pr=<url> sha=<sha>"
CI_WAIT = timedelta(hours=6)  # CI still running this long after the verdict leaves the pull request open
NIGHT_SHIFT = "night_shift"
PAUSED = "the night shift of the project is paused: the hub merges nothing while it is"
MOVED = "moved"  # the outcome of a change closed, merged or judged again while a pass of curator.changes read it


# The job


async def _due_changes(engine: AsyncEngine) -> list:
    c, pr = tables.curator_changes, tables.project_repos
    async with engine.begin() as conn:
        query = (
            select(c, pr.c.origin)
            .join_from(c, pr, (pr.c.project_id == c.c.project_id) & (pr.c.name == c.c.repo), isouter=True)
            .where(c.c.state.in_(("pr_pending", "judged")))
            .order_by(c.c.id)
        )
        return (await conn.execute(query)).all()


async def _owner_of(conn: AsyncConnection, project_id: int) -> int | None:
    s = tables.schedules
    query = select(s.c.owner_id).where(s.c.project_id == project_id, s.c.kind == NIGHT_SHIFT)
    return (await conn.execute(query)).scalar_one_or_none()


def _pr_body(project: str, change, proposal_title: str) -> str:
    return (
        f"The Curator of project {project} on the evo-agents hub made this change from proposal "
        f"#{change.proposal_id} (tier {change.tier}): {proposal_title}.\n\n"
        f"Its plan on the hub is {change.plan_id}. The Judge writes its verdict as the check run "
        f'"{judge.JUDGE_CHECK_NAME}"; the hub merges only a change the Judge passed, of a tier the charter of the '
        "project lets it merge, with CI green unless that charter merges without CI."
    )


def _signs_of(diff, change, charter: dict, body: dict, cut: str | None) -> list[dict]:
    """``diff_signs.hack_signs`` of a pull request's files, and first, so the cap on signs never drops it, the sign of a
    list of files GitHub did not give whole (``cut``)."""
    signs = diff_signs.hack_signs(
        diff,
        repo=change.repo,
        protected=[item for item in charter.get("protected_paths") or [] if isinstance(item, str)],
        verify_commands=judge.verify_commands(body),
    )
    if cut is not None:
        signs.insert(0, {"kind": "diff_unreadable", "path": "(the pull request's files)", "line": None, "text": cut})
    return signs


async def _pull_inputs(engine: AsyncEngine, change) -> tuple[str, str, dict, str, dict]:
    """(its proposal's title, the project, the charter, the trailers the Builder's run signs it with, the plan's body)
    of a change whose pull request the hub opens."""
    from evo_agents.hub.server import pull_trailers

    async with engine.begin() as conn:
        pr, p = tables.proposals, tables.projects
        title, project = (
            await conn.execute(
                select(pr.c.title, p.c.name)
                .join_from(pr, p, p.c.id == pr.c.project_id)
                .where(pr.c.id == change.proposal_id)
            )
        ).one()
        charter = await charter_of(conn, change.project_id)
        rev, signed = tables.plans, await pull_trailers.block(conn, change.builder_run_id)
        body = (
            await conn.execute(
                select(rev.c.body).where(rev.c.project_id == change.project_id, rev.c.plan_id == change.plan_id)
            )
        ).scalar_one_or_none() or {}
    return title, project, charter, signed, body


async def _pull_opened(engine: AsyncEngine, change, project: str, made: dict, base: str, signs, facts, charter) -> str:
    """Keep pull request ``made`` the hub opened for ``change``: the change waits for its Judge, or with ``signs`` fails
    at once and puts its proposal at tier 3; its outcome, or "moved" for a change no longer waiting for it."""
    head, url = (made.get("head") or {}).get("sha"), made.get("html_url")
    async with engine.begin() as conn:
        locked = await change_of_plan(conn, change.project_id, change.plan_id, lock=True)
        if locked is None or locked.state != "pr_pending":
            return "moved"
        values = {"pr_number": made["number"], "pr_url": url, "base_branch": base, "head_sha": head, "reason": None}
        values.update(tier=await change_tiers.retier(conn, locked, facts, charter), knowledge=facts.knowledge)
        if signs:
            _, failures = judge.final_verdict(
                agent=None, verify=[], expected_verify=[], hidden=[], hidden_count=0, signs=signs, head_matches=True
            )
            verdict = {"signs": signs, "failures": failures[:1], "source": "hub", "head_sha": head}
            await raise_tier(conn, locked, signs)
            values.update(state="judged", passed=False, verdict=verdict, tier=3)
        else:
            values["state"] = "judge_pending"
        await set_change(conn, change.id, **values)
        if change.builder_run_id is not None:
            await write_event(
                conn,
                change.builder_run_id,
                {"text": f"The hub opened pull request {url} of the Curator's change #{change.id}.", "pr": url},
            )
        await audit.record(
            conn,
            actor_id=None,
            token_id=None,
            action=PULL,
            target=f"{project}/{change.plan_id} change:{change.id} pr={url}",
            project_id=change.project_id,
        )
    return "signs" if signs else "opened"


async def _open_pull(engine: AsyncEngine, app: GitHubApp, change, reader: change_tiers.GraphReader) -> str:
    from evo_agents.hub.server import pulls

    target = github_target(change.origin)
    if target is None:
        async with engine.begin() as conn:
            await set_change(conn, change.id, state="open", reason="its repo has no origin on GitHub")
        return "open"
    title, project, charter, signed, body = await _pull_inputs(engine, change)

    async def opened(token: str) -> dict:
        async with engine.connect() as conn:  # the names of the guard leave the title and body on a published repo
            pr_title, pr_body = guard.scrub(
                await guard.policy(conn),
                f"github.com/{target.owner}/{target.name}",
                f"Curator: {title}"[:240],
                _pr_body(project, change, title),
            )
        found = await pulls.repository(app, token, target.owner, target.name)
        base = found.get("default_branch")
        if not isinstance(base, str) or not base:
            raise GitHubRefused(f"GitHub names no default branch of {target.owner}/{target.name}")
        made = await pulls.open_pull(
            app,
            token,
            target.owner,
            target.name,
            branch=change.branch,
            base=base,
            title=pr_title,
            body=pr_body + signed,
        )
        return {"pull": made, "base": base}

    try:
        found = await pulls.with_token(app, target.owner, target.name, "open", opened)
    except GitHubRefused as exc:
        settled = await settle_by_branch(engine, app, change, target)
        if settled is not None:  # its branch was merged, or its pull request closed, by hand
            return settled
        async with engine.begin() as conn:
            await set_change(conn, change.id, reason=reason_line(f"the pull request could not be opened: {exc}"))
        return "refused"
    made = found["pull"]
    number = made.get("number")
    if not judge.is_sha((made.get("head") or {}).get("sha")) or type(number) is not int:
        raise GitHubUnavailable("GitHub answered without the pull request's number and head")

    async def read(token: str) -> tuple[list[dict], str | None]:
        return await _files_of(app, token, target, number, made.get("changed_files"))

    files, cut = await pulls.with_token(app, target.owner, target.name, "read", read)
    diff = diff_signs.from_github_files(files)
    signs = _signs_of(diff, change, charter, body, cut)
    facts = await change_tiers.pull_facts(engine, app, change, target, diff_signs.changed_paths(diff), made, reader)
    return await _pull_opened(engine, change, project, made, found["base"], signs, facts, charter)


async def _files_of(app: GitHubApp, token: str, target: Target, number: int, expected) -> tuple[list[dict], str | None]:
    """(the files of pull request ``number`` as GitHub lists them, why that list is not the whole of them or None):
    a list GitHub cuts short, or that holds fewer files than the pull request says it changes, is not read in part."""
    from evo_agents.hub.server import pulls

    try:
        files = await pulls.pull_files(app, token, target.owner, target.name, number)
    except pulls.Truncated as exc:
        return [], str(exc)
    if type(expected) is int and expected > len(files):
        return files, f"GitHub lists {len(files)} of the {expected} files the pull request changes"
    return files, None


async def night_paused(conn: AsyncConnection, project_id: int) -> bool:
    """Whether the project's night shift is paused (a member paused it, or its circuit breaker): it merges nothing."""
    s = tables.schedules
    query = select(exists().where(s.c.project_id == project_id, s.c.kind == NIGHT_SHIFT, s.c.paused_at.is_not(None)))
    return bool((await conn.execute(query)).scalar_one())


async def _left_for_owner(conn: AsyncConnection, change_id: int) -> None:
    """Tell the thread of the change's proposal, when it has the agent answer, that the hub left the change open for
    its owner (``curator_threads.on_left_open``, in a savepoint of this transaction)."""
    from evo_agents.hub.server.curator_threads import on_left_open  # it reads changes through this module

    await on_left_open(conn, change_id)


async def _unchanged(conn: AsyncConnection, change) -> bool:
    """Whether ``change`` is still as this pass read it, in the same state at the same head, its row locked until the
    caller's transaction ends: a person may have closed it, or an agent merged it, since."""
    c = tables.curator_changes
    query = select(c.c.state, c.c.head_sha).where(c.c.id == change.id).with_for_update()
    row = (await conn.execute(query)).first()
    return row is not None and (row.state, row.head_sha) == (change.state, change.head_sha)


async def _reopen(engine: AsyncEngine, change, why: str, kept: str | None = None) -> str:
    """Leave a judged change open for a person, saying ``why``; with ``kept``, the outcome of a change left as it is for
    a later pass (its night shift paused, its principal asked). MOVED, and nothing written, for a change that moved on
    since the pass read it."""
    async with engine.begin() as conn:
        if not await _unchanged(conn, change):
            return MOVED
        if kept is None:
            await set_change(conn, change.id, state="open", reason=why)
            await _left_for_owner(conn, change.id)
        else:
            c = tables.curator_changes
            await conn.execute(update(c).values(reason=why).where(c.c.id == change.id))
    return kept or "open"


async def _write_check(engine: AsyncEngine, app: GitHubApp, change) -> str:
    from evo_agents.hub.server import pulls

    target = github_target(change.origin)
    verdict = change.verdict or {}
    reasons = list(verdict.get("failures") or [])
    conclusion, title, summary = judge.check_run_output(
        bool(change.passed),
        reasons,
        verdict.get("verify") or [],
        verdict.get("hidden") or [],
        verdict.get("signs") or [],
    )

    async def write(token: str) -> int:
        return await pulls.create_check_run(
            app,
            token,
            target.owner,
            target.name,
            sha=change.head_sha,
            conclusion=conclusion,
            title=title,
            summary=summary,
            external_id=f"curator-change:{change.id}",
        )

    check_id = await pulls.with_token(app, target.owner, target.name, "check", write)
    async with engine.begin() as conn:
        await set_change(conn, change.id, check_run_id=check_id)
    return "checked"


async def _ruleset_now(engine: AsyncEngine, curator_app: GitHubApp | None, change, target: Target):
    """Check the repo's ruleset again now, with the Curator's App, and keep what it found; the ``RulesetCheck``, None
    without the Curator's App."""
    from evo_agents.hub.server.pulls import RulesetCheck, check_ruleset

    if curator_app is None:
        return None
    try:
        found = await check_ruleset(curator_app, target.owner, target.name)
    except GitHubUnavailable:
        raise
    except GitHubRefused as exc:  # check_ruleset turns refusals into a failed check; this is belt and braces
        found = RulesetCheck(False, None, str(exc))
    async with engine.begin() as conn:
        await store_check(conn, change.project_id, change.repo, f"{target.owner}/{target.name}", found, None)
    return found


async def _read_merge(app: GitHubApp, target: Target, change, token: str) -> dict:
    """What the hub reads of a judged change's pull request before it merges it: the pull request, its files and
    whether GitHub cut them, the check runs and statuses of its head, its head commit's message, and the repo."""
    from evo_agents.hub.server import pulls

    found = await pulls.pull(app, token, target.owner, target.name, change.pr_number)
    files, cut = await _files_of(app, token, target, change.pr_number, found.get("changed_files"))
    head = (found.get("head") or {}).get("sha")
    runs_found = await pulls.check_runs(app, token, target.owner, target.name, head) if judge.is_sha(head) else None
    statuses = await pulls.combined_status(app, token, target.owner, target.name, head) if judge.is_sha(head) else None
    try:
        commits = await pulls.pull_commits(app, token, target.owner, target.name, change.pr_number)
    except GitHubRefused:
        commits = []
    message = next(
        (
            (item.get("commit") or {}).get("message")
            for item in commits
            if isinstance(item, dict) and item.get("sha") == head
        ),
        None,
    )
    repo = await pulls.repository(app, token, target.owner, target.name)
    return {
        "pull": found,
        "files": files,
        "cut": cut,
        "runs": runs_found,
        "statuses": statuses,
        "message": message,
        "repo": repo,
    }


async def _tier_again(engine: AsyncEngine, app: GitHubApp, change, target: Target, diff, pull: dict, charter, reader):
    """The tier of a judged change from its pull request's files as GitHub lists them now (``change_tiers.retier``),
    kept on the change when it rose."""
    facts = await change_tiers.pull_facts(engine, app, change, target, diff_signs.changed_paths(diff), pull, reader)
    async with engine.begin() as conn:
        tier = await change_tiers.retier(conn, change, facts, charter)
        if tier != change.tier:
            await set_change(conn, change.id, tier=tier)
    return tier


@dataclass(frozen=True)
class MergeRead:
    """What the hub read of a judged change's pull request just before a merge: the facts ``judge.merge_decision``
    reads, the pull request as GitHub has it now, and its files as a diff."""

    facts: judge.MergeFacts
    pull: dict
    diff: list


@dataclass(frozen=True)
class MergedBy:
    """The agent of a proposal's thread that merged a change for its owner (``curator_threads``): its handle, its chat
    run, and the owner it acted for."""

    agent: str
    chat_run_id: int
    user_id: int
    login: str


def _auto(charter: dict) -> tuple[int, ...]:
    return tuple(item for item in charter.get("auto_merge") or [] if isinstance(item, int))


async def merge_read(
    engine: AsyncEngine,
    app: GitHubApp,
    curator_app: GitHubApp | None,
    change,
    charter: dict,
    body: dict,
    reader: change_tiers.GraphReader | None = None,
) -> MergeRead:
    """Read judged ``change``'s pull request again from GitHub, and its repo's ruleset with the Curator's App, and
    gather the facts of its merge: the signs of its files now, its tier again from them with ``reader`` (the change's
    own without one), and its CI as ``charter`` reads it. GitHubUnavailable and GitHubRefused go to the caller."""
    from evo_agents.hub.server import pulls

    target = github_target(change.origin)
    without_ci = charter.get("merge_without_ci") is True
    ruleset = await _ruleset_now(engine, curator_app, change, target)
    seen = await pulls.with_token(app, target.owner, target.name, "read", partial(_read_merge, app, target, change))
    found = seen["pull"]
    diff = diff_signs.from_github_files(seen["files"])
    signs = _signs_of(diff, change, charter, body, seen["cut"])
    tier = change.tier
    if reader is not None:
        tier = await _tier_again(engine, app, change, target, diff, found, charter, reader)
    if isinstance(seen["message"], str):
        ci, ci_reasons = judge.ci_state(
            seen["runs"],
            seen["statuses"],
            required=ruleset.required_checks if ruleset is not None else [],
            head_message=seen["message"],
            without_ci=without_ci,
        )
    else:
        ci, ci_reasons = "unreadable", ["the hub could not read the message of the pull request's head commit"]
    facts = judge.MergeFacts(
        forge=change.forge,
        tier=tier,
        auto_merge=_auto(charter),
        repo_checked=ruleset is not None and ruleset.protected,
        passed=bool(change.passed),
        judged_sha=change.head_sha,
        pr_state=found.get("state"),
        pr_merged=found.get("merged") is True,
        pr_head=(found.get("head") or {}).get("sha"),
        pr_base=(found.get("base") or {}).get("ref"),
        default_branch=seen["repo"].get("default_branch"),
        signs=tuple(signs),
        ci=ci,
        ci_reasons=tuple(ci_reasons),
        mergeable=found.get("mergeable") if isinstance(found.get("mergeable"), bool) else None,
        merge_without_ci=without_ci,
    )
    return MergeRead(facts, found, diff)


def merge_act(change, read: MergeRead) -> dict:
    """The act of merging ``change``'s pull request at the head the Judge passed, as the gate K5 names it."""
    scope = approval_heads.scope_of(diff_signs.changed_paths(read.diff), read.pull.get("body"))
    return autonomy_gates.pull_act(change.repo, change.pr_number, change.base_branch, change.head_sha, scope)


async def merge_at_head(app: GitHubApp, change, tier: int) -> str:
    """Merge ``change``'s pull request with the workers' App at the head the Judge passed; the merge commit.
    GitHubRefused when GitHub refuses it (the head moved, a conflict)."""
    from evo_agents.hub.server import pulls

    target = github_target(change.origin)

    async def merge(token: str) -> str:
        title = f"Merge the Curator's change #{change.id} (proposal #{change.proposal_id}, tier {tier})"
        return await pulls.merge_pull(
            app, token, target.owner, target.name, change.pr_number, sha=change.head_sha, title=title
        )

    return await pulls.with_token(app, target.owner, target.name, "merge", merge)


async def record_merge(
    conn: AsyncConnection, change, merged: str, read: MergeRead, act: dict, by: MergedBy | None = None
) -> None:
    """``change`` merged as commit ``merged``: its state, the ledger line, the owner's notice, the audit row and the
    notice of K5 for the run the gate read (the Builder's, or the chat run of ``by``, the agent that merged it)."""
    tier, ci = read.facts.tier, read.facts.ci
    extra = {"before_sha": (read.pull.get("base") or {}).get("sha")}  # the default branch it merged into
    if by is not None:
        extra.update(actor="user", actor_id=by.user_id, login=by.login, agent=by.agent, chat_run=by.chat_run_id)
    await set_change(
        conn, change.id, state="merged", merged_at=func.now(), merge_sha=merged, reason=None, ledger_extra=extra
    )
    project = (
        await conn.execute(select(tables.projects.c.name).where(tables.projects.c.id == change.project_id))
    ).scalar_one()
    who = "The hub" if by is None else by.agent
    owner_id = await _owner_of(conn, change.project_id) if by is None else by.user_id
    if owner_id is not None:
        from evo_agents.hub.server.notifications import notify, run_link

        how = "" if by is None else f" for {by.login} in chat run {by.chat_run_id}"
        await notify(
            conn,
            user_id=owner_id,
            kind="notice",
            notice_kind="merge_default_branch",
            project_id=change.project_id,
            run_id=change.builder_run_id,
            title=f"{who} merged pull request #{change.pr_number} of {change.repo} into {change.base_branch}",
            body=(
                f"The Curator's change #{change.id} (proposal #{change.proposal_id}, tier {tier}) passed its "
                f"Judge at {change.head_sha} with {'CI green' if ci == 'green' else 'no CI'}; "
                f"{'the hub' if by is None else by.agent} merged {change.pr_url} as {merged}{how}."
            ),
            details={
                "repo": change.repo,
                "branch": change.base_branch,
                "commits": [merged],
                "change_id": change.id,
                "pr": change.pr_url,
            },
            link=run_link(project, change.builder_run_id) if change.builder_run_id else None,
        )
    via = "" if by is None else f" via={by.agent} in chat run {by.chat_run_id}"
    await audit.record(
        conn,
        actor_id=None if by is None else by.user_id,
        token_id=None,
        action=MERGE,
        target=f"{project}/{change.plan_id} change:{change.id} pr={change.pr_url} sha={merged}{via}",
        project_id=change.project_id,
    )
    run_id = change.builder_run_id if by is None else by.chat_run_id
    told = f"{who} merged pull request #{change.pr_number} of {change.repo} for run {run_id}"
    await autonomy_gates.tell_after(conn, run_id, "K5", key=act["key"], title=told, repo=change.repo)


async def _merge(
    engine: AsyncEngine, app: GitHubApp, curator_app: GitHubApp | None, change, reader: change_tiers.GraphReader
) -> str:
    if not change.passed:
        failures = (change.verdict or {}).get("failures") or []
        return await _reopen(engine, change, reason_line("the Judge did not pass it: " + "; ".join(failures)))
    async with engine.begin() as conn:
        charter = await charter_of(conn, change.project_id)
        tier, waits = await change_tiers.merge_tier(conn, change)
        body = (
            await conn.execute(
                select(tables.plans.c.body).where(
                    tables.plans.c.project_id == change.project_id, tables.plans.c.plan_id == change.plan_id
                )
            )
        ).scalar_one_or_none() or {}
        paused = await night_paused(conn, change.project_id)
    if tier not in _auto(charter):  # no call to GitHub for a change the hub would never merge
        why = reason_line(waits) if tier != 0 else "the charter's auto_merge does not name tier 0"
        return await _reopen(engine, change, why)
    if paused:
        return await _reopen(engine, change, PAUSED, "paused")
    read = await merge_read(engine, app, curator_app, change, charter, body, reader)
    decided, reasons = judge.merge_decision(read.facts)
    if decided == "wait":
        async with engine.begin() as conn:
            if not await _unchanged(conn, change):
                return MOVED
            verdict_at = change.updated_at  # when the verdict, then its check run, was written
            if verdict_at is not None and await _older_than(conn, verdict_at, CI_WAIT):
                await set_change(
                    conn,
                    change.id,
                    state="open",
                    reason=reason_line(f"CI did not finish within 6 hours: {'; '.join(reasons)}"),
                )
                await _left_for_owner(conn, change.id)
                return "open"
            await conn.execute(
                update(tables.curator_changes)
                .values(reason=reason_line("; ".join(reasons)))
                .where(tables.curator_changes.c.id == change.id)
            )
        return "wait"
    if decided == "open":
        return await _reopen(engine, change, reason_line("; ".join(reasons)))

    async with engine.begin() as conn:
        if not await _unchanged(conn, change):  # closed or merged while the hub read GitHub
            return MOVED
        paused = paused or await night_paused(conn, change.project_id)  # paused while the hub read GitHub
        act = merge_act(change, read)
        said = None if paused else await autonomy_gates.check(conn, change.builder_run_id, "K5", **act)
    if paused:
        return await _reopen(engine, change, PAUSED, "paused")
    if not said.act:  # K5 of the principal of the Builder's run (``autonomy_gates``)
        return await _reopen(engine, change, reason_line(said.reason), "wait" if said.waits else None)
    try:
        merged = await merge_at_head(app, change, read.facts.tier)
    except GitHubRefused as exc:
        return await _reopen(engine, change, reason_line(f"GitHub refused the merge: {exc}"))
    async with engine.begin() as conn:
        c = tables.curator_changes
        state = select(c.c.state).where(c.c.id == change.id).with_for_update()
        if (await conn.execute(state)).scalar_one_or_none() == "merged":  # an agent recorded its merge meanwhile
            return MOVED
        await record_merge(conn, change, merged, read, act)
    log.info("curator change merged", extra={"change_id": change.id, "sha": merged})
    return "merged"


async def _older_than(conn: AsyncConnection, moment: datetime, span: timedelta) -> bool:
    return bool((await conn.execute(select(func.now() - span > moment))).scalar_one())


async def _judged_elsewhere(engine: AsyncEngine, change) -> str:
    """A judged change off GitHub: left open, or handed to the merge queue when its policy says so
    (``merge_gate.curator_judged``); MOVED for a change that moved on since the pass read it."""
    outcome, why = await merge_gate.curator_judged(engine, change)
    async with engine.begin() as conn:
        if not await _unchanged(conn, change):
            return MOVED
        await set_change(conn, change.id, state="open", reason=reason_line(why))
    return outcome


async def _move_on(
    engine: AsyncEngine,
    github_app: GitHubApp | None,
    curator_app: GitHubApp | None,
    change,
    reader: change_tiers.GraphReader,
    outcomes: Counter,
) -> None:
    """Move one due change on, counting its outcome in ``outcomes``."""
    if change.forge != "github":
        if change.state == "judged":
            outcomes[await _judged_elsewhere(engine, change)] += 1
        return
    if github_app is None:
        outcomes["no_app"] += 1
        return
    if change.state == "pr_pending":
        outcomes[await _open_pull(engine, github_app, change, reader)] += 1
        return
    if change.check_run_id is None and judge.is_sha(change.head_sha) and github_target(change.origin) is not None:
        outcomes[await _write_check(engine, github_app, change)] += 1
    outcomes[await _merge(engine, github_app, curator_app, change, reader)] += 1


async def advance(
    engine: AsyncEngine, github_app: GitHubApp | None, curator_app: GitHubApp | None, *, blobs=None, data_dir=None
) -> dict:
    """One pass of the job ``curator.changes`` (see the module's docstring); how many changes ended in each outcome.
    GitHub failing leaves a change as it is for the next pass. ``blobs`` and ``data_dir`` are where the knowledge
    graphs the tier rules read come from (``change_tiers.GraphReader``)."""
    outcomes: Counter = Counter()
    reader = change_tiers.GraphReader.at(blobs, data_dir)
    for change in await _due_changes(engine):
        try:
            await _move_on(engine, github_app, curator_app, change, reader, outcomes)
        except GitHubUnavailable as exc:
            log.warning("a curator change waits for GitHub", extra={"change_id": change.id, "why": str(exc)})
            outcomes["github"] += 1
        except Exception:  # one change failing leaves the others their turn
            log.exception("a curator change failed", extra={"change_id": change.id})
            outcomes["failed"] += 1
    if github_app is not None:
        await watch_pass(engine, github_app, outcomes)
    await skill_release.release(engine, github_app, blobs, outcomes)
    await check_rulesets(engine, curator_app, outcomes)
    return dict(sorted(outcomes.items()))
