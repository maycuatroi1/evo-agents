"""Schema 0011 and sealing: the secrets, bindings and leases of ``docs/credentials.md`` in Postgres, and the AES-256-GCM
of ``evo_agents.hub.server.sealing`` that keeps their values.

The checks step 3 of the worker-credentials plan names: a database with workers and runs goes up from 0010 to 0011,
down to 0010 with the schema 0010 had, and up again; a value sealed under another key, or opened as the value of
another owner, name, kind or lease, does not open; and a dump of the database holds none of the values sealed into
it. Around them: a value read back from its row opens there and nowhere else, and the constraints refuse rows the
design rules out."""

import base64
import hashlib
import secrets
import shutil
import subprocess
from datetime import UTC, datetime, timedelta

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from psycopg import errors, sql

from evo_agents.hub.config import HubConfig, load_config
from evo_agents.hub.migrate import migrate
from evo_agents.hub.server import sealing
from evo_agents.hub.server.sealing import Sealed, Sealer, Unsealable, lease_aad, secret_aad
from tests.hub.test_migrate import SNAPSHOT, move_to, one, query, tables
from tests.hub.test_run_tables import DAY, add_plan, add_run, seed_runs

KEY = sealing.new_key()
SEALER = Sealer.from_config(load_config({"EVO_HUB_DSN": "postgresql://hub@db/hub", "EVO_HUB_SECRETS_KEY": KEY}))
# Values drawn for this run of the tests, so that a dump holding one cannot be a coincidence.
ENV_VALUE = "oauth-sample-" + secrets.token_hex(16)
GIT_VALUE = "glpat-sample-" + secrets.token_hex(10)
TOKEN_VALUE = "ghs_" + secrets.token_hex(18)
GITLAB = "https://gitlab.example.org/ops"
GITHUB = "https://github.com/maycuatroi1/evo-agents"


# Sealing


def test_a_sealed_value_opens_only_under_its_key_as_the_value_it_was_sealed_as():
    aad = secret_aad(7, "gitlab-ops", "git")
    sealed = SEALER.seal(GIT_VALUE, aad)
    assert SEALER.open(sealed, aad) == GIT_VALUE
    assert GIT_VALUE.encode() not in sealed.ciphertext
    assert len(sealed.nonce) == sealing.NONCE_BYTES and len(sealed.ciphertext) == len(GIT_VALUE) + sealing.TAG_BYTES
    assert sealed.key_id == SEALER.key_id == hashlib.sha256(base64.urlsafe_b64decode(KEY + "=")).hexdigest()[:8]

    # another owner, name or kind, or a lease: the associated data differs and the value does not open
    for other in (
        secret_aad(8, "gitlab-ops", "git"),
        secret_aad(7, "gitlab-ops-2", "git"),
        secret_aad(7, "gitlab-ops", "env"),
        lease_aad(7),
        b"",
    ):
        with pytest.raises(Unsealable, match="does not open here"):
            SEALER.open(sealed, other)

    # another key: refused by its key_id, and refused by AES-GCM when the key_id is made to match
    other_key = Sealer(base64.urlsafe_b64decode(sealing.new_key() + "="))
    with pytest.raises(Unsealable, match=f"sealed under key {SEALER.key_id}, and the hub's key is {other_key.key_id}"):
        other_key.open(sealed, aad)
    with pytest.raises(Unsealable, match="does not open here"):
        other_key.open(Sealed(sealed.ciphertext, sealed.nonce, other_key.key_id), aad)

    # one bit changed in the ciphertext or the nonce, or a nonce of another length
    flipped = bytes([sealed.ciphertext[0] ^ 1]) + sealed.ciphertext[1:]
    for altered in (
        Sealed(flipped, sealed.nonce, sealed.key_id),
        Sealed(sealed.ciphertext, bytes([sealed.nonce[0] ^ 1]) + sealed.nonce[1:], sealed.key_id),
        Sealed(sealed.ciphertext, b"", sealed.key_id),
        Sealed(sealed.ciphertext[: sealing.TAG_BYTES - 1], sealed.nonce, sealed.key_id),
    ):
        with pytest.raises(Unsealable):
            SEALER.open(altered, aad)


