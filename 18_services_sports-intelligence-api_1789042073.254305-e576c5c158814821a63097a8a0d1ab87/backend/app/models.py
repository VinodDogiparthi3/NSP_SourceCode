from typing import Any, Literal

from pydantic import BaseModel, Field


class ConversationTurn(BaseModel):
    role: Literal["user", "assistant"]
    text: str = Field(min_length=1, max_length=6000)


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=3000)
    conversation_id: str | None = None
    top_k: int | None = Field(default=None, ge=1, le=20)
    history: list[ConversationTurn] = Field(default_factory=list, max_length=12)


class FeedbackRequest(BaseModel):
    conversation_id: str
    message_id: str
    rating: Literal["up", "down"]
    note: str | None = Field(default=None, max_length=1000)


class SourceRecord(BaseModel):
    record_id: str
    source: Literal["NSP", "SOPC", "EARLY_WARNING"]
    title: str
    document_text: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    distance: float = 0.0


class QueryTask(BaseModel):
    label: str = ""
    route: Literal["analytics", "semantic_search", "dropout_risk"]
    dataset: Literal["nsp", "sopc", "both", "dropout"]
    search_query: str = ""
    sql: str = ""


class QueryPlan(BaseModel):
    route: Literal[
        "greeting",
        "analytics",
        "semantic_search",
        "dropout_risk",
        "multi",
        "out_of_scope",
        "clarification",
    ]
    dataset: Literal["nsp", "sopc", "both", "dropout"]
    search_query: str = ""
    sql: str = ""
    rationale: str = ""
    normalized_question: str = ""
    tasks: list[QueryTask] = Field(default_factory=list, max_length=4)


class DropoutAlert(BaseModel):
    athlete_id: str
    sport: str
    federation: str
    region: str
    city: str
    age: float | None
    age_group: str
    dropout_probability: float
    risk_level: Literal["HIGH", "MEDIUM", "LOW"]
    days_since_activity: int
    signals: list[str]
    recommended_action: str
