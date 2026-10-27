"""A core plugin as the actor of a decision on the knowledge graph or of a dismissal of a review item.

Revision ID: 0174
Revises: 0173
Create Date: 2026-10-27

The names below are copies of ``evo_agents.hub.tables_kg`` as of this revision, never imports, so a later change to
them takes a new revision. Every statement is an Alembic operation on SQLAlchemy Core expressions.

A core plugin that declares an egress sink (plugin API 1.6, ``evo_agents.hub.server.core_plugin_api``) takes decisions on
a project's graph and dismisses items of its review queue as itself, not as a member. Two columns say so:
kg_decision_events.actor_plugin and kg_review_dismissals.dismissed_by_plugin hold the plugin's id (1 to 63 characters,
as plugin.schema.json bounds an id). The member columns, kg_decision_events.actor_id and
kg_review_dismissals.dismissed_by, become nullable, and a check keeps each row to exactly one of the two: a member or
a plugin, never both and never none. Every row of 0173 has its member, so it passes.

Going back drops the plugin columns and makes the member columns required again. A dismissal a plugin made goes (the
next build lists its item again); a step of a decision's history a plugin took stays, its actor the member with the
lowest id, because a decision's history is numbered without gaps and its other steps would lose their order.
"""

import sqlalchemy as sa
from alembic import op

revision = "0174"
down_revision = "0173"
branch_labels = None
depends_on = None

DISMISSALS, DISMISSAL_ACTOR, DISMISSAL_PLUGIN = "kg_review_dismissals", "dismissed_by", "dismissed_by_plugin"
EVENTS, EVENT_ACTOR, EVENT_PLUGIN = "kg_decision_events", "actor_id", "actor_plugin"
MAX_PLUGIN_CHARS = 63

col = sa.column


def _check(table: str, actor: str, plugin: str) -> None:
    op.create_check_constraint(
        f"{table}_actor_check",
        table,
        sa.or_(
            sa.and_(col(actor).is_not(None), col(plugin).is_(None)),
            sa.and_(
                col(actor).is_(None),
                col(plugin).is_not(None),  # a check passes on NULL: char_length of none would let "neither" in
                sa.func.char_length(col(plugin)).between(1, MAX_PLUGIN_CHARS),
            ),
        ),
    )


def upgrade() -> None:
    for table, actor, plugin in ((DISMISSALS, DISMISSAL_ACTOR, DISMISSAL_PLUGIN), (EVENTS, EVENT_ACTOR, EVENT_PLUGIN)):
        op.add_column(table, sa.Column(plugin, sa.Text()))
        op.alter_column(table, actor, existing_type=sa.BigInteger(), nullable=True)
        _check(table, actor, plugin)


def downgrade() -> None:
    users = sa.table("users", col("id"))
    first = sa.select(sa.func.min(users.c.id)).scalar_subquery()
    dismissals = sa.table(DISMISSALS, col(DISMISSAL_ACTOR), col(DISMISSAL_PLUGIN))
    events = sa.table(EVENTS, col(EVENT_ACTOR), col(EVENT_PLUGIN))
    for table in (EVENTS, DISMISSALS):  # first: the plugin's step gets a member while its plugin column still holds
        op.drop_constraint(f"{table}_actor_check", table, type_="check")
    op.execute(dismissals.delete().where(dismissals.c[DISMISSAL_PLUGIN].is_not(None)))
    op.execute(events.update().values({EVENT_ACTOR: first}).where(events.c[EVENT_ACTOR].is_(None)))
    for table, actor, plugin in ((EVENTS, EVENT_ACTOR, EVENT_PLUGIN), (DISMISSALS, DISMISSAL_ACTOR, DISMISSAL_PLUGIN)):
        op.alter_column(table, actor, existing_type=sa.BigInteger(), nullable=False)
        op.drop_column(table, plugin)
