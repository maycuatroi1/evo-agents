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

from evo_agents.hub import runs
from evo_agents.hub.skill_sync import MARKER
from evo_agents.hub.skills import pack
from evo_agents.worker.pool_cli import JOIN_TOKEN_ENV
from tests.hub import live
from tests.hub.test_skills_api import publish, write_skill
from tests.worker.test_daemon import PROJECT, make_stack, wait_until  # noqa: F401 - the fixture of the daemon tests


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


def test_a_pool_nodes_daemon_syncs_the_hubs_skills_with_its_worker_token_and_no_hub_login(make_stack, s3, github):
    """Step 4 (criterion a5): the daemon of a node that joined with a join token writes the hub's global skills
    into the skills directory of each runtime installed on the machine when it starts, each with its marker, though
    nobody signed in to the hub there; its heartbeat names the feature skill_sync."""
    stack = make_stack(**s3.config())
    admin = live.bearer(live.sign_in(stack.client, github, live.ADMIN, 801)["token"])
    bundle = pack(write_skill(stack.tmp / "published", "house-style", "house rules"))
    publish(stack.client, {live.ADMIN: admin}, live.ADMIN, bundle.data, "house-style", None)
    assert stack.cli("pool", "create", "ci", "--project", PROJECT, "--json").returncode == 0
    issued = stack.cli("pool", "token", "create", "ci", "--json")
    assert issued.returncode == 0, issued.stderr
    env, state = _machine(stack, "node")
    joined = stack.cli("join", "--url", stack.url, "--token", json.loads(issued.stdout)["token"], env=env)
    assert joined.returncode == 0, joined.stderr
    home = state.parents[1]
    (home / ".claude").mkdir()  # claude-code is installed here
    assert not (home / ".evo" / "hub" / "token").exists()  # and nobody signed in to the hub

    stack.env, owner_env = env, stack.env
    try:
        proc = stack.start_daemon()
        marker = home / ".claude" / "skills" / "house-style" / MARKER
        wait_until(marker.is_file, f"{marker} written", explain=stack.daemon_output)
        written = json.loads(marker.read_text(encoding="utf-8"))
        assert (written["name"], written["version"], written["sha256"]) == ("house-style", 1, bundle.sha256)
        skill = (home / ".claude" / "skills" / "house-style" / "SKILL.md").read_text(encoding="utf-8")
        assert "house rules" in skill
        stack.wait_log("skills synced", proc)
        assert not (home / ".evo" / "hub" / "token").exists()

        (worker,) = stack.client.get("/v1/workers", headers=stack.owner).json()
        wait_until(
            lambda: (
                runs.SKILL_SYNC
                in stack.client.get(f"/v1/workers/{worker['id']}", headers=stack.owner).json()["features"]
            ),
            "the heartbeat to name skill_sync",
        )
        assert stack.stop_daemon(proc) == 0, stack.daemon_output()
    finally:
        stack.env = owner_env
