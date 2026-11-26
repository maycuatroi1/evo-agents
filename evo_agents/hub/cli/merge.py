"""``evo-agents hub merge list|resume|pass``: the hub's merge queue on GitLab of a project (plan gitlab-merge-queue),
over GET /v1/projects/{p}/merge-queue, POST .../merge-queue/repos/{repo}/resume and POST .../merge-queue/{id}/pass.

``list`` prints the breaker of each repo the queue passed over, then its merge requests (``--repo``'s alone when
given), newest first: where each comes from, where it stands and why it waits or is blocked. ``resume REPO`` resets the
breaker of a repo that tripped, which takes the admin role on the project: its paused merge requests wait for their
turn again, at their place. ``pass REPO IID --reason TEXT`` passes the hold of merge request !IID of REPO at a gate
that holds it for a person (gate 3, score hacking), which takes the admin role too: the queue goes on from that gate at
the same head. Every member of the project reads the queue.

Every command finds its project with ``--project``, or in the harness.yaml (``hub.project``) of the harness around the
current directory. ``--json`` prints what the hub answered. Standard library only, like the rest of the client.

The commands are classes of ``evo_agents.clikit`` (docs/structure.md, section Command classes): :class:`MergeQueue` is
the gateway to the project's merge queue, and :data:`MERGE` their group.
"""

from __future__ import annotations

from typing import Any

from evo_agents.clikit import Arg, Group, Result, Table, Text, Views, returns_object
from evo_agents.hub.cli.base import PROJECT, HubCommand, HubContext, ProjectResource, when
from evo_agents.hub.client import HubError

MAX_LIST = 200  # merge requests the hub lists at most (merge_view.MAX_LIST)
WHY_CHARS = 80  # of a merge request's reason in the table
# The keys of the hub's answers these commands print with --json (the contract, `evo-agents hub contract print`).
QUEUE_KEYS = ("project", "requests", "samples", "breakers")
BREAKER_KEYS = ("repo", "state", "cause", "reason", "tripped_at", "trips", "resumed_by", "resumed_at", "red_since")
REQUEST_KEYS = (  # a merge request of the queue (QueuedMergeRequest)
    *("id", "repo", "iid", "source", "target_branch", "source_branch", "run_id", "plan_id", "steps", "change_id"),
    *("profile", "model", "title", "web_url", "state", "reason", "head_sha", "fingerprint", "risk", "verdict"),
    *("judge_run_id", "queued_at", "merged_at", "created_at", "updated_at", "events", "before_sha", "rebases"),
    *("after_sha", "reverts_id", "uat_result", "prod_result"),
)


class MergeQueue(ProjectResource):
    """The merge queue of a project: GET /v1/projects/{p}/merge-queue and the routes below it (Gateway)."""

    collection = "merge-queue"

    def list(self, limit: int, repo: str | None = None) -> Any:
        return self.call("GET", limit=limit, repo=repo or None)

    def of_repo(self, repo: str) -> Any:
        """As many merge requests of ``repo`` as the hub lists, to find one by its number."""
        return self.call("GET", repo=repo, limit=MAX_LIST)

    def resume(self, repo: str) -> Any:
        return self.call("POST", "repos", repo, "resume")

    def pass_hold(self, request: int, reason: str) -> Any:
        return self.call("POST", request, "pass", body={"reason": reason})


def short(text: str | None) -> str:
    line = " ".join((text or "").split())
    return line if len(line) <= WHY_CHARS else line[: WHY_CHARS - 3] + "..."


def breaker_lines(project: str, breaker: dict) -> list[str]:
    repo = breaker["repo"]
    if breaker["state"] != "tripped":
        return [f"Breaker of {repo}: armed"]
    return [
        f"Breaker of {repo}: tripped at {when(breaker['tripped_at'])} UTC ({breaker['cause']}): {breaker['reason']}",
        f"  an admin resets it: evo-agents hub merge resume {repo} --project {project}",
    ]


