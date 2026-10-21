"""What follows a merge of the hub's merge queue on GitLab (plan gitlab-merge-queue, criterion a6): the checks of the
target branch after it, the PROD release under the policy's ``prod_release``, the culprit of a check that fails, its
revert and the rollback of PROD, the audit samples and the figures. Pure functions over plain values, standard library
only; ``evo_agents.hub.server.post_merge`` runs them in the job hub.merge_queue.

**The checks.** Each merge has its commit on the target branch (``after_sha``) and the pipeline that commit runs there.
The UAT check is the policy's job ``jobs.uat_check`` of that pipeline; a repo that names none has its pipeline as its
check, and no check after deploy. The PROD check is ``jobs.prod_check`` after ``jobs.prod_deploy``. A manual job the hub
plays; a job that fails, or is cancelled, runs again once to rule a flake out (``job_step``, ``pipeline_step``).
Each check ends with one of RESULTS: ``passed``; ``failed``, failing again after its retry; or ``none``, no result
(cancelled or skipped after its retry, a declared job missing from the pipeline, or still running after
``merge.pipeline_minutes``).

**The culprit** (``settle``; the design's step 6 of "Sau merge"). The last good commit of a target branch is the newest
merge whose check passed: every merge before it is cleared with it. Of the merges after it, a check that failed or has
no result points at suspects, the merges from the oldest after the last good one up to it. The oldest suspect decides:
its own check failed, and it is the culprit, reverted (the commit before it passed); its check still runs, and the
hub waits; its check has no result, and the hub cannot tell which suspect broke the branch, so it reverts none and asks
an admin, listing the suspects. While the revert of a culprit has no result of its own, no other suspect is blamed:
its check says whether those merged after the culprit are clean. For PROD the same holds of the deploys: one deploy
carries the merges whose own UAT check did not pass alone, or that came after a culprit not yet reverted, and a PROD
check that fails blames the deploy's merge only when it carried none (``prod_culprit``).

**PROD** (``deploy_target``). Deploys go in merge order, one at a time on a branch: the next starts once the PROD check
of the one before ended. ``immediate`` deploys the oldest merge whose UAT check passed; ``after_uat`` the newest whose
UAT check passed ``after_uat_hours`` ago; ``manual`` the newest an admin approved. A merge that comes after a culprit
whose revert is not merged yet is never deployed alone: the revert's deploy carries it.

**The revert** opens ``evo-revert/<iid>`` from the target branch with a revert of each commit of the culprit, the
newest first (``newest_first``), and its merge request goes first in the queue. It passes the pipeline and a check that
its diff undoes the merged diff exactly (``undo_problem``), never a Judge or a sample.

**Samples and figures.** A merge request the queue merged, a revert aside, is drawn for an audit sample with the
policy's ``audit.sample_rate`` (``sampled``); an admin answers it ``ok`` or ``faulty``. ``figures`` counts, for each
repo, agent profile (a person's merge request is ``person``) and model: the merges, the culprits reverted, the rollbacks
of PROD, the Judge's passes found wrong (a merge request it passed that was reverted, rolled back or sampled faulty),
and the time spent in the queue and to the checks.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from evo_agents.hub import judge

RESULTS = ("passed", "failed", "none")
SAMPLE_RESULTS = ("ok", "faulty")
UAT_STATES = ("merged", "verifying")  # merged: waits for its commit's pipeline; verifying: reads its UAT check
CULPRIT_STATES = ("reverting", "reverted")  # a merge the hub reverts, or reverted
FLIGHT_STATES = ("releasing", "rolling_back")  # a deploy or a rollback of PROD may run
REVERT = "revert"  # merge_queue.source of the revert of a culprit
REVERT_BRANCH = "evo-revert/{iid}"
ROLLBACK_VARIABLE = "EVO_ROLLBACK_TO_SHA"  # the commit the rollback job puts PROD back at
GREEN = "success"
MANUAL = "manual"
PIPELINE_GREEN = frozenset({GREEN, MANUAL})  # a pipeline whose manual jobs alone wait has passed
WAITING = frozenset({"created", "waiting_for_resource", "preparing", "pending", "running", "scheduled"})
RETRYABLE = frozenset({"failed", "canceled"})
MAX_SUSPECTS = 50  # named in an admin's decision
DAY_SECONDS = 86400


# One check


def job_step(status: str | None, *, played: bool, retried: bool) -> str:
    """What the hub does next with a job of a merge's pipeline whose status is ``status`` (None when the pipeline
    has no such job): ``play`` a manual job it has not played, ``wait``, ``retry`` one that failed or was cancelled
    and has not run again, or the check's result (RESULTS)."""
    if status is None:
        return "none"
    if status == MANUAL:
        return "wait" if played else "play"
    if status == GREEN:
        return "passed"
    if status in WAITING:
        return "wait"
    if status in RETRYABLE and not retried:
        return "retry"
    return "failed" if status == "failed" else "none"