def test_a_leased_token_opens_only_as_the_value_of_its_lease():
    sealed = SEALER.seal(TOKEN_VALUE, lease_aad(41))
    assert SEALER.open(sealed, lease_aad(41)) == TOKEN_VALUE
    for other in (lease_aad(42), secret_aad(41, "lease", "git")):
        with pytest.raises(Unsealable):
            SEALER.open(sealed, other)


def test_every_seal_draws_a_new_nonce():
    aad = secret_aad(7, "claude-oauth", "env")
    first, second = SEALER.seal(ENV_VALUE, aad), SEALER.seal(ENV_VALUE, aad)
    assert first.nonce != second.nonce and first.ciphertext != second.ciphertext
    assert SEALER.open(first, aad) == SEALER.open(second, aad) == ENV_VALUE


def test_the_key_stays_out_of_reprs_and_errors():
    key = base64.urlsafe_b64decode(KEY + "=")
    assert repr(SEALER) == f"Sealer(key_id={SEALER.key_id!r})"
    sealed = SEALER.seal(ENV_VALUE, secret_aad(7, "claude-oauth", "env"))
    for aad in (secret_aad(7, "other", "env"), lease_aad(1)):
        with pytest.raises(Unsealable) as caught:
            SEALER.open(sealed, aad)
        message = str(caught.value)
        assert KEY not in message and ENV_VALUE not in message and key.hex() not in message
    with pytest.raises(ValueError, match="a sealing key is 32 bytes, not 16"):
        Sealer(key[:16])
    assert len(KEY) == 43 and set(KEY) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")


def test_without_the_key_the_hub_has_no_sealer(tmp_path):
    config = HubConfig(dsn="postgresql://hub@db/hub", data_dir=tmp_path)
    assert Sealer.from_config(config) is None
    assert config.credentials_missing() == ["EVO_HUB_SECRETS_KEY"]
    keyed = load_config({"EVO_HUB_DSN": "postgresql://hub@db/hub", "EVO_HUB_SECRETS_KEY": KEY})
    assert keyed.credentials_missing() == [] and Sealer.from_config(keyed).key_id == SEALER.key_id


# Schema 0011


def insert(conn, table: str, row: dict) -> int:
    statement = sql.SQL("INSERT INTO {} ({}) VALUES ({}) RETURNING id").format(
        sql.Identifier(table),
        sql.SQL(", ").join(map(sql.Identifier, row)),
        sql.SQL(", ").join(sql.Placeholder() * len(row)),
    )
    return conn.execute(statement, list(row.values())).fetchone()[0]


def add_secret(conn, owner: int, name: str, kind: str, value: str, **columns) -> int:
    """The secret ``name`` of ``owner``, sealed as the hub seals it: kind env sets CLAUDE_CODE_OAUTH_TOKEN, kind git
    answers for the origins under ``GITLAB`` with the username oauth2."""
    sealed = SEALER.seal(value, secret_aad(owner, name, kind))
    row = {"owner_id": owner, "name": name, "kind": kind}
    row |= {"env_var": "CLAUDE_CODE_OAUTH_TOKEN"} if kind == "env" else {"url_prefix": GITLAB, "username": "oauth2"}
    row |= {"sealed": sealed.ciphertext, "nonce": sealed.nonce, "key_id": sealed.key_id}
    return insert(conn, "secrets", row | columns)


