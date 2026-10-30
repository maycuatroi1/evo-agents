"""The judge runs of the hub's merge queue on GitLab (plan gitlab-merge-queue, criterion a4: gates 4, 5 and 7): the
run the job queues for a merge request whose gates 0 to 3 passed, what its claim and its prompt hold, what it reads,
and its verdict. ``evo_agents.hub.merge_gate`` holds the rules, ``evo_agents.hub.server.merge_gate`` the job.

A judge run of the queue is a run of kind ``judge`` on no plan and no schedule (schema 0081), which the merge request
it judges names (merge_queue.judge_run_id). The admin who wrote the policy's newest revision dispatches it, so it
reads the repo with that admin's git secret and the project's graph as them; it is pinned to the policy's
``judge.worker``, else to the worker on duty of the project's charter (or a ready node of its pool), whose daemon must say it judges merge requests
(``runs.MERGE_JUDGE``); it runs on Claude Code with the model ``judges.merge_judge_model`` picks, and never reads the
Builder's transcript. A project has one judge run active at a time, the Curator's included: another waits for it.

Its claim names the merge request as a Curator's judge run names a change (``merge_claim``: the branch, the target
branch, the head to judge, its URL, and the run's own key, kept as a SHA-256), so the worker guards it as one, and its
prompt is ``merge_gate.build_prompt`` (``merge_prompt``). GET /v1/worker/runs/{id}/merge-judge, with the run's key,
hands it the verify of the plan's steps in the repo and the hidden checks of the project's charter, which only it
reads. POST /v1/worker/runs/{id}/merge-verdict, with the key, takes its verdict: the worker's own reading of the diff
(gate 3 again), the checks (gate 4), and the Judge (gate 5, or 7 for the second Judge), every dimension passed. The
first Judge passing records the risk (gate 6): a normal risk lets the merge request into the queue (``ready``), a
high one has the job queue the second Judge. Each gate is a line of the ledger, the Judge's verdict another
(``judged``: its run, its model, each dimension, the patch, the findings it raised and its marks of earlier ones); a
gate not passed blocks the merge request with why.

The findings (``evo_agents.hub.findings``) live in the verdict (merge_queue.verdict ``findings``), which a new patch
hands on (``findings.carried``): the prompt of a later round lists those still open on its Judge's dimensions, and the
verdict's marks of them are checked here (``merge_gate.judge_round``), not taken on the Judge's word.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import findings, judge, judges, merge_gate, runs, tables, tiers
from evo_agents.hub.access import has_role
from evo_agents.hub.server import audit
from evo_agents.hub.server import merge_gate as queue
from evo_agents.hub.server.auth import NO_STORE
from evo_agents.hub.server.changes import (
    HiddenResult,
    JudgeKey,
    SecurityPass,
    Sign,
    security_dimension,
    security_findings,
)
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.night_duty import on_duty
from evo_agents.hub.server.projects import project_access
from evo_agents.hub.server.run_state import notify_queued, write_event
from evo_agents.hub.server.runs.models import NOT_HELD, OBJECT_NAME, RunId, VerifyResult
from evo_agents.hub.server.runs.service.claims import worker_of
from evo_agents.hub.server.runs.service.dispatch import insert_run, pinnable, unfit
from evo_agents.hub.server.security import MACHINE, CurrentUser, Principal

log = logging.getLogger(__name__)

JUDGE_TIMEOUT_S = 3600  # a judge run of the queue, its checks and its agent
MAX_DIMENSIONS = 20
NIGHT_SHIFT = "night_shift"

worker_router = APIRouter(prefix="/v1/worker", tags=["worker protocol"], responses={401: {"model": ErrorBody}})
REFUSALS = {code: {"model": ErrorBody} for code in (403, 404, 409)}


def _digest(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


# Queueing a judge run


async def _judge_worker(conn: AsyncConnection, project) -> tuple[int | None, str | None]:
    """(the worker the project's judge runs go to, why there is none): the policy's judge.worker, else the worker on
    duty of its charter."""
    w, wp, s = tables.workers, tables.worker_projects, tables.schedules
    name = project.policy["judge"]["worker"]
    if name:
        serves = select(wp.c.worker_id).where(wp.c.project_id == project.id)
        query = select(w.c.id).where(func.lower(w.c.name) == name.lower(), w.c.revoked_at.is_(None), w.c.id.in_(serves))
        found = (await conn.execute(query.order_by((w.c.owner_id != project.owner_id), w.c.id).limit(1))).first()
        if found is None:
            return None, f"no live worker {name} (the policy's judge.worker) serves project {project.name}"
        return found.id, None
    query = select(s.c.worker_id, s.c.pool_id, s.c.project_id).where(
        s.c.project_id == project.id, s.c.kind == NIGHT_SHIFT
    )
    found = (await conn.execute(query.order_by(s.c.id).limit(1))).first()
    if found is None:
        return None, "the policy names no judge.worker and the project has no charter with a worker on duty"
    if found.pool_id is None:
        return found.worker_id, None
    duty, why = await on_duty(conn, found)  # a pool on duty: a ready node of it, one that judges merge requests first
    if duty is None:
        return None, why
    return next((node for node in duty.nodes if runs.MERGE_JUDGE in node.features), duty.nodes[0]).id, None


async def queue_judge_run(
    conn: AsyncConnection, project, *, repo: str, branch: str, title: str, builder_model, built: bool, second: bool
) -> tuple[int | None, str | None, str]:
    """Queue a judge run of ``project`` (``merge_gate.Project``) on ``branch`` of ``repo``, its second Judge when
    ``second``: (its id, its model, why that model), or (None, None, why it waits). The merge queue's and the merge
    check's (``merge_check``) judge runs both start here."""
    if project.owner_id is None:
        return None, None, "the project's policy has no revision to name who dispatches its Judge"
    u = tables.users
    login = (await conn.execute(select(u.c.login).where(u.c.id == project.owner_id))).scalar_one()
    member = Principal(user_id=project.owner_id, login=login, admin=False, token_id=0, kind=MACHINE, token_hash="")
    try:
        access = await project_access(conn, member, project.name)
    except HTTPException:
        access = None
    if access is None or not has_role(access.role, "writer"):
        return None, None, f"{login}, who wrote the policy, no longer holds writer on project {project.name}"
    worker_id, why = await _judge_worker(conn, project)
    if worker_id is None:
        return None, None, why
    try:
        worker = await pinnable(conn, member, access, worker_id)
    except HTTPException as exc:
        return None, None, f"its worker cannot take it: {exc.detail}"
    if runs.MERGE_JUDGE not in worker.features:
        return (
            None,
            None,
            f"the daemon of worker {worker.name} does not say it judges merge requests: upgrade it and restart it",
        )
    problems = unfit(worker, project.name, "judge", [repo], judges.CLAUDE_CODE)
    if problems:
        return None, None, f"worker {worker.name} cannot take it: {'; '.join(problems)}"
    model, chosen = judges.merge_judge_model(
        project.policy["judge"]["model"], builder_model, built=built, second=second
    )
    values = {
        "kind": "judge",
        "project_id": project.id,
        "plan_id": None,
        "plan_revision": None,
        "title": title[:200],
        "dispatched_by": project.owner_id,
        "dispatched_via": MACHINE,
        "pinned_worker_id": worker.id,
        "requested_runtime": judges.CLAUDE_CODE,
        "runtime": judges.CLAUDE_CODE,
        "model": model,
        "mode": "headless",
        "approval": "auto",
        "timeout_s": JUDGE_TIMEOUT_S,
        "repos": [{"repo": repo, "branch": branch}],
        "max_attempts": 1,  # a judge run that ends without a verdict is tried again by the queue, not the reaper
    }
    try:
        run_id = await insert_run(conn, values)
    except IntegrityError:
        return None, None, "another judge run of the project is active: this one comes after it"
    return run_id, model, chosen


async def queue_judge(conn: AsyncConnection, project, row, *, second: bool) -> str | None:
    """Queue the judge run of merge request ``row`` of ``project`` (``merge_gate.Project``), its second Judge when
    ``second``; None once queued, else why it waits."""
    run_id, model, chosen = await queue_judge_run(
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


async def _judged(conn: AsyncConnection, run_id: int, *, lock: bool = False):
    q = tables.merge_queue
    query = select(q).where(q.c.judge_run_id == run_id)
    return (await conn.execute(query.with_for_update() if lock else query)).one_or_none()


async def merge_claim(conn: AsyncConnection, view) -> dict | None:
    """What the claim of judge run ``view`` tells its worker when it judges a merge request of the queue, as a judge
    run of the Curator is told of its change (``changes.run_curator``), with a new key of its own; None for any other
    run."""
    if view.kind != "judge" or view.plan_id:
        return None
    row = await _judged(conn, view.id, lock=True)
    if row is None:
        from evo_agents.hub.server import merge_check  # it reads this module

        return await merge_check.claim(conn, view)
    charter = await queue.charter_of(conn, row.project_id)
    found = {
        "role": "judge",
        "protected_paths": queue.protected_of(charter),
        "branch": row.source_branch,
        "forge": "gitlab",
        "base_branch": row.target_branch,
        "head_sha": row.head_sha,
        "pr_url": row.web_url,
    }
    if row.state == "judging":
        key = secrets.token_urlsafe(32)
        await queue.set_row(conn, row.id, judge_key=_digest(key))
        found["judge_key"] = key
    return found


async def _plan_and_task(conn: AsyncConnection, row) -> tuple[dict | None, str | None]:
    plan = await queue.plan_body(conn, row.project_id, row.plan_id) if row.source in ("plan", "builder") else None
    task = None
    if row.source == "task" and row.run_id is not None:
        r = tables.runs
        task = (await conn.execute(select(r.c.request).where(r.c.id == row.run_id))).scalar_one_or_none()
    return plan, task


async def merge_prompt(conn: AsyncConnection, view) -> str | None:
    """The prompt of judge run ``view`` when it judges a merge request of the queue; None for any other run."""
    if view.kind != "judge" or view.plan_id:
        return None
    row = await _judged(conn, view.id)
    if row is None:
        from evo_agents.hub.server import merge_check  # it reads this module

        return await merge_check.prompt(conn, view)
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


class MergeJudgeInputs(BaseModel):
    """What a judge run of the queue reads: the merge request, the verify of its plan's steps in the repo, the
    charter's protected paths, and the project's hidden checks, which no other run reads."""

    queue_id: int
    repo: str
    iid: int
    branch: str
    base_branch: str
    head_sha: str | None
    second: bool = Field(description="the second Judge, of a merge request of high risk")
    verify: list[str]
    protected_paths: list[str]
    hidden_checks: list[str] = Field(description="commands run in the worktree; never logged, never shown")


class Dimension(BaseModel):
    verdict: Literal[judge.VERDICTS]
    reason: str = Field("", max_length=2000)


class JudgeFinding(BaseModel):
    """A thing the Judge found wrong, on one of the dimensions it judges."""

    dimension: str = Field(min_length=1, max_length=findings.MAX_DIMENSION_CHARS)
    path: str | None = Field(None, max_length=findings.MAX_PATH_CHARS)
    line: int | None = Field(None, ge=1)
    text: str = Field(min_length=1, max_length=findings.MAX_TEXT_CHARS)


class FindingMark(BaseModel):
    """What the Judge says of a finding of an earlier round."""

    status: Literal[findings.MARKS]
    where: str | None = Field(None, max_length=findings.MAX_WHERE_CHARS, description="the file:line that shows it")
    reason: str = Field("", max_length=findings.MAX_TEXT_CHARS)


class MergeVerdictIn(BaseModel):
    verdict: Literal[judge.VERDICTS] | None = Field(description="the Judge agent's; null when it gave none")
    reasons: str | None = Field(None, max_length=judge.MAX_REASONS_CHARS)
    head_sha: str = Field(pattern=OBJECT_NAME, description="the commit the worktree was at")
    base_sha: str | None = Field(None, pattern=OBJECT_NAME)
    verify: list[VerifyResult] = Field(default_factory=list, max_length=50)
    hidden: list[HiddenResult] = Field(default_factory=list, max_length=50)
    signs: list[Sign] = Field(default_factory=list, max_length=judge.MAX_SIGNS)
    paths: list[Annotated[str, Field(min_length=1, max_length=tiers.MAX_PATH_CHARS)]] = Field(
        default_factory=list, max_length=judge.MAX_PATHS
    )
    dimensions: dict[str, Dimension] | None = Field(
        None, description="each dimension the Judge judged apart, with its verdict and reason"
    )
    findings: list[JudgeFinding] = Field(
        default_factory=list, max_length=findings.MAX_NEW, description="what the Judge found wrong in this round"
    )
    prior: dict[str, FindingMark] | None = Field(
        None, description="the Judge's mark of each finding of an earlier round its prompt listed, by id"
    )
    security: SecurityPass | None = Field(
        None, description="the security pass of the run, which judges the dimension security apart from the agent"
    )
    conflicts: list[Annotated[str, Field(min_length=1, max_length=tiers.MAX_PATH_CHARS)]] | None = Field(
        None,
        max_length=judge.MAX_PATHS,
        description="a merge check's: the paths where merging the base into the head conflicts; null when it merged",
    )


class MergeVerdict(BaseModel):
    queue_id: int
    passed: bool = Field(description="this Judge's verdict, its checks and the worker's reading of the diff")
    state: str = Field(description="where the merge request stands now")
    reason: str | None


async def held_judge(conn: AsyncConnection, user: Principal, run_id: int):
    """The row of judge run ``run_id`` of the queue or of a merge check, which the worker of ``user`` holds, locked;
    404 for any other run."""
    worker_id = (await worker_of(conn, user))[0]
    r = tables.runs
    run = (
        await conn.execute(
            select(r.c.id, r.c.state, r.c.worker_id, r.c.kind, r.c.project_id, r.c.plan_id, r.c.model)
            .where(r.c.id == run_id)
            .with_for_update()
        )
    ).one_or_none()
    if run is None or run.worker_id != worker_id or run.state not in runs.HELD_STATES:
        raise HTTPException(404, NOT_HELD.format(id=run_id))
    if run.kind != "judge" or run.plan_id is not None:
        raise HTTPException(404, f"run {run_id} is no judge run of the merge queue")
    return run


def require_key(run_id: int, key: str | None, stored: str | None) -> None:
    """403 unless ``key`` is the run's own key, whose digest is ``stored``."""
    if not key or stored is None or not hmac.compare_digest(_digest(key), stored):
        raise HTTPException(
            403,
            f"run {run_id} is a judge run: what it reads and its verdict take the run's own key "
            f"({judge.JUDGE_KEY_HEADER}), which its claim handed the daemon",
        )


async def _held(conn: AsyncConnection, user: Principal, run_id: int, key: str | None):
    """(run row, queue row) of the judge run ``run_id`` of the queue the worker of ``user`` holds, whose merge request
    waits for its verdict, asked with the run's key; 404 for any other run, 409 for a merge request that waits for no
    verdict of it, 403 without the key."""
    run = await held_judge(conn, user, run_id)
    row = await _judged(conn, run_id, lock=True)
    if row is None or row.state != "judging":
        raise HTTPException(409, f"run {run_id} judges no merge request that waits for its verdict")
    require_key(run_id, key, row.judge_key)
    return run, row


def hidden_of(charter: dict) -> list[str]:
    """The hidden checks of a project's ``charter``, which only its judge runs read."""
    return [item for item in (charter.get("judge") or {}).get("hidden_checks") or [] if isinstance(item, str)]


async def _checks(conn: AsyncConnection, run_id: int):
    """The module of the merge checks when judge run ``run_id`` judges one; None for a run of the queue."""
    from evo_agents.hub.server import merge_check  # it reads this module

    return merge_check if await merge_check.judged_check(conn, run_id) is not None else None


@worker_router.get("/runs/{run_id}/merge-judge", response_model=MergeJudgeInputs, responses=REFUSALS)
async def merge_judge_inputs(
    request: Request, run_id: RunId, user: CurrentUser, response: Response, judge_key: JudgeKey = None
) -> MergeJudgeInputs:
    """What the judge run of the queue this worker holds reads, with the run's own key."""
    async with request.app.state.engine.begin() as conn:
        checks = await _checks(conn, run_id)
        if checks is not None:  # a pull request on github.com
            found = await checks.inputs(conn, user, run_id, judge_key)
            response.headers.update(NO_STORE)
            return found
        run, row = await _held(conn, user, run_id, judge_key)
        charter = await queue.charter_of(conn, row.project_id)
        hidden = hidden_of(charter)
        plan, _ = await _plan_and_task(conn, row)
        verify = merge_gate.verify_of(plan, row.repo)
        verdict = {**(row.verdict or {}), "hidden_count": len(hidden), "fingerprint": row.fingerprint}
        await queue.set_row(conn, row.id, verdict=verdict)
        text = (
            f"The Judge read its inputs: {len(verify)} verify command(s) of the plan and {len(hidden)} hidden "
            "check(s) of the project, which only this run reads."
        )
        await write_event(conn, run_id, {"text": text, "queue_id": row.id})
    response.headers.update(NO_STORE)
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


@worker_router.post("/runs/{run_id}/merge-verdict", response_model=MergeVerdict, responses=REFUSALS)
async def merge_verdict(
    request: Request, run_id: RunId, body: MergeVerdictIn, user: CurrentUser, judge_key: JudgeKey = None
) -> MergeVerdict:
    """The verdict of the judge run of the queue this worker holds, with the run's own key: the gates it closes."""
    if body.dimensions is not None and len(body.dimensions) > MAX_DIMENSIONS:
        raise HTTPException(422, f"a verdict names at most {MAX_DIMENSIONS} dimensions")
    if body.prior is not None and len(body.prior) > findings.MAX_LEDGER:
        raise HTTPException(422, f"a verdict marks at most {findings.MAX_LEDGER} findings")
    async with request.app.state.engine.begin() as conn:
        checks = await _checks(conn, run_id)
        if checks is not None:  # a pull request on github.com
            return await checks.verdict(conn, user, run_id, body, judge_key)
        run, row = await _held(conn, user, run_id, judge_key)
        second = queue.next_round(row.verdict, row.risk) == "second"
        plan, _ = await _plan_and_task(conn, row)
        hidden_count = (row.verdict or {}).get("hidden_count")
        if not isinstance(hidden_count, int):
            hidden_count = len(hidden_of(await queue.charter_of(conn, row.project_id)))
        found = Judged(
            body=body,
            second=second,
            expected=merge_gate.verify_of(plan, row.repo),
            hidden_count=hidden_count,
            head=row.head_sha,
            ledger=findings.ledger_of(row.verdict),
            run_id=run_id,
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
        await _close_gates(conn, row, found, entry)
        row = await queue.row_of(conn, row.id)
        said = "passed" if entry["passed"] else "failed"
        text = f"The merge queue took the verdict: the Judge {said} merge request !{row.iid}; it is {row.state} now."
        await write_event(conn, run_id, {"text": text, "queue_id": row.id, "passed": entry["passed"]})
    log.info("merge verdict recorded", extra={"run_id": run_id, "queue_id": row.id, "passed": entry["passed"]})
    return MergeVerdict(queue_id=row.id, passed=entry["passed"], state=row.state, reason=row.reason)


class Judged:
    """What a verdict of the queue's Judge says of each gate it closes."""

    def __init__(
        self,
        *,
        body: MergeVerdictIn,
        second: bool,
        expected: list[str],
        hidden_count: int,
        head,
        ledger: list[dict] | None = None,
        run_id: int | None = None,
    ):
        self.body, self.second = body, second
        self.verify = [item.model_dump(exclude_none=True) for item in body.verify]
        self.hidden = [item.model_dump(exclude_none=True) for item in body.hidden]
        self.signs = [sign.model_dump() for sign in body.signs]
        self.dimensions = {name: item.model_dump() for name, item in (body.dimensions or {}).items()}
        self.dimensions[merge_gate.SECURITY] = security_dimension(body.security)
        self.checks_ok, self.checks_why = judge.final_verdict(
            agent="pass",
            verify=self.verify,
            expected_verify=expected,
            hidden=self.hidden,
            hidden_count=hidden_count,
            signs=[],
            head_matches=True,
        )
        names = merge_gate.gate_names(second)
        ledger = ledger or []
        if body.security is not None:  # the pass ran on this head: what it finds now is all it finds
            ledger = findings.supersede(ledger, merge_gate.SECURITY, run_id=run_id, head=body.head_sha)
        new = findings.read_new([item.model_dump() for item in body.findings], names) + security_findings(body.security)
        self.marks = findings.read_marks({key: item.model_dump() for key, item in (body.prior or {}).items()})
        self.judge_ok, self.judge_why = merge_gate.judge_round(
            body.verdict, self.dimensions, names, ledger=ledger, marks=self.marks, new=new
        )
        self.ledger, self.raised = findings.record_round(
            ledger,
            names=names,
            marks=self.marks,
            new=new,
            dimensions=merge_gate.read_dimensions(self.dimensions),
            run_id=run_id,
            head=body.head_sha,
        )
        if body.head_sha != head:
            self.judge_ok = False
            self.judge_why.append("the Judge read another commit than the merge request's head")

    def entry(self, run_id: int, model: str | None) -> dict:
        passed = not self.signs and self.checks_ok and self.judge_ok
        return {
            "run_id": run_id,
            "model": model,
            "agent": self.body.verdict,
            "reasons": self.body.reasons,
            "dimensions": self.dimensions,
            "head_sha": self.body.head_sha,
            "verify": self.verify,
            "hidden": self.hidden,
            "signs": self.signs,
            "failures": self.checks_why + self.judge_why,
            "passed": passed,
            "findings": [item["id"] for item in self.raised],
            "prior": self.marks,
            "security": self.body.security.model_dump(exclude={"findings"}) if self.body.security else None,
        }


async def _close_gates(conn: AsyncConnection, row, found: Judged, entry: dict) -> None:
    """The ledger of the gates a verdict closes, and where the merge request goes."""
    sha, run_id = row.head_sha, entry["run_id"]
    if found.signs:
        kinds = ", ".join(sorted({item["kind"] for item in found.signs}))
        why = f"the worker's reading of the diff shows signs of score hacking ({kinds})"
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