def pipeline_step(status: str | None, *, retried: bool) -> str:
    """What the hub does next with the pipeline of a merge when its repo names no UAT check job: ``wait`` (None: no
    pipeline yet), ``retry``, or the result. A pipeline whose manual jobs alone wait (the deploy) has passed."""
    if status is None or status in WAITING:
        return "wait"
    if status in PIPELINE_GREEN:
        return "passed"
    if status in RETRYABLE and not retried:
        return "retry"
    return "failed" if status == "failed" else "none"


def latest_jobs(jobs: Iterable) -> dict:
    """The last try of each job of a pipeline, by name (a retry has a higher id)."""
    found: dict = {}
    for job in jobs:
        if job.name not in found or job.id > found[job.name].id:
            found[job.name] = job
    return found


# The culprit


@dataclass(frozen=True)
class Settled:
    """What the UAT checks of a target branch's merges, in merge order, settle."""

    passed: tuple[int, ...] = ()  # their own check passed
    cleared: tuple[tuple[int, int], ...] = ()  # (a merge, the later merge whose passed check cleared it)
    culprit: int | None = None  # the oldest suspect, whose own check failed: the hub reverts it
    suspects: tuple[int, ...] = ()  # the oldest suspect's check has no result: an admin picks the one to revert


def settle(results: Iterable[tuple[int, str | None]], *, revert_pending: bool = False) -> Settled:
    """Settle the merges of a target branch still in their UAT check: ``results`` are (row, its own result or None
    while its check runs), in merge order; ``revert_pending`` while a culprit's revert has no result of its own."""
    rows = list(results)
    last = max((i for i, (_, result) in enumerate(rows) if result == "passed"), default=None)
    passed, cleared = [], []
    if last is not None:
        good = rows[last][0]
        for row_id, result in rows[:last]:
            if result == "passed":
                passed.append(row_id)
            else:
                cleared.append((row_id, good))
        passed.append(good)
    rest = rows[last + 1 :] if last is not None else rows
    failing = [i for i, (_, result) in enumerate(rest) if result in ("failed", "none")]
    if not failing or revert_pending or rest[0][1] is None:
        return Settled(tuple(passed), tuple(cleared))
    if rest[0][1] == "failed":
        return Settled(tuple(passed), tuple(cleared), culprit=rest[0][0])
    return Settled(tuple(passed), tuple(cleared), suspects=tuple(row_id for row_id, _ in rest[: failing[-1] + 1]))


def prod_culprit(target: int, carried: Iterable[int], result: str) -> int | None:
    """The culprit of a deploy of ``target`` whose PROD check ended ``result`` (failed or none): ``target`` when its own
    check failed and the deploy carried no other merge; None when the hub cannot tell."""
    return target if result == "failed" and not list(carried) else None


# PROD


@dataclass(frozen=True)
class Candidate:
    """A merge waiting for PROD (``releasing``), in merge order."""

    id: int
    passed: bool  # its own UAT check passed: its pipeline may deploy
    behind: bool  # merged after a culprit not yet reverted: only a deploy after the revert carries it
    eligible: bool  # after_uat: its UAT check passed long enough ago; manual: an admin approved it


def deploy_target(candidates: Iterable[Candidate], release: str) -> tuple[int, list[int]] | None:
    """(the merge whose pipeline the hub deploys, the merges before it its deploy carries), or None while no merge
    may be deployed. ``release`` is the policy's ``prod_release``."""
    rows = list(candidates)
    able = [i for i, row in enumerate(rows) if row.passed and not row.behind]
    if release != "immediate":
        able = [i for i in able if rows[i].eligible]
    if not able:
        return None
    index = able[0] if release == "immediate" else able[-1]
    return rows[index].id, [row.id for row in rows[:index]]


# The revert