def add_lease(conn, ids, run: int, secret: int | None = None, token: str | None = None, **columns) -> int:
    """A lease of ``secret`` to ``run`` on the worker of ``ids``, or without one a GitHub App token that lives an hour,
    sealed as the hub seals it: the row first, then the value bound to the row's id."""
    row = {"run_id": run, "worker_id": ids["worker"], "secret_id": secret, "target": GITLAB}
    if secret is None:
        expires = datetime.now(UTC) + timedelta(hours=1)
        row |= {"provider": "github-app", "target": GITHUB, "external_id": "4242", "expires_at": expires}
    else:
        row["provider"] = "secret"
    lease = insert(conn, "credential_leases", row | columns)
    if token is not None:
        sealed = SEALER.seal(token, lease_aad(lease))
        conn.execute(
            "UPDATE credential_leases SET sealed_value = %s, nonce = %s, key_id = %s WHERE id = %s",
            (sealed.ciphertext, sealed.nonce, sealed.key_id, lease),
        )
    return lease


def seed_credentials(conn, ids, run: int) -> dict:
    """Two secrets of the user of ``ids`` bound to its project, claude-oauth (env) for any of the user's workers and
    gitlab-ops (git) for the worker ``ids["worker"]`` only, and ``run`` holding a lease of gitlab-ops and a GitHub
    App token."""
    creds = {
        "env_secret": add_secret(conn, ids["user"], "claude-oauth", "env", ENV_VALUE),
        "git_secret": add_secret(conn, ids["user"], "gitlab-ops", "git", GIT_VALUE),
    }
    conn.execute(
        "INSERT INTO secret_bindings (secret_id, project_id) VALUES (%s, %s)", (creds["env_secret"], ids["project"])
    )
    conn.execute(
        "INSERT INTO secret_bindings (secret_id, project_id, worker_id) VALUES (%s, %s, %s)",
        (creds["git_secret"], ids["project"], ids["worker"]),
    )
    creds["lease"] = add_lease(conn, ids, run, secret=creds["git_secret"])
    creds["token_lease"] = add_lease(conn, ids, run, token=TOKEN_VALUE)
    return creds


def stored(conn, table: str, column: str, row: int) -> Sealed:
    """The sealed value of ``row`` as the database holds it."""
    statement = sql.SQL("SELECT {}, nonce, key_id FROM {} WHERE id = %s").format(
        sql.Identifier(column), sql.Identifier(table)
    )
    return Sealed(*conn.execute(statement, (row,)).fetchone())


@pytest.fixture
def db(hub_db):
    """A migrated database with ``seed_runs`` in it, a running run of step 4 and ``seed_credentials`` for that run,
    and a superuser connection to it, in autocommit."""
    migrate(hub_db.dsn)
    with pg.admin(hub_db.admin_dsn) as conn:
        ids = seed_runs(conn)
        ids["held"] = add_run(conn, ids, "running", step="4")
        yield conn, ids | seed_credentials(conn, ids, ids["held"])


