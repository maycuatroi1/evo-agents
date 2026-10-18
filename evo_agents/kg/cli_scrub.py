"""``evo-agents kg scrub``: old copies of secrets in this machine's kg home (``evo_agents.kg.scrub``).

By default it only reads: for each project, the blobs holding a secret with their state (old, live, kept), the paths
and item ids that refer to them and the kinds of secret they hold, the run logs of old runs that would go with them,
the run logs in use a purge would rewrite, and the memo and graph rows holding one. ``--purge`` prints what it is about
to delete, asks before deleting (or takes ``--yes``), rewrites those logs, deletes, and compacts the memo and the
graph. Nothing it prints or writes holds a value.
"""

from __future__ import annotations

import contextlib
import json
import sys
from pathlib import Path

from evo_agents.kg.corpus import kg_home
from evo_agents.kg.project import ProjectError, resolve_project
from evo_agents.kg.scrub import KEPT, LIVE, OLD, ScrubBusy, holding_sources, project_dirs, purge, scan_project, totals

SHOWN = 20  # blobs listed per project in text; --json lists them all


def project_targets(args) -> list[tuple[str, Path]]:
    """(name, directory) of the projects to read: --all, a project's directory under the kg home, or the project
    --project or the cwd resolves to. Raises ProjectError."""
    home = kg_home()
    if args.all:
        return project_dirs(home)
    name = args.project
    if name and "/" not in name and name not in (".", "..") and (home / name).is_dir():
        return [(name, home / name)]
    project = resolve_project(name)
    return [(project.name, project.root)]


def _kinds(secrets: dict[str, int]) -> str:
    return ", ".join(f"{kind} {count}" for kind, count in secrets.items())


def size_text(size: int) -> str:
    for unit, scale in (("GiB", 1024**3), ("MiB", 1024**2), ("KiB", 1024)):
        if size >= scale:
            return f"{size / scale:.1f} {unit}"
    return f"{size} B"


def _print_states(scan) -> None:
    counts = scan.counts()
    print(f"  old {counts[OLD]}: nothing in use refers to them; --purge deletes them")
    if counts[LIVE]:
        print(
            f"  live {counts[LIVE]}: items of the corpus now; sync the project so its connector redacts them (the git "
            "connector does), then scrub again"
        )
    if counts[KEPT]:
        pinned = f"; {len(scan.pinned)} of them stay, named by a run log a sync left unfinished" if scan.pinned else ""
        print(
            f"  kept {counts[KEPT]}: only records the corpus no longer holds refer to them, in run logs still in use; "
            f"--purge rewrites those {counts['rewrite']} run log(s) without such records, then deletes them{pinned}"
        )
    if counts["logs"]:
        print(f"  run logs: {counts['logs']} of old runs name a blob --purge deletes; they go with it")
    if counts["memo"]:
        print(f"  memo: {counts['memo']} row(s); --purge deletes them and the next build computes them again")
    if counts["graph"]:
        print(f"  graph: {counts['graph']} row(s); kg build rewrites them from the corpus once it is clean")


def print_scan(scan) -> None:
    if scan.clean:
        print(f"{scan.project}: nothing holds a secret ({scan.scanned} blob(s) read)")
    else:
        kinds = f" ({_kinds(scan.secrets())})" if scan.blobs else ""
        print(f"{scan.project}: {len(scan.blobs)} of {scan.scanned} blob(s) hold a secret{kinds}")
        _print_states(scan)
        for finding in scan.blobs[:SHOWN]:
            where = ", ".join(finding.paths[:2]) or "(no run log names it)"
            print(f"  {finding.state:<4}  {finding.sha256[:16]}  {where}  {_kinds(finding.secrets)}")
        if len(scan.blobs) > SHOWN:
            print(f"  and {len(scan.blobs) - SHOWN} more: --json lists them all")
    for warning in scan.warnings:
        print(f"  warning: {warning}")


