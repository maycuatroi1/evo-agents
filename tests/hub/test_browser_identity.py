"""Browser identities on the hub (``evo_agents.hub.server.browser_identities`` and ``browser_profiles``), criterion a2
of the plan agent-browser. A member creates, lists and deletes their own identity with the writer role, and no other
member sees it; the project's identity is an admin's to create and delete, and every member sees it; its worker is the
caller's own or a shared one that serves the project; names are unique per owner; a delete is refused while a run
holds the identity. Each profile version is encrypted under a fresh data key sealed for its identity and version, so
neither the bucket nor a table nor a log holds a cookie; the hub keeps two versions and records the older for
deletion; a save that loses keeps nothing. Every write adds an audit row, and ``hub browser identity list|create|delete``
print metadata only, with the keys of the contract."""

import logging
import secrets
from functools import partial
from types import SimpleNamespace

import pytest

from tests.hub import live, pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select, update

from evo_agents.hub import blob_gc, tables
from evo_agents.hub.blobs import KIND_LIMITS, blob_key
from evo_agents.hub.server import browser_profiles as profiles
from evo_agents.hub.server.app import create_app
from evo_agents.hub.server.browser_identities import BrowserIdentity
from evo_agents.hub.server.sealing import KEY_BYTES, Sealed, Sealer, Unsealable, browser_identity_aad
from tests.hub import test_secrets_cli as secrets_cli
from tests.hub.contract_keys import assert_json_keys
from tests.hub.live import sql, table_dump
from tests.hub.test_browser_claim import add_browser_worker, browser_beat
from tests.hub.test_run_cli import as_json, ok
from tests.hub.test_run_tables import add_run
from tests.hub.test_runs import OTHER, OWNER, PROJECT, add_worker, members
from tests.hub.test_secrets_cli import run_cli
from tests.hub.test_shared_workers import share

ADMIN_GRANT = {"role": "admin", "max_level": "internal"}
KEY = secrets.token_bytes(KEY_BYTES)
PATH = f"/v1/projects/{PROJECT}/browser-identities"
CHROMIUM = "Chromium 139.0.7258.5"
# What no answer may carry: the blobs' hashes and the sealed data keys.
HIDDEN = {f"{prefix}{name}" for prefix in ("", "previous_") for name in profiles.VERSION_COLUMNS} - {"size"}

if pg.DSN:
    app_key, cli_hub = secrets_cli.app_key, secrets_cli.hub


def marker() -> str:
    """A cookie value drawn for this test, so that finding it anywhere cannot be a coincidence."""
    return "session=" + secrets.token_hex(16)


@pytest.fixture
def client(hub_db, tmp_path, github):
    config = live.hub_config(hub_db, tmp_path, github, secrets_key=KEY)
    with TestClient(create_app(config), base_url="https://hub.test") as client:
        yield client


@pytest.fixture
def hub(client, github) -> dict:
    """The members of ``tests.hub.test_runs``, the owner made an admin of the project, and the owner's worker box."""
    headers = members(client, github)
    granted = client.put(f"/v1/admin/projects/{PROJECT}/grants/{OWNER}", json=ADMIN_GRANT, headers=headers["admin"])
    assert granted.status_code == 200, granted.text
    add_browser_worker(client, headers["owner"], "box")
    return headers


@pytest.fixture
def stored(hub_db, tmp_path, github, s3):
    """The hub with its sealing key and a bucket, the members of ``hub``, and what the profile functions take."""
    config = live.hub_config(hub_db, tmp_path, github, secrets_key=KEY, **s3.config())
    with TestClient(create_app(config), base_url="https://hub.test") as client:
        headers = members(client, github)
        granted = client.put(f"/v1/admin/projects/{PROJECT}/grants/{OWNER}", json=ADMIN_GRANT, headers=headers["admin"])
        assert granted.status_code == 200, granted.text
        box = add_browser_worker(client, headers["owner"], "box")
        state = client.app.state
        yield SimpleNamespace(
            client=client,
            headers=headers,
            db=hub_db,
            s3=s3,
            box=box,
            engine=state.engine,
            store=state.blobs,
            sealer=state.sealer,
            owner_id=user_id(hub_db, OWNER),
        )


def user_id(db, login: str) -> int:
    return sql(db, select(tables.users.c.id).where(tables.users.c.login == login))[0][0]


