"""Command line entry point: ``evo-agents``."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from evo_agents import __version__

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


def _harness_validate(args) -> int:
    from evo_agents.harness import validate_harness
    from evo_agents.schema import errors

    targets = args.paths or [str(Path.cwd())]
    reports = []
    for target in targets:
        reports.extend(validate_harness(target, _verify_issues))
    failed = [r for r in reports if not r.ok]
    if args.json:
        print(json.dumps({"ok": not failed, "files": [r.to_json() for r in reports]}, ensure_ascii=False, indent=2))
        return 1 if failed else 0
    for report in reports:
        errs = errors(report.issues)
        warns = [i for i in report.issues if i.severity == "warning"]
        mark = "OK  " if report.ok else "FAIL"
        print(f"{mark} {report.kind:<9} {report.path}  ({len(errs)} error(s), {len(warns)} warning(s))")
        shown = report.issues if args.verbose else errs + warns[:5]
        for issue in shown:
            print(f"       {issue}")
        if not args.verbose and len(warns) > 5:
            print(f"       ... {len(warns) - 5} more warning(s); use -v to see all")
    print(f"{len(reports)} file(s), {len(failed)} with errors")
    return 1 if failed else 0


def _harness_init(args) -> int:
    from evo_agents.harness.init import InitError, init_harness

    try:
        made = init_harness(args.path, args.project)
    except InitError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return exc.code
    names = " and ".join(path.name for path in made.files)
    print(f"Wrote {names} in {made.root} for project {made.project}, with {len(made.repos)} repo(s):")
    width = max(len(repo.name) for repo in made.repos)
    for repo in made.repos:
        origin = f"origin {repo.origin}" if repo.origin else "no origin"
        branch = f"branch {repo.branch}" if repo.branch else "no branch (detached HEAD)"
        print(f"  {repo.name:<{width}}  {origin}, {branch}")
    print(f"Next: `evo-agents harness validate {made.root}`, then `evo-agents hub project register {made.root}`.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="evo-agents", description="Shared tooling for agent harnesses.")
    parser.add_argument("--version", action="version", version=f"evo-agents {__version__}")
    sub = parser.add_subparsers(dest="group", required=True)

    harness = sub.add_parser("harness", help="write, read and validate harness manifests")
    hsub = harness.add_subparsers(dest="command", required=True)
    validate = hsub.add_parser("validate", help="validate harness.yaml, contracts.yaml, knowledge.yaml and plans")
    validate.add_argument("paths", nargs="*", help="harness roots (default: the one containing the cwd)")
    validate.add_argument("--json", action="store_true", help="machine-readable output")
    validate.add_argument("-v", "--verbose", action="store_true", help="show every warning")
    validate.set_defaults(func=_harness_validate)
    init = hsub.add_parser(
        "init", help="write a minimal harness.yaml and knowledge.yaml for the git repos of a directory"
    )
    init.add_argument(
        "path",
        nargs="?",
        default=".",
        help="a repo's directory, or one whose direct subdirectories are repos (default: the cwd)",
    )
    init.add_argument("--project", help="the project's name (default: the directory's name, as a hub project name)")
    init.set_defaults(func=_harness_init)

    from evo_agents.kg.cli import register as register_kg

    register_kg(sub)

    from evo_agents.hub.cli import register as register_hub

    register_hub(sub)

    from evo_agents.worker.cli import register as register_worker

    register_worker(sub)

    from evo_agents.plugins.cli import register as register_plugin

    register_plugin(sub)
    return parser


def _utf8_streams() -> None:
    if os.name != "nt":
        return
    for stream, handling in ((sys.stdin, "strict"), (sys.stdout, "strict"), (sys.stderr, "backslashreplace")):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors=handling)


def main(argv: list[str] | None = None) -> int:
    _utf8_streams()
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv[:3] == ["kg", "hook", "post-search"]:
        # Claude Code runs this one after every grep: it loads neither the parsers of other commands nor the harness.
        from evo_agents.kg.post_search import main as post_search

        def run() -> int:
            return post_search(argv[3:])
    elif argv[:2] == ["kg", "hook"]:
        from evo_agents.kg.cli import run_hook  # the parsers of the other hooks only

        def run() -> int:
            return run_hook(argv[2:])
    else:
        args = build_parser().parse_args(argv)
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
