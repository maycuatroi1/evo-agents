"""``evo-agents hub project register|list|archive|unarchive``: the projects on the hub, registered from their harness.

``register`` reads the harness (its knowledge.yaml) through ``registration`` and registers its project, or brings the
hub up to date with it; the first registration needs a hub admin. ``list`` prints the projects the login sees with its
role and the hub sink of each (``--archived`` adds the archived ones, with when). ``archive`` stops a project taking new
runs, kg pushes and builds, and ``unarchive`` makes it live again; :func:`archive_lines` says which it is.

The commands are classes of ``evo_agents.clikit`` (docs/structure.md, section Command classes): :class:`Projects` is
the gateway to ``/v1/projects``. Standard library only, like the rest of the client; ``register`` reads the harness
with the harness loader (PyYAML, a core dependency).
"""

from __future__ import annotations

from urllib.parse import quote

from evo_agents.clikit import Arg, Group, Result, Table, Text, returns_array, returns_object
from evo_agents.hub.cli.base import HubCommand, HubContext, HubRoutes, when

PROJECT_KEYS = (
    "name",
    "harness",
    "levels",
    "locations",
    "default_label",
    "sinks",
    "repos",
    "role",
    "max_level",
    "created_at",
    "updated_at",
    "archived_at",
    "archived_by",
)
COLUMNS = ("PROJECT", "ROLE", "MAX LEVEL", "HUB SINK", "REPOS", "UPDATED (UTC)")


class Projects(HubRoutes):
    """GET /v1/projects, PUT /v1/projects/{p}, and POST .../archive and .../unarchive (Gateway)."""

    base = "projects"

    @staticmethod
    def one(project: str) -> str:
        return f"/{quote(project, safe='')}"

    def list(self, archived: bool) -> list[dict]:
        return self.call("GET", "?archived=true" if archived else "")

    def register(self, project: str, body: dict) -> dict:
        return self.call("PUT", self.one(project), body)

    def change_state(self, project: str, action: str) -> dict:
        return self.call("POST", f"{self.one(project)}/{action}")


def hub_sink(project: dict) -> str:
    """The clearance of the project's hub sink, as ``level`` or ``level/location``; ``none`` without one."""
    sink = next((s for s in project["sinks"] if s["kind"] == "hub"), None)
    if sink is None:
        return "none"
    clearance = sink["clearance"]
    return clearance["level"] + (f"/{clearance['location']}" if clearance.get("location") else "")


def archive_lines(project: dict) -> list[str]:
    """What ``hub project archive`` and ``unarchive`` print of the project the hub answered."""
    name = project["name"]
    if project["archived_at"] is None:
        return [f"Project {name} is live: runs, kg pushes and builds, its night shift, Loop and plugins' jobs go on."]
    return [
        f"Project {name} is archived since {when(project['archived_at'])} UTC, by {project['archived_by']}: it takes no "
        "new run, kg push or build, and the hub runs nothing for it; what it holds reads as before.",
        f"Make it live again with `evo-agents hub project unarchive {name}`.",
    ]


class ProjectRegister(HubCommand):
    name = "register"
    help = (
        "register the project of a harness (its knowledge.yaml), or bring the hub up to date with it; "
        "the first registration needs a hub admin"
    )
    arguments = (
        Arg("root", metavar="HARNESS_ROOT", nargs="?", help="the harness, or a directory inside it (default: the cwd)"),
    )
    output = returns_object(*PROJECT_KEYS, "created", "changed", schema="Registered")

    def run(self, ctx: HubContext) -> Result:
        from evo_agents.hub.registration import registration  # the harness loader, imported only when it runs

        found = registration(ctx.args.root)
        project = Projects(ctx.hub).register(found.project, found.body)
        counts = (
            f"{len(project['repos'])} repo(s), {len(project['sinks'])} sink(s), levels {' < '.join(project['levels'])}"
        )
        if project["created"]:
            lines = [f"Registered project {project['name']} from {found.root}: {counts}."]
        elif project["changed"]:
            lines = [f"Updated project {project['name']} from {found.root}: {counts}."]
        else:
            lines = [f"Project {project['name']} is registered as {found.root} declares it; nothing changed."]
        warning = found.push_warning()
        if warning:
            lines.append(f"note: {warning}")
        return Result(project, Text(*lines))


class ProjectList(HubCommand):
    name = "list"
    help = "the projects you see, with your role and the hub sink of each"
    arguments = (Arg("--archived", action="store_true", help="the archived projects too, with when"),)
    output = returns_array(*PROJECT_KEYS, schema="Project")

    def run(self, ctx: HubContext) -> Result:
        archived = ctx.args.archived
        projects = Projects(ctx.hub).list(archived)
        header = (*COLUMNS, "ARCHIVED (UTC)") if archived else COLUMNS
        return Result(projects, Table(header, [self.row(p, archived) for p in projects], bare=True))

    @staticmethod
    def row(project: dict, archived: bool) -> tuple:
        row = (
            project["name"],
            project["role"] or "-",
            project["max_level"] or "-",
            hub_sink(project),
            len(project["repos"]),
            when(project["updated_at"]),
        )
        return (*row, when(project["archived_at"])) if archived else row


STATE_ARGUMENTS = (Arg("project", metavar="PROJECT", help="the project's name on the hub"),)
STATE_OUTPUT = returns_object(*PROJECT_KEYS, schema="Project")


def change_state(ctx: HubContext, action: str) -> Result:
    """``archive`` or ``unarchive``: the action of the same name on the project, and what it is now."""
    project = Projects(ctx.hub).change_state(ctx.args.project, action)
    return Result(project, Text(*archive_lines(project)))


class ProjectArchive(HubCommand):
    name = "archive"
    help = (
        "archive a project: it takes no new run, kg push or build, and the hub runs nothing for it; what it holds "
        "reads as before (an admin of it; its active runs cancelled first)"
    )
    arguments = STATE_ARGUMENTS
    output = STATE_OUTPUT

    def run(self, ctx: HubContext) -> Result:
        return change_state(ctx, "archive")


class ProjectUnarchive(HubCommand):
    name = "unarchive"
    help = "make an archived project live again (an admin of it)"
    arguments = STATE_ARGUMENTS
    output = STATE_OUTPUT

    def run(self, ctx: HubContext) -> Result:
        return change_state(ctx, "unarchive")


PROJECT_LIST = ProjectList()
PROJECT = Group(
    "project",
    "projects on the hub, registered from their harness",
    [ProjectRegister(), PROJECT_LIST, ProjectArchive(), ProjectUnarchive()],
)
