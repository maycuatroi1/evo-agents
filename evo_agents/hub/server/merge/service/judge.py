"""The judge runs of the hub's merge queue on GitLab (plan gitlab-merge-queue, criterion a4: gates 4, 5 and 7): the
run the job queues for a merge request whose gates 0 to 3 passed, what its claim and its prompt hold, what it reads,
and its verdict. ``evo_agents.hub.merge_gate`` holds the rules, ``evo_agents.hub.server.merge.service.gate`` the job.

A judge run of the queue is a run of kind ``judge`` on no plan and no schedule (schema 0081), which the merge request
it judges names (merge_queue.judge_run_id). The admin who wrote the policy's newest revision dispatches it, so it
reads the repo with that admin's git secret and the project's graph as them; it goes to the nodes of the pool of the project's default agent, whose daemon must say it judges merge requests
(``runs.MERGE_JUDGE``, the claim's gate ``claims.merge_judge_gate``); it runs on Claude Code with the model ``judges.merge_judge_model`` picks, and never reads the
Builder's transcript. A project has one judge run active at a time, the Curator's included: another waits for it.

Its claim names the merge request as a Curator's judge run names a change (``merge_claim``: the branch, the target
branch, the head to judge, its URL, and the run's own key, kept as a SHA-256), so the worker guards it as one, and its
prompt is ``merge_gate.build_prompt`` (``merge_prompt``). GET /v1/worker/runs/{id}/merge-judge, with the run's key,
hands it the verify of the plan's steps in the repo and the hidden checks of the project's charter, which only it
reads. POST /v1/worker/runs/{id}/merge-verdict, with the key, takes its verdict: the worker's own reading of the diff
(gate 3 again, unless an admin's pass covers the head judged: the claim said ``signs_passed`` and the Judge weighed the
signs against the plan), the checks (gate 4), and the Judge (gate 5, or 7 for the second Judge), every dimension
passed. A verdict whose signs held the merge request at gate 3 does not hold for the patch
(``merge_gate.held_by_signs``): after an admin's pass the Judge judges it. The first Judge passing records the risk
(gate 6): a normal risk lets the merge request into the queue (``ready``), a high one has the job queue the second
Judge. Each gate is a line of the ledger, the Judge's verdict another (``judged``: its run, its model, each dimension,
the patch, the findings it raised and its marks of earlier ones); a gate not passed blocks the merge request with
why.

The findings (``evo_agents.hub.findings``) live in the verdict (merge_queue.verdict ``findings``), which a new patch
hands on (``findings.carried``): the prompt of a later round lists those still open on its Judge's dimensions, and the
verdict's marks of them are checked here (``merge_gate.judge_round``), not taken on the Judge's word.
"""

from __future__ import annotations

import logging
import secrets

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import findings, merge_gate
from evo_agents.hub.server import audit
from evo_agents.hub.server.forge.models.forges import GITLAB
from evo_agents.hub.server.merge.models.judge import MAX_DIMENSIONS, MergeJudgeInputs, MergeVerdict, MergeVerdictIn
from evo_agents.hub.server.merge.repository import check as check_rows
from evo_agents.hub.server.merge.repository import judge as repository
from evo_agents.hub.server.merge.service import check, judging
from evo_agents.hub.server.merge.service import gate as queue
from evo_agents.hub.server.merge.service import passes as merge_pass
from evo_agents.hub.server.run_state import notify_queued, write_event
from evo_agents.hub.server.security import Principal

log = logging.getLogger(__name__)


# Queueing a judge run


