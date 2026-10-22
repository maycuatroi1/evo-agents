"""The memories of agents and the provenance of every memory revision (0101), which ``evo_agents.hub.tables`` adds to
memories and memory_revisions so that it describes both tables whole.

memories.profile_id is the namespace of a memory: NULL for the project's shared memories and a person's own (every
memory before 0101), else the agent profile whose memory it is, in that profile's project (scope project alone, a
CHECK of the migration). The keys of a memory take the namespace in: memories_shared_key and memories_owned_key hold
the memories of no agent, as before, and memories_agent_key gives one memory per (profile, location, name), whatever
its type.

memory_revisions takes where each revision came from when the agent of a run wrote it (``evo_agents.hub.server.
memory_agents``): the run, the profile and version it ran under, the person at its origin (runs.dispatched_by), its
hop, and its chat and chat message; all NULL for a write that came from no run. The run and the chat are plain ids,
kept as the history of the memory whatever happens to them; memory_revisions_run_idx counts what one run wrote.

The upkeep of an agent's memories (0102, ``evo_agents.hub.server.memory_upkeep``): memories.read_at is when the agent
of a run last opened it (memory_get), archived_at when the daily job archived it, nobody having opened it for 30 days
(out of the index and the searches of runs until a writer restores it), and stale_at when the job found that a path it
cites (code:REPO:PATH) is gone from the project's knowledge graph; all NULL for a memory of no agent.

The Curator's promotion of an agent's memories (0103, ``evo_agents.hub.server.memory_promotion``): memories.promoted_at
is when an accepted proposal wrote an agent's memory into a shared one, promoted_into that shared memory (NULL once it
is deleted); memory_revisions.proposal_id names the proposal whose promotion wrote a revision, a plain id kept as
history.
"""

from __future__ import annotations

from sqlalchemy import BigInteger, Column, DateTime, ForeignKeyConstraint, Index, Integer, SmallInteger, Table

SHARED = ("project", "reference")
OWNED = ("user", "feedback")
UPKEEP = ("read_at", "archived_at", "stale_at")  # 0102


def memory_agent_tables(memories: Table, memory_revisions: Table) -> None:
    """Add the namespace, the keys that take it in and the provenance columns to ``memories`` and
    ``memory_revisions``."""
    memories.append_column(Column("profile_id", BigInteger))
    for stamp in UPKEEP:
        memories.append_column(Column(stamp, DateTime(timezone=True)))
    memories.append_column(Column("promoted_at", DateTime(timezone=True)))
    memories.append_column(Column("promoted_into", BigInteger))
    memories.append_constraint(
        ForeignKeyConstraint(
            ["promoted_into"], ["memories.id"], ondelete="SET NULL", name="memories_promoted_into_fkey"
        )
    )
    memories.append_constraint(
        ForeignKeyConstraint(
            ["profile_id"], ["agent_profiles.id"], ondelete="RESTRICT", name="memories_profile_id_fkey"
        )
    )
    m = memories.c
    shared = (m.scope == "project") & m.profile_id.is_(None)
    Index(
        "memories_shared_key",
        m.project_id,
        m.location,
        m.name,
        unique=True,
        postgresql_where=shared & m.type.in_(SHARED),
    )
    Index(
        "memories_owned_key",
        m.owner_id,
        m.project_id,
        m.location,
        m.name,
        unique=True,
        postgresql_where=shared & m.type.in_(OWNED),
    )
    Index(
        "memories_agent_key", m.profile_id, m.location, m.name, unique=True, postgresql_where=m.profile_id.isnot(None)
    )
    for name, kind in (
        ("run_id", BigInteger),
        ("profile_id", BigInteger),
        ("profile_version", Integer),
        ("origin_id", BigInteger),
        ("hop", SmallInteger),
        ("chat_id", BigInteger),
        ("chat_message_id", BigInteger),
        ("proposal_id", BigInteger),
    ):
        memory_revisions.append_column(Column(name, kind))
    memory_revisions.append_constraint(
        ForeignKeyConstraint(
            ["profile_id", "profile_version"],
            ["agent_profile_versions.profile_id", "agent_profile_versions.version"],
            ondelete="RESTRICT",
            name="memory_revisions_profile_fkey",
        )
    )
    memory_revisions.append_constraint(
        ForeignKeyConstraint(["origin_id"], ["users.id"], ondelete="RESTRICT", name="memory_revisions_origin_id_fkey")
    )
    r = memory_revisions.c
    Index("memory_revisions_run_idx", r.run_id, r.memory_id, postgresql_where=r.run_id.isnot(None))
