"""A member chooses the tier of each kind of notification, their quiet hours, their time zone and their summary push.

Revision ID: 0149
Revises: 0148
Create Date: 2026-10-25

notification_preferences gains (plan hub-mobile-phase-0, criterion a3):

- ``tiers``, the tier the member chose for kinds of notification: a JSON object whose keys are kinds (KIND: decision,
  proposal or a kind of notice) and whose values are TIERS, empty by default. The kinds are not listed here, so a kind of
  notice added later takes no revision; the hub checks a kind when the member sets it.
- ``quiet_start`` and ``quiet_end``, the member's quiet hours as HH:MM, both or neither, and not the same time.
- ``time_zone``, an IANA name of 1 to MAX_TIME_ZONE_CHARS characters, which the hub checks against Postgres's list
  when the member sets it; NULL is the hub's web time zone.
- ``digest_at``, the local time of the member's summary push as HH:MM; NULL is the hub's default.

Every row of 0148 keeps its hide_details and gets the defaults. The names below are copies of
``evo_agents.hub.tables_push`` and ``evo_agents.hub.curator`` as of this revision, never imports, so a later change to
them takes a new revision. Every statement is an Alembic operation on SQLAlchemy Core expressions.

Going back drops the five columns, which is the schema 0148 runs on: its hub pushes a decision asked of a member and
nothing else, at any hour.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0149"
down_revision = "0148"
branch_labels = None
depends_on = None

TABLE = "notification_preferences"
CHECK = "notification_preferences_check"
TIERS = ("interrupt", "brief", "off")  # evo_agents.hub.tables_push.TIERS
KIND = "^[a-z][a-z0-9_]{0,63}$"  # a kind of notification, as a key of tiers: decision, proposal or a kind of notice
MAX_TIME_ZONE_CHARS = 64  # evo_agents.hub.curator.MAX_TIME_ZONE_CHARS
TIME_OF_DAY = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"  # evo_agents.hub.curator.TIME_OF_DAY
# a key of tiers that is no kind, or a value that is not one of TIERS
IS_A_TIER = " || ".join(f'@.value == "{tier}"' for tier in TIERS)
NOT_A_TIER = f'$.keyvalue() ? (!(@.key like_regex "{KIND}") || @.value.type() != "string" || !({IS_A_TIER}))'
COLUMNS = ("tiers", "quiet_start", "quiet_end", "time_zone", "digest_at")

col = sa.column


def _jsonpath(text: str):
    return sa.cast(sa.literal(text), postgresql.JSONPATH)


def _time_of_day(name: str):
    return col(name).regexp_match(TIME_OF_DAY)


def upgrade() -> None:
    op.add_column(TABLE, sa.Column("tiers", postgresql.JSONB(), server_default="{}", nullable=False))
    for name in COLUMNS[1:]:
        op.add_column(TABLE, sa.Column(name, sa.Text(), nullable=True))
    tiers = col("tiers")
    # jsonb_path_exists of keyvalue() fails on a value that is not an object: the CASE reads it only for one
    shape = sa.case(
        (sa.func.jsonb_typeof(tiers) == "object", ~sa.func.jsonb_path_exists(tiers, _jsonpath(NOT_A_TIER))),
        else_=False,
    )
    # both ends or neither: an OR alone would let one NULL end through, the CHECK passing on NULL
    quiet = sa.and_(
        sa.func.num_nulls(col("quiet_start"), col("quiet_end")).in_((0, 2)),
        sa.or_(
            col("quiet_start").is_(None),
            sa.and_(_time_of_day("quiet_start"), _time_of_day("quiet_end"), col("quiet_start") != col("quiet_end")),
        ),
    )
    zone = sa.or_(col("time_zone").is_(None), sa.func.char_length(col("time_zone")).between(1, MAX_TIME_ZONE_CHARS))
    digest = sa.or_(col("digest_at").is_(None), _time_of_day("digest_at"))
    op.create_check_constraint(CHECK, TABLE, sa.and_(shape, quiet, zone, digest))


def downgrade() -> None:
    op.drop_constraint(CHECK, TABLE, type_="check")
    for name in reversed(COLUMNS):
        op.drop_column(TABLE, name)
