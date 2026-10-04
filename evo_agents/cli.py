"""Command line entry point: ``evo-agents``."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from evo_agents import __version__
from evo_agents.harness import validate_harness
from evo_agents.schema import errors


def _harness_validate(args) -> int:
    targets = args.paths or [str(Path.cwd())]
    reports = []
    for target in targets:
        reports.extend(validate_harness(target))
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="evo-agents", description="Shared tooling for agent harnesses.")
    parser.add_argument("--version", action="version", version=f"evo-agents {__version__}")
    sub = parser.add_subparsers(dest="group", required=True)

    harness = sub.add_parser("harness", help="read and validate harness manifests")
    hsub = harness.add_subparsers(dest="command", required=True)
    validate = hsub.add_parser("validate", help="validate harness.yaml, contracts.yaml, knowledge.yaml and plans")
    validate.add_argument("paths", nargs="*", help="harness roots (default: the one containing the cwd)")
    validate.add_argument("--json", action="store_true", help="machine-readable output")
    validate.add_argument("-v", "--verbose", action="store_true", help="show every warning")
    validate.set_defaults(func=_harness_validate)

    from evo_agents.kg.cli import register as register_kg

    register_kg(sub)

    from evo_agents.hub.cli import register as register_hub

    register_hub(sub)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:
        return 0


if __name__ == "__main__":
    sys.exit(main())
