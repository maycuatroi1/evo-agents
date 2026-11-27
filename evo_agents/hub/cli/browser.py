"""``evo-agents hub browser identity list|create|login|disable|enable|delete``: the browser identities of a project
(``evo_agents.hub.server.browser.service.identities``), over /v1/projects/{p}/browser-identities.

A browser identity is a Chromium profile kept logged in to some sites, which runs of the project use on the one worker
that holds it. ``list`` shows the project's identities and your own, with any grant on the project. ``create NAME``
makes one with no profile yet (version 0) for each ``--domain`` it keeps the cookies of, held by ``--worker`` (yours,
or a shared one, serving the project): your own with the writer role, or the project's with ``--owner project`` as an
admin. ``delete NAME`` deletes your identity of that name, or the project's (``--owner project``, as an admin); its
keys go from the hub and its profiles from the blob store. ``login NAME`` queues a login session (a run of kind
browser): Chromium on the identity's profile, which you open in the live view of the run on the web, sign in through and
Save; ``--worker`` moves the identity to another worker with it. ``disable`` and ``enable`` switch an identity off and
on. Each command prints metadata only, never a cookie or a key;
``--json`` prints the hub's answer. Standard library only, like the rest of the client.

The commands are classes of ``evo_agents.clikit`` (docs/structure.md, section Command classes): :class:`Identities` is
the gateway to the browser identities of a project, and :meth:`Identities.named` finds the one a command names.
"""

from __future__ import annotations

from evo_agents.clikit import Arg, Group, Result, Table, Text, UsageError, returns_array, returns_object
from evo_agents.hub.cli.base import PROJECT, HubCommand, HubContext, ProjectResource, when
from evo_agents.hub.cli.run.keys import RUN_KEYS

OWNERS = ("member", "project", "agent")
# The keys of a browser identity in --json (the contract, `evo-agents hub contract print`).
IDENTITY_KEYS = (
    "id",
    "project",
    "name",
    "owner",
    "owner_login",
    "agent",
    "agent_default",
    "domains",
    "login_url",
    "worker",
    "worker_owner",
    "status",
    "version",
    "size",
    "chromium_version",
    "held_by_run",
    "last_saved_at",
    "last_saved_by",
    "last_used_at",
    "created_at",
    "can_manage",
)


class Identities(ProjectResource):
    """The browser identities of a project: /v1/projects/{p}/browser-identities and the routes below it (Gateway)."""

    collection = "browser-identities"

    @classmethod
    def of(cls, ctx: HubContext) -> Identities:
        project = ctx.project  # the project is found before the hub is signed in to, as these commands always did
        return cls(ctx.hub, project)

    def list(self) -> list[dict]:
        return self.call("GET")

    def create(self, body: dict) -> dict:
        return self.call("POST", body=body)

    def named(self, name: str, owner: str | None) -> dict:
        """The identity ``name`` of ``owner`` (any, when None); UsageError when there is none, or more than one."""
        named = [i for i in self.list() if i["name"] == name]
        if owner is not None:
            named = [i for i in named if i["owner"] == owner]
        if not named:
            raise UsageError(f"project {self.project} has no browser identity {name} of yours or its own")
        if len(named) > 1:
            raise UsageError(f"both you and project {self.project} have a browser identity {name}: pass --owner")
        return named[0]

    def login(self, identity: int, worker: str | None) -> dict:
        return self.call("POST", identity, "login", body={"worker": worker})

    def switch(self, identity: int, action: str) -> dict:
        return self.call("POST", identity, action)

    def delete(self, identity: int) -> None:
        self.call("DELETE", identity)


def owner_text(identity: dict) -> str:
    if identity["owner"] == "agent":
        return f"agent {identity['agent']}" + (" (default)" if identity["agent_default"] else "")
    return "project" if identity["owner"] == "project" else identity["owner_login"]


NAME = Arg("name", metavar="NAME", help="the identity's name")
OWNER = Arg("--owner", choices=OWNERS, help="whose identity, when you and the project both have the name")
ONE_IDENTITY = returns_object(*IDENTITY_KEYS, schema="BrowserIdentity")


class IdentityList(HubCommand):
    name = "list"
    help = "the project's browser identities and your own: metadata, never a cookie"
    arguments = (PROJECT,)
    output = returns_array(*IDENTITY_KEYS, schema="BrowserIdentity")
    columns = ("ID", "NAME", "OWNER", "DOMAINS", "WORKER", "STATUS", "VERSION", "HELD BY", "LAST SAVED")

    def run(self, ctx: HubContext) -> Result:
        found = Identities.of(ctx).list()
        if not found:
            said = (
                f"Project {ctx.project} has no browser identity you can use; `hub browser identity create` makes one."
            )
            return Result(found, Text(said))
        return Result(found, Table(self.columns, [self.row(i) for i in found]))

    @staticmethod
    def row(i: dict) -> tuple:
        return (
            i["id"],
            i["name"],
            owner_text(i),
            ", ".join(i["domains"]),
            i["worker"],
            i["status"],
            i["version"],
            f"#{i['held_by_run']}" if i["held_by_run"] else "-",
            when(i["last_saved_at"]),
        )


