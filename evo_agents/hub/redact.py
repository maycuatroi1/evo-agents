"""Secrets out of what leaves this machine: every string that looks like a credential becomes ``***``.

``redact`` and ``redact_data`` clean the digest of a Claude Code session (``evo_agents.hub.digest``) before the Stop
hook of the evo-hub plugin sends it to the hub. In this order:

1. the values the caller names, such as this machine's hub token, in each form a text may hold them: as they are,
   URL-encoded and URL-decoded;
2. a private key in PEM, from its BEGIN line to its END line, or to the end of the text when it was cut before that;
3. what the hub's log removes (``evo_agents.hub.log.scrub``): the values registered there, which on a worker are the
   leases its runs hold, the password of a URI and of ``password=`` (git's credential helper prints a git lease that
   way), hub tokens, web sessions and worker tokens (``evh_``, ``evs_``, ``evw_``), worker pairing codes, GitHub
   tokens of every kind, Bearer credentials, the signature and keys of an S3 request, and credential query parameters;
4. SECRET_PATTERNS: Anthropic keys (API keys and OAuth tokens), OpenAI keys, AWS access key ids, Slack tokens, Google
   API keys, Basic credentials of an Authorization header, and the value of a variable or key whose name says it holds
   a secret, unless an earlier step replaced it already. That last one is how the value of a lease shows in a
   session: ``evo-agents worker env`` prints a lease of kind env as ``export NAME='value'``, and the environment of a
   run as ``GITHUB_TOKEN=...``; it also takes an AWS secret key (``AWS_SECRET_ACCESS_KEY=``,
   ``aws_secret_access_key =``) and keys such as ``"client_secret": "..."``.

It errs on the side of removing: a value that only looks like a secret goes too. Standard library only: the hook runs
on a core install.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from urllib.parse import quote, unquote

from evo_agents.hub.log import MASK, MIN_SECRET_LENGTH, scrub

_PEM = re.compile(
    r"-----BEGIN (?P<kind>[A-Z0-9 ]*?)PRIVATE KEY-----(?:[\s\S]*?-----END (?P=kind)PRIVATE KEY-----|[\s\S]*\Z)"
)
# Names of variables and keys that hold a secret: an environment variable in upper case with one of these words in
# it, or a key in any case that ends with one.
_SECRET_WORDS = r"TOKEN|SECRET|PASSWORD|PASSWD|PASSPHRASE|API_?KEY|ACCESS_?KEY|SECRET_?KEY|PRIVATE_?KEY|CREDENTIALS?"
_NAMED = re.compile(
    r"""
    (?P<head>
        (?:\b[A-Z][A-Z0-9_]*?(?:"""
    + _SECRET_WORDS
    + r""")[A-Z0-9_]*\b                                           # GITHUB_TOKEN, AWS_SECRET_ACCESS_KEY
          |["']?\b(?i:[a-z0-9_]*?(?:token|secret|password|passwd|passphrase|api_?key|access_?key|secret_?key
                |private_?key|client_secret))["']?)                  # "token":, client_secret =, apiKey:
        \s*[=:]\s*
        (?P<quote>["']?)
    )
    (?!\*\*\*)[^\s"'`,;&|)}\]]{6,}                                 # not one replaced already
    """,
    re.VERBOSE,
)
SECRET_PATTERNS = (
    # Anthropic: API keys (sk-ant-api03-), OAuth tokens (sk-ant-oat01-), admin keys
    (re.compile(r"\b(?P<head>sk-ant-)[A-Za-z0-9_-]{8,}"), r"\g<head>" + MASK),
    # OpenAI: sk-, sk-proj-, sk-svcacct-, sk-admin- and a long tail
    (re.compile(r"\b(?P<head>sk-)(?!ant-)[A-Za-z0-9_-]{20,}"), r"\g<head>" + MASK),
    # AWS access key ids: long-term (AKIA), temporary (ASIA) and the others of the family
    (re.compile(r"\b(?:AKIA|ASIA|ABIA|ACCA|A3T[A-Z0-9])[A-Z0-9]{16}\b"), MASK),
    # Slack tokens: bot, user, app, refresh
    (re.compile(r"\b(?P<head>xox[abposr]-)[A-Za-z0-9-]{10,}"), r"\g<head>" + MASK),
    # Google API keys
    (re.compile(r"\bAIza[0-9A-Za-z_-]{35}"), MASK),
    # Basic credentials of an Authorization header
    (re.compile(r"(?P<head>\bAuthorization\s*:\s*Basic\s+)[A-Za-z0-9+/=]{8,}", re.IGNORECASE), r"\g<head>" + MASK),
    # The value of a variable or key named as a secret
    (_NAMED, r"\g<head>" + MASK),
)


def _forms(value: str) -> set[str]:
    """``value`` as a text may hold it: as it is, URL-encoded, and URL-decoded."""
    forms = {value, quote(value, safe="")}
    decoded = unquote(value)
    if decoded != value:
        forms.add(decoded)
    return {form for form in forms if len(form) >= MIN_SECRET_LENGTH}


def _pem(found: re.Match) -> str:
    kind = found["kind"]
    return f"-----BEGIN {kind}PRIVATE KEY-----{MASK}-----END {kind}PRIVATE KEY-----"


def secret_forms(values: Iterable[str | None]) -> list[str]:
    """The forms of ``values`` to replace, longest first, so a value inside another goes with it."""
    forms = set()
    for value in values:
        if isinstance(value, str) and len(value) >= MIN_SECRET_LENGTH:
            forms |= _forms(value)
    return sorted(forms, key=len, reverse=True)


def redact(text: str, values: Iterable[str | None] = ()) -> str:
    """``text`` with ``values`` and everything that looks like a secret replaced by ``***``."""
    return _redact(text, secret_forms(values))


def _redact(text: str, forms: list[str]) -> str:
    for form in forms:
        if form in text:
            text = text.replace(form, MASK)
    text = scrub(_PEM.sub(_pem, text))
    for pattern, replacement in SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def redact_data(value, values: Iterable[str | None] = ()):
    """``value`` (JSON data) with every string in it, keys included, redacted as ``redact`` does."""
    return _redact_data(value, secret_forms(values))


def _redact_data(value, forms: list[str]):
    if isinstance(value, str):
        return _redact(value, forms)
    if isinstance(value, dict):
        return {_redact(str(key), forms): _redact_data(item, forms) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_redact_data(item, forms) for item in value]
    return value
