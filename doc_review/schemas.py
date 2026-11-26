"""Pydantic request models."""

from __future__ import annotations

from pydantic import BaseModel, Field, model_validator


class DocumentCreate(BaseModel):
    title: str = "Untitled document"
    content: str = ""


class EditCreate(BaseModel):
    base_revision: int = Field(ge=0)
    start: int = Field(ge=0)
    end: int = Field(ge=0)
    replacement: str = ""
    request_id: str | None = Field(default=None, min_length=1, max_length=200)

    @model_validator(mode="after")
    def check_interval(self) -> "EditCreate":
        if self.start > self.end:
            raise ValueError("start must be less than or equal to end")
        return self


class SuggestionInput(BaseModel):
    client_id: str | None = Field(default=None, max_length=200)
    start: int = Field(ge=0)
    end: int = Field(ge=0)
    replacement: str = ""

    @model_validator(mode="after")
    def check_nonempty(self) -> "SuggestionInput":
        if self.start >= self.end:
            raise ValueError("suggestion interval must be non-empty (start < end)")
        return self


class SuggestionGroupCreate(BaseModel):
    base_revision: int = Field(ge=0)
    suggestions: list[SuggestionInput] = Field(min_length=1)
    request_id: str | None = Field(default=None, min_length=1, max_length=200)
    # Default: persist a valid historical proposal and report drift.
    # Set true to turn any base->current mapping conflict into HTTP 409.
    strict: bool = False


class AcceptRequest(BaseModel):
    # May also be supplied through the Idempotency-Key header.
    request_id: str | None = Field(default=None, min_length=1, max_length=200)


class CloseRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=1000)


# ---- Release (publishing) API ----


class ReleaseItemInput(BaseModel):
    document_id: str = Field(min_length=1, max_length=100)
    revision: int = Field(ge=0)
    references: list[str] = Field(default_factory=list)


class ReleasePrepareRequest(BaseModel):
    items: list[ReleaseItemInput] = Field(min_length=1)
    # If supplied, retrying the same prepare call returns the first ticket.
    request_id: str | None = Field(default=None, min_length=1, max_length=200)


class ReleaseConfirmRequest(BaseModel):
    request_id: str | None = Field(default=None, min_length=1, max_length=200)


class RetractRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=1000)
