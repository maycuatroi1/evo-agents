"""Plan runs, the decisions their agents ask, and the notifications that tell the owner.

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-06

``docs/workers.md`` (Plan runs) and ``docs/notifications.md`` describe what these tables hold the state of, and
``evo_agents.hub.runs`` the kinds, states and limits; the lists below are copies of that module's as of this revision,
never imports, so a later change to the module takes a new revision.

runs.kind is 'step' for a run of one step, as every run before this revision, and 'plan' for a plan run, one session
on one worker that does every step of its plan not done yet. A plan run has no step_key and no repo of its own; repos
lists the repos it works in, 1 to 50 objects each with a repo name and the branch the plan names for it (absent or
null when the plan names none). A run of one step has a step key and a repo and no repos, so each column says which
kind it belongs to. model is the model the dispatch asked for, NULL for the runtime's own choice. run_seconds is the
time the run's agent has run, which the hub adds up at each move and heartbeat and compares with timeout_s: waiting
for an answer and being parked do not count (``runs.run_seconds``). A run of one step keeps the timeout of 5 to 240
minutes; a plan run may take up to 24 hours. waiting_since is set while the run is 'waiting', from when its agent's
turn ended with a decision open, and parked_at once it is 'parked'; the reaper reads both through indexes of their
own. resume_of_run_id is the parked plan run that a plan run goes on from, in the same session and worktrees.

The states take 'waiting', a held state (the worker keeps the run and its lease), and 'parked', which no worker holds.
Both are active: a step has at most one active run, as before, and a plan has at most one active plan run, which a
second partial unique index holds.

decisions holds the questions a plan run's agent asks its owner: the run, its project, plan and step (the step is
optional), the category, the question, a context in markdown of at most 16 KiB, and 2 to 6 options, each an object
with a key (letters, digits, _ and -, at most 32 characters), a label of one line, an optional description and an
optional flag recommended, at most one option with it set. state is 'open' until the owner answers ('answered', with
an option of the decision, text of their own or both, who answered and when), or the parked run's time runs out
('expired'), or the run ends another way ('cancelled'). delivered_at is when the worker handed the answer to the agent.
A decision goes with its run.

notifications is one decision or notice for one member: kind 'decision' names the decision, kind 'notice' the
notice's kind (``runs.NOTICE_KINDS``) and its details (an object such as the repo, branch and commits of a push), with
a title of one line, a body, a link to the web page that shows it (a path of the hub's own site) and when the member
read it. A decision notifies each member once. notification_channels is where a member gets notifications besides the
web, which every member has without a row: a kind of channel (not 'web') and the config its class in the api's
registry reads, at most one channel of a kind per member. notification_deliveries is one notification on one channel,
NULL for the web: pending until the channel's class sends it, with the attempts made, when to try next and the last
error, then delivered or, after 5 attempts, failed. Notifications and their deliveries go with their member, run or
decision, and deliveries with their channel.

Going back deletes the plan runs (their events, inbox messages, decisions and notifications with them) and drops the
new tables and columns, which is the schema that release 0.3.0 runs on. A run of one step left waiting goes back to
running and one left parked is cancelled, as 0.3.0 knows neither state.

The statements go through SQLAlchemy's text(): no percent signs, and no colon directly before a word.
"""

from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None

TIMEOUT_SECONDS = (5 * 60, 240 * 60)  # a run of one step, as in 0009
PLAN_TIMEOUT_SECONDS = 24 * 60 * 60  # the most a plan run may take, runs.PLAN_TIMEOUT_CHOICES[-1] hours
MAX_REPOS = 50
MAX_QUESTION_CHARS = 2000
MAX_CONTEXT_BYTES = 16 * 1024  # as evo_agents.hub.runs.MAX_DECISION_CONTEXT_BYTES
MAX_OPTIONS_BYTES = 16 * 1024  # all the options of a decision, as JSON
MAX_ANSWER_BYTES = 4 * 1024  # leaves room in the inbox message (8 KiB) for the question and the option
MAX_BODY_BYTES = 16 * 1024
MAX_DELIVERY_ATTEMPTS = 5  # as evo_agents.hub.runs.MAX_DELIVERY_ATTEMPTS
OPTIONS = (2, 6)  # as evo_agents.hub.runs.DECISION_OPTIONS
OPTION_KEY = "^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$"  # as evo_agents.hub.runs.OPTION_KEY

