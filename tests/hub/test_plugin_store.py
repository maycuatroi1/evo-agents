"""The hub's store of plugins (``evo_agents.hub.server.plugin_store``): a hub admin publishes a signed bundle through
the blob store, installs, upgrades and removes a version; the hub refuses, with the reason in the message, a bundle
that is unsigned, signed wrongly, altered, of a publisher the operator does not trust, a core plugin of a publisher
that is not core, or outside its engines and plugin API; at each start it turns off an installed plugin that no
longer runs on its release and tells the hub admins, and serves on. Then ``evo-agents hub plugin`` against
``hub serve``.

Everything here needs the hub: it skips without EVO_HUB_TEST_DSN and gets a database and a moto bucket of its own
(``tests.hub.s3``). Sigstore signatures go through ``tests.plugins.samples.FakeSigstore``."""

from __future__ import annotations

import json
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from evo_agents.plugins.signing import SignatureUnavailable, parse_publishers
from tests.hub import live, pg
from tests.hub.contract_keys import assert_json_keys
from tests.plugins.samples import (
    OTHER_SEED,
    SIGNER,
    WORKFLOW,
    FakeSigstore,
    bundle_of,
    ed25519_publishers,
    ed25519_signature,
    sigstore_signature,
)

needs_pg = pytest.mark.skipif(not pg.DSN, reason=pg.SKIP_REASON)
pytest.importorskip("cryptography")

# acme signs with a key and with its release workflow, evo is a core publisher, beta is trusted for another plugin.
PUBLISHERS = (
    f"{ed25519_publishers()},acme=sigstore:{WORKFLOW},{ed25519_publishers(publisher='evo')},"
    f"{ed25519_publishers(OTHER_SEED, 'beta')}"
)


def make_app(hub_db, tmp_path, s3, *, publishers: str = PUBLISHERS, hub_version: str | None = None, sigstore=None):
    from evo_agents.hub.config import HubConfig
    from evo_agents.hub.server.app import create_app

    config = HubConfig(
        dsn=hub_db.dsn,
        data_dir=tmp_path / "cache",
        pool_min_size=1,
        pool_max_size=4,
        pool_timeout=5.0,
        admins=frozenset({live.ADMIN}),
        plugin_publishers=parse_publishers(publishers),
        plugin_core_publishers=frozenset({"evo"}),
        **s3.config(),
    )
    from evo_agents.hub.plugin_calls import CoreRegistry

    app = create_app(config)
    if hub_version is not None:
        app.state.hub_version = hub_version
    app.state.plugin_sigstore = sigstore or FakeSigstore()
    # The store as published bundles fill it: the core plugins of the image (Telegram) stay out of it here, and
    # test_plugin_points and the command line's test below show them beside published ones.
    app.state.core_plugins = CoreRegistry(())
    return app


@contextmanager
def started(hub_db, tmp_path, s3, **changes):
    """A hub with the plugin publishers above (``make_app`` takes ``changes``), its admin and alice, a member who is
    not one."""
    from fastapi.testclient import TestClient

    app = make_app(hub_db, tmp_path, s3, **changes)
    with TestClient(app) as client:
        who = {login: live.bearer(live.insert_token(hub_db, login)) for login in (live.ADMIN, "alice")}
        yield SimpleNamespace(client=client, app=app, db=hub_db, s3=s3, who=who, tmp=tmp_path)


@pytest.fixture
def hub(hub_db, tmp_path, s3):
    with started(hub_db, tmp_path, s3) as running:
        yield running


def committed(hub, data: bytes, login: str = live.ADMIN) -> None:
    """Upload and commit ``data`` as a bundle the hub holds itself, as `hub plugin publish` does."""
    from tests.hub.s3 import put_presigned

    item = {"sha256": _sha(data), "size": len(data), "kind": "plugin-bundle"}
    asked = hub.client.post("/v1/blobs/uploads", json={"items": [item]}, headers=hub.who[login])
    assert asked.status_code == 200, asked.text
    for ticket in asked.json()["uploads"]:
        assert put_presigned(ticket["url"], data) == 200
        done = hub.client.post("/v1/blobs/commit", json={"upload_ids": [ticket["upload_id"]]}, headers=hub.who[login])
        assert done.status_code == 200, done.text