def project_id(db) -> int:
    return sql(db, select(tables.projects.c.id).where(tables.projects.c.name == PROJECT))[0][0]


def create(client, headers, name: str, *, worker: str = "box", owner: str = "member", domains=("github.com",)):
    body = {"name": name, "domains": list(domains), "worker": worker, "owner": owner}
    return client.post(PATH, json=body, headers=headers)


def created(client, headers, name: str, **changes) -> dict:
    response = create(client, headers, name, **changes)
    assert response.status_code == 201, response.text
    assert response.headers["cache-control"] == "no-store"
    identity = response.json()
    assert set(identity) == set(BrowserIdentity.model_fields) and not HIDDEN & set(identity)
    return identity


def listed(client, headers) -> list[dict]:
    response = client.get(PATH, headers=headers)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    return response.json()


def remove(client, headers, identity_id: int):
    return client.delete(f"{PATH}/{identity_id}", headers=headers)


def audit_rows(db) -> list[tuple]:
    a, u, p = tables.audit, tables.users, tables.projects
    query = (
        select(a.c.action, a.c.target, u.c.login, p.c.name)
        .join_from(a, u, u.c.id == a.c.actor_id)
        .outerjoin(p, p.c.id == a.c.project_id)
        .where(a.c.action.like("browser_identity.%"))
        .order_by(a.c.id)
    )
    return sql(db, query)


def versions(db, identity_id: int) -> tuple:
    """(version, sha256, previous_sha256, deleted_at) of the identity."""
    b = tables.browser_identities
    query = select(b.c.version, b.c.sha256, b.c.previous_sha256, b.c.deleted_at).where(b.c.id == identity_id)
    return sql(db, query)[0]


def pending(db) -> set[str]:
    """The browser profiles recorded for deletion and not deleted yet."""
    d = tables.blob_deletions
    query = select(d.c.sha256).where(d.c.kind == profiles.KIND, d.c.deleted_at.is_(None))
    return {row[0] for row in sql(db, query)}


def save(hub, identity_id: int, base_version: int, archive: bytes, **changes) -> int:
    call = partial(
        profiles.save_profile,
        hub.engine,
        hub.store,
        hub.sealer,
        identity_id=identity_id,
        base_version=base_version,
        archive=archive,
        chromium_version=CHROMIUM,
        actor_id=hub.owner_id,
        token_id=None,
        **changes,
    )
    return hub.client.portal.call(call)


def opened(hub, identity_id: int, *, previous: bool = False):
    return hub.client.portal.call(
        partial(profiles.open_profile, hub.engine, hub.store, hub.sealer, identity_id, previous=previous)
    )


def collect(hub) -> blob_gc.Deleted:
    return hub.client.portal.call(partial(blob_gc.delete_pending, hub.engine, hub.store))


def held(db, identity_id: int, worker_id: int) -> int:
    """A running run that holds the identity; its id."""
    ids = {"project": project_id(db), "user": user_id(db, OWNER), "worker": worker_id}
    with live.connect(db) as conn:
        run_id = add_run(conn, ids, state="running", kind="release")
    b = tables.browser_identities
    sql(db, update(b).values(held_by_run=run_id).where(b.c.id == identity_id))
    return run_id


# The routes


