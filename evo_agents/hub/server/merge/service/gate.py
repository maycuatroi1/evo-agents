"""The hub's merge queue on GitLab, up to its gate before merge (plan gitlab-merge-queue, criterion a4; the rules are
``evo_agents.hub.merge_gate``'s): the job that reads the merge requests of each repo an autonomy policy has in mode
auto, opens those of the hub's runs, runs the gates on each one and queues its Judges; the hand-off of the Curator's
merge requests its Judge passed. ``evo_agents.hub.server.merge_judge`` holds the judge runs; ``service.gate_pass`` the pass over one repo, and
``repository.gate`` the queries of the rows and their ledger.

``advance`` is one pass of the job ``hub.merge_queue``. For each project with a policy (or with merge requests still
in its queue), and each repo of it: a repo in mode off has its merge requests leave the queue (``closed``), open for a
person again. A repo in mode auto that no forge secret of the project covers is left as it is: the hub cannot read
GitLab for it, and nothing of it moves. Otherwise the hub opens the forge secret (``forge.forge_token``) and, through
the GitLab client, for as long as the pass needs it:

- opens the merge request of each plan whose steps in the repo are all done, from the branch the plan names for it,
  once a plan run or step run of the plan ended done since the repo went to mode auto (``merge_gate.auto_since``), and
  of each task run that ended done since then and pushed its branch of the repo; or takes the merge request open of
  that branch already. GitLab refusing is kept as the row's reason, and tried again after OPEN_RETRY. Each opening is
  audited (``merge.open``).
- takes each merge request labeled ``merge.opt_in_label`` by a member of role Developer or above (resource label
  events), and finds the merge request of each Curator's change handed to it (``curator_judged``) by its branch.
- reads each merge request of the queue again: out of scope, it leaves the queue; a new head sends it back to the
  first gate; in ``gating`` it runs gates 0 to 3 and computes the risk (gate 6) with the knowledge graph, read as the
  admin who wrote the policy; in ``judging`` it queues the next judge run (``merge_judge.queue_judge``) or, after
  JUDGE_ATTEMPTS runs without a verdict, blocks it.
- follows each merge of the repo's target branch on: its checks, PROD, the rollback and the revert of a culprit
  (``evo_agents.hub.server.merge.service.post_merge``), whose revert it gates by its pipeline and by its diff undoing the one
  merged, with no Judge;
- moves the turn of the repo's target branch on: the merge requests ``ready`` are rebased, tested and merged one at a
  time, a revert first (``evo_agents.hub.server.merge.service.turn``).

The breaker of the repo (``evo_agents.hub.server.merge.service.breaker``) trips on what follows a merge; while it is tripped,
the merge requests at their gates wait, those ready or in their turn are paused, and a revert alone moves on.

A gate that does not pass leaves the merge request ``gating`` with why when it waits for something on GitLab (its
pipeline, the project's settings, the end of its draft), or ``blocked`` with why; a blocked one moves only after a
new push, or, held at gate 3 for a person, once an admin passes it (``evo_agents.hub.server.merge.service.passes``). Each new
reason, and each gate passed at a head, is a line of the ledger (merge_events), and the hub
comments on the merge request once for each reason that blocks it or each setting it misses; held by a Judge, the
comment names the Judge's findings open, each one whole the first time and by its id after that. A new patch drops the
Judges' verdicts and keeps their findings (``findings.carried``), for the next Judge to mark. GitLab failing leaves
the repo for the next pass. A merge request out of scope, or of a project without a policy, is never written to, as
before the policy (criterion a9). ``evo_agents.hub.server.merge.routes.view`` shows the queue.
"""

from __future__ import annotations

import logging
from collections import Counter

