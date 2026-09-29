from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class ApiConfig(BaseModel):
    base_url: str = ""
    api_key: str = ""
    model: str = ""


class PaperUpdate(BaseModel):
    title: str | None = None
    title_zh: str | None = None
    abstract: str | None = None
    notes: str | None = None
    authors: list[str] = Field(default_factory=list)
    affiliations: list[str] = Field(default_factory=list)
    publication_date: str | None = None
    doi_url: str | None = None
    journal_name: str | None = None


class JournalLookupRequest(BaseModel):
    journal_name: str | None = None


class PartitionLookupBatchRequest(BaseModel):
    scope: Literal["all", "unchecked"] = "all"


class TagCreate(BaseModel):
    name: str
    aliases: list[str] = Field(default_factory=list)
    description: str = ""


class TagUpdate(BaseModel):
    name: str
    aliases: list[str] = Field(default_factory=list)
    description: str = ""


class AssignTagsRequest(BaseModel):
    tag_ids: list[str]


class FileConflictResolve(BaseModel):
    action: str
