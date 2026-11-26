"""Command line entry point: ``evo-agents``. :func:`tree` is the whole command line as one ``Group`` of
``evo_agents.clikit``: the ``harness`` commands, written here, then the tree of each package's ``cli``.

``main`` answers ``evo-agents kg hook ...`` before building any of it: Claude Code runs ``kg hook post-search`` after
every grep, so that hook loads no parser at all, and the other hooks only their own.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Mapping
from pathlib import Path

from evo_agents import __version__
from evo_agents.clikit import Arg, Command, Context, Group, Result, Text

WORKER_ON_WINDOWS = (
    "evo-agents worker runs on macOS and Linux only: its daemon needs process groups, tmux and launchd or systemd. "
    "On Windows, run the worker inside WSL (https://learn.microsoft.com/windows/wsl/install); every other command "
    "of evo-agents works here."
)


def _verify_issues(plan: dict) -> list:
    """A warning for each step not done whose verify cannot be one shell command line, as the hub refuses a new one
    (``evo_agents.hub.plans.unrunnable_verify``); a warning, since a plan the hub holds keeps an older one."""
    from evo_agents.hub.plans import VERIFY_RULE, unrunnable_verify
    from evo_agents.schema import Issue

    return [
        Issue(f"steps[{index}].verify", f"the verify of step {key} {why}. {VERIFY_RULE}", "warning")
        for index, key, why in unrunnable_verify(None, plan)
    ]


class HarnessCommand(Command[Context]):
    """A command of ``evo-agents harness``, on the harness around the current directory or the paths it is given."""

    def context(self, args: argparse.Namespace) -> Context:
        return Context(args)

    def failed(self, exc: Exception) -> int | None:
        from evo_agents.harness.init import InitError

        if not isinstance(exc, InitError):
            return None
        print(f"error: {exc}", file=sys.stderr)
        return exc.code


def report_lines(report, verbose: bool) -> list[str]:
    """What the terminal prints of the validation of one file."""
    from evo_agents.schema import errors

    errs = errors(report.issues)
    warns = [i for i in report.issues if i.severity == "warning"]
    mark = "OK  " if report.ok else "FAIL"
    lines = [f"{mark} {report.kind:<9} {report.path}  ({len(errs)} error(s), {len(warns)} warning(s))"]
    shown = report.issues if verbose else errs + warns[:5]
    lines.extend(f"       {issue}" for issue in shown)
    if not verbose and len(warns) > 5:
        lines.append(f"       ... {len(warns) - 5} more warning(s); use -v to see all")
    return lines


class Validate(HarnessCommand):
    name = "validate"
    help = "validate harness.yaml, contracts.yaml, knowledge.yaml and plans"
    arguments = (
        Arg("paths", nargs="*", help="harness roots (default: the one containing the cwd)"),
        Arg("--json", action="store_true", help="machine-readable output"),
        Arg("-v", "--verbose", action="store_true", help="show every warning"),
    )

    def run(self, ctx: Context) -> Result:
        from evo_agents.harness import validate_harness

        reports = []
        for target in ctx.args.paths or [str(Path.cwd())]:
            reports.extend(validate_harness(target, _verify_issues))
        failed = [r for r in reports if not r.ok]
        lines = [line for report in reports for line in report_lines(report, ctx.args.verbose)]
        lines.append(f"{len(reports)} file(s), {len(failed)} with errors")
        data = {"ok": not failed, "files": [r.to_json() for r in reports]}
        return Result(data, Text(*lines), 1 if failed else 0)


class Init(HarnessCommand):
    name = "init"
    help = "write a minimal harness.yaml and knowledge.yaml for the git repos of a directory"
    arguments = (
        Arg(
            "path",
            nargs="?",
            default=".",
            help="a repo's directory, or one whose direct subdirectories are repos (default: the cwd)",
        ),
        Arg("--project", help="the project's name (default: the directory's name, as a hub project name)"),
    )

    def run(self, ctx: Context) -> Result:
        from evo_agents.harness.init import init_harness

        made = init_harness(ctx.args.path, ctx.args.project)
        names = " and ".join(path.name for path in made.files)
        lines = [f"Wrote {names} in {made.root} for project {made.project}, with {len(made.repos)} repo(s):"]
        width = max(len(repo.name) for repo in made.repos)
        for repo in made.repos:
            origin = f"origin {repo.origin}" if repo.origin else "no origin"
            branch = f"branch {repo.branch}" if repo.branch else "no branch (detached HEAD)"
            lines.append(f"  {repo.name:<{width}}  {origin}, {branch}")
        lines.append(
            f"Next: `evo-agents harness validate {made.root}`, then `evo-agents hub project register {made.root}`."
        )
        return Result(None, Text(*lines))


HARNESS = Group("harness", "write, read and validate harness manifests", (Validate(), Init()), dest="command")


def tree() -> Group:
    """The whole command line: ``harness``, then the knowledge graph's, the hub's, the worker's and the plugins'
    commands, each package's tree imported when the parser is built."""
    from evo_agents.hub.cli import tree as hub
    from evo_agents.kg.cli import tree as kg
    from evo_agents.plugins.cli import tree as plugin
    from evo_agents.worker.cli import tree as worker

    return Group(
        "evo-agents",
        "Shared tooling for agent harnesses.",
        (HARNESS, kg(), hub(), worker(), plugin()),
        dest="group",
        arguments=(Arg("--version", action="version", version=f"evo-agents {__version__}"),),
    )


def build_parser() -> argparse.ArgumentParser:
    return tree().parser()


def _utf8_streams() -> None:
    if os.name != "nt":
        return
    for stream, handling in ((sys.stdin, "strict"), (sys.stdout, "strict"), (sys.stderr, "backslashreplace")):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors=handling)


def _run_lease_env(run_id: int, environ: Mapping[str, str]) -> Mapping[str, str] | None:
    """What run N's socket adds to its agent's environment (``evo-agents worker env --run N``), for the Claude Code
    calls of the knowledge graph, which imports no worker (``evo_agents.kg.claude_code.use_lease_env``)."""
    from evo_agents.worker import credential_commands

    return credential_commands.lease_env(run_id, environ=environ)


def main(argv: list[str] | None = None) -> int:
    _utf8_streams()
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv[:3] == ["kg", "hook", "post-search"]:
        # Claude Code runs this one after every grep: it loads neither the parsers of other commands nor the harness.
        from evo_agents.hub.kg_grep_hook import main as post_search  # asks the hub (evo_agents.kg.post_search)

        def run() -> int:
            return post_search(argv[3:])
    elif argv[:2] == ["kg", "hook"]:
        from evo_agents.kg.cli.hook import run_hook  # the parsers of the other hooks only

        def run() -> int:
            return run_hook(argv[2:])
    else:
        parser = build_parser()
        if argv[:1] == ["hub"]:
            from evo_agents.hub.cli.extensions import refreshed

            if refreshed(argv):  # a command a plugin added since the list was kept: read the hub's list again
                parser = build_parser()
        args = parser.parse_args(argv)
        from evo_agents.kg import claude_code

        claude_code.use_lease_env(_run_lease_env)
        if args.group == "worker" and os.name == "nt":
            print(f"error: {WORKER_ON_WINDOWS}", file=sys.stderr)
            return 2

        def run() -> int:
            return int(args.func(args) or 0)

    try:
        return run()
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:
        return 0


if __name__ == "__main__":
    sys.exit(main())