from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import approval_heads, autonomy, judge, merge_gate
from evo_agents.hub.server.autonomy.repository.policies import project_repos
from evo_agents.hub.server.autonomy.service import gates as autonomy_gates
from evo_agents.hub.server.changes.service import tiers as change_tiers
from evo_agents.hub.server.forge import forge_token, has_forge
from evo_agents.hub.server.gitlab import GitLabUnavailable, MergeRequest, project_of
from evo_agents.hub.server.merge.models.gate import Project
from evo_agents.hub.server.merge.repository import gate as queries
from evo_agents.hub.server.merge.repository.gate import charter_of, plan_body, record, row_of, set_row

log = logging.getLogger(__name__)


# The rows


async def hold(conn: AsyncConnection, row, gate: int, why: str, *, blocked: bool = False, sha: str | None = None):
    """Keep merge request ``row`` at ``gate``: ``gating`` while it waits, ``blocked`` when it is; a reason it did not
    have before is a line of the ledger."""
    reason = merge_gate.gate_reason(gate, why)
    state = "blocked" if blocked else "gating"
    if reason == row.reason and state == row.state:
        return
    await set_row(conn, row.id, state=state, reason=reason)
    detail = {} if blocked else {"waits": True}
    await record(conn, row, "gate", gate=gate, passed=False, reason=reason, sha=sha or row.head_sha, detail=detail)


async def wait(conn: AsyncConnection, row, why: str) -> None:
    """Keep ``row`` where it is, with why it waits for something on GitLab that is no gate (its draft, GitLab)."""
    reason = merge_gate.reason_line(merge_gate.WAITS + why)
    if reason != row.reason:
        await set_row(conn, row.id, reason=reason)


async def close(conn: AsyncConnection, row, why: str, mr: MergeRequest | None = None) -> None:
    """``row`` leaves the queue: its merge request is a person's again, or it is merged or closed on GitLab; ``mr``,
    as GitLab has it now, puts its state there and the commit of a merge in the ledger's line
    (``merge_gate.closed_detail``), so the Loop counts a merge a person made outside the queue."""
    reason = merge_gate.reason_line(why)
    await set_row(conn, row.id, state="closed", reason=reason, judge_run_id=None, judge_key=None)
    detail = merge_gate.closed_detail(mr.state, mr.merge_commit_sha, mr.sha) if mr is not None else {}
    await record(conn, row, "closed", reason=reason, sha=row.head_sha, detail=detail)


async def ready(conn: AsyncConnection, row, why: str) -> bool:
    """Every gate passed at the head of ``row``: it waits for its turn on the target branch (the queue's), at the place
    it had when its turn sent it back to its gates (``queued_at`` kept), else last. Whether it did: K5 of the
    principal of the run that made it (``autonomy_gates``) keeps it judging while the principal is asked, and blocks it
    on a deny or a refusal."""
    said = await autonomy_gates.check(conn, row.run_id, "K5", **merge_act(row))
    if not said.act:
        reason = merge_gate.reason_line(merge_gate.WAITS + said.reason if said.waits else said.reason)  # pyright: ignore[reportArgumentType, reportOperatorIssue]
        if reason != row.reason:
            await set_row(conn, row.id, state="judging" if said.waits else "blocked", reason=reason)
        return False
    await queries.set_ready(conn, row.id)
    await record(conn, row, "queued", reason=why, sha=row.head_sha)
    return True


def merge_act(row) -> dict:
    """The act of merging ``row``, as the gates of autonomy name it."""
    return {
        "key": f"merge {row.repo}!{row.iid}",
        "what": f"merge !{row.iid} of {row.repo} into {row.target_branch}",
        "repo": row.repo,
        "risk": bool((row.risk or {}).get("high")),
        "approval": approval_heads.approval(row.head_sha, None, f"{row.repo}!{row.iid}"),
    }


def protected_of(charter: dict) -> list[str]:
    return [item for item in charter.get("protected_paths") or [] if isinstance(item, str)]


# What the queue takes


