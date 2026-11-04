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
SLACK_TOKEN = re.compile(r"\b(?P<head>xox[abposr]-|xapp-)[A-Za-z0-9-]{10,}")  # bot, user, refresh; app-level (xapp-)
GOOGLE_API_KEY = re.compile(r"\bAIza[0-9A-Za-z_-]{35}")
GOOGLE_OAUTH_TOKEN = re.compile(r"\b(?P<head>ya29\.)[A-Za-z0-9_-]{20,}")  # OAuth access tokens
STRIPE_KEY = re.compile(r"\b(?P<head>[sr]k_live_)[A-Za-z0-9]{16,}")  # secret (sk_live_) and restricted (rk_live_) keys
GITLAB_TOKEN = re.compile(r"\b(?P<head>glpat-)[A-Za-z0-9_-]{20,}")  # personal access tokens
HUGGING_FACE_TOKEN = re.compile(r"\b(?P<head>hf_)[A-Za-z0-9]{30,}")
NPM_TOKEN = re.compile(r"\b(?P<head>npm_)[A-Za-z0-9]{30,}")
GITHUB_TOKEN = re.compile(r"\b(?P<head>gh[opsur]_|github_pat_)[A-Za-z0-9_]{16,}")  # every kind of GitHub token
# hub, web, worker, run, signal ingest, invite and join tokens
HUB_TOKEN = re.compile(r"\b(?P<head>ev[hswrinj]_)[A-Za-z0-9_-]{8,}")

# The password of a URI, scheme://user:password@host, any scheme
URI_PASSWORD = re.compile(r"(?P<head>\b[A-Za-z][A-Za-z0-9+.-]*://[^:/@\s]*:)(?P<value>[^@\s/]+)@")

PROVIDER_KEYS = (
    ("anthropic_key", ANTHROPIC_KEY),
    ("openai_key", OPENAI_KEY),
    ("aws_key_id", AWS_ACCESS_KEY_ID),
    ("slack_token", SLACK_TOKEN),
    ("google_api_key", GOOGLE_API_KEY),
    ("google_oauth_token", GOOGLE_OAUTH_TOKEN),
    ("stripe_key", STRIPE_KEY),
    ("gitlab_token", GITLAB_TOKEN),
    ("hugging_face_token", HUGGING_FACE_TOKEN),
    ("npm_token", NPM_TOKEN),
    ("github_token", GITHUB_TOKEN),
    ("hub_token", HUB_TOKEN),
)
