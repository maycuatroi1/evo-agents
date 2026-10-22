"""The invite links of the hub (0104), which ``evo_agents.hub.tables`` adds to its metadata as ``tables.invites``.

A hub admin makes an invite link (``evo_agents.hub.server.invites``); a signed-in member accepts it once. Kind ``join``
names a project, a role and a max level, and accepting it gives that grant. Kind ``register`` names none: accepting
it gives the right to register one new project, which the registration that uses it marks with used_at and the new
project's id. for_login, when set, is the one GitHub login that may accept the link. token_hash is the hex SHA-256
of the link's token, which the hub never keeps. A link is open until accepted_at, revoked_at or expires_at; its
status is read from those three. The CHECKs on kind and on the fields each kind fills are the migration's alone.
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    ForeignKeyConstraint,
    Identity,
    Index,
    MetaData,
    PrimaryKeyConstraint,
    Table,
    Text,
    UniqueConstraint,
    func,
)

KINDS = ("join", "register")


def _when(name: str) -> Column:
    return Column(name, DateTime(timezone=True))


def invite_tables(metadata: MetaData) -> Table:
    """invites (0104), added to ``metadata``."""
    return Table(
        "invites",
        metadata,
        Column("id", BigInteger, Identity(always=True), nullable=False),
        Column("kind", Text, nullable=False),
        Column("project_id", BigInteger),  # join: the project; register: the project its right registered
        Column("role", Text),  # join only
        Column("max_level", Text),  # join only
        Column("for_login", Text),  # the one login that may accept it; NULL for anyone signed in
        Column("token_hash", Text, nullable=False),
        Column("created_by", BigInteger, nullable=False),
        Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
        Column("expires_at", DateTime(timezone=True), nullable=False),
        _when("accepted_at"),
        Column("accepted_by", BigInteger),
        _when("revoked_at"),
        Column("revoked_by", BigInteger),
        _when("used_at"),  # register: when its right registered a project
        ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE", name="invites_project_id_fkey"),
        ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="RESTRICT", name="invites_created_by_fkey"),
        ForeignKeyConstraint(["accepted_by"], ["users.id"], ondelete="RESTRICT", name="invites_accepted_by_fkey"),
        ForeignKeyConstraint(["revoked_by"], ["users.id"], ondelete="RESTRICT", name="invites_revoked_by_fkey"),
        PrimaryKeyConstraint("id", name="invites_pkey"),
        UniqueConstraint("token_hash", name="invites_token_hash_key"),
        Index("invites_accepted_by_idx", "accepted_by"),
    )
