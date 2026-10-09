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
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import FunctionType

import pytest

from tests.hub import live, pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from psycopg import errors
from sqlalchemy import Executable, delete, func, insert, select, update

from evo_agents.hub import tables
from evo_agents.hub.config import HubConfig, load_config
from evo_agents.hub.migrate import migrate
from evo_agents.hub.server import sealing
from evo_agents.hub.server.sealing import Sealed, Sealer, Unsealable, lease_aad, secret_aad
from tests.hub.test_migrate import SNAPSHOT, move_to, one, query
from tests.hub.test_migrate import tables as table_names
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

SECRETS, BINDINGS, LEASES = tables.secrets, tables.secret_bindings, tables.credential_leases
WORKERS, RUNS = tables.workers, tables.runs
SEALED_COLUMNS = ("sealed", "nonce", "key_id")


def add_row(conn, table, row: dict) -> int:
    """Insert ``row`` into ``table``; its id."""
    return one(conn, insert(table).values(**row).returning(table.c.id))


def add_secret(conn, owner: int, name: str, kind: str, value: str, **columns) -> int:
    """The secret ``name`` of ``owner``, sealed as the hub seals it: kind env sets CLAUDE_CODE_OAUTH_TOKEN, kind git
    answers for the origins under ``GITLAB`` with the username oauth2."""
    sealed = SEALER.seal(value, secret_aad(owner, name, kind))
    row = {"owner_id": owner, "name": name, "kind": kind}
    row |= {"env_var": "CLAUDE_CODE_OAUTH_TOKEN"} if kind == "env" else {"url_prefix": GITLAB, "username": "oauth2"}
    row |= {"sealed": sealed.ciphertext, "nonce": sealed.nonce, "key_id": sealed.key_id}
    return add_row(conn, SECRETS, row | columns)


