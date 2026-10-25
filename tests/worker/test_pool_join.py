"""``evo-agents worker pool`` and ``evo-agents worker join --token``, step 1 of the worker-pools plan (criterion a1):
the owner signed in on one machine creates a pool and a join token with the CLI; machines that are not signed in join
it with the token from the flag or from EVO_JOIN_TOKEN, each as a worker of its own with its own ``evw_`` token, named
after the pool and the host (the same host here, so the second takes a suffix), with the pool's labels and pool=<name>;
the token used up, the next machine is refused as with a wrong code, and the token never reaches the output."""

import json
import re
import socket

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)
pytest.importorskip("aiohttp", reason="the daemon needs the worker extra, evo-ak[worker]")

from evo_agents.worker.pool_cli import JOIN_TOKEN_ENV
from tests.worker.test_daemon import PROJECT, make_stack  # noqa: F401 - the fixture of the daemon tests


def _machine(stack, name: str) -> tuple[dict, object]:
    """The environment of another machine, with a home of its own and nobody signed in, and its worker's state."""
    home = stack.tmp / name
    home.mkdir()
    return {**stack.env, "HOME": str(home)}, home / ".evo" / "worker"


def _config(state) -> dict:
    return json.loads((state / "config.json").read_text(encoding="utf-8"))


def test_the_owner_makes_a_pool_and_machines_join_it_with_one_token(make_stack):
    stack = make_stack()
    made = stack.cli("pool", "create", "ci", "--project", PROJECT, "--slots", "2", "--label", "gpu", "--json")
    assert made.returncode == 0, made.stderr
    assert (json.loads(made.stdout)["name"], json.loads(made.stdout)["labels"]) == ("ci", ["gpu"])
    issued = stack.cli("pool", "token", "create", "ci", "--max-uses", "2", "--hours", "1", "--json")
    assert issued.returncode == 0, issued.stderr
    token = json.loads(issued.stdout)["token"]
    assert token.startswith("evj_")
    shown = stack.cli("pool", "token", "list", "ci")
    assert shown.returncode == 0 and "live" in shown.stdout and token not in shown.stdout

    host = re.sub(r"[^a-z0-9_-]+", "-", socket.gethostname().split(".")[0].lower()).strip("-_") or "node"
    expected = host if host == "ci" or host.startswith("ci-") else f"ci-{host}"
    first_env, first_state = _machine(stack, "first")
    first = stack.cli("join", "--url", stack.url, "--token", token, env=first_env)
    assert first.returncode == 0, first.stderr
    second_env, second_state = _machine(stack, "second")
    second = stack.cli("join", "--url", stack.url, env={**second_env, JOIN_TOKEN_ENV: token})
    assert second.returncode == 0, second.stderr
    names = []
    for result, state in ((first, first_state), (second, second_state)):
        assert "Joined" in result.stdout and token not in result.stdout + result.stderr
        config = _config(state)
        assert (config["slots"], config["labels"]) == (2, ["gpu", "pool=ci"])
        worker_token = (state / "token").read_text(encoding="utf-8").strip()
        assert worker_token.startswith("evw_")
        names.append(config["name"])
    assert names == [expected, f"{expected}-2"]  # one host name here: the second machine takes a suffix
    assert (first_state / "token").read_text() != (second_state / "token").read_text()

    third_env, third_state = _machine(stack, "third")
    refused = stack.cli("join", "--url", stack.url, env={**third_env, JOIN_TOKEN_ENV: token})
    assert refused.returncode == 1, refused.stdout
    assert "join token is wrong, expired, revoked or used up" in refused.stderr
    assert not (third_state / "token").exists()
    neither = stack.cli("join", "--url", stack.url, env=third_env)
    assert neither.returncode == 1 and "give --code" in neither.stderr
    both = stack.cli("join", "--url", stack.url, "--code", "ABCD-EFGH", "--token", token, env=third_env)
    assert both.returncode == 1 and "not both" in both.stderr

    listed = stack.cli("pool", "show", "ci", "--json")
    assert listed.returncode == 0, listed.stderr
    pool = json.loads(listed.stdout)
    assert sorted(node["name"] for node in pool["nodes"]) == sorted(names)
    changed = stack.cli("pool", "set", "ci", "--slots", "3", "--no-labels", "--json")
    assert changed.returncode == 0, changed.stderr
    assert (json.loads(changed.stdout)["slots"], json.loads(changed.stdout)["labels"]) == (3, [])
    for node in pool["nodes"]:
        worker = stack.client.get(f"/v1/workers/{node['id']}", headers=stack.owner).json()
        assert (worker["slots"], worker["labels"], worker["pool"]) == (3, ["pool=ci"], "ci")

    (token_row,) = json.loads(stack.cli("pool", "token", "list", "ci", "--json").stdout)
    assert (token_row["status"], token_row["uses"]) == ("used", 2)
    revoked = stack.cli("pool", "token", "revoke", "ci", str(token_row["id"]))
    assert revoked.returncode == 0 and "Revoked" in revoked.stdout, revoked.stderr
    pools = stack.cli("pool", "list")
    assert pools.returncode == 0 and "Pool ci" in pools.stdout
    missing = stack.cli("pool", "show", "nope")
    assert missing.returncode == 1 and "no pool named nope" in missing.stderr
