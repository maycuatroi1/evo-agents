"""The routes of the Curator's changes (``evo_agents.hub.server.changes`` describes the design): what a judge run
reads and the verdict it posts, with the run's own key, the list of a project's changes, and an admin's close of a
change left open with no pull request."""

from __future__ import annotations

import hmac
import logging
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Path, Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import findings, judge, runs, security_pass, tables
from evo_agents.hub.access import has_role
from evo_agents.hub.server import audit, change_tiers
from evo_agents.hub.server import plans as plan_routes
from evo_agents.hub.server.admin import ProjectName
from evo_agents.hub.server.auth import NO_STORE
from evo_agents.hub.server.change_views import (
    Change,
    ChangeClose,
    ChangeList,
    JudgeInputs,
    JudgeKey,
    VerdictIn,
    change_of_plan,
    change_view,
    changes_query,
    charter_of,
    key_digest,
    raise_tier,
    security_dimension,
    security_findings,
    set_change,
)
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.projects import project_access
from evo_agents.hub.server.protection import (
    need_reader,
    reason_line,
)
from evo_agents.hub.server.run_state import write_event
from evo_agents.hub.server.runs.models import MAX_ID, NOT_HELD, RunId
from evo_agents.hub.server.runs.service.claims import worker_of
from evo_agents.hub.server.security import CurrentUser, Principal

log = logging.getLogger(__name__)

CLOSE = "curator.close"  # an admin closed a change with no pull request: "<project> change:<id> proposal:<id>"
MAX_LIST = 200

router = APIRouter(prefix="/v1/projects", tags=["curator"], responses={401: {"model": ErrorBody}})
worker_router = APIRouter(prefix="/v1/worker", tags=["worker protocol"], responses={401: {"model": ErrorBody}})
REFUSALS: dict[int | str, dict[str, Any]] = {code: {"model": ErrorBody} for code in (403, 404, 409)}

ChangeId = Annotated[int, Path(ge=1, le=MAX_ID)]


# The judge run's side


async def _held_judge(conn: AsyncConnection, user: Principal, run_id: int, key: str | None):
    """(run row, change row) of the judge run ``run_id`` the worker of ``user`` holds, whose change waits for its
    verdict, asked with the key the run's claim handed its daemon; 404 for any other run, 409 for a change that waits
    for no verdict of it, 403 without the key."""
    worker_id = (await worker_of(conn, user))[0]
    r = tables.runs
    row = (
        await conn.execute(
            select(r.c.id, r.c.state, r.c.worker_id, r.c.kind, r.c.project_id, r.c.plan_id, r.c.plan_revision)
            .where(r.c.id == run_id)
            .with_for_update()
        )
    ).one_or_none()
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
    return row, change


async def _plan_verify(conn: AsyncConnection, row) -> list[str]:
    rev = tables.plan_revisions
    body = (
        await conn.execute(
            select(rev.c.body).where(
                rev.c.project_id == row.project_id, rev.c.plan_id == row.plan_id, rev.c.revision == row.plan_revision
            )
        )
    ).scalar_one_or_none()
    return judge.verify_commands(body or {})


@worker_router.get("/runs/{run_id}/judge", response_model=JudgeInputs, responses=REFUSALS)
async def judge_inputs(
    request: Request, run_id: RunId, user: CurrentUser, response: Response, judge_key: JudgeKey = None
) -> JudgeInputs:
    """What the judge run this worker holds reads: its change, its proposal, the plan's verify commands, the
    charter's protected paths and the project's hidden checks; with the run's own key."""
    async with request.app.state.engine.begin() as conn:
        row, change = await _held_judge(conn, user, run_id, judge_key)
        charter = await charter_of(conn, row.project_id)
        hidden = [item for item in (charter.get("judge") or {}).get("hidden_checks") or [] if isinstance(item, str)]
        await set_change(conn, change.id, hidden_count=len(hidden))
        pr = tables.proposals
        proposal = (
            await conn.execute(
                select(pr.c.id, pr.c.title, pr.c.kind, pr.c.tier, pr.c.summary, pr.c.paths).where(
                    pr.c.id == change.proposal_id
                )
            )
        ).one()
        verify = await _plan_verify(conn, row)
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
    response.headers.update(NO_STORE)
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
        hidden_checks=hidden,
        knowledge=knowledge,
    )


