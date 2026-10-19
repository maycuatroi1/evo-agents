"""The template of a document: which headings and table rows are which kind of item, and what a code looks like.

``srs-basic`` is the only template of this release. It reads the common headings of an SRS (an Approval Page, a
Revision History, numbered sections, use cases, business rules, screens, a table of common business rules and one of
non-functional requirements) and holds no content of any client. A project replaces the pattern of a kind of code in
the plugin's config (``code_patterns``); the headings stay the template's.

A code is compared in its normalized form (``code_key``): runs of whitespace as one space, case folded, so ``UC ORD-1``
and ``uc  ord-1`` are one code. It is shown as written, whitespace tidied (``tidy``).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

from evo_agents.core_plugins.srs import CODED_KINDS, TEMPLATES
from evo_agents.hub.tables_srs import code_key

CODE_PATTERNS = {
    "use_case": r"^UC [A-Z]+-\d+$",
    "business_rule": r"^BR [A-Z]+\d+-\d+$",
    "screen": r"^SCR-\d+$",
    "nfr": r"^NFR-[A-Z]+-\d+$",
    "cbr": r"^CBR \d+$",
}
ROW_KINDS = ("cbr", "nfr")  # an item that is a row of a table, not a heading
SCREEN_PREFIX = "SCR-"

# Headings, as their text reads once the leading #s are gone.
USE_CASE = re.compile(r"^(?P<code>UC\s+[^\s:]+)\s*:\s*(?P<title>.+)$")
BUSINESS_RULE = re.compile(
    r"^(?:(?P<label>Steps?\s*\((?P<steps>[^)]*)\)[^:]*?)\s*:\s*)?"
    r"(?P<code>BR\s+\S+)\s+(?P<dash>[–—-])\s+(?P<title>.+)$"
)
SCREEN = re.compile(r"^Screen\s*:\s*(?P<title>.+)$", re.IGNORECASE)
APPROVAL_PAGE = re.compile(r"^approval\s+page$", re.IGNORECASE)
REVISION_HISTORY = re.compile(r"^revision\s+history$", re.IGNORECASE)
NUMBERED = re.compile(r"^\d+(?:\.\d+)*\.?\s+\S")
NAMED_SECTION = re.compile(r"^(?:actors?\b|site\s+map\b|open\s+items?\b|pending\s+points?\b)", re.IGNORECASE)
GLOSSARY = re.compile(r"\b(?:glossary|definitions?|terms|abbreviations|acronyms)\b", re.IGNORECASE)
CBR_SECTION = re.compile(r"common\s+business\s+rules", re.IGNORECASE)
ANCHOR = re.compile(r"^<!--\s*srs:\s*(?P<code>[^<>]+?)\s*-->$")  # the code of a screen, written after its heading
UNNUMBERED = re.compile(r"^\d+(?:\.\d+)*\.?\s+")


class TemplateError(ValueError):
    """A template or a pattern of the config the hub cannot use."""


def tidy(code: str) -> str:
    """A code as shown: whitespace runs as one space."""
    return " ".join(code.split())


def screen_number(code: str) -> int | None:
    """The number of a screen's code ``SCR-<n>``; None for any other code."""
    found = re.fullmatch(r"scr-(\d+)", code_key(code))
    return int(found[1]) if found else None


@dataclass(frozen=True)
class Heading:
    """What a heading's text makes: the kind of item, its code, its title and the fields its heading holds."""

    kind: str
    title: str
    code: str | None = None
    fields: tuple[tuple[str, object], ...] = ()
    known: bool = True  # False for a heading the template does not name, kept as a section


@dataclass(frozen=True)
class Template:
    """A template: its name and the pattern of each kind of code."""

    name: str
    patterns: Mapping[str, re.Pattern]

    def code_problem(self, kind: str, code: str) -> str | None:
        """Why ``code`` cannot be the code of an item of ``kind``; None when it can."""
        pattern = self.patterns.get(kind)
        if pattern is None:
            return f"an item of kind {kind} has no code"
        if pattern.fullmatch(tidy(code)) is None:
            return f"{tidy(code)!r} does not match the pattern of a {kind.replace('_', ' ')}: {pattern.pattern}"
        return None

    def row_kind(self, cell: str, in_cbr_section: bool) -> str | None:
        """The kind of item a table row is whose first cell is ``cell``: nfr anywhere, cbr under a heading of common
        business rules; None for a row that stays text."""
        text = tidy(cell.replace("*", ""))
        if self.patterns["nfr"].fullmatch(text):
            return "nfr"
        if in_cbr_section and self.patterns["cbr"].fullmatch(text):
            return "cbr"
        return None


def heading(text: str) -> Heading:
    """What the heading ``text`` (its #s gone) makes in srs-basic."""
    text = text.strip()
    found = USE_CASE.match(text)
    if found:
        return Heading("use_case", found["title"].strip(), tidy(found["code"]))
    found = BUSINESS_RULE.match(text)
    if found:
        steps = [step.strip() for step in (found["steps"] or "").split(",") if step.strip()]
        label = found["label"].strip() if found["label"] else None
        fields = (("steps", steps), ("step_label", label), ("dash", found["dash"]))
        return Heading("business_rule", found["title"].strip(), tidy(found["code"]), fields)
    found = SCREEN.match(text)
    if found:
        return Heading("screen", found["title"].strip())
    if APPROVAL_PAGE.match(text):
        return Heading("approval_page", text)
    if REVISION_HISTORY.match(text):
        return Heading("revision_history", text)
    if GLOSSARY.search(UNNUMBERED.sub("", text)):
        return Heading("glossary", text)
    return Heading("section", text, known=bool(NUMBERED.match(text) or NAMED_SECTION.match(text)))


def heading_text(kind: str, title: str, code: str | None, fields: Mapping) -> str:
    """The text of the heading of an item, the inverse of ``heading``."""
    if kind == "use_case" and code:
        return f"{code}: {title}"
    if kind == "business_rule" and code:
        label = fields.get("step_label")
        dash = fields.get("dash") or "–"
        return f"{label}: {code} {dash} {title}" if label else f"{code} {dash} {title}"
    if kind == "screen":
        return f"Screen: {title}"
    return title


def template_of(name: str | None = None, patterns: Mapping[str, str] | None = None) -> Template:
    """The template ``name`` (srs-basic when None) with the patterns of ``patterns`` replacing its own."""
    name = name or TEMPLATES[0]
    if name not in TEMPLATES:
        raise TemplateError(f"no template {name!r}: this hub has {', '.join(TEMPLATES)}")
    compiled = {}
    for kind in CODED_KINDS:
        source = (patterns or {}).get(kind) or CODE_PATTERNS[kind]
        try:
            compiled[kind] = re.compile(source)
        except re.error as exc:
            raise TemplateError(f"code_patterns.{kind}: {source!r} is not a regular expression: {exc}") from None
    return Template(name, compiled)


__all__ = [
    "ANCHOR",
    "CBR_SECTION",
    "CODE_PATTERNS",
    "ROW_KINDS",
    "SCREEN_PREFIX",
    "Heading",
    "Template",
    "TemplateError",
    "code_key",
    "heading",
    "heading_text",
    "screen_number",
    "template_of",
    "tidy",
]