def newest_first(commits: Iterable, head: str | None) -> list[str]:
    """The commits of a merge request, the newest first: along the first parents from ``head``, then any GitLab listed
    that the walk did not reach, in its order."""
    listed = [commit for commit in commits if getattr(commit, "id", None)]
    by_id = {commit.id: commit for commit in listed}
    order, at = [], head
    while at in by_id and at not in order:
        order.append(at)
        parents = by_id[at].parent_ids
        at = parents[0] if parents else None
    return order + [commit.id for commit in listed if commit.id not in order]


def _sides(diff: judge.FileDiff) -> tuple[str | None, str | None]:
    if diff.status == "added":
        return None, diff.path
    if diff.status == "removed":
        return diff.old_path or diff.path, None
    return diff.old_path or diff.path, diff.path


def _patch(files: Iterable[judge.FileDiff], *, undone: bool) -> list:
    key = []
    for diff in files:
        before, after = _sides(diff)
        added, removed = [text for _, text in diff.added], [text for _, text in diff.removed]
        key.append([after, before, removed, added] if undone else [before, after, added, removed])
    return sorted(key, key=repr)


def undo_problem(revert: list[judge.FileDiff], merged: list[judge.FileDiff]) -> str | None:
    """Why the diff ``revert`` does not undo exactly the diff ``merged`` (each file's lines added and removed, swapped,
    and its path before and after, swapped); None when it does."""
    if not merged:
        return "GitLab lists no diff of the merge request it reverts"
    if not all(diff.readable for diff in [*revert, *merged]):
        return "its diff, or the one it reverts, holds a file the hub cannot read: a person checks it"
    if _patch(revert, undone=False) != _patch(merged, undone=True):
        return "its diff is not the exact inverse of the diff merged: a person checks it"
    return None


# Samples


def sampled(rate: float, draw: float) -> bool:
    """Whether a merge is drawn for an audit sample, ``draw`` being uniform in [0, 1)."""
    return draw < rate


# Figures


def agent_of(source: str, profile: str | None) -> str | None:
    """Who a merge request's figures count it for: its run's agent profile, or ``person`` for a person's."""
    return "person" if source == "label" else profile


def _mean(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 1) if values else None


def figures(rows: Iterable[Mapping], *, rolled_back: set[int], faulty: set[int]) -> list[dict]:
    """The figures of the merges ``rows`` (id, repo, source, profile, model, state, judged: the Judge passed it,
    waited, to_uat, to_prod: seconds or None), for each repo, agent and model; ``rolled_back`` and ``faulty`` name the
    rows PROD was rolled back for and those a sample found faulty."""
    groups: dict[tuple, dict] = {}
    for row in rows:
        key = (row["repo"], agent_of(row["source"], row["profile"]), row["model"])
        group = groups.setdefault(key, {"rows": [], "waited": [], "to_uat": [], "to_prod": []})
        group["rows"].append(row)
        for name in ("waited", "to_uat", "to_prod"):
            if row[name] is not None:
                group[name].append(row[name])
    found = []
    for (repo, agent, model), group in sorted(groups.items(), key=lambda item: tuple(map(str, item[0]))):
        kept = group["rows"]
        culprits = {row["id"] for row in kept if row["state"] in CULPRIT_STATES}
        rolled = {row["id"] for row in kept if row["id"] in rolled_back}
        wrong = {row["id"] for row in kept if row["judged"]} & (culprits | rolled | faulty)
        found.append(
            {
                "repo": repo,
                "agent": agent,
                "model": model,
                "merged": len(kept),
                "reverted": len(culprits),
                "rolled_back": len(rolled),
                "judge_misses": len(wrong),
                "wait_seconds": _mean(group["waited"]),
                "wait_seconds_max": max(group["waited"], default=None),
                "uat_seconds": _mean(group["to_uat"]),
                "prod_seconds": _mean(group["to_prod"]),
            }
        )
    return found


# What a person reads


def revert_title(iid: int, title: str | None) -> str:
    return f"Revert !{iid}: {' '.join(str(title or '').split())}"[:255]


def revert_description(project: str, iid: int, target: str, why: str, shas: list[str]) -> str:
    commits = "\n".join(f"- {sha}" for sha in shas)
    return (
        f"Opened by the evo-agents hub's merge queue of project {project}: it reverts !{iid} on {target}, the newest "
        f"commit first, because {why}.\n\n{commits}\n\nThe queue merges it first, once its pipeline is green and its "
        "diff undoes the merged one exactly."
    )


def iids(values: Iterable[int | None]) -> str:
    """Merge requests as people name them: ``!3, !4``."""
    return ", ".join(f"!{value}" for value in values if value is not None)
