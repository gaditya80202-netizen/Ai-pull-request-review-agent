from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Severity(str, Enum):
    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    INFO = "INFO"


class LLMFinding(BaseModel):
    """A single finding returned by an AI agent."""

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
    )

    severity: Severity
    category: str = Field(min_length=1, max_length=80)
    summary: str = Field(min_length=1, max_length=500)
    file_path: str = Field(min_length=1, max_length=1000)

    line_start: int = Field(ge=1)
    line_end: int | None = Field(default=None, ge=1)

    suggestion: str | None = Field(default=None, max_length=4000)
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str = Field(min_length=1, max_length=4000)

    @model_validator(mode="after")
    def validate_line_range(self):
        if (
            self.line_end is not None
            and self.line_end < self.line_start
        ):
            raise ValueError(
                "line_end cannot be before line_start"
            )
        return self


class LLMFindingList(BaseModel):
    """The complete structured response expected from an AI agent."""

    model_config = ConfigDict(extra="forbid")

    findings: list[LLMFinding] = Field(
        default_factory=list,
        max_length=50,
    )


class Finding(LLMFinding):
    """A validated finding with its source agent assigned by our code."""

    agent_type: Literal["security", "quality", "tests", "docs"]
