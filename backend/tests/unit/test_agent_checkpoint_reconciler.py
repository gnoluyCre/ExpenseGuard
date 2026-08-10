import uuid
from datetime import UTC, datetime

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from app.core.agent.checkpoint_reconciler import LangGraphCheckpointReconciler
from app.core.agent.service_models import EvidenceStepView, ReconciliationSnapshot


@pytest.mark.asyncio
async def test_business_facts_overwrite_ahead_or_behind_checkpoint() -> None:
    run_id = uuid.UUID("10000000-0000-0000-0000-000000000001")
    reconciler = LangGraphCheckpointReconciler(InMemorySaver())
    step = EvidenceStepView(
        id=uuid.UUID("20000000-0000-0000-0000-000000000001"),
        tenant_id=uuid.UUID("30000000-0000-0000-0000-000000000001"),
        investigation_run_id=run_id,
        correlation_finding_id=uuid.UUID("40000000-0000-0000-0000-000000000001"),
        detection_run_id=uuid.UUID("50000000-0000-0000-0000-000000000001"),
        file_version_id=uuid.UUID("60000000-0000-0000-0000-000000000001"),
        step_no=1,
        action_kind="terminate",
        model_action={"kind": "terminate"},
        decision_summary="证据不足",
        payload_fingerprint="a" * 64,
        tool_name=None,
        tool_input=None,
        tool_output=None,
        created_at=datetime(2026, 8, 10, tzinfo=UTC),
    )
    await reconciler.reconcile(
        ReconciliationSnapshot(
            investigation_run_id=run_id,
            committed_steps=(step,),
            terminal=True,
        )
    )
    first = await reconciler.read(run_id)
    assert first is not None
    assert first["committed_step_count"] == 1
    assert first["terminal"] is True

    await reconciler.reconcile(
        ReconciliationSnapshot(
            investigation_run_id=run_id,
            committed_steps=(),
            terminal=False,
        )
    )
    corrected = await reconciler.read(run_id)
    assert corrected is not None
    assert corrected["committed_step_count"] == 0
    assert corrected["step_fingerprints"] == []
    assert corrected["terminal"] is False