# As evo_agents.hub.runs at this revision, and at 0009 for going back.
KINDS = "('step', 'plan')"
STATES = (
    "('queued', 'leased', 'running', 'interactive', 'verifying', 'waiting', 'review', 'parked', 'done', 'failed', "
    "'lost', 'cancelled')"
)
HELD = "('leased', 'running', 'interactive', 'verifying', 'waiting')"
ACTIVE = "('queued', 'leased', 'running', 'interactive', 'verifying', 'waiting', 'review', 'parked')"
CATEGORIES = "('deploy', 'delete_data', 'live_migration', 'external_send', 'spend_money', 'architecture', 'scope')"
DECISION_STATES = "('open', 'answered', 'expired', 'cancelled')"
NOTIFICATION_KINDS = "('decision', 'notice')"
NOTICE_KINDS = "('push_default_branch', 'merge_default_branch', 'plan_finished', 'run_failed')"
DELIVERY_STATES = "('pending', 'delivered', 'failed')"
STATES_0009 = (
    "('queued', 'leased', 'running', 'interactive', 'verifying', 'review', 'done', 'failed', 'lost', 'cancelled')"
)
HELD_0009 = "('leased', 'running', 'interactive', 'verifying')"
ACTIVE_0009 = "('queued', 'leased', 'running', 'interactive', 'verifying', 'review')"

ONE_LINE = r"^[^\\x00-\\x1f\\x7f]{1,200}$"  # a jsonpath regex: 1 to 200 characters, no control character
# An element of runs.repos that is not {"repo": <one line>, "branch": <one line or null>, ...}.
BAD_REPO = (
    rf'lax $[*] ? (!(@.type() == "object") || !(@.repo.type() == "string") || !(@.repo like_regex "{ONE_LINE}") '
    r'|| (exists(@.branch) && !(@.branch.type() == "null" '
    rf'|| (@.branch.type() == "string" && @.branch like_regex "{ONE_LINE}"))))'
)
# An option of a decision that is not {"key", "label", "description"?, "recommended"?} of the right types.
BAD_OPTION = (
    rf'lax $[*] ? (!(@.type() == "object") || !(@.key.type() == "string") || !(@.key like_regex "{OPTION_KEY}") '
    rf'|| !(@.label.type() == "string") || !(@.label like_regex "{ONE_LINE}") '
    r'|| (exists(@.description) && !(@.description.type() == "string" || @.description.type() == "null")) '
    r'|| (exists(@.recommended) && !(@.recommended.type() == "boolean")))'
)


def _line(column: str, limit: int) -> str:
    """1 to ``limit`` characters of ``column``, without a control character."""
    return rf"char_length({column}) BETWEEN 1 AND {limit} AND {column} !~ '[\x01-\x1f\x7f]'"


