"""The tables of app sessions (0262), which ``evo_agents.hub.tables`` adds to its metadata and exposes as its own
attributes (``tables.auth_apps``, ``tables.app_identities``): queries name them there.

auth_apps holds each first-party app a hub admin registered to sign its own users in (``evo_agents.hub.server.
app_sessions``): its name, its secret sealed under EVO_HUB_SECRETS_KEY (``sealing.auth_app_aad``), who registered it and
when, and when it was revoked. A revoked app signs nobody in, and the sessions it opened are revoked with it.

app_identities ties the stable identity an app gives a person (the subject of its enterprise sign-in) to one users row,
whose ``github_id`` stays NULL: the same subject of the same app finds the same member every time, whatever login or
name the app sends. One row an app and subject, which goes with the user or the app.
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
)
from sqlalchemy.dialects.postgresql import JSONB

from evo_agents.hub.columns import stamp


def app_session_tables(metadata: MetaData) -> tuple[Table, Table]:
    """(auth_apps, app_identities), added to ``metadata``."""
    apps = Table(
        "auth_apps",
        metadata,
        Column("id", BigInteger, Identity(always=True), nullable=False),
        Column("name", Text, nullable=False),
        Column("sealed_secret", JSONB, nullable=False),  # Sealer.seal_stored: {ciphertext, nonce, key_id}
        Column("created_by", BigInteger, nullable=False),
        stamp("created_at"),
        Column("revoked_at", DateTime(timezone=True)),
        PrimaryKeyConstraint("id", name="auth_apps_pkey"),
        UniqueConstraint("name", name="auth_apps_name_key"),
        ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="RESTRICT", name="auth_apps_created_by_fkey"),
    )
    identities = Table(
        "app_identities",
        metadata,
        Column("app_id", BigInteger, nullable=False),
        Column("subject", Text, nullable=False),
        Column("user_id", BigInteger, nullable=False),
        stamp("created_at"),
        PrimaryKeyConstraint("app_id", "subject", name="app_identities_pkey"),
        ForeignKeyConstraint(["app_id"], ["auth_apps.id"], ondelete="CASCADE", name="app_identities_app_id_fkey"),
        ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE", name="app_identities_user_id_fkey"),
        Index("app_identities_user_id_idx", "user_id"),
    )
    return apps, identities