class IdentityCreate(HubCommand):
    name = "create"
    help = "make a browser identity with no profile yet, held by one worker"
    arguments = (
        Arg("name", metavar="NAME", help="lower case, unique among its owner's identities in the project"),
        Arg(
            "--domain", action="append", required=True, metavar="HOST", help="a site it keeps the cookies of; repeat it"
        ),
        Arg("--worker", required=True, metavar="NAME", help="your worker, or a shared one, serving it"),
        Arg(
            "--owner",
            choices=OWNERS,
            default="member",
            help="member: yours (default); project: an admin's for the project; agent: an admin's for an agent profile "
            "(--agent), which its runs use by default",
        ),
        Arg("--agent", metavar="HANDLE", help="the agent profile owning the identity, with --owner agent"),
        Arg("--login-url", metavar="URL", help="the page its login session opens; default: its first domain"),
        PROJECT,
    )
    output = ONE_IDENTITY

    def run(self, ctx: HubContext) -> Result:
        args = ctx.args
        domains = [domain.strip().lower() for domain in args.domain]
        if any(not domain for domain in domains):
            raise UsageError("a --domain is a host name such as github.com, never empty")
        project = ctx.project
        body = {"name": args.name, "domains": domains, "worker": args.worker, "owner": args.owner}
        if args.login_url:
            body["login_url"] = args.login_url
        if (args.owner == "agent") != (args.agent is not None):
            raise UsageError("--owner agent goes with --agent HANDLE, and --agent with --owner agent")
        if args.agent:
            body["agent"] = args.agent
        created = Identities.of(ctx).create(body)
        whose = {"project": "the project's", "agent": f"agent {created['agent']}'s"}.get(created["owner"], "your")
        said = (
            f"Created {whose} browser identity {created['name']} (id {created['id']}) in {project}, held by worker "
            f"{created['worker']}, for {', '.join(created['domains'])}. It has no profile yet: log it in on its worker."
        )
        return Result(created, Text(said))


class IdentityLogin(HubCommand):
    name = "login"
    help = "queue a login session: Chromium on the identity's profile, to sign in through the live view"
    arguments = (
        NAME,
        Arg("--worker", metavar="NAME", help="move the identity to this worker with the session"),
        OWNER,
        PROJECT,
    )
    output = returns_object(*RUN_KEYS, schema="Run")

    def run(self, ctx: HubContext) -> Result:
        identities = Identities.of(ctx)
        identity = identities.named(ctx.args.name, ctx.args.owner)
        run = identities.login(identity["id"], ctx.args.worker)
        said = (
            f"Queued login session run #{run['id']} of browser identity {identity['name']} in {ctx.project}. Open the "
            "run on the web, sign in through its live view and press Save; cancelling the run saves nothing."
        )
        return Result(run, Text(said))


def switch(ctx: HubContext, action: str, done: str) -> Result:
    """``disable`` or ``enable`` the identity the command names."""
    identities = Identities.of(ctx)
    identity = identities.named(ctx.args.name, ctx.args.owner)
    changed = identities.switch(identity["id"], action)
    said = (
        f"Browser identity {changed['name']} (id {changed['id']}) of {ctx.project} is {done}: status "
        f"{changed['status']}."
    )
    return Result(changed, Text(said))


class IdentityDisable(HubCommand):
    name = "disable"
    help = "disable a browser identity: no run attaches it"
    arguments = (NAME, OWNER, PROJECT)
    output = ONE_IDENTITY

    def run(self, ctx: HubContext) -> Result:
        return switch(ctx, "disable", "disabled")


class IdentityEnable(HubCommand):
    name = "enable"
    help = "enable a disabled browser identity"
    arguments = (NAME, OWNER, PROJECT)
    output = ONE_IDENTITY

    def run(self, ctx: HubContext) -> Result:
        return switch(ctx, "enable", "enabled")


class IdentityDelete(HubCommand):
    name = "delete"
    help = "delete a browser identity: its keys and profiles go from the hub"
    arguments = (NAME, OWNER, PROJECT)
    output = returns_object(*IDENTITY_KEYS, "deleted", schema="BrowserIdentity", added=("deleted",))

    def run(self, ctx: HubContext) -> Result:
        identities = Identities.of(ctx)
        identity = identities.named(ctx.args.name, ctx.args.owner)
        identities.delete(identity["id"])
        said = (
            f"Deleted browser identity {identity['name']} (id {identity['id']}) of {ctx.project}: its keys are gone "
            "from the hub and its profiles from the blob store."
        )
        return Result({**identity, "deleted": True}, Text(said))


IDENTITY = Group(  # hub browser identity
    "identity",
    "browser identities: Chromium profiles kept logged in, each held by one worker",
    [IdentityList(), IdentityCreate(), IdentityLogin(), IdentityDisable(), IdentityEnable(), IdentityDelete()],
)
BROWSER = Group("browser", "browsers for runs: the identities they log in with", [IDENTITY])  # hub browser