def test_0011_goes_up_with_workers_and_runs_down_to_the_schema_of_0010_and_up_again(hub_db):
    move_to(hub_db, "0010")
    at_0010 = query(hub_db, SNAPSHOT)
    with pg.admin(hub_db.admin_dsn) as conn:
        ids = seed_runs(conn)
        held = add_run(conn, ids, "running", step="4")
        add_plan(conn, ids, "rollout")
        plan_run = add_run(conn, ids, "waiting", kind="plan", plan_id="rollout", timeout_s=DAY)
    runs = [(ids["run"], "queued"), (ids["done_run"], "done"), (held, "running"), (plan_run, "waiting")]
    workers = [(ids["worker"], "mac-mini"), (ids["other_worker"], "linux-box")]

    move_to(hub_db, "0011")
    assert tables(hub_db) >= pg.CREDENTIAL_TABLES
    # the workers of 0010 take runs from any dispatch, and how their runs were dispatched is not known
    assert query(hub_db, "SELECT id, name, dispatch_from FROM workers ORDER BY id") == [w + ("any",) for w in workers]
    assert query(hub_db, "SELECT id, state, dispatched_via FROM runs ORDER BY id") == [r + (None,) for r in runs]
    at_0011 = query(hub_db, SNAPSHOT)
    with pg.admin(hub_db.admin_dsn) as conn:
        creds = seed_credentials(conn, ids, held)
        conn.execute("UPDATE workers SET dispatch_from = 'web' WHERE id = %s", (ids["worker"],))
        conn.execute("UPDATE runs SET dispatched_via = 'web' WHERE id = %s", (held,))
        token = stored(conn, "credential_leases", "sealed_value", creds["token_lease"])
        assert SEALER.open(token, lease_aad(creds["token_lease"])) == TOKEN_VALUE

    # Back at 0010: the schema is the one 0010 had, which release 0.4.1 runs on. The secrets and leases went; the
    # workers and runs stayed as they were.
    move_to(hub_db, "0010", down=True)
    assert query(hub_db, SNAPSHOT) == at_0010
    assert not tables(hub_db) & pg.CREDENTIAL_TABLES
    assert query(hub_db, "SELECT id, name FROM workers ORDER BY id") == workers
    assert query(hub_db, "SELECT id, state FROM runs ORDER BY id") == runs

    move_to(hub_db, "0011")
    assert query(hub_db, SNAPSHOT) == at_0011
    assert query(hub_db, "SELECT count(*) FROM secrets") == [(0,)]
    with pg.admin(hub_db.admin_dsn) as conn:  # every worker takes any dispatch again, and the owner sets the secrets
        assert one(conn, "SELECT count(*) FROM workers WHERE dispatch_from = 'any'") == 2
        seed_credentials(conn, ids, held)


def test_a_dump_of_the_database_holds_none_of_the_sealed_values(hub_db, db):
    conn, ids = db
    pg_dump = shutil.which("pg_dump")
    if pg_dump is None:
        pytest.skip("pg_dump is not installed")
    dump = subprocess.run([pg_dump, "--no-owner", "--dbname", hub_db.dsn], capture_output=True, text=True, timeout=120)
    assert dump.returncode == 0, dump.stderr
    # the rows are in the dump, sealed
    for table, column, row in (
        ("secrets", "sealed", ids["env_secret"]),
        ("secrets", "sealed", ids["git_secret"]),
        ("credential_leases", "sealed_value", ids["token_lease"]),
    ):
        assert stored(conn, table, column, row).ciphertext.hex() in dump.stdout
    for value in (ENV_VALUE, GIT_VALUE, TOKEN_VALUE, KEY):
        assert value not in dump.stdout
        assert value.encode().hex() not in dump.stdout
        assert base64.b64encode(value.encode()).decode() not in dump.stdout


def test_a_value_read_back_opens_in_its_own_row_and_nowhere_else(db):
    conn, ids = db
    env_aad = secret_aad(ids["user"], "claude-oauth", "env")
    git_aad = secret_aad(ids["user"], "gitlab-ops", "git")
    assert SEALER.open(stored(conn, "secrets", "sealed", ids["env_secret"]), env_aad) == ENV_VALUE
    assert SEALER.open(stored(conn, "secrets", "sealed", ids["git_secret"]), git_aad) == GIT_VALUE
    token = stored(conn, "credential_leases", "sealed_value", ids["token_lease"])
    assert SEALER.open(token, lease_aad(ids["token_lease"])) == TOKEN_VALUE

    # gitlab-ops' value copied into the row of claude-oauth does not open as claude-oauth's
    conn.execute(
        "UPDATE secrets SET (sealed, nonce, key_id) = (SELECT sealed, nonce, key_id FROM secrets WHERE id = %s) "
        "WHERE id = %s",
        (ids["git_secret"], ids["env_secret"]),
    )
    with pytest.raises(Unsealable):
        SEALER.open(stored(conn, "secrets", "sealed", ids["env_secret"]), env_aad)
    # renamed, a secret does not open under its new name: the api seals the value again
    conn.execute("UPDATE secrets SET name = 'gitlab-ops-2' WHERE id = %s", (ids["git_secret"],))
    with pytest.raises(Unsealable):
        SEALER.open(
            stored(conn, "secrets", "sealed", ids["git_secret"]), secret_aad(ids["user"], "gitlab-ops-2", "git")
        )
    # a token copied into another lease does not open as that lease's
    other = add_lease(conn, ids, ids["held"])
    conn.execute(
        "UPDATE credential_leases SET (sealed_value, nonce, key_id) = "
        "(SELECT sealed_value, nonce, key_id FROM credential_leases WHERE id = %s) WHERE id = %s",
        (ids["token_lease"], other),
    )
    with pytest.raises(Unsealable):
        SEALER.open(stored(conn, "credential_leases", "sealed_value", other), lease_aad(other))