async def queue_judge(conn: AsyncConnection, project, row, *, second: bool) -> str | None:
    """Queue the judge run of merge request ``row`` of ``project`` (``merge_gate.Project``), its second Judge when
    ``second``; None once queued, else why it waits."""
    run_id, model, chosen = await judging.queue_judge_run(
        conn,
        project,
        repo=row.repo,
        branch=row.source_branch,
        title=f"{'Second Judge' if second else 'Judge'} of !{row.iid} of {row.repo}: {row.title or ''}",
        builder_model=row.model,
        built=row.source != "label",
        second=second,
    )
    if run_id is None:
        return chosen
    which = "the second Judge" if second else "the Judge"
    await queue.set_row(conn, row.id, judge_run_id=run_id, judge_key=None, reason=None)
    head = (row.head_sha or "")[:12]
    text = (
        f"Queued by the merge queue of project {project.name} as {which} of merge request !{row.iid} of {row.repo} "
        f"at {head}, on Claude Code ({model or 'the default model'}: {chosen}). It reads the merge request, its diff, "
        "the plan's verify and the project's hidden checks, never the Builder's transcript."
    )
    await write_event(conn, run_id, {"text": text, "merge_request": row.iid, "queue_id": row.id})
    await notify_queued(conn, run_id)
    await audit.record(
        conn,
        actor_id=project.owner_id,
        token_id=None,
        action=audit.MERGE_JUDGE,
        target=f"{project.name}/{row.repo} !{row.iid} run:{run_id} {'second' if second else 'first'} model={model}",
        project_id=project.id,
    )
    log.info("merge judge queued", extra={"project": project.name, "run_id": run_id, "queue_id": row.id})
    return None


# The claim and the prompt


async def merge_claim(conn: AsyncConnection, view) -> dict | None:
    """What the claim of judge run ``view`` tells its worker when it judges a merge request of the queue, as a judge
    run of the Curator is told of its change (``changes.run_curator``), with a new key of its own; None for any other
    run."""
    if view.kind != "judge" or view.plan_id:
        return None
    row = await repository.judged_entry(conn, view.id, lock=True)
    if row is None:
        return await check.claim(conn, view)
    charter = await queue.charter_of(conn, row.project_id)
    found = {
        "role": "judge",
        "protected_paths": queue.protected_of(charter),
        "branch": row.source_branch,
        "forge": GITLAB.name,
        "base_branch": row.target_branch,
        "head_sha": row.head_sha,
        "pr_url": row.web_url,
        # an admin passed gate 3 at this head: the Judge weighs the signs against the plan, not failing them unjudged
        "signs_passed": await merge_pass.covers(conn, row) is not None,
    }
    if row.state == "judging":
        key = secrets.token_urlsafe(32)
        await queue.set_row(conn, row.id, judge_key=judging.digest(key))
        found["judge_key"] = key
    return found


async def _plan_and_task(conn: AsyncConnection, row) -> tuple[dict | None, str | None]:
    plan = await queue.plan_body(conn, row.project_id, row.plan_id) if row.source in ("plan", "builder") else None
    task = None
    if row.source == "task" and row.run_id is not None:
        task = await repository.run_request(conn, row.run_id)
    return plan, task


async def merge_prompt(conn: AsyncConnection, view) -> str | None:
    """The prompt of judge run ``view`` when it judges a merge request of the queue; None for any other run."""
    if view.kind != "judge" or view.plan_id:
        return None
    row = await repository.judged_entry(conn, view.id)
    if row is None:
        return await check.prompt(conn, view)
    plan, task = await _plan_and_task(conn, row)
    mr = {
        "iid": row.iid,
        "title": row.title,
        "description": row.description,
        "web_url": row.web_url,
        "source_branch": row.source_branch,
        "target_branch": row.target_branch,
    }
    return merge_gate.build_prompt(
        view.project,
        repo=row.repo,
        folder=None,
        mr=mr,
        base=f"origin/{row.target_branch}",
        head=row.head_sha,
        plan=plan,
        task=task,
        second=queue.next_round(row.verdict, row.risk) == "second",
        risk=(row.risk or {}).get("reasons") or [],
        earlier=findings.ledger_of(row.verdict),
    )


# The judge run's side