def add_lease(conn, ids, run: int, secret: int | None = None, token: str | None = None, **columns) -> int:
    """A lease of ``secret`` to ``run`` on the worker of ``ids``, or without one a GitHub App token that lives an hour,
    sealed as the hub seals it: the row first, then the value bound to the row's id."""
    row = {"run_id": run, "worker_id": ids["worker"], "secret_id": secret, "target": GITLAB}
    if secret is None:
        expires = datetime.now(UTC) + timedelta(hours=1)
        row |= {"provider": "github-app", "target": GITHUB, "external_id": "4242", "expires_at": expires}
    else:
        row["provider"] = "secret"
    lease = add_row(conn, LEASES, row | columns)
    if token is not None:
        sealed = SEALER.seal(token, lease_aad(lease))
        conn.execute(
            update(LEASES)
            .values(sealed_value=sealed.ciphertext, nonce=sealed.nonce, key_id=sealed.key_id)
            .where(LEASES.c.id == lease)
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
    conn.execute(insert(BINDINGS).values(secret_id=creds["env_secret"], project_id=ids["project"]))
    conn.execute(
        insert(BINDINGS).values(secret_id=creds["git_secret"], project_id=ids["project"], worker_id=ids["worker"])
    )
    creds["lease"] = add_lease(conn, ids, run, secret=creds["git_secret"])
    creds["token_lease"] = add_lease(conn, ids, run, token=TOKEN_VALUE)
    return creds


def stored(conn, table, column: str, row: int) -> Sealed:
    """The sealed value of ``row`` of ``table`` as the database holds it, ``column`` its ciphertext."""
    found = select(table.c[column].label("ciphertext"), table.c.nonce, table.c.key_id).where(table.c.id == row)
    return Sealed(**conn.execute(found).one()._mapping)


def copied(source, row: int, **columns) -> dict:
    """Values for an UPDATE: each of ``columns`` (target=source column) read from ``row`` of ``source``, through an
    alias, so the UPDATE may write another row of the same table."""
    copy = source.alias("source")
    return {target: select(copy.c[name]).where(copy.c.id == row).scalar_subquery() for target, name in columns.items()}


def count(table, *where):
    return select(func.count()).select_from(table).where(*where)


@pytest.fixture
def db(hub_db):
    """A migrated database with ``seed_runs`` in it, a running run of step 4 and ``seed_credentials`` for that run,
    and a connection to it as its owner, in autocommit."""
    migrate(hub_db.dsn)
    with live.connect(hub_db) as conn:
        ids = seed_runs(conn)
        ids["held"] = add_run(conn, ids, "running", step="4")
        yield conn, ids | seed_credentials(conn, ids, ids["held"])


@pytest.mark.empty_db
def test_0011_goes_up_with_workers_and_runs_down_to_the_schema_of_0010_and_up_again(hub_db):
    move_to(hub_db, "0010")
    at_0010 = query(hub_db, SNAPSHOT)
    with live.connect(hub_db) as conn:
        ids = seed_runs(conn)
        held = add_run(conn, ids, "running", step="4")
        add_plan(conn, ids, "rollout")
        plan_run = add_run(conn, ids, "waiting", kind="plan", plan_id="rollout", timeout_s=DAY)
    runs = [(ids["run"], "queued"), (ids["done_run"], "done"), (held, "running"), (plan_run, "waiting")]
    workers = [(ids["worker"], "mac-mini"), (ids["other_worker"], "linux-box")]

    move_to(hub_db, "0011")
    assert table_names(hub_db) >= pg.CREDENTIAL_TABLES
    # the workers of 0010 take runs from any dispatch, and how their runs were dispatched is not known
    listed = select(WORKERS.c.id, WORKERS.c.name, WORKERS.c.dispatch_from).order_by(WORKERS.c.id)
    assert query(hub_db, listed) == [w + ("any",) for w in workers]
    listed = select(RUNS.c.id, RUNS.c.state, RUNS.c.dispatched_via).order_by(RUNS.c.id)
    assert query(hub_db, listed) == [r + (None,) for r in runs]
    at_0011 = query(hub_db, SNAPSHOT)
    with live.connect(hub_db) as conn:
        creds = seed_credentials(conn, ids, held)
        conn.execute(update(WORKERS).values(dispatch_from="web").where(WORKERS.c.id == ids["worker"]))
        conn.execute(update(RUNS).values(dispatched_via="web").where(RUNS.c.id == held))
        token = stored(conn, LEASES, "sealed_value", creds["token_lease"])
        assert SEALER.open(token, lease_aad(creds["token_lease"])) == TOKEN_VALUE

    # Back at 0010: the schema is the one 0010 had, which release 0.4.1 runs on. The secrets and leases went; the
    # workers and runs stayed as they were.
    move_to(hub_db, "0010", down=True)
    assert query(hub_db, SNAPSHOT) == at_0010
    assert not table_names(hub_db) & pg.CREDENTIAL_TABLES
    assert query(hub_db, select(WORKERS.c.id, WORKERS.c.name).order_by(WORKERS.c.id)) == workers
    assert query(hub_db, select(RUNS.c.id, RUNS.c.state).order_by(RUNS.c.id)) == runs

    move_to(hub_db, "0011")
    assert query(hub_db, SNAPSHOT) == at_0011
    assert query(hub_db, count(SECRETS)) == [(0,)]
    with live.connect(hub_db) as conn:  # every worker takes any dispatch again, and the owner sets the secrets
        assert one(conn, count(WORKERS, WORKERS.c.dispatch_from == "any")) == 2
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
        (SECRETS, "sealed", ids["env_secret"]),
        (SECRETS, "sealed", ids["git_secret"]),
        (LEASES, "sealed_value", ids["token_lease"]),
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
    assert SEALER.open(stored(conn, SECRETS, "sealed", ids["env_secret"]), env_aad) == ENV_VALUE
    assert SEALER.open(stored(conn, SECRETS, "sealed", ids["git_secret"]), git_aad) == GIT_VALUE
    token = stored(conn, LEASES, "sealed_value", ids["token_lease"])
    assert SEALER.open(token, lease_aad(ids["token_lease"])) == TOKEN_VALUE

    # gitlab-ops' value copied into the row of claude-oauth does not open as claude-oauth's
    git_value = copied(SECRETS, ids["git_secret"], **{column: column for column in SEALED_COLUMNS})
    conn.execute(update(SECRETS).values(**git_value).where(SECRETS.c.id == ids["env_secret"]))
    with pytest.raises(Unsealable):
        SEALER.open(stored(conn, SECRETS, "sealed", ids["env_secret"]), env_aad)
    # renamed, a secret does not open under its new name: the api seals the value again
    conn.execute(update(SECRETS).values(name="gitlab-ops-2").where(SECRETS.c.id == ids["git_secret"]))
    with pytest.raises(Unsealable):
        SEALER.open(stored(conn, SECRETS, "sealed", ids["git_secret"]), secret_aad(ids["user"], "gitlab-ops-2", "git"))
    # a token copied into another lease does not open as that lease's
    other = add_lease(conn, ids, ids["held"])
    token_value = copied(LEASES, ids["token_lease"], sealed_value="sealed_value", nonce="nonce", key_id="key_id")
    conn.execute(update(LEASES).values(**token_value).where(LEASES.c.id == other))
    with pytest.raises(Unsealable):
        SEALER.open(stored(conn, LEASES, "sealed_value", other), lease_aad(other))


@dataclass(frozen=True)
class Change:
    """A statement on the seeded rows, built once their ids are known; ``label`` names it in the test's id."""

    label: str
    build: Callable[[dict], Executable]


def _shown(value) -> str:
    if isinstance(value, FunctionType):
        return "<of the ids>"
    text = repr(value) if isinstance(value, str | bytes | None) else str(value)
    return text if len(text) <= 40 else text[:37] + "..."


def changed(table, key: str, **values) -> Change:
    """UPDATE ``table`` SET ``values`` on the row ``ids[key]``; a value that is a function takes the ids."""

    def build(ids: dict):
        given = {name: value(ids) if isinstance(value, FunctionType) else value for name, value in values.items()}
        return update(table).values(**given).where(table.c.id == ids[key])

    shown = ", ".join(f"{name}={_shown(value)}" for name, value in values.items())
    return Change(f"{table.name} {key}: {shown}", build)


def bound(**row) -> Change:
    """INSERT INTO secret_bindings the row whose values name keys of the ids, or are numbers as they are."""

    def build(ids: dict):
        return insert(BINDINGS).values(**{name: ids.get(value, value) for name, value in row.items()})

    return Change("secret_bindings: " + ", ".join(f"{name}={value}" for name, value in row.items()), build)


def _copy_of_env_secret(ids: dict):
    columns = ("owner_id", "name", "kind", "env_var", *SEALED_COLUMNS)
    found = select(*(SECRETS.c[name] for name in columns)).where(SECRETS.c.id == ids["env_secret"])
    return insert(SECRETS).from_select(columns, found)


def _git_secret_on_lease(ids: dict):
    git_value = copied(SECRETS, ids["git_secret"], sealed_value="sealed", nonce="nonce", key_id="key_id")
    return update(LEASES).values(**git_value).where(LEASES.c.id == ids["lease"])


EARLIER = timedelta(seconds=1)


@pytest.mark.parametrize(
    "change, error",
    [
        # secrets: a name, a kind and its target
        (changed(SECRETS, "git_secret", name="GitLab Ops"), errors.CheckViolation),
        (changed(SECRETS, "git_secret", name=""), errors.CheckViolation),
        (changed(SECRETS, "git_secret", name=func.repeat("a", 65)), errors.CheckViolation),
        (changed(SECRETS, "git_secret", kind="file"), errors.CheckViolation),
        (changed(SECRETS, "env_secret", kind="git"), errors.CheckViolation),  # no url_prefix
        (changed(SECRETS, "env_secret", env_var=None), errors.CheckViolation),
        (changed(SECRETS, "env_secret", url_prefix="https://gitlab.example.org"), errors.CheckViolation),
        (changed(SECRETS, "env_secret", username="oauth2"), errors.CheckViolation),
        (changed(SECRETS, "git_secret", username=None), errors.CheckViolation),
        (changed(SECRETS, "git_secret", username="oauth2\n"), errors.CheckViolation),
        # variables that steer the shell, git, the interpreter or the worker
        (changed(SECRETS, "env_secret", env_var="PATH"), errors.CheckViolation),
        (changed(SECRETS, "env_secret", env_var="HOME"), errors.CheckViolation),
        (changed(SECRETS, "env_secret", env_var="SSH_AUTH_SOCK"), errors.CheckViolation),
        (changed(SECRETS, "env_secret", env_var="GIT_ASKPASS"), errors.CheckViolation),
        (changed(SECRETS, "env_secret", env_var="EVO_HUB_TOKEN"), errors.CheckViolation),
        (changed(SECRETS, "env_secret", env_var="LD_PRELOAD"), errors.CheckViolation),
        (changed(SECRETS, "env_secret", env_var="DYLD_INSERT_LIBRARIES"), errors.CheckViolation),
        (changed(SECRETS, "env_secret", env_var="PYTHONPATH"), errors.CheckViolation),
        (changed(SECRETS, "env_secret", env_var="gitlab_token"), errors.CheckViolation),
        (changed(SECRETS, "env_secret", env_var="1TOKEN"), errors.CheckViolation),
        # url_prefix in the form of normalize_origin
        (changed(SECRETS, "git_secret", url_prefix="http://gitlab.example.org/ops"), errors.CheckViolation),
        (changed(SECRETS, "git_secret", url_prefix="https://gitlab.example.org/ops/"), errors.CheckViolation),
        (changed(SECRETS, "git_secret", url_prefix="git@gitlab.example.org:ops"), errors.CheckViolation),
        (changed(SECRETS, "git_secret", url_prefix="https://GitLab.example.org/ops"), errors.CheckViolation),
        (changed(SECRETS, "git_secret", url_prefix="https://me@gitlab.example.org/ops"), errors.CheckViolation),
        (changed(SECRETS, "git_secret", url_prefix="https://gitlab.example.org/o ps"), errors.CheckViolation),
        # the sealed value: all three columns while the secret lives, none once it is deleted
        (changed(SECRETS, "git_secret", sealed=None, nonce=None, key_id=None), errors.CheckViolation),
        (changed(SECRETS, "git_secret", deleted_at=func.now()), errors.CheckViolation),
        (changed(SECRETS, "git_secret", nonce=None), errors.CheckViolation),
        (changed(SECRETS, "git_secret", key_id=None), errors.CheckViolation),
        (changed(SECRETS, "git_secret", nonce=bytes(16)), errors.CheckViolation),
        (changed(SECRETS, "git_secret", sealed=bytes(16)), errors.CheckViolation),
        (changed(SECRETS, "git_secret", sealed=bytes(16401)), errors.CheckViolation),
        (changed(SECRETS, "git_secret", key_id="ABCDEF12"), errors.CheckViolation),
        (changed(SECRETS, "git_secret", key_id="abcdef1"), errors.CheckViolation),
        (changed(SECRETS, "git_secret", updated_at=SECRETS.c.created_at - EARLIER), errors.CheckViolation),
        (
            Change("secrets: a copy of env_secret", _copy_of_env_secret),
            errors.UniqueViolation,  # a name once among the owner's live secrets
        ),
        # bindings: once each, to rows that exist
        (bound(secret_id="env_secret", project_id="project"), errors.UniqueViolation),
        (bound(secret_id="git_secret", project_id="project", worker_id="worker"), errors.UniqueViolation),
        (bound(secret_id="git_secret", project_id="project", worker_id=999999), errors.ForeignKeyViolation),
        # leases: a secret's lease names it and holds no value; a token has an end, and its value while sealed
        (changed(LEASES, "lease", provider="gitlab"), errors.CheckViolation),
        (changed(LEASES, "lease", secret_id=None), errors.CheckViolation),
        (changed(LEASES, "token_lease", secret_id=lambda ids: ids["git_secret"]), errors.CheckViolation),
        (changed(LEASES, "token_lease", expires_at=None), errors.CheckViolation),
        (changed(LEASES, "token_lease", expires_at=LEASES.c.issued_at), errors.CheckViolation),
        (changed(LEASES, "lease", revoked_at=LEASES.c.issued_at - EARLIER), errors.CheckViolation),
        (
            Change("credential_leases lease: the sealed value of git_secret", _git_secret_on_lease),
            errors.CheckViolation,
        ),
        (changed(LEASES, "token_lease", nonce=None), errors.CheckViolation),
        (changed(LEASES, "token_lease", key_id=None), errors.CheckViolation),
        (changed(LEASES, "lease", target=""), errors.CheckViolation),
        (changed(LEASES, "lease", target="https://a\nb"), errors.CheckViolation),
        (changed(LEASES, "token_lease", external_id=""), errors.CheckViolation),
        (changed(LEASES, "lease", worker_id=999999), errors.ForeignKeyViolation),
        (
            Change("secrets: git_secret deleted", lambda ids: delete(SECRETS).where(SECRETS.c.id == ids["git_secret"])),
            errors.ForeignKeyViolation,  # leased: deleted softly only
        ),
        # who may dispatch to a worker, and how a run was dispatched
        (changed(WORKERS, "worker", dispatch_from="machine"), errors.CheckViolation),
        (changed(WORKERS, "worker", dispatch_from=None), errors.NotNullViolation),
        (changed(RUNS, "run", dispatched_via="cli"), errors.CheckViolation),
    ],
    ids=lambda value: value.label if isinstance(value, Change) else value.__name__,
)
def test_constraints_of_0011_refuse_bad_rows(db, change, error):
    conn, ids = db
    with pytest.raises(error):
        conn.execute(change.build(ids))


def test_constraints_of_0011_accept_good_rows(db):
    conn, ids = db
    for statement in (
        # the owner deletes gitlab-ops: the row stays for its lease, without its value
        update(SECRETS)
        .values(deleted_at=func.now(), updated_at=func.now(), sealed=None, nonce=None, key_id=None)
        .where(SECRETS.c.id == ids["git_secret"]),
        update(LEASES).values(revoked_at=func.now()).where(LEASES.c.id == ids["lease"]),
        # claude-oauth for one worker as well as for every worker, under other variables
        insert(BINDINGS).values(secret_id=ids["env_secret"], project_id=ids["project"], worker_id=ids["worker"]),
        update(SECRETS)
        .values(env_var="ANTHROPIC_API_KEY", expires_at=func.now() + func.make_interval(1))  # a year
        .where(SECRETS.c.id == ids["env_secret"]),
        update(SECRETS).values(env_var="GITLAB_TOKEN").where(SECRETS.c.id == ids["env_secret"]),
        # the GitHub token ends, and the pruning drops its value
        update(LEASES)
        .values(revoked_at=func.now(), sealed_value=None, nonce=None, key_id=None)
        .where(LEASES.c.id == ids["token_lease"]),
        # the owner keeps the worker to dispatches from the web, and dispatches from the web and from a machine
        update(WORKERS).values(dispatch_from="web").where(WORKERS.c.id == ids["worker"]),
        update(RUNS).values(dispatched_via="web").where(RUNS.c.id == ids["held"]),
        update(RUNS).values(dispatched_via="machine").where(RUNS.c.id == ids["run"]),
    ):
        conn.execute(statement)
    # a deleted secret's name is free again, for a git secret on a whole host or a port of its own
    again = add_secret(conn, ids["user"], "gitlab-ops", "git", GIT_VALUE, url_prefix="https://gitlab.example.org")
    add_secret(conn, ids["user"], "self-hosted", "git", GIT_VALUE, url_prefix="https://git.example.org:8443/a/b.c")
    conn.execute(
        insert(BINDINGS).values(
            [
                {"secret_id": again, "project_id": ids["project"], "worker_id": ids["other_worker"]},
                {"secret_id": again, "project_id": ids["project"], "worker_id": None},
            ]
        )
    )
    add_lease(conn, ids, ids["held"], secret=again, expires_at=datetime.now(UTC) + timedelta(days=90))
    assert one(conn, count(SECRETS, SECRETS.c.name == "gitlab-ops")) == 2
    assert one(conn, count(BINDINGS, BINDINGS.c.secret_id == again)) == 2
    # the leases go with their run
    conn.execute(delete(RUNS).where(RUNS.c.id == ids["held"]))
    assert one(conn, count(LEASES)) == 0
