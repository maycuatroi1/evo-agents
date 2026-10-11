"""Secrets out of a file's text before it enters the corpus: each value becomes a marker that names its kind.

The git connector passes the text of every file it reads through ``redact_file``. In this order, each step leaving
alone what an earlier one replaced:

1. a private key in PEM, the whole block from its BEGIN line to its END line, when it holds at least
   ``PEM_MIN_BODY`` characters of base64: ``[redacted:pem]``;
2. a key in the format of a provider (``evo_agents.secret_patterns.PROVIDER_KEYS``, shared with the hub): Anthropic
   and OpenAI keys, AWS access key ids, Slack tokens, Google API keys, GitHub tokens and hub tokens, such as
   ``[redacted:github_token]``;
3. the password of a URI: ``postgres://app:[redacted:uri_password]@db:5432/app``;
4. the literal value of a variable or key whose name ends with the word password, passwd, passphrase, secret, token,
   api_key, access_key or private_key, in any case, after ``_``, ``-``, ``.`` or a lower-to-upper case change:
   ``DB_PASSWORD: [redacted:password]``, ``"apiKey": "[redacted:api_key]"``.

What is not a secret stays, since the patterns favour precision over recall:

- a reference to a secret: ``${VAR}``, ``$VAR``, ``{{ secrets.X }}``, ``%(name)s``, ``os.environ[...]``,
  ``process.env.X``;
- an empty value, a placeholder (``xxx``, ``***``, ``...``, ``<your-token>``, ``changeme``, ``example``, a value
  with ``test``, ``fake`` or ``dummy`` in it, one character repeated) and the start of a value the code concatenates
  (``"sk-ant-" + ...``);
- words that describe a secret: a label with spaces, a type name, a path, a URL, a number, a value that itself says
  password, secret, token or key;
- in code, anything but a quoted literal, since what follows ``=`` unquoted is an expression; config files, shell
  scripts and documents take a bare value that runs to the end of the line too;
- a short value: under 8 characters, or, for a value of letters only (a word, ``CONSTANT_NAME``), anything outside a
  config file or shell script under an upper-case name such as ``DB_PASSWORD``, and there under 4 characters or
  equal to a word of the name (``POSTGRES_PASSWORD: postgres``, a well-known default); a URI password the same way,
  and equal to its user name (``guest:guest``);
- a well-known default (``admin``, ``root``, ``guest``, ``minioadmin``);
- a provider key without a digit and a letter in it, or shorter than its format (``MIN_PROVIDER_LENGTH``).

The rest of the text is kept as it is, so the file's structure still enters the graph. The counts by kind let the
sync's audit line say what went, never the values.
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import PurePosixPath

from evo_agents.kg.connectors._files import CONFIG_NAMES, KIND_BY_SUFFIX
from evo_agents.secret_patterns import PROVIDER_KEYS, URI_PASSWORD

MARKER = "[redacted:{}]"
MARKER_PREFIX = "[redacted:"
PEM_MIN_BODY = 64  # base64 characters of a key block at least; a shorter one is an example in a document
MIN_LENGTH = 8  # characters of a secret that is not a plain word
MIN_PLAIN_LENGTH = 4  # characters of a plain word that counts as a secret in a config file
# Characters of a provider key at least, prefix included: AWS access key ids have 20, Anthropic and OpenAI keys far
# more than 40, so a shorter one is made up, as in a test.
MIN_PROVIDER_LENGTH = {"anthropic_key": 40, "openai_key": 40}
MIN_PROVIDER_LENGTH_DEFAULT = 20

PEM_BLOCK = re.compile(
    r"-----BEGIN (?P<kind>(?:[A-Z0-9]+ )*)PRIVATE KEY(?P<block>(?: BLOCK)?)-----"
    r"(?P<body>(?:[A-Za-z0-9+/=\s]|\\[nrt]|(?:Proc-Type|DEK-Info|Comment|Version|Hash):[^\n\\]*)*?)"
    r"-----END (?P=kind)PRIVATE KEY(?P=block)-----"
)
NAMED = re.compile(
    r"""
    (?<![\w.-])
    (?P<name>
        (?:[a-z_][\w.-]*?(?:[_.-]|(?-i:(?<=[a-z0-9])(?=[A-Z]))))?
        (?:pass(?:word|wd|phrase)|secret(?:[_-]?(?:access[_-]?)?key)?|private[_-]?key|api[_-]?key|access[_-]?key|token)
    )
    ["']?[ \t]*(?:=>|:=|=(?!=)|:(?![:=]))[ \t]*
    (?:
        (?P<quote>["'`])(?P<quoted>(?:\\.|(?!(?P=quote))[^\\\n])*)(?P=quote)
      | (?P<bare>[^\s"'`,;\#{}()\[\]]+)(?=["']?[ \t\r]*(?:[\#;].*)?$)
    )
    """,
    re.VERBOSE | re.IGNORECASE | re.MULTILINE,
)
NOT_SECRET = (
    # a reference to a secret
    re.compile(r"\$[{(A-Za-z_]|[{}]|%\(|^%[sdr]$|<%|^!|^ENC\[|process\.env|os\.environ|getenv|^env[.:(\[]", re.I),
    # a placeholder, a value made up for a test, one character repeated
    re.compile(
        r"x{3,}|\*{2,}|\.{3}|\u2026|^<.*>$|your|example|sample|placeholder|change[_-]?me|dummy|fake|mock|test"
        r"|redacted|^(.)\1+$|^(?:admin|root|guest|minioadmin)$",
        re.I,
    ),
    # a value that says what it is: a name, a label (mat khau is Vietnamese for password)
    re.compile(r"pass|p[a@4]ssw|s3cr|secret|token|key|private|credential|mat[_-]?khau", re.I),
    # a path or a URL
    re.compile(r"^(?:/|\./|\.\./|~/)|://|^[\w.-]+\.(?:pem|key|crt|cer|p12|pfx|jks|json|txt|ya?ml|env)$", re.I),
    # a number, a rate, a type, a flag
    re.compile(
        r"^[-+]?\d+(?:\.\d+)?(?:e[-+]?\d+)?$|^\d+/[a-z]+$|^(?:none|null|nil|true|false|undefined|required|optional"
        r"|string|str|int|number|bool|boolean|any|bytes|text|read|write)$",
        re.I,
    ),
)
_PLAIN = re.compile(r"_*[A-Za-z]+(?:[_.-][A-Za-z]+)*_*")
_UPPER_NAME = re.compile(r"[A-Z][A-Z0-9_]*")
_SEPARATORS = re.compile(r"[_.-]")


def redact_file(text: str, path: str) -> tuple[str, dict[str, int]]:
    """``text`` of the file at ``path`` with each secret replaced by ``[redacted:<kind>]``, and the count by kind."""
    found: Counter[str] = Counter()
    style = file_style(path)
    text = PEM_BLOCK.sub(lambda m: _pem(m, found), text)
    for kind, pattern in PROVIDER_KEYS:
        text = pattern.sub(lambda m, kind=kind: _provider_key(m, kind, found), text)
    text = URI_PASSWORD.sub(lambda m: _uri_password(m, style, found), text)
    text = NAMED.sub(lambda m: _named(m, style, found), text)
    return text, dict(sorted(found.items()))


def file_style(path: str) -> str:
    """``code``, where a bare value after ``=`` is an expression; ``config`` (config files, shell scripts), where it is
    a literal and a plain word under an upper-case name may be a password; ``doc`` (documents), where it is a
    literal too."""
    pure = PurePosixPath(path)
    kind = KIND_BY_SUFFIX.get(pure.suffix.lower())
    if pure.suffix.lower() == ".sh" or kind in ("yaml", "config") or pure.name in CONFIG_NAMES:
        return "config"
    return "code" if kind == "code" else "doc"


def is_literal_secret(value: str, *, plain_ok: bool = False) -> bool:
    """Whether ``value``, given to a name that says it holds a secret, is the secret itself rather than a reference
    to it, a placeholder, or words that describe it. ``plain_ok``: a plain word counts, as under ``DB_PASSWORD`` in a
    config file."""
    if not value or value.startswith(MARKER_PREFIX) or any(c.isspace() for c in value) or value[-1] in "-_.":
        return False
    if any(pattern.search(value) for pattern in NOT_SECRET):
        return False
    if _PLAIN.fullmatch(value):
        return plain_ok and len(value) >= MIN_PLAIN_LENGTH
    return len(value) >= MIN_LENGTH


def name_kind(name: str) -> str:
    """The kind of secret a variable or key holds, by the last word of its name."""
    letters = re.sub(r"[^a-z]", "", name.lower())
    if letters.endswith("privatekey"):
        return "private_key"
    if letters.endswith(("password", "passwd", "passphrase")):
        return "password"
    if letters.endswith(("secret", "secretkey", "secretaccesskey")):
        return "secret"
    if letters.endswith("token"):
        return "token"
    return "api_key"


def _pem(match: re.Match, found: Counter) -> str:
    if len(re.sub(r"\\[nrt]|[^A-Za-z0-9+/=]", "", match["body"])) < PEM_MIN_BODY:
        return match[0]
    found["pem"] += 1
    return MARKER.format("pem")


def _provider_key(match: re.Match, kind: str, found: Counter) -> str:
    value = match[0]
    if len(value) < MIN_PROVIDER_LENGTH.get(kind, MIN_PROVIDER_LENGTH_DEFAULT):
        return value
    if not (re.search(r"[0-9]", value) and re.search(r"[A-Za-z]", value)):
        return value
    head = match.groupdict().get("head") or ""
    if any(pattern.search(value[len(head) :]) for pattern in NOT_SECRET[1:3]):
        return value
    found[kind] += 1
    return MARKER.format(kind)


def _uri_password(match: re.Match, style: str, found: Counter) -> str:
    value, user = match["value"], match["head"].rpartition("://")[2][:-1]
    if value == user or not is_literal_secret(value, plain_ok=style == "config"):
        return match[0]
    found["uri_password"] += 1
    return f"{match['head']}{MARKER.format('uri_password')}@"


def _named(match: re.Match, style: str, found: Counter) -> str:
    quoted = match["quote"] is not None
    if not quoted and style == "code":
        return match[0]
    value = match["quoted"] if quoted else match["bare"]
    name = match["name"].rsplit(".", 1)[-1]
    words = _SEPARATORS.split(name.lower())
    plain_ok = style == "config" and bool(_UPPER_NAME.fullmatch(name)) and value.lower() not in words
    if not is_literal_secret(value, plain_ok=plain_ok):
        return match[0]
    kind = name_kind(name)
    found[kind] += 1
    start = match.start("quoted" if quoted else "bare") - match.start()
    return match[0][:start] + MARKER.format(kind) + match[0][start + len(value) :]
