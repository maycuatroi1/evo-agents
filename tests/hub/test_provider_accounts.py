"""Provider accounts: a member's and an agent's API keys of model providers (``evo_agents.hub.server.provider_accounts``
and ``agent_provider_accounts``), plan model-providers, criterion a2.

A member adds, edits, rotates the key of, pauses, resumes and deletes one through the API and the CLI; the key is
sealed under the owner, name and kind provider and never comes back in a route, a log record, an audit row or an
error; an edit needs no key, a new key keeps the id; an env secret of the member serves as the key, copied and sealed
again while it stays and is still an account; Test connection and Fetch models call the endpoint with the key from
the hub, pinned to the address it checked, and answer the model count and a sanitized error; only public https base
URLs are kept and called, checked when written and again at the call; /v1/secrets neither lists, writes nor deletes a
provider account; a share of one is of class api_key; an agent's provider accounts are written by a project admin.
The endpoint is httpx.MockTransport and the resolver a dict, both set on the app's state."""

import io
import logging
import secrets

import httpx
import pytest

from tests.hub import live, pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from fastapi.testclient import TestClient
from sqlalchemy import select

from evo_agents.hub import tables
from evo_agents.hub.server import public_endpoint
from evo_agents.hub.server.app import create_app
from evo_agents.hub.server.provider_probe import sanitized
from evo_agents.hub.server.sealing import KEY_BYTES, Sealed, Sealer, agent_provider_aad, secret_aad
from tests.hub.live import sql, table_dump
from tests.hub.test_run_cli import write_credentials
from tests.hub.test_runs import OTHER, OWNER, PROJECT, add_worker, members
from tests.hub.test_secrets_cli import run_cli

KEY = secrets.token_bytes(KEY_BYTES)
PUBLIC = "93.184.216.34"
ADMIN_GRANT = {"role": "admin", "max_level": "internal"}
ADDRESSES = {
    "openrouter.ai": [PUBLIC],
    "api.openai.com": [PUBLIC],
    "api.z.ai": [PUBLIC],
    "llm.example.com": [PUBLIC],
    "inside.example.com": ["10.0.0.5"],
    "meta.example.com": ["169.254.169.254"],
    "nat.example.com": ["100.64.1.2"],
    "ula.example.com": ["fd00::1"],
    "mixed.example.com": [PUBLIC, "127.0.0.1"],
}
MODELS = {"data": [{"id": "openai/gpt-x"}, {"id": "anthropic/claude-y"}, {"id": "bad name"}]}


def sample(prefix: str = "sk-or-v1-") -> str:
    """A key drawn for this test, so that finding it anywhere cannot be a coincidence."""
    return prefix + secrets.token_hex(16)


class Endpoint:
    """The provider's API as the hub reaches it: GET /models answers the models to ``good`` and 401 to any other key,
    echoing the key it got, as some providers do."""

    def __init__(self) -> None:
        self.good: set[str] = set()
        self.seen: list[httpx.Request] = []
        self.models_path = True

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        got = request.headers.get("authorization", "").removeprefix("Bearer ")
        if request.url.path.endswith("/models") and not self.models_path:
            return httpx.Response(404, json={"error": "no such path"})
        if got not in self.good:
            return httpx.Response(401, json={"error": {"message": f"Incorrect API key provided: {got}"}})
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json=MODELS)
        return httpx.Response(200, json={"choices": [{"message": {"content": "hi"}}]})


async def resolve(host: str, port: int) -> list[str]:
    if host not in ADDRESSES:
        raise OSError("no such host")
    return list(ADDRESSES[host])


@pytest.fixture
def endpoint() -> Endpoint:
    return Endpoint()


@pytest.fixture
def client(hub_db, tmp_path, github, endpoint):
    config = live.hub_config(hub_db, tmp_path, github, secrets_key=KEY)
    app = create_app(config)
    app.state.provider_transport = httpx.MockTransport(endpoint)
    app.state.provider_resolver = resolve
    with TestClient(app, base_url="https://hub.test") as client:
        yield client


@pytest.fixture
def hub(client, github) -> dict:
    """The members of ``tests.hub.test_runs``, the owner an admin of the project, and the agent @auditor."""
    headers = members(client, github)
    granted = client.put(f"/v1/admin/projects/{PROJECT}/grants/{OWNER}", json=ADMIN_GRANT, headers=headers["admin"])
    assert granted.status_code == 200, granted.text
    made = client.post(
        f"/v1/projects/{PROJECT}/agents", json={"handle": "auditor", "name": "Auditor"}, headers=headers["owner"]
    )
    assert made.status_code == 201, made.text
    return headers