UPGRADE = (
    f"""
    ALTER TABLE runs
        ADD COLUMN kind text NOT NULL DEFAULT 'step' CHECK (kind IN {KINDS}),
        ADD COLUMN repos jsonb CHECK (jsonb_typeof(repos) = 'array'
                                      AND jsonb_array_length(repos) BETWEEN 1 AND {MAX_REPOS}
                                      AND NOT jsonb_path_exists(repos, '{BAD_REPO}')),
        ADD COLUMN model text CHECK ({_line("model", 200)}),
        ADD COLUMN run_seconds integer NOT NULL DEFAULT 0 CHECK (run_seconds >= 0),
        ADD COLUMN waiting_since timestamptz,
        ADD COLUMN parked_at timestamptz,
        ADD COLUMN resume_of_run_id bigint REFERENCES runs (id) ON DELETE RESTRICT,
        ALTER COLUMN step_key DROP NOT NULL,
        ALTER COLUMN repo DROP NOT NULL,
        DROP CONSTRAINT runs_state_check,
        ADD CONSTRAINT runs_state_check CHECK (state IN {STATES}),
        DROP CONSTRAINT runs_timeout_s_check,
        ADD CONSTRAINT runs_timeout_s_check CHECK (
            timeout_s BETWEEN {TIMEOUT_SECONDS[0]}
                          AND CASE kind WHEN 'plan' THEN {PLAN_TIMEOUT_SECONDS} ELSE {TIMEOUT_SECONDS[1]} END),
        DROP CONSTRAINT runs_check6,
        ADD CONSTRAINT runs_held_lease_check CHECK (state NOT IN {HELD} OR lease_expires_at IS NOT NULL),
        ADD CONSTRAINT runs_kind_step_key_check CHECK ((kind = 'plan') = (step_key IS NULL)),
        ADD CONSTRAINT runs_kind_repo_check CHECK ((kind = 'plan') = (repo IS NULL)),
        ADD CONSTRAINT runs_kind_repos_check CHECK ((kind = 'plan') = (repos IS NOT NULL)),
        ADD CONSTRAINT runs_waiting_check CHECK (state <> 'waiting' OR waiting_since IS NOT NULL),
        ADD CONSTRAINT runs_parked_check CHECK (state <> 'parked' OR parked_at IS NOT NULL),
        ADD CONSTRAINT runs_resume_check CHECK (resume_of_run_id IS NULL OR (kind = 'plan' AND resume_of_run_id <> id))
    """,
    "DROP INDEX runs_active_step_key",
    f"CREATE UNIQUE INDEX runs_active_step_key ON runs (project_id, plan_id, step_key) "
    f"WHERE kind = 'step' AND state IN {ACTIVE}",
    f"CREATE UNIQUE INDEX runs_active_plan_key ON runs (project_id, plan_id) WHERE kind = 'plan' AND state IN {ACTIVE}",
    "DROP INDEX runs_lease_idx",
    f"CREATE INDEX runs_lease_idx ON runs (lease_expires_at) WHERE state IN {HELD}",  # the reaper's scan
    "CREATE INDEX runs_waiting_idx ON runs (waiting_since) WHERE state = 'waiting'",  # to park after a day
    "CREATE INDEX runs_parked_idx ON runs (parked_at) WHERE state = 'parked'",  # to cancel after a week
    f"""
    CREATE TABLE decisions (
        id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        run_id bigint NOT NULL REFERENCES runs (id) ON DELETE CASCADE,
        project_id bigint NOT NULL REFERENCES projects (id) ON DELETE RESTRICT,
        plan_id text NOT NULL,
        step_key text CHECK ({_line("step_key", 200)}),
        category text NOT NULL CHECK (category IN {CATEGORIES}),
        question text NOT NULL CHECK (char_length(question) BETWEEN 1 AND {MAX_QUESTION_CHARS}),
        context text CHECK (octet_length(context) BETWEEN 1 AND {MAX_CONTEXT_BYTES}),
        options jsonb NOT NULL CHECK (jsonb_typeof(options) = 'array'
                                      AND jsonb_array_length(options) BETWEEN {OPTIONS[0]} AND {OPTIONS[1]}
                                      AND octet_length(CAST(options AS text)) <= {MAX_OPTIONS_BYTES}
                                      AND NOT jsonb_path_exists(options, '{BAD_OPTION}')
                                      AND jsonb_array_length(
                                          jsonb_path_query_array(options, '$[*] ? (@.recommended == true)')) <= 1),
        state text NOT NULL DEFAULT 'open' CHECK (state IN {DECISION_STATES}),
        answer_option text CHECK (answer_option IS NULL  -- else the key of one of the options
                                  OR (answer_option ~ '{OPTION_KEY}'
                                      AND options @> jsonb_build_array(jsonb_build_object('key', answer_option)))),
        answer_text text CHECK (octet_length(answer_text) BETWEEN 1 AND {MAX_ANSWER_BYTES}),
        answered_by bigint REFERENCES users (id) ON DELETE RESTRICT,
        asked_at timestamptz NOT NULL DEFAULT now(),
        answered_at timestamptz,
        delivered_at timestamptz,  -- when the worker handed the answer to the agent
        CHECK ((state = 'answered') = (answered_at IS NOT NULL)),
        CHECK ((state = 'answered') = (answered_by IS NOT NULL)),
        CHECK ((state = 'answered') = (answer_option IS NOT NULL OR answer_text IS NOT NULL)),
        CHECK (answered_at >= asked_at),
        CHECK (delivered_at IS NULL OR (answered_at IS NOT NULL AND delivered_at >= answered_at))
    )
    """,
    "CREATE INDEX decisions_project_idx ON decisions (project_id, id DESC)",
    "CREATE INDEX decisions_run_idx ON decisions (run_id, id)",
    "CREATE INDEX decisions_open_idx ON decisions (run_id) WHERE state = 'open'",
    f"""
    CREATE TABLE notifications (
        id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        user_id bigint NOT NULL REFERENCES users (id) ON DELETE CASCADE,
        kind text NOT NULL CHECK (kind IN {NOTIFICATION_KINDS}),
        notice_kind text CHECK (notice_kind IN {NOTICE_KINDS}),
        project_id bigint REFERENCES projects (id) ON DELETE CASCADE,
        run_id bigint REFERENCES runs (id) ON DELETE CASCADE,
        decision_id bigint REFERENCES decisions (id) ON DELETE CASCADE,
        title text NOT NULL CHECK ({_line("title", 200)}),
        body text CHECK (octet_length(body) BETWEEN 1 AND {MAX_BODY_BYTES}),
        details jsonb CHECK (jsonb_typeof(details) = 'object'),
        link text CHECK (char_length(link) <= 2000 AND link ~ '^/([^/]|$)'),  -- a path of the hub's own site
        created_at timestamptz NOT NULL DEFAULT now(),
        read_at timestamptz CHECK (read_at >= created_at),
        CHECK ((kind = 'decision') = (decision_id IS NOT NULL)),
        CHECK ((kind = 'notice') = (notice_kind IS NOT NULL))
    )
    """,
    "CREATE INDEX notifications_user_idx ON notifications (user_id, id DESC)",
    "CREATE INDEX notifications_unread_idx ON notifications (user_id) WHERE read_at IS NULL",  # the bell's count
    "CREATE UNIQUE INDEX notifications_decision_key ON notifications (decision_id, user_id) "
    "WHERE decision_id IS NOT NULL",
    """
    CREATE TABLE notification_channels (
        id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        user_id bigint NOT NULL REFERENCES users (id) ON DELETE CASCADE,
        kind text NOT NULL CHECK (kind ~ '^[a-z][a-z0-9_-]{0,31}$' AND kind <> 'web'),
        config jsonb NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(config) = 'object'),
        enabled boolean NOT NULL DEFAULT true,
        created_at timestamptz NOT NULL DEFAULT now(),
        UNIQUE (user_id, kind)
    )
    """,
    f"""
    CREATE TABLE notification_deliveries (
        id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        notification_id bigint NOT NULL REFERENCES notifications (id) ON DELETE CASCADE,
        channel_id bigint REFERENCES notification_channels (id) ON DELETE CASCADE,  -- NULL: the web
        state text NOT NULL DEFAULT 'pending' CHECK (state IN {DELIVERY_STATES}),
        attempts smallint NOT NULL DEFAULT 0 CHECK (attempts BETWEEN 0 AND {MAX_DELIVERY_ATTEMPTS}),
        next_at timestamptz NOT NULL DEFAULT now(),
        last_error text CHECK (char_length(last_error) BETWEEN 1 AND 2000),
        created_at timestamptz NOT NULL DEFAULT now(),
        delivered_at timestamptz,
        CHECK ((state = 'delivered') = (delivered_at IS NOT NULL)),
        CHECK (state <> 'failed' OR last_error IS NOT NULL)
    )
    """,
    "CREATE UNIQUE INDEX notification_deliveries_channel_key ON notification_deliveries (notification_id, channel_id)",
    "CREATE UNIQUE INDEX notification_deliveries_web_key ON notification_deliveries (notification_id) "
    "WHERE channel_id IS NULL",
    # the job takes the deliveries that are due
    "CREATE INDEX notification_deliveries_due_idx ON notification_deliveries (next_at) WHERE state = 'pending'",
)

