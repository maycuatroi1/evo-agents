"""Runs on a provider account's model through the hub's gateway route: runs.gateway_provider and gateway leases.

Revision ID: 0149
Revises: 0148
Create Date: 2026-10-25

runs.gateway_provider is the prefix of the run's model, ``<alias>/<model>``, when the run goes through the model
gateway (``evo_agents.hub.gateway_runs``): the catalog's alias of a provider, or ``evo-<secret id>`` for an endpoint a
member named; null for any other run. credential_leases.provider takes ``gateway``: one row holds the gateway's key
made for the run (external_id the key's id at the gateway, its value and the run's token sealed under the lease's id,
token_hash the SHA-256 of that token, unique), and one row per provider account the key reaches (secret_id the account,
external_id the same key id, share_id the share it came under), so a gateway row may name a secret and hold a sealed
value. The checks of 0011 that said otherwise are replaced by named ones; the names below are those Postgres gave the
inline checks of 0011.

Going back refuses while a gateway lease or a run of a gateway model is recorded, as 0148's hub would not know them.
"""

import sqlalchemy as sa
from alembic import op

revision = "0149"
down_revision = "0148"
branch_labels = None
depends_on = None

LEASES = "credential_leases"
PROVIDER_CHECK = "credential_leases_provider_check"
OLD_SECRET_CHECK = "credential_leases_check2"  # (provider = 'secret') = (secret_id IS NOT NULL)
OLD_SEALED_CHECK = "credential_leases_check4"  # sealed_value IS NULL OR provider = 'github-app'
SECRET_CHECK = "credential_leases_secret_check"
SEALED_CHECK = "credential_leases_sealed_check"
TOKEN_CHECK = "credential_leases_token_hash_check"
TOKEN_INDEX = "credential_leases_token_idx"
RUN_CHECK = "runs_gateway_provider_check"
PROVIDERS_0011 = ("secret", "github-app")
PROVIDERS = (*PROVIDERS_0011, "gateway")

col = sa.column


def upgrade() -> None:
    op.add_column("runs", sa.Column("gateway_provider", sa.Text()))
    op.create_check_constraint(
        RUN_CHECK,
        "runs",
        sa.and_(
            sa.func.char_length(col("gateway_provider")).between(1, 64),
            col("gateway_provider").op("~")("^[a-z0-9][a-z0-9._-]*$"),
        ),
    )
    op.add_column(LEASES, sa.Column("token_hash", sa.LargeBinary()))
    op.drop_constraint(PROVIDER_CHECK, LEASES, type_="check")
    op.drop_constraint(OLD_SECRET_CHECK, LEASES, type_="check")
    op.drop_constraint(OLD_SEALED_CHECK, LEASES, type_="check")
    provider = col("provider")
    op.create_check_constraint(PROVIDER_CHECK, LEASES, provider.in_(PROVIDERS))
    op.create_check_constraint(
        SECRET_CHECK, LEASES, sa.or_(provider == "gateway", (provider == "secret") == col("secret_id").is_not(None))
    )
    op.create_check_constraint(
        SEALED_CHECK, LEASES, sa.or_(col("sealed_value").is_(None), provider.in_(("github-app", "gateway")))
    )
    op.create_check_constraint(
        TOKEN_CHECK,
        LEASES,
        sa.or_(
            col("token_hash").is_(None),
            sa.and_(provider == "gateway", sa.func.octet_length(col("token_hash")) == 32),
        ),
    )
    op.create_index(TOKEN_INDEX, LEASES, ["token_hash"], unique=True, postgresql_where=col("token_hash").is_not(None))


def downgrade() -> None:
    bind = op.get_bind()
    leases = sa.table(LEASES, col("id"), col("provider"))
    runs = sa.table("runs", col("id"), col("gateway_provider"))
    found = (
        bind.execute(sa.select(leases.c.id).where(leases.c.provider == "gateway").limit(1)).first()
        or bind.execute(sa.select(runs.c.id).where(runs.c.gateway_provider.is_not(None)).limit(1)).first()
    )
    if found is not None:
        raise RuntimeError(
            "a run of a gateway model or a gateway lease is recorded: revision 0148 knows neither, so going back would "
            "lose them; end and prune those runs first"
        )
    op.drop_index(TOKEN_INDEX, LEASES)
    for name in (TOKEN_CHECK, SEALED_CHECK, SECRET_CHECK, PROVIDER_CHECK):
        op.drop_constraint(name, LEASES, type_="check")
    provider = col("provider")
    op.create_check_constraint(PROVIDER_CHECK, LEASES, provider.in_(PROVIDERS_0011))
    op.create_check_constraint(OLD_SECRET_CHECK, LEASES, (provider == "secret") == col("secret_id").is_not(None))
    op.create_check_constraint(
        OLD_SEALED_CHECK, LEASES, sa.or_(col("sealed_value").is_(None), provider == "github-app")
    )
    op.drop_column(LEASES, "token_hash")
    op.drop_constraint(RUN_CHECK, "runs", type_="check")
    op.drop_column("runs", "gateway_provider")
