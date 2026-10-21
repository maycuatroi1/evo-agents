"""The provider and category an account has by the variable its secret sets (evo_agents/hub/account_providers.py)."""

import pytest

from evo_agents.hub.account_providers import category_of, provider_of


@pytest.mark.parametrize(
    ("env_var", "provider", "category"),
    [
        ("CLAUDE_CODE_OAUTH_TOKEN", "claude", "model"),
        ("ANTHROPIC_API_KEY", "anthropic", "model"),
        ("OPENAI_API_KEY", "openai", "model"),
        ("GITLAB_TOKEN", "gitlab", "service"),
        ("GITLAB_PRIVATE_TOKEN", "gitlab", "service"),
        ("GITHUB_TOKEN", "github", "service"),
        ("GH_TOKEN", "github", "service"),
        ("NOTION_API_KEY", "notion", "service"),
        ("LARK_APP_SECRET", "lark", "service"),
        ("FEISHU_APP_SECRET", "lark", "service"),
        ("DEPLOY_SSH_KEY", "ssh", "service"),
        ("SSH_PRIVATE_KEY", "ssh", "service"),
        ("SLACK_TOKEN", "other", "service"),
        ("OPENAI_ORG", "other", "service"),
    ],
)
def test_the_variable_names_the_provider_and_its_category(env_var, provider, category):
    assert provider_of(env_var) == provider
    assert category_of(provider) == category
