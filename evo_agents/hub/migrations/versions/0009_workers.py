"""Workers and runs: the machines members register, the plan steps the hub hands them, and what the runs report.

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-05

``docs/workers.md`` describes the protocol these tables hold the state of, and ``evo_agents.hub.runs`` the states,
event kinds and limits; the lists below are copies of that module's as of this revision, never imports, so a later
change to the module takes a new revision.

tokens.kind takes 'worker', the evw_ token of one worker, whose host is the worker's hostname. workers is a machine
its owner registered: a name unique among the owner's workers that are not revoked (a revoked worker's name may be
used again), host facts, 1 to 8 slots, labels, what its heartbeats report of its runtimes, checkouts (JSON
objects whose keys the api sets) and free slots, whether it allows the web terminal, and its last heartbeat, drain
and revocation,
from which ``runs.worker_status`` reads its status. worker_projects lists the projects a worker takes runs of.
worker_pairings is a code the web created for a machine to join with: it lasts at most 10 minutes and is locked
after 5 wrong tries, and once used it names the worker it made. The hub keeps the HMAC-SHA256 of the whole code under
the session secret and its first four characters in clear, the selector: a join finds the pairing by its selector, so
a wrong rest of the code counts as a wrong try against that pairing, and no two unused pairings share one. projects
is an array of project ids, checked by the api when the worker joins.

runs is one attempt at one plan step on one worker, dispatched from a revision of the plan that plan_revisions
holds. requested_runtime is the runtime the dispatch asked for, 'any' included, which the next attempt asks for
again; runtime is the same until a worker claims the run, when a request for 'any' takes the runtime the worker
picked. pinned_worker_id is the worker a dispatch named, worker_id the one that claimed the run. A run that left
'queued' other than to 'cancelled' or 'failed' was claimed, so it has a worker, a runtime and leased_at; a held run
has a lease, which the reaper reads; a final run has finished_at, and a failed one an error. attempt counts the runs of
one dispatch, at most max_attempts (3, as runs.MAX_ATTEMPTS), and each one after the first names the run it
retries. At most one run of a step is active, which a partial unique index over the active states holds.

run_events is a run's log. seq is the hub's own count over all of the run's events, the worker's and the hub's;
runs.event_seq is the last seq it gave out. A worker numbers its events too, and runs.events_acked is the highest of
those numbers stored with none missing below, which the hub answers as ack_seq; a worker's event at or below it is
a resend. truncated marks a body the hub cut to 64 KiB. Events are never updated; the hub deletes them some days
after their run ends, and they go with their run.
run_inbox holds the owner's messages to the run's agent, at most 8 KiB each, until the worker has them.

Deletion rules as in 0001: the rows that describe a worker or a run go with it, runs RESTRICT the deletion of their
project, plan revision, worker and dispatcher, and pairings go with the member who made them.

Going back drops the tables, then the worker tokens: the audit rows written with one keep their actor and lose the
token (the update trigger is off for that statement only), and tokens.kind takes 'machine' and 'web' again, which is
the schema that release 0.2.3 runs on.

The statements go through SQLAlchemy's text(): no percent signs, and no colon directly before a word.
"""

from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None

HEX_SHA256 = "'^[0-9a-f]{64}$'"
OBJECT_NAME = "'^([0-9a-f]{40}|[0-9a-f]{64})$'"  # a full SHA-1 or SHA-256 git object name
WORKER_NAME = "'^[A-Za-z0-9][A-Za-z0-9._-]*$'"
SELECTOR = "'^[0-9A-HJKMNP-TV-Z]{4}$'"  # four characters of Crockford base32
LABEL = "[A-Za-z0-9][A-Za-z0-9._-]{0,39}"
MAX_LABELS = 16
SLOTS = "smallint NOT NULL DEFAULT 1 CHECK (slots BETWEEN 1 AND 8)"
PAIRING_MINUTES = 10
PAIRING_ATTEMPTS = 5
MAX_ATTEMPTS = 3  # as evo_agents.hub.runs.MAX_ATTEMPTS
TIMEOUT_SECONDS = (5 * 60, 240 * 60)
MAX_MESSAGE_BYTES = 8 * 1024  # as evo_agents.hub.runs.MAX_MESSAGE_BYTES
MAX_EVIDENCE_BYTES = 16 * 1024