def path(name: str = "", tail: str = "") -> str:
    return "/v1/provider-accounts" + (f"/{name}" if name else "") + tail


def account(value: str | None = None, **extra) -> dict:
    found = {"provider": "openrouter", "projects": [PROJECT], **extra}
    return found | ({"value": value} if value is not None else {})


def written(client, headers, name: str, body: dict) -> dict:
    response = client.put(path(name), json=body, headers=headers)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    return response.json()


def stored(db, name: str) -> tuple:
    """(id, owner_id, profile_id, kind, sealed, nonce, key_id, deleted_at) of the secret ``name``, newest first."""
    s = tables.secrets
    query = select(s.c.id, s.c.owner_id, s.c.profile_id, s.c.kind, s.c.sealed, s.c.nonce, s.c.key_id, s.c.deleted_at)
    return sql(db, query.where(s.c.name == name).order_by(s.c.id.desc()))[0]


def opened(row: tuple, aad: bytes) -> str:
    return Sealer(KEY).open(Sealed(row[4], row[5], row[6]), aad)


def audit_rows(db) -> list[tuple]:
    a = tables.audit
    query = select(a.c.action, a.c.target).where(a.c.action.like("provider-account.%")).order_by(a.c.id)
    return sql(db, query)


def nowhere(client, db, headers, caplog, *values: str) -> None:
    """None of ``values`` in an answer of the reading routes, a log record, the audit or a table but sealed."""
    answers = [client.get(path(), headers=headers).text, client.get("/v1/secrets", headers=headers).text]
    dump = table_dump(db)
    for value in values:
        assert all(value not in text for text in answers)
        assert value not in caplog.text
        assert value not in str(audit_rows(db))
        assert value not in dump


# The member's routes


def test_provider_account_is_added_edited_rotated_paused_resumed_and_deleted_without_its_key_shown(
    client, hub, hub_db, endpoint, caplog
):
    caplog.set_level(logging.DEBUG)
    key, rotated = sample(), sample()
    endpoint.good |= {key, rotated}
    made = written(client, hub["owner"], "openrouter-main", account(key, models=["a/b"], models_from="endpoint"))
    assert made["created"] is True and "value" not in made and key not in str(made)
    assert (made["provider"], made["alias"], made["base_url"], made["api_format"]) == (
        "openrouter",
        "openrouter",
        "https://openrouter.ai/api/v1",
        "openai",
    )
    assert made["models"] == ["a/b", "openai/gpt-x", "anthropic/claude-y"]
    assert (made["projects"], made["workers"], made["state"], made["weight"]) == ([PROJECT], [], "ready", 1)
    row = stored(hub_db, "openrouter-main")
    secret_id, owner_id = row[0], row[1]
    assert row[3] == "provider" and opened(row, secret_aad(owner_id, "openrouter-main", "provider")) == key
    # the call went to the address checked, with the host name in Host and in TLS
    (seen,) = endpoint.seen
    assert seen.url.host == PUBLIC and seen.headers["host"] == "openrouter.ai"
    assert seen.extensions["sni_hostname"] == "openrouter.ai"

    # an edit needs no key: the key and the id stay
    changed = client.patch(
        path("openrouter-main"), json={"models": ["x/y"], "weight": 3, "priority": 2}, headers=hub["owner"]
    )
    assert changed.status_code == 200, changed.text
    assert (changed.json()["models"], changed.json()["weight"], changed.json()["priority"]) == (["x/y"], 3, 2)
    assert stored(hub_db, "openrouter-main")[:7] == row[:7]

    # a key change keeps the id, so weight and priority go on
    rotated_answer = client.patch(path("openrouter-main"), json={"value": rotated}, headers=hub["owner"])
    assert rotated_answer.status_code == 200, rotated_answer.text
    assert (rotated_answer.json()["weight"], rotated_answer.json()["priority"]) == (3, 2)
    after = stored(hub_db, "openrouter-main")
    assert after[0] == secret_id and opened(after, secret_aad(owner_id, "openrouter-main", "provider")) == rotated

    paused = client.patch(path("openrouter-main"), json={"paused": True}, headers=hub["owner"]).json()
    assert paused["state"] == "paused" and paused["paused_at"] is not None
    resumed = client.patch(path("openrouter-main"), json={"paused": False}, headers=hub["owner"]).json()
    assert resumed["state"] == "ready" and resumed["paused_at"] is None

    shown = client.get(path("openrouter-main"), headers=hub["owner"])
    assert shown.status_code == 200 and shown.headers["cache-control"] == "no-store"
    assert client.get(path("openrouter-main"), headers=hub["other"]).status_code == 404
    nowhere(client, hub_db, hub["owner"], caplog, key, rotated)

    assert client.delete(path("openrouter-main"), headers=hub["owner"]).status_code == 204
    gone = stored(hub_db, "openrouter-main")
    assert gone[0] == secret_id and gone[4:7] == (None, None, None) and gone[7] is not None
    b = tables.secret_bindings
    assert sql(hub_db, select(b.c.project_id).where(b.c.secret_id == secret_id)) == []
    assert client.get(path(), headers=hub["owner"]).json() == []
    assert [action for action, _ in audit_rows(hub_db)] == ["provider-account.put"] + ["provider-account.set"] * 4 + [
        "provider-account.delete"
    ]
    assert all(
        target.startswith(f"secret:{secret_id} provider=openrouter host=openrouter.ai")
        for _, target in audit_rows(hub_db)
    )


