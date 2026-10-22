"""Credential shares: the rules both ends of ``evo-agents hub account share`` and ``hub secret share`` read, and the
words of the terms warning the web, the command line and the audit show the same way. Standard library only, so the
command line reads them without the server; ``docs/credentials.md`` describes the design, the routes are
``evo_agents.hub.server.credential_shares``.

The owner of a secret or an account shares it with one member (grantee kind ``user``), a unit of the org chart and
every unit below it (``unit``), or everyone on the hub (``all``), for some of the projects the secret is bound to. A
share may carry the autonomy level a run using it is held to, a monthly budget in USD, a usage ceiling in percent of
the account's windows, the most runs that may hold it at once and an end. A secret of kind forge is never shared: no
run ever gets it.

The hub sorts a credential into a class by its kind, the provider its variable names and how it signed in
(``credential_class``). A personal subscription token, an account of provider claude whether its token was pasted or
came from OAuth, is shared only when the request carries its owner's acknowledgement of TERMS_WARNING; an API key, a
git secret and any other env secret need none.
"""

from __future__ import annotations

from evo_agents.hub.account_providers import provider_of

USER, UNIT, ALL = GRANTEE_KINDS = ("user", "unit", "all")
SUBSCRIPTION, API_KEY, GIT, ENV = CREDENTIAL_CLASSES = ("subscription", "api_key", "git", "env")
API_KEY_PROVIDERS = frozenset({"anthropic", "openai"})
SUBSCRIPTION_PROVIDERS = frozenset({"claude"})
MAX_BUDGET_USD = 1_000_000
MAX_RUNNING = 100
MAX_SHARE_PROJECTS = 100
MAX_SHARES_PER_SECRET = 100  # live ones

# Shown before a personal subscription token is shared, in the 409 that refuses a share without the acknowledgement,
# on the command line, on the web, and kept whole in the audit row share.terms_ack: one text, never reworded apart.
TERMS_WARNING = (
    "This is a personal Claude subscription token. Anthropic's Consumer Terms (section 2) do not allow you to let "
    "anyone else use your account. Anthropic may limit, suspend or close the account without notice. Every request "
    "a run makes with this token is made in your name, as the account holder."
)
ACK_HINT = "To share it anyway, acknowledge the warning: terms_ack true in the API, --ack-terms on the command line."


def credential_class(kind: str, env_var: str | None) -> str:
    """The class of a secret of ``kind`` setting ``env_var``: subscription for a personal Claude account, whatever
    its sign-in (token or OAuth); api_key for an API key of a model provider; git for kind git; env for the rest."""
    if kind == "git":
        return GIT
    provider = provider_of(env_var or "")
    if provider in SUBSCRIPTION_PROVIDERS:
        return SUBSCRIPTION
    if provider in API_KEY_PROVIDERS:
        return API_KEY
    return ENV


def needs_terms_ack(credential: str) -> bool:
    """Whether sharing a credential of class ``credential`` needs its owner to acknowledge TERMS_WARNING."""
    return credential == SUBSCRIPTION


def terms_refusal() -> str:
    """The message of the 409 that refuses to share a subscription token without the acknowledgement: the warning
    word for word, then how to acknowledge it."""
    return f"{TERMS_WARNING} {ACK_HINT}"
