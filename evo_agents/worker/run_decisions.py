"""The decisions a run's agent asks its owner (``evo-agents worker ask``, ``evo_agents.worker.cli.ask``): the options,
the credential request and the approval a question carries, checked as the hub would check them; and the note of a
decision or a wait in the run's file of decisions, which the daemon waits on (``note_decision``), for every command
that ends in one. A problem is a WorkerStateError, which the command prints as one ``error:`` line.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from evo_agents.hub import credential_requests, runs
from evo_agents.hub.client import HubError
from evo_agents.worker import pull_heads
from evo_agents.worker.agent_input import decision_option
from evo_agents.worker.home import WorkerHome, WorkerStateError


def credential_request(args) -> dict | None:
    """The credential request of ``worker ask`` (``--env-var``, ``--secret-name``, ``--hub-account``), as the hub takes
    it; None when the agent asks none. Refused as the hub would refuse it, before it is asked."""
    env_var, secret_name = getattr(args, "env_var", None), getattr(args, "secret_name", None)
    hub_account = bool(getattr(args, "hub_account", False))
    if env_var is None and secret_name is None and not hub_account:
        return None
    if args.category != credential_requests.CATEGORY:
        raise WorkerStateError(
            f"--env-var, --secret-name and --hub-account ask for a credential: use --category "
            f"{credential_requests.CATEGORY}"
        )
    if args.option or args.wait_option:
        raise WorkerStateError(
            "a credential request takes the hub's options (paste the value, the owner's hub account, both, refuse): "
            "leave out --option and --wait-option"
        )
    refusal = credential_requests.request_refusal(env_var, secret_name, hub_account)
    if refusal is not None:
        raise WorkerStateError(refusal)
    request: dict = {"hub_account": hub_account}
    if env_var is not None:
        request |= {"env_var": env_var, "secret_name": secret_name or credential_requests.default_secret_name(env_var)}
    return request


def asked_approval(category: str, ref: str) -> dict:
    """The approval ``worker ask --pull`` binds its decision to: the pull request's head and scope as gh reads them in
    the run's directory (``pull_heads``)."""
    if category != "merge":
        raise WorkerStateError("--pull binds a merge approval to a pull request's head: use --category merge")
    bound = pull_heads.read(ref, None, Path.cwd())
    if bound is None:
        raise WorkerStateError(f"gh could not read pull request {ref} here: `gh pr view {ref}` says why")
    return bound


def asked_options(args) -> list[dict]:
    """The options of ``worker ask`` that asks no credential: 2 to 6, each with a key of its own."""
    options = [decision_option(text) for text in args.option or []]
    keys = [option["key"] for option in options]
    fewest, most = runs.DECISION_OPTIONS
    if not fewest <= len(options) <= most or len(set(keys)) != len(keys):
        raise WorkerStateError(f"a decision offers {fewest} to {most} options, each with a key of its own")
    for key in args.wait_option or []:
        if key not in keys:
            raise WorkerStateError(f"--wait-option names one of the options: {', '.join(keys)}")
        options[keys.index(key)]["waits"] = True
    if all(option.get("waits") for option in options):
        raise WorkerStateError("a decision needs an option that answers it: --wait-option names every one")
    return options


def note_decision(home: WorkerHome, run_id: int, decision_id, *, credential: bool = False) -> int:
    """Note decision ``decision_id`` of run ``run_id`` in the run's file of decisions, which the daemon waits on; its
    id. A credential request is noted as one, so the daemon takes the run's leases again once it is answered."""
    if not isinstance(decision_id, int):
        raise HubError("the hub did not answer with the decision's id")
    line = {"id": decision_id, "asked_at": datetime.now(UTC).isoformat(timespec="seconds")}
    if credential:
        line[credential_requests.NOTED] = True
    noted = json.dumps(line)
    with open(home.decisions_path(run_id), "a", encoding="utf-8") as handle:
        handle.write(noted + "\n")
    return decision_id


def noted_decision(home: WorkerHome, run_id: int, answer, *, credential: bool = False) -> int:
    """The id of the decision the hub answered with (``answer``), noted in the run's file of decisions."""
    decision_id = answer.get("id") if isinstance(answer, dict) else None
    return note_decision(home, run_id, decision_id, credential=credential)
