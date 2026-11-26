"""``evo-agents hub skills publish|list|sync|audience``: the client commands over ``evo_agents.hub.skill_sync``.

``publish`` prints the version it created, or that the hub had this bundle as the latest version already. ``list``
prints the skills one sees with their latest version and audience. ``audience`` makes a global skill everyone's or hub
admins' alone, and prints whether that changed it. ``sync`` prints what it did per skills directory, ``--check``
what it would do without writing anything; ``--quiet`` prints only errors on stderr and, with ``--check``, the number
of differences. ``--json`` prints the whole answer or report. Exit status 1 when something failed, 0 otherwise,
differences found by ``--check`` included.

The commands are classes of ``evo_agents.clikit`` (docs/structure.md, section Command classes), and :data:`SKILLS`
their group; ``skill_sync`` holds the calls to the hub, which the hooks make too.
"""

from __future__ import annotations

from evo_agents.clikit import NOTHING, Arg, Group, Result, Table, Text, returns_array, returns_object
from evo_agents.hub.cli.base import EXIT_FAILED, HubCommand, HubContext, when
from evo_agents.hub.cli.reports import Reported, to_stderr
from evo_agents.hub.skill_sync import (
    AUDIENCES,
    CURRENT,
    DONE,
    RUNTIMES,
    WRITES,
    Report,
    list_skills,
    parse_runtimes,
    parse_scope,
    publish,
    set_audience,
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
# The keys of a skill in `hub skills list --json` (the contract, `evo-agents hub contract print`).
SKILL_KEYS = (
    "scope",
    "project",
    "name",
    "version",
    "description",
    "sha256",
    "size",
    "source_repo",
    "source_commit",
    "published_by",
    "published_at",
    "audience",
)


def size_text(size: int) -> str:
    return (
        f"{size} B" if size < 1024 else f"{size / 1024:.1f} KiB" if size < 1024 * 1024 else f"{size / 1048576:.1f} MiB"
    )


def where(skill: dict) -> str:
    return "global" if skill.get("project") is None else f"project {skill['project']}"


class SkillsPublish(HubCommand):
    name = "publish"
    help = "pack a skill directory and publish it as the skill's next version"
    arguments = (
        Arg("dir", metavar="DIR", help="the skill directory, holding SKILL.md"),
        Arg(
            "--scope",
            default="global",
            help="global (a hub admin only) or project:NAME (the writer role on hub project NAME); default: global",
        ),
        Arg("--source-repo", help="the repo the directory comes from, such as agent-skills"),
        Arg("--source-commit", help="the commit of --source-repo it was published from"),
        Arg(
            "--audience",
            choices=AUDIENCES,
            help="who reads a global skill: everyone signed in, or hub admins alone; default: everyone for a new "
            "skill, unchanged for one that exists",
        ),
    )
    output = returns_object(
        "scope", "project", "name", "created", "latest", "audience", "bundle", schema="Published", added=("bundle",)
    )

    def run(self, ctx: HubContext) -> Result:
        args = ctx.args
        _, project = parse_scope(args.scope)
        published = publish(ctx.hub, args.dir, project, args.source_repo, args.source_commit, args.audience)
        latest = published["latest"]
        what = f"{where(published)} skill {published['name']}"
        if published["created"]:
            lines = [
                f"Published {what} version {latest['version']} ({size_text(latest['size'])}, sha256 {latest['sha256']})."
            ]
        else:
            lines = [f"The hub holds this bundle as {what} version {latest['version']} already; nothing changed."]
        if project is None and published.get("audience") == "admins":
            lines.append(
                "Hub admins alone see it: `evo-agents hub skills audience NAME everyone` opens it to every member."
            )
        return Result(published, Text(*lines))


class SkillsList(HubCommand):
    name = "list"
    help = "the skills you see, with their latest version"
    arguments = (Arg("--scope", help="global, project (every project you see) or project:NAME; default: all"),)
    output = returns_array(*SKILL_KEYS, schema="Skill")
    columns = ("PROJECT", "NAME", "VERSION", "SHA256", "SIZE", "PUBLISHED (UTC)", "BY", "SOURCE", "AUDIENCE")

    def run(self, ctx: HubContext) -> Result:
        args = ctx.args
        scope, project = parse_scope(args.scope, any_project=True) if args.scope else (None, None)
        found = list_skills(ctx.hub, scope, project)
        return Result(found, Table(self.columns, [self.row(s) for s in found], "No skill you can see on this hub."))

    @staticmethod
    def row(s: dict) -> tuple:
        return (
            s["project"] or "(global)",
            s["name"],
            s["version"],
            s["sha256"][:12],
            size_text(s["size"]),
            when(s["published_at"]),
            s["published_by"],
            f"{s['source_repo']}@{s['source_commit'][:12]}" if s.get("source_repo") else "-",
            s.get("audience", "everyone"),
        )


class SkillsAudience(HubCommand):
    name = "audience"
    help = "make a global skill everyone's or hub admins' alone (a hub admin only); one audit row a change"
    arguments = (
        Arg("name", metavar="NAME", help="the global skill"),
        Arg("audience", choices=AUDIENCES, help="everyone signed in, or hub admins alone"),
    )
    output = returns_object("name", "audience", "changed", schema="SkillAudience")

    def run(self, ctx: HubContext) -> Result:
        changed = set_audience(ctx.hub, ctx.args.name, ctx.args.audience)
        who = "every signed-in member" if changed["audience"] == "everyone" else "hub admins alone"
        if changed["changed"]:
            said = f"Global skill {changed['name']} is for {who} now."
        else:
            said = f"Global skill {changed['name']} is for {who} already; nothing changed."
        return Result(changed, Text(said))


def summary(report: Report) -> str:
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


def report_lines(report: Report) -> list[str]:
    lines = [summary(report)]
    for target in report.targets:
        shown = [a for a in target["actions"] if a["action"] != CURRENT]
        gitignore = target["gitignore"] in ("changed", "would change")
        if not shown and not gitignore:
            continue
        place = "global" if target["project"] is None else f"project {target['project']}"
        lines.append(f"{target['directory']} ({target['runtime']}, {place})")
        for action in shown:
            verb = (WOULD if report.check else DONE).get(action["action"]) or NOT_DONE[action["action"]]
            lines.append(f"  {verb:<14} {action['name']}: {action['detail']}")
        if gitignore:
            lines.append(
                f"  {'would update' if report.check else 'updated':<14} .gitignore: the block of synced skills"
            )
    if report.backup:
        lines.append(f"What was replaced or removed is saved in {report.backup}.")
    return lines + [f"note: {note}" for note in report.notes]


class SkillsSync(HubCommand):
    name = "sync"
    help = (
        "write the latest version of every skill you see into each runtime's skills directory, and the skills of "
        "your hub projects into their harnesses; directories the hub did not write are left alone"
    )
    arguments = (
        Arg("--runtime", help=f"some of {','.join(RUNTIMES)}, separated by commas (default: all that exist here)"),
        Arg("--check", action="store_true", help="say what would change; write and download nothing"),
        Arg(
            "--adopt",
            action="store_true",
            help="replace a directory of the same name that the hub did not write, after saving it in "
            "~/.evo/hub/backups",
        ),
        Arg("--quiet", action="store_true", help="print only errors, and with --check the number of differences"),
    )
    output = returns_object("hub", "check", "differences", "counts", "targets", "notes", "errors", "backup")

    def run(self, ctx: HubContext) -> Result:
        args = ctx.args
        runtimes = parse_runtimes(args.runtime)
        report = sync(ctx.hub, runtimes=runtimes, check=args.check, adopt=args.adopt)
        if args.quiet:
            view = Text(str(report.differences)) if args.check else NOTHING
        else:
            view = Text(*report_lines(report))
        errors = to_stderr(*(f"error: {error}" for error in report.errors))
        return Reported(report.as_json(), view, EXIT_FAILED if report.errors else 0, errors)


SKILLS = Group(
    "skills",
    "skills on the hub: publish a directory, list them, sync every runtime, set who reads one",
    [SkillsPublish(), SkillsList(), SkillsAudience(), SkillsSync()],
)
