"""A decision that asks a run's owner for a credential the run lacks (``docs/credentials.md``, "A run that asks for a
credential"): a variable of the agent's environment, such as an API key, the owner's account on the hub, or both.

The agent asks it with ``evo-agents worker ask --category credential --env-var NAME [--secret-name NAME]
[--hub-account]``: the request (``env_var``, ``secret_name``, ``hub_account``) is stored with the decision, and the hub
gives it the options of ``options``, never the agent's own. The owner answers with an option, and for the variable with
its value, which the command line reads from stdin or asks for without echo (``evo-agents hub decision answer ID
--secret``) and the web takes in a password field: the hub keeps it as the owner's secret of kind env for the run's
project (``evo_agents.hub.server.credentials.service.answers``), and the decision, its answer, the run's inbox, events, logs and
audit never hold it. The worker takes the run's leases again once the answer reaches the run, so its agent reads the
value with ``evo-agents worker env --run N``; a parked run is leased it when it resumes.

Standard library only: the hub, the worker's ``worker ask`` and the daemon all read it.
"""

from __future__ import annotations

import re

from evo_agents.hub.credentials import MAX_SECRET_NAME_CHARS, SECRET_NAME, env_name_refusal

CATEGORY = "credential"  # the decision's category, of autonomy_card.DECISION_CATEGORIES (K8)
MAX_ENV_VAR_CHARS = 100  # the variable a request names: its options' labels repeat it, one line of 200 characters
# The options the hub gives a request: the value pasted, the owner's account on the hub, both, or nothing.
VALUE = "value"
AS_ME = "as_me"
BOTH = "both"
REFUSE = "refuse"
KEYS = (BOTH, VALUE, AS_ME, REFUSE)
# The option the hub adds when the owner holds a secret of kind env for the variable already (plan
# hub-attention-runtime, a2; ``evo_agents.hub.server.credentials.service.resolver``): the hub chose it when it answered the
# request itself, and the owner chooses it to bind that secret to the run's project and lease it, nothing pasted.
FOUND = "found"
NOTED = "credential"  # the key `worker ask` sets in the run's file of decisions for a request, for the daemon
# What the agent runs to read what the run's leases add to its environment once an answer gave it something.
ENV_COMMAND = 'eval "$(evo-agents worker env --run "$EVO_RUN_ID")"'

_NOT_A_NAME = re.compile(r"[^a-z0-9._-]+")


def default_secret_name(env_var: str) -> str:
    """The name of the secret that keeps the value of ``env_var`` when the agent proposes none: the variable in lower
    case with ``-`` for ``_``, as a secret's name may be (``credentials.SECRET_NAME``)."""
    name = _NOT_A_NAME.sub("-", env_var.lower().replace("_", "-")).strip("-._")[:MAX_SECRET_NAME_CHARS].rstrip("-._")
    return name or "secret"


def request_refusal(env_var: str | None, secret_name: str | None, hub_account: bool) -> str | None:
    """Why a request for ``env_var`` (kept as ``secret_name``) and, with ``hub_account``, the owner's account on the hub
    cannot be asked; None when it can."""
    if env_var is None and not hub_account:
        return (
            "a credential request names the variable the run needs (env_var), the owner's hub account (hub_account), "
            "or both"
        )
    if env_var is None:
        if secret_name is not None:
            return "secret_name names the secret that keeps the value of env_var: name the variable too"
        return None
    if len(env_var) > MAX_ENV_VAR_CHARS:
        return f"env_var is at most {MAX_ENV_VAR_CHARS} characters"
    refusal = env_name_refusal(env_var)
    if refusal is not None:
        return refusal
    if secret_name is not None and not SECRET_NAME.fullmatch(secret_name):
        return (
            f"secret_name {secret_name!r} is not a secret's name: lower case letters, digits, '.', '_' and '-', at "
            f"most {MAX_SECRET_NAME_CHARS}, starting with a letter or a digit"
        )
    return None