async def _held(conn: AsyncConnection, user: Principal, run_id: int, key: str | None):
    """(run row, queue row) of the judge run ``run_id`` of the queue the worker of ``user`` holds, whose merge request
    waits for its verdict, asked with the run's key; 404 for any other run, 409 for a merge request that waits for no
    verdict of it, 403 without the key."""
    run = await judging.held_judge(conn, user, run_id)
    row = await repository.judged_entry(conn, run_id, lock=True)
    if row is None or row.state != "judging":
        raise HTTPException(409, f"run {run_id} judges no merge request that waits for its verdict")
    judging.require_key(run_id, key, row.judge_key)
    return run, row


async def inputs(engine: AsyncEngine, user: Principal, run_id: int, judge_key: str | None) -> MergeJudgeInputs:
    """What the judge run ``run_id`` of the queue or of a merge check, which the worker of ``user`` holds, reads,
    with the run's own key ``judge_key``."""
    async with engine.begin() as conn:
        if await check_rows.judged_check(conn, run_id) is not None:  # a pull request on github.com
            return await check.inputs(conn, user, run_id, judge_key)
        run, row = await _held(conn, user, run_id, judge_key)
        charter = await queue.charter_of(conn, row.project_id)
        hidden = judging.hidden_of(charter)
        plan, _ = await _plan_and_task(conn, row)
        verify = merge_gate.verify_of(plan, row.repo)
        verdict = {**(row.verdict or {}), "hidden_count": len(hidden), "fingerprint": row.fingerprint}
        await queue.set_row(conn, row.id, verdict=verdict)
        text = (
            f"The Judge read its inputs: {len(verify)} verify command(s) of the plan and {len(hidden)} hidden "
            "check(s) of the project, which only this run reads."
        )
        await write_event(conn, run_id, {"text": text, "queue_id": row.id})
    return MergeJudgeInputs(
        queue_id=row.id,
        repo=row.repo,
        iid=row.iid,
        branch=row.source_branch,
        base_branch=row.target_branch,
        head_sha=row.head_sha,
        second=queue.next_round(row.verdict, row.risk) == "second",
        verify=verify,
        protected_paths=queue.protected_of(charter),
        hidden_checks=hidden,
    )


async def verdict(
    engine: AsyncEngine, user: Principal, run_id: int, body: MergeVerdictIn, judge_key: str | None
) -> MergeVerdict:
    """Take the verdict of the judge run ``run_id`` of the queue or of a merge check, which the worker of ``user``
    holds, with the run's own key ``judge_key``: the gates it closes."""
    if body.dimensions is not None and len(body.dimensions) > MAX_DIMENSIONS:
        raise HTTPException(422, f"a verdict names at most {MAX_DIMENSIONS} dimensions")
    if body.prior is not None and len(body.prior) > findings.MAX_LEDGER:
        raise HTTPException(422, f"a verdict marks at most {findings.MAX_LEDGER} findings")
    async with engine.begin() as conn:
        if await check_rows.judged_check(conn, run_id) is not None:  # a pull request on github.com
            return await check.verdict(conn, user, run_id, body, judge_key)
        run, row = await _held(conn, user, run_id, judge_key)
        second = queue.next_round(row.verdict, row.risk) == "second"
        plan, _ = await _plan_and_task(conn, row)
        hidden_count = (row.verdict or {}).get("hidden_count")
        if not isinstance(hidden_count, int):
            hidden_count = len(judging.hidden_of(await queue.charter_of(conn, row.project_id)))
        passed_signs = await merge_pass.covers(conn, row)  # an admin passed gate 3 at the head judged
        found = judging.Judged(
            body=body,
            second=second,
            expected=merge_gate.verify_of(plan, row.repo),
            hidden_count=hidden_count,
            head=row.head_sha,
            ledger=findings.ledger_of(row.verdict),
            run_id=run_id,
            signs_weighed=passed_signs is not None and body.signs_weighed,
        )
        entry = found.entry(run_id, run.model)
        verdict = {
            **(row.verdict or {}),
            "fingerprint": row.fingerprint,
            ("second" if second else "first"): entry,
            "findings": found.ledger,
        }
        await queue.set_row(conn, row.id, verdict=verdict, judge_run_id=None, judge_key=None, judge_attempts=0)
        row = await queue.row_of(conn, row.id)
        await _close_gates(conn, row, found, entry, passed_signs=passed_signs is not None)
        row = await queue.row_of(conn, row.id)  # pyright: ignore[reportOptionalMemberAccess]
        said = "passed" if entry["passed"] else "failed"
        text = f"The merge queue took the verdict: the Judge {said} merge request !{row.iid}; it is {row.state} now."  # pyright: ignore[reportOptionalMemberAccess]
        await write_event(conn, run_id, {"text": text, "queue_id": row.id, "passed": entry["passed"]})  # pyright: ignore[reportOptionalMemberAccess]
    log.info("merge verdict recorded", extra={"run_id": run_id, "queue_id": row.id, "passed": entry["passed"]})  # pyright: ignore[reportOptionalMemberAccess]
    return MergeVerdict(queue_id=row.id, passed=entry["passed"], state=row.state, reason=row.reason)  # pyright: ignore[reportOptionalMemberAccess]


