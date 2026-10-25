"""The contract of the model gateway's client with the real image, plan model-providers, step 3: the pinned OmniRoute
started by the entrypoint of deploy/hub/docker-compose.yml, managed by ``GatewayClient`` signed in with the password
derived from the root secret, as the api does. ``FakeGateway`` (test_gateway.py) stands for the same API in the other
tests.

Marked gateway_image and skipped without Docker: it pulls the image and starts Node, which takes a minute. Run it with
``python -m pytest -m gateway_image tests/hub/test_gateway_image.py``; ``-m "not gateway_image"`` leaves it out."""

import asyncio
import secrets
import shutil
import subprocess
import time
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from evo_agents.hub.server.gateway_client import GATEWAY_IMAGE, GatewayClient, GatewayError
from tests.hub.test_compose import compose


def _docker_works() -> bool:
    """Docker answers and runs Linux images. Docker on a Windows runner answers in Windows containers mode, where the
    gateway's Linux image does not start and ``docker run`` fails."""
    if not shutil.which("docker"):
        return False
    info = subprocess.run(["docker", "info", "--format", "{{.OSType}}"], capture_output=True, text=True, timeout=30)
    return info.returncode == 0 and info.stdout.strip() == "linux"


pytestmark = [
    pytest.mark.gateway_image,
    pytest.mark.skipif(not _docker_works(), reason="needs Docker to run the gateway's image"),
]

START_SECONDS = 180


def _service() -> dict:
    return compose()["services"]["gateway"]


def _script() -> str:
    """The entrypoint's script as the shell gets it: compose reads $$ as $."""
    return _service()["entrypoint"][-1].replace("$$", "$")


def _run(secret: str, *extra: str) -> str:
    environment = dict(_service()["environment"], EVO_HUB_GATEWAY_SECRET=secret)
    flags = [arg for name, value in environment.items() for arg in ("-e", f"{name}={value}")]
    name = f"evo-gateway-contract-{secrets.token_hex(4)}"
    command = ["docker", "run", "-d", "--name", name, "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true"]
    command += [*flags, *extra, "--entrypoint", "/bin/sh", GATEWAY_IMAGE, "-c", _script()]
    subprocess.run(command, check=True, capture_output=True, timeout=600)
    return name


def _remove(name: str) -> None:
    subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=60)


@pytest.fixture(scope="module")
def gateway():
    secret = secrets.token_urlsafe(32)
    name = _run(secret, "-p", "127.0.0.1::20128")
    try:
        port = subprocess.run(["docker", "port", name, "20128"], check=True, capture_output=True, text=True)
        url = "http://" + port.stdout.splitlines()[0].strip()
        deadline = time.monotonic() + START_SECONDS
        while time.monotonic() < deadline:
            try:
                if httpx.get(url + "/api/health", timeout=2).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(1)
        else:
            logs = subprocess.run(["docker", "logs", name], capture_output=True, text=True).stdout[-2000:]
            pytest.fail(f"the gateway did not answer within {START_SECONDS} s:\n{logs}")
        yield url, secret, name
    finally:
        _remove(name)


async def _round_trip(url: str, secret: str) -> dict:
    client = GatewayClient(url, secret)
    try:
        seen = {"healthy": await client.healthy()}
        node = await client.create_node("evo-7", "evo-7", "openai-compatible", "chat", "https://llm.example.com/v1")
        on_node = await client.create_connection(node.id, "evo-7-0123456789abcdef", "sk-node-" + secrets.token_hex(8))
        seen["new_inactive"] = on_node.active is False
        await client.update_connection(on_node.id, active=True)
        catalog = await client.create_connection(
            "openrouter", "evo-8-0123456789abcdef", "sk-or-" + secrets.token_hex(8), "https://openrouter.ai/api/v1"
        )
        renamed = await client.update_connection(catalog.id, name="evo-8-fedcba9876543210", key="sk-or-x" * 3)
        seen["renamed_in_place"] = (renamed.id, renamed.name) == (catalog.id, "evo-8-fedcba9876543210")
        listed = {c.id: c for c in await client.connections()}
        seen["listed"] = listed[on_node.id].active and listed[on_node.id].provider == node.id and catalog.id in listed
        seen["node_listed"] = [n.id for n in await client.nodes()] == [node.id]
        issued = await client.create_key(
            "run-1", [on_node.id], datetime(2030, 1, 1, 7, tzinfo=timezone(timedelta(hours=7)))
        )
        value = issued.value.get_secret_value()
        keys = {k.id: k for k in await client.keys()}
        seen["key"] = keys[issued.id].no_log and keys[issued.id].allowed_connections == (on_node.id,)
        seen["usage"] = await client.key_usage(issued.id)
        seen["with_key"] = await _infer(url, value)
        await client.revoke_key(issued.id)
        seen["revoked_gone"] = issued.id not in {k.id for k in await client.keys()}
        seen["after_revoke"] = await _infer(url, value)
        seen["without_key"] = await _infer(url, None)
        await client.delete_node(node.id)
        seen["cascade"] = on_node.id not in {c.id for c in await client.connections()}
        await client.delete_connection(catalog.id)
        seen["empty"] = await client.connections() == []
        return seen
    finally:
        await client.aclose()


async def _infer(url: str, key: str | None) -> int:
    """The status of a chat completion with ``key``; 401 comes at once when the gateway refuses it."""
    headers = {"authorization": f"Bearer {key}"} if key else {}
    body = {"model": "evo-7/some-model", "messages": [{"role": "user", "content": "hi"}]}
    async with httpx.AsyncClient(timeout=10) as http:
        try:
            return (await http.post(url + "/v1/chat/completions", json=body, headers=headers)).status_code
        except httpx.ReadTimeout:
            return 0  # past the key check, waiting on the provider's endpoint, which does not exist


def test_gateway_image_contract_sign_in_connections_nodes_keys_and_usage(gateway):
    url, secret, name = gateway
    seen = asyncio.run(_round_trip(url, secret))
    usage = seen.pop("usage")
    assert (usage.requests, usage.prompt_tokens, usage.completion_tokens) == (0, 0, 0)
    with_key, after_revoke, without_key = seen.pop("with_key"), seen.pop("after_revoke"), seen.pop("without_key")
    assert with_key != 401, "a live key reaches the provider (which fails: the endpoint is fake)"
    assert after_revoke == 401 and without_key == 401, "REQUIRE_API_KEY: no key, or a revoked one, is refused"
    assert all(seen.values()), seen
    environ = subprocess.run(
        [
            "docker",
            "exec",
            name,
            "sh",
            "-c",
            "tr '\\0' '\\n' < /proc/1/environ; tr '\\0' '\\n' < /proc/$(pgrep -n node)/environ",
        ],
        capture_output=True,
        text=True,
    ).stdout
    assert secret not in environ, "the entrypoint drops the root secret before the gateway starts"


def test_gateway_image_contract_refuses_a_client_with_another_secret(gateway):
    url, _, _ = gateway

    async def listed():
        client = GatewayClient(url, secrets.token_urlsafe(32))
        try:
            return await client.connections()
        finally:
            await client.aclose()

    with pytest.raises(GatewayError, match="sign in"):
        asyncio.run(listed())


def test_gateway_image_contract_exits_at_once_when_off():
    name = _run("")
    try:
        waited = subprocess.run(["docker", "wait", name], capture_output=True, text=True, timeout=120)
        logs = subprocess.run(["docker", "logs", name], capture_output=True, text=True)
    finally:
        _remove(name)
    assert waited.stdout.strip() == "0" and "gateway off: EVO_HUB_GATEWAY_SECRET is not set" in logs.stdout