@pytest.mark.parametrize(
    "statement, error",
    [
        # secrets: a name, a kind and its target
        ("UPDATE secrets SET name = 'GitLab Ops' WHERE id = {git_secret}", errors.CheckViolation),
        ("UPDATE secrets SET name = '' WHERE id = {git_secret}", errors.CheckViolation),
        ("UPDATE secrets SET name = repeat('a', 65) WHERE id = {git_secret}", errors.CheckViolation),
        ("UPDATE secrets SET kind = 'file' WHERE id = {git_secret}", errors.CheckViolation),
        ("UPDATE secrets SET kind = 'git' WHERE id = {env_secret}", errors.CheckViolation),  # no url_prefix
        ("UPDATE secrets SET env_var = NULL WHERE id = {env_secret}", errors.CheckViolation),
        ("UPDATE secrets SET url_prefix = 'https://gitlab.example.org' WHERE id = {env_secret}", errors.CheckViolation),
        ("UPDATE secrets SET username = 'oauth2' WHERE id = {env_secret}", errors.CheckViolation),
        ("UPDATE secrets SET username = NULL WHERE id = {git_secret}", errors.CheckViolation),
        ("UPDATE secrets SET username = E'oauth2\\n' WHERE id = {git_secret}", errors.CheckViolation),
        # variables that steer the shell, git, the interpreter or the worker
        ("UPDATE secrets SET env_var = 'PATH' WHERE id = {env_secret}", errors.CheckViolation),
        ("UPDATE secrets SET env_var = 'HOME' WHERE id = {env_secret}", errors.CheckViolation),
        ("UPDATE secrets SET env_var = 'SSH_AUTH_SOCK' WHERE id = {env_secret}", errors.CheckViolation),
        ("UPDATE secrets SET env_var = 'GIT_ASKPASS' WHERE id = {env_secret}", errors.CheckViolation),
        ("UPDATE secrets SET env_var = 'EVO_HUB_TOKEN' WHERE id = {env_secret}", errors.CheckViolation),
        ("UPDATE secrets SET env_var = 'LD_PRELOAD' WHERE id = {env_secret}", errors.CheckViolation),
        ("UPDATE secrets SET env_var = 'DYLD_INSERT_LIBRARIES' WHERE id = {env_secret}", errors.CheckViolation),
        ("UPDATE secrets SET env_var = 'PYTHONPATH' WHERE id = {env_secret}", errors.CheckViolation),
        ("UPDATE secrets SET env_var = 'gitlab_token' WHERE id = {env_secret}", errors.CheckViolation),
        ("UPDATE secrets SET env_var = '1TOKEN' WHERE id = {env_secret}", errors.CheckViolation),
        # url_prefix in the form of normalize_origin
        (
            "UPDATE secrets SET url_prefix = 'http://gitlab.example.org/ops' WHERE id = {git_secret}",
            errors.CheckViolation,
        ),
        (
            "UPDATE secrets SET url_prefix = 'https://gitlab.example.org/ops/' WHERE id = {git_secret}",
            errors.CheckViolation,
        ),
        ("UPDATE secrets SET url_prefix = 'git@gitlab.example.org:ops' WHERE id = {git_secret}", errors.CheckViolation),
        (
            "UPDATE secrets SET url_prefix = 'https://GitLab.example.org/ops' WHERE id = {git_secret}",
            errors.CheckViolation,
        ),
        (
            "UPDATE secrets SET url_prefix = 'https://me@gitlab.example.org/ops' WHERE id = {git_secret}",
            errors.CheckViolation,
        ),
        (
            "UPDATE secrets SET url_prefix = 'https://gitlab.example.org/o ps' WHERE id = {git_secret}",
            errors.CheckViolation,
        ),
        # the sealed value: all three columns while the secret lives, none once it is deleted
        (
            "UPDATE secrets SET sealed = NULL, nonce = NULL, key_id = NULL WHERE id = {git_secret}",
            errors.CheckViolation,
        ),
        ("UPDATE secrets SET deleted_at = now() WHERE id = {git_secret}", errors.CheckViolation),
        ("UPDATE secrets SET nonce = NULL WHERE id = {git_secret}", errors.CheckViolation),
        ("UPDATE secrets SET key_id = NULL WHERE id = {git_secret}", errors.CheckViolation),
        ("UPDATE secrets SET nonce = decode(repeat('00', 16), 'hex') WHERE id = {git_secret}", errors.CheckViolation),
        ("UPDATE secrets SET sealed = decode(repeat('00', 16), 'hex') WHERE id = {git_secret}", errors.CheckViolation),
        (
            "UPDATE secrets SET sealed = decode(repeat('00', 16401), 'hex') WHERE id = {git_secret}",
            errors.CheckViolation,
        ),
        ("UPDATE secrets SET key_id = 'ABCDEF12' WHERE id = {git_secret}", errors.CheckViolation),
        ("UPDATE secrets SET key_id = 'abcdef1' WHERE id = {git_secret}", errors.CheckViolation),
        (
            "UPDATE secrets SET updated_at = created_at - interval '1 second' WHERE id = {git_secret}",
            errors.CheckViolation,
        ),
        (
            "INSERT INTO secrets (owner_id, name, kind, env_var, sealed, nonce, key_id) "
            "SELECT owner_id, name, kind, env_var, sealed, nonce, key_id FROM secrets WHERE id = {env_secret}",
            errors.UniqueViolation,  # a name once among the owner's live secrets
        ),
        # bindings: once each, to rows that exist
        (
            "INSERT INTO secret_bindings (secret_id, project_id) VALUES ({env_secret}, {project})",
            errors.UniqueViolation,
        ),
        (
            "INSERT INTO secret_bindings (secret_id, project_id, worker_id) VALUES ({git_secret}, {project}, {worker})",
            errors.UniqueViolation,
        ),
        (
            "INSERT INTO secret_bindings (secret_id, project_id, worker_id) VALUES ({git_secret}, {project}, 999999)",
            errors.ForeignKeyViolation,
        ),
        # leases: a secret's lease names it and holds no value; a token has an end, and its value while sealed
        ("UPDATE credential_leases SET provider = 'gitlab' WHERE id = {lease}", errors.CheckViolation),
        ("UPDATE credential_leases SET secret_id = NULL WHERE id = {lease}", errors.CheckViolation),
        ("UPDATE credential_leases SET secret_id = {git_secret} WHERE id = {token_lease}", errors.CheckViolation),
        ("UPDATE credential_leases SET expires_at = NULL WHERE id = {token_lease}", errors.CheckViolation),
        ("UPDATE credential_leases SET expires_at = issued_at WHERE id = {token_lease}", errors.CheckViolation),
        (
            "UPDATE credential_leases SET revoked_at = issued_at - interval '1 second' WHERE id = {lease}",
            errors.CheckViolation,
        ),
        (
            "UPDATE credential_leases SET (sealed_value, nonce, key_id) = "
            "(SELECT sealed, nonce, key_id FROM secrets WHERE id = {git_secret}) WHERE id = {lease}",
            errors.CheckViolation,
        ),
        ("UPDATE credential_leases SET nonce = NULL WHERE id = {token_lease}", errors.CheckViolation),
        ("UPDATE credential_leases SET key_id = NULL WHERE id = {token_lease}", errors.CheckViolation),
        ("UPDATE credential_leases SET target = '' WHERE id = {lease}", errors.CheckViolation),
        ("UPDATE credential_leases SET target = E'https://a\\nb' WHERE id = {lease}", errors.CheckViolation),
        ("UPDATE credential_leases SET external_id = '' WHERE id = {token_lease}", errors.CheckViolation),
        ("UPDATE credential_leases SET worker_id = 999999 WHERE id = {lease}", errors.ForeignKeyViolation),
        ("DELETE FROM secrets WHERE id = {git_secret}", errors.ForeignKeyViolation),  # leased: deleted softly only
        # who may dispatch to a worker, and how a run was dispatched
        ("UPDATE workers SET dispatch_from = 'machine' WHERE id = {worker}", errors.CheckViolation),
        ("UPDATE workers SET dispatch_from = NULL WHERE id = {worker}", errors.NotNullViolation),
        ("UPDATE runs SET dispatched_via = 'cli' WHERE id = {run}", errors.CheckViolation),
    ],
)
def test_constraints_of_0011_refuse_bad_rows(db, statement, error):
    conn, ids = db
    with pytest.raises(error):
        conn.execute(statement.format(**ids))


