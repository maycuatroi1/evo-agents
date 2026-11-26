"""``evo-agents hub curator changes``, ``curator change close`` and ``curator protection``: what the accepted proposals
became, and whether a ruleset keeps the Curator's App off each repo's default branch."""

from __future__ import annotations

from evo_agents.clikit import Arg, Group, Result, Table, Text, Views, returns_object
from evo_agents.hub.cli.base import PROJECT, HubCommand, HubContext
from evo_agents.hub.cli.curator.resources import CHANGE, REPO_CHECK_KEYS, Changes, Protection
from evo_agents.hub.cli_client import when


class CuratorChanges(HubCommand):
    name = "changes"
    help = "what the accepted proposals became: plan, branch, pull request, the Judge's verdict, merge"
    arguments = (PROJECT,)
    output = returns_object("project", "changes", schema="ChangeList")
    columns = ("ID", "PROPOSAL", "TIER", "STATE", "JUDGE", "REPO", "PULL REQUEST")

    def run(self, ctx: HubContext) -> Result:
        found = Changes.of(ctx).list()
        rows = [self.row(change) for change in found["changes"]]
        lines = [f"{len(rows)} change(s) of the Curator in {ctx.project}"]
        lines += [f"#{change['id']}: {change['reason']}" for change in found["changes"] if change["reason"]]
        return Result(found, Views(Table(self.columns, rows), Text(*lines)))

    @staticmethod
    def row(change: dict) -> tuple:
        verdict = "-" if change["passed"] is None else ("pass" if change["passed"] else "fail")
        pull = change["pr_url"] or ("merge request" if change["forge"] == "gitlab" else "-")
        return (
            f"#{change['id']}",
            f"#{change['proposal_id']}",
            change["tier"],
            change["state"],
            verdict,
            change["repo"],
            pull,
        )


class ChangeClose(HubCommand):
    name = "close"
    help = "close a change left open with no pull request, saying why; an admin of the project only"
    arguments = (
        Arg("id", type=int, help="the change's id, as `curator changes` lists it"),
        Arg("--reason", required=True, help="why it is closed, in one line"),
        PROJECT,
    )
    output = CHANGE

    def run(self, ctx: HubContext) -> Result:
        change = Changes.of(ctx).close(ctx.args.id, ctx.args.reason)
        said = (
            f"Change #{change['id']} of {ctx.project} (proposal #{change['proposal_id']}) is {change['state']}: "
            f"{change['reason']}"
        )
        return Result(change, Text(said))


CURATOR_CHANGE = Group("change", "one change of the Curator: close one left open with no pull request", [ChangeClose()])


class CuratorProtection(HubCommand):
    name = "protection"
    help = "whether a ruleset keeps the Curator off each repo's default branch; --check REPO checks one"
    arguments = (PROJECT, Arg("--check", metavar="REPO", help="check this repo now; an admin of the project only"))
    output = returns_object(
        "project",
        "repos",
        schema="Protection",
        variants=[("--check", returns_object(*REPO_CHECK_KEYS, schema="RepoCheck"))],
    )
    columns = ("REPO", "FORGE", "PROTECTED", "CHECKED")

    def run(self, ctx: HubContext) -> Result:
        if ctx.args.check:
            found = Protection.of(ctx).check(ctx.args.check)
            word = "keeps the Curator's App off" if found["protected"] else "does not keep the Curator's App off"
            branch = found["default_branch"] or "its default branch"
            return Result(
                found, Text(f"{found['repo']} ({found['github_repo']}): its ruleset {word} {branch}", found["reason"])
            )
        found = Protection.of(ctx).list()
        return Result(found, Table(self.columns, [self.row(repo) for repo in found["repos"]]))

    @staticmethod
    def row(repo: dict) -> tuple:
        protected = "-" if repo["protected"] is None else ("yes" if repo["protected"] else "no")
        checked = when(repo["checked_at"]) if repo["checked_at"] else "never"
        return (repo["repo"], repo["forge"] or "-", protected, checked)