def test_browser_identity_of_a_member_is_created_listed_and_deleted_by_that_member_alone(client, hub, hub_db, caplog):
    caplog.set_level(logging.DEBUG)
    domains = ("github.com", "accounts.lumen.example", "github.com")
    identity = created(client, hub["owner"], "github-work", domains=domains)
    assert (identity["project"], identity["name"], identity["owner"], identity["owner_login"]) == (
        PROJECT,
        "github-work",
        "member",
        OWNER,
    )
    assert identity["domains"] == ["github.com", "accounts.lumen.example"]  # once each, in the order given
    assert (identity["worker"], identity["worker_owner"], identity["status"], identity["version"]) == (
        "box",
        OWNER,
        "active",
        0,
    )
    assert (identity["size"], identity["held_by_run"], identity["last_saved_at"], identity["can_manage"]) == (
        None,
        None,
        None,
        True,
    )
    assert listed(client, hub["owner"]) == [identity]
    assert listed(client, hub["other"]) == []  # another member never sees it, a writer or not
    assert remove(client, hub["other"], identity["id"]).status_code == 404
    assert remove(client, hub["admin"], identity["id"]).status_code == 404  # nor does an admin of the hub

    refused = create(client, hub["reader"], "github-reader")
    assert refused.status_code == 403 and "needs the writer role" in refused.json()["message"]
    assert create(client, hub["stranger"], "github-stranger").status_code in (403, 404)

    assert remove(client, hub["owner"], identity["id"]).status_code == 204
    assert listed(client, hub["owner"]) == []
    assert versions(hub_db, identity["id"])[3] is not None  # kept, deleted
    assert remove(client, hub["owner"], identity["id"]).status_code == 404
    again = created(client, hub["owner"], "github-work")  # the name is free again
    assert again["id"] != identity["id"]

    target = f"{PROJECT} browser-identity:"
    assert audit_rows(hub_db) == [
        ("browser_identity.create", f"{target}{identity['id']}", OWNER, PROJECT),
        ("browser_identity.delete", f"{target}{identity['id']}", OWNER, PROJECT),
        ("browser_identity.create", f"{target}{again['id']}", OWNER, PROJECT),
    ]
    for record in caplog.records:  # the sites an identity logs in to stay out of the logs
        assert "accounts.lumen.example" not in str(vars(record)), record.getMessage()


def test_browser_identity_of_the_project_is_an_admins_to_create_and_delete_and_every_member_sees_it(
    client, hub, hub_db
):
    refused = create(client, hub["other"], "northwind-crm", owner="project", domains=("crm.northwind.example",))
    assert refused.status_code == 403 and "needs the admin role" in refused.json()["message"]
    project = created(client, hub["owner"], "northwind-crm", owner="project", domains=("crm.northwind.example",))
    assert (project["owner"], project["owner_login"], project["can_manage"]) == ("project", None, True)

    for who in ("other", "reader"):
        (seen,) = listed(client, hub[who])
        assert (seen["id"], seen["can_manage"]) == (project["id"], False)
    gone = remove(client, hub["other"], project["id"])
    assert gone.status_code == 403 and "needs the admin role" in gone.json()["message"]

    # names are unique per owner: the project's and a member's may share one, two of either may not
    mine = created(client, hub["owner"], "northwind-crm")
    add_browser_worker(client, hub["other"], "lab")
    theirs = created(client, hub["other"], "northwind-crm", worker="lab")
    assert {i["id"] for i in listed(client, hub["owner"])} == {project["id"], mine["id"]}
    assert {i["id"] for i in listed(client, hub["other"])} == {project["id"], theirs["id"]}
    for who, owner in (("owner", "project"), ("owner", "member")):
        twice = create(client, hub[who], "northwind-crm", owner=owner)
        assert twice.status_code == 409 and "already" in twice.json()["message"]

    run_id = held(hub_db, project["id"], sql(hub_db, select(tables.workers.c.id))[0][0])
    busy = remove(client, hub["owner"], project["id"])
    assert busy.status_code == 409 and f"run {run_id} holds" in busy.json()["message"]
    shown = {i["id"]: i["held_by_run"] for i in listed(client, hub["other"])}
    assert shown == {project["id"]: run_id, theirs["id"]: None}
    b = tables.browser_identities
    sql(hub_db, update(b).values(held_by_run=None).where(b.c.id == project["id"]))
    assert remove(client, hub["owner"], project["id"]).status_code == 204
    actions = [(action, login) for action, _, login, _ in audit_rows(hub_db)]
    assert actions == [("browser_identity.create", login) for login in (OWNER, OWNER, OTHER)] + [
        ("browser_identity.delete", OWNER)
    ]


def test_browser_identity_needs_a_worker_of_the_member_or_a_shared_one_that_serves_the_project(client, hub, hub_db):
    box = {"id": sql(hub_db, select(tables.workers.c.id).where(tables.workers.c.name == "box"))[0][0]}
    add_browser_worker(client, hub["other"], "lab")
    for name in ("box", "nowhere"):  # the owner's worker, not shared, and none at all
        refused = create(client, hub["other"], "github-other", worker=name)
        assert refused.status_code == 422 and f"worker {name} is neither" in refused.json()["message"]
    assert created(client, hub["other"], "github-own", worker="lab")["worker_owner"] == OTHER

    assert share(client, hub["owner"], box).status_code == 200
    on_shared = created(client, hub["other"], "github-other", worker="box")
    assert (on_shared["worker"], on_shared["worker_owner"], on_shared["owner_login"]) == ("box", OWNER, OTHER)

    w, wp = tables.workers, tables.worker_projects
    sql(hub_db, delete(wp).where(wp.c.worker_id == box["id"]))  # box no longer serves the project
    assert create(client, hub["other"], "github-elsewhere", worker="box").status_code == 422
    sql(hub_db, update(w).values(revoked_at=func.now()).where(w.c.name == "lab"))
    assert create(client, hub["other"], "github-revoked", worker="lab").status_code == 422


