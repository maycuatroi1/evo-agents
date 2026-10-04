"""Initial hub schema: access, projects, memories, plans, skills, knowledge graph ingests and audit.

Revision ID: 0001
Revises:
Create Date: 2026-10-04

Access: users (GitHub logins), tokens (machine tokens and web sessions, kept only as a hex SHA-256), projects
with the repos and sinks registered from their knowledge.yaml, and grants (a role and a maximum level per
user and project). Content: memories and plans with integer revisions and a history that is only appended
to, skills whose versions point at a bundle in the blob store (metadata and key only, never the bytes), and
kg_ingests for the run logs pushed to a project. audit says who did what to which target, never the content.

A label is jsonb ``{level, location, integrity, projects}`` by the names the project declares. Deletion
rules: rows that only describe their parent (a project's repos, sinks and grants, a user's tokens, an
object's revisions or versions) go with it; content RESTRICTs the deletion of its project, so removing a
project is a deliberate purge; actor columns and audit RESTRICT the deletion of a user or token, so history
keeps its author. Revisions, skill versions and audit rows are never updated.

The statements go through SQLAlchemy's text(): no percent signs, and no colon directly before a word.
"""

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

HEX_SHA256 = "'^[0-9a-f]{64}$'"
PREFIXED_SHA256 = "'^sha256:[0-9a-f]{64}$'"  # the kg/1 form of a digest
MEMORY_TYPES = "('user', 'feedback', 'project', 'reference')"
LABEL = "jsonb NOT NULL CHECK (jsonb_typeof(label) = 'object')"
BLOB_LIMIT = 10 * 1024 * 1024  # a skill bundle is at most 10 MiB

