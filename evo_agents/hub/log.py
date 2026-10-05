"""Logging for the hub server: one JSON object per line on stderr, with secrets removed.

Secrets are removed when a record is formatted, so a library that logs a DSN or a token is covered too:
every value registered with ``register_secret`` (the DSN password, the Sentry DSN, the GitHub client secret, the
session secret, the S3 key pair) becomes ``***``, and so do the password of any URI or ``password=`` pair, hub tokens
(``evh_...``) and web sessions (``evs_...``), GitHub tokens (``gho_...``, ``github_pat_...``), bearer credentials,
the signature, access key id and session token of an S3 request or presigned URL, which is a bearer credential
until it expires, and the value of every query parameter in ``QUERY_SECRETS`` (the OAuth code and state of a web
sign-in callback among them), whichever logger wrote the URL. Worker tokens (``evw_...``) are masked as hub tokens are,
and a worker pairing code as the hub shows it (``XXXX-XXXX``, upper case Crockford base32) becomes ``***`` whole; the
parts of a UUID are left alone.
Standard library only: the CLI configures logging before it knows whether the hub-server extra is there.
"""

from __future__ import annotations

import json
import logging
import re
import sys
import threading
from datetime import datetime, timezone
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit, urlunsplit

MASK = "***"
MIN_SECRET_LENGTH = 3  # shorter values would mask ordinary words; DSN passwords are never that short in use

_secrets: set[str] = set()
_secrets_lock = threading.Lock()

# Query parameters whose value is a credential, matched by name after ``?``, ``&`` or ``&amp;``, any case.
QUERY_SECRETS = (
    "code",
    "state",
    "code_verifier",
    "access_token",
    "refresh_token",
    "token",
    "client_secret",
    "X-Amz-Signature",
    "X-Amz-Credential",
    "X-Amz-Security-Token",
)

_PATTERNS = (
    # scheme://user:password@host, any scheme
    (re.compile(r"(?P<head>\b[A-Za-z][A-Za-z0-9+.-]*://[^:/@\s]*:)[^@\s/]+@"), r"\g<head>" + MASK + "@"),
    # password=... in a libpq key=value DSN or a query string, quoted or bare
    (re.compile(r"(?P<head>\bpassword\s*=\s*)(?:'(?:[^'\\]|\\.)*'|[^\s&]+)", re.IGNORECASE), r"\g<head>" + MASK),
    (re.compile(r"\b(?P<head>ev[hsw]_)[A-Za-z0-9_-]{8,}"), r"\g<head>" + MASK),
    # a pairing code, XXXX-XXXX of Crockford base32 (no I, L, O or U), standing on its own
    (re.compile(r"(?<![A-Za-z0-9_-])[0-9A-HJKMNP-TV-Z]{4}-[0-9A-HJKMNP-TV-Z]{4}(?![A-Za-z0-9_-])"), MASK),
    (re.compile(r"\b(?P<head>gh[opsur]_|github_pat_)[A-Za-z0-9_]{16,}"), r"\g<head>" + MASK),
    (re.compile(r"(?P<head>\bBearer\s+)[A-Za-z0-9._~+/=-]{8,}", re.IGNORECASE), r"\g<head>" + MASK),
    # SigV4: X-Amz-Signature= and X-Amz-Credential= of a presigned URL, Signature= and Credential= of a header,
    # and the "Signature:" line botocore logs at debug level
    (re.compile(r"(?P<head>\bSignature(?:=|:\s*))[0-9A-Fa-f]{16,}"), r"\g<head>" + MASK),
    (re.compile(r"(?P<head>\bCredential=)[A-Za-z0-9]+"), r"\g<head>" + MASK),
    (re.compile(r"(?P<head>\bX-Amz-Security-Token=)[^&\s\"']+", re.IGNORECASE), r"\g<head>" + MASK),
    (
        re.compile(r"(?P<head>(?:[?&]|&amp;)(?:" + "|".join(map(re.escape, QUERY_SECRETS)) + r")=)[^&#\s\"'<>]+", re.I),
        r"\g<head>" + MASK,
    ),
)

QUIET_LOGGERS = {
    "alembic.runtime.plugins": logging.WARNING,  # a line per Alembic plugin on every import
    # A line per outbound request with its full URL, query string included; the GitHub client logs its own line per
    # call, with the path only. mcp installs httpx2 and httpcore2, the successors, which Starlette's TestClient prefers.
    "httpx": logging.WARNING,
    "httpcore": logging.WARNING,
    "httpx2": logging.WARNING,
    "httpcore2": logging.WARNING,
    # boto3 at debug level logs every request with its headers and body; the blob store logs its own lines.
    "boto3": logging.WARNING,
    "botocore": logging.WARNING,
    "s3transfer": logging.WARNING,
    "urllib3": logging.WARNING,
    "mcp.server.streamable_http": logging.WARNING,  # a line after every stateless /mcp request
}