def test_browser_identity_needs_a_worker_whose_daemon_reports_the_feature_browser(client, hub, hub_db):
    plain = add_worker(client, hub["other"], "plain")  # its daemon never ran `evo-agents worker browser install`
    refused = create(client, hub["other"], "github-plain", worker="plain")
    assert refused.status_code == 422
    assert "does not report the feature browser" in refused.json()["message"]
    assert "evo-agents worker browser install" in refused.json()["message"]
    assert browser_beat(client, plain).status_code == 200  # installed, its daemon restarted
    assert created(client, hub["other"], "github-plain", worker="plain")["worker"] == "plain"
    assert browser_beat(client, plain, None, features=()).status_code == 200  # a daemon that lost it
    assert create(client, hub["other"], "github-again", worker="plain").status_code == 422


@pytest.mark.parametrize(
    "change",
    [
        {"name": "GitHub Work"},
        {"name": ""},
        {"domains": []},
        {"domains": ["GitHub.com"]},
        {"domains": ["https://github.com"]},
        {"domains": ["github.com/login"]},
        {"domains": ["-github.com"]},
        {"domains": [f"site{n}.lumen.example" for n in range(51)]},
        {"owner": "everyone"},
        {"worker": "box; rm"},
    ],
)
def test_browser_identity_refuses_a_bad_name_domain_owner_or_worker(client, hub, hub_db, change):
    body = {"name": "github-work", "domains": ["github.com"], "worker": "box", "owner": "member", **change}
    assert client.post(PATH, json=body, headers=hub["owner"]).status_code == 422
    assert sql(hub_db, select(func.count()).select_from(tables.browser_identities))[0][0] == 0


# The profiles


def test_browser_profile_is_sealed_per_version_and_two_are_kept_so_no_cookie_reaches_the_bucket_or_a_table(
    stored, caplog
):
    caplog.set_level(logging.DEBUG)
    identity = created(stored.client, stored.headers["owner"], "github-work")
    cookie = marker()
    archives = [f"{cookie} version {n} ".encode() * 200 for n in (1, 2, 3)]
    hashes = []
    for base, archive in enumerate(archives):
        assert save(stored, identity["id"], base, archive) == base + 1
        version, sha256, previous, _ = versions(stored.db, identity["id"])
        assert (version, previous) == (base + 1, hashes[-1] if hashes else None)
        hashes.append(sha256)
    assert len(set(hashes)) == 3

    assert opened(stored, identity["id"]) == (3, archives[2])
    assert opened(stored, identity["id"], previous=True) == (2, archives[1])
    assert pending(stored.db) == {hashes[0]}  # the third save let the first version go
    for key in stored.s3.keys():
        assert cookie.encode() not in stored.s3.get(key), key
    assert cookie not in table_dump(stored.db)
    assert cookie not in caplog.text and all(cookie not in str(vars(record)) for record in caplog.records)

    deleted = collect(stored)
    assert deleted.objects == 1
    assert stored.s3.get(blob_key(hashes[0])) is None
    assert all(stored.s3.get(blob_key(sha256)) is not None for sha256 in hashes[1:])

    (shown,) = listed(stored.client, stored.headers["owner"])
    assert (shown["version"], shown["chromium_version"], shown["last_saved_by"]) == (3, CHROMIUM, OWNER)
    assert shown["size"] == len(stored.s3.get(blob_key(hashes[2]))) and shown["last_saved_at"] is not None
    target = f"{PROJECT} browser-identity:{identity['id']}"
    assert [row[:2] for row in audit_rows(stored.db)] == [("browser_identity.create", target)] + [
        ("browser_identity.save", f"{target} version={n}") for n in (1, 2, 3)
    ]


