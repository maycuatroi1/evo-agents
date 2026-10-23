"""``evo-agents hub browser identity list|create|login|disable|enable|delete``: the browser identities of a project
(``evo_agents.hub.server.browser_identities``), over /v1/projects/{p}/browser-identities.

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
"""

from __future__ import annotations

import sys
from urllib.parse import quote

from evo_agents.hub.cli_agents import PROJECT_HELP, project_of
from evo_agents.hub.cli_client import client_command, print_json, signed_in, table, when
from evo_agents.hub.contract import json_option, returns_array, returns_object
from evo_agents.hub.run_cli_keys import RUN_KEYS

EXIT_USAGE = 2
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


def _path(project: str) -> str:
    return f"/v1/projects/{quote(project, safe='')}/browser-identities"


def _usage(text: str) -> int:
    print(f"error: {text}", file=sys.stderr)
    return EXIT_USAGE


def _owner(identity: dict) -> str:
    if identity["owner"] == "agent":
        return f"agent {identity['agent']}" + (" (default)" if identity["agent_default"] else "")
    return "project" if identity["owner"] == "project" else identity["owner_login"]


@client_command
def cmd_list(args) -> int:
    project = project_of(args)
    hub, _ = signed_in()
    found = hub.call("GET", _path(project))
    if args.json:
        print_json(found)
        return 0
    if not found:
        print(f"Project {project} has no browser identity you can use; `hub browser identity create` makes one.")
        return 0
    rows = [
        (
            i["id"],
            i["name"],
            _owner(i),
            ", ".join(i["domains"]),
            i["worker"],
            i["status"],
            i["version"],
            f"#{i['held_by_run']}" if i["held_by_run"] else "-",
            when(i["last_saved_at"]),
        )
        for i in found
    ]
    table(("ID", "NAME", "OWNER", "DOMAINS", "WORKER", "STATUS", "VERSION", "HELD BY", "LAST SAVED"), rows)
    return 0


@client_command
def cmd_create(args) -> int:
    domains = [domain.strip().lower() for domain in args.domain]
    if any(not domain for domain in domains):
        return _usage("a --domain is a host name such as github.com, never empty")
    project = project_of(args)
    body = {"name": args.name, "domains": domains, "worker": args.worker, "owner": args.owner}
    if args.login_url:
        body["login_url"] = args.login_url
    if (args.owner == "agent") != (args.agent is not None):
        return _usage("--owner agent goes with --agent HANDLE, and --agent with --owner agent")
    if args.agent:
        body["agent"] = args.agent
    hub, _ = signed_in()
    created = hub.call("POST", _path(project), body)
    if args.json:
        print_json(created)
        return 0
    whose = {"project": "the project's", "agent": f"agent {created['agent']}'s"}.get(created["owner"], "your")
    print(
        f"Created {whose} browser identity {created['name']} (id {created['id']}) in {project}, held by worker "
        f"{created['worker']}, for {', '.join(created['domains'])}. It has no profile yet: log it in on its worker."
    )
    return 0


def _named(hub, project: str, args) -> dict | None:
    """The identity ``args.name`` names, or None after a usage error is printed."""
    named = [i for i in hub.call("GET", _path(project)) if i["name"] == args.name]
    if args.owner is not None:
        named = [i for i in named if i["owner"] == args.owner]
    if not named:
        _usage(f"project {project} has no browser identity {args.name} of yours or its own")
        return None
    if len(named) > 1:
        _usage(f"both you and project {project} have a browser identity {args.name}: pass --owner")
        return None
    return named[0]


@client_command
def cmd_login(args) -> int:
    project = project_of(args)
    hub, _ = signed_in()
    identity = _named(hub, project, args)
    if identity is None:
        return EXIT_USAGE
    run = hub.call("POST", f"{_path(project)}/{identity['id']}/login", {"worker": args.worker})
    if args.json:
        print_json(run)
        return 0
    print(
        f"Queued login session run #{run['id']} of browser identity {identity['name']} in {project}. Open the run on "
        "the web, sign in through its live view and press Save; cancelling the run saves nothing."
    )
    return 0


def _switch(args, action: str, done: str) -> int:
    project = project_of(args)
    hub, _ = signed_in()
    identity = _named(hub, project, args)
    if identity is None:
        return EXIT_USAGE
    changed = hub.call("POST", f"{_path(project)}/{identity['id']}/{action}")
    if args.json:
        print_json(changed)
        return 0
    print(
        f"Browser identity {changed['name']} (id {changed['id']}) of {project} is {done}: status {changed['status']}."
    )
    return 0