def iid_of(text: str) -> int:
    """The number of a merge request, written 12 or !12."""
    found = text[1:] if text.startswith("!") else text
    if not found.isdigit() or int(found) < 1:
        raise HubError(f"{text} names no merge request: write its number, as 12 or !12")
    return int(found)


class MergeList(HubCommand):
    name = "list"
    help = "the breaker of each repo, then the merge requests of the queue, newest first"
    arguments = (
        Arg("--repo", help="the merge requests of this repo alone"),
        Arg(
            "--limit", type=int, default=50, choices=range(1, MAX_LIST + 1), metavar="N", help="at most N (default 50)"
        ),
        PROJECT,
    )
    output = returns_object(*QUEUE_KEYS, schema="MergeQueueView")
    columns = ("MR", "REPO", "SOURCE", "STATE", "WHY")

    def run(self, ctx: HubContext) -> Result:
        found = MergeQueue.of(ctx).list(ctx.args.limit, ctx.args.repo)
        if ctx.args.json:
            return Result(found)
        lines = [line for breaker in found["breakers"] for line in breaker_lines(ctx.project, breaker)]
        empty = f"The merge queue of project {ctx.project} holds no merge request."
        rows = [self.row(item) for item in found["requests"]]
        return Result(found, Views(Text(*lines), Table(self.columns, rows, empty)))

    @staticmethod
    def row(item: dict) -> tuple:
        iid = f"!{item['iid']}" if item["iid"] is not None else "-"
        return (iid, item["repo"], item["source"], item["state"], short(item["reason"]) or "-")


class MergeResume(HubCommand):
    name = "resume"
    help = "reset the breaker of a repo that tripped (the admin role on the project)"
    arguments = (Arg("repo", metavar="REPO", help="the repo whose breaker tripped"), PROJECT)
    output = returns_object(*BREAKER_KEYS, "resumed", schema="BreakerReset")

    def run(self, ctx: HubContext) -> Result:
        repo = ctx.args.repo
        reset = MergeQueue.of(ctx).resume(repo)
        resumed = reset["resumed"]
        waiting = ", ".join(f"!{iid}" for iid in resumed)
        said = f"Reset the breaker of {repo} in project {ctx.project}: " + (
            f"{waiting} wait for their turn again." if resumed else "no merge request was paused."
        )
        return Result(reset, Text(said))


class MergePass(HubCommand):
    name = "pass"
    help = "pass the hold of a merge request at a gate held for a person (the admin role on the project)"
    arguments = (
        Arg("repo", metavar="REPO", help="the repo of the merge request"),
        Arg("iid", metavar="IID", help="the merge request's number on GitLab, as 12 or !12"),
        Arg("--reason", required=True, help="why the hold may go on: the ledger and the audit keep it"),
        PROJECT,
    )
    output = returns_object(*REQUEST_KEYS, schema="QueuedMergeRequest")

    def run(self, ctx: HubContext) -> Result:
        args = ctx.args
        queue = MergeQueue.of(ctx)
        project, iid = ctx.project, iid_of(args.iid)
        found = queue.of_repo(args.repo)
        held = next((item for item in found["requests"] if item["iid"] == iid), None)
        if held is None:
            raise HubError(f"the merge queue of project {project} holds no merge request !{iid} of {args.repo}")
        passed = queue.pass_hold(held["id"], args.reason)
        gate = (held["reason"] or "").split(":", 1)[0] or "its gate"
        head = (passed["head_sha"] or "")[:12] or "its head"
        said = (
            f"Passed the hold of !{iid} of {args.repo} at {gate} in project {project}: the queue goes on from that gate "
            f"at {head}, and a new push takes the pass back."
        )
        return Result(passed, Text(said))


MERGE = Group(
    "merge",
    "the merge queue of a project on GitLab and the breaker of each repo",
    [MergeList(), MergeResume(), MergePass()],
)
