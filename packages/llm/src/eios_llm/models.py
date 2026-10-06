"""Vendor-neutral request/response models. No provider SDK types appear here."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class LLMMessage(BaseModel):
    model_config = ConfigDict(frozen=True)

    role: Literal["system", "user", "assistant"]
    content: str = Field(max_length=2_000_000)


class LLMRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    messages: list[LLMMessage] = Field(min_length=1, max_length=200)
    model: str | None = Field(default=None, description="provider default when omitted")
    max_tokens: int = Field(default=1024, ge=1, le=200_000)
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    json_mode: bool = False
    data_classification: Literal["public", "default", "project"] = "default"

    @property
    def system(self) -> str:
        return "\n\n".join(m.content for m in self.messages if m.role == "system")

    @property
    def turns(self) -> list[LLMMessage]:
        return [m for m in self.messages if m.role != "system"]


class LLMUsage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens


class LLMResponse(BaseModel):
    text: str
    model: str
    provider: str
    usage: LLMUsage = Field(default_factory=LLMUsage)
    finish_reason: str = "stop"
    latency_ms: int = 0


class LLMError(Exception):
    """Provider failure. ``retryable`` marks transient errors (429, 5xx, timeouts)."""

    def __init__(self, message: str, *, retryable: bool = False, status: int | None = None) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.status = status
