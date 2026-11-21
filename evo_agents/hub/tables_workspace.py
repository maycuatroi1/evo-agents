"""The tables of a project's workspace for first-party apps (0263, 0264), which ``evo_agents.hub.tables`` adds to its
metadata and exposes as its own attributes: queries name them there.

member_tasks holds what a member handed a task run with no agent type (``evo_agents.hub.server.member_tasks``): the
JSON input and the external assets' URIs, keyed by the task's id, the id of its first run. Its retries read the same
row. One row a task, which goes with the run or the project.

project_webhooks holds the outbound webhooks a project's admin set (``evo_agents.hub.server.project_webhooks``): the
https URL, the events it takes, and its signing secret sealed under EVO_HUB_SECRETS_KEY (``sealing.webhook_aad``).
webhook_deliveries is their outbox (``evo_agents.hub.webhook_events``): one row an event and webhook, stored in the
transaction that made the event, delivered at least once and retried with a backoff.

dlp_policies holds each project's data loss prevention policy (``evo_agents.hub.dlp``): its mode, the rule kinds it
applies and the confidential-contract keywords it adds. dlp_counts counts what the policy redacted or blocked, by rule
kind, surface and action: numbers only, never a matched value.
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
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
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB


def _stamp(name: str) -> Column:
    return Column(name, DateTime(timezone=True), nullable=False, server_default=func.now())


def member_task_tables(metadata: MetaData) -> tuple[Table, Table, Table]:
    """(member_tasks, project_webhooks, webhook_deliveries), added to ``metadata`` (0263)."""
    tasks = Table(
        "member_tasks",
        metadata,
        Column("task_id", BigInteger, nullable=False),
        Column("project_id", BigInteger, nullable=False),
        Column("input", JSONB, nullable=False, server_default="{}"),
        Column("assets", JSONB, nullable=False, server_default="[]"),
        _stamp("created_at"),
        PrimaryKeyConstraint("task_id", name="member_tasks_pkey"),
        ForeignKeyConstraint(["task_id"], ["runs.id"], ondelete="CASCADE", name="member_tasks_task_id_fkey"),
        ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE", name="member_tasks_project_id_fkey"),
        Index("member_tasks_project_id_idx", "project_id"),
    )
    hooks = Table(
        "project_webhooks",
        metadata,
        Column("id", BigInteger, Identity(always=True), nullable=False),
        Column("project_id", BigInteger, nullable=False),
        Column("url", Text, nullable=False),
        Column("events", ARRAY(Text), nullable=False),
        Column("sealed_secret", JSONB, nullable=False),  # Sealer.seal_stored: {ciphertext, nonce, key_id}
        Column("active", Boolean, nullable=False, server_default="true"),
        Column("created_by", BigInteger),
        _stamp("created_at"),
        _stamp("updated_at"),
        PrimaryKeyConstraint("id", name="project_webhooks_pkey"),
        ForeignKeyConstraint(
            ["project_id"], ["projects.id"], ondelete="CASCADE", name="project_webhooks_project_id_fkey"
        ),
        ForeignKeyConstraint(
            ["created_by"], ["users.id"], ondelete="SET NULL", name="project_webhooks_created_by_fkey"
        ),
        Index("project_webhooks_project_id_idx", "project_id"),
    )
    deliveries = Table(
        "webhook_deliveries",
        metadata,
        Column("id", BigInteger, Identity(always=True), nullable=False),
        Column("webhook_id", BigInteger, nullable=False),
        Column("event", Text, nullable=False),
        Column("payload", JSONB, nullable=False),
        Column("state", Text, nullable=False, server_default="pending"),
        Column("attempts", Integer, nullable=False, server_default="0"),
        Column("next_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
        Column("last_status", Integer),
        Column("last_error", Text),
        _stamp("created_at"),
        Column("delivered_at", DateTime(timezone=True)),
        PrimaryKeyConstraint("id", name="webhook_deliveries_pkey"),
        ForeignKeyConstraint(
            ["webhook_id"], ["project_webhooks.id"], ondelete="CASCADE", name="webhook_deliveries_webhook_id_fkey"
        ),
        Index("webhook_deliveries_due_idx", "state", "next_at"),
        Index("webhook_deliveries_webhook_id_idx", "webhook_id"),
    )
    return tasks, hooks, deliveries


def dlp_tables(metadata: MetaData) -> tuple[Table, Table]:
    """(dlp_policies, dlp_counts), added to ``metadata`` (0264)."""
    policies = Table(
        "dlp_policies",
        metadata,
        Column("project_id", BigInteger, nullable=False),
        Column("mode", Text, nullable=False),
        Column("kinds", ARRAY(Text), nullable=False),
        Column("keywords", ARRAY(Text), nullable=False, server_default="{}"),
        Column("updated_by", BigInteger),
        _stamp("updated_at"),
        PrimaryKeyConstraint("project_id", name="dlp_policies_pkey"),
        ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE", name="dlp_policies_project_id_fkey"),
        ForeignKeyConstraint(["updated_by"], ["users.id"], ondelete="SET NULL", name="dlp_policies_updated_by_fkey"),
    )
    counts = Table(
        "dlp_counts",
        metadata,
        Column("project_id", BigInteger, nullable=False),
        Column("kind", Text, nullable=False),
        Column("surface", Text, nullable=False),
        Column("action", Text, nullable=False),
        Column("count", BigInteger, nullable=False, server_default="0"),
        _stamp("last_at"),
        PrimaryKeyConstraint("project_id", "kind", "surface", "action", name="dlp_counts_pkey"),
        ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE", name="dlp_counts_project_id_fkey"),
    )
    return policies, counts
