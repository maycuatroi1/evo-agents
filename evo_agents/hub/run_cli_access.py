"""``evo-agents hub run access RUN --as-me|--revoke``, for ``evo_agents.hub.run_cli``: let a run of yours call the hub's
API as you, secrets, tokens and workers aside, or stop it (PUT /v1/projects/{p}/runs/{id}/access). With ``--revoke``
the run's token ends at once, and its agent's next call to the hub gets 401 (``docs/credentials.md``, "A run that runs
as its owner"). Only the run's owner may, never a run's own token. Standard library only, like the rest of the client.
"""

from __future__ import annotations

from evo_agents.hub.cli_client import client_command, print_json, project_path, signed_in
from evo_agents.hub.contract import json_option
from evo_agents.hub.run_cli_options import AS_ME_HELP


def said(run: dict) -> str:
    """What ``access`` prints of the run the hub answered."""
    if run.get("as_owner"):
        return (
            f"Run #{run['id']} runs as you: while a worker holds it, its agent calls the hub's API as you, secrets, "
            f"tokens and workers aside, through a token of its own. `evo-agents hub run access {run['id']} --revoke` "
            "stops it."
        )
    return f"Run #{run['id']} no longer runs as you: the token it held, if any, has ended."


def cmd_access(args, project) -> int:
    hub, _ = signed_in()
    run = hub.call("PUT", f"{project_path(project(args))}/runs/{args.run}/access", {"as_me": args.as_me})
    if args.json:
        print_json(run)
        return 0
    print(said(run))
    return 0


def register_access(rsub, with_run, project, run_json) -> None:
    access = rsub.add_parser(
        "access",
        help="let a run of yours call the hub's API as you, secrets, tokens and workers aside, or stop it (owner only)",
    )
    with_run(access)
    chosen = access.add_mutually_exclusive_group(required=True)
    chosen.add_argument("--as-me", dest="as_me", action="store_true", default=None, help=AS_ME_HELP)
    chosen.add_argument(
        "--revoke",
        dest="as_me",
        action="store_false",
        default=None,
        help="stop it: the run's token ends at once, and its agent's next call to the hub gets 401",
    )
    json_option(access, run_json)
    access.set_defaults(func=client_command(lambda args: cmd_access(args, project)))
