"""The Curator's changes that work in several repos (``evo_agents.hub.server.changes.service.job`` describes those of one).

An accepted proposal whose draft and paths name several repos of the project, each on GitHub, becomes one plan in them
all (``judge.curator_repos_plan``), ordered owner before consumer by the project's seams (``project_seams``: the
``owned_by`` and ``consumed_by`` edges of the Seam nodes the project's knowledge graph read from contracts.yaml, and
the draft's own ``seams_touched``), the draft's order kept where no seam says otherwise. Its change has a row of
curator_change_repos per repo in that order (``record_repos``), with the repos merged before it (``depends_on``: the
owners of the seams it consumes, or the repo before it when no seam names one); the change's own repo is the first.

The night shift's Builder waits until the hub checked the ruleset of each of its repos (``ruleset_waits``) and runs
the plan in them all. Once it is built, the job ``curator.changes`` opens a pull request of the change's branch in
each repo (``open_pulls``), each read for signs and its tier on its own; the change waits for its Judge once none
waits for its pull request. A judge run judges one repo at a time, in plan order (``next_to_judge``): it reads that
repo's pull request and the verify of that repo's steps alone, and its verdict lands on that repo's row
(``judged_repo``); the change is judged once every repo is, passed when every repo passed. The job then writes each
repo's check run and merges the pull request of each repo the Judge passed, as for one repo, but only once those of its
owners are merged (``judge.merge_waits``); the change is merged when every repo is. A repo whose branch changes nothing
gets no pull request: its row is closed, and its consumers do not wait for it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import diff_signs, judge
from evo_agents.hub.server import audit
from evo_agents.hub.server.autonomy.service import gates as autonomy_gates
from evo_agents.hub.server.changes.repository import job as job_store
from evo_agents.hub.server.changes.repository import repos as store
from evo_agents.hub.server.changes.repository.repos import repos_of
from evo_agents.hub.server.changes.repository.views import change_of_plan, charter_of, plan_body, raise_tier
from evo_agents.hub.server.changes.service import tiers as change_tiers
from evo_agents.hub.server.changes.service.moves import set_change
from evo_agents.hub.server.github import GitHubRefused, GitHubUnavailable
from evo_agents.hub.server.protection import github_target, reason_line, ruleset_wait

log = logging.getLogger(__name__)

LANDED = ("merged", "closed")  # a repo whose consumers no longer wait: merged, or its branch changes nothing
MOVED = "moved"
REPO_FIELDS = ("repo", "branch", "forge", "pr_number", "pr_url", "head_sha", "verdict", "passed", "check_run_id")


# The seams and the rows of a change


def draft_seams(draft) -> list[tuple[str, str]]:
    """(owner, consumer) of each seam the draft's ``seams_touched`` names."""
    found = draft.get("seams_touched") if isinstance(draft, dict) else None
    pairs = []
    for seam in found if isinstance(found, list) else []:
        if isinstance(seam, dict) and isinstance(seam.get("owner"), str):
            named = seam.get("consumers")
            consumers = named if isinstance(named, list) else []
            pairs += [(seam["owner"], item) for item in consumers if isinstance(item, str)]
    return pairs


async def project_seams(conn: AsyncConnection, project_id: int, draft) -> list[tuple[str, str]]:
    """(owner, consumer) of each seam the project registered, as its knowledge graph's main branch has it (a Seam
    ``owned_by`` one repo and ``consumed_by`` others), then those of ``draft``."""
    pairs = []
    for row in await store.seam_edges(conn, project_id):
        if row.owner.startswith("repo:") and row.consumer.startswith("repo:"):
            pairs.append((row.owner.removeprefix("repo:"), row.consumer.removeprefix("repo:")))
    return pairs + draft_seams(draft)