UPGRADE = (
    """
    CREATE TABLE users (
        id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        login text NOT NULL CHECK (login ~ '^[A-Za-z0-9][A-Za-z0-9_-]*$' AND char_length(login) <= 100),
        github_id bigint UNIQUE CHECK (github_id > 0),  -- NULL for a login granted access before signing in
        created_at timestamptz NOT NULL DEFAULT now(),
        last_seen_at timestamptz
    )
    """,
    "CREATE UNIQUE INDEX users_login_key ON users (lower(login))",  # GitHub logins are case-insensitive
    f"""
    CREATE TABLE tokens (
        id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        user_id bigint NOT NULL REFERENCES users (id) ON DELETE CASCADE,
        kind text NOT NULL CHECK (kind IN ('machine', 'web')),  -- an evh_ token of one machine, or a web session
        token_hash text NOT NULL UNIQUE CHECK (token_hash ~ {HEX_SHA256}),
        host text CHECK (char_length(host) BETWEEN 1 AND 255),
        created_at timestamptz NOT NULL DEFAULT now(),
        last_used_at timestamptz,
        expires_at timestamptz NOT NULL,
        revoked_at timestamptz,
        CHECK (kind = 'web' OR host IS NOT NULL),
        CHECK (expires_at > created_at)
    )
    """,
    "CREATE INDEX tokens_user_id_idx ON tokens (user_id)",
    """
    CREATE TABLE projects (
        id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        name text NOT NULL UNIQUE CHECK (name ~ '^[a-z0-9][a-z0-9-]*$' AND char_length(name) <= 100),
        levels text[] NOT NULL CHECK (cardinality(levels) >= 1),  -- lowest first, as in knowledge.yaml
        locations text[] NOT NULL CHECK (cardinality(locations) >= 1),
        default_label jsonb NOT NULL CHECK (jsonb_typeof(default_label) = 'object'),  -- of the harness source
        created_by bigint NOT NULL REFERENCES users (id) ON DELETE RESTRICT,
        created_at timestamptz NOT NULL DEFAULT now(),
        updated_at timestamptz NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE project_repos (
        project_id bigint NOT NULL REFERENCES projects (id) ON DELETE CASCADE,
        name text NOT NULL CHECK (name <> ''),
        origin text,
        default_branch text,
        PRIMARY KEY (project_id, name)
    )
    """,
    """
    CREATE TABLE project_sinks (
        project_id bigint NOT NULL REFERENCES projects (id) ON DELETE CASCADE,
        sink_id text NOT NULL CHECK (sink_id <> ''),
        kind text NOT NULL CHECK (kind IN ('agent-session', 'llm-backend', 'writeback-target', 'cli', 'hub')),
        clearance jsonb NOT NULL CHECK (jsonb_typeof(clearance -> 'level') = 'string'),
        PRIMARY KEY (project_id, sink_id)
    )
    """,
    # The hub sink's clearance bounds what may be pushed to the project, so a project has at most one.
    "CREATE UNIQUE INDEX project_sinks_one_hub_key ON project_sinks (project_id) WHERE kind = 'hub'",
    """
    CREATE TABLE grants (
        user_id bigint NOT NULL REFERENCES users (id) ON DELETE CASCADE,
        project_id bigint NOT NULL REFERENCES projects (id) ON DELETE CASCADE,
        role text NOT NULL CHECK (role IN ('reader', 'writer', 'admin')),
        max_level text NOT NULL CHECK (max_level <> ''),  -- one of the levels of the project
        granted_by bigint NOT NULL REFERENCES users (id) ON DELETE RESTRICT,
        granted_at timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (user_id, project_id)
    )
    """,
    "CREATE INDEX grants_project_id_idx ON grants (project_id)",
    f"""
    CREATE TABLE memories (
        id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        scope text NOT NULL CHECK (scope IN ('project', 'personal')),
        project_id bigint REFERENCES projects (id) ON DELETE RESTRICT,
        location text NOT NULL CHECK (location <> ''),  -- a repo name or 'harness'; for personal, the slug rest
        name text NOT NULL CHECK (name <> ''),
        type text NOT NULL CHECK (type IN {MEMORY_TYPES}),
        owner_id bigint NOT NULL REFERENCES users (id) ON DELETE RESTRICT,
        label {LABEL},
        body text NOT NULL,
        revision integer NOT NULL DEFAULT 1 CHECK (revision >= 1),
        deleted boolean NOT NULL DEFAULT false,
        created_at timestamptz NOT NULL DEFAULT now(),
        updated_at timestamptz NOT NULL DEFAULT now(),
        updated_by bigint NOT NULL REFERENCES users (id) ON DELETE RESTRICT,
        search tsvector GENERATED ALWAYS AS (to_tsvector('simple', name || ' ' || body)) STORED,
        CHECK ((scope = 'project') = (project_id IS NOT NULL))
    )
    """,
    # Who sees a memory decides its identity: project and reference memories of a project are shared by its
    # members; user and feedback memories, and every personal memory, are their owner's alone.
    """
    CREATE UNIQUE INDEX memories_shared_key ON memories (project_id, location, name)
        WHERE scope = 'project' AND type IN ('project', 'reference')
    """,
    """
    CREATE UNIQUE INDEX memories_owned_key ON memories (owner_id, project_id, location, name)
        WHERE scope = 'project' AND type IN ('user', 'feedback')
    """,
    "CREATE UNIQUE INDEX memories_personal_key ON memories (owner_id, location, name) WHERE scope = 'personal'",
    "CREATE INDEX memories_search_idx ON memories USING gin (search)",
    "CREATE INDEX memories_updated_idx ON memories (updated_at, id)",  # the cursor of change listings
    f"""
    CREATE TABLE memory_revisions (
        memory_id bigint NOT NULL REFERENCES memories (id) ON DELETE CASCADE,
        revision integer NOT NULL CHECK (revision >= 1),
        type text NOT NULL CHECK (type IN {MEMORY_TYPES}),
        label {LABEL},
        body text NOT NULL,
        deleted boolean NOT NULL,
        actor_id bigint NOT NULL REFERENCES users (id) ON DELETE RESTRICT,
        created_at timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (memory_id, revision)
    )
    """,
    f"""
    CREATE TABLE plans (
        project_id bigint NOT NULL REFERENCES projects (id) ON DELETE RESTRICT,
        plan_id text NOT NULL CHECK (plan_id ~ '^[a-z0-9][a-z0-9-]*$'),  -- the id of plan.schema.json
        area text NOT NULL CHECK (area IN ('active', 'completed')),
        label {LABEL},
        body jsonb NOT NULL CHECK (jsonb_typeof(body) = 'object'),
        revision integer NOT NULL DEFAULT 1 CHECK (revision >= 1),
        digest text NOT NULL CHECK (digest ~ {PREFIXED_SHA256}),
        created_at timestamptz NOT NULL DEFAULT now(),
        updated_at timestamptz NOT NULL DEFAULT now(),
        updated_by bigint NOT NULL REFERENCES users (id) ON DELETE RESTRICT,
        PRIMARY KEY (project_id, plan_id)
    )
    """,
    f"""
    CREATE TABLE plan_revisions (
        project_id bigint NOT NULL,
        plan_id text NOT NULL,
        revision integer NOT NULL CHECK (revision >= 1),
        area text NOT NULL CHECK (area IN ('active', 'completed')),
        label {LABEL},
        body jsonb NOT NULL CHECK (jsonb_typeof(body) = 'object'),
        digest text NOT NULL CHECK (digest ~ {PREFIXED_SHA256}),
        summary text NOT NULL DEFAULT '',
        actor_id bigint NOT NULL REFERENCES users (id) ON DELETE RESTRICT,
        created_at timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (project_id, plan_id, revision),
        FOREIGN KEY (project_id, plan_id) REFERENCES plans (project_id, plan_id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE skills (
        id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        scope text NOT NULL CHECK (scope IN ('global', 'project')),
        project_id bigint REFERENCES projects (id) ON DELETE RESTRICT,
        name text NOT NULL CHECK (name ~ '^[A-Za-z0-9][A-Za-z0-9._-]*$' AND char_length(name) <= 100),
        created_by bigint NOT NULL REFERENCES users (id) ON DELETE RESTRICT,
        created_at timestamptz NOT NULL DEFAULT now(),
        CHECK ((scope = 'project') = (project_id IS NOT NULL))
    )
    """,
    "CREATE UNIQUE INDEX skills_global_key ON skills (name) WHERE scope = 'global'",
    "CREATE UNIQUE INDEX skills_project_key ON skills (project_id, name) WHERE scope = 'project'",
    f"""
    CREATE TABLE skill_versions (
        skill_id bigint NOT NULL REFERENCES skills (id) ON DELETE CASCADE,
        version integer NOT NULL CHECK (version >= 1),
        name text NOT NULL CHECK (name <> ''),  -- name and description from the SKILL.md frontmatter
        description text NOT NULL DEFAULT '',
        sha256 text NOT NULL CHECK (sha256 ~ {HEX_SHA256}),
        size bigint NOT NULL CHECK (size BETWEEN 1 AND {BLOB_LIMIT}),
        r2_key text NOT NULL CHECK (r2_key = 'blobs/sha256/' || sha256),  -- bundles are content-addressed
        source_repo text CHECK (source_repo <> ''),
        source_commit text CHECK (source_commit ~ '^[0-9a-f]{{7,64}}$'),
        published_by bigint NOT NULL REFERENCES users (id) ON DELETE RESTRICT,
        published_at timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (skill_id, version),
        CHECK ((source_repo IS NULL) = (source_commit IS NULL))
    )
    """,
    f"""
    CREATE TABLE kg_ingests (
        project_id bigint NOT NULL REFERENCES projects (id) ON DELETE RESTRICT,
        run_id uuid NOT NULL,  -- the connector run; pushing it again is a no-op
        source text NOT NULL CHECK (source <> ''),
        log_sha256 text NOT NULL CHECK (log_sha256 ~ {HEX_SHA256}),  -- the run log, at blobs/sha256/<hash>
        log_size bigint NOT NULL CHECK (log_size > 0),
        pushed_by bigint NOT NULL REFERENCES users (id) ON DELETE RESTRICT,
        received_at timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (project_id, run_id)
    )
    """,
    """
    CREATE TABLE audit (
        id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        at timestamptz NOT NULL DEFAULT now(),
        actor_id bigint REFERENCES users (id) ON DELETE RESTRICT,  -- NULL for actions of the server itself
        token_id bigint REFERENCES tokens (id) ON DELETE RESTRICT,
        action text NOT NULL CHECK (action ~ '^[a-z][a-z0-9_.-]*$'),
        target text NOT NULL DEFAULT '',
        CHECK (token_id IS NULL OR actor_id IS NOT NULL)
    )
    """,
    "CREATE INDEX audit_at_idx ON audit (at)",
    "CREATE INDEX audit_actor_id_idx ON audit (actor_id, at)",
    """
    CREATE FUNCTION hub_reject_update() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        RAISE EXCEPTION USING ERRCODE = 'restrict_violation', MESSAGE = TG_TABLE_NAME || ' rows are never updated';
    END
    $$
    """,
    *(
        f"CREATE TRIGGER {table}_no_update BEFORE UPDATE ON {table} FOR EACH ROW EXECUTE FUNCTION hub_reject_update()"
        for table in ("memory_revisions", "plan_revisions", "skill_versions", "audit")
    ),
)

DOWNGRADE = (
    "DROP TABLE audit, kg_ingests, skill_versions, skills, plan_revisions, plans, memory_revisions, memories, "
    "grants, project_sinks, project_repos, projects, tokens, users",
    "DROP FUNCTION hub_reject_update()",
)


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(statement)