# As evo_agents.hub.runs at this revision.
RUNTIMES = "('any', 'claude-code', 'opencode', 'codex')"  # RUNTIMES, and 'any' while no worker has picked one
MODES = "('headless', 'interactive')"
APPROVALS = "('auto', 'review')"
STATES = "('queued', 'leased', 'running', 'interactive', 'verifying', 'review', 'done', 'failed', 'lost', 'cancelled')"
HELD = "('leased', 'running', 'interactive', 'verifying')"
ACTIVE = "('queued', 'leased', 'running', 'interactive', 'verifying', 'review')"
TERMINAL = "('done', 'failed', 'lost', 'cancelled')"
UNCLAIMED = "('queued', 'cancelled', 'failed')"  # the states of a run no worker claimed
EVENT_KINDS = (
    "('agent_message_chunk', 'agent_thought_chunk', 'tool_call', 'tool_call_update', 'plan', 'usage_update', "
    "'user_message', 'state', 'system', 'output')"
)


def _line(column: str, limit: int) -> str:
    """1 to ``limit`` characters of ``column``, without a control character."""
    return rf"char_length({column}) BETWEEN 1 AND {limit} AND {column} !~ '[\x01-\x1f\x7f]'"


# Every element a label, matched on the array as JSON, where no label can pass for two of them.
LABELS_JSON = rf'^\[("{LABEL}"(,"{LABEL}")*)?\]$'
LABELS = (
    f"labels text[] NOT NULL DEFAULT '{{}}' CHECK (cardinality(labels) <= {MAX_LABELS} "
    f"AND CAST(array_to_json(labels) AS text) ~ '{LABELS_JSON}')"
)

