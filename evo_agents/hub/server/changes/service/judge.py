"""A judge run of a change of the Curator (``evo_agents.hub.server.changes.service.job`` describes the design): what it
reads, with the run's own key, and the verdict it posts (the routes: ``evo_agents.hub.server.changes.routes``)."""

from __future__ import annotations

import hmac
import logging

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import findings, judge, runs, security_pass, tiers
from evo_agents.hub.server.changes.models.views import (
    Change,
    JudgeInputs,
    VerdictIn,
    key_digest,
    security_dimension,
    security_findings,
)
from evo_agents.hub.server.changes.repository import judge as store
from evo_agents.hub.server.changes.repository.views import change_of_plan, change_view, charter_of, raise_tier
from evo_agents.hub.server.changes.service import repos as change_repos
from evo_agents.hub.server.changes.service import tiers as change_tiers
from evo_agents.hub.server.changes.service.moves import set_change
from evo_agents.hub.server.run_state import write_event
from evo_agents.hub.server.runs.models import NOT_HELD
from evo_agents.hub.server.runs.service.claims import worker_of
from evo_agents.hub.server.security import Principal

log = logging.getLogger(__name__)


async def held_judge(conn: AsyncConnection, user: Principal, run_id: int, key: str | None):
    """(run row, change row) of the judge run ``run_id`` the worker of ``user`` holds, whose change waits for its
    verdict, asked with the key the run's claim handed its daemon; 404 for any other run, 409 for a change that waits
    for no verdict of it, 403 without the key. The change of a run judging one repo of a change in several is as that
    repo has it (``change_repos.in_focus``)."""
    worker_id = (await worker_of(conn, user))[0]
    row = await store.locked_run(conn, run_id)
    if row is None or row.worker_id != worker_id or row.state not in runs.HELD_STATES:
        raise HTTPException(404, NOT_HELD.format(id=run_id))
    if row.kind != "judge":
        raise HTTPException(404, f"run {run_id} is not a judge run: only a judge run reads what a Judge reads")
    change = await change_of_plan(conn, row.project_id, row.plan_id, lock=True)
    if change is None or change.state != "judging" or change.judge_run_id != run_id:
        raise HTTPException(409, f"run {run_id} judges no change that waits for its verdict")
    if not key or change.judge_key is None or not hmac.compare_digest(key_digest(key), change.judge_key):
        raise HTTPException(
            403,
            f"run {run_id} is a judge run: what it reads and its verdict take the run's own key "
            f"({judge.JUDGE_KEY_HEADER}), which its claim handed the daemon; the worker token alone reads no hidden "
            "check and writes no verdict",
        )
    return row, await change_repos.in_focus(conn, change)  # a change in several repos: the repo it judges now


async def _judged_by(conn: AsyncConnection, user: Principal, run_id: int) -> Change | None:
    """The change judge run ``run_id``, held by the worker of ``user``, already judged, for a verdict ``held_judge``
    refuses with 409: the post that recorded it answered after the worker stopped waiting, and the worker's retry gets
    that verdict again instead of a 409 that fails the run. None for any other run or change."""
    worker_id = (await worker_of(conn, user))[0]
    row = await store.run_row(conn, run_id)
    if row is None or row.worker_id != worker_id or row.state not in runs.HELD_STATES or row.kind != "judge":
        return None
    change = await change_of_plan(conn, row.project_id, row.plan_id)
    verdict = getattr(change, "verdict", None)
    if change is None or change.state != "judged" or not isinstance(verdict, dict) or verdict.get("run_id") != run_id:
        return None
    log.info("verdict posted again", extra={"run_id": run_id, "change_id": change.id})
    return await change_view(conn, change.id)


async def _plan_verify(conn: AsyncConnection, row, change) -> list[str]:
    """The verify commands of the plan revision judge run ``row`` judges: of the steps of the repo it judges alone, for
    a change in several repos."""
    body = await store.revision_body(conn, row.project_id, row.plan_id, row.plan_revision)
    if getattr(change, "repo_row_id", None) is not None:
        body = change_repos.repo_body(body, change.repo)
    return judge.verify_commands(body or {})


async def judge_inputs(engine: AsyncEngine, user: Principal, run_id: int, key: str | None) -> JudgeInputs:
    """What the judge run ``run_id`` the worker of ``user`` holds reads, with the run's own key ``key``."""
    async with engine.begin() as conn:
        row, change = await held_judge(conn, user, run_id, key)
        charter = await charter_of(conn, row.project_id)
        hidden = [item for item in (charter.get("judge") or {}).get("hidden_checks") or [] if isinstance(item, str)]
        await set_change(conn, change.id, hidden_count=len(hidden))
        proposal = await store.judge_proposal(conn, change.proposal_id)
        verify = await _plan_verify(conn, row, change)
        knowledge = change_tiers.judge_knowledge(change)
        checks = f", {len(knowledge['checks'])} check(s) of knowledge.yaml" if knowledge else ""
        await write_event(
            conn,
            run_id,
            {
                "text": f"The Judge read its inputs: {len(verify)} verify command(s) of the plan{checks} and "
                f"{len(hidden)} hidden check(s) of the project, which only this run reads.",
                "change_id": change.id,
            },
        )
    log.info("judge inputs read", extra={"run_id": run_id, "change_id": change.id, "hidden": len(hidden)})
    return JudgeInputs(
        change_id=change.id,
        repo=change.repo,
        branch=change.branch,
        base_branch=change.base_branch,
        head_sha=change.head_sha,
        proposal=dict(proposal._mapping),
        verify=verify,
        protected_paths=[item for item in charter.get("protected_paths") or [] if isinstance(item, str)],
        evolvable_paths=tiers.evolvable_paths(charter),
        hidden_checks=hidden,
        knowledge=knowledge,
    )