def _plan_line(total: dict, projects: int) -> str:
    kept = total["to_delete"] - total[OLD]
    rewrite = f", after rewriting {total['rewrite']} run log(s) in use" if total["rewrite"] else ""
    return (
        f"{total[OLD]} old blob(s)"
        + (f" and {kept} kept blob(s)" if kept else "")
        + f" ({size_text(total['bytes'])}), "
        f"{total['logs']} run log(s) of old runs and {total['memo']} memo row(s) in {projects} project(s){rewrite}"
    )


def _confirmed(args, total: dict, projects: int) -> bool:
    """Whether to go on with the purge: nothing to delete, --yes, or a yes at the prompt."""
    if not (total["to_delete"] or total["logs"] or total["rewrite"] or total["memo"]):
        if not args.json:
            print("Nothing to delete; compacting the memo and the graph.")
        return True
    if not args.json:
        print(f"About to delete {_plan_line(total, projects)}. This cannot be undone.")
    if args.yes:
        return True
    answer = input("Delete them? [y/N] ")
    return answer.strip().lower() in ("y", "yes")


def _scan(targets, quiet: bool) -> list:
    scans = []
    for name, root in targets:
        if not quiet:
            print(f"reading {name} ...", file=sys.stderr, flush=True)
        scans.append(scan_project(name, root))
    return scans


def _output(args, scans: list) -> None:
    if args.json:
        body = {"purge": args.purge, "home": str(kg_home()), "projects": [s.to_json() for s in scans]}
        print(json.dumps({**body, "totals": totals(scans)}, ensure_ascii=False, indent=2))
        return
    for scan in scans:
        print_scan(scan)


def _purge_all(args, scans: list) -> int:
    """Ask, delete what ``scans`` found, compact, and say what went. Run holding every project's sources."""
    if not args.json:
        _output(args, scans)
    if not _confirmed(args, totals(scans), len(scans)):
        print("Nothing was deleted.")
        return 1
    for scan in scans:
        shown = len(scan.warnings)
        deleted = purge(scan)
        if not args.json:
            rewritten = (
                f"; rewrote {deleted['rewritten']} run log(s) in use without the {deleted['dropped']} record(s) the "
                "corpus no longer holds"
                if deleted["rewritten"]
                else ""
            )
            print(
                f"{scan.project}: deleted {deleted['blobs']} blob(s) ({size_text(deleted['bytes'])}), {deleted['logs']} "
                f"run log(s) and {deleted['memo']} memo row(s){rewritten}; memo and graph compacted"
            )
            for warning in scan.warnings[shown:]:
                print(f"  warning: {warning}")
    if args.json:
        _output(args, scans)
    return 0


def cmd_scrub(args) -> int:
    if args.all and args.project:
        print("error: pass --project P or --all, not both", file=sys.stderr)
        return 2
    if args.purge and not args.yes and (args.json or not sys.stdin.isatty()):
        print("error: --purge deletes for good; pass --yes too when no terminal can confirm it", file=sys.stderr)
        return 2
    try:
        targets = project_targets(args)
    except ProjectError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    with contextlib.ExitStack() as held:
        try:
            for _, root in targets if args.purge else ():
                held.enter_context(holding_sources(root))
        except ScrubBusy as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        scans = _scan(targets, args.json)
        if args.purge:
            return _purge_all(args, scans)
    _output(args, scans)
    if not args.json:
        print(f"Count only, nothing was deleted. --purge deletes {_plan_line(totals(scans), len(scans))}.")
    return 0


def register(ksub) -> None:
    scrub = ksub.add_parser(
        "scrub", help="find old copies of secrets in this machine's kg home, and with --purge delete them"
    )
    scrub.add_argument("--project", help="project name or harness path (default: from the cwd)")
    scrub.add_argument("--all", action="store_true", help="every project directory of the kg home, and its shared memo")
    scrub.add_argument(
        "--purge",
        action="store_true",
        help="delete the old blobs holding a secret, the run logs of old runs naming them, memo rows holding one",
    )
    scrub.add_argument("--yes", action="store_true", help="with --purge: delete without asking")
    scrub.add_argument("--json", action="store_true", help="machine-readable output")
    scrub.set_defaults(func=cmd_scrub)