@client_command
def cmd_disable(args) -> int:
    return _switch(args, "disable", "disabled")


@client_command
def cmd_enable(args) -> int:
    return _switch(args, "enable", "enabled")


@client_command
def cmd_delete(args) -> int:
    project = project_of(args)
    hub, _ = signed_in()
    identity = _named(hub, project, args)
    if identity is None:
        return EXIT_USAGE
    hub.call("DELETE", f"{_path(project)}/{identity['id']}")
    if args.json:
        print_json({**identity, "deleted": True})
        return 0
    print(
        f"Deleted browser identity {identity['name']} (id {identity['id']}) of {project}: its keys are gone from the "
        "hub and its profiles from the blob store."
    )
    return 0


# Registering


def register_browser(hsub) -> None:
    """``browser identity ...``, added to the subparsers ``hsub`` of ``hub``."""
    browser = hsub.add_parser("browser", help="browsers for runs: the identities they log in with")
    bsub = browser.add_subparsers(dest="browser_command", required=True)
    identity = bsub.add_parser(
        "identity", help="browser identities: Chromium profiles kept logged in, each held by one worker"
    )
    isub = identity.add_subparsers(dest="identity_command", required=True)

    listed = isub.add_parser("list", help="the project's browser identities and your own: metadata, never a cookie")
    listed.add_argument("--project", help=PROJECT_HELP)
    json_option(listed, returns_array(*IDENTITY_KEYS, schema="BrowserIdentity"))
    listed.set_defaults(func=cmd_list)

    create = isub.add_parser("create", help="make a browser identity with no profile yet, held by one worker")
    create.add_argument("name", metavar="NAME", help="lower case, unique among its owner's identities in the project")
    create.add_argument(
        "--domain", action="append", required=True, metavar="HOST", help="a site it keeps the cookies of; repeat it"
    )
    create.add_argument("--worker", required=True, metavar="NAME", help="your worker, or a shared one, serving it")
    create.add_argument(
        "--owner",
        choices=OWNERS,
        default="member",
        help="member: yours (default); project: an admin's for the project; agent: an admin's for an agent profile "
        "(--agent), which its runs use by default",
    )
    create.add_argument("--agent", metavar="HANDLE", help="the agent profile owning the identity, with --owner agent")
    create.add_argument(
        "--login-url", metavar="URL", help="the page its login session opens; default: its first domain"
    )
    create.add_argument("--project", help=PROJECT_HELP)
    json_option(create, returns_object(*IDENTITY_KEYS, schema="BrowserIdentity"))
    create.set_defaults(func=cmd_create)

    login = isub.add_parser(
        "login", help="queue a login session: Chromium on the identity's profile, to sign in through the live view"
    )
    login.add_argument("name", metavar="NAME", help="the identity's name")
    login.add_argument("--worker", metavar="NAME", help="move the identity to this worker with the session")
    login.add_argument("--owner", choices=OWNERS, help="whose identity, when you and the project both have the name")
    login.add_argument("--project", help=PROJECT_HELP)
    json_option(login, returns_object(*RUN_KEYS, schema="Run"))
    login.set_defaults(func=cmd_login)

    for action, helptext, func in (
        ("disable", "disable a browser identity: no run attaches it", cmd_disable),
        ("enable", "enable a disabled browser identity", cmd_enable),
    ):
        switched = isub.add_parser(action, help=helptext)
        switched.add_argument("name", metavar="NAME", help="the identity's name")
        switched.add_argument(
            "--owner", choices=OWNERS, help="whose identity, when you and the project both have the name"
        )
        switched.add_argument("--project", help=PROJECT_HELP)
        json_option(switched, returns_object(*IDENTITY_KEYS, schema="BrowserIdentity"))
        switched.set_defaults(func=func)

    delete = isub.add_parser("delete", help="delete a browser identity: its keys and profiles go from the hub")
    delete.add_argument("name", metavar="NAME", help="the identity's name")
    delete.add_argument("--owner", choices=OWNERS, help="whose identity, when you and the project both have the name")
    delete.add_argument("--project", help=PROJECT_HELP)
    json_option(delete, returns_object(*IDENTITY_KEYS, "deleted", schema="BrowserIdentity", added=("deleted",)))
    delete.set_defaults(func=cmd_delete)
