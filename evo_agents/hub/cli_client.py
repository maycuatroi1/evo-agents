"""``evo-agents hub login``, ``logout``, ``whoami``, ``token``, ``admin``, ``project`` and ``registry``: the client
side of the hub.

Standard library only, like ``client``; ``project register`` reads the harness with the harness loader (PyYAML, a
core dependency). Results go to stdout; a failure is one ``error:`` line on stderr and exit status 1, and a 401
says to run ``evo-agents hub login``. ``--json`` prints what the hub answered. No command ever prints a token.
``hub memory`` is in ``cli_memory``.
"""

from __future__ import annotations

import functools
import json
import sys
from datetime import datetime, timezone
from urllib.parse import quote

from evo_agents.hub.access import ROLES
from evo_agents.hub.client import (
    Hub,
    HubError,
    NotSignedIn,
    check_url,
    hub_dir,
    load_credentials,
    login,
    remove_credentials,
)

EXIT_FAILED = 1


def _client_command(func):
    """Turn a HubError of ``func`` into an ``error:`` line and exit status 1."""

    @functools.wraps(func)
    def run(args) -> int:
        try:
            return func(args)
        except HubError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return EXIT_FAILED

    return run


def _signed_in():
    credentials = load_credentials()
    return Hub(credentials.url, credentials.token), credentials


def _print_json(data) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2))


def _when(value: str | None) -> str:
    """An ISO timestamp from the hub as ``YYYY-MM-DD HH:MM`` UTC; ``-`` for none."""
    if not value:
        return "-"
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    if moment.tzinfo is not None:
        moment = moment.astimezone(timezone.utc)
    return moment.strftime("%Y-%m-%d %H:%M")


def _grants(grants: list[dict]) -> str:
    return ", ".join(f"{g['project']} {g['role']} up to {g['max_level']}" for g in grants) or "none"


def _table(header: tuple[str, ...], rows: list[tuple]) -> None:
    widths = [max(len(str(cell)) for cell in column) for column in zip(header, *rows, strict=False)]
    for row in (header, *rows):
        print("  ".join(str(cell).ljust(width) for cell, width in zip(row, widths, strict=True)).rstrip())


@_client_command
def cmd_login(args) -> int:
    url = args.url
    if not url:
        try:
            url = load_credentials().url
        except HubError:
            raise HubError("which hub? pass --url, for example --url https://agents.omelet.tech") from None
    signed = login(check_url(url))
    role = " (admin)" if signed.get("admin") else ""
    print(f"Signed in to {check_url(url)} as {signed['login']}{role}; the token is in {hub_dir() / 'token'}.")
    return 0


@_client_command
def cmd_logout(args) -> int:
    try:
        hub, credentials = _signed_in()
    except NotSignedIn:
        print("Not signed in; nothing to do.")
        return 0
    outcome = "the token is revoked and deleted from this machine"
    try:
        hub.call("POST", "/v1/auth/logout")
    except HubError as exc:
        if exc.status == 401:  # revoked or expired already: nothing left to revoke
            outcome = "the token was no longer valid and is deleted from this machine"
        elif args.force:
            outcome = f"the token is deleted from this machine but NOT revoked ({exc})"
        else:
            raise HubError(
                f"{exc}. The token was not revoked and stays in {hub_dir()}; try again, or pass --force to delete "
                "it anyway (it then stays valid on the hub until it expires or is revoked from another machine)"
            ) from None
    remove_credentials()
    print(f"Signed out of {credentials.url}; {outcome}.")
    return 0


@_client_command
def cmd_whoami(args) -> int:
    hub, credentials = _signed_in()
    me = hub.call("GET", "/v1/auth/whoami")
    if args.json:
        _print_json({**me, "url": credentials.url})
        return 0
    token = me["token"]
    print(f"{me['login']}{' (admin)' if me['admin'] else ''} on {credentials.url}")
    print(f"token {token['id']}: {token['kind']} on {token['host'] or 'the web'}, expires {_when(token['expires_at'])}")
    print(f"grants: {_grants(me['grants'])}")
    return 0


@_client_command
def cmd_token_list(args) -> int:
    hub, _ = _signed_in()
    tokens = hub.call("GET", "/v1/tokens?all=true" if args.all else "/v1/tokens")
    if args.json:
        _print_json(tokens)
        return 0
    rows = [
        (
            ("*" if t["current"] else " ") + str(t["id"]),
            t["kind"],
            t["host"] or "-",
            _when(t["created_at"]),
            _when(t["last_used_at"]),
            _when(t["expires_at"]),
            t["state"],
        )
        for t in tokens
    ]
    _table((" ID", "KIND", "HOST", "CREATED", "LAST USED", "EXPIRES (UTC)", "STATE"), rows)
    print("* the token of this machine")
    return 0


