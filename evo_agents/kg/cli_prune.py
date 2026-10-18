"""``evo-agents kg prune``: retention of this machine's kg home (``evo_agents.kg.retention``).

By default it only counts, for each project: the run logs it would delete or rewrite and why it keeps the others, the
rows of the runs table, the blobs, the history of the graph and the memo rows that the periods make old. ``--delete``
prints that, asks (or takes ``--yes``), deletes, compacts and says what went. ``--logs``, ``--history`` and ``--memo``
set the periods of the pass over the saved ones. ``--schedule on`` counts every project, asks, and saves the periods
with the schedule on: from then on every ``kg sync --due --all``, the LaunchAgent's command, prunes each project once
a day (``scheduled``). ``--schedule off`` stops that. The schedule is off until the owner turns it on.
"""

from __future__ import annotations

import contextlib
import json
import sys
from collections import Counter

from evo_agents.kg.cli_scrub import project_targets, size_text
from evo_agents.kg.corpus import kg_home
from evo_agents.kg.ids import utc_now
from evo_agents.kg.project import ProjectError
from evo_agents.kg.retention import (
    DEFAULTS,
    HUB,
    IN_USE,
    KEPT,
    PERIODS,
    WINDOW,
    WRITING,
    Config,
    Prune,
    RetentionError,
    apply,
    checked_periods,
    config_path,
    load_config,
    plan,
    policy_of,
    prune_scheduled,
    save_config,
)
from evo_agents.kg.scrub import ScrubBusy, holding_sources, project_dirs

KEPT_TEXT = {
    WINDOW: "within the logs period",
    HUB: "the hub has not received (evo-agents hub kg push sends them)",
    IN_USE: "in use",
    WRITING: "still being written",
}
TOTALS = ("logs", "log_bytes", "rewrite", "dropped", "runs", "blobs", "blob_bytes", "history", "builds", "memo")


def periods_text(periods: dict[str, str]) -> str:
    return ", ".join(f"{name} {periods[name]}" for name in PERIODS)


def print_plan(found: Prune) -> None:
    if found.busy or found.error:
        print(f"{found.project}: nothing read, {found.busy or found.error}")
        return
    counts, kept = found.counts(), found.kept
    keep = "; ".join(f"{kept[reason]} {KEPT_TEXT[reason]}" for reason in KEPT if kept[reason])
    print(f"{found.project}: {size_text(found.size)} on disk")
    print(
        f"  run logs: delete {counts['logs']} ({size_text(counts['log_bytes'])}), rewrite {counts['rewrite']} without "
        f"the {counts['dropped']} record(s) the corpus no longer holds" + (f"; keep {keep}" if keep else "")
    )
    print(f"  runs rows: delete {counts['runs']}, of runs whose log is gone or never was")
    print(f"  blobs: delete {counts['blobs']} ({size_text(counts['blob_bytes'])}), keep {found.blobs_held}")
    print(f"  graph history: delete {counts['history']} row(s) and {counts['builds']} build(s)")
    if found.memo_cut is None and found.memo_rows:
        print(f"  memo: {found.memo_rows} row(s), none deleted until a build has recorded the rows it uses")
    else:
        print(f"  memo: delete {counts['memo']} of {found.memo_rows} row(s)")
    for warning in found.warnings:
        print(f"  warning: {warning}")


def summary_line(found: Prune) -> str:
    """What one pass deleted in a project, on one line."""
    if found.busy or found.error:
        return f"project {found.project}: nothing deleted, {found.busy or found.error}"
    done = found.deleted or {}
    rewrote = f", rewrote {done['rewritten']}" if done.get("rewritten") else ""
    return (
        f"project {found.project}: deleted {done.get('logs', 0)} run log(s) ({size_text(done.get('log_bytes', 0))})"
        f"{rewrote}, {done.get('blobs', 0)} blob(s) ({size_text(done.get('blob_bytes', 0))}), {done.get('runs', 0)} "
        f"runs row(s), {done.get('history', 0)} history row(s) of {done.get('builds', 0)} build(s) and "
        f"{done.get('memo', 0)} memo row(s); {size_text(found.size)} -> {size_text(done.get('size_after') or 0)}"
    )


def totals(found: list[Prune]) -> dict[str, int]:
    total: Counter[str] = Counter()
    for one in found:
        total.update(one.counts())
    return {key: total[key] for key in TOTALS}


def plan_line(total: dict[str, int], projects: int) -> str:
    rewrite = f", rewrite {total['rewrite']} run log(s) in use" if total["rewrite"] else ""
    return (
        f"{total['logs']} run log(s) ({size_text(total['log_bytes'])}), {total['blobs']} blob(s) "
        f"({size_text(total['blob_bytes'])}), {total['runs']} runs row(s), {total['history']} history row(s) of "
        f"{total['builds']} build(s) and {total['memo']} memo row(s) in {projects} project(s){rewrite}"
    )


def _plans(targets, periods: dict[str, str], quiet: bool) -> list[Prune]:
    home, found = kg_home(), []
    for name, root in targets:
        if not quiet:
            print(f"reading {name} ...", file=sys.stderr, flush=True)
        found.append(plan(name, root, periods, policy=policy_of(name, home)))
    return found


def _json(found: list[Prune], **extra) -> None:
    body = {**extra, "home": str(kg_home()), "projects": [one.to_json() for one in found], "totals": totals(found)}
    print(json.dumps(body, ensure_ascii=False, indent=2))


def _asked(args, question: str) -> bool:
    if args.yes:
        return True
    return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")