def repo_rows(ordered: list[str], seams) -> list[dict]:
    """The row of each repo of ``ordered`` (``judge.repo_order``): its position from 1 and the repos merged before it,
    the owners of the seams it consumes or, when no seam names one, the repo before it (``judge.merge_waits``)."""
    return [
        {
            "repo": repo,
            "position": index + 1,
            "depends_on": judge.owners_of(repo, seams, ordered[:index]) or ordered[max(index - 1, 0) : index],
        }
        for index, repo in enumerate(ordered)
    ]


async def repo_names(conn: AsyncConnection, change) -> list[str]:
    """The repos ``change`` works in, in plan order: its own alone for a change in one repo."""
    return [row.repo for row in await repos_of(conn, change.id)] or [change.repo]


def overlay(change, row, **more) -> SimpleNamespace:
    """``change`` as the code of a change in one repo reads it, for ``row``'s repo: its repo, branch, pull request,
    head, verdict and check run (and origin when ``row`` has one); ``repo_row_id`` names the row."""
    values = dict(change._mapping) if hasattr(change, "_mapping") else dict(vars(change))
    values.update({key: getattr(row, key) for key in REPO_FIELDS})
    if hasattr(row, "origin"):
        values["origin"] = row.origin
    values.update({"base_branch": None, "repo_row_id": row.id, **more})
    return SimpleNamespace(**values)


def repo_body(body, repo: str) -> dict:
    """Plan ``body`` with the steps of ``repo`` alone: what the Judge of that repo runs again."""
    if not isinstance(body, dict):
        return {}
    found = body.get("steps")
    steps = found if isinstance(found, list) else []
    return {**body, "steps": [step for step in steps if isinstance(step, dict) and step.get("repo") == repo]}


async def in_focus(conn: AsyncConnection, change) -> Any:
    """``change`` as its judge run reads it: for a change in several repos, ``overlay`` of the repo its judge run
    judges now; ``change`` itself otherwise."""
    if change is None or change.judge_run_id is None:
        return change
    row = await store.judging_row(conn, change.id, change.judge_run_id)
    return change if row is None else overlay(change, row)


# The Builder


async def ruleset_waits(conn: AsyncConnection, project_id: int, repos: list[str], *, curator_app: bool) -> str | None:
    """Why the Builder of a change on GitHub waits for the ruleset of one of ``repos``; None once each is checked."""
    for repo in repos:
        why = await ruleset_wait(conn, project_id, repo, curator_app=curator_app)
        if why is not None:
            return why
    return None


# The pull requests


@dataclass(frozen=True)
class PullInputs:
    """What the pull requests of a change read: its proposal's title, the project, the charter, the Builder's trailers
    and the plan's body."""

    title: str
    project: str
    charter: dict
    signed: str
    body: dict


async def open_pulls(engine: AsyncEngine, app, change, reader: change_tiers.GraphReader) -> str:
    """Open the pull request of each repo of built ``change`` that waits for one; the change waits for its Judge once
    none does. Its outcome: opened, signs, nothing, refused, or MOVED."""
    from evo_agents.hub.server.changes.service import job

    async with engine.begin() as conn:
        rows = await repos_of(conn, change.id)
    inputs = PullInputs(*await job.pull_inputs(engine, change))
    outcome = "opened"
    for row in rows:
        if row.state == "pr_pending":
            found = await _open_repo(engine, app, change, row, inputs, reader)
            outcome = found if found in ("refused", MOVED) else outcome
    if outcome != "opened":
        return outcome
    async with engine.begin() as conn:
        return await _after_pulls(conn, change)


