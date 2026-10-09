"""Secrets out of a session digest before it leaves the machine (``evo_agents.hub.redact``).

Each kind of secret the curator-agent plan names, step 2, is in the fixture: GitHub tokens, Anthropic keys, OpenAI
keys, AWS keys, PEM private keys, hub tokens (evh_), worker tokens (evw_) and the value of a lease as a session shows
it. Each fixture value is built from parts, so no file of the repository holds one whole. Every value is gone after
``redact``, and so is every value the caller names; the text around them stays readable, and ordinary text that only
looks a little like a secret (token counts, words with hyphens) is left alone."""

import json

import pytest

from evo_agents.hub import redact as redaction
from evo_agents.hub.log import MASK
from evo_agents.hub.redact import redact, redact_data


def _joined(*parts: str) -> str:
    return "".join(parts)


GITHUB_CLASSIC = _joined("gh", "p_", "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8")
GITHUB_APP = _joined("gh", "s_", "Z9y8X7w6V5u4T3s2R1q0P9o8N7m6L5k4J3i2")  # an installation token, as a git lease holds
GITHUB_FINE = _joined("github", "_pat_", "11ABCDEFG0", "abcdefghijklmnopqrstuvwxyz0123456789ABCDEFGHIJKLMNOP")
ANTHROPIC_KEY = _joined("sk-", "ant-", "api03-", "k" * 40)
ANTHROPIC_OAUTH = _joined("sk-", "ant-", "oat01-", "t" * 40)  # CLAUDE_CODE_OAUTH_TOKEN, as an env lease holds
OPENAI_KEY = _joined("sk-", "proj-", "Ab3" * 16)
OPENAI_PLAIN = _joined("sk-", "Q" * 48)
AWS_KEY_ID = _joined("AK", "IA", "IOSFODNN7EXAMPLE")
AWS_SECRET = _joined("wJalrXUtnFEMI", "/K7MDENG/", "bPxRfiCYEXAMPLEKEY")
PEM_BODY = _joined("MIIEowIBAAKCAQEA", "x" * 64)
PEM = _joined("-----BEGIN ", "RSA PRIVATE KEY-----\n", PEM_BODY, "\n-----END RSA ", "PRIVATE KEY-----")
PEM_CUT = _joined("-----BEGIN ", "OPENSSH PRIVATE KEY-----\n", PEM_BODY)  # a key cut before its END line
HUB_TOKEN = _joined("ev", "h_", "H" * 43)
WORKER_TOKEN = _joined("ev", "w_", "W" * 43)
LEASE_ENV = _joined("lease-", "env-value-", "4f9c2e71")  # what `evo-agents worker env` prints for a lease of kind env
LEASE_GIT = _joined("lease-", "git-password-", "8d1a")  # what git's credential helper prints for a git lease

CASES = {
    "GitHub classic token": (f"git push https://x-access-token:{GITHUB_CLASSIC}@github.com/o/r.git", GITHUB_CLASSIC),
    "GitHub installation token": (f"export GH_TOKEN={GITHUB_APP}", GITHUB_APP),
    "GitHub fine-grained token": (f"gh auth login --with-token <<< {GITHUB_FINE}", GITHUB_FINE),
    "Anthropic API key": (f"ANTHROPIC_API_KEY={ANTHROPIC_KEY} claude -p hi", ANTHROPIC_KEY),
    "Anthropic OAuth token": (f"curl -H 'x-api-key: {ANTHROPIC_OAUTH}' https://api.anthropic.com", ANTHROPIC_OAUTH),
    "OpenAI project key": (f'{{"api_key": "{OPENAI_KEY}"}}', OPENAI_KEY),
    "OpenAI key": (f"Error: invalid key {OPENAI_PLAIN} (401)", OPENAI_PLAIN),
    "AWS access key id": (f"aws configure set aws_access_key_id {AWS_KEY_ID}", AWS_KEY_ID),
    "AWS secret key": (f"aws_secret_access_key = {AWS_SECRET}", AWS_SECRET),
    "AWS secret key in env": (f"AWS_SECRET_ACCESS_KEY={AWS_SECRET} aws s3 ls", AWS_SECRET),
    "PEM private key": (f"cat id_rsa\n{PEM}\ndone", PEM_BODY),
    "PEM private key cut": (f"head -c 120 key.pem\n{PEM_CUT}", PEM_BODY),
    "hub token": (f"Authorization: Bearer {HUB_TOKEN}", HUB_TOKEN),
    "hub token alone": (f"~/.evo/hub/token holds {HUB_TOKEN}", HUB_TOKEN),
    "worker token": (f"worker token {WORKER_TOKEN} written", WORKER_TOKEN),
    "lease of kind env": (f"export CLAUDE_CODE_OAUTH_TOKEN='{LEASE_ENV}'", LEASE_ENV),
    "lease of kind git": (f"protocol=https\nhost=github.com\nusername=x-access-token\npassword={LEASE_GIT}", LEASE_GIT),
}


