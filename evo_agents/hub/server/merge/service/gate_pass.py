"""One pass of the job hub.merge_queue over one repo of the hub's merge queue on GitLab, with its GitLab project open
(``service.gate`` runs it for each repo the queue takes): it opens the merge requests of the runs whose work is done,
takes those labeled, finds those of the Curator's changes, moves each merge request of the queue through its gates,
then what follows its merges (``service.post_merge``) and the turn of its target branch (``service.turn``)."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import autonomy, diff_signs, findings, judge, merge_gate, runs, task_runs
from evo_agents.hub.server import audit, pull_trailers
from evo_agents.hub.server.changes.service import tiers as change_tiers
from evo_agents.hub.server.forge.client.gitlab import GitLab
from evo_agents.hub.server.forge.models.gitlab import GitLabRefused, MergeRequest
from evo_agents.hub.server.merge.models.gate import Candidate, Project
from evo_agents.hub.server.merge.repository import gate as queries
from evo_agents.hub.server.merge.repository.gate import older, plan_body, record, row_of, set_row
from evo_agents.hub.server.merge.service import breaker, passes, post_merge, turn
from evo_agents.hub.server.merge.service.gate import close, hold, hold_judging, next_round, protected_of, ready, wait

log = logging.getLogger(__name__)


class RepoPass:
    """One pass over one repo of the queue, with its GitLab project open."""

    def __init__(self, engine, project: Project, repo: str, settings: dict, path: str, gl: GitLab, reader, outcomes):
        self.engine, self.project, self.repo, self.settings = engine, project, repo, settings
        self.path, self.gl, self.reader, self.outcomes = path, gl, reader, outcomes
        self.target: str = settings[autonomy.TARGET_BRANCH]
        self.label: str = project.policy["merge"]["opt_in_label"]
        self._checked: list[str] | None = None
        self._project_id: int | None = None  # the GitLab project's id
        self.tripped = False  # the repo's breaker tripped (service.breaker): a revert alone moves on

    async def run(self) -> None:
        self.tripped = await breaker.tripped(self.engine, self.project.id, self.repo)
        listed = {mr.iid: mr for mr in await self.gl.merge_requests(self.path, target_branch=self.target)}
        await self.open_runs(listed)
        await self.take_labeled(listed)
        await self.find_builders(listed)
        async with self.engine.begin() as conn:
            ids = await queries.open_ids(conn, self.project.id, self.repo)
        for row_id in ids:
            try:
                await self.move(row_id)
            except GitLabRefused as exc:
                await self.refused(row_id, exc)
        await post_merge.follow(self)
        await turn.take_turns(self)

    async def refused(self, row_id: int, exc: GitLabRefused) -> None:
        """GitLab refused a call for merge request ``row_id``: gone (404), it leaves the queue; else it waits."""
        log.warning("a merge request of the queue was refused", extra={"row": row_id, "why": str(exc)})
        async with self.engine.begin() as conn:
            row = await row_of(conn, row_id, lock=True)
            if row is not None and exc.status == 404:
                await close(conn, row, f"GitLab no longer has it: {exc}")
            elif row is not None:
                await wait(conn, row, f"GitLab refused the hub: {exc}")
        self.outcomes["refused"] += 1

    # Taking merge requests

    async def _insert(self, conn: AsyncConnection, values: dict, why: str, kind: str):
        place = {"project_id": self.project.id, "repo": self.repo, "target_branch": self.target}
        row_id = await queries.insert_row(conn, {**place, **values})
        row = await row_of(conn, row_id)
        await record(conn, row, kind, reason=why, sha=row.head_sha)  # pyright: ignore[reportOptionalMemberAccess]
        return row

    async def _plan_candidates(self, conn: AsyncConnection, since: datetime) -> list[Candidate]:
        found = []
        for plan_id, run_id in await queries.done_plans(conn, self.project.id, since):
            if judge.is_curator_plan_id(plan_id):
                continue  # the Curator's merge request opens with its push, and comes through curator_judged
            body = await plan_body(conn, self.project.id, plan_id)
            if body is None or not merge_gate.plan_ready(body, self.repo, self.target):
                continue
            known = await queries.plan_row(conn, self.project.id, self.repo, plan_id)
            if known is not None and (known.iid is not None or not await older(conn, known.updated_at)):
                continue
            steps = merge_gate.steps_of(body, self.repo)
            goal = " ".join(str(body.get("goal") or "").split())
            titles = "\n".join(f"- step {step.get('id')}: {' '.join(str(step.get('title')).split())}" for step in steps)
            found.append(
                Candidate(
                    source="plan",
                    branch=merge_gate.plan_branch(body, self.repo),  # pyright: ignore[reportArgumentType]
                    title=f"{plan_id}: {goal or 'the work of the plan'}"[:255],
                    description=(
                        f"Opened by the evo-agents hub for plan {plan_id} of project {self.project.name}, whose "
                        f"steps in {self.repo} are done (run #{run_id} last).\n\n{goal}\n\n{titles}"
                    ),
                    run_id=run_id,
                    plan_id=plan_id,
                    steps=[str(step.get("id")) for step in steps],
                    row_id=None if known is None else known.id,
                )
            )
        return found

    async def _task_candidates(self, conn: AsyncConnection, since: datetime) -> list[Candidate]:
        found = []
        for row in await queries.done_tasks(conn, self.project.id, self.repo, since):
            branch = row.branch if task_runs.is_task_branch(row.branch) else task_runs.branch_of(row.id)
            title = " ".join(str(row.title or f"task run #{row.id}").split())
            found.append(
                Candidate(
                    source="task",
                    branch=branch,
                    title=title[:255],
                    description=f"Opened by the evo-agents hub for task run #{row.id} of project {self.project.name}."
                    + (f"\n\n{row.request.strip()}" if isinstance(row.request, str) and row.request.strip() else ""),
                    run_id=row.id,
                    plan_id=None,
                    steps=[],
                    row_id=None,
                )
            )
        return found

    async def open_runs(self, listed: dict[int, MergeRequest]) -> None:
        """Open the merge request of each plan and task run whose work in the repo is done (see the docstring)."""
        since = merge_gate.auto_since(self.project.revisions, self.repo)
        if since is None:
            return
        async with self.engine.begin() as conn:
            candidates = await self._plan_candidates(conn, since) + await self._task_candidates(conn, since)
            for candidate in candidates:
                candidate.description += await pull_trailers.block(conn, candidate.run_id)
        by_branch = {mr.source_branch: mr for mr in listed.values()}
        for candidate in candidates:
            mr, opened, problem = by_branch.get(candidate.branch), False, None
            if mr is None:
                try:
                    mr = await self.gl.create_merge_request(
                        self.path,
                        source_branch=candidate.branch,
                        target_branch=self.target,
                        title=candidate.title,
                        description=candidate.description,
                    )
                    opened = True
                except GitLabRefused as exc:
                    problem = str(exc)
            await self._keep_candidate(candidate, mr, opened, problem)

    async def _keep_candidate(self, candidate: Candidate, mr: MergeRequest | None, opened: bool, problem) -> None:
        async with self.engine.begin() as conn:
            profile, model = await queries.builder(conn, candidate.run_id)
            values = {
                "source": candidate.source,
                "source_branch": candidate.branch,
                "run_id": candidate.run_id,
                "plan_id": candidate.plan_id,
                "steps": candidate.steps,
                "profile": profile,
                "model": model,
            }
            if mr is None:
                reason = merge_gate.reason_line(
                    f"GitLab did not open the merge request of {candidate.branch}: {problem}"
                )
                if candidate.row_id is None:  # a line of no gate: merge_events_check holds "gate" to a gate's number
                    await self._insert(conn, {**values, "state": "blocked", "reason": reason}, reason, "queued")
                else:
                    await set_row(conn, candidate.row_id, reason=reason)
                self.outcomes["not_opened"] += 1
                return
            mr_values = {"iid": mr.iid, "title": mr.title, "web_url": mr.web_url, "state": "gating", "reason": None}
            why = f"the hub opened !{mr.iid}" if opened else f"!{mr.iid} of {candidate.branch} was open already"
            try:
                if candidate.row_id is None:
                    row = await self._insert(conn, {**values, **mr_values}, why, "opened" if opened else "queued")
                else:
                    await set_row(conn, candidate.row_id, **mr_values)
                    row = await row_of(conn, candidate.row_id)
                    await record(conn, row, "opened" if opened else "queued", reason=why)
            except IntegrityError:  # the merge request is in the queue already, under another source
                return
            if opened:
                target = f"{self.project.name}/{self.repo} !{mr.iid} source={candidate.source}"
                plan = f" plan={candidate.plan_id}" if candidate.plan_id else ""
                await audit.record(
                    conn,
                    actor_id=None,
                    token_id=None,
                    action=audit.MERGE_OPEN,
                    target=f"{target}{plan} run:{candidate.run_id}",
                    project_id=self.project.id,
                )
        self.outcomes["opened" if opened else "taken"] += 1

    async def take_labeled(self, listed: dict[int, MergeRequest]) -> None:
        """Take each merge request labeled ``merge.opt_in_label`` by a Developer or above, or take it back."""
        async with self.engine.begin() as conn:
            known = await queries.rows_by_iid(conn, self.project.id, self.repo, list(listed))
        for iid, mr in listed.items():
            row = known.get(iid)
            if self.label not in mr.labels or (row is not None and (row.source != "label" or row.state != "closed")):
                continue
            user_id = merge_gate.labeler(await self.gl.label_events(self.path, iid), self.label)
            level = await self.gl.access_level(self.path, user_id) if user_id is not None else None
            if level is None or level < merge_gate.DEVELOPER:
                continue
            why = f"labeled {self.label} by a member of role {level} on GitLab"
            async with self.engine.begin() as conn:
                values = {"iid": iid, "title": mr.title, "web_url": mr.web_url, "state": "gating", "reason": None}
                if row is None:
                    await self._insert(
                        conn, {"source": "label", "source_branch": mr.source_branch, **values}, why, "queued"
                    )
                else:
                    await set_row(conn, row.id, head_sha=None, **values)
                    await record(conn, await row_of(conn, row.id), "queued", reason=why)
            self.outcomes["taken"] += 1

    async def find_builders(self, listed: dict[int, MergeRequest]) -> None:
        """Give each Curator's change handed to the queue its merge request, found by its branch."""
        async with self.engine.begin() as conn:
            waiting = await queries.waiting_builders(conn, self.project.id, self.repo)
            by_branch = {mr.source_branch: mr for mr in listed.values()}
            for row in waiting:
                mr = by_branch.get(row.source_branch)
                if mr is None:
                    continue
                try:
                    async with conn.begin_nested():
                        await set_row(conn, row.id, iid=mr.iid, title=mr.title, web_url=mr.web_url, reason=None)
                except IntegrityError:
                    continue

    # Moving a merge request on

    def scope_of(self, row, mr: MergeRequest) -> tuple[str, str | None]:
        """Whether merge request ``mr`` of ``row`` is still the queue's (``merge_gate.scope``)."""
        facts = merge_gate.Scope(
            mode=autonomy.AUTO,
            forge=True,
            target_branch=self.target,
            state=mr.state,
            target=mr.target_branch,
            fork=mr.source_project_id is not None and mr.source_project_id != mr.target_project_id,
            draft=mr.draft,
            source=row.source if row.source != "label" or self.label in mr.labels else None,
            labeler_ok=True,  # checked when the queue took it (take_labeled)
            paused=self.tripped and row.source != "revert",  # the revert of a failure goes on (merge_breaker)
        )
        return merge_gate.scope(facts)

    async def move(self, row_id: int) -> None:
        async with self.engine.begin() as conn:
            row = await row_of(conn, row_id)
        mr = await self.gl.merge_request(self.path, row.iid)  # pyright: ignore[reportOptionalMemberAccess]
        verdict, why = self.scope_of(row, mr)
        async with self.engine.begin() as conn:
            row = await row_of(conn, row_id, lock=True)
            if row is None or row.state not in merge_gate.OPEN_STATES:
                return
            if verdict == "out":
                await close(conn, row, f"it left the queue: {why}", mr)
                self.outcomes["closed"] += 1
                return
            if mr.sha and mr.sha != row.head_sha:
                await self.new_head(conn, row, mr)
                row = await row_of(conn, row_id)
            if verdict == "wait":
                await wait(conn, row, why)  # pyright: ignore[reportArgumentType]
                return
            if row.state == "ready" and (row.reason or "").startswith(merge_gate.WAITS):  # pyright: ignore[reportOptionalMemberAccess]
                await set_row(conn, row.id, reason=None)  # pyright: ignore[reportOptionalMemberAccess]  # what it waited for on GitLab is over: its turn may come
        if row.state == "gating":  # pyright: ignore[reportOptionalMemberAccess]
            await self.gate(row_id, mr)
        elif row.state == "judging":  # pyright: ignore[reportOptionalMemberAccess]
            await self.judging(row_id)
        await self.comment(row_id)

    async def new_head(self, conn: AsyncConnection, row, mr: MergeRequest) -> None:
        """A head the gates have not read, pushed by someone other than the queue: they start again, a judge run of
        the head before no longer counts, and the merge request waits for a turn after those queued before it."""
        await set_row(
            conn,
            row.id,
            stamp=("head_at",),
            head_sha=mr.sha,
            state="gating",
            reason=None,
            queued_at=None,
            rebases=0,
            before_sha=None,
            title=mr.title,
            description=merge_gate.kept_description(mr.description),
            web_url=mr.web_url,
            judge_run_id=None,
            judge_key=None,
            judge_attempts=0,
        )
        if row.head_sha is not None:
            await record(conn, row, "head", reason=f"a new head {mr.sha[:12]}: the gates start again", sha=mr.sha)  # pyright: ignore[reportOptionalSubscript]

    async def repo_check(self) -> list[str]:
        """Gate 0, once a pass: what the GitLab project lacks for the queue."""
        if self._checked is None:
            settings = await self.gl.project(self.path)
            branch = await self.gl.protected_branch(self.path, self.target)
            self._project_id = settings.id
            self._checked = merge_gate.repo_checked(
                merge_method=settings.merge_method,
                pipeline_must_succeed=settings.only_allow_merge_if_pipeline_succeeds,
                skipped_is_success=settings.allow_merge_on_skipped_pipeline,
                branch=self.target,
                push_levels=branch.push_access_levels if branch is not None else None,
                merge_levels=branch.merge_access_levels if branch is not None else None,
            )
        return self._checked

    async def head_message(self, mr: MergeRequest) -> str | None:
        commits = await self.gl.merge_request_commits(self.path, mr.iid)
        found = next((commit for commit in commits if commit.id == mr.sha), None)
        return (found.message or found.title) if found is not None else None

    async def expected_branch(self, conn: AsyncConnection, row) -> tuple[str | None, dict | None]:
        """(the branch the work of ``row`` is on, its plan's body)."""
        if row.source == "plan":
            body = await plan_body(conn, self.project.id, row.plan_id)
            return (merge_gate.plan_branch(body, self.repo) if body else None), body
        if row.source == "builder":
            return row.source_branch, await plan_body(conn, self.project.id, row.plan_id)
        return row.source_branch, None

    async def gate(self, row_id: int, mr: MergeRequest) -> None:
        """Gates 0 to 3 at the head of the merge request, then the risk; a judge run when they pass."""
        missing = await self.repo_check()
        if missing:
            async with self.engine.begin() as conn:
                await hold(conn, await row_of(conn, row_id), 0, "the GitLab project " + "; ".join(missing))
            return
        message = await self.head_message(mr)
        pipeline = mr.head_pipeline
        state, why = merge_gate.pipeline_state(
            mr.sha, pipeline.sha if pipeline else None, pipeline.status if pipeline else None, message
        )
        async with self.engine.begin() as conn:
            row = await row_of(conn, row_id, lock=True)
            if state == "pending":
                minutes = self.project.policy["merge"]["pipeline_minutes"]
                if row.head_at is not None and await older(conn, row.head_at, timedelta(minutes=minutes)):  # pyright: ignore[reportOptionalMemberAccess]
                    await hold(conn, row, 1, f"{why} after {minutes} minutes", blocked=True)
                else:
                    await hold(conn, row, 1, why)  # pyright: ignore[reportArgumentType]
                return
            if state == "red":
                await hold(conn, row, 1, why, blocked=True)  # pyright: ignore[reportArgumentType]
                return
            branch, body = await self.expected_branch(conn, row)
            mr_project = self.path if self._project_id in (None, mr.project_id) else f"project {mr.project_id}"
            problem = merge_gate.plan_repo_problem(
                row.source,  # pyright: ignore[reportOptionalMemberAccess]
                project=self.path,
                mr_project=mr_project,
                source_branch=mr.source_branch,
                branch=branch,
            )
            if problem is not None:
                await hold(conn, row, 2, problem, blocked=True)
                return
        files = merge_gate.files_of(await self.gl.merge_request_diffs(self.path, mr.iid))
        if row.source == "revert":  # its diff must undo the one merged, in the place of gate 3 and the Judges  # pyright: ignore[reportOptionalMemberAccess]
            await post_merge.revert_gated(self, row_id, mr, files)
            return
        hacked = self.hacked(files, body)
        passed_by = None
        if hacked is not None:
            passed_by = await passes.passes(self.engine, row_id, hacked, sha=mr.sha)  # an admin's pass of this head
            if passed_by is None:
                async with self.engine.begin() as conn:
                    row = await row_of(conn, row_id, lock=True)
                    await hold(conn, row, 3, hacked, blocked=True)
                return
        risk = await self.risk(diff_signs.changed_paths(files))
        await self.passed(row_id, mr, merge_gate.patch_fingerprint(files), risk, passed_by)

    def hacked(self, files: list[diff_signs.FileDiff], body: dict | None) -> str | None:
        """Gate 3: why the diff ``files`` of a merge request on plan ``body`` shows signs of score hacking; None when
        it shows none."""
        signs = diff_signs.hack_signs(
            files,
            repo=self.repo,
            protected=protected_of(self.project.charter),
            verify_commands=merge_gate.verify_of(body, self.repo),
        )
        if not signs:
            return None
        kinds = ", ".join(sorted({item["kind"] for item in signs}))
        where = signs[0]["path"] + (f":{signs[0]['line']}" if signs[0].get("line") else "")
        return f"the diff shows signs of score hacking ({kinds}), first at {where}"

    async def risk(self, paths: list[str]) -> dict:
        """Gate 6: the risk of the diff's ``paths``, with what the project's graph says they reach."""
        named = change_tiers.named_paths(self.repo, paths)
        targets = [f"{repo}:{path}" for repo, path in named]
        graph = await change_tiers.impact_as_owner(
            self.engine, self.project.id, targets, self.reader, owner_id=self.project.owner_id
        )
        async with self.engine.begin() as conn:
            unknown = await change_tiers.unknown_paths(conn, self.project.id, named, graph)
        return merge_gate.risk_of(
            self.repo,
            [path for _, path in named],
            risk_paths=self.project.policy["merge"]["risk_paths"],
            protected=protected_of(self.project.charter),
            impacted=graph.impacted if graph is not None else (),
            unknown=unknown,
        )

    async def passed(self, row_id: int, mr: MergeRequest, fingerprint: str, risk: dict, passed_by=None) -> None:
        """Gates 0 to 3 passed at the head (gate 3 by an admin's pass, ``passed_by``: the line's reason and detail):
        the Judges next, unless their verdicts hold for this patch."""
        async with self.engine.begin() as conn:
            row = await row_of(conn, row_id, lock=True)
            if row.state != "gating" or row.head_sha != mr.sha:  # pyright: ignore[reportOptionalMemberAccess]
                return
            for gate in range(4):
                why, detail = passed_by if gate == 3 and passed_by is not None else (None, None)
                await record(conn, row, "gate", gate=gate, passed=True, reason=why, sha=mr.sha, detail=detail)
            same = (row.verdict or {}).get("fingerprint") == fingerprint  # pyright: ignore[reportOptionalMemberAccess]
            verdict = row.verdict if same else findings.carried(row.verdict)  # pyright: ignore[reportOptionalMemberAccess]  # a new patch: its findings stay
            await set_row(
                conn,
                row.id,  # pyright: ignore[reportOptionalMemberAccess]
                state="judging",
                reason=None,
                fingerprint=fingerprint,
                risk=risk,
                verdict=verdict,
                title=mr.title,  # the Judge reads them as they are now
                description=merge_gate.kept_description(mr.description),
            )
            row = await row_of(conn, row_id)
            if verdict is not None and next_round(verdict, risk) == "ready":
                for gate in (4, 5, 6, 7):
                    why = f"the patch is the one judged before (fingerprint {fingerprint[:12]}): its verdict holds"
                    await record(conn, row, "gate", gate=gate, passed=True, reason=why, sha=mr.sha)
                if await ready(conn, row, "every gate passed, the Judges' verdicts kept for the same patch"):
                    self.outcomes["ready"] += 1
                return
        await self.judging(row_id)

    async def judging(self, row_id: int) -> None:
        """Queue the next judge run of ``row_id``, or block it once JUDGE_ATTEMPTS runs ended without a verdict."""
        from evo_agents.hub.server.merge_judge import queue_judge  # it reads the rows through this module

        async with self.engine.begin() as conn:
            row = await row_of(conn, row_id, lock=True)
            if row is None or row.state != "judging":
                return
            step = next_round(row.verdict, row.risk)
            if step == "ready":
                if await ready(conn, row, "every gate passed"):
                    self.outcomes["ready"] += 1
                return
            if step == "failed":
                await hold(conn, row, 5, "the Judges' verdict for this patch did not pass it", blocked=True)
                return
            second = step == "second"
            gate = 7 if second else 5
            if row.judge_run_id is not None:
                ended = await queries.run_state(conn, row.judge_run_id)
                if ended not in runs.TERMINAL_STATES:
                    return
                attempts = row.judge_attempts + 1
                why = f"judge run #{row.judge_run_id} ended {ended} without a verdict"
                if attempts >= judge.JUDGE_ATTEMPTS:
                    await set_row(conn, row.id, judge_run_id=None, judge_key=None, judge_attempts=attempts)
                    await hold(
                        conn, row, gate, f"{attempts} judge runs ended without a verdict; the last: {why}", blocked=True
                    )
                    return
                await set_row(conn, row.id, judge_run_id=None, judge_key=None, judge_attempts=attempts)
                row = await row_of(conn, row_id)
            why = await queue_judge(conn, self.project, row, second=second)
            if why is not None:
                await hold_judging(conn, row, gate, why)
            else:
                self.outcomes["judging"] += 1

    async def comment(self, row_id: int) -> None:
        """Say once on GitLab why the queue blocks the merge request, or which setting the project misses."""
        async with self.engine.begin() as conn:
            row = await row_of(conn, row_id)
        if row is None or not row.reason:
            return
        if row.state != "blocked" and not (row.state == "gating" and row.reason.startswith("gate 0 ")):
            return
        new, still = [], []
        if row.state == "blocked" and merge_gate.held_at(row.reason) in (5, 7):  # held by a Judge: its findings
            new, still = findings.unposted(findings.ledger_of(row.verdict))
        if row.reason == row.commented and not new:
            return
        passable = merge_gate.pass_refusal(row.state, row.reason, row.source) is None
        try:
            note = merge_gate.comment(row.reason, waits=row.state != "blocked", passable=passable, new=new, still=still)
            await self.gl.comment(self.path, row.iid, note)
        except GitLabRefused as exc:
            log.warning("the merge queue could not comment", extra={"row": row_id, "why": str(exc)})
            return
        async with self.engine.begin() as conn:
            values = {"commented": row.reason}
            if new:  # the ids named, read again under the lock: a verdict may have come in since
                locked = await row_of(conn, row_id, lock=True)
                ledger = findings.mark_posted(findings.ledger_of(locked.verdict), [item["id"] for item in new])  # pyright: ignore[reportOptionalMemberAccess]
                values["verdict"] = {**(locked.verdict or {}), "findings": ledger}  # pyright: ignore[reportOptionalMemberAccess]
            await set_row(conn, row_id, **values)