def options(env_var: str | None, secret_name: str | None, hub_account: bool) -> list[dict]:
    """The options of a request, as a decision stores them (key, label, description): the ones that give what it asks
    for, then REFUSE."""
    value = None
    if env_var is not None:
        name = secret_name or default_secret_name(env_var)
        value = (
            f"Paste the value: the hub keeps it as your secret {name} of kind env for this run's project, never shows "
            "it again, and the run reads it at once. Answer with `evo-agents hub decision answer ID --secret`, which "
            "reads it from stdin or asks for it without echo, or in the password field on the web."
        )
    account = (
        "The run calls the hub's API as you, with your grants on every project, secrets, tokens and workers aside, "
        "until it ends or you stop it with `evo-agents hub run access RUN --revoke`."
    )
    found = []
    if env_var is not None and hub_account:
        both = f"Paste the value of {env_var} and let the run use my hub account"
        found.append({"key": BOTH, "label": both, "description": f"{value} {account}"})
    if env_var is not None:
        found.append({"key": VALUE, "label": f"Paste the value of {env_var}", "description": value})
    if hub_account:
        found.append({"key": AS_ME, "label": "Let the run use my hub account", "description": account})
    refuse = "The run gets nothing: its agent goes on without it, or asks again with what it found."
    found.append({"key": REFUSE, "label": "Refuse", "description": refuse})
    return found


def gives_value(option: str | None) -> bool:
    """Whether choosing ``option`` gives the variable's value, which the answer carries."""
    return option in (VALUE, BOTH)


def binds_found(option: str | None) -> bool:
    """Whether choosing ``option`` binds the owner's secret the hub found for the variable (FOUND)."""
    return option == FOUND


def grants_account(option: str | None) -> bool:
    """Whether choosing ``option`` lets the run run as its owner on the hub (``evo_agents.hub.server.run_access``)."""
    return option in (AS_ME, BOTH)


# A question in words that wants a credential's value (plan hub-attention-runtime, a1). The agent of a plan run or an
# author run asks for a credential with a credential request, whose answer the hub keeps as a secret and leases to the
# run; asked in prose, as scope, question or architecture, the owner can only answer by pasting the value into a
# decision, so the route refuses it and names the command that asks it right (``prose_credential``).
PROSE_CATEGORIES = ("scope", "question", "architecture")
# A variable as an environment names it (upper case words joined by _) with a word of a credential among them:
# OPENAI_API_KEY, BOT_TOKEN, DB_PASSWORD. A key of a table or a cache (PRIMARY_KEY, CACHE_KEY) is none.
_ENV_NAME = re.compile(r"(?<![A-Za-z0-9_])[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+(?![A-Za-z0-9_])")
_CREDENTIAL_PARTS = frozenset(
    ("KEY", "KEYS", "APIKEY", "TOKEN", "TOKENS", "SECRET", "SECRETS", "PASSWORD", "PASSWD", "PASS")
)
_NOT_CREDENTIAL_KEYS = frozenset(
    (
        "PRIMARY",
        "FOREIGN",
        "UNIQUE",
        "COMPOSITE",
        "SORT",
        "PARTITION",
        "CACHE",
        "IDEMPOTENCY",
        "LOOKUP",
        "HASH",
        "ROUTING",
    )
)
# A credential named in words: it counts only next to the act of giving its value (_GIVES).
_CREDENTIAL_WORDS = re.compile(
    r"\b(?:api[ _-]?keys?|access[ _-]?keys?|secret[ _-]?keys?|private[ _-]?keys?|ssh[ _-]?keys?|deploy[ _-]?keys?|"
    r"(?:bot|access|api|auth|personal[ _-]access|bearer|refresh|github|gitlab|telegram|slack|npm|pypi)?[ _-]?tokens?|"
    r"passwords?|passphrases?|credentials?|client[ _-]?secrets?|"
    r"sign[ _-]?in|log[ _-]?in|đăng[ ]nhập|mật[ ]khẩu)\b",
    re.IGNORECASE,
)
_GIVES = re.compile(
    r"\b(?:provide|give|paste|share|send|supply|enter|type|set|export|add|put|value|"
    r"cung[ ]cấp|đưa|gửi|dán|nhập|cấp|giá[ ]trị|điền)\b",
    re.IGNORECASE,
)