async def takes(conn: AsyncConnection, project_id: int, repo: str, origin: str | None) -> tuple[bool, str | None]:
    """(whether the queue takes the merge requests of ``repo``, its target branch): its policy has it in mode auto
    and a forge secret of the project covers its origin on GitLab."""
    repos = await project_repos(conn, project_id)
    body = await queries.newest_policy(conn, project_id)
    policy = autonomy.repo_policy(body, repo, repos.get(repo))
    if policy["mode"] != autonomy.AUTO or not origin or project_of(origin) is None:
        return False, policy[autonomy.TARGET_BRANCH]
    return await has_forge(conn, project_id, origin), policy[autonomy.TARGET_BRANCH]


async def curator_judged(engine: AsyncEngine, change) -> tuple[str, str]:
    """(outcome, reason) of a judged change of the Curator on GitLab, for the job curator.changes: the merge queue
    takes it when the Judge passed it and its repo is in mode auto with a forge secret (``judge.merge_decision``), as
    a row of source builder whose merge request the job finds by its branch; else it stays open, as before."""
    if not change.passed:
        return "open", "the Judge did not pass it: " + "; ".join((change.verdict or {}).get("failures") or [])
    async with engine.begin() as conn:
        queue, target = await takes(conn, change.project_id, change.repo, change.origin)
        facts = judge.MergeFacts(
            forge=change.forge or "gitlab",
            tier=change.tier,
            auto_merge=(),
            repo_checked=False,
            passed=bool(change.passed),
            judged_sha=change.head_sha,
            pr_state=None,
            pr_merged=False,
            pr_head=None,
            pr_base=None,
            default_branch=None,
            signs=(),
            ci="none",
            queue=queue and bool(target),
        )
        decided, reasons = judge.merge_decision(facts)
        if decided != "queue":
            return "open", "; ".join(reasons)
        if not await queries.change_queued(conn, change.id):
            profile, model = await queries.builder(conn, change.builder_run_id)
            body = await plan_body(conn, change.project_id, change.plan_id) or {}
            steps = [str(step.get("id")) for step in body.get("steps") or [] if isinstance(step, dict)]
            values = {
                "project_id": change.project_id,
                "repo": change.repo,
                "source": "builder",
                "target_branch": target,
                "source_branch": change.branch,
                "run_id": change.builder_run_id,
                "plan_id": change.plan_id,
                "steps": steps,
                "change_id": change.id,
                "profile": profile,
                "model": model,
                "reason": f"waits for the merge request of {change.branch} into {target}",
            }
            row_id = await queries.insert_row(conn, values)
            row = await row_of(conn, row_id)
            await record(conn, row, "queued", reason=f"the Curator's change #{change.id}, its Judge passed it")
    return "queued", f"handed to the merge queue of {change.repo}: {reasons[0]}"


# The job


async def _project_ids(engine: AsyncEngine) -> list[int]:
    async with engine.begin() as conn:
        return sorted(await queries.project_ids(conn))


async def _load(conn: AsyncConnection, project_id: int) -> Project:
    name = await queries.project_name(conn, project_id)
    revisions = await queries.policy_revisions(conn, project_id)
    repos = await project_repos(conn, project_id)
    newest = revisions[0].body if revisions else None
    return Project(
        id=project_id,
        name=name,
        policy=autonomy.effective(newest, repos),
        repos=repos,
        revisions=[(row.created_at, row.body) for row in revisions],
        owner_id=revisions[0].created_by if revisions else None,
        charter=await charter_of(conn, project_id),
    )


async def advance(engine: AsyncEngine, sealer, *, blobs=None, data_dir=None, transport=None) -> dict:
    """One pass of the job hub.merge_queue up to the gate (see the module's docstring); how many merge requests ended
    in each outcome. ``transport`` is the GitLab client's, for the tests."""
    outcomes: Counter = Counter()
    reader = change_tiers.GraphReader.at(blobs, data_dir)
    for project_id in await _project_ids(engine):
        try:
            async with engine.begin() as conn:
                project = await _load(conn, project_id)
            for repo, settings in project.policy[autonomy.REPOS].items():
                await _repo(engine, sealer, project, repo, settings, outcomes, reader, transport)
        except Exception:  # one project failing leaves the others their pass
            log.exception("a project's merge queue failed", extra={"project_id": project_id})
            outcomes["failed"] += 1
    return dict(sorted(outcomes.items()))