DOWNGRADE = (
    "DROP TABLE notification_deliveries, notification_channels, notifications, decisions",
    "DELETE FROM runs WHERE kind = 'plan'",
    "UPDATE runs SET state = 'running' WHERE state = 'waiting'",
    "UPDATE runs SET state = 'cancelled', finished_at = now(), lease_expires_at = NULL WHERE state = 'parked'",
    "DROP INDEX runs_active_plan_key, runs_waiting_idx, runs_parked_idx, runs_active_step_key, runs_lease_idx",
    f"CREATE UNIQUE INDEX runs_active_step_key ON runs (project_id, plan_id, step_key) WHERE state IN {ACTIVE_0009}",
    f"CREATE INDEX runs_lease_idx ON runs (lease_expires_at) WHERE state IN {HELD_0009}",
    f"""
    ALTER TABLE runs
        DROP CONSTRAINT runs_state_check,
        ADD CONSTRAINT runs_state_check CHECK (state IN {STATES_0009}),
        DROP CONSTRAINT runs_held_lease_check,
        ADD CONSTRAINT runs_check6 CHECK (state NOT IN {HELD_0009} OR lease_expires_at IS NOT NULL),
        DROP CONSTRAINT runs_timeout_s_check,
        ADD CONSTRAINT runs_timeout_s_check CHECK (timeout_s BETWEEN {TIMEOUT_SECONDS[0]} AND {TIMEOUT_SECONDS[1]}),
        DROP COLUMN kind,
        DROP COLUMN repos,
        DROP COLUMN model,
        DROP COLUMN run_seconds,
        DROP COLUMN waiting_since,
        DROP COLUMN parked_at,
        DROP COLUMN resume_of_run_id,
        ALTER COLUMN step_key SET NOT NULL,
        ALTER COLUMN repo SET NOT NULL
    """,
)


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(statement)
