"""The table of browser identities (0117), which ``evo_agents.hub.tables`` adds to its metadata and exposes as its own
attribute (``tables.browser_identities``): queries name it there.

A browser identity is a Chromium profile kept logged in to some sites, which runs of a project use on the one worker
that holds it. A member owns it (owner_kind member, owner_id that member), the project does (owner_kind project,
owner_id null), or an agent profile does (owner_kind agent, profile_id that profile, owner_id null); its name is
unique among the live identities of its owner in the project. domains are the sites whose cookies it keeps, worker_id
the worker its profile opens on, status active, needs_login or disabled, and held_by_run the run that holds it now, if
any.

version counts the profiles saved, 0 before the first. The current one is the sealed blob sha256 (size bytes, kind
browser-profile) with its data key sealed under EVO_HUB_SECRETS_KEY (sealed_key, key_nonce, key_id), and the
Chromium that wrote it; the one before it, version - 1, is the previous_* columns, and older ones leave the bucket
(``evo_agents.hub.server.browser.service.profiles``). An identity deleted keeps its row, without its keys or blobs.
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    Identity,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    PrimaryKeyConstraint,
    Table,
    Text,
)
from sqlalchemy.dialects.postgresql import ARRAY

from evo_agents.hub.columns import fkey, stamp, when


def _version(prefix: str) -> list[Column]:
    """The columns of one saved version: the blob and its sealed data key."""
    return [
        Column(f"{prefix}sha256", Text),  # the sealed blob, kind browser-profile
        Column(f"{prefix}size", BigInteger),  # its bytes
        Column(f"{prefix}sealed_key", LargeBinary),  # its data key, sealed for browser-identity:{id}:{version}
        Column(f"{prefix}key_nonce", LargeBinary),
        Column(f"{prefix}key_id", Text),
    ]


def browser_tables(metadata: MetaData) -> Table:
    """browser_identities, added to ``metadata``."""
    identities = Table(
        "browser_identities",
        metadata,
        Column("id", BigInteger, Identity(always=True), nullable=False),
        Column("project_id", BigInteger, nullable=False),
        Column("owner_kind", Text, nullable=False),  # member, project or agent
        Column("owner_id", BigInteger),  # the member; null for the project's and an agent's
        Column("profile_id", BigInteger),  # the agent profile that owns an agent's identity (0122)
        Column(
            "profile_default", Boolean, nullable=False, server_default="false"
        ),  # the one its profile hands its runs
        Column("name", Text, nullable=False),
        Column("domains", ARRAY(Text), nullable=False),
        Column("worker_id", BigInteger, nullable=False),
        Column("login_url", Text),  # the page a login session opens; null: the home page of the first domain (0120)
        Column("status", Text, nullable=False, server_default="active"),
        Column("version", Integer, nullable=False, server_default="0"),
        Column("held_by_run", BigInteger),
        *_version(""),
        Column("chromium_version", Text),  # the Chromium that saved the current version
        when("last_saved_at"),
        Column("last_saved_by", BigInteger),
        *_version("previous_"),  # version - 1, kept to fall back on
        when("last_used_at"),
        Column("created_by", BigInteger),
        stamp("created_at"),
        stamp("updated_at"),
        when("deleted_at"),
        fkey("browser_identities", "project_id", "projects", ondelete="RESTRICT"),
        fkey("browser_identities", "owner_id", "users", ondelete="RESTRICT"),
        fkey("browser_identities", "worker_id", "workers", ondelete="RESTRICT"),
        fkey("browser_identities", "held_by_run", "runs", ondelete="SET NULL"),
        fkey("browser_identities", "profile_id", "agent_profiles", ondelete="RESTRICT"),
        fkey("browser_identities", "last_saved_by", "users", ondelete="SET NULL"),
        fkey("browser_identities", "created_by", "users", ondelete="SET NULL"),
        PrimaryKeyConstraint("id", name="browser_identities_pkey"),
        Index("browser_identities_worker_idx", "worker_id"),
        Index("browser_identities_held_by_run_idx", "held_by_run"),
    )
    live = identities.c.deleted_at.is_(None)
    Index(  # a member's identity: unique by name among that member's live ones in the project
        "browser_identities_member_name_key",
        identities.c.project_id,
        identities.c.owner_id,
        identities.c.name,
        unique=True,
        postgresql_where=(identities.c.owner_kind == "member") & live,
    )
    Index(  # a project's identity: unique by name among the project's live ones
        "browser_identities_project_name_key",
        identities.c.project_id,
        identities.c.name,
        unique=True,
        postgresql_where=(identities.c.owner_kind == "project") & live,
    )
    Index(  # an agent's identity: unique by name among its profile's live ones (0122)
        "browser_identities_profile_name_key",
        identities.c.profile_id,
        identities.c.name,
        unique=True,
        postgresql_where=(identities.c.owner_kind == "agent") & live,
    )
    Index(  # the identity a profile hands its runs: at most one live one per profile (0122)
        "browser_identities_profile_default_key",
        identities.c.profile_id,
        unique=True,
        postgresql_where=identities.c.profile_default & live,
    )
    return identities