@_client_command
def cmd_token_revoke(args) -> int:
    hub, credentials = _signed_in()
    current = hub.call("GET", "/v1/auth/whoami")["token"]["id"]
    hub.call("DELETE", f"/v1/tokens/{args.id}")
    if args.id == current:
        remove_credentials()
        print(f"Revoked token {args.id}, the token of this machine; run `evo-agents hub login` to sign in again.")
    else:
        print(f"Revoked token {args.id}.")
    return 0


def _grant_path(project: str, login: str) -> str:
    return f"/v1/admin/projects/{quote(project, safe='')}/grants/{quote(login, safe='')}"


@_client_command
def cmd_admin_grant(args) -> int:
    hub, _ = _signed_in()
    body = {"role": args.role, "max_level": args.max_level}
    grant = hub.call("PUT", _grant_path(args.project, args.login), body)
    verb = "Granted" if grant["created"] else "Changed the grant:"
    print(f"{verb} {grant['login']} {grant['role']} on {grant['project']}, up to level {grant['max_level']}.")
    return 0


@_client_command
def cmd_admin_revoke(args) -> int:
    hub, _ = _signed_in()
    hub.call("DELETE", _grant_path(args.project, args.login))
    print(f"Revoked the grant of {args.login} on {args.project}.")
    return 0


@_client_command
def cmd_admin_users(args) -> int:
    hub, _ = _signed_in()
    users = hub.call("GET", "/v1/admin/users")
    if args.json:
        _print_json(users)
        return 0
    rows = [
        (
            u["login"],
            "yes" if u["admin"] else "no",
            "yes" if u["signed_in"] else "not yet",
            _when(u["last_seen_at"]),
            u["active_tokens"],
            _grants(u["grants"]),
        )
        for u in users
    ]
    _table(("LOGIN", "ADMIN", "SIGNED IN", "LAST SEEN (UTC)", "TOKENS", "GRANTS"), rows)
    return 0


@_client_command
def cmd_admin_stats(args) -> int:
    hub, _ = _signed_in()
    counts = hub.call("GET", "/v1/admin/stats")
    if args.json:
        _print_json(counts)
        return 0
    _table(("TABLE", "ROWS"), sorted(counts.items()))
    return 0


def _project_path(project: str) -> str:
    return f"/v1/projects/{quote(project, safe='')}"


def _hub_sink(project: dict) -> str:
    sink = next((s for s in project["sinks"] if s["kind"] == "hub"), None)
    if sink is None:
        return "none"
    clearance = sink["clearance"]
    return clearance["level"] + (f"/{clearance['location']}" if clearance.get("location") else "")


@_client_command
def cmd_project_register(args) -> int:
    from evo_agents.hub.registration import registration

    found = registration(args.root)
    hub, _ = _signed_in()
    project = hub.call("PUT", _project_path(found.project), found.body)
    if args.json:
        _print_json(project)
        return 0
    counts = f"{len(project['repos'])} repo(s), {len(project['sinks'])} sink(s), levels {' < '.join(project['levels'])}"
    if project["created"]:
        print(f"Registered project {project['name']} from {found.root}: {counts}.")
    elif project["changed"]:
        print(f"Updated project {project['name']} from {found.root}: {counts}.")
    else:
        print(f"Project {project['name']} is registered as {found.root} declares it; nothing changed.")
    warning = found.push_warning()
    if warning:
        print(f"note: {warning}")
    return 0


@_client_command
def cmd_project_list(args) -> int:
    hub, _ = _signed_in()
    projects = hub.call("GET", "/v1/projects")
    if args.json:
        _print_json(projects)
        return 0
    rows = [
        (p["name"], p["role"] or "-", p["max_level"] or "-", _hub_sink(p), len(p["repos"]), _when(p["updated_at"]))
        for p in projects
    ]
    _table(("PROJECT", "ROLE", "MAX LEVEL", "HUB SINK", "REPOS", "UPDATED (UTC)"), rows)
    return 0


@_client_command
def cmd_registry_pull(args) -> int:
    from pathlib import Path

    from evo_agents.hub.registry import pull

    hub, _ = _signed_in()
    workspace = Path(args.workspace).expanduser().resolve() if args.workspace else None
    registry = Path(args.registry).expanduser() if args.registry else None
    result = pull(hub, registry, workspace)
    if args.json:
        backup = str(result.backup) if result.backup else None
        _print_json(
            {"registry": str(result.registry), "backup": backup, "clusters": result.clusters, "skipped": result.skipped}
        )
        return 0
    if not result.changed:
        print(f"{result.registry} already holds every hub project you see; nothing written.")
    elif result.backup:
        print(f"Wrote {result.registry}; the previous version is saved as {result.backup}.")
    else:
        print(f"Wrote {result.registry}, which did not exist before.")
    rows = [
        (c["status"], c["name"], c["project"], c["root"] + ("" if c["present"] else "  (not on this machine)"))
        for c in result.clusters
    ]
    if rows:
        _table(("STATUS", "CLUSTER", "PROJECT", "ROOT"), rows)
    for skipped in result.skipped:
        print(f"skipped {skipped}")
    return 0


