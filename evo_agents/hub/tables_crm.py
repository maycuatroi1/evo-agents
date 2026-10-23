"""The tables of the core plugin crm (0118), which ``evo_agents.hub.tables`` adds to its metadata and exposes as its own
attributes (``tables.crm_contacts``, ``tables.crm_touchpoints``, ``tables.crm_stage_history``, ``tables.crm_cursors``,
``tables.crm_deletions``): the contacts of a project, people who found it themselves (``docs/crm-plugin.md``).

crm_contacts holds one row for each GitHub login (lower case) in a project: the public profile link, the source the
contact was first seen from, its stage (lead, invited, activated or retained, which only moves forward), when it was
first seen and when the stage last moved, and the label of the row (the project's default label when it was made). No
email address and nothing read from another service is kept.

crm_touchpoints holds what the contact did, one row for each kind and reference (a note is the one kind a person
writes): the kind (invite_request, invite_link, grant, issue, pull_request, discussion, note), the reference that makes
it one row (an issue number, an invite id), a link to it when it has one, when it happened, and the writer of a note.
It goes with the contact: deleting a contact deletes its touchpoints.

crm_stage_history holds each stage a contact reached and when it reached it, deleted with the contact.

crm_cursors holds how far the intake of a source has read a project (``since``), so a contact that was deleted is not
made again from what was read before.

crm_deletions holds one row for each contact deleted on request: the number the contact had, how many touchpoints went
with it, who deleted it and when. It holds no login, no link and no text of the contact.
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    ForeignKeyConstraint,
    Identity,
    Index,
    Integer,
    MetaData,
    PrimaryKeyConstraint,
    Table,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB

STAGES = ("lead", "invited", "activated", "retained")  # in the order a contact moves through them
SOURCES = ("invite_request", "invite_link", "grant", "issue", "pull_request", "discussion")
KINDS = (*SOURCES, "note")  # crm_touchpoints.kind
MAX_NOTE_CHARS = 1000
LOGIN = r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$"  # a GitHub login, which crm_contacts.login holds in lower case


def _stamp(name: str) -> Column:
    return Column(name, DateTime(timezone=True), nullable=False, server_default=func.now())


def _key(table: str, column: str, target: str, ondelete: str) -> ForeignKeyConstraint:
    return ForeignKeyConstraint([column], [f"{target}.id"], ondelete=ondelete, name=f"{table}_{column}_fkey")


def contacts_table(metadata: MetaData) -> Table:
    contacts = Table(
        "crm_contacts",
        metadata,
        Column("id", BigInteger, Identity(always=True), nullable=False),
        Column("project_id", BigInteger, nullable=False),
        Column("login", Text, nullable=False),
        Column("profile_url", Text, nullable=False),
        Column("source", Text, nullable=False),
        Column("stage", Text, nullable=False, server_default="lead"),
        Column("label", JSONB, nullable=False, server_default="{}"),
        _stamp("first_seen_at"),
        _stamp("stage_at"),
        _stamp("created_at"),
        _key("crm_contacts", "project_id", "projects", ondelete="CASCADE"),
        PrimaryKeyConstraint("id", name="crm_contacts_pkey"),
        UniqueConstraint("project_id", "login", name="crm_contacts_login_key"),
        Index("crm_contacts_stage_idx", "project_id", "stage"),
    )
    return contacts


def touchpoints_table(metadata: MetaData) -> Table:
    return Table(
        "crm_touchpoints",
        metadata,
        Column("id", BigInteger, Identity(always=True), nullable=False),
        Column("contact_id", BigInteger, nullable=False),
        Column("kind", Text, nullable=False),
        Column("ref", Text, nullable=False, server_default=""),
        Column("url", Text),
        Column("note", Text),
        Column("author_id", BigInteger),
        Column("happened_at", DateTime(timezone=True), nullable=False),
        _stamp("created_at"),
        _key("crm_touchpoints", "contact_id", "crm_contacts", ondelete="CASCADE"),
        _key("crm_touchpoints", "author_id", "users", ondelete="SET NULL"),
        PrimaryKeyConstraint("id", name="crm_touchpoints_pkey"),
        UniqueConstraint("contact_id", "kind", "ref", name="crm_touchpoints_once_key"),
        Index("crm_touchpoints_author_idx", "author_id"),
    )


def history_table(metadata: MetaData) -> Table:
    return Table(
        "crm_stage_history",
        metadata,
        Column("id", BigInteger, Identity(always=True), nullable=False),
        Column("contact_id", BigInteger, nullable=False),
        Column("stage", Text, nullable=False),
        Column("reached_at", DateTime(timezone=True), nullable=False),
        _stamp("created_at"),
        _key("crm_stage_history", "contact_id", "crm_contacts", ondelete="CASCADE"),
        PrimaryKeyConstraint("id", name="crm_stage_history_pkey"),
        UniqueConstraint("contact_id", "stage", name="crm_stage_history_once_key"),
    )


def cursors_table(metadata: MetaData) -> Table:
    return Table(
        "crm_cursors",
        metadata,
        Column("project_id", BigInteger, nullable=False),
        Column("source", Text, nullable=False),
        Column("since", DateTime(timezone=True), nullable=False),
        _stamp("updated_at"),
        _key("crm_cursors", "project_id", "projects", ondelete="CASCADE"),
        PrimaryKeyConstraint("project_id", "source", name="crm_cursors_pkey"),
    )


def deletions_table(metadata: MetaData) -> Table:
    return Table(
        "crm_deletions",
        metadata,
        Column("id", BigInteger, Identity(always=True), nullable=False),
        Column("project_id", BigInteger, nullable=False),
        Column("contact_id", BigInteger, nullable=False),
        Column("touchpoints", Integer, nullable=False),
        Column("deleted_by", BigInteger),
        _stamp("deleted_at"),
        _key("crm_deletions", "project_id", "projects", ondelete="CASCADE"),
        _key("crm_deletions", "deleted_by", "users", ondelete="SET NULL"),
        PrimaryKeyConstraint("id", name="crm_deletions_pkey"),
        Index("crm_deletions_project_idx", "project_id"),
    )


def invites_sent_table(metadata: MetaData) -> Table:
    """Each invite link @community made for an invite request (0121): once per request, counted by the weekly cap."""
    return Table(
        "crm_invites_sent",
        metadata,
        Column("id", BigInteger, Identity(always=True), nullable=False),
        Column("project_id", BigInteger, nullable=False),
        Column("ref", Text, nullable=False),
        Column("login", Text, nullable=False),
        Column("invite_id", BigInteger),
        Column("run_id", BigInteger),
        _stamp("sent_at"),
        _key("crm_invites_sent", "project_id", "projects", ondelete="CASCADE"),
        _key("crm_invites_sent", "invite_id", "invites", ondelete="SET NULL"),
        _key("crm_invites_sent", "run_id", "runs", ondelete="SET NULL"),
        PrimaryKeyConstraint("id", name="crm_invites_sent_pkey"),
        UniqueConstraint("project_id", "ref", name="crm_invites_sent_ref_key"),
        Index("crm_invites_sent_week_idx", "project_id", "sent_at"),
    )


def crm_tables(metadata: MetaData) -> tuple[Table, Table, Table, Table, Table, Table]:
    """(crm_contacts, crm_touchpoints, crm_stage_history, crm_cursors, crm_deletions, crm_invites_sent), added to
    ``metadata``."""
    return (
        contacts_table(metadata),
        touchpoints_table(metadata),
        history_table(metadata),
        cursors_table(metadata),
        deletions_table(metadata),
        invites_sent_table(metadata),
    )
