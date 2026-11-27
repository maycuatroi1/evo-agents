"""The shapes of a project's data loss prevention policy (``evo_agents.hub.server.dlp_policy``), and ``Blocked``, a
model call the policy blocked."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field

from evo_agents.hub import dlp

BLOCKED = "the project's DLP policy blocked this call: it holds {what}"

Mode = Literal["off", "redact", "block"]
Kind = Literal["cif", "national_id", "account_number", "financial_figure", "contract_keyword"]
Keyword = Annotated[str, Field(min_length=1, max_length=dlp.MAX_KEYWORD_CHARS)]


class DlpPolicyWrite(BaseModel):
    mode: Mode
    kinds: list[Kind] = Field(default_factory=lambda: list(dlp.KINDS))
    keywords: list[Keyword] = Field(default_factory=list, max_length=dlp.MAX_KEYWORDS)


class DlpCount(BaseModel):
    kind: Kind
    surface: Literal["gateway_proxy", "hub_runtime"]
    action: Literal["redacted", "blocked"]
    count: int


class DlpPolicy(BaseModel):
    project: str
    mode: Mode
    kinds: list[Kind]
    keywords: list[str]
    builtin_keywords: list[str]
    updated_at: str | None
    counts: list[DlpCount]


class DlpInspectIn(BaseModel):
    text: str = Field(max_length=dlp.MAX_INSPECT_BYTES)


class DlpFinding(BaseModel):
    kind: Kind
    count: int


class DlpInspection(BaseModel):
    mode: Mode
    blocked: bool
    text: str  # the text as the policy sends it: redacted in redact mode, as given otherwise
    findings: list[DlpFinding]


class Blocked(Exception):
    """A model call the policy blocked; ``kinds`` counts its matches by rule kind."""

    def __init__(self, counts):
        self.counts = dict(counts)
        super().__init__(BLOCKED.format(what=what(self.counts)))


def what(counts) -> str:
    """``counts`` as words: kinds and numbers, never a value."""
    return ", ".join(f"{count} {kind}" for kind, count in sorted(dict(counts).items()))
