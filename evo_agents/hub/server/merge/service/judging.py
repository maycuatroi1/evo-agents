"""What the judge runs of the merge queue and of the merge checks share: queueing one (``queue_judge_run``), the
run's own key, the hidden checks it reads, and the reading of its verdict (``Judged``). ``service.judge`` holds the
queue's side and ``service.check`` the merge check's."""

from __future__ import annotations

import hashlib
import hmac

from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import findings, judge, judges, merge_gate, merge_infra, runs
from evo_agents.hub.access import has_role
from evo_agents.hub.server.changes.models.views import security_dimension, security_findings
from evo_agents.hub.server.merge.models.judge import MergeVerdictIn
from evo_agents.hub.server.merge.repository import judge as repository
from evo_agents.hub.server.projects import project_access
from evo_agents.hub.server.runs.models import NOT_HELD
from evo_agents.hub.server.runs.service.claims import worker_of
from evo_agents.hub.server.runs.service.dispatch import insert_run
from evo_agents.hub.server.security import MACHINE, Principal

JUDGE_TIMEOUT_S = 3600  # a judge run of the queue, its checks and its agent


def digest(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


# Queueing a judge run


async def queue_judge_run(
    conn: AsyncConnection, project, *, repo: str, branch: str, title: str, builder_model, built: bool, second: bool
) -> tuple[int | None, str | None, str]:
    """Queue a judge run of ``project`` (``merge_gate.Project``) on ``branch`` of ``repo``, its second Judge when
    ``second``: (its id, its model, why that model), or (None, None, why it waits). The merge queue's and the merge
    check's (``service.check``) judge runs both start here."""
    if project.owner_id is None:
        return None, None, "the project's policy has no revision to name who dispatches its Judge"
    login = await repository.login_of(conn, project.owner_id)
    member = Principal(user_id=project.owner_id, login=login, admin=False, token_id=0, kind=MACHINE, token_hash="")
    try:
        access = await project_access(conn, member, project.name)
    except HTTPException:
        access = None
    if access is None or not has_role(access.role, "writer"):
        return None, None, f"{login}, who wrote the policy, no longer holds writer on project {project.name}"
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
        "pinned_worker_id": None,
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


# The judge run's side


async def held_judge(conn: AsyncConnection, user: Principal, run_id: int):
    """The row of judge run ``run_id`` of the queue or of a merge check, which the worker of ``user`` holds, locked;
    404 for any other run."""
    worker_id = (await worker_of(conn, user))[0]
    run = await repository.judge_run(conn, run_id)
    if run is None or run.worker_id != worker_id or run.state not in runs.HELD_STATES:
        raise HTTPException(404, NOT_HELD.format(id=run_id))
    if run.kind != "judge" or run.plan_id is not None:
        raise HTTPException(404, f"run {run_id} is no judge run of the merge queue")
    return run


def require_key(run_id: int, key: str | None, stored: str | None) -> None:
    """403 unless ``key`` is the run's own key, whose digest is ``stored``."""
    if not key or stored is None or not hmac.compare_digest(digest(key), stored):
        raise HTTPException(
            403,
            f"run {run_id} is a judge run: what it reads and its verdict take the run's own key "
            f"({judge.JUDGE_KEY_HEADER}), which its claim handed the daemon",
        )


def hidden_of(charter: dict) -> list[str]:
    """The hidden checks of a project's ``charter``, which only its judge runs read."""
    return [item for item in (charter.get("judge") or {}).get("hidden_checks") or [] if isinstance(item, str)]


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
        signs_weighed: bool = False,
        skip_unchanged: bool = False,
    ):
        """``signs_weighed``: the Judge weighed the signs of score hacking against the plan (``judge.WEIGH_SIGNS``: a
        merge check of a pull request, or a merge request of the queue whose gate 3 an admin passed at the head
        judged, ``merge_pass.covers``), so they fail nothing by themselves; any other signs of the queue hold at gate
        3. ``skip_unchanged``: a merge check's, whose verify of done steps the pull request does not change
        (``merge_infra.unchanged``) count as passed, each named in the entry (``unchanged``)."""
        self.body, self.second = body, second
        self.signs_weighed = signs_weighed
        self.expected, self.hidden_count = expected, hidden_count
        self.verify = [item.model_dump(exclude_none=True) for item in body.verify]
        self.unchanged = [item["command"] for item in self.verify if skip_unchanged and merge_infra.unchanged(item)]
        counted = merge_infra.without_unchanged(self.verify) if skip_unchanged else self.verify
        self.counted = counted
        self.hidden = [item.model_dump(exclude_none=True) for item in body.hidden]
        self.signs = [sign.model_dump() for sign in body.signs]
        self.dimensions = {name: item.model_dump() for name, item in (body.dimensions or {}).items()}
        self.dimensions[merge_gate.SECURITY] = security_dimension(body.security)
        self.checks_ok, self.checks_why = judge.final_verdict(
            agent="pass",
            verify=counted,
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
        self.round = (names, ledger, new)  # what ``infra_causes`` judges the round again with
        self.ledger, self.raised = findings.record_round(
            ledger,
            names=names,
            marks=self.marks,
            new=new,
            dimensions=merge_gate.read_dimensions(self.dimensions),
            run_id=run_id,
            head=body.head_sha,
        )
        self.head_matches = body.head_sha == head
        if not self.head_matches:
            self.judge_ok = False
            self.judge_why.append("the Judge read another commit than the merge request's head")

    @property
    def passed(self) -> bool:
        return (self.signs_weighed or not self.signs) and self.checks_ok and self.judge_ok

    def infra_causes(self) -> list[str]:
        """The hub's own causes of this verdict's fail (``merge_infra.verdict_causes``): none when it passed, or when
        the pull request fails it too."""
        names, ledger, new = self.round
        checks_ok_without_base, _ = judge.final_verdict(
            agent="pass",
            verify=merge_infra.without_base(self.counted),
            expected_verify=self.expected,
            hidden=merge_infra.without_base(self.hidden),
            hidden_count=self.hidden_count,
            signs=[],
            head_matches=True,
        )
        judge_ok_with_security = False
        if self.body.security is None and self.body.verdict is not None:
            passing = {**self.dimensions, merge_gate.SECURITY: {"verdict": "pass", "reason": ""}}
            judge_ok_with_security, _ = merge_gate.judge_round(
                self.body.verdict, passing, names, ledger=ledger, marks=self.marks, new=new
            )
        return merge_infra.verdict_causes(
            passed=self.passed,
            unweighed_signs=bool(self.signs) and not self.signs_weighed,
            head_matches=self.head_matches,
            checks_ok=self.checks_ok,
            checks_ok_without_base=checks_ok_without_base,
            agent=self.body.verdict,
            judge_ok=self.judge_ok,
            judge_ok_with_security=judge_ok_with_security,
        )

    def entry(self, run_id: int, model: str | None) -> dict:
        passed = self.passed
        return {
            "run_id": run_id,
            "model": model,
            "agent": self.body.verdict,
            "reasons": self.body.reasons,
            "dimensions": self.dimensions,
            "head_sha": self.body.head_sha,
            "verify": self.verify,
            "unchanged": self.unchanged,
            "hidden": self.hidden,
            "signs": self.signs,
            "signs_weighed": self.signs_weighed,
            "failures": self.checks_why + self.judge_why,
            "passed": passed,
            "findings": [item["id"] for item in self.raised],
            "prior": self.marks,
            "security": self.body.security.model_dump(exclude={"findings"}) if self.body.security else None,
        }
