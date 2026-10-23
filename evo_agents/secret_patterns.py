"""Patterns that find secrets in text, shared by the hub and the knowledge graph.

The hub masks what they match with ``***``: in a session digest before the Stop hook of the evo-hub plugin sends it
(``evo_agents.hub.redact``) and in every log line it writes (``evo_agents.hub.log``). The git connector of the
knowledge graph replaces it with a marker naming its kind before a file enters the corpus (``evo_agents.kg.redact``).
A pattern names the part a mask keeps, such as a token's prefix, as group ``head``; ``PROVIDER_KEYS`` names the kind of
each key format.

Standard library only, and outside ``evo_agents.hub``: the hook, the hub's logging and the knowledge graph all run on
a core install, and ``evo_agents.kg`` may not import ``evo_agents.hub``.
"""

from __future__ import annotations

import re

# A private key in PEM, from its BEGIN line to its END line, or to the end of the text when it was cut before that.
PEM_PRIVATE_KEY = re.compile(
    r"-----BEGIN (?P<kind>[A-Z0-9 ]*?)PRIVATE KEY-----(?:[\s\S]*?-----END (?P=kind)PRIVATE KEY-----|[\s\S]*\Z)"
)

# Key formats of providers
ANTHROPIC_KEY = re.compile(r"\b(?P<head>sk-ant-)[A-Za-z0-9_-]{8,}")  # API keys (sk-ant-api03-), OAuth (sk-ant-oat01-)
OPENAI_KEY = re.compile(r"\b(?P<head>sk-)(?!ant-)[A-Za-z0-9_-]{20,}")  # sk-, sk-proj-, sk-svcacct-, sk-admin-
AWS_ACCESS_KEY_ID = re.compile(r"\b(?:AKIA|ASIA|ABIA|ACCA|A3T[A-Z0-9])[A-Z0-9]{16}\b")  # long-term and temporary
SLACK_TOKEN = re.compile(r"\b(?P<head>xox[abposr]-)[A-Za-z0-9-]{10,}")  # bot, user, app, refresh
GOOGLE_API_KEY = re.compile(r"\bAIza[0-9A-Za-z_-]{35}")
GITHUB_TOKEN = re.compile(r"\b(?P<head>gh[opsur]_|github_pat_)[A-Za-z0-9_]{16,}")  # every kind of GitHub token
HUB_TOKEN = re.compile(r"\b(?P<head>ev[hswin]_)[A-Za-z0-9_-]{8,}")  # hub, web, worker, signal ingest and invite tokens

# The password of a URI, scheme://user:password@host, any scheme
URI_PASSWORD = re.compile(r"(?P<head>\b[A-Za-z][A-Za-z0-9+.-]*://[^:/@\s]*:)(?P<value>[^@\s/]+)@")

PROVIDER_KEYS = (
    ("anthropic_key", ANTHROPIC_KEY),
    ("openai_key", OPENAI_KEY),
    ("aws_key_id", AWS_ACCESS_KEY_ID),
    ("slack_token", SLACK_TOKEN),
    ("google_api_key", GOOGLE_API_KEY),
    ("github_token", GITHUB_TOKEN),
    ("hub_token", HUB_TOKEN),
)