def test_constraints_of_0011_accept_good_rows(db):
    conn, ids = db
    for statement in (
        # the owner deletes gitlab-ops: the row stays for its lease, without its value
        "UPDATE secrets SET deleted_at = now(), updated_at = now(), sealed = NULL, nonce = NULL, key_id = NULL "
        "WHERE id = {git_secret}",
        "UPDATE credential_leases SET revoked_at = now() WHERE id = {lease}",
        # claude-oauth for one worker as well as for every worker, under other variables
        "INSERT INTO secret_bindings (secret_id, project_id, worker_id) VALUES ({env_secret}, {project}, {worker})",
        "UPDATE secrets SET env_var = 'ANTHROPIC_API_KEY', expires_at = now() + interval '1 year' "
        "WHERE id = {env_secret}",
        "UPDATE secrets SET env_var = 'GITLAB_TOKEN' WHERE id = {env_secret}",
        # the GitHub token ends, and the pruning drops its value
        "UPDATE credential_leases SET revoked_at = now(), sealed_value = NULL, nonce = NULL, key_id = NULL "
        "WHERE id = {token_lease}",
        # the owner keeps the worker to dispatches from the web, and dispatches from the web and from a machine
        "UPDATE workers SET dispatch_from = 'web' WHERE id = {worker}",
        "UPDATE runs SET dispatched_via = 'web' WHERE id = {held}",
        "UPDATE runs SET dispatched_via = 'machine' WHERE id = {run}",
    ):
        conn.execute(statement.format(**ids))
    # a deleted secret's name is free again, for a git secret on a whole host or a port of its own
    again = add_secret(conn, ids["user"], "gitlab-ops", "git", GIT_VALUE, url_prefix="https://gitlab.example.org")
    add_secret(conn, ids["user"], "self-hosted", "git", GIT_VALUE, url_prefix="https://git.example.org:8443/a/b.c")
    conn.execute(
        "INSERT INTO secret_bindings (secret_id, project_id, worker_id) VALUES (%s, %s, %s), (%s, %s, NULL)",
        (again, ids["project"], ids["other_worker"], again, ids["project"]),
    )
    add_lease(conn, ids, ids["held"], secret=again, expires_at=datetime.now(UTC) + timedelta(days=90))
    assert one(conn, "SELECT count(*) FROM secrets WHERE name = 'gitlab-ops'") == 2
    assert one(conn, "SELECT count(*) FROM secret_bindings WHERE secret_id = %s", again) == 2
    # the leases go with their run
    conn.execute("DELETE FROM runs WHERE id = %s", (ids["held"],))
    assert one(conn, "SELECT count(*) FROM credential_leases") == 0
