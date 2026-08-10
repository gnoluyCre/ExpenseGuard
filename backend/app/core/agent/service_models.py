"""Strong service-layer contracts for CP-F7.3 investigation orchestration."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Protocol

from app.core.agent.sanitization import PiiTokenDraft
from app.core.agent.tools import ToolContext
from app.core.errors import ExpenseGuardError


class InvestigationServiceError(ExpenseGuardError):
    status_code = 409


class InvestigationInputError(InvestigationServiceError):
    status_code = 422


class InvestigationNotFoundError(InvestigationServiceError):
    status_code = 404


class InvestigationUnavailableError(InvestigationServiceError):
    status_code = 503


class InvestigationInternalError(InvestigationServiceError):
    status_code = 500

    def __init__(
        self,
        *,
        code: str = "INVESTIGATION_RUN_FAILED",
        message: str = "异常取证暂时不可用，已转人工处理",
    ) -> None:
        super().__init__(code=code, message=message)


@dataclass(frozen=True)
class InvestigationRunOptions:
    provider_kind: Literal["disabled", "openai_compatible"]
    provider_model: str
    max_steps: int
    timeout_seconds: int
    redaction_version: str
    agent_version: str = "react-v1"
    action_schema_version: int = 1
    prompt_template_version: str = "prompt-v1"


@dataclass(frozen=True)
class InvestigationSeed:
    """Already-sanitized provider evidence and server-owned tool scope."""

    evidence: dict[str, Any]
    tool_context: ToolContext
    token_drafts: tuple[PiiTokenDraft, ...] = ()


@dataclass(frozen=True)
class InvestigationRunView:
    id: uuid.UUID
    tenant_id: uuid.UUID
    correlation_finding_id: uuid.UUID
    detection_run_id: uuid.UUID
    file_version_id: uuid.UUID
    actor_id: uuid.UUID
    provider_kind: str
    provider_model: str
    max_steps: int
    timeout_seconds: int
    redaction_version: str
    agent_version: str
    action_schema_version: int
    prompt_template_version: str
    input_fingerprint: str
    config_fingerprint: str
    created_at: datetime


@dataclass(frozen=True)
class EvidenceStepView:
    id: uuid.UUID
    investigation_run_id: uuid.UUID
    tenant_id: uuid.UUID
    correlation_finding_id: uuid.UUID
    detection_run_id: uuid.UUID
    file_version_id: uuid.UUID
    step_no: int
    action_kind: str
    model_action: dict[str, Any]
    decision_summary: str
    payload_fingerprint: str
    tool_name: str | None
    tool_input: dict[str, Any] | None
    tool_output: dict[str, Any] | None
    created_at: datetime

    def prompt_fact(self) -> dict[str, Any]:
        return {
            "step_no": self.step_no,
            "action": self.model_action,
            "observation": self.tool_output,
        }


@dataclass(frozen=True)
class InvestigationResultView:
    id: uuid.UUID
    investigation_run_id: uuid.UUID
    tenant_id: uuid.UUID
    correlation_finding_id: uuid.UUID
    detection_run_id: uuid.UUID
    file_version_id: uuid.UUID
    outcome: str
    evidence_sufficient: bool | None
    summary: str
    reason_code: str
    citations: tuple[dict[str, Any], ...]
    result_fingerprint: str
    completed_at: datetime


@dataclass(frozen=True)
class InvestigationDetail:
    run: InvestigationRunView
    steps: tuple[EvidenceStepView, ...]
    result: InvestigationResultView | None
    reused_existing: bool


@dataclass(frozen=True)
class InvestigationPage:
    items: tuple[InvestigationDetail, ...]
    total: int
    limit: int
    offset: int


@dataclass(frozen=True)
class EvidenceStepPage:
    items: tuple[EvidenceStepView, ...]
    total: int
    limit: int
    offset: int


@dataclass(frozen=True)
class ReconciliationSnapshot:
    """Business facts supplied to an optional checkpoint adapter."""

    investigation_run_id: uuid.UUID
    committed_steps: tuple[EvidenceStepView, ...]
    terminal: bool


class CheckpointReconciler(Protocol):
    async def reconcile(self, snapshot: ReconciliationSnapshot) -> None: ...


class NoopCheckpointReconciler:
    """CP-F7.3 default; real AsyncPostgresSaver wiring is a later integration seam."""

    async def reconcile(self, snapshot: ReconciliationSnapshot) -> None:
        del snapshot
