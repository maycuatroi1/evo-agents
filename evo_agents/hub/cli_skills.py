"""``evo-agents hub skills publish|list|sync``: the client commands over ``evo_agents.hub.skill_sync``.

``publish`` prints the version it created, or that the hub had this bundle as the latest version already. ``list``
prints the skills one sees with their latest version. ``sync`` prints what it did per skills directory, ``--check``
what it would do without writing anything; ``--quiet`` prints only errors on stderr and, with ``--check``, the number
of differences. ``--json`` prints the whole answer or report. Exit status 1 when something failed, 0 otherwise,
differences found by ``--check`` included.
"""

from __future__ import annotations

import sys

from evo_agents.hub.cli_client import EXIT_FAILED, _client_command, _print_json, _signed_in, _table, _when
from evo_agents.hub.skill_sync import (
    CURRENT,
    DONE,
    RUNTIMES,
    WRITES,
    Report,
    list_skills,
    parse_runtimes,
    parse_scope,
    publish,
    sync,
)

WOULD = {
    "install": "would install",
    "update": "would update",
    "restore": "would restore",
    "adopt": "would adopt",
    "remove": "would remove",
}
NOT_DONE = {"unmanaged": "not managed", "kept": "left as is"}


def _size(size: int) -> str:
    return (
        f"{size} B" if size < 1024 else f"{size / 1024:.1f} KiB" if size < 1024 * 1024 else f"{size / 1048576:.1f} MiB"
    )


def _where(skill: dict) -> str:
    return "global" if skill.get("project") is None else f"project {skill['project']}"


@_client_command
def cmd_skills_publish(args) -> int:
    _, project = parse_scope(args.scope)
    hub, _ = _signed_in()
    published = publish(hub, args.dir, project, args.source_repo, args.source_commit)
    if args.json:
        _print_json(published)
        return 0
    latest = published["latest"]
    what = f"{_where(published)} skill {published['name']}"
    if published["created"]:
        print(f"Published {what} version {latest['version']} ({_size(latest['size'])}, sha256 {latest['sha256']}).")
    else:
        print(f"The hub holds this bundle as {what} version {latest['version']} already; nothing changed.")
    return 0


@_client_command
def cmd_skills_list(args) -> int:
    scope, project = parse_scope(args.scope, any_project=True) if args.scope else (None, None)
    hub, _ = _signed_in()
    found = list_skills(hub, scope, project)
    if args.json:
        _print_json(found)
        return 0
    if not found:
        print("No skill you can see on this hub.")
        return 0
    rows = [
        (
            s["project"] or "(global)",
            s["name"],
            s["version"],
            s["sha256"][:12],
            _size(s["size"]),
            _when(s["published_at"]),
            s["published_by"],
            f"{s['source_repo']}@{s['source_commit'][:12]}" if s.get("source_repo") else "-",
        )
        for s in found
    ]
    _table(("PROJECT", "NAME", "VERSION", "SHA256", "SIZE", "PUBLISHED (UTC)", "BY", "SOURCE"), rows)
    return 0


def _summary(report: Report) -> str:
    counts = report.counts()
    if report.check:
        parts = [f"{counts[key]} to {key}" for key in WRITES if counts.get(key)]
        parts += [f"{counts[key]} {NOT_DONE[key]}" for key in NOT_DONE if counts.get(key)]
        parts.append(f"{counts.get(CURRENT, 0)} up to date")
        return (
            f"Checked against {report.hub}, nothing written: {report.differences} difference(s); "
            + ", ".join(parts)
            + "."
        )
    parts = [f"{counts[key]} {DONE[key]}" for key in WRITES if counts.get(key)]
    parts += [f"{counts[key]} {NOT_DONE[key]}" for key in NOT_DONE if counts.get(key)]
    parts.append(f"{counts.get(CURRENT, 0)} up to date")
    return f"Synced from {report.hub}: " + ", ".join(parts) + "."


def _print_report(report: Report) -> None:
    print(_summary(report))
    for target in report.targets:
        shown = [a for a in target["actions"] if a["action"] != CURRENT]
        gitignore = target["gitignore"] in ("changed", "would change")
        if not shown and not gitignore:
            continue
        place = "global" if target["project"] is None else f"project {target['project']}"
        print(f"{target['directory']} ({target['runtime']}, {place})")
        for action in shown:
            verb = (WOULD if report.check else DONE).get(action["action"]) or NOT_DONE[action["action"]]
            print(f"  {verb:<14} {action['name']}: {action['detail']}")
        if gitignore:
            print(f"  {'would update' if report.check else 'updated':<14} .gitignore: the block of synced skills")
    if report.backup:
        print(f"What was replaced or removed is saved in {report.backup}.")
    for note in report.notes:
        print(f"note: {note}")


@_client_command
def cmd_skills_sync(args) -> int:
    runtimes = parse_runtimes(args.runtime)
    hub, _ = _signed_in()
    report = sync(hub, runtimes=runtimes, check=args.check, adopt=args.adopt)
    if args.json:
        _print_json(report.as_json())
    elif args.quiet:
        if args.check:
            print(report.differences)
    else:
        _print_report(report)
    for error in report.errors:
        print(f"error: {error}", file=sys.stderr)
    return EXIT_FAILED if report.errors else 0


def register_skills(hsub) -> None:
    skills = hsub.add_parser("skills", help="skills on the hub: publish a directory, list them, sync every runtime")
    ssub = skills.add_subparsers(dest="skills_command", required=True)

    publish_parser = ssub.add_parser(
        "publish", help="pack a skill directory and publish it as the skill's next version"
    )
    publish_parser.add_argument("dir", metavar="DIR", help="the skill directory, holding SKILL.md")
    publish_parser.add_argument(
        "--scope",
        default="global",
        help="global (a hub admin only) or project:NAME (the writer role on hub project NAME); default: global",
    )
    publish_parser.add_argument("--source-repo", help="the repo the directory comes from, such as agent-skills")
    publish_parser.add_argument("--source-commit", help="the commit of --source-repo it was published from")
    publish_parser.add_argument("--json", action="store_true", help="machine-readable output")
    publish_parser.set_defaults(func=cmd_skills_publish)

    listed = ssub.add_parser("list", help="the skills you see, with their latest version")
    listed.add_argument("--scope", help="global, project (every project you see) or project:NAME; default: all")
    listed.add_argument("--json", action="store_true", help="machine-readable output")
    listed.set_defaults(func=cmd_skills_list)

    synced = ssub.add_parser(
        "sync",
        help="write the latest version of every skill you see into each runtime's skills directory, and the skills of "
        "your hub projects into their harnesses; directories the hub did not write are left alone",
    )
    synced.add_argument(
        "--runtime", help=f"some of {','.join(RUNTIMES)}, separated by commas (default: all that exist here)"
    )
    synced.add_argument("--check", action="store_true", help="say what would change; write and download nothing")
    synced.add_argument(
        "--adopt",
        action="store_true",
        help="replace a directory of the same name that the hub did not write, after saving it in ~/.evo/hub/backups",
    )
    synced.add_argument(
        "--quiet", action="store_true", help="print only errors, and with --check the number of differences"
    )
    synced.add_argument("--json", action="store_true", help="machine-readable output")
    synced.set_defaults(func=cmd_skills_sync)