UPGRADE = (
    "ALTER TABLE tokens DROP CONSTRAINT tokens_kind_check, "
    "ADD CONSTRAINT tokens_kind_check CHECK (kind IN ('machine', 'web', 'worker'))",
    f"""
    CREATE TABLE workers (
        id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        owner_id bigint NOT NULL REFERENCES users (id) ON DELETE RESTRICT,
        token_id bigint NOT NULL UNIQUE REFERENCES tokens (id) ON DELETE RESTRICT,
        name text NOT NULL CHECK (name ~ {WORKER_NAME} AND char_length(name) <= 100),
        hostname text NOT NULL CHECK ({_line("hostname", 255)}),
        os text NOT NULL CHECK ({_line("os", 40)}),
        arch text NOT NULL CHECK ({_line("arch", 40)}),
        agent_version text NOT NULL CHECK ({_line("agent_version", 100)}),  -- of the evo-agents daemon
        slots {SLOTS},
        {LABELS},
        runtimes jsonb NOT NULL DEFAULT '{{}}' CHECK (jsonb_typeof(runtimes) = 'object'),
        checkouts jsonb NOT NULL DEFAULT '{{}}' CHECK (jsonb_typeof(checkouts) = 'object'),
        free_slots smallint CHECK (free_slots BETWEEN 0 AND 8),  -- as the last heartbeat reported them
        allow_web_terminal boolean NOT NULL DEFAULT false,
        created_at timestamptz NOT NULL DEFAULT now(),
        last_heartbeat_at timestamptz,
        drained_at timestamptz,
        revoked_at timestamptz
    )
    """,
    "CREATE UNIQUE INDEX workers_name_key ON workers (owner_id, lower(name)) WHERE revoked_at IS NULL",
    """
    CREATE TABLE worker_projects (
        worker_id bigint NOT NULL REFERENCES workers (id) ON DELETE CASCADE,
        project_id bigint NOT NULL REFERENCES projects (id) ON DELETE CASCADE,
        PRIMARY KEY (worker_id, project_id)
    )
    """,
    f"""
    CREATE TABLE worker_pairings (
        id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        code_selector text NOT NULL CHECK (code_selector ~ {SELECTOR}),  -- the first four characters of the code
        code_hash text NOT NULL CHECK (code_hash ~ {HEX_SHA256}),  -- of all eight, without the hyphen
        owner_id bigint NOT NULL REFERENCES users (id) ON DELETE CASCADE,
        name text NOT NULL CHECK (name ~ {WORKER_NAME} AND char_length(name) <= 100),
        projects bigint[] NOT NULL CHECK (cardinality(projects) BETWEEN 1 AND 100
                                          AND array_position(projects, NULL) IS NULL),
        slots {SLOTS},
        {LABELS},
        allow_web_terminal boolean NOT NULL DEFAULT false,
        attempts smallint NOT NULL DEFAULT 0 CHECK (attempts BETWEEN 0 AND {PAIRING_ATTEMPTS}),  -- wrong tries
        created_at timestamptz NOT NULL DEFAULT now(),
        expires_at timestamptz NOT NULL,
        used_at timestamptz,
        worker_id bigint UNIQUE REFERENCES workers (id) ON DELETE CASCADE,
        CHECK (expires_at > created_at AND expires_at <= created_at + interval '{PAIRING_MINUTES} minutes'),
        CHECK ((used_at IS NULL) = (worker_id IS NULL))
    )
    """,
    # a join looks a code up by its selector among the unused ones; a selector an unused code holds is drawn again
    "CREATE UNIQUE INDEX worker_pairings_selector_key ON worker_pairings (code_selector) WHERE used_at IS NULL",
    "CREATE INDEX worker_pairings_owner_idx ON worker_pairings (owner_id) WHERE used_at IS NULL",
    f"""
    CREATE TABLE runs (
        id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        project_id bigint NOT NULL REFERENCES projects (id) ON DELETE RESTRICT,
        plan_id text NOT NULL,
        step_key text NOT NULL CHECK ({_line("step_key", 200)}),
        plan_revision integer NOT NULL,
        dispatched_by bigint NOT NULL REFERENCES users (id) ON DELETE RESTRICT,
        pinned_worker_id bigint REFERENCES workers (id) ON DELETE RESTRICT,
        worker_id bigint REFERENCES workers (id) ON DELETE RESTRICT,
        requested_runtime text NOT NULL CHECK (requested_runtime IN {RUNTIMES}),
        runtime text NOT NULL CHECK (runtime IN {RUNTIMES}),
        mode text NOT NULL CHECK (mode IN {MODES}),
        approval text NOT NULL CHECK (approval IN {APPROVALS}),
        timeout_s integer NOT NULL CHECK (timeout_s BETWEEN {TIMEOUT_SECONDS[0]} AND {TIMEOUT_SECONDS[1]}),
        attempt smallint NOT NULL DEFAULT 1,
        max_attempts smallint NOT NULL DEFAULT {MAX_ATTEMPTS} CHECK (max_attempts BETWEEN 1 AND {MAX_ATTEMPTS}),
        parent_run_id bigint REFERENCES runs (id) ON DELETE RESTRICT,
        state text NOT NULL DEFAULT 'queued' CHECK (state IN {STATES}),
        lease_expires_at timestamptz,
        session_id text CHECK ({_line("session_id", 200)}),
        repo text NOT NULL CHECK ({_line("repo", 200)}),
        branch text CHECK ({_line("branch", 255)}),
        commit_sha text CHECK (commit_sha ~ {OBJECT_NAME}),
        diffstat jsonb CHECK (jsonb_typeof(diffstat) = 'object'),
        verify jsonb CHECK (jsonb_typeof(verify) = 'array'),  -- each verify command the daemon ran, and its result
        evidence text CHECK (octet_length(evidence) BETWEEN 1 AND {MAX_EVIDENCE_BYTES}),
        usage jsonb CHECK (jsonb_typeof(usage) = 'object'),
        error text CHECK (char_length(error) BETWEEN 1 AND 2000),
        log_sha256 text CHECK (log_sha256 ~ {HEX_SHA256}),  -- blob kinds run-log and run-diff
        diff_sha256 text CHECK (diff_sha256 ~ {HEX_SHA256}),
        event_seq integer NOT NULL DEFAULT 0,  -- the last run_events.seq given out
        events_acked integer NOT NULL DEFAULT 0,  -- ack_seq, counted in the numbers of the worker
        cancel_requested_at timestamptz,
        queued_at timestamptz NOT NULL DEFAULT now(),
        leased_at timestamptz,
        started_at timestamptz,
        finished_at timestamptz,
        FOREIGN KEY (project_id, plan_id, plan_revision)
            REFERENCES plan_revisions (project_id, plan_id, revision) ON DELETE RESTRICT,
        CHECK (attempt BETWEEN 1 AND max_attempts),
        CHECK (attempt = 1 OR parent_run_id IS NOT NULL),
        CHECK (parent_run_id <> id),
        CHECK (pinned_worker_id IS NULL OR worker_id IS NULL OR worker_id = pinned_worker_id),
        CHECK (runtime = requested_runtime OR (requested_runtime = 'any' AND worker_id IS NOT NULL)),
        CHECK (state IN {UNCLAIMED} OR (worker_id IS NOT NULL AND runtime <> 'any' AND leased_at IS NOT NULL)),
        CHECK (state NOT IN {HELD} OR lease_expires_at IS NOT NULL),
        CHECK ((state IN {TERMINAL}) = (finished_at IS NOT NULL)),
        CHECK (state <> 'failed' OR error IS NOT NULL),
        CHECK (events_acked BETWEEN 0 AND event_seq)
    )
    """,
    f"CREATE UNIQUE INDEX runs_active_step_key ON runs (project_id, plan_id, step_key) WHERE state IN {ACTIVE}",
    # a claim takes the oldest queued run of a project the worker serves that its owner dispatched
    "CREATE INDEX runs_claim_idx ON runs (project_id, dispatched_by, id) WHERE state = 'queued'",
    f"CREATE INDEX runs_lease_idx ON runs (lease_expires_at) WHERE state IN {HELD}",  # the reaper's scan
    "CREATE INDEX runs_worker_idx ON runs (worker_id, id DESC) WHERE worker_id IS NOT NULL",
    "CREATE INDEX runs_project_idx ON runs (project_id, id DESC)",
    f"""
    CREATE TABLE run_events (
        run_id bigint NOT NULL REFERENCES runs (id) ON DELETE CASCADE,
        seq integer NOT NULL CHECK (seq >= 1),
        at timestamptz NOT NULL DEFAULT now(),
        kind text NOT NULL CHECK (kind IN {EVENT_KINDS}),
        body jsonb NOT NULL,
        truncated boolean NOT NULL DEFAULT false,
        PRIMARY KEY (run_id, seq)
    )
    """,
    "CREATE TRIGGER run_events_no_update BEFORE UPDATE ON run_events FOR EACH ROW EXECUTE FUNCTION hub_reject_update()",
    f"""
    CREATE TABLE run_inbox (
        id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        run_id bigint NOT NULL REFERENCES runs (id) ON DELETE CASCADE,
        sent_by bigint NOT NULL REFERENCES users (id) ON DELETE RESTRICT,
        body text NOT NULL CHECK (octet_length(body) BETWEEN 1 AND {MAX_MESSAGE_BYTES}),
        created_at timestamptz NOT NULL DEFAULT now(),
        delivered_at timestamptz CHECK (delivered_at >= created_at)
    )
    """,
    "CREATE INDEX run_inbox_pending_idx ON run_inbox (run_id, id) WHERE delivered_at IS NULL",
)

DOWNGRADE = (
    "DROP TABLE run_inbox, run_events, runs, worker_pairings, worker_projects, workers",
    "ALTER TABLE audit DISABLE TRIGGER audit_no_update",
    "UPDATE audit SET token_id = NULL WHERE token_id IN (SELECT id FROM tokens WHERE kind = 'worker')",
    "ALTER TABLE audit ENABLE TRIGGER audit_no_update",
    "DELETE FROM tokens WHERE kind = 'worker'",
    "ALTER TABLE tokens DROP CONSTRAINT tokens_kind_check, "
    "ADD CONSTRAINT tokens_kind_check CHECK (kind IN ('machine', 'web'))",
)


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(statement)