@pytest.mark.parametrize("text, secret", CASES.values(), ids=CASES.keys())
def test_redact_replaces_each_kind_of_secret(text, secret):
    cleaned = redact(text)
    assert secret not in cleaned, cleaned
    assert MASK in cleaned


def test_redact_keeps_the_text_around_a_secret():
    assert redact(CASES["GitHub classic token"][0]) == "git push https://x-access-token:***@github.com/o/r.git"
    assert redact(CASES["AWS secret key in env"][0]) == "AWS_SECRET_ACCESS_KEY=*** aws s3 ls"
    assert redact(CASES["lease of kind env"][0]) == "export CLAUDE_CODE_OAUTH_TOKEN='***'"
    masked = _joined("-----BEGIN ", "RSA PRIVATE KEY-----***-----END RSA ", "PRIVATE KEY-----")
    assert redact(CASES["PEM private key"][0]) == f"cat id_rsa\n{masked}\ndone"
    assert redact(CASES["hub token"][0]) == "Authorization: Bearer evh_***"


def test_redact_replaces_the_values_it_is_given_in_every_form():
    value = "machine/token+value=1"
    for text in (f"echo {value}", "echo machine%2Ftoken%2Bvalue%3D1"):
        assert redact(text, [value]) == "echo ***"
    assert redact("short ab", ["ab"]) == "short ab", "values under 3 characters would mask ordinary words"


def test_redact_leaves_ordinary_text_alone():
    for text in (
        "max_tokens: 4096, input_tokens: 123456, MAX_TOKENS=4096",
        "risk-assessment-procedure-for-the-team and task-something-long-enough",
        "python -m pytest -q tests/hub -k 'digest or redact or tool_stats'",
        "the token was refused; see `evo-agents hub login`",
    ):
        assert redact(text) == text


def test_redact_data_cleans_every_string_and_key_of_a_digest():
    digest = {
        "user_turns": [f"my key is {ANTHROPIC_KEY}"],
        "commands": [f"export GITHUB_TOKEN={GITHUB_APP}"],
        "errors": [{"gen_ai.tool.name": "Bash", "text": PEM, "n": 1}],
        "models": [{"model": "claude-opus-5-5", "messages": 3}],
        "files_read": 2,
        "started_at": None,
        f"odd key {HUB_TOKEN}": True,
    }
    cleaned = redact_data(digest, [LEASE_ENV])
    text = json.dumps(cleaned)
    for secret in (ANTHROPIC_KEY, GITHUB_APP, PEM_BODY, HUB_TOKEN):
        assert secret not in text
    assert cleaned["files_read"] == 2 and cleaned["started_at"] is None
    assert cleaned["models"] == digest["models"]
    assert cleaned["errors"][0]["gen_ai.tool.name"] == "Bash"


def test_redact_patterns_are_compiled_once_and_cover_every_kind():
    sources = " ".join(pattern.pattern for pattern, _ in redaction.SECRET_PATTERNS)
    for word in ("sk-ant-", "AKIA", "xox", "AIza", "Basic", "TOKEN"):
        assert word in sources, word