@worker_router.post("/runs/{run_id}/verdict", response_model=Change, responses=REFUSALS)
async def record_verdict(
    request: Request, run_id: RunId, body: VerdictIn, user: CurrentUser, judge_key: JudgeKey = None
) -> Change:
    """The verdict of the judge run this worker holds, with the run's own key: the hub passes the change only as
    ``judge.final_verdict`` says, and the paths the diff touches give its proposal its tier again."""
    async with request.app.state.engine.begin() as conn:
        row, change = await _held_judge(conn, user, run_id, judge_key)
        expected = await _plan_verify(conn, row) + change_tiers.judge_checks(change)
        hidden_count = change.hidden_count
        if hidden_count is None:
            charter = await charter_of(conn, row.project_id)
            hidden_count = len((charter.get("judge") or {}).get("hidden_checks") or [])
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
        elif body.paths:
            charter = await charter_of(conn, row.project_id)
            state = request.app.state
            reader = change_tiers.GraphReader(state.blobs, state.kg_graphs)
            facts = await change_tiers.verdict_facts(state.engine, change, body.paths, reader)
            values["tier"] = await change_tiers.retier(conn, change, facts, charter)
        await set_change(conn, change.id, **values)
        text = f"The Judge {'passed' if passed else 'failed'} the Curator's change #{change.id}"
        text += "." if passed else f": {'; '.join(failures)}."
        await write_event(conn, run_id, {"text": text, "change_id": change.id, "passed": passed})
        view = await change_view(conn, change.id)
    log.info("verdict recorded", extra={"run_id": run_id, "change_id": change.id, "passed": passed})
    return view


# Reading


@router.get("/{project}/curator/changes", response_model=ChangeList, responses=REFUSALS)
async def list_changes(request: Request, project: ProjectName, user: CurrentUser) -> ChangeList:
    """The project's Curator changes, newest first, with the plans the caller may read."""
    c, pr = tables.curator_changes, tables.proposals
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        need_reader(access)
        query = (
            changes_query()
            .add_columns(pr.c.label.label("proposal_label"))
            .join(pr, pr.c.id == c.c.proposal_id)
            .where(c.c.project_id == access.project_id)
            .order_by(c.c.id.desc())
            .limit(MAX_LIST)
        )
        rows = (await conn.execute(query)).all()
        from evo_agents.hub.server.runs.service.views import visible_plans

        readable = set(await visible_plans(conn, access, None))
    through = plan_routes._sink(access, None) or ""  # no hub sink: no sink lets it through
    shown = [
        Change(**{key: value for key, value in row._mapping.items() if key != "proposal_label"})
        for row in rows
        if access.visible(row.proposal_label, through) and (row.plan_id is None or row.plan_id in readable)
    ]
    return ChangeList(project=access.name, changes=shown)


# Closing by hand


@router.post("/{project}/curator/changes/{change_id}/close", response_model=Change, responses=REFUSALS)
async def close_change(
    request: Request, project: ProjectName, change_id: ChangeId, body: ChangeClose, user: CurrentUser
) -> Change:
    """Close a change left open with no pull request, saying why; the admins of the project alone may."""
    c, pr = tables.curator_changes, tables.proposals
    why = reason_line(body.reason)
    if why is None:
        raise HTTPException(422, "say why the change is closed: the reason is empty")
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        need_reader(access)
        if not has_role(access.role, "admin"):
            raise HTTPException(
                403, f"closing a change of project {access.name} needs the admin role on it; you hold {access.role}"
            )
        query = (
            select(c, pr.c.label.label("proposal_label"))
            .join_from(c, pr, pr.c.id == c.c.proposal_id)
            .where(c.c.id == change_id, c.c.project_id == access.project_id)
            .with_for_update(of=c)
        )
        row = (await conn.execute(query)).one_or_none()
        from evo_agents.hub.server.runs.service.views import visible_plans

        if (
            row is None
            or not access.visible(row.proposal_label, plan_routes._sink(access, None) or "")
            or (row.plan_id is not None and row.plan_id not in set(await visible_plans(conn, access, None)))
        ):
            raise HTTPException(404, f"no change #{change_id} of the Curator in project {access.name} you may read")
        if row.state != "open":
            raise HTTPException(
                409, f"change #{change_id} is {row.state}: only a change left open is closed by hand; nothing changed"
            )
        if row.pr_number is not None:
            raise HTTPException(
                409,
                f"change #{change_id} has pull request {row.pr_url or row.pr_number}: close that on GitHub and the "
                "hub closes the change when it reads it again; nothing changed",
            )
        await set_change(
            conn, change_id, state="closed", reason=why, ledger_extra={"actor_id": user.user_id, "login": user.login}
        )
        await audit.record(
            conn,
            actor_id=user.user_id,
            token_id=user.token_id,
            action=CLOSE,
            target=f"{access.name} change:{change_id} proposal:{row.proposal_id}",
            project_id=access.project_id,
        )
        view = await change_view(conn, change_id)
    log.info("curator change closed by hand", extra={"change_id": change_id, "login": user.login})
    return view
