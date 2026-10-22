"""The API of the plugin srs as Pydantic models: what its routes take and answer, and how a document, a version and
an item become an answer. Every model is named ``Srs...`` in the hub's OpenAPI document.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from evo_agents.core_plugins.srs import edits
from evo_agents.core_plugins.srs.diff import ItemChange
from evo_agents.core_plugins.srs.doc import Document, Item, Problem
from evo_agents.core_plugins.srs.findings import FindingRow
from evo_agents.core_plugins.srs.imports import MAX_MARKDOWN_BYTES, Preview
from evo_agents.core_plugins.srs.reviews import CommentRow
from evo_agents.core_plugins.srs.store import DocumentRow, ReviewRow, VersionRow
from evo_agents.core_plugins.srs.template import code_key
from evo_agents.core_plugins.srs.use_case_reviews import CaseState
from evo_agents.hub.runs import RUN_STATES
from evo_agents.hub.server.projects import LabelIn
from evo_agents.hub.tables_srs import (
    DOCUMENT_KINDS,
    FINDING_KINDS,
    FINDING_LEVELS,
    FINDING_STATUSES,
    ITEM_KINDS,
    MAX_COMMENT_CHARS,
    MAX_RESOLUTION_CHARS,
    MAX_REVIEW_CHARS,
    MAX_SUMMARY_CHARS,
    MAX_TITLE_CHARS,
    REF_RELS,
    REVIEW_DECISIONS,
    REVIEW_ROLES,
    SLUG,
    SOURCE_KINDS,
    VERSION_STATES,
)

MAX_BODY_CHARS = 200_000  # of one item
MAX_CHANGES = 500  # in one save
CHANGES = ("added", "removed", "changed", "moved")
USE_CASE_CHANGES = ("added", "changed", "removed")
USE_CASE_STATES = ("pending", "approved", "changes_requested")


class SrsDocumentSummary(BaseModel):
    """A BA document of a project, with its latest version."""

    slug: str
    title: str
    kind: Literal[DOCUMENT_KINDS]
    template: str
    approved_version: int | None = Field(description="the version approved last")
    latest_version: int | None
    latest_state: Literal[VERSION_STATES] | None
    label: dict = Field(description="the label every row of the document carries")
    created_by: str
    created_at: datetime
    archived_at: datetime | None


class SrsReview(BaseModel):
    reviewer: str
    role: Literal[REVIEW_ROLES] | None = Field(description="the role the review counts for")
    decision: Literal[REVIEW_DECISIONS]
    body: str
    round: int
    created_at: datetime


class SrsReviewRun(BaseModel):
    """@ba's review of a version's current round: the task run the hub handed when the version entered review."""

    id: int
    state: Literal[RUN_STATES] = Field(description="approving the version waits while it is queued or running")


class SrsVersion(BaseModel):
    """A version of a document, never changed after it was saved, and the reviews of each round."""

    number: int
    parent_number: int | None = Field(description="the version it was built on")
    state: Literal[VERSION_STATES]
    round: int = Field(description="how many times it was submitted")
    author: str
    drafted_by_run: int | None = Field(description="the run of the agent that drafted it, when one did")
    summary: str
    created_at: datetime
    submitted_at: datetime | None
    decided_at: datetime | None
    review_run: SrsReviewRun | None = Field(
        description="@ba's review of the current round, when the hub handed one; none in a project without @ba"
    )
    reviews: list[SrsReview]


class SrsUseCaseReview(BaseModel):
    """A review of a use case that counts for the version, with the version it was recorded on."""

    reviewer: str
    role: Literal[REVIEW_ROLES] | None = Field(description="the role the review counts for")
    decision: Literal[REVIEW_DECISIONS]
    body: str
    version: int = Field(description="the version it was recorded on: an earlier one when the use case reads the same")
    created_at: datetime


class SrsUseCase(BaseModel):
    """A use case a version needs reviewed, with its state and the reviews that count."""

    key: str
    code: str | None
    title: str
    change: Literal[USE_CASE_CHANGES] = Field(description="what the version does to it since the version approved last")
    state: Literal[USE_CASE_STATES]
    reviews: list[SrsUseCaseReview]


class SrsVersionCases(SrsVersion):
    """A version with the use cases it needs reviewed: those it adds, changes (the use case, or a business rule under
    it) or drops since the version approved last, every use case while none was approved; empty for a version that is
    neither a draft nor in review."""

    use_cases: list[SrsUseCase]


class SrsRef(BaseModel):
    rel: Literal[REF_RELS]
    to: str = Field(description="the key of the item it links to")
    to_code: str | None


class SrsItem(BaseModel):
    """An item of a version: its key (fixed), kind, code and aliases, title, parent (key), heading level (null for a
    table row), the fields its kind holds, its Markdown body and its links."""

    key: str
    kind: Literal[ITEM_KINDS]
    code: str | None
    aliases: list[str]
    title: str
    parent: str | None
    heading_level: int | None
    fields: dict[str, Any]
    body: str
    refs: list[SrsRef]


class SrsProblem(BaseModel):
    level: Literal["error", "warning"]
    check: str
    message: str
    code: str | None
    item: str | None = Field(description="the key of the item")
    line: int | None = Field(description="the line of the Markdown it came from")


class SrsVersionDetail(SrsVersionCases):
    """A version with its content and what is wrong with it."""

    document: str
    preamble: str
    items: list[SrsItem]
    problems: list[SrsProblem]


class SrsDocument(SrsDocumentSummary):
    """A document with every version, the newest first, and one of them whole (the latest unless asked)."""

    versions: list[SrsVersion]
    shown: SrsVersionDetail | None


class SrsItemDetail(SrsItem):
    """An item of a version, with the items above it and those under it."""

    document: str
    version: int
    path: list[str] = Field(description="the titles of the items above it, the top first")
    children: list[SrsItem]


class SrsImport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    markdown: str = Field(min_length=1, max_length=MAX_MARKDOWN_BYTES)
    document: str | None = Field(None, pattern=SLUG, description="the document whose next version this is")
    slug: str | None = Field(None, pattern=SLUG, description="the slug of a new document; from its title if left out")
    title: str | None = Field(None, min_length=1, max_length=MAX_TITLE_CHARS)
    kind: Literal[DOCUMENT_KINDS] = "srs"
    summary: str = Field("", max_length=MAX_SUMMARY_CHARS)
    label: LabelIn | None = Field(None, description="a new document's label, an admin's choice; the project's if not")
    dry_run: bool = False


class SrsPreviewItem(BaseModel):
    kind: Literal[ITEM_KINDS]
    code: str | None
    title: str
    level: int | None
    parent: int | None = Field(description="the index of its parent in items")
    line: int | None


class SrsPreview(BaseModel):
    items: list[SrsPreviewItem]
    counts: dict[str, int] = Field(description="items per kind")
    problems: list[SrsProblem]
    added: list[str] = Field(description="the codes (or titles) of the items an import adds to the document")
    changed: list[str]
    removed: list[str]


class SrsImportResult(BaseModel):
    document: str
    created: bool = Field(description="whether the import created the document")
    version: int | None = Field(description="the version saved; null for a dry run")
    dry_run: bool
    preview: SrsPreview


class SrsRefIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rel: Literal[REF_RELS]
    to: str = Field(min_length=1, max_length=100, description="the code or key of the item it links to")


class SrsNewItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal[ITEM_KINDS]
    title: str = Field(min_length=1, max_length=500)
    body: str = Field("", max_length=MAX_BODY_CHARS)
    code: str | None = Field(None, min_length=1, max_length=100)
    parent: str | None = Field(None, description="the key of its parent")
    after: str | None = Field(None, description="the key of the sibling it follows")
    heading_level: int | None = Field(None, ge=1, le=6)
    steps: list[str] | None = Field(None, max_length=50, description="a business rule's steps of its use case")


class SrsChange(BaseModel):
    """A change of the item ``key`` (title, body, steps, links, or drop it with ``retire``), or a ``new`` item."""

    model_config = ConfigDict(extra="forbid")

    key: str | None = Field(None, min_length=1, max_length=64)
    title: str | None = Field(None, min_length=1, max_length=500)
    body: str | None = Field(None, max_length=MAX_BODY_CHARS)
    steps: list[str] | None = Field(None, max_length=50)
    refs: list[SrsRefIn] | None = Field(None, max_length=100)
    retire: bool = False
    new: SrsNewItem | None = None

    @model_validator(mode="after")
    def _one_target(self):
        if (self.key is None) == (self.new is None):
            raise ValueError("a change names an item (key) or a new one (new), not both")
        return self


class SrsSave(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_version: int = Field(ge=1, description="the version the changes were made on")
    summary: str = Field("", max_length=MAX_SUMMARY_CHARS)
    preamble: str | None = Field(None, max_length=MAX_BODY_CHARS)
    changes: list[SrsChange] = Field(min_length=1, max_length=MAX_CHANGES)


class SrsReviewIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal[REVIEW_DECISIONS]
    role: Literal[REVIEW_ROLES] | None = Field(None, description="the role it counts for; one the reviewer holds")
    body: str = Field("", max_length=MAX_REVIEW_CHARS)


class SrsWithdrawIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    comment: str | None = Field(
        None, min_length=1, max_length=MAX_COMMENT_CHARS, description="why, kept as a comment on the version"
    )


class SrsRetireIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    codes: list[Annotated[str, Field(min_length=1, max_length=100)]] = Field(
        min_length=1, max_length=MAX_CHANGES, description="the codes or aliases of the items to retire"
    )
    summary: str = Field("", max_length=MAX_SUMMARY_CHARS, description="the new version's; from the codes if left out")
    dry_run: bool = False


class SrsRetiredItem(BaseModel):
    key: str
    kind: Literal[ITEM_KINDS]
    code: str | None
    title: str


class SrsRetireResult(BaseModel):
    document: str
    version: int | None = Field(description="the version saved, a draft; null for a dry run")
    dry_run: bool
    dropped: list[SrsRetiredItem] = Field(
        description="every item the version drops: each one retired and the items under it, in document order"
    )


class SrsDiffItem(BaseModel):
    key: str
    kind: Literal[ITEM_KINDS]
    code: str | None
    change: Literal[CHANGES]
    title_from: str | None
    title_to: str | None
    lines: list[str] = Field(description="a unified diff of the item's Markdown")


class SrsDiff(BaseModel):
    document: str
    from_version: int
    to_version: int
    preamble: list[str] = Field(description="a unified diff of the text before the first heading; empty when the same")
    items: list[SrsDiffItem]


class SrsExport(BaseModel):
    document: str
    version: int
    markdown: str


class SrsComment(BaseModel):
    id: int
    version: int | None
    item: str | None = Field(description="the key of the item")
    item_code: str | None
    parent_id: int | None
    author: str
    run_id: int | None
    body: str
    created_at: datetime
    resolved_by: str | None
    resolved_at: datetime | None


class SrsCommentIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    body: str = Field(min_length=1, max_length=MAX_COMMENT_CHARS)
    version: int | None = Field(None, ge=1)
    item: str | None = Field(None, min_length=1, max_length=100, description="the code or key of the item")
    parent_id: int | None = Field(None, ge=1, description="the comment it answers")


class SrsFindingSource(BaseModel):
    kind: Literal[SOURCE_KINDS] = Field(
        description="node: an id of the project's knowledge graph; code: a code an item of the project holds; path: "
        "repo:path, or a path, a file or document node carries; version: a later version of the document"
    )
    ref: str
    note: str | None = None
    document: str | None = Field(
        None, description="of a code: the slug of the document whose item holds it, when the reader sees that document"
    )


class SrsFinding(BaseModel):
    """A conflict found in a document, with the sources that show it and, once settled, how."""

    id: int
    version: int = Field(description="the version it was found on")
    item: str | None = Field(description="the key of the item it is about; null for the whole document")
    item_code: str | None
    kind: Literal[FINDING_KINDS]
    level: Literal[FINDING_LEVELS] = Field(description="an open finding of level decide holds approval")
    sources: list[SrsFindingSource] = Field(description="what shows the conflict, at least one")
    summary: str
    status: Literal[FINDING_STATUSES]
    resolution: str | None = Field(description="how it was settled, or why it was waived")
    resolution_sources: list[SrsFindingSource] = Field(description="what settled it, when settled from the graph")
    settled_by: str | None = Field(description="the member who settled it; null when settled from the graph")
    settled_at: datetime | None
    chat_link: str | None = Field(description="the link of the chat message that settled it")
    run_id: int | None = Field(description="the run of @ba that recorded it")
    created_at: datetime


class SrsWaiveIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=MAX_RESOLUTION_CHARS, description="why, kept as its resolution")


def edit_changes(changes: list[SrsChange]) -> list[edits.Change]:
    """The changes of a save as ``edits`` applies them."""
    found = []
    for change in changes:
        new = None
        if change.new is not None:
            values = change.new.model_dump()
            level = values.pop("heading_level")
            new = edits.NewItem(**values, level=level)
        refs = [(ref.rel, ref.to) for ref in change.refs] if change.refs is not None else None
        found.append(edits.Change(change.key, change.title, change.body, change.steps, refs, change.retire, new))
    return found


# From the plugin's rows to answers


def summary_model(row: DocumentRow) -> SrsDocumentSummary:
    return SrsDocumentSummary(**{name: getattr(row, name) for name in SrsDocumentSummary.model_fields})


def review_model(row: ReviewRow) -> SrsReview:
    return SrsReview(**{name: getattr(row, name) for name in SrsReview.model_fields})


def version_model(row: VersionRow) -> SrsVersion:
    values = {name: getattr(row, name) for name in SrsVersion.model_fields if name not in ("reviews", "review_run")}
    run = None
    if row.review_run_id is not None and row.review_run_state is not None:
        run = SrsReviewRun(id=row.review_run_id, state=row.review_run_state)
    return SrsVersion(**values, review_run=run, reviews=[review_model(review) for review in row.reviews])


def use_case_model(case: CaseState) -> SrsUseCase:
    reviews = [SrsUseCaseReview(**{n: getattr(r, n) for n in SrsUseCaseReview.model_fields}) for r in case.reviews]
    return SrsUseCase(
        key=case.key, code=case.code, title=case.title, change=case.change, state=case.state, reviews=reviews
    )


def cases_model(row: VersionRow, cases: list[CaseState]) -> SrsVersionCases:
    return SrsVersionCases(**version_model(row).model_dump(), use_cases=[use_case_model(case) for case in cases])


def problem_model(problem: Problem) -> SrsProblem:
    return SrsProblem(**problem.as_json())


def item_model(doc: Document, item: Item) -> SrsItem:
    codes = {other.key: other.code for other in doc.items}
    return SrsItem(
        key=item.key or "",
        kind=item.kind,
        code=item.code,
        aliases=item.aliases,
        title=item.title,
        parent=doc.items[item.parent].key if item.parent is not None else None,
        heading_level=item.level,
        fields=item.fields,
        body=item.body,
        refs=[SrsRef(rel=rel, to=key, to_code=codes.get(key)) for rel, key in item.refs],
    )


def detail_model(
    slug: str, row: VersionRow, doc: Document, problems: list[Problem], cases: list[CaseState]
) -> SrsVersionDetail:
    return SrsVersionDetail(
        **cases_model(row, cases).model_dump(),
        document=slug,
        preamble=doc.preamble,
        items=[item_model(doc, item) for item in doc.items],
        problems=[problem_model(problem) for problem in problems],
    )


def item_detail_model(slug: str, number: int, doc: Document, index: int) -> SrsItemDetail:
    item = doc.items[index]
    return SrsItemDetail(
        **item_model(doc, item).model_dump(),
        document=slug,
        version=number,
        path=[doc.items[at].title for at in reversed(doc.ancestors(index))],
        children=[item_model(doc, doc.items[at]) for at in doc.children(index)],
    )


def preview_model(preview: Preview) -> SrsPreview:
    return SrsPreview(
        items=[SrsPreviewItem(**item) for item in preview.items],
        counts=preview.counts,
        problems=[problem_model(problem) for problem in preview.problems],
        added=preview.added,
        changed=preview.changed,
        removed=preview.removed,
    )


def diff_model(slug: str, low: int, high: int, preamble: list[str], changes: list[ItemChange]) -> SrsDiff:
    items = [SrsDiffItem(**change.__dict__) for change in changes]
    return SrsDiff(document=slug, from_version=low, to_version=high, preamble=preamble, items=items)


def retire_model(slug: str, number: int | None, dry_run: bool, dropped: list[Item]) -> SrsRetireResult:
    items = [SrsRetiredItem(key=item.key or "", kind=item.kind, code=item.code, title=item.title) for item in dropped]
    return SrsRetireResult(document=slug, version=number, dry_run=dry_run, dropped=items)


def comment_model(row: CommentRow) -> SrsComment:
    return SrsComment(**row.__dict__)


def finding_model(row: FindingRow, documents: Mapping[str, str] | None = None) -> SrsFinding:
    """``row`` as an answer, each code among its sources with the slug ``documents`` maps the code's key to."""
    held = documents or {}

    def sources(listed: list[dict]) -> list[SrsFindingSource]:
        return [
            SrsFindingSource(**source, document=held.get(code_key(source["ref"])) if source["kind"] == "code" else None)
            for source in listed
        ]

    values = row.__dict__ | {"sources": sources(row.sources), "resolution_sources": sources(row.resolution_sources)}
    return SrsFinding(**values)