def _sha(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


def publish(hub, packed, signature, name: str | None = None, login: str = live.ADMIN, commit: bool = True):
    if commit:
        committed(hub, packed.data)
    body = {"sha256": packed.sha256, "size": len(packed.data), "signature": signature}
    return hub.client.post(f"/v1/plugins/{name or packed.manifest.id}/versions", json=body, headers=hub.who[login])


def act(hub, name: str, verb: str, version: str | None = None, login: str = live.ADMIN):
    body = None if verb == "remove" else {"version": version}
    return hub.client.post(f"/v1/plugins/{name}/{verb}", json=body, headers=hub.who[login])


def refusal(response, status: int = 422) -> str:
    assert response.status_code == status, response.text
    return response.json()["message"]


def rows(db, table: str) -> int:
    from sqlalchemy import func, select

    from evo_agents.hub import tables

    return live.sql(db, select(func.count()).select_from(tables.metadata.tables[table]))[0][0]


def audit_rows(db) -> list[tuple]:
    from sqlalchemy import select

    from evo_agents.hub import tables

    audit = tables.audit
    query = select(audit.c.action, audit.c.target).where(audit.c.action.like("plugin.%")).order_by(audit.c.id)
    return [tuple(row) for row in live.sql(db, query)]


@needs_pg
def test_plugin_install_publish_list_show_upgrade_and_remove(hub):
    first, second = bundle_of(hub.tmp / "v1"), bundle_of(hub.tmp / "v2", version="1.1.0")
    published = publish(hub, first, ed25519_signature(first.data))
    assert published.status_code == 200, published.text
    version = published.json()["version"]
    assert published.json()["created"] is True and version["version"] == "1.0.0" and version["compatible"] is True
    assert (version["signature_kind"], version["publisher"], version["tier"]) == ("ed25519", "acme", "isolated")
    assert version["signed_by"].startswith("ed25519:") and version["permissions"] == ["runs:read", "chats:write"]
    again = publish(hub, first, ed25519_signature(first.data), commit=False)
    assert again.status_code == 200 and again.json()["created"] is False  # the same bytes change nothing

    listed = hub.client.get("/v1/plugins", headers=hub.who[live.ADMIN]).json()
    assert [(p["name"], p["state"], p["installed_version"], p["latest_version"]) for p in listed] == [
        ("sandbox", "available", None, "1.0.0")
    ]
    installed = act(hub, "sandbox", "install")
    assert installed.status_code == 200, installed.text
    assert (installed.json()["state"], installed.json()["installed_version"]) == ("installed", "1.0.0")
    assert installed.json()["installed_by"] == live.ADMIN
    assert "upgrade" in refusal(act(hub, "sandbox", "install"), 409)

    assert publish(hub, second, ed25519_signature(second.data)).status_code == 200
    shown = hub.client.get("/v1/plugins/sandbox", headers=hub.who[live.ADMIN]).json()
    assert [v["version"] for v in shown["versions"]] == ["1.1.0", "1.0.0"]
    assert (shown["upgrade_to"], shown["latest_version"], shown["manifest"]["version"]) == ("1.1.0", "1.1.0", "1.0.0")
    assert "is not above 1.0.0" in refusal(act(hub, "sandbox", "upgrade", "1.0.0"), 409)
    upgraded = act(hub, "sandbox", "upgrade")
    assert upgraded.status_code == 200 and upgraded.json()["installed_version"] == "1.1.0"
    assert upgraded.json()["upgrade_to"] is None
    assert "no version above the installed one" in refusal(act(hub, "sandbox", "upgrade"), 409)

    removed = act(hub, "sandbox", "remove")
    assert removed.status_code == 200 and removed.json()["state"] == "available"
    assert removed.json()["installed_version"] is None and removed.json()["latest_version"] == "1.1.0"
    assert "is not installed" in refusal(act(hub, "sandbox", "remove"), 409)
    assert "is not installed" in refusal(act(hub, "sandbox", "upgrade"), 409)
    assert act(hub, "sandbox", "install", "1.0.0").json()["installed_version"] == "1.0.0"  # any version by number
    assert "has no version 9.9.9" in refusal(act(hub, "sandbox", "upgrade", "9.9.9"), 404)
    assert "no plugin ghost on this hub" in refusal(
        hub.client.get("/v1/plugins/ghost", headers=hub.who[live.ADMIN]), 404
    )
    assert "no plugin ghost" in refusal(act(hub, "ghost", "install"), 404)
    assert rows(hub.db, "plugin_versions") == 2

    assert audit_rows(hub.db) == [
        ("plugin.publish", "plugin:sandbox@1.0.0"),
        ("plugin.install", "plugin:sandbox@1.0.0"),
        ("plugin.publish", "plugin:sandbox@1.1.0"),
        ("plugin.upgrade", "plugin:sandbox@1.1.0"),
        ("plugin.remove", "plugin:sandbox@1.1.0"),
        ("plugin.install", "plugin:sandbox@1.0.0"),
    ]


@needs_pg
def test_plugin_install_refuses_a_backend_the_plugin_host_could_not_run(hub):
    """The second review of PR #72 (L4): the plugin host has no killer user for a plugin whose id on the hub is
    MAX_PLUGIN_ID or more, as that user would meet the backends'. The hub refuses to install such a plugin's backend,
    and says why, rather than let the host refuse to start it."""
    from sqlalchemy import func, select

    from evo_agents.plugins.bundle import pack
    from evo_agents.plugins.environment import MAX_PLUGIN_ID
    from tests.plugins.samples import manifest, write_plugin

    live.sql(hub.db, select(func.setval(func.pg_get_serial_sequence("plugins", "id"), MAX_PLUGIN_ID, False)))
    data = manifest(backend={"runtime": "python", "entry": "backend/main.py"})
    packed = pack(write_plugin(hub.tmp / "far", data, {"backend/main.py": "print('far')\n"}))
    published = publish(hub, packed, ed25519_signature(packed.data))
    assert published.status_code == 200, published.text
    message = refusal(act(hub, "sandbox", "install"), 409)
    assert f"has id {MAX_PLUGIN_ID} on this hub" in message and f"below {MAX_PLUGIN_ID}" in message
    shown = hub.client.get("/v1/plugins/sandbox", headers=hub.who[live.ADMIN]).json()
    assert shown["installed_version"] is None


@needs_pg
def test_plugin_install_is_for_hub_admins_and_a_version_never_changes(hub):
    packed = bundle_of(hub.tmp / "v1")
    for method, path in (
        ("GET", "/v1/plugins"),
        ("GET", "/v1/plugins/sandbox"),
        ("POST", "/v1/plugins/sandbox/install"),
    ):
        response = hub.client.request(method, path, json={}, headers=hub.who["alice"])
        assert response.status_code == 403, response.text
    asked = hub.client.post(
        "/v1/blobs/uploads",
        json={"items": [{"sha256": packed.sha256, "size": len(packed.data), "kind": "plugin-bundle"}]},
        headers=hub.who["alice"],
    )
    assert asked.status_code == 403  # the hub's own blobs are for hub admins only

    assert "is not committed for the hub" in refusal(publish(hub, packed, ed25519_signature(packed.data), commit=False))
    assert publish(hub, packed, ed25519_signature(packed.data)).status_code == 200
    other_bytes = bundle_of(hub.tmp / "other", description="the same version, other bytes")
    assert "a version never changes" in refusal(publish(hub, other_bytes, ed25519_signature(other_bytes.data)), 409)
    beta = bundle_of(hub.tmp / "beta", publisher="beta", version="2.0.0")
    message = refusal(publish(hub, beta, ed25519_signature(beta.data, OTHER_SEED)), 409)
    assert "plugin sandbox is published by acme; publisher beta cannot publish a version of it" in message
    renamed = bundle_of(hub.tmp / "renamed", id="elsewhere")
    message = refusal(publish(hub, renamed, ed25519_signature(renamed.data), name="sandbox"))
    assert "the bundle is of plugin elsewhere, not sandbox" in message
    assert rows(hub.db, "plugin_versions") == 1 and rows(hub.db, "plugins") == 1


@needs_pg
def test_plugin_sign_refusals_name_the_reason_and_record_nothing(hub):
    packed = bundle_of(hub.tmp / "v1")
    good = ed25519_signature(packed.data)
    cases = [
        (None, "refused (unsigned): the bundle is not signed"),
        ({**good, "kind": "gpg"}, "refused (malformed): a signature's kind is sigstore or ed25519"),
        (ed25519_signature(packed.data + b"!"), "refused (altered): the bundle is not the one signed"),
        (ed25519_signature(packed.data, OTHER_SEED), "refused (invalid): the Ed25519 signature is wrong"),
        (
            sigstore_signature(packed.data, identity=SIGNER.replace("release", "nightly")),
            "refused (untrusted): signed by",
        ),
        (sigstore_signature(packed.data, valid=False), "refused (invalid): the Sigstore signature does not verify"),
    ]
    for signature, expected in cases:
        message = refusal(publish(hub, packed, signature))
        assert message.startswith(expected) and message.endswith("; nothing was published"), message
    stranger = bundle_of(hub.tmp / "stranger", id="stranger", publisher="nobody")
    message = refusal(publish(hub, stranger, ed25519_signature(stranger.data)))
    assert message.startswith("refused (untrusted): publisher nobody is not trusted on this hub")
    core = bundle_of(hub.tmp / "core", id="acme-core", tier="core")
    message = refusal(publish(hub, core, ed25519_signature(core.data)))
    assert (
        message.startswith("refused (untrusted): acme-core is a core plugin")
        and "EVO_HUB_PLUGIN_CORE_PUBLISHERS" in message
    )
    garbage = SimpleNamespace(
        data=b"not a bundle", sha256=_sha(b"not a bundle"), manifest=SimpleNamespace(id="sandbox")
    )
    assert refusal(publish(hub, garbage, ed25519_signature(garbage.data))).startswith("refused (malformed): not a gzip")
    assert rows(hub.db, "plugins") == rows(hub.db, "plugin_versions") == 0 and audit_rows(hub.db) == []

    # A Sigstore signature of the publisher's workflow, and a core plugin of a core publisher, are taken.
    signed = publish(hub, packed, sigstore_signature(packed.data))
    assert signed.status_code == 200, signed.text
    assert (signed.json()["version"]["signature_kind"], signed.json()["version"]["signed_by"]) == ("sigstore", SIGNER)
    evo_core = bundle_of(hub.tmp / "evo-core", id="evo-core", publisher="evo", tier="core")
    assert publish(hub, evo_core, ed25519_signature(evo_core.data)).status_code == 200

    # A signature that cannot be checked here records nothing and says so.
    def unavailable(data, bundle):
        raise SignatureUnavailable("cannot load Sigstore's trust root (OSError: offline); try again shortly")

    hub.app.state.plugin_sigstore = unavailable
    later = bundle_of(hub.tmp / "v2", version="1.1.0")
    message = refusal(publish(hub, later, sigstore_signature(later.data)), 503)
    assert message.startswith("the signature cannot be checked on this hub now: cannot load Sigstore's trust root")
    assert rows(hub.db, "plugin_versions") == 2


@needs_pg
def test_plugin_compat_refuses_a_bundle_outside_its_engines_or_plugin_api(hub):
    for changes, expected in (
        ({"engines": {"evo-agents": ">=99"}}, "it runs on evo-agents >=99, not on"),
        ({"engines": {"evo-agents": "<0.16"}}, "it runs on evo-agents <0.16, not on"),
        ({"plugin_api": 2}, "it is written for plugin API 2;"),
    ):
        packed = bundle_of(hub.tmp / json.dumps(changes)[:40].replace("/", "_"), **changes)
        message = refusal(publish(hub, packed, ed25519_signature(packed.data)))
        assert (
            message.startswith("refused (incompatible): sandbox 1.0.0 does not run on this hub: ")
            and expected in message
        )
    assert rows(hub.db, "plugin_versions") == 0


@needs_pg
def test_plugin_install_checks_the_stored_bytes_and_the_publisher_again(hub, hub_db, tmp_path, s3):
    from fastapi.testclient import TestClient

    from evo_agents.hub.blobs import blob_key

    packed = bundle_of(hub.tmp / "v1")
    assert publish(hub, packed, ed25519_signature(packed.data)).status_code == 200
    changed = bytearray(packed.data)
    changed[len(changed) // 2] ^= 0xFF  # the same size, other bytes
    hub.s3.put(blob_key(packed.sha256), bytes(changed))
    message = refusal(act(hub, "sandbox", "install"))
    assert message.startswith("refused (altered): the blob store holds other bytes than bundle")
    hub.s3.put(blob_key(packed.sha256), packed.data)

    # The operator stopped trusting acme's key: what was published before is not installed any longer.
    with TestClient(make_app(hub_db, tmp_path, s3, publishers=f"acme=sigstore:{WORKFLOW}")) as client:
        refused = client.post("/v1/plugins/sandbox/install", json={}, headers=hub.who[live.ADMIN])
        assert refusal(refused).startswith("refused (untrusted): publisher acme signs with Sigstore on this hub")
    assert act(hub, "sandbox", "install").status_code == 200


def notices(db) -> list[tuple]:
    from sqlalchemy import select

    from evo_agents.hub import tables

    n, u = tables.notifications, tables.users
    query = (
        select(u.c.login, n.c.notice_kind, n.c.title, n.c.link, n.c.details)
        .join_from(n, u, u.c.id == n.c.user_id)
        .where(n.c.notice_kind == "plugin_disabled")
        .order_by(n.c.id)
    )
    return [tuple(row) for row in live.sql(db, query)]


@needs_pg
def test_plugin_compat_at_start_turns_off_what_no_longer_runs_and_tells_the_hub_admins(hub_db, tmp_path, s3):
    with started(hub_db, tmp_path, s3, hub_version="0.16.1") as hub:  # a hub the plugin runs on
        old = bundle_of(tmp_path / "old", engines={"evo-agents": ">=0.16, <0.17"})
        assert publish(hub, old, ed25519_signature(old.data)).status_code == 200
        assert act(hub, "sandbox", "install").status_code == 200
    assert notices(hub_db) == []

    with started(hub_db, tmp_path, s3, hub_version="0.17.0") as hub:  # the hub was upgraded past the plugin
        shown = hub.client.get("/v1/plugins/sandbox", headers=hub.who[live.ADMIN]).json()
        assert (shown["state"], shown["installed_version"]) == ("disabled", "1.0.0")
        reason = (
            "plugin sandbox 1.0.0 does not run on evo-agents 0.17.0: it runs on evo-agents >=0.16, <0.17, not on 0.17.0"
        )
        assert shown["disabled_reason"] == reason and shown["disabled_at"] is not None
        assert hub.client.get("/v1/health/live").status_code == 200  # the hub serves on
        [(login, kind, title, link, details)] = notices(hub_db)
        assert (login, kind, link) == (live.ADMIN, "plugin_disabled", "/admin/plugins")
        assert title == "Plugin sandbox turned off: it does not run on evo-agents 0.17.0"
        assert details == {"plugin": "sandbox", "version": "1.0.0", "hub_version": "0.17.0"}
        assert audit_rows(hub_db)[-1] == ("plugin.disable", "plugin:sandbox@1.0.0")
        assert "refused (incompatible)" in refusal(act(hub, "sandbox", "install", "1.0.0"))
        assert "has no version that runs on this hub" in refusal(act(hub, "sandbox", "install"), 409)

    with started(hub_db, tmp_path, s3, hub_version="0.17.0") as hub:  # a second start says nothing new
        assert len(notices(hub_db)) == 1
        fixed = bundle_of(tmp_path / "fixed", version="1.1.0", engines={"evo-agents": ">=0.16"})
        assert publish(hub, fixed, ed25519_signature(fixed.data)).status_code == 200
        upgraded = act(hub, "sandbox", "upgrade").json()
        assert (upgraded["state"], upgraded["installed_version"], upgraded["disabled_reason"]) == (
            "installed",
            "1.1.0",
            None,
        )


@needs_pg
def test_plugin_compat_check_that_fails_at_start_leaves_the_hub_serving(hub_db, tmp_path, s3, monkeypatch, caplog):
    from fastapi.testclient import TestClient

    from evo_agents.hub.server import plugin_store

    async def broken(*args):
        raise RuntimeError("the database went away")

    monkeypatch.setattr(plugin_store, "disable_incompatible", broken)
    with TestClient(make_app(hub_db, tmp_path, s3)) as client:
        assert client.get("/v1/health/live").status_code == 200
    assert any("cannot check the installed plugins" in record.getMessage() for record in caplog.records)


# The CLI against `hub serve`


def sign_in(home: Path, url: str, login: str, token: str) -> None:
    directory = home / ".evo" / "hub"
    directory.mkdir(parents=True)
    (directory / "token").write_text(token + "\n", encoding="utf-8")
    (directory / "config.json").write_text(json.dumps({"url": url, "login": login}), encoding="utf-8")


def plugin_cli(args: list[str], home: Path) -> subprocess.CompletedProcess:
    command = [sys.executable, "-m", "evo_agents", "hub", "plugin", *args]
    return subprocess.run(
        command, env=pg.clean_env(HOME=str(home)), capture_output=True, text=True, timeout=120, cwd=home
    )


def ok(result: subprocess.CompletedProcess) -> subprocess.CompletedProcess:
    assert result.returncode == 0, result.stdout + result.stderr
    return result


@needs_pg
def test_plugin_install_cli_publishes_lists_shows_installs_upgrades_and_removes_against_hub_serve(hub_db, tmp_path, s3):
    publishers = ed25519_publishers()
    with live.running_hub(
        hub_db, tmp_path, EVO_HUB_ADMINS=live.ADMIN, EVO_HUB_PLUGIN_PUBLISHERS=publishers, **s3.env()
    ) as served:
        home = tmp_path / "admin"
        token = live.insert_token(hub_db, live.ADMIN)
        sign_in(home, served.url, live.ADMIN, token)
        files = tmp_path / "bundles"
        files.mkdir()
        first, second = bundle_of(tmp_path / "v1"), bundle_of(tmp_path / "v2", version="1.1.0")
        for packed in (first, second):
            path = files / f"sandbox-{packed.manifest.version}.tar.gz"
            path.write_bytes(packed.data)
        (files / "sandbox-1.0.0.tar.gz.sig.json").write_text(json.dumps(ed25519_signature(first.data)))
        (files / "elsewhere.json").write_text(json.dumps(ed25519_signature(second.data)))

        unsigned = plugin_cli(["publish", str(files / "sandbox-1.1.0.tar.gz")], home)
        assert unsigned.returncode == 1 and "refused (unsigned): the bundle is not signed" in unsigned.stderr
        published = ok(plugin_cli(["publish", str(files / "sandbox-1.0.0.tar.gz")], home)).stdout
        assert published.startswith("Published plugin sandbox 1.0.0, signed by ed25519:")
        assert "holds already" in ok(plugin_cli(["publish", str(files / "sandbox-1.0.0.tar.gz")], home)).stdout
        args = ["publish", str(files / "sandbox-1.1.0.tar.gz"), "--signature", str(files / "elsewhere.json"), "--json"]
        assert_json_keys("hub plugin publish", json.loads(ok(plugin_cli(args, home)).stdout))
        missing = plugin_cli(["publish", str(tmp_path / "nowhere.tar.gz")], home)
        assert missing.returncode == 1 and "cannot read" in missing.stderr
        not_a_bundle = files / "notes.tar.gz"
        not_a_bundle.write_bytes(b"plain text")
        refused = plugin_cli(["publish", str(not_a_bundle)], home)
        assert refused.returncode == 1 and "is not a plugin bundle: not a gzip-compressed tar" in refused.stderr

        listed = json.loads(ok(plugin_cli(["list", "--json"], home)).stdout)
        assert_json_keys("hub plugin list", listed)
        # beside sandbox, Builder, SRS, Tasks and Telegram: the core plugins evo-ak ships, installed with it, which the
        # hub records as it starts
        found = [(p["name"], p["state"], p["latest_version"]) for p in listed]
        assert found == [
            ("builder", "installed", "1.0.0"),
            ("sandbox", "available", "1.1.0"),
            ("srs", "installed", "1.0.0"),
            ("tasks", "installed", "1.0.0"),
            ("telegram", "installed", "1.0.0"),
        ]
        assert "installed 1.0.0 by the hub's image at" in ok(plugin_cli(["show", "telegram"], home)).stdout
        installed = json.loads(ok(plugin_cli(["install", "sandbox", "--version", "1.0.0", "--json"], home)).stdout)
        assert_json_keys("hub plugin install", installed)
        assert installed["installed_version"] == "1.0.0"
        table = ok(plugin_cli(["list"], home)).stdout
        assert "sandbox" in table and "installed" in table and "1.1.0" in table and "telegram" in table
        shown = ok(plugin_cli(["show", "sandbox"], home)).stdout
        assert "Plugin sandbox of publisher acme: installed" in shown and "upgrade waiting: 1.1.0" in shown
        assert "asks for runs:read, chats:write" in shown
        assert_json_keys("hub plugin show", json.loads(ok(plugin_cli(["show", "sandbox", "--json"], home)).stdout))
        assert ok(plugin_cli(["upgrade", "sandbox"], home)).stdout == "Upgraded plugin sandbox 1.1.0.\n"
        upgrade_again = plugin_cli(["upgrade", "sandbox"], home)
        assert upgrade_again.returncode == 1 and "no version above the installed one" in upgrade_again.stderr
        removed = json.loads(ok(plugin_cli(["remove", "sandbox", "--json"], home)).stdout)
        assert_json_keys("hub plugin remove", removed)
        assert removed["state"] == "available"
        assert ok(plugin_cli(["upgrade", "--help"], home)).stdout.startswith("usage:")
    log = served.log()
    for secret in (token, s3.secret_access_key, "X-Amz-Signature"):
        assert secret not in log
    messages = [line["msg"] for line in pg.log_lines(log)]
    assert {"plugin published", "plugin installed", "plugin upgraded", "plugin removed"} <= set(messages)