def test_provider_account_takes_its_key_from_an_env_secret_that_stays_and_still_leases(client, hub, hub_db):
    key = sample("zhipu-")
    secret = {"kind": "env", "env_var": "ZHIPU_API_KEY", "projects": [PROJECT], "value": key}
    assert client.put("/v1/secrets/zhipu-glm", json=secret, headers=hub["owner"]).status_code == 200
    made = written(client, hub["owner"], "glm-main", account(provider="glm", from_secret="zhipu-glm", models=["g"]))
    assert made["base_url"] == "https://api.z.ai/api/coding/paas/v4" and key not in str(made)
    copy, original = stored(hub_db, "glm-main"), stored(hub_db, "zhipu-glm")
    assert copy[0] != original[0] and copy[4] != original[4]
    assert opened(copy, secret_aad(copy[1], "glm-main", "provider")) == key
    assert opened(original, secret_aad(original[1], "zhipu-glm", "env")) == key and original[7] is None
    names = [s["name"] for s in client.get("/v1/secrets", headers=hub["owner"]).json()]
    assert names == ["zhipu-glm"]  # the provider account is not a secret of these routes
    accounts = client.get("/v1/accounts", headers=hub["owner"])
    listed = {a["name"]: (a["env_var"], a["gateway"] is not None) for a in accounts.json()}
    assert accounts.status_code == 200 and listed == {  # the env secret stays an account; the copy is a provider's
        "zhipu-glm": ("ZHIPU_API_KEY", False),
        "glm-main": ("gateway:glm", True),
    }
    # another member's secret, or a missing one, is not found; both or neither key is 422
    assert client.put(path("x"), json=account(from_secret="zhipu-glm"), headers=hub["other"]).status_code == 404
    both = client.put(path("x"), json=account(sample(), from_secret="zhipu-glm"), headers=hub["owner"])
    assert both.status_code == 422
    assert client.put(path("x"), json=account(), headers=hub["owner"]).status_code == 422


def test_provider_account_shares_the_name_space_of_secrets_and_is_never_written_by_the_secret_routes(
    client, hub, hub_db
):
    env = {"kind": "env", "env_var": "OPENAI_API_KEY", "projects": [PROJECT], "value": sample()}
    assert client.put("/v1/secrets/taken", json=env, headers=hub["owner"]).status_code == 200
    assert client.put(path("taken"), json=account(sample()), headers=hub["owner"]).status_code == 409
    written(client, hub["owner"], "mine", account(sample()))
    assert client.put("/v1/secrets/mine", json=env, headers=hub["owner"]).status_code == 409
    assert client.delete("/v1/secrets/mine", headers=hub["owner"]).status_code == 404
    assert stored(hub_db, "mine")[7] is None


def test_provider_account_needs_a_writer_role_and_the_members_own_workers(client, hub, hub_db):
    reader = {**account(sample()), "projects": ["nowhere"]}
    assert client.put(path("a"), json=reader, headers=hub["owner"]).status_code in (403, 404)
    assert client.put(path("a"), json=account(sample(), projects=[]), headers=hub["owner"]).status_code == 422
    add_worker(client, hub["other"], "theirs")
    foreign = client.put(path("a"), json=account(sample(), workers=["theirs"]), headers=hub["owner"])
    assert foreign.status_code == 403
    add_worker(client, hub["owner"], "box")
    assert written(client, hub["owner"], "a", account(sample(), workers=["box"]))["workers"] == ["box"]