def register_client(hsub) -> None:
    login_parser = hsub.add_parser("login", help="sign in to a hub with GitHub (device flow) and keep a machine token")
    login_parser.add_argument("--url", help="the hub, such as https://agents.omelet.tech (default: the last one used)")
    login_parser.set_defaults(func=cmd_login)

    logout = hsub.add_parser("logout", help="revoke this machine's token on the hub and delete it here")
    logout.add_argument(
        "--force", action="store_true", help="delete the local token even when the hub cannot be reached to revoke it"
    )
    logout.set_defaults(func=cmd_logout)

    whoami = hsub.add_parser("whoami", help="the signed-in login, whether it is an admin, and its grants")
    whoami.add_argument("--json", action="store_true", help="machine-readable output")
    whoami.set_defaults(func=cmd_whoami)

    token = hsub.add_parser("token", help="your hub tokens")
    tsub = token.add_subparsers(dest="token_command", required=True)
    token_list = tsub.add_parser("list", help="your tokens with kind, host, last use and expiry")
    token_list.add_argument("--all", action="store_true", help="include revoked and expired tokens")
    token_list.add_argument("--json", action="store_true", help="machine-readable output")
    token_list.set_defaults(func=cmd_token_list)
    revoke = tsub.add_parser("revoke", help="revoke one of your tokens")
    revoke.add_argument("id", metavar="ID", type=int, help="token id, from `hub token list`")
    revoke.set_defaults(func=cmd_token_revoke)

    admin = hsub.add_parser("admin", help="users and grants (hub admins only)")
    asub = admin.add_subparsers(dest="admin_command", required=True)
    grant = asub.add_parser("grant", help="give a login a role on a project, or change it")
    grant.add_argument("login", metavar="LOGIN", help="GitHub login; it may not have signed in yet")
    grant.add_argument("project", metavar="PROJECT", help="project name on the hub")
    grant.add_argument("--role", required=True, choices=ROLES)
    grant.add_argument("--max-level", required=True, help="highest label level of the project the login may see")
    grant.set_defaults(func=cmd_admin_grant)
    admin_revoke = asub.add_parser("revoke", help="take away the role a login has on a project")
    admin_revoke.add_argument("login", metavar="LOGIN")
    admin_revoke.add_argument("project", metavar="PROJECT")
    admin_revoke.set_defaults(func=cmd_admin_revoke)
    users = asub.add_parser("users", help="every user with admin flag, last sign-in, tokens and grants")
    users.add_argument("--json", action="store_true", help="machine-readable output")
    users.set_defaults(func=cmd_admin_users)
    stats = asub.add_parser("stats", help="rows in every hub table")
    stats.add_argument("--json", action="store_true", help="machine-readable output")
    stats.set_defaults(func=cmd_admin_stats)

    project = hsub.add_parser("project", help="projects on the hub, registered from their harness")
    psub = project.add_subparsers(dest="project_command", required=True)
    register = psub.add_parser(
        "register",
        help="register the project of a harness (its knowledge.yaml), or bring the hub up to date with it; "
        "the first registration needs a hub admin",
    )
    register.add_argument(
        "root", metavar="HARNESS_ROOT", nargs="?", help="the harness, or a directory inside it (default: the cwd)"
    )
    register.add_argument("--json", action="store_true", help="machine-readable output")
    register.set_defaults(func=cmd_project_register)
    project_list = psub.add_parser("list", help="the projects you see, with your role and the hub sink of each")
    project_list.add_argument("--json", action="store_true", help="machine-readable output")
    project_list.set_defaults(func=cmd_project_list)

    registry = hsub.add_parser("registry", help="the harness registry of this machine")
    rsub = registry.add_subparsers(dest="registry_command", required=True)
    pull = rsub.add_parser(
        "pull",
        help="write the cluster of every hub project you see into the registry; other clusters stay as they are, "
        "and the previous file is kept as registry.json.bak.<timestamp>",
    )
    pull.add_argument(
        "--workspace", help="where the harnesses and repos live here (default: each project's, such as ~/github)"
    )
    pull.add_argument("--registry", help="the registry file (default: ~/.claude/harness/registry.json)")
    pull.add_argument("--json", action="store_true", help="machine-readable output")
    pull.set_defaults(func=cmd_registry_pull)

    from evo_agents.hub.cli_memory import register_memory

    register_memory(hsub)
