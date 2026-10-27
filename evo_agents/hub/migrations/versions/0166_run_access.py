"""Runs that call the hub as their owner (``evo_agents.hub.server.run_access``): runs.as_owner, tokens of kind run and
leases of provider run-access.

Revision ID: 0166
Revises: 0165
Create Date: 2026-10-27

runs.as_owner says the run's owner, the member who dispatched it, let it call the hub's API as them; false for every
run before. tokens.kind takes ``run``: the token ``evr_...`` of one such run, the owner's, whose SHA-256 alone the hub
keeps, as for any token. credential_leases.provider takes ``run-access``: the lease that hands that token to the run's
worker, token_id naming it (set for that provider alone, and unique), the token sealed under the lease's id. The checks
of 0009 and 0152 that said otherwise are replaced under the same names.

Going back refuses while a token of kind run or a lease of provider run-access is recorded, as 0165's hub would not
know them; runs.as_owner is dropped with its column.
"""

import sqlalchemy as sa
from alembic import op

revision = "0166"
down_revision = "0165"
branch_labels = None
depends_on = None

LEASES = "credential_leases"
RUN_ACCESS = "run-access"  # evo_agents.hub.credentials.RUN_ACCESS
TOKEN_KINDS_0009 = ("machine", "web", "worker")
TOKEN_KINDS = (*TOKEN_KINDS_0009, "run")  # evo_agents.hub.server.security.PREFIXES
PROVIDERS_0152 = ("secret", "github-app", "gateway")
PROVIDERS = (*PROVIDERS_0152, RUN_ACCESS)
SEALED_0152 = ("github-app", "gateway")
SEALED = (*SEALED_0152, RUN_ACCESS)
TOKEN_KIND_CHECK = "tokens_kind_check"
PROVIDER_CHECK = "credential_leases_provider_check"
SEALED_CHECK = "credential_leases_sealed_check"
TOKEN_ID_CHECK = "credential_leases_token_id_check"
TOKEN_ID_KEY = "credential_leases_token_id_fkey"
TOKEN_ID_INDEX = "credential_leases_token_id_idx"

col = sa.column


def _checks(kinds, providers, sealed) -> None:
    op.create_check_constraint(TOKEN_KIND_CHECK, "tokens", col("kind").in_(kinds))
    op.create_check_constraint(PROVIDER_CHECK, LEASES, col("provider").in_(providers))
    op.create_check_constraint(SEALED_CHECK, LEASES, sa.or_(col("sealed_value").is_(None), col("provider").in_(sealed)))


def _drop_checks() -> None:
    op.drop_constraint(TOKEN_KIND_CHECK, "tokens", type_="check")
    op.drop_constraint(PROVIDER_CHECK, LEASES, type_="check")
    op.drop_constraint(SEALED_CHECK, LEASES, type_="check")


def upgrade() -> None:
    op.add_column("runs", sa.Column("as_owner", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column(LEASES, sa.Column("token_id", sa.BigInteger()))
    op.create_foreign_key(TOKEN_ID_KEY, LEASES, "tokens", ["token_id"], ["id"], ondelete="RESTRICT")
    token_id = col("token_id")
    op.create_index(TOKEN_ID_INDEX, LEASES, ["token_id"], unique=True, postgresql_where=token_id.is_not(None))
    _drop_checks()
    _checks(TOKEN_KINDS, PROVIDERS, SEALED)
    op.create_check_constraint(TOKEN_ID_CHECK, LEASES, (col("provider") == RUN_ACCESS) == token_id.is_not(None))


def downgrade() -> None:
    bind = op.get_bind()
    leases = sa.table(LEASES, col("id"), col("provider"))
    tokens = sa.table("tokens", col("id"), col("kind"))
    found = (
        bind.execute(sa.select(leases.c.id).where(leases.c.provider == RUN_ACCESS).limit(1)).first()
        or bind.execute(sa.select(tokens.c.id).where(tokens.c.kind == "run").limit(1)).first()
    )
    if found is not None:
        raise RuntimeError(
            "a token of kind run or a lease of provider run-access is recorded: revision 0165 knows neither, so going "
            "back would lose them; end and prune those runs, and delete their tokens, first"
        )
    op.drop_constraint(TOKEN_ID_CHECK, LEASES, type_="check")
    _drop_checks()
    _checks(TOKEN_KINDS_0009, PROVIDERS_0152, SEALED_0152)
    op.drop_index(TOKEN_ID_INDEX, LEASES)
    op.drop_constraint(TOKEN_ID_KEY, LEASES, type_="foreignkey")
    op.drop_column(LEASES, "token_id")
    op.drop_column("runs", "as_owner")