def _count(args, targets, periods: dict[str, str], config: Config) -> int:
    found = _plans(targets, periods, args.json)
    if args.json:
        _json(found, delete=False, periods=periods, scheduled=config.scheduled)
        return 0
    for one in found:
        print_plan(one)
    schedule = "on" if config.scheduled else "off"
    print(
        f"Count only, nothing was deleted. Periods: {periods_text(periods)}. --delete deletes "
        f"{plan_line(totals(found), len(found))}. Scheduled pruning is {schedule} (kg prune --schedule on|off)."
    )
    return 0


def _delete(args, targets, periods: dict[str, str]) -> int:
    with contextlib.ExitStack() as held:
        try:
            for _, root in targets:
                held.enter_context(holding_sources(root))
        except ScrubBusy as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        found = _plans(targets, periods, args.json)
        if not args.json:
            for one in found:
                print_plan(one)
        total = totals(found)
        if any(total.values()):
            if not args.json:
                print(f"About to delete {plan_line(total, len(found))}. This cannot be undone.")
            if not _asked(args, "Delete them?"):
                print("Nothing was deleted.")
                return 1
        for one in found:
            apply(one)
            if not args.json:
                print(summary_line(one))
                for warning in one.warnings:
                    print(f"  warning: {warning}")
    if args.json:
        _json(found, delete=True, periods=periods)
    return 0


def _schedule(args, periods: dict[str, str]) -> int:
    if args.schedule == "off":
        path = save_config(Config(False, periods, utc_now()))
        print(f"Scheduled pruning is off ({path}); kg prune --delete still deletes by hand.")
        return 0
    found = _plans(project_dirs(kg_home()), periods, args.json)
    if not args.json:
        for one in found:
            print_plan(one)
        print(
            f"With the schedule on, every kg sync --due --all (the LaunchAgent runs it hourly) deletes, in each project "
            f"once a day, what periods {periods_text(periods)} make old: now {plan_line(totals(found), len(found))}. "
            "What it deletes cannot be undone."
        )
    if not _asked(args, "Turn scheduled pruning on?"):
        print("Scheduled pruning stays off.")
        return 1
    path = save_config(Config(True, periods, utc_now()))
    if args.json:
        _json(found, schedule="on", config=load_config().to_json())
    else:
        print(f"Scheduled pruning is on ({path}).")
    return 0


def _refused(args) -> str | None:
    if args.all and args.project:
        return "pass --project P or --all, not both"
    if args.schedule and (args.delete or args.project or args.all):
        return "--schedule covers every project of the kg home and deletes nothing now; drop --delete, --project, --all"
    if (args.delete or args.schedule == "on") and not args.yes and (args.json or not sys.stdin.isatty()):
        return "this deletes for good; pass --yes too when no terminal can confirm it"
    return None


def cmd_prune(args) -> int:
    refused = _refused(args)
    if refused:
        print(f"error: {refused}", file=sys.stderr)
        return 2
    try:
        config = load_config()
        periods = checked_periods({**config.periods, **{name: getattr(args, name) for name in PERIODS}})
        if args.schedule:
            return _schedule(args, periods)
        targets = project_targets(args)
    except (ProjectError, RetentionError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.delete:
        return _delete(args, targets, periods)
    return _count(args, targets, periods, config)


# The pass of a scheduled sync (kg sync --due --all)


def scheduled() -> tuple[list[Prune] | None, str | None]:
    """Prune what the schedule asks for now: the projects pruned (None while the schedule is off), and the error that
    stopped the pass, if one did."""
    try:
        return prune_scheduled(), None
    except RetentionError as exc:
        return None, f"{exc} (fix or delete {config_path()})"


def scheduled_json(found: list[Prune] | None, error: str | None) -> dict | None:
    if found is None and error is None:
        return None
    projects = found or []
    ok = error is None and all(one.error is None for one in projects)
    return {"ok": ok, "error": error, "projects": [one.to_json() for one in projects]}


def print_scheduled(found: list[Prune] | None, error: str | None) -> None:
    # stdout, like the rest of a scheduled run's output
    if error:
        print(f"FAIL prune: {error}")
    for one in found or []:
        print(("FAIL prune " if one.error else "prune ") + summary_line(one))
        for warning in one.warnings:
            print(f"     warning: {warning}")


def register(ksub) -> None:
    prune = ksub.add_parser(
        "prune",
        help="count, and with --delete delete, the old run logs, graph history and memo rows of this machine's kg home",
    )
    prune.add_argument("--project", help="project name or harness path (default: from the cwd)")
    prune.add_argument("--all", action="store_true", help="every project directory of the kg home, and its shared memo")
    prune.add_argument("--delete", action="store_true", help="delete what the count finds, after a yes")
    prune.add_argument("--yes", action="store_true", help="with --delete or --schedule on: do not ask")
    for name, what in (
        ("logs", "run logs, blobs and runs rows"),
        ("history", "the builds of the graph and the rows only they read"),
        ("memo", "memo rows after their last use"),
    ):
        prune.add_argument(
            f"--{name}",
            metavar="PERIOD",
            help=f"how long to keep {what}: 0, or a number with m, h, d or w (default {DEFAULTS[name]}, or the "
            "period --schedule saved)",
        )
    prune.add_argument(
        "--schedule",
        choices=("on", "off"),
        help="on: count, ask, and from then on prune every project once a day in kg sync --due --all; off: stop",
    )
    prune.add_argument("--json", action="store_true", help="machine-readable output")
    prune.set_defaults(func=cmd_prune)
