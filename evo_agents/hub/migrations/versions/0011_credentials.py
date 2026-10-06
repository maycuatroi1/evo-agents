"""Credentials of runs: the secrets members keep on the hub, where they are bound, and the leases runs get of them.

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-06

``docs/credentials.md`` describes the design these tables hold the state of, ``evo_agents.hub.credentials`` the kinds,
providers and limits, and ``evo_agents.hub.server.sealing`` how a value is sealed; the lists below are copies of
those modules' as of this revision, never imports, so a later change to them takes a new revision.

secrets is one value a member wrote: a name unique among the owner's secrets that are not deleted (a deleted secret's
name may be used again), kind 'env' with the variable it sets in the agent's environment (never one of the variables
that steer the shell, git or the worker), or kind 'git' with the https prefix of the origins it answers for, in the
normalized form of ``credentials.normalize_origin``, and the username git gets. The value is sealed with AES-256-GCM:
sealed is the ciphertext with its 16-byte tag, nonce the 12 random bytes drawn for it and key_id the first 8 hex
digits of the SHA-256 of the key that sealed it, which is not in the database. The associated data binds it to
its owner, name and kind. expires_at is the end its owner gave it. Deleting a secret keeps its row, for the leases
and the audit that name it, and drops the sealed value: a deleted secret has none, a live one has all three columns.

secret_bindings says where a secret goes: to runs of a project, on any worker of the secret's owner (worker_id NULL)
or on one of them. A binding goes with its secret, project or worker.

credential_leases is one credential handed to one run on one worker: a member's secret (provider 'secret'), or an
installation token the hub's GitHub App made (provider 'github-app'), whose installation is external_id. target is
the variable or the origins the lease is for. A GitHub token lives an hour, so it has expires_at, and stays sealed
(sealed_value, nonce, key_id, bound to the lease's id) until it expires, so the hub can still revoke it; a lease of a
secret never holds a value. revoked_at is when the run gave it back or the hub took it. A lease goes with its run, and
RESTRICTs the deletion of its worker and its secret.

workers.dispatch_from is who may hand the worker its runs: 'any' (as every worker before this revision), or 'web',
dispatches from a web session only. runs.dispatched_via is the credential of the dispatch, 'machine' or 'web', NULL
for the runs dispatched before this revision.

Going back drops the tables and the two columns, which is the schema that release 0.4.1 runs on. The secrets and
the leases go with them: their owners set the secrets again after coming back up.

The statements go through SQLAlchemy's text(): no percent signs, and no colon directly before a word.
"""

from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None

MAX_SECRET_BYTES = 16384  # one value, as evo_agents.hub.credentials.MAX_SECRET_BYTES
TAG_BYTES = 16  # of AES-GCM, appended to the ciphertext (evo_agents.hub.server.sealing)
NONCE_BYTES = 12
SEALED = f"BETWEEN {1 + TAG_BYTES} AND {MAX_SECRET_BYTES + TAG_BYTES}"
KEY_ID = r"'^[0-9a-f]{8}$'"  # the first 8 hex digits of the key's SHA-256

# As evo_agents.hub.credentials at this revision.
SECRET_KINDS = "('env', 'git')"
PROVIDERS = "('secret', 'github-app')"
SECRET_NAME = r"'^[a-z0-9][a-z0-9._-]{0,63}$'"
ENV_NAME = r"'^[A-Z_][A-Z0-9_]*$'"
DENIED_ENV = "('HOME', 'PATH', 'SHELL', 'SSH_AUTH_SOCK', 'TMPDIR', 'USER')"
DENIED_ENV_PREFIXES = r"'^(EVO_|GIT_|LD_|DYLD_|PYTHON)'"
# https://host[:port][/path], host in lower case, no user, no trailing slash: the form of normalize_origin
URL_PREFIX = r"'^https://[a-z0-9._-]+(:[0-9]{1,5})?(/[^\s]*[^/\s])?$'"
# Who may hand a worker its runs, and the credential a run was dispatched with (docs/credentials.md).
DISPATCH_FROM = "('any', 'web')"
DISPATCHED_VIA = "('machine', 'web')"


def _line(column: str, limit: int) -> str:
    """1 to ``limit`` characters of ``column``, without a control character."""
    return rf"char_length({column}) BETWEEN 1 AND {limit} AND {column} !~ '[\x01-\x1f\x7f]'"