def test_browser_profile_opens_only_as_the_identity_and_version_it_was_sealed_for(stored):
    first = created(stored.client, stored.headers["owner"], "github-work")
    second = created(stored.client, stored.headers["owner"], "github-play")
    archive = marker().encode()
    save(stored, first["id"], 0, archive)
    b = tables.browser_identities
    version_columns = [b.c[name] for name in profiles.VERSION_COLUMNS]
    sha256, _, sealed_key, key_nonce, key_id = sql(stored.db, select(*version_columns).where(b.c.id == first["id"]))[0]
    key = Sealed(bytes(sealed_key), bytes(key_nonce), key_id)
    blob = stored.s3.get(blob_key(sha256))
    sealer = Sealer(KEY)

    assert len(sealer.open_bytes(key, browser_identity_aad(first["id"], 1))) == KEY_BYTES
    assert profiles.open_profile_blob(sealer, first["id"], 1, blob, key) == archive
    for identity_id, version in ((second["id"], 1), (first["id"], 2)):
        with pytest.raises(Unsealable):
            sealer.open_bytes(key, browser_identity_aad(identity_id, version))
        with pytest.raises(Unsealable):
            profiles.open_profile_blob(sealer, identity_id, version, blob, key)
    altered = blob[:-1] + bytes([blob[-1] ^ 1])
    with pytest.raises(Unsealable):
        profiles.open_profile_blob(sealer, first["id"], 1, altered, key)
    with pytest.raises(Unsealable):
        profiles.open_profile_blob(Sealer(secrets.token_bytes(KEY_BYTES)), first["id"], 1, blob, key)

    # the version of one identity copied onto another does not open there
    copied = dict(zip(profiles.VERSION_COLUMNS, (sha256, len(blob), sealed_key, key_nonce, key_id), strict=True))
    sql(stored.db, update(b).values(version=1, **copied).where(b.c.id == second["id"]))
    with pytest.raises(Unsealable):
        opened(stored, second["id"])
    assert opened(stored, first["id"]) == (1, archive)


def test_browser_profile_save_that_loses_keeps_the_version_there_and_records_its_own_blob_for_deletion(stored):
    identity = created(stored.client, stored.headers["owner"], "github-work")
    assert save(stored, identity["id"], 0, b"first") == 1
    current = versions(stored.db, identity["id"])

    with pytest.raises(profiles.ProfileConflict, match="at version 1 now, not 0"):
        save(stored, identity["id"], 0, b"late")
    assert versions(stored.db, identity["id"]) == current
    (lost,) = pending(stored.db)
    assert lost != current[1] and stored.s3.get(blob_key(lost)) is not None

    run_id = held(stored.db, identity["id"], stored.box["id"])
    with pytest.raises(profiles.ProfileConflict, match=f"run {run_id + 1} does not hold"):
        save(stored, identity["id"], 1, b"elsewhere", run_id=run_id + 1)
    assert save(stored, identity["id"], 1, b"second", run_id=run_id) == 2
    assert opened(stored, identity["id"]) == (2, b"second")
    assert len(pending(stored.db)) == 2  # the two that lost, and no version kept
    assert collect(stored).objects == 2
    assert opened(stored, identity["id"], previous=True) == (1, b"first")


def test_browser_profile_has_a_limit_so_its_sealed_blob_stays_within_its_kind():
    sealer = Sealer(KEY)
    assert profiles.MAX_ARCHIVE_BYTES == KIND_LIMITS["browser-profile"] - profiles.OVERHEAD
    largest = profiles.seal_profile(sealer, 7, 1, bytes(profiles.MAX_ARCHIVE_BYTES))
    assert largest.size == len(largest.blob) == KIND_LIMITS["browser-profile"]
    with pytest.raises(profiles.ProfileTooLarge):
        profiles.seal_profile(sealer, 7, 1, bytes(profiles.MAX_ARCHIVE_BYTES + 1))
    one, other = (profiles.seal_profile(sealer, 7, 1, b"same archive") for _ in range(2))
    assert one.sha256 != other.sha256 and one.key.ciphertext != other.key.ciphertext  # a fresh data key each time


