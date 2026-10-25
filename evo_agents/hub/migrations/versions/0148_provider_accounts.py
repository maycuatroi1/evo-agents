"""Provider accounts: an API key of a model provider with the endpoint it calls and the models it serves.

Revision ID: 0148
Revises: 0147
Create Date: 2026-10-25

A provider account is a row of secrets of the new kind provider, whose sealed value is the API key, and a row of
provider_accounts: provider, the id of its entry in the catalog; base_url, an https root; api_format, one of FORMATS;
models, a JSON array of names. A secret of kind provider has no env_var, url_prefix or username, which the checks of
0011 and 0080 already say of a kind they do not name; an agent profile may own one as well as an env secret
(secrets_profile_env_check widened). FORMATS is a copy of ``evo_agents.hub.provider_accounts.API_FORMATS`` as of this
revision, never an import, so a later change to it takes a new revision.

Going back drops provider_accounts and deletes the secrets of kind provider, with their bindings, accounts rows and
shares (ON DELETE CASCADE); it refuses while a run or a lease names one of them, as 0147's hub would not know it.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0148"
down_revision = "0147"
branch_labels = None
depends_on = None

KINDS = ("env", "git", "forge", "provider")
KINDS_0080 = ("env", "git", "forge")
FORMATS = ("openai", "anthropic", "openai-responses", "gemini")
KIND_CHECK = "secrets_kind_check"
ENV_CHECK = "secrets_profile_env_check"  # 0104: a profile's secret is kind env; from here env or provider
FORMAT_CHECK = "provider_accounts_api_format_check"
URL_CHECK = "provider_accounts_base_url_check"
MODELS_CHECK = "provider_accounts_models_check"

col = sa.column


def _secret_checks(kinds, profile_kinds) -> None:
    op.drop_constraint(KIND_CHECK, "secrets", type_="check")
    op.drop_constraint(ENV_CHECK, "secrets", type_="check")
    op.create_check_constraint(KIND_CHECK, "secrets", col("kind").in_(kinds))
    op.create_check_constraint(
        ENV_CHECK, "secrets", sa.or_(col("profile_id").is_(None), col("kind").in_(profile_kinds))
    )


def upgrade() -> None:
    _secret_checks(KINDS, ("env", "provider"))
    op.create_table(
        "provider_accounts",
        sa.Column("secret_id", sa.BigInteger(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("base_url", sa.Text(), nullable=False),
        sa.Column("api_format", sa.Text(), nullable=False),
        sa.Column("models", postgresql.JSONB(), server_default="[]", nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["secret_id"], ["secrets.id"], ondelete="CASCADE", name="provider_accounts_secret_id_fkey"
        ),
        sa.PrimaryKeyConstraint("secret_id", name="provider_accounts_pkey"),
    )
    op.create_check_constraint(FORMAT_CHECK, "provider_accounts", col("api_format").in_(FORMATS))
    op.create_check_constraint(
        URL_CHECK,
        "provider_accounts",
        sa.and_(col("base_url").like("https://%"), sa.func.char_length(col("base_url")) <= 2000),
    )
    op.create_check_constraint(
        MODELS_CHECK, "provider_accounts", sa.func.jsonb_typeof(col("models", postgresql.JSONB)) == "array"
    )


def downgrade() -> None:
    secrets = sa.table("secrets", col("id"), col("kind"))
    providers = sa.select(secrets.c.id).where(secrets.c.kind == "provider")
    for name in ("run_accounts", "credential_leases"):
        held = sa.table(name, col("secret_id"))
        found = op.get_bind().execute(sa.select(held.c.secret_id).where(held.c.secret_id.in_(providers)).limit(1))
        if found.first() is not None:
            raise RuntimeError(
                f"{name} names a provider account: revision 0147 has no provider accounts, so going back would lose "
                "what those runs took; end and prune those runs first"
            )
    op.drop_table("provider_accounts")
    op.execute(secrets.delete().where(secrets.c.kind == "provider"))
    _secret_checks(KINDS_0080, ("env",))
