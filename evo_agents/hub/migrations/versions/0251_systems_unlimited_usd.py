"""No budget by default: a stored system spec whose trace_usd or daily_usd is the old default of 200 USD sets none.

Revision ID: 0251
Revises: 0250
Create Date: 2026-11-16

``evo_agents.hub.system_spec.check`` filled limits.trace_usd and limits.daily_usd with their default, 200 USD, into
every spec it stored, so a spec that set no limit reads 200.0 as one a user typed would. The default is now 0
(``system_spec.UNLIMITED_USD``), no limit. Nothing stored tells a filled default from a typed 200, so this revision
takes every 200.0 for the default and writes 0, in each system as it is now (agent_systems) and in each of its stored
revisions (agent_system_revisions), so restoring an old revision brings back no cap. A user who wants a cap of 200
sets it again. ops_daily_usd was never filled in and stays as stored.

Going back changes nothing: the 0 this revision wrote cannot be told from a 0 a user set.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0251"
down_revision = "0250"
branch_labels = None
depends_on = None

OLD_DEFAULT_USD = 200  # system_spec.LIMITS trace_usd and daily_usd as of revision 0250
LIMITS = ("trace_usd", "daily_usd")


def upgrade() -> None:
    for name in ("agent_systems", "agent_system_revisions"):
        specs = sa.table(name, sa.column("spec", JSONB))
        for limit in LIMITS:
            value = specs.c.spec["limits"][limit]
            op.execute(
                sa.update(specs)
                .values(
                    spec=sa.func.jsonb_set(
                        specs.c.spec, sa.literal(["limits", limit], sa.ARRAY(sa.Text)), sa.func.to_jsonb(0)
                    )
                )
                .where(sa.func.jsonb_typeof(value) == "number", value.astext.cast(sa.Numeric) == OLD_DEFAULT_USD)
            )


def downgrade() -> None:
    pass
