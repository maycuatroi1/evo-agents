"""Which member an email belongs to: an address a member listed on the hub (the table user_emails, schema 0065), or
GitHub's noreply address of a login, which needs no row.

GitHub gives every account a noreply address, ``ID+LOGIN@users.noreply.github.com`` (older accounts
``LOGIN@users.noreply.github.com``), and writes it as the author of the commits made on github.com when the account
keeps its email private. ``login_of_email`` names the login of such an address without asking anyone. Addresses are
compared without case, as the table's unique index compares them. Standard library only: the CLI checks an address
with ``EMAIL`` before it asks the hub.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

MAX_EMAIL_CHARS = 254  # the longest address SMTP carries (RFC 5321); user_emails_email_check (0065)
EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")  # one @, no space, a dot in the domain; user_emails_email_check
MAX_EMAILS = 20  # a member's rows: an address is taken unchecked, so one member cannot hold the hub's addresses
NOREPLY_DOMAIN = "users.noreply.github.com"
# ID+LOGIN or LOGIN at GitHub's noreply domain; LOGIN as evo_agents.hub.config.LOGIN accepts it
NOREPLY = re.compile(r"(?:[0-9]{1,20}\+)?([A-Za-z0-9][A-Za-z0-9_-]{0,99})@users\.noreply\.github\.com", re.IGNORECASE)


def email_key(email: str) -> str:
    """``email`` as the hub compares it: without the spaces around it, in lower case."""
    return email.strip().lower()


def email_problem(email: str) -> str | None:
    """Why ``email`` cannot be listed as a member's address; None when it can."""
    if len(email) > MAX_EMAIL_CHARS:
        return f"an email address is at most {MAX_EMAIL_CHARS} characters"
    if not EMAIL.fullmatch(email):
        return f"{email!r} is not an email address: it takes one @, no space and a dot in its domain"
    return None


def noreply_login(email: str) -> str | None:
    """The login GitHub's noreply address ``email`` names, as written there; None for any other address."""
    found = NOREPLY.fullmatch(email.strip())
    return found[1] if found else None


def login_of_email(email: str, known: Mapping[str, str]) -> str | None:
    """The login of the member ``email`` belongs to: the one whose row holds it in ``known`` (``email_key`` of each
    listed address to its member's login), else the login a GitHub noreply address names; None when neither does.
    A listed address wins over the login its noreply form names."""
    listed = known.get(email_key(email))
    return listed if listed is not None else noreply_login(email)