@pytest.mark.parametrize(
    "url",
    [
        "http://openrouter.ai/api/v1",
        "https://127.0.0.1/v1",
        "https://10.1.2.3/v1",
        "https://169.254.169.254/latest",
        "https://100.64.0.1/v1",
        "https://[fd00::1]/v1",
        "https://[::ffff:127.0.0.1]/v1",
        "https://224.0.0.1/v1",
        "https://localhost/v1",
        "https://postgres:5432/v1",
        "https://hub.internal/v1",
        "https://user:pw@llm.example.com/v1",
        "https://llm.example.com/v1?x=1",
        "https://inside.example.com/v1",
        "https://meta.example.com/v1",
        "https://nat.example.com/v1",
        "https://ula.example.com/v1",
        "https://mixed.example.com/v1",
        "https://nowhere.example.com/v1",
    ],
)
def test_provider_account_refuses_a_base_url_off_the_public_internet(client, hub, hub_db, url):
    response = client.put(
        path("custom"), json=account(sample(), provider="custom-openai", base_url=url), headers=hub["owner"]
    )
    assert response.status_code == 422, response.text
    probe = client.post(
        path(tail="/probe"),
        json={"provider": "custom-openai", "base_url": url, "value": sample()},
        headers=hub["owner"],
    )
    assert probe.status_code in (200, 422) and probe.json().get("ok") is not True
    assert sql(hub_db, select(tables.provider_accounts.c.secret_id)) == []


def test_provider_account_is_checked_again_at_the_call_when_its_host_turned_private(client, hub, endpoint):
    key = sample()
    endpoint.good.add(key)
    written(
        client, hub["owner"], "custom", account(key, provider="custom-openai", base_url="https://llm.example.com/v1")
    )
    ADDRESSES["llm.example.com"] = ["10.9.9.9"]
    try:
        found = client.post(path("custom", "/probe"), json={}, headers=hub["owner"])
    finally:
        ADDRESSES["llm.example.com"] = [PUBLIC]
    assert found.status_code == 200 and found.json()["ok"] is False and "private" in found.json()["error"]
    assert endpoint.seen == []


def test_provider_account_probe_answers_the_model_count_and_a_sanitized_error(client, hub, endpoint):
    key, wrong = sample(), sample()
    endpoint.good.add(key)
    good = client.post(path(tail="/probe"), json={"provider": "openrouter", "value": key}, headers=hub["owner"])
    assert good.status_code == 200 and good.headers["cache-control"] == "no-store"
    assert (good.json()["ok"], good.json()["count"], good.json()["models"]) == (
        True,
        2,
        ["openai/gpt-x", "anthropic/claude-y"],
    )
    bad = client.post(path(tail="/probe"), json={"provider": "openrouter", "value": wrong}, headers=hub["owner"])
    assert bad.json()["ok"] is False and bad.json()["status"] == 401
    assert wrong not in bad.text and bad.json()["error"].startswith("HTTP 401")
    refused = client.put(path("w"), json=account(wrong, models_from="endpoint"), headers=hub["owner"])
    assert refused.status_code == 422 and wrong not in refused.text
    # an endpoint without GET /models gets the smallest request of its format with the model named
    endpoint.models_path = False
    written(client, hub["owner"], "nolist", account(key, models=["m-1"]))
    tried = client.post(path("nolist", "/probe"), json={}, headers=hub["owner"])
    assert (tried.json()["ok"], tried.json()["models"]) == (True, ["m-1"])
    assert endpoint.seen[-1].url.path == "/api/v1/chat/completions"


def test_provider_account_catalog_models_and_a_provider_that_signs_in_instead(client, hub):
    made = written(client, hub["owner"], "oa", account(sample(), provider="openai", models_from="catalog"))
    assert made["models"] == ["gpt-6.1-sol"]
    assert client.put(path("c"), json=account(sample(), provider="claude"), headers=hub["owner"]).status_code == 422
    assert client.put(path("c"), json=account(sample(), provider="no-such"), headers=hub["owner"]).status_code == 422
    custom = client.put(path("c"), json=account(sample(), provider="custom-openai"), headers=hub["owner"])
    assert custom.status_code == 422 and "base_url" in custom.text


def test_provider_account_share_is_of_class_api_key(client, hub):
    written(client, hub["owner"], "shared", account(sample()))
    share = {"grantee_kind": "user", "login": OTHER, "projects": [PROJECT]}
    made = client.post("/v1/secrets/shared/shares", json=share, headers=hub["owner"])
    assert made.status_code == 201, made.text
    assert made.json()["credential"] == "api_key"
    assert [s["id"] for s in client.get(path("shared"), headers=hub["owner"]).json()["shares"]] == [made.json()["id"]]