async def _open_repo(engine: AsyncEngine, app, change, row, inputs: PullInputs, reader) -> str:
    """Open ``row``'s pull request (``job.opener``), read its files for signs and the tier, and keep it."""
    from evo_agents.hub.server import pulls
    from evo_agents.hub.server.changes.service import job

    target, view = github_target(row.origin), overlay(change, row)
    if target is None:
        async with engine.begin() as conn:
            await store.set_row(conn, row.id, state="open", reason="its repo has no origin on GitHub")
            await set_change(conn, change.id, state="open", reason=f"repo {row.repo} has no origin on GitHub")
        return "refused"
    opened = job.opener(engine, app, target, view, inputs.title, inputs.project, inputs.signed)
    try:
        found = await pulls.with_token(app, target.owner, target.name, "open", opened)
    except GitHubRefused as exc:
        async with engine.begin() as conn:
            why = reason_line(f"the pull request of {row.repo} could not be opened: {exc}")
            await set_change(conn, change.id, reason=why)
        return "refused"
    if found["pull"] is None:
        async with engine.begin() as conn:
            why = f"its branch changes nothing against {found['base']}: no pull request and no Judge"
            await store.set_row(conn, row.id, state="closed", reason=why)
        return "nothing"
    made = found["pull"]
    number, head = made.get("number"), (made.get("head") or {}).get("sha")
    if not judge.is_sha(head) or type(number) is not int:
        raise GitHubUnavailable("GitHub answered without the pull request's number and head")

    async def read(token: str):
        return await job.files_of(app, token, target, number, made.get("changed_files"))

    files, cut = await pulls.with_token(app, target.owner, target.name, "read", read)
    diff = diff_signs.from_github_files(files)
    signs = job.signs_of(diff, view, inputs.charter, repo_body(inputs.body, row.repo), cut)
    facts = await change_tiers.pull_facts(engine, app, view, target, diff_signs.changed_paths(diff), made, reader)
    return await _pull_kept(engine, change, row, made, signs, facts, inputs)


async def _pull_kept(engine: AsyncEngine, change, row, made: dict, signs, facts, inputs: PullInputs) -> str:
    """Keep pull request ``made`` of ``row``: the repo waits for its Judge, or with ``signs`` fails at once and puts the
    proposal at tier 3."""
    from evo_agents.hub.server.changes.service import job

    head, url = made["head"]["sha"], made.get("html_url")
    async with engine.begin() as conn:
        locked = await change_of_plan(conn, change.project_id, change.plan_id, lock=True)
        if locked is None or locked.state != "pr_pending":
            return MOVED
        values = {"pr_number": made["number"], "pr_url": url, "head_sha": head, "reason": None}
        tier = await change_tiers.retier(conn, overlay(locked, row), facts, inputs.charter)
        if signs:
            _, failures = judge.final_verdict(
                agent=None, verify=[], expected_verify=[], hidden=[], hidden_count=0, signs=signs, head_matches=True
            )
            verdict = {"signs": signs, "failures": failures[:1], "source": "hub", "head_sha": head}
            values.update(state="judged", passed=False, verdict=verdict)
            await raise_tier(conn, locked, signs)
            tier = 3
        else:
            values["state"] = "judge_pending"
        await store.set_row(conn, row.id, **values)
        if tier > locked.tier:
            await set_change(conn, change.id, tier=tier)
        await audit.record(
            conn,
            actor_id=None,
            token_id=None,
            action=job.PULL,
            target=f"{inputs.project}/{change.plan_id} change:{change.id} repo={row.repo} pr={url}",
            project_id=change.project_id,
        )
    return "signs" if signs else "opened"


async def _after_pulls(conn: AsyncConnection, change) -> str:
    """Move ``change`` on once none of its repos waits for its pull request: closed when no branch changes anything,
    waiting for its Judge, or judged and failed when signs failed every repo with a pull request."""
    locked = await change_of_plan(conn, change.project_id, change.plan_id, lock=True)
    if locked is None or locked.state != "pr_pending":
        return MOVED
    rows = await repos_of(conn, change.id)
    if any(row.state == "pr_pending" for row in rows):
        return "refused"
    if all(row.state == "closed" for row in rows):
        why = "its branch changes nothing in any of its repos: the hub opened no pull request and queued no Judge"
        await set_change(conn, change.id, state="closed", reason=why, ledger_extra={"actor": "curator"})
        return "nothing"
    if any(row.state == "judge_pending" for row in rows):
        await set_change(conn, change.id, state="judge_pending", reason=None)
        return "opened"
    await set_change(conn, change.id, **_judged_values(rows))
    return "signs"


