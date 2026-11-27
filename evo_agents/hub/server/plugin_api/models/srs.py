"""The shapes of the use cases a plugin lists of an SRS document (``routes.srs``)."""

from __future__ import annotations

from pydantic import BaseModel, Field

PERMISSION = "srs:read"
FEATURE = "srs"
USE_CASE = "use_case"


class ApprovedUseCase(BaseModel):
    code: str = Field(description="its code in that version, as UC POST-21")
    key: str = Field(description="the hub's key of the item, the same in every version of the document")


class ApprovedUseCases(BaseModel):
    """The use cases of the version of a document approved last, identifiers only."""

    document: str = Field(description="the document's slug")
    version: int = Field(description="the version approved last, which every use case below is read from")
    use_cases: list[ApprovedUseCase] = Field(description="in the document's order")


__all__ = ["FEATURE", "PERMISSION", "USE_CASE", "ApprovedUseCase", "ApprovedUseCases"]
