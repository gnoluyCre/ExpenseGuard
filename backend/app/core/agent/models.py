"""Strict contracts for the F7 anomaly-investigation agent."""

from datetime import date
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictAgentModel(BaseModel):
    """Forbid silent provider/schema drift at every model boundary."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class InvestigationOutcome(StrEnum):
    SUFFICIENT = "sufficient"
    INSUFFICIENT = "insufficient"
    UNAVAILABLE = "unavailable"
    MAX_STEPS = "max_steps"
    FAILED = "failed"


class GetCorrelationRowsCall(StrictAgentModel):
    kind: Literal["get_correlation_rows"] = "get_correlation_rows"
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=20, ge=1, le=100)


class GetEmployeeHistoryCall(StrictAgentModel):
    kind: Literal["get_employee_history"] = "get_employee_history"
    employee_token: str = Field(pattern=r"^EMP_v[1-9][0-9]?_[0-9a-f]{16}$")
    days: int = Field(default=365, ge=1, le=1095)
    limit: int = Field(default=20, ge=1, le=100)


class GetSupplierHistoryCall(StrictAgentModel):
    kind: Literal["get_supplier_history"] = "get_supplier_history"
    supplier_token: str = Field(pattern=r"^SUP_v[1-9][0-9]?_[0-9a-f]{16}$")
    days: int = Field(default=365, ge=1, le=1095)
    limit: int = Field(default=20, ge=1, le=100)


class SearchPolicyClausesCall(StrictAgentModel):
    kind: Literal["search_policy_clauses"] = "search_policy_clauses"
    query: str = Field(min_length=1, max_length=500)
    expense_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    limit: int = Field(default=8, ge=1, le=20)

    @field_validator("expense_date")
    @classmethod
    def expense_date_must_exist(cls, value: str) -> str:
        try:
            date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("expense_date 必须是有效 ISO 日期") from exc
        return value


ReadOnlyToolCall = Annotated[
    GetCorrelationRowsCall
    | GetEmployeeHistoryCall
    | GetSupplierHistoryCall
    | SearchPolicyClausesCall,
    Field(discriminator="kind"),
]


class ToolCallAction(StrictAgentModel):
    kind: Literal["tool_call"] = "tool_call"
    call: ReadOnlyToolCall
    rationale_summary: str = Field(min_length=1, max_length=500)


class TerminateAction(StrictAgentModel):
    kind: Literal["terminate"] = "terminate"
    outcome: Literal[InvestigationOutcome.SUFFICIENT, InvestigationOutcome.INSUFFICIENT]
    evidence_sufficient: bool
    reason_code: str = Field(pattern=r"^[A-Z][A-Z0-9_]{2,63}$")
    summary: str = Field(min_length=1, max_length=2000)
    clause_id: str | None = Field(default=None, max_length=128)
    quote: str | None = Field(default=None, max_length=4000)

    @model_validator(mode="after")
    def outcome_matches_boolean(self) -> "TerminateAction":
        expected = self.outcome == InvestigationOutcome.SUFFICIENT
        if self.evidence_sufficient is not expected:
            raise ValueError("evidence_sufficient 必须与 outcome 一致")
        if (self.clause_id is None) != (self.quote is None):
            raise ValueError("clause_id 与 quote 必须同时存在或同时为空")
        return self


AgentAction = Annotated[ToolCallAction | TerminateAction, Field(discriminator="kind")]


class TokenUsage(StrictAgentModel):
    prompt_tokens: int = Field(default=0, ge=0)
    completion_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def total_is_consistent(self) -> "TokenUsage":
        if self.total_tokens != self.prompt_tokens + self.completion_tokens:
            raise ValueError("total_tokens 必须等于 prompt_tokens + completion_tokens")
        return self


class ProviderResponse(StrictAgentModel):
    action: AgentAction
    usage: TokenUsage = Field(default_factory=TokenUsage)


class InvestigationPrompt(StrictAgentModel):
    """Only sanitized data may cross the provider boundary."""

    system_instruction: str = Field(min_length=1, max_length=8000)
    evidence_json: str = Field(min_length=2, max_length=64_000)
    prior_steps_json: str = Field(default="[]", min_length=2, max_length=128_000)