# The Judge, one repo at a time


async def judge_queued(conn: AsyncConnection, row_id: int, run_id: int) -> None:
    await store.set_row(conn, row_id, state="judging", judge_run_id=run_id, reason=None)


def _judged_values(rows) -> dict:
    """The change's values once each of its repos is judged (or has nothing to judge): passed when each passed."""
    judged = [row for row in rows if row.state == "judged"]
    failures = [f"{row.repo}: {item}" for row in judged for item in (row.verdict or {}).get("failures") or []]
    verdict = {"repos": {row.repo: row.verdict for row in judged}, "failures": failures, "source": "judge"}
    return {"state": "judged", "passed": all(row.passed for row in judged), "verdict": verdict, "reason": None}


async def judged_repo(conn: AsyncConnection, change, values: dict) -> None:
    """The verdict ``values`` of the judge run of ``change`` (``overlay`` of its repo): on that repo's row; the change
    waits for the Judge of its next repo, or is judged once every repo is."""
    kept = {key: values[key] for key in ("passed", "verdict", "head_sha")}
    await store.set_row(conn, change.repo_row_id, state="judged", judge_key=None, **kept)
    rows = await repos_of(conn, change.id)
    moved = {"judge_key": None, "judge_attempts": 0, "reason": None}
    if "tier" in values:
        moved["tier"] = values["tier"]
    if any(row.state == "judge_pending" for row in rows):
        moved["state"] = "judge_pending"
    else:
        moved.update(_judged_values(rows))
    await set_change(conn, change.id, **moved)


# The merges, owners first


async def move_on(engine: AsyncEngine, app, curator_app, change, reader: change_tiers.GraphReader) -> str:
    """One pass of the job over due ``change`` in several repos: its pull requests, or its merges."""
    if change.state == "pr_pending":
        return await open_pulls(engine, app, change, reader)
    return await merge_repos(engine, app, curator_app, change, reader)


async def _write_checks(engine: AsyncEngine, app, change, rows) -> None:
    from evo_agents.hub.server.changes.service import job

    for row in rows:
        target = github_target(row.origin)
        if row.state != "judged" or row.check_run_id is not None or not judge.is_sha(row.head_sha) or not target:
            continue
        external = f"curator-change:{change.id}:{row.repo}"
        found = await job.check_run(app, target, row.head_sha, bool(row.passed), row.verdict or {}, external)
        async with engine.begin() as conn:
            await store.set_row(conn, row.id, check_run_id=found)


async def merge_repos(engine: AsyncEngine, app, curator_app, change, reader: change_tiers.GraphReader) -> str:
    """Write each repo's check run, then merge the pull request of each repo in plan order once its owners' are
    merged, as ``job._merge`` merges one; a repo that waits leaves its consumers waiting too."""
    from evo_agents.hub.server.changes.service import job

    async with engine.begin() as conn:
        rows = await repos_of(conn, change.id)
    await _write_checks(engine, app, change, rows)
    if not change.passed:
        failures = (change.verdict or {}).get("failures") or []
        return await job.reopen(engine, change, reason_line("the Judge did not pass it: " + "; ".join(failures)))
    async with engine.begin() as conn:
        charter = await charter_of(conn, change.project_id)
        tier, waits = await change_tiers.merge_tier(conn, change)
        body = await plan_body(conn, change.project_id, change.plan_id)
        paused = await job_store.night_paused(conn, change.project_id)
    if tier not in job.auto(charter):
        why = reason_line(waits) if tier != 0 else "the charter's auto_merge does not name tier 0"
        return await job.reopen(engine, change, why)
    if paused:
        return await job.reopen(engine, change, job.PAUSED, "paused")
    states = {row.repo: "merged" if row.state in LANDED else row.state for row in rows}
    ordered, waiting = [row.repo for row in rows], []
    for row in rows:
        if row.state != "judged":
            continue
        owners = judge.merge_waits(row.repo, states, ordered, [(owner, row.repo) for owner in row.depends_on])
        if owners:
            waiting.append(f"{row.repo} waits for the merge of {', '.join(owners)}")
            continue
        outcome, why = await _merge_repo(engine, app, curator_app, change, row, charter, body, reader)
        if outcome == "merged":
            states[row.repo] = "merged"
        elif outcome == "wait" and why:
            waiting.append(why)
        else:  # left open, paused, waiting for the gate K5 (its reason written), or moved
            return outcome
    return await _waits(engine, change, waiting)