UPGRADE = (
    f"""
    CREATE TABLE secrets (
        id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        owner_id bigint NOT NULL REFERENCES users (id) ON DELETE RESTRICT,
        name text NOT NULL CHECK (name ~ {SECRET_NAME}),
        kind text NOT NULL CHECK (kind IN {SECRET_KINDS}),
        env_var text CHECK (char_length(env_var) <= 200 AND env_var ~ {ENV_NAME}
                            AND env_var NOT IN {DENIED_ENV} AND env_var !~ {DENIED_ENV_PREFIXES}),
        url_prefix text CHECK (char_length(url_prefix) <= 2000 AND url_prefix ~ {URL_PREFIX}),
        username text CHECK ({_line("username", 200)}),
        sealed bytea CHECK (octet_length(sealed) {SEALED}),
        nonce bytea CHECK (octet_length(nonce) = {NONCE_BYTES}),
        key_id text CHECK (key_id ~ {KEY_ID}),
        created_at timestamptz NOT NULL DEFAULT now(),
        updated_at timestamptz NOT NULL DEFAULT now(),
        expires_at timestamptz,
        deleted_at timestamptz,
        CHECK ((kind = 'env') = (env_var IS NOT NULL)),
        CHECK ((kind = 'git') = (url_prefix IS NOT NULL)),
        CHECK ((kind = 'git') = (username IS NOT NULL)),
        CHECK ((deleted_at IS NULL) = (sealed IS NOT NULL)),  -- deleting a secret drops its value
        CHECK ((sealed IS NULL) = (nonce IS NULL)),
        CHECK ((sealed IS NULL) = (key_id IS NULL)),
        CHECK (updated_at >= created_at),
        CHECK (deleted_at >= created_at)
    )
    """,
    "CREATE UNIQUE INDEX secrets_name_key ON secrets (owner_id, name) WHERE deleted_at IS NULL",
    """
    CREATE TABLE secret_bindings (
        secret_id bigint NOT NULL REFERENCES secrets (id) ON DELETE CASCADE,
        project_id bigint NOT NULL REFERENCES projects (id) ON DELETE CASCADE,
        worker_id bigint REFERENCES workers (id) ON DELETE CASCADE  -- NULL for any worker of the secret's owner
    )
    """,
    # a binding once; worker ids start at 1, so 0 stands for any worker
    "CREATE UNIQUE INDEX secret_bindings_key ON secret_bindings (secret_id, project_id, coalesce(worker_id, 0))",
    "CREATE INDEX secret_bindings_project_idx ON secret_bindings (project_id)",  # the secrets a run's project gets
    "CREATE INDEX secret_bindings_worker_idx ON secret_bindings (worker_id) WHERE worker_id IS NOT NULL",
    f"""
    CREATE TABLE credential_leases (
        id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        run_id bigint NOT NULL REFERENCES runs (id) ON DELETE CASCADE,
        worker_id bigint NOT NULL REFERENCES workers (id) ON DELETE RESTRICT,
        secret_id bigint REFERENCES secrets (id) ON DELETE RESTRICT,  -- NULL for a token the hub made
        provider text NOT NULL CHECK (provider IN {PROVIDERS}),
        target text NOT NULL CHECK ({_line("target", 2000)}),  -- the variable, or the origins
        external_id text CHECK ({_line("external_id", 200)}),  -- the GitHub App installation of a token
        issued_at timestamptz NOT NULL DEFAULT now(),
        expires_at timestamptz CHECK (expires_at > issued_at),
        revoked_at timestamptz CHECK (revoked_at >= issued_at),
        sealed_value bytea CHECK (octet_length(sealed_value) {SEALED}),
        nonce bytea CHECK (octet_length(nonce) = {NONCE_BYTES}),
        key_id text CHECK (key_id ~ {KEY_ID}),
        CHECK ((provider = 'secret') = (secret_id IS NOT NULL)),
        CHECK (provider <> 'github-app' OR expires_at IS NOT NULL),
        CHECK (sealed_value IS NULL OR provider = 'github-app'),  -- kept only to revoke a GitHub token
        CHECK ((sealed_value IS NULL) = (nonce IS NULL)),
        CHECK ((sealed_value IS NULL) = (key_id IS NULL))
    )
    """,
    "CREATE INDEX credential_leases_run_idx ON credential_leases (run_id, id)",
    # what revoking a worker or a secret gives back
    "CREATE INDEX credential_leases_worker_idx ON credential_leases (worker_id) WHERE revoked_at IS NULL",
    "CREATE INDEX credential_leases_secret_idx ON credential_leases (secret_id) WHERE revoked_at IS NULL",
    # the pruning drops the sealed tokens past their end
    "CREATE INDEX credential_leases_sealed_idx ON credential_leases (expires_at) WHERE sealed_value IS NOT NULL",
    f"ALTER TABLE workers ADD COLUMN dispatch_from text NOT NULL DEFAULT 'any' "
    f"CHECK (dispatch_from IN {DISPATCH_FROM})",
    f"ALTER TABLE runs ADD COLUMN dispatched_via text CHECK (dispatched_via IN {DISPATCHED_VIA})",
)

DOWNGRADE = (
    "DROP TABLE credential_leases, secret_bindings, secrets",
    "ALTER TABLE runs DROP COLUMN dispatched_via",
    "ALTER TABLE workers DROP COLUMN dispatch_from",
)


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(statement)
