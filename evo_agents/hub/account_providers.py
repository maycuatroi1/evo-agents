"""The provider and category of an account, read from the variable its secret sets (``docs/credentials.md``, section
"Accounts"): a table here, no column. A pure module: the server and the CLI both read it.

``model`` accounts sign an agent's runtime in, so a run's cost belongs to them; every other variable is a ``service``
credential, which carries no cost.
"""

from __future__ import annotations

from typing import Literal, get_args

Provider = Literal["claude", "anthropic", "openai", "gitlab", "github", "notion", "lark", "ssh", "other"]
Category = Literal["model", "service"]
PROVIDERS: tuple[str, ...] = get_args(Provider)
CATEGORIES: tuple[str, ...] = get_args(Category)

# Exact variable names, then prefixes, then suffixes; the first match wins, no match is "other".
_NAMES: dict[str, Provider] = {
    "CLAUDE_CODE_OAUTH_TOKEN": "claude",
    "ANTHROPIC_API_KEY": "anthropic",
    "OPENAI_API_KEY": "openai",
    "GH_TOKEN": "github",
}
_PREFIXES: tuple[tuple[str, Provider], ...] = (
    ("GITLAB_", "gitlab"),
    ("GITHUB_", "github"),
    ("NOTION_", "notion"),
    ("LARK_", "lark"),
    ("FEISHU_", "lark"),
    ("SSH_", "ssh"),
)
_SUFFIXES: tuple[tuple[str, Provider], ...] = (("_SSH_KEY", "ssh"),)
MODEL_PROVIDERS = frozenset({"claude", "anthropic", "openai"})


def provider_of(env_var: str) -> Provider:
    """The provider of the variable ``env_var``: claude, anthropic, openai, gitlab, github, notion, lark, ssh or other."""
    if env_var in _NAMES:
        return _NAMES[env_var]
    for prefix, provider in _PREFIXES:
        if env_var.startswith(prefix):
            return provider
    for suffix, provider in _SUFFIXES:
        if env_var.endswith(suffix):
            return provider
    return "other"


def category_of(provider: str) -> Category:
    """model for the runtime's own sign-in (claude, anthropic, openai), service for the rest."""
    return "model" if provider in MODEL_PROVIDERS else "service"