async def _waits(engine: AsyncEngine, change, waiting: list[str]) -> str:
    """``wait`` with why as the change's reason while a repo waits; ``merged`` once none does."""
    if not waiting:
        return "merged"
    async with engine.begin() as conn:
        why = reason_line("; ".join(waiting))
        await store.set_judged_reason(conn, change.id, why)
    return "wait"


async def _merge_repo(engine: AsyncEngine, app, curator_app, change, row, charter, body, reader) -> tuple[str, str]:
    """Merge ``row``'s pull request as ``job._merge`` merges a change's: (merged, wait with why, or the outcome of
    the change left open)."""
    from evo_agents.hub.server.changes.service import job

    view = overlay(change, row)
    read = await job.merge_read(engine, app, curator_app, view, charter, repo_body(body, row.repo), reader)
    decided, reasons = judge.merge_decision(read.facts)
    if decided == "wait":
        return "wait", f"{row.repo}: {'; '.join(reasons)}"
    if decided == "open":
        return await job.reopen(engine, change, reason_line(f"{row.repo}: {'; '.join(reasons)}")), ""
    view = overlay(change, row, base_branch=read.facts.default_branch)
    act = job.merge_act(view, read)
    async with engine.begin() as conn:
        if not await job_store.unchanged(conn, change):
            return MOVED, ""
        paused = await job_store.night_paused(conn, change.project_id)
        said = None if paused else await autonomy_gates.check(conn, change.builder_run_id, "K5", **act)
    if paused or said is None:
        return await job.reopen(engine, change, job.PAUSED, "paused"), ""
    if not said.act:
        kept = "wait" if said.waits else None
        return await job.reopen(engine, change, reason_line(said.reason), kept), ""
    try:
        merged = await job.merge_at_head(app, view, read.facts.tier)
    except GitHubRefused as exc:
        return await job.reopen(engine, change, reason_line(f"GitHub refused the merge of {row.repo}: {exc}")), ""
    async with engine.begin() as conn:
        return await _record_repo_merge(conn, change, view, merged, read, act), ""


async def _record_repo_merge(conn: AsyncConnection, change, view, merged: str, read, act: dict) -> str:
    """``view``'s repo merged as commit ``merged``; the change is merged (``job.record_merge``) once each repo is."""
    from evo_agents.hub.server.changes.service import job

    if await job_store.locked_state(conn, change.id) != "judged":  # an agent or a person moved it meanwhile
        return MOVED
    await store.set_row(conn, view.repo_row_id, state="merged", merged_at=func.now(), merge_sha=merged, reason=None)
    if all(row.state in LANDED for row in await repos_of(conn, change.id)):
        await job.record_merge(conn, view, merged, read, act)
        log.info("curator change merged in its repos", extra={"change_id": change.id, "sha": merged})
        return "merged"
    project = await job_store.project_name(conn, change.project_id)
    await audit.record(
        conn,
        actor_id=None,
        token_id=None,
        action=job.MERGE,
        target=f"{project}/{change.plan_id} change:{change.id} repo={view.repo} pr={view.pr_url} sha={merged}",
        project_id=change.project_id,
    )
    log.info("curator change merged in one repo", extra={"change_id": change.id, "repo": view.repo, "sha": merged})
    return "merged"
