"""Bounds on memories, so the table holds only what a memory directory of Claude Code can: a file name, a location
that is a repo, ``harness`` or a personal slug, and at most 256 KiB of text.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-04

The API checks the same rules first (``evo_agents.hub.memory``), so a request that breaks one gets a 422 naming it;
these constraints keep every other way in to the same rules. A memory's name is a file name ending in .md, without a
slash, a backslash or a control character, never MEMORY.md (the index of the directory, which is never pushed) and
not hidden. A personal location is what is left of a slug, ``[~A-Za-z0-9-][A-Za-z0-9-]*``. A body, current or
past, is at most 256 KiB of UTF-8, and a tombstone keeps none.

The statements go through SQLAlchemy's text(): no percent signs, and no colon directly before a word.
"""

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

MAX_BODY = 256 * 1024  # bytes, as evo_agents.hub.memory.MAX_BODY
NO_SEPARATORS = r"'[/\\\x01-\x1f\x7f]'"  # a slash, a backslash or a control character

UPGRADE = (
    f"""
    ALTER TABLE memories
        ADD CONSTRAINT memories_name_shape CHECK (
            octet_length(name) <= 255 AND name ~ '[.]md$' AND name !~ '^[.]' AND name !~ {NO_SEPARATORS}
            AND lower(name) <> 'memory.md'),
        ADD CONSTRAINT memories_location_shape CHECK (
            octet_length(location) <= 255 AND location !~ {NO_SEPARATORS}
            AND (scope = 'project' OR location ~ '^[~A-Za-z0-9-][A-Za-z0-9-]*$')),
        ADD CONSTRAINT memories_body_size CHECK (octet_length(body) <= {MAX_BODY}),
        ADD CONSTRAINT memories_tombstone_empty CHECK (NOT deleted OR body = '')
    """,
    f"ALTER TABLE memory_revisions ADD CONSTRAINT memory_revisions_body_size CHECK (octet_length(body) <= {MAX_BODY})",
)

DOWNGRADE = (
    "ALTER TABLE memory_revisions DROP CONSTRAINT memory_revisions_body_size",
    "ALTER TABLE memories DROP CONSTRAINT memories_tombstone_empty, DROP CONSTRAINT memories_body_size, "
    "DROP CONSTRAINT memories_location_shape, DROP CONSTRAINT memories_name_shape",
)


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(statement)