def test_provider_account_probe_sanitizes_keys_and_bearers():
    key = sample()
    said = sanitized(f"bad key {key}\nAuthorization: Bearer abcdefghijklmnop api_key=zzzzzzzzzzzzzzzz" + "x" * 400, key)
    assert key not in said and "abcdefghijklmnop" not in said and "zzzzzzzzzzzzzzzz" not in said
    assert "\n" not in said and len(said) <= 200


def test_provider_account_base_url_rules_without_the_network():
    assert public_endpoint.base_url("HTTPS://API.OpenAI.com/v1/") == "https://api.openai.com/v1"
    for host in ("127.0.0.1", "10.0.0.1", "169.254.169.254", "100.100.0.1", "fc00::1", "::ffff:10.0.0.1", "single"):
        assert public_endpoint.host_refusal(host) is not None
    assert public_endpoint.host_refusal("api.openai.com") is None


# An agent's provider accounts


def test_agent_provider_account_is_written_by_a_project_admin_and_sealed_for_its_profile(client, hub, hub_db):
    box = add_worker(client, hub["owner"], "box")
    base = f"/v1/projects/{PROJECT}/agents/auditor/provider-accounts"
    key, rotated = sample(), sample()
    body = {"provider": "openrouter", "value": key, "workers": ["box"], "models": ["a/b"]}
    refused = client.put(f"{base}/or", json=body, headers=hub["other"])
    assert refused.status_code == 403
    made = client.put(f"{base}/or", json=body, headers=hub["owner"])
    assert made.status_code == 200, made.text
    assert made.json()["created"] is True and key not in made.text and made.json()["workers"] == ["box"]
    row = stored(hub_db, "or")
    assert row[1] is None and row[3] == "provider" and opened(row, agent_provider_aad(row[2], "or")) == key
    assert box["id"] in [w for (w,) in sql(hub_db, select(tables.secret_bindings.c.worker_id))]
    env = {"env_var": "OPENAI_API_KEY", "value": sample(), "workers": ["box"]}
    clash = client.put(f"/v1/projects/{PROJECT}/agents/auditor/accounts/or", json=env, headers=hub["owner"])
    assert clash.status_code == 409
    changed = client.patch(f"{base}/or", json={"value": rotated, "weight": 4}, headers=hub["owner"])
    assert changed.status_code == 200, changed.text
    after = stored(hub_db, "or")
    assert after[0] == row[0] and opened(after, agent_provider_aad(row[2], "or")) == rotated
    listed = client.get(base, headers=hub["other"])
    assert listed.status_code == 200 and [a["weight"] for a in listed.json()] == [4] and rotated not in listed.text
    plain = client.get(f"/v1/projects/{PROJECT}/agents/auditor/accounts", headers=hub["owner"])
    assert plain.json() == []
    assert client.delete(f"{base}/or", headers=hub["owner"]).status_code == 204
    assert stored(hub_db, "or")[7] is not None and client.get(base, headers=hub["owner"]).json() == []


# The command line


def test_provider_account_cli_adds_lists_sets_pauses_and_deletes_without_printing_the_key(
    client, hub, hub_db, tmp_path, monkeypatch, capsys
):
    """The CLI against the app in this process: ``Hub`` calls go through the TestClient's transport."""
    from evo_agents.hub import client as hub_client

    home = write_credentials(tmp_path / "owner", "https://hub.test", OWNER, hub["owner"]["Authorization"][7:])
    real_call = hub_client.Hub.call

    def call(self, method, target, body=None, *, timeout=None):
        response = client.request(method, target, json=body, headers=hub["owner"])
        if response.status_code >= 400:
            return real_call(self, method, target, body, timeout=timeout)  # pragma: no cover
        return response.json() if response.content else None

    monkeypatch.setattr(hub_client.Hub, "call", call)
    key = sample()

    def cli(*args, stdin=None):
        return run_cli(monkeypatch, capsys, home, "hub", "provider-account", *args, stdin=stdin)

    added = cli(
        "add",
        "or-cli",
        "--provider",
        "openrouter",
        "--project",
        PROJECT,
        "--model",
        "a/b",
        stdin=io.StringIO(key + "\n"),
    )
    assert added.code == 0, added.err
    assert "Created provider account or-cli" in added.out
    listed = cli("list", "--json")
    assert listed.code == 0 and '"or-cli"' in listed.out
    assert cli("set", "or-cli", "--weight", "5").code == 0
    assert cli("pause", "or-cli").code == 0 and "paused" in cli("show", "or-cli").out
    assert cli("resume", "or-cli").code == 0
    row = stored(hub_db, "or-cli")
    assert opened(row, secret_aad(row[1], "or-cli", "provider")) == key
    assert cli("delete", "or-cli").code == 0
    for result in (added, listed):
        assert key not in result.out and key not in result.err
