"""From the rows of srs to the answers of its API (``models.api``), and from a save's changes to those ``edits``
applies."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal, cast

from evo_agents.hub.server.srs.models.api import (
    SrsChange,
    SrsComment,
    SrsDiff,
    SrsDiffItem,
    SrsDocumentSummary,
    SrsFinding,
    SrsFindingSource,
    SrsItem,
    SrsItemDetail,
    SrsPreview,
    SrsPreviewItem,
    SrsProblem,
    SrsRef,
    SrsRetiredItem,
    SrsRetireResult,
    SrsReview,
    SrsReviewRun,
    SrsRevision,
    SrsUseCase,
    SrsUseCaseReview,
    SrsVersion,
    SrsVersionCases,
    SrsVersionDetail,
)
from evo_agents.hub.server.srs.models.diff import ItemChange
from evo_agents.hub.server.srs.models.doc import Document, Item, Problem
from evo_agents.hub.server.srs.models.template import code_key
from evo_agents.hub.server.srs.repository.findings import FindingRow
from evo_agents.hub.server.srs.repository.reviews import CommentRow
from evo_agents.hub.server.srs.repository.store import DocumentRow, ReviewRow, VersionRow, case_path
from evo_agents.hub.server.srs.service import edits
from evo_agents.hub.server.srs.service.imports import Preview
from evo_agents.hub.server.srs.service.use_case_reviews import CaseState


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


def use_case_model(case: CaseState, link: str = "") -> SrsUseCase:
    reviews = [SrsUseCaseReview(**{n: getattr(r, n) for n in SrsUseCaseReview.model_fields}) for r in case.reviews]
    return SrsUseCase(
        key=case.key,
        code=case.code,
        title=case.title,
        change=case.change,
        state=case.state,
        reviews=reviews,
        rev=case.rev,
        released=case.released,
        submitted=case.submitted,
        author=case.author,
        authors=list(case.author_logins),
        owners=list(case.owners),
        link=link,
    )


def cases_model(row: VersionRow, cases: list[CaseState], where: tuple[str, str] | None = None) -> SrsVersionCases:
    """``row`` with its use cases to review; ``where`` (project, slug) gives each the link of its review."""

    def link(case: CaseState) -> str:
        return case_path(where[0], where[1], row.number, case.key) if where else ""

    return SrsVersionCases(
        **version_model(row).model_dump(), use_cases=[use_case_model(case, link(case)) for case in cases]
    )


def problem_model(problem: Problem) -> SrsProblem:
    return SrsProblem(**problem.as_json())


def item_model(
    doc: Document, item: Item, revs: Mapping[str, int] | None = None, released: Mapping[str, int] | None = None
) -> SrsItem:
    codes = {other.key: other.code for other in doc.items}
    return SrsItem(
        rev=(revs or {}).get(item.key or ""),
        released=(released or {}).get(item.key or ""),
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
    slug: str,
    row: VersionRow,
    doc: Document,
    problems: list[Problem],
    cases: list[CaseState],
    project: str | None = None,
    revs: Mapping[str, int] | None = None,
    released: Mapping[str, int] | None = None,
) -> SrsVersionDetail:
    return SrsVersionDetail(
        **cases_model(row, cases, (project, slug) if project else None).model_dump(),
        document=slug,
        preamble=doc.preamble,
        items=[item_model(doc, item, revs, released) for item in doc.items],
        problems=[problem_model(problem) for problem in problems],
    )


def item_detail_model(
    slug: str,
    number: int,
    doc: Document,
    index: int,
    revs: Mapping[str, int] | None = None,
    released: int | None = None,
    history: list[SrsRevision] | None = None,
) -> SrsItemDetail:
    item = doc.items[index]
    held = {item.key or "": released} if released is not None else None
    return SrsItemDetail(
        **item_model(doc, item, revs, held).model_dump(),
        document=slug,
        version=number,
        path=[doc.items[at].title for at in reversed(doc.ancestors(index))],
        children=[item_model(doc, doc.items[at], revs) for at in doc.children(index)],
        revisions=history or [],
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


def comment_model(row: CommentRow, anchor: str | None = None) -> SrsComment:
    values = {name: value for name, value in row.__dict__.items() if name != "author_id"}
    return SrsComment(**values, anchor=cast(Literal["found", "changed"] | None, anchor))


def finding_model(row: FindingRow, documents: Mapping[str, str] | None = None) -> SrsFinding:
    """``row`` as an answer, each code among its sources with the slug ``documents`` maps the code's key to."""
    held = documents or {}

    def sources(listed: list[dict]) -> list[SrsFindingSource]:
        return [
            SrsFindingSource(**source, document=held.get(code_key(source["ref"])) if source["kind"] == "code" else None)
            for source in listed
        ]

    values: dict[str, Any] = row.__dict__ | {
        "sources": sources(row.sources),
        "resolution_sources": sources(row.resolution_sources),
    }
    return SrsFinding(**values)
