"""The tables of the outputs of runs (0085), which ``evo_agents.hub.tables`` adds to its metadata and exposes as its own
attributes (``tables.run_outputs``, ``tables.run_output_links``): queries name them there.

run_outputs holds each file a run's agent made that the hub keeps (``evo_agents.hub.outputs``): its run and the run's
project, which holds its bytes as a blob (``blobs``, kind run-output); its source (sent, tool or outputs), name, media
type, size, SHA-256 and caption; the tool call of the run's trace it came from, when it came from one; and when the hub
recorded it. A run holds one output per SHA-256. run_output_links ties an output to a question of its run (a decision)
or to a chat message its run's agent wrote, one of the two per row, so the question and the message carry it.
``projects.output_tool_images`` is a project's switch of the images its machines take out of tools' results: off, the
hub refuses outputs of source tool and the machines send none.
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
    MetaData,
    PrimaryKeyConstraint,
    Table,
    Text,
    UniqueConstraint,
    func,
    true,
)


def _fk(table: str, column: str, target: str, *, ondelete: str) -> ForeignKeyConstraint:
    return ForeignKeyConstraint([column], [target], ondelete=ondelete, name=f"{table}_{column}_fkey")


def _id() -> Column:
    return Column("id", BigInteger, Identity(always=True), nullable=False)


def _stamp(name: str) -> Column:
    return Column(name, DateTime(timezone=True), nullable=False, server_default=func.now())


def output_tables(metadata: MetaData, projects: Table) -> tuple[Table, Table]:
    """(run_outputs, run_output_links), added to ``metadata``; the switch of a project added to ``projects``."""
    projects.append_column(Column("output_tool_images", Boolean, nullable=False, server_default=true()))
    outputs = Table(
        "run_outputs",
        metadata,
        _id(),
        Column("run_id", BigInteger, nullable=False),
        Column("project_id", BigInteger, nullable=False),
        Column("source", Text, nullable=False),
        Column("name", Text, nullable=False),
        Column("media_type", Text, nullable=False),
        Column("size", BigInteger, nullable=False),
        Column("sha256", Text, nullable=False),
        Column("caption", Text),
        Column("tool_call_id", Text),
        _stamp("created_at"),
        _fk("run_outputs", "run_id", "runs.id", ondelete="CASCADE"),
        _fk("run_outputs", "project_id", "projects.id", ondelete="CASCADE"),
        PrimaryKeyConstraint("id", name="run_outputs_pkey"),
        UniqueConstraint("run_id", "sha256", name="run_outputs_run_id_sha256_key"),
    )
    Index("run_outputs_sha256_idx", outputs.c.sha256)
    links = Table(
        "run_output_links",
        metadata,
        _id(),
        Column("output_id", BigInteger, nullable=False),
        Column("decision_id", BigInteger),
        Column("chat_message_id", BigInteger),
        _stamp("created_at"),
        _fk("run_output_links", "output_id", "run_outputs.id", ondelete="CASCADE"),
        _fk("run_output_links", "decision_id", "decisions.id", ondelete="CASCADE"),
        _fk("run_output_links", "chat_message_id", "chat_messages.id", ondelete="CASCADE"),
        PrimaryKeyConstraint("id", name="run_output_links_pkey"),
        UniqueConstraint("output_id", "decision_id", name="run_output_links_decision_key"),
        UniqueConstraint("output_id", "chat_message_id", name="run_output_links_message_key"),
    )
    Index("run_output_links_decision_idx", links.c.decision_id, postgresql_where=links.c.decision_id.is_not(None))
    Index(
        "run_output_links_message_idx",
        links.c.chat_message_id,
        postgresql_where=links.c.chat_message_id.is_not(None),
    )
    return outputs, links