async def _held_or_judged(conn: AsyncConnection, user: Principal, run_id: int, key: str | None):
    """(run row, change row) as ``held_judge`` has them, or the change, a ``Change``, when its 409 is for a verdict
    this judge run already gave: the worker sent it again."""
    try:
        return await held_judge(conn, user, run_id, key)
    except HTTPException as exc:
        judged = await _judged_by(conn, user, run_id) if exc.status_code == 409 else None
        if judged is None:
            raise
        return judged


def _verdict_of(run_id: int, body: VerdictIn, change, expected: list[str], hidden_count: int) -> tuple:
    """(passed, the verdict, its signs) of ``body`` for ``change``, as ``judge.final_verdict`` and the security pass
    say."""
    signs = [sign.model_dump() for sign in body.signs]
    head = change.head_sha or body.head_sha  # GitLab: the tip the Judge read is the one judged
    verify = [item.model_dump(exclude_none=True) for item in body.verify]
    hidden = [item.model_dump(exclude_none=True) for item in body.hidden]
    passed, failures = judge.final_verdict(
        agent=body.verdict,
        verify=verify,
        expected_verify=expected,
        hidden=hidden,
        hidden_count=hidden_count,
        signs=signs,
        head_matches=body.head_sha == head,
    )
    dimension = security_dimension(body.security)
    ledger = findings.supersede(
        findings.ledger_of(change.verdict), security_pass.DIMENSION, run_id=run_id, head=body.head_sha
    )
    ledger, _ = findings.record_round(
        ledger,
        names=[security_pass.DIMENSION],
        marks={},
        new=security_findings(body.security),
        dimensions={security_pass.DIMENSION: dimension},
        run_id=run_id,
        head=body.head_sha,
    )
    if dimension["verdict"] != "pass" and not signs:  # with signs the Judge did not start, nor the pass
        passed = False
        failures.append(f"the Judge failed {security_pass.DIMENSION}: {dimension['reason']}")
    verdict = {
        "run_id": run_id,
        "agent": body.verdict,
        "reasons": body.reasons,
        "dimensions": {security_pass.DIMENSION: dimension},
        "security": body.security.model_dump(exclude={"findings"}) if body.security else None,
        "findings": ledger,
        "head_sha": body.head_sha,
        "base_sha": body.base_sha,
        "verify": verify,
        "hidden": hidden,
        "signs": signs,
        "failures": failures,
        "source": "judge",
    }
    return passed, verdict, signs, head, failures


async def record_verdict(state, user: Principal, run_id: int, body: VerdictIn, key: str | None) -> Change:
    """The verdict ``body`` of judge run ``run_id``, with the app's ``state``: the graph read for its tier is read
    before the transaction that locks the run and its change."""
    facts = None
    if body.paths and not body.signs:
        async with state.engine.begin() as conn:
            held = await _held_or_judged(conn, user, run_id, key)
            if isinstance(held, Change):
                return held
            _, held = held
        reader = change_tiers.GraphReader(state.blobs, state.config.data_dir)
        facts = await change_tiers.verdict_facts(state.engine, held, body.paths, reader)
    async with state.engine.begin() as conn:
        held = await _held_or_judged(conn, user, run_id, key)
        if isinstance(held, Change):
            return held
        row, change = held
        expected = await _plan_verify(conn, row, change) + change_tiers.judge_checks(change)
        hidden_count = change.hidden_count
        if hidden_count is None:
            charter = await charter_of(conn, row.project_id)
            hidden_count = len((charter.get("judge") or {}).get("hidden_checks") or [])
        passed, verdict, signs, head, failures = _verdict_of(run_id, body, change, expected, hidden_count)
        values = {
            "state": "judged",
            "passed": passed,
            "verdict": verdict,
            "head_sha": head,
            "reason": None,
            "judge_key": None,
        }
        if signs:
            await raise_tier(conn, change, signs)
            values["tier"] = 3
        elif facts is not None:
            charter = await charter_of(conn, row.project_id)
            values["tier"] = await change_tiers.retier(conn, change, facts, charter)
        if getattr(change, "repo_row_id", None) is not None:  # on its repo's row; the change judged once each is
            await change_repos.judged_repo(conn, change, values)
        else:
            await set_change(conn, change.id, **values)
        text = f"The Judge {'passed' if passed else 'failed'} the Curator's change #{change.id}"
        text += "." if passed else f": {'; '.join(failures)}."
        await write_event(conn, run_id, {"text": text, "change_id": change.id, "passed": passed})
        view = await change_view(conn, change.id)
    log.info("verdict recorded", extra={"run_id": run_id, "change_id": change.id, "passed": passed})
    return view
