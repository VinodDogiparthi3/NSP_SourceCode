from typing import Literal

from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    conversation_id: str | None = None
    top_k: int | None = Field(default=None, ge=1, le=20)


class FeedbackRequest(BaseModel):
    conversation_id: str
    message_id: str
    rating: Literal["up", "down"]
    note: str | None = Field(default=None, max_length=1000)


class SourceRecord(BaseModel):
    record_id: str
    contactid: str | None = None
    document_text: str
    individual_type: str | None = None
    federation: str | None = None
    city: str | None = None
    region: str | None = None
    distance: float