async def _repo(engine, sealer, project: Project, repo: str, settings: dict, outcomes, reader, transport) -> None:
    facts = project.repos.get(repo)
    origin = facts.origin if facts is not None else None
    target = settings[autonomy.TARGET_BRANCH]
    if settings["mode"] != autonomy.AUTO or not origin or project_of(origin) is None or not target:
        async with engine.begin() as conn:
            waiting = (*merge_gate.QUEUED_STATES, "paused")  # paused: its breaker tripped
            for row in await queries.waiting_rows(conn, project.id, repo, waiting):
                await close(conn, row, f"the policy has {repo} in mode {settings['mode']}: a person merges it")
                outcomes["closed"] += 1
        return
    async with engine.begin() as conn:
        token = await forge_token(conn, sealer, project.id, origin)
    if token is None:  # nothing of the repo moves, and its merge requests stay as they are on GitLab
        outcomes["no_forge"] += 1
        return
    from evo_agents.hub.server.merge.service.gate_pass import RepoPass  # it imports this module

    try:
        async with token.gitlab(transport=transport) as gl:
            found = RepoPass(engine, project, repo, settings, token.project.path, gl, reader, outcomes)
            await found.run()
    except GitLabUnavailable as exc:
        log.warning("a repo of the merge queue waits for GitLab", extra={"project": project.name, "why": str(exc)})
        outcomes["gitlab"] += 1


async def hold_judging(conn: AsyncConnection, row, gate: int, why: str) -> None:
    """Keep ``row`` judging, with why its judge run waits; a new reason is a line of the ledger."""
    reason = merge_gate.gate_reason(gate, f"waits: {why}")
    if reason == row.reason:
        return
    await set_row(conn, row.id, reason=reason)
    await record(conn, row, "gate", gate=gate, passed=False, reason=reason, sha=row.head_sha, detail={"waits": True})


async def judge_run_title(conn: AsyncConnection, run_id: int, plan_id: str | None) -> str:
    """The title of the notice a failed judge run sends its owner: the Curator's names its plan, the queue's the merge
    request it judged (``changes.judge_ended``)."""
    if plan_id:
        return f"Judge run #{run_id} of {plan_id} failed"
    found = await queries.judged_by(conn, run_id)
    what = f"merge request !{found.iid} of {found.repo}" if found is not None else "a merge request of the merge queue"
    return f"Judge run #{run_id} of {what} failed"


def next_round(verdict: dict | None, risk: dict | None) -> str:
    """What the Judges' ``verdict`` for a patch whose risk is ``risk`` calls for next: ``first`` (the Judge),
    ``second`` (the second Judge, for a high risk), ``ready`` (both said what they had to), or ``failed``. A round its
    signs held at gate 3 (``held_by_signs``) is still to come."""
    first, second = (verdict or {}).get("first"), (verdict or {}).get("second")
    if first is None or held_by_signs(first):
        return "first"
    if not first.get("passed"):
        return "failed"
    if not (risk or {}).get("high"):
        return "ready"
    if second is None or held_by_signs(second):
        return "second"
    return "ready" if second.get("passed") else "failed"


def held_by_signs(entry: dict) -> bool:
    """Whether a Judge's ``entry`` of the verdict held the merge request at gate 3: the worker's reading of the diff
    showed signs of score hacking no Judge weighed (``merge_judge._close_gates``), so no later gate read it and its
    agent most often never started. Once an admin passes that hold, the Judge judges the patch: the entry does not hold
    for it (before, the second pass of such a hold failed gate 5 on the entry, and only a merge by hand moved it)."""
    return bool(entry.get("signs")) and entry.get("signs_weighed") is not True