def test_browser_identity_delete_drops_its_keys_and_records_both_profiles_for_deletion(stored):
    identity = created(stored.client, stored.headers["owner"], "github-work")
    save(stored, identity["id"], 0, b"first")
    save(stored, identity["id"], 1, b"second")
    _, sha256, previous, _ = versions(stored.db, identity["id"])
    assert remove(stored.client, stored.headers["owner"], identity["id"]).status_code == 204

    b = tables.browser_identities
    columns = [b.c[f"{prefix}{name}"] for prefix in ("", "previous_") for name in profiles.VERSION_COLUMNS]
    (row,) = sql(stored.db, select(*columns, b.c.deleted_at).where(b.c.id == identity["id"]))
    assert set(row[:-1]) == {None} and row[-1] is not None
    assert pending(stored.db) == {sha256, previous}
    assert collect(stored).objects == 2
    assert stored.s3.get(blob_key(sha256)) is None and stored.s3.get(blob_key(previous)) is None
    assert opened(stored, identity["id"]) is None
    with pytest.raises(profiles.ProfileConflict, match="is deleted"):
        save(stored, identity["id"], 2, b"third")


# The commands


def test_browser_identity_commands_create_list_and_delete_printing_metadata_only(cli_hub, monkeypatch, capsys):
    grant = cli_hub.client.put(
        f"/v1/admin/projects/{PROJECT}/grants/{OWNER}", json=ADMIN_GRANT, headers=cli_hub.headers["admin"]
    )
    assert grant.status_code == 200, grant.text
    add_browser_worker(cli_hub.client, cli_hub.headers["owner"], "mac-mini")
    owner, other = cli_hub.homes["owner"], cli_hub.homes["other"]

    def identity(who, *args):
        return run_cli(monkeypatch, capsys, who, "hub", "browser", "identity", *args, "--project", PROJECT)

    made = as_json(
        identity(
            owner,
            "create",
            "github-work",
            "--domain",
            "GitHub.com ",
            "--domain",
            "gitlab.com",
            "--worker",
            "mac-mini",
            "--json",
        )
    )
    assert_json_keys("hub browser identity create", made)
    assert (made["domains"], made["owner"], made["version"]) == (["github.com", "gitlab.com"], "member", 0)
    text = ok(
        identity(
            owner,
            "create",
            "northwind-crm",
            "--domain",
            "crm.northwind.example",
            "--worker",
            "mac-mini",
            "--owner",
            "project",
        )
    ).out
    assert text.startswith("Created the project's browser identity northwind-crm")
    ok(identity(owner, "create", "northwind-crm", "--domain", "crm.northwind.example", "--worker", "mac-mini"))

    shown = as_json(identity(owner, "list", "--json"))
    assert_json_keys("hub browser identity list", shown)
    assert sorted((i["owner"], i["name"]) for i in shown) == [
        ("member", "github-work"),
        ("member", "northwind-crm"),
        ("project", "northwind-crm"),
    ]
    table_text = ok(identity(owner, "list")).out
    assert "github-work" in table_text and "github.com, gitlab.com" in table_text and "project" in table_text
    assert [i["name"] for i in as_json(identity(other, "list", "--json"))] == ["northwind-crm"]

    missing = identity(other, "delete", "github-work")
    assert missing.code == 2 and "has no browser identity github-work" in missing.err
    both = identity(owner, "delete", "northwind-crm")
    assert both.code == 2 and "pass --owner" in both.err
    gone = as_json(identity(owner, "delete", "northwind-crm", "--owner", "project", "--json"))
    assert_json_keys("hub browser identity delete", gone)
    assert (gone["owner"], gone["deleted"]) == ("project", True)
    assert ok(identity(owner, "delete", "github-work")).out.startswith("Deleted browser identity github-work")
    assert [i["name"] for i in as_json(identity(owner, "list", "--json"))] == ["northwind-crm"]
    refused = identity(other, "create", "github-other", "--domain", "github.com", "--worker", "mac-mini")
    assert refused.code == 1 and "worker mac-mini" in refused.err


def test_browser_identity_commands_are_in_the_contract():
    from evo_agents.hub.contract import commands

    listed_ = commands()
    for verb in ("list", "create", "delete"):
        name = f"hub browser identity {verb}"
        assert name in listed_, name
        assert set(listed_[name]["json"]["keys"]) >= set(BrowserIdentity.model_fields), name
    assert OTHER  # a member of the hub fixture
