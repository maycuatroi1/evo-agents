"""Secrets of kind forge: a GitLab token of a project that only the hub's own process opens.

Revision ID: 0067
Revises: 0066
Create Date: 2026-10-21

The names below are copies of ``evo_agents.hub.credentials`` as of this revision, never imports, so a later change to
them takes a new revision. Every statement is an Alembic operation on SQLAlchemy Core expressions.

secrets.kind takes ``forge`` (plan gitlab-merge-queue, criterion a3): the hub reads, rebases and merges the merge
requests of the GitLab projects under its url_prefix with it, and no run is ever leased it. Like a git secret it has a
url_prefix; unlike one it has no username, as GitLab reads the token from the PRIVATE-TOKEN header, and it always has
an end (expires_at). The table takes no column: the checks secrets_kind_check and secrets_check1 of 0011 widen, and
secrets_forge_expires_check is new.

Going back deletes the forge secrets with their bindings (no lease names one) and puts the checks of 0011 back.
"""

import sqlalchemy as sa
from alembic import op

revision = "0067"
down_revision = "0066"
branch_labels = None
depends_on = None

KINDS_0011 = ("env", "git")
FORGE = "forge"
KINDS = (*KINDS_0011, FORGE)  # evo_agents.hub.credentials.SECRET_KINDS
KIND_CHECK = "secrets_kind_check"  # kind is one of the kinds
URL_PREFIX_CHECK = "secrets_check1"  # (kind = 'git') = (url_prefix IS NOT NULL), as Postgres named it in 0011
FORGE_EXPIRES_CHECK = "secrets_forge_expires_check"

col = sa.column


def _kind_checks(kinds, url_prefix_kinds) -> None:
    """secrets_kind_check and secrets_check1 again: kind one of ``kinds``, and a url_prefix for ``url_prefix_kinds``
    alone."""
    op.drop_constraint(KIND_CHECK, "secrets", type_="check")
    op.drop_constraint(URL_PREFIX_CHECK, "secrets", type_="check")
    op.create_check_constraint(KIND_CHECK, "secrets", col("kind").in_(kinds))
    if len(url_prefix_kinds) == 1:
        has_prefix = col("kind") == url_prefix_kinds[0]
    else:
        has_prefix = col("kind").in_(url_prefix_kinds)
    op.create_check_constraint(URL_PREFIX_CHECK, "secrets", has_prefix == col("url_prefix").is_not(None))


def upgrade() -> None:
    _kind_checks(KINDS, ("git", FORGE))
    op.create_check_constraint(
        FORGE_EXPIRES_CHECK, "secrets", sa.or_(col("kind") != FORGE, col("expires_at").is_not(None))
    )


def downgrade() -> None:
    secrets = sa.table("secrets", col("kind"))
    op.execute(secrets.delete().where(secrets.c.kind == FORGE))  # their bindings go with them (ON DELETE CASCADE)
    op.drop_constraint(FORGE_EXPIRES_CHECK, "secrets", type_="check")
    _kind_checks(KINDS_0011, ("git",))