# Attributes every LogRecord has; anything else on a record came from ``extra=`` and goes into the JSON.
_RECORD_ATTRS = set(vars(logging.LogRecord("", 0, "", 0, "", None, None))) | {"message", "asctime", "color_message"}


def register_secret(value: str | None) -> None:
    """Never log ``value``: every formatted line has it replaced by ``***``."""
    if not value or len(value) < MIN_SECRET_LENGTH:
        return
    with _secrets_lock:
        _secrets.add(value)
        decoded = unquote(value)
        if decoded != value and len(decoded) >= MIN_SECRET_LENGTH:
            _secrets.add(decoded)
        _secrets.add(quote(value, safe=""))


def scrub(text: str) -> str:
    with _secrets_lock:
        secrets = sorted(_secrets, key=len, reverse=True)
    for secret in secrets:
        if secret in text:
            text = text.replace(secret, MASK)
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def scrub_data(value):
    """``value`` with secrets removed from every string in it; other objects become scrubbed strings."""
    if isinstance(value, str):
        return scrub(value)
    if isinstance(value, bool | int | float) or value is None:
        return value
    if isinstance(value, dict):
        return {str(k): scrub_data(v) for k, v in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        return [scrub_data(v) for v in value]
    return scrub(str(value))


def dsn_password(dsn: str) -> str | None:
    """The password written in a postgresql:// URI or a key=value DSN, or None."""
    if "://" in dsn:
        parts = urlsplit(dsn)
        userinfo = parts.netloc.rpartition("@")[0]
        if ":" in userinfo:
            return userinfo.split(":", 1)[1] or None
        for key, value in parse_qsl(parts.query):
            if key == "password":
                return value or None
        return None
    match = re.search(r"\bpassword\s*=\s*('(?:[^'\\]|\\.)*'|\S+)", dsn)
    if not match:
        return None
    value = match.group(1)
    if value.startswith("'"):
        value = re.sub(r"\\(.)", r"\1", value[1:-1])
    return value or None


def redact_dsn(dsn: str) -> str:
    """The DSN with its password replaced by ``***``, for logs. Works on both libpq forms without parsing
    through libpq, whose error messages quote the DSN they could not parse."""
    if "://" in dsn:
        parts = urlsplit(dsn)
        userinfo, at, hostinfo = parts.netloc.rpartition("@")
        if ":" in userinfo:
            userinfo = userinfo.split(":", 1)[0] + ":" + MASK
        query = parts.query
        if query:
            pairs = [(k, MASK if k == "password" else v) for k, v in parse_qsl(query, keep_blank_values=True)]
            query = urlencode(pairs, safe="*")
        return urlunsplit((parts.scheme, userinfo + at + hostinfo, parts.path, query, parts.fragment))
    return re.sub(r"(\bpassword\s*=\s*)('(?:[^'\\]|\\.)*'|\S+)", r"\g<1>" + MASK, dsn)


class JsonFormatter(logging.Formatter):
    """``{"ts", "level", "logger", "msg", ...extra, "exc"}`` on one line, secrets removed."""

    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": scrub(record.getMessage()),
        }
        for key, value in record.__dict__.items():
            if key not in _RECORD_ATTRS and not key.startswith("_") and key not in entry:
                entry[key] = scrub_data(value)
        if record.exc_info:
            entry["exc"] = scrub(self.formatException(record.exc_info))
        if record.stack_info:
            entry["stack"] = scrub(self.formatStack(record.stack_info))
        return json.dumps(entry, ensure_ascii=False, default=str)


class _HubHandler(logging.StreamHandler):
    """Marks the handler this module installed, so configuring twice replaces it instead of adding one."""


def configure_logging(level: str | int = "INFO", stream=None) -> None:
    """Send every log record of the process, warnings included, to ``stream`` (stderr) as JSON lines."""
    root = logging.getLogger()
    for handler in list(root.handlers):
        if isinstance(handler, _HubHandler):
            root.removeHandler(handler)
    handler = _HubHandler(stream or sys.stderr)
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)
    root.setLevel(level.upper() if isinstance(level, str) else level)
    for name, quiet in QUIET_LOGGERS.items():
        logging.getLogger(name).setLevel(quiet)
    logging.captureWarnings(True)