async def _close_gates(
    conn: AsyncConnection, row, found: judging.Judged, entry: dict, *, passed_signs: bool = False
) -> None:
    """The ledger of the gates a verdict closes, and where the merge request goes. The worker's reading of the diff
    holds it at gate 3 again unless an admin's pass covers the head judged and the Judge weighed the signs
    (``passed_signs``, ``found.signs_weighed``)."""
    sha, run_id = row.head_sha, entry["run_id"]
    if found.signs and not found.signs_weighed:
        kinds = ", ".join(sorted({item["kind"] for item in found.signs}))
        why = f"the worker's reading of the diff shows signs of score hacking ({kinds})"
        if passed_signs:  # a worker from before the hub told it of the pass fails the signs unjudged
            why += ", and its Judge did not weigh them against the plan although an admin passed gate 3 at this head"
        await queue.hold(conn, row, 3, why, blocked=True)
        return
    await queue.record(
        conn,
        row,
        "gate",
        gate=4,
        passed=found.checks_ok,
        reason="; ".join(found.checks_why) or None,
        sha=sha,
        run_id=run_id,
        detail={"verify": found.verify, "hidden": [item.get("exit_code") for item in found.hidden]},
    )
    if not found.checks_ok:
        await queue.hold(conn, row, 4, "; ".join(found.checks_why), blocked=True)
        return
    gate = 7 if found.second else 5
    detail = {
        "model": entry["model"],
        "dimensions": entry["dimensions"],
        "fingerprint": row.fingerprint,
        "findings": found.raised,
        "prior": found.marks,
    }
    await queue.record(
        conn, row, "judged", passed=found.judge_ok, reason=found.body.reasons, sha=sha, run_id=run_id, detail=detail
    )
    if not found.judge_ok:
        await queue.hold(conn, row, gate, "; ".join(found.judge_why), blocked=True)
        return
    await queue.record(conn, row, "gate", gate=gate, passed=True, sha=sha, run_id=run_id)
    risk = row.risk or {}
    if not found.second:
        level = "high" if risk.get("high") else "normal"
        reasons = risk.get("reasons") or []
        why = f"the risk is {level}" + (f": {'; '.join(reasons)}" if reasons else "")
        await queue.record(conn, row, "gate", gate=6, passed=True, reason=why, sha=sha, detail={"risk": risk})
        if risk.get("high"):
            await queue.set_row(conn, row.id, reason=merge_gate.gate_reason(7, "waits for the second Judge"))
            return
        why = "the risk is not high: no second Judge"
        await queue.record(conn, row, "gate", gate=7, passed=True, reason=why, sha=sha)
    await queue.ready(conn, row, "every gate passed")