# Tokens a model reads and writes, not a credential (run 8919 was refused for "set ... the Token strategy"): counted
# ("515 tokens", "200k tokens", "more tokens", "output tokens"), the word before a noun it qualifies ("token budget",
# "Token strategy", "the token ceiling"), joined to a word by a hyphen ("token-and-quota-economy", "token-based"), or,
# in Vietnamese, after the noun it qualifies ("chiến lược token", "ngân sách token"). Removed before
# _CREDENTIAL_WORDS, so "the token ceiling" asks for nothing while "the token" or "the GitHub token" still does.
_TOKEN_NOUNS = (
    r"strateg(?:y|ies)|budgets?|ceilings?|caps?|limits?|quotas?|econom(?:y|ics)|usage|spend(?:ing)?|costs?|"
    r"pric(?:e|es|ing)|counts?|counting|counters?|accounting|windows?|rates?|per|savings?|efficiency|consumption|"
    r"pacing|routing|polic(?:y|ies)|plans?|modes?|reports?|metrics?|stats|statistics|totals?|burn|throughput|"
    r"estimates?|bounds?|thresholds?|allowances?|ratios?|overhead|footprint|bills?|billing|trims?|reductions?|growth|"
    r"length|size|math"
)
_TOKEN_MEASURES = (
    r"\d[\d.,]*\s*[km]?|more|fewer|extra|enough|million|thousand|input|output|prompt|completion|cached|cache|"
    r"reasoning|thinking|context|max|total"
)
_VI_TOKEN_MEASURES = r"chiến lược|ngân sách|trần|hạn mức|giới hạn|số|lượng|chi phí|mức|tiết kiệm|tốn|tiêu thụ|tiêu tốn"
_COUNTED_TOKENS = re.compile(
    rf"\b(?:{_TOKEN_MEASURES})[ _-]?tokens?\b|\btokens?(?:[ _]+(?:{_TOKEN_NOUNS})\b|-\w)|\btokens[ _]+(?:used|spent)\b|"
    rf"\b(?:{_VI_TOKEN_MEASURES}) tokens?\b",
    re.IGNORECASE,
)
# A route of the hub's API names a method, not the act of giving: "PUT /v1/projects/{p}/policy".
_ROUTE = re.compile(r"\b(?:GET|HEAD|POST|PUT|PATCH|DELETE)\s+/\S*")
# The sentences of a question: a credential in words counts only with a word of giving in its own sentence.
_SENTENCE_END = re.compile(r"(?<=[.!?;])\s+|\n+")


def _asks_value(text: str) -> tuple[str, str] | None:
    """The credential in words (_CREDENTIAL_WORDS, counted tokens aside) and the word of giving its value (_GIVES,
    routes aside, and outside the credential's words, so "đăng nhập" alone gives nothing) that a sentence of ``text``
    holds together, lower case; None when no sentence holds both. Both come from the two fixed vocabularies, never
    from the rest of the text."""
    for sentence in _SENTENCE_END.split(text):
        words = _ROUTE.sub(" ", _COUNTED_TOKENS.sub(" ", sentence))
        credential, gives = _CREDENTIAL_WORDS.search(words), _GIVES.search(_CREDENTIAL_WORDS.sub(" ", words))
        if credential and gives:
            return " ".join(_SEPARATORS.split(credential.group().lower())).strip(), gives.group().lower()
    return None


_SEPARATORS = re.compile(r"[\s_-]+")


def _names_credential(name: str) -> bool:
    """Whether the variable ``name`` says it holds a credential: one of its words is in _CREDENTIAL_PARTS, and a KEY
    is not the key of a table or a cache."""
    parts = name.split("_")
    for at, part in enumerate(parts):
        if part in _CREDENTIAL_PARTS and not (part.startswith("KEY") and at and parts[at - 1] in _NOT_CREDENTIAL_KEYS):
            return True
    return False


ASK_COMMAND = "evo-agents worker ask --category credential --env-var NAME"


def prose_credential(category: str, question: str, context: str | None) -> str | None:
    """Why a decision of ``category`` with ``question`` and ``context`` cannot be asked, when it is in PROSE_CATEGORIES
    and asks for a credential's value in words: a variable named as an environment names a credential, or a credential
    in words next to giving its value. The reason names ASK_COMMAND, with the variable for NAME when the text names
    one, or the two words it read together, so an agent whose question asks for no credential sees what to reword.
    None for any other decision. The reason repeats the variable's name or those words alone, never the text they
    were found in."""
    if category not in PROSE_CATEGORIES:
        return None
    text = f"{question}\n{context or ''}"
    named = next((name for name in _ENV_NAME.findall(text) if _names_credential(name)), None)
    words = None if named else _asks_value(text)
    if named is None and words is None:
        return None
    command = ASK_COMMAND.replace("NAME", named) if named else ASK_COMMAND
    read = f' (it read "{words[0]}" and "{words[1]}" in one sentence)' if words else ""
    return (
        f"a {category} decision does not ask for a credential's value{read}: ask for it with `{command}` (add "
        "`--hub-account` for the owner's account on the hub), so the owner gives it in a password field, the hub keeps "
        f"it as their secret, and the run reads it with `{ENV_COMMAND}`"
    )
