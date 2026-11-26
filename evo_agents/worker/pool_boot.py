"""How a new machine becomes a node of a worker pool: the join token ``worker join`` takes (``join_secret``: its
--token, or EVO_JOIN_TOKEN, so it stays out of the shell's history and the process list), and the cloud-init
user-data ``worker pool user-data`` prints (``render_user_data``): ``deploy/worker/bootstrap.sh``, shipped in the
package as ``evo_agents/worker/bootstrap.sh``, with the hub and the join token, or one token per agent, written in.
Standard library only.
"""

from __future__ import annotations

import os
import shlex
from importlib import resources

from evo_agents.hub.client import HubError

JOIN_TOKEN_ENV = "EVO_JOIN_TOKEN"
AGENT_TOKENS_ENV = "EVO_AGENT_TOKENS"
BOOTSTRAP = "bootstrap.sh"  # in evo_agents.worker, a copy of deploy/worker/bootstrap.sh
SETTINGS_MARK = "# evo-bootstrap settings:"


def join_secret(args) -> tuple[str | None, str | None]:
    """(code, token) of ``worker join``: its --code, or its --token, else EVO_JOIN_TOKEN; exactly one of them."""
    token = args.token
    if args.code is not None and token is not None:
        raise HubError("give --code or --token, not both: a pairing code and a join token each join a machine alone")
    if args.code is None and token is None:
        token = os.environ.get(JOIN_TOKEN_ENV) or None
        if token is None:
            raise HubError(
                f"give --code with a pairing code from the hub's web, or --token (or {JOIN_TOKEN_ENV}) with a pool's "
                "join token from `evo-agents worker pool token create`"
            )
    return args.code, token


def bootstrap_script() -> str:
    """The text of deploy/worker/bootstrap.sh, as the package ships it."""
    return resources.files("evo_agents.worker").joinpath(BOOTSTRAP).read_text(encoding="utf-8")


def render_user_data(
    url: str, token: str | None, script: str | None = None, agent_tokens: dict[str, str] | None = None
) -> str:
    """The bootstrap script as cloud-init user-data: its settings line replaced with the hub and the join token, or,
    with ``agent_tokens`` (handle to the join token of that agent's pool), the hub and one node per agent."""
    script = bootstrap_script() if script is None else script
    lines = script.splitlines(keepends=True)
    marks = [index for index, line in enumerate(lines) if line.startswith(SETTINGS_MARK)]
    if len(marks) != 1 or not lines[0].startswith("#!"):
        raise HubError(f"{BOOTSTRAP} of this install has no single settings line; install evo-agents again")
    if agent_tokens:
        pairs = " ".join(f"{handle}={agent_token}" for handle, agent_token in agent_tokens.items())
        secret = f"{AGENT_TOKENS_ENV}={shlex.quote(pairs)}\n"
    else:
        secret = f"{JOIN_TOKEN_ENV}={shlex.quote(token or '')}\n"
    settings = (
        f"{SETTINGS_MARK} written by `evo-agents worker pool user-data`; the join token is a secret\n"
        f"EVO_HUB_URL={shlex.quote(url)}\n"
        f"{secret}"
    )
    lines[marks[0]] = settings
    return "".join(lines)
