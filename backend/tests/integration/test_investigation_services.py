"""CP-F7.3 run/query, terminal, replay, and recovery integration tests."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import uuid
from collections.abc import Callable
from pathlib import Path

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession, async_sessionmaker

from app.core.agent.models import (
    GetCorrelationRowsCall,
    GetEmployeeHistoryCall,
    GetSupplierHistoryCall,
    ProviderResponse,
    SearchPolicyClausesCall,
    TerminateAction,
    ToolCallAction,
)
from app.core.agent.provider import ScriptedLlmProvider
from app.core.agent.query_service import (
    get_investigation,
    list_candidate_investigations,
    list_investigation_steps,
)
from app.core.agent.redaction import PiiKind
from app.core.agent.run_service import run_investigation
from app.core.agent.sanitization import PiiTokenDraft
from app.core.agent.service_models import (
    CheckpointReconciler,
    InvestigationDetail,
    InvestigationRunOptions,
    InvestigationSeed,
    InvestigationServiceError,
    ReconciliationSnapshot,
)
from app.core.agent.tools import ToolContext, ToolObservation
from app.core.tenancy.scope import bind_tenant
from app.db.models.audit import AuditLog
from app.db.models.findings import EvidenceStep
from app.db.models.investigation import (
    InvestigationRequest,
    InvestigationResult,
    InvestigationRun,
    PiiToken,
)

pytestmark = pytest.mark.integration

HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64
HASH_D = "d" * 64
HASH_E = "e" * 64
HASH_F = "f" * 64


class CountingTools:
    def __init__(self) -> None:
        self.call_count = 0

    async def get_correlation_rows(
        self, context: ToolContext, call: GetCorrelationRowsCall
    ) -> ToolObservation:
        del context, call
        self.call_count += 1
        return ToolObservation(tool_name="get_correlation_rows", data={"rows": []})

    async def get_employee_history(
        self, context: ToolContext, call: GetEmployeeHistoryCall
    ) -> ToolObservation:
        del context, call
        raise AssertionError("unexpected employee tool")

    async def get_supplier_history(
        self, context: ToolContext, call: GetSupplierHistoryCall
    ) -> ToolObservation:
        del context, call
        raise AssertionError("unexpected supplier tool")

    async def search_policy_clauses(
        self, context: ToolContext, call: SearchPolicyClausesCall
    ) -> ToolObservation:
        del context, call
        raise AssertionError("unexpected policy tool")


class RecordingReconciler:
    def __init__(self) -> None:
        self.snapshots: list[ReconciliationSnapshot] = []

    async def reconcile(self, snapshot: ReconciliationSnapshot) -> None:
        self.snapshots.append(snapshot)


async def _seed_candidate(conn: AsyncConnection) -> dict[str, uuid.UUID]:
    ids = {
        name: uuid.uuid4()
        for name in (
            "tenant",
            "other_tenant",
            "user",
            "other_user",
            "file",
            "config",
            "run",
            "finding",
        )
    }
    await conn.execute(
        text(
            "INSERT INTO tenant (id,slug,name) VALUES "
            "(:tenant,:slug,'F7 service'),(:other_tenant,:other_slug,'Other')"
        ),
        {
            **ids,
            "slug": f"f7-service-{ids['tenant'].hex[:8]}",
            "other_slug": f"f7-other-{ids['other_tenant'].hex[:8]}",
        },
    )
    await conn.execute(
        text(
            "INSERT INTO app_user "
            "(id,tenant_id,username,password_hash,role,is_active) VALUES "
            "(:user,:tenant,'f7-service','test','auditor',true),"
            "(:other_user,:other_tenant,'f7-other','test','auditor',true)"
        ),
        ids,
    )
    await conn.execute(
        text(
            "INSERT INTO file_version "
            "(id,tenant_id,filename,content_hash,row_count,uploaded_by,parse_status,"
            "parsed_at,revision_no) VALUES "
            "(:file,:tenant,'f7-service.xlsx',:hash,2,:user,'parsed',now(),1)"
        ),
        {**ids, "hash": HASH_A},
    )
    await conn.execute(
        text(
            "INSERT INTO detection_config "
            "(id,tenant_id,version,definition,definition_canonical,schema_version,"
            "algorithm_bundle_version,config_fingerprint,created_by,change_reason,"
            "idempotency_key_hash,request_fingerprint) VALUES "
            "(:config,:tenant,1,'{}'::jsonb,'{}',1,'correlation-v1',:hash_a,:user,"
            "'fixture',:hash_b,:hash_c)"
        ),
        {**ids, "hash_a": HASH_A, "hash_b": HASH_B, "hash_c": HASH_C},
    )
    await conn.execute(
        text(
            "INSERT INTO detection_run "
            "(id,tenant_id,file_version_id,detection_config_id,config_version,"
            "config_fingerprint,algorithm_bundle_version,input_fingerprint,run_fingerprint,"
            "source_row_count,parsed_row_count,error_row_count,finding_count,created_by,"
            "completed_at) VALUES "
            "(:run,:tenant,:file,:config,1,:hash_a,'correlation-v1',:hash_d,:hash_e,"
            "2,2,0,1,:user,now())"
        ),
        {**ids, "hash_a": HASH_A, "hash_d": HASH_D, "hash_e": HASH_E},
    )
    await conn.execute(
        text(
            "INSERT INTO correlation_finding "
            "(id,tenant_id,file_version_id,detection_run_id,detector,detector_version,"
            "finding_key,evidence_schema_version,evidence_json,reasoning_snapshot,"
            "severity_impact,severity_confidence) VALUES "
            "(:finding,:tenant,:file,:run,'split_invoice','split-window-v1',:hash_f,1,"
            "'{}'::jsonb,'candidate',0,0)"
        ),
        {**ids, "hash_f": HASH_F},
    )
    return ids


def _seed(
    ids: dict[str, uuid.UUID],
    *,
    finding_id: uuid.UUID | None = None,
    token_drafts: tuple[PiiTokenDraft, ...] = (),
) -> InvestigationSeed:
    chosen_finding = finding_id or ids["finding"]
    return InvestigationSeed(
        evidence={
            "schema_version": 1,
            "candidate": {"finding_id": str(chosen_finding), "rows": [1, 2]},
        },
        tool_context=ToolContext(
            tenant_id=ids["tenant"],
            detection_run_id=ids["run"],
            correlation_finding_id=chosen_finding,
        ),
        token_drafts=token_drafts,
    )


def _token_draft(
    *,
    source_hmac: str = HASH_A,
    display_value: str = "EMP_v1_0123456789abcdef",
) -> PiiTokenDraft:
    return PiiTokenDraft(
        token_version="v1",
        pii_kind=PiiKind.EMPLOYEE_NAME,
        source_hmac=source_hmac,
        token=display_value,
    )


def _options(*, max_steps: int = 6, disabled: bool = False) -> InvestigationRunOptions:
    return InvestigationRunOptions(
        provider_kind="disabled" if disabled else "openai_compatible",
        provider_model="unconfigured" if disabled else "scripted-test",
        max_steps=max_steps,
        timeout_seconds=30,
        redaction_version="v1",
    )


def _terminate(*, sufficient: bool) -> ProviderResponse:
    return ProviderResponse(
        action=TerminateAction(
            outcome="sufficient" if sufficient else "insufficient",
            evidence_sufficient=sufficient,
            reason_code="EVIDENCE_SUFFICIENT" if sufficient else "EVIDENCE_INSUFFICIENT",
            summary="证据充分" if sufficient else "证据不足，需要人工复核",
        )
    )


def _tool_call() -> ProviderResponse:
    return ProviderResponse(
        action=ToolCallAction(
            call=GetCorrelationRowsCall(),
            rationale_summary="读取参与行",
        )
    )


async def _run(
    session_factory: async_sessionmaker[AsyncSession],
    ids: dict[str, uuid.UUID],
    *,
    key: str,
    provider: ScriptedLlmProvider | None,
    tools: CountingTools,
    options: InvestigationRunOptions | None = None,
    seed: InvestigationSeed | None = None,
    reconciler: CheckpointReconciler | None = None,
    fault_hook: Callable[[str], None] | None = None,
) -> InvestigationDetail:
    return await run_investigation(
        session_factory,
        tenant_id=ids["tenant"],
        actor_id=ids["user"],
        detection_run_id=ids["run"],
        correlation_finding_id=(seed or _seed(ids)).tool_context.correlation_finding_id,
        file_version_id=ids["file"],
        idempotency_key=key,
        options=options or _options(),
        seed=seed or _seed(ids),
        provider=provider,
        tools=tools,
        reconciler=reconciler,
        fault_hook=fault_hook,
    )


async def test_sufficient_replay_is_zero_provider_tool_and_queries_are_stable(
    engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
    clean_db: None,
) -> None:
    del clean_db
    async with engine.begin() as conn:
        ids = await _seed_candidate(conn)
    provider = ScriptedLlmProvider([_terminate(sufficient=True)])
    tools = CountingTools()
    created = await _run(session_factory, ids, key="f7-replay-0001", provider=provider, tools=tools)
    assert created.result is not None and created.result.outcome == "sufficient"
    assert len(created.steps) == 1
    assert provider.call_count == 1

    replay_checkpoint = RecordingReconciler()
    replayed = await _run(
        session_factory,
        ids,
        key="f7-replay-0001",
        provider=provider,
        tools=tools,
        options=_options(max_steps=1, disabled=True),
        reconciler=replay_checkpoint,
    )
    assert replayed.reused_existing
    assert replayed.run.id == created.run.id
    assert provider.call_count == 1
    assert tools.call_count == 0
    assert replay_checkpoint.snapshots == []

    async with session_factory() as db:
        bind_tenant(db.sync_session, ids["tenant"])
        detail = await get_investigation(
            db, tenant_id=ids["tenant"], investigation_run_id=created.run.id
        )
        page = await list_candidate_investigations(
            db,
            tenant_id=ids["tenant"],
            detection_run_id=ids["run"],
            correlation_finding_id=ids["finding"],
            limit=20,
            offset=0,
        )
        steps = await list_investigation_steps(
            db,
            tenant_id=ids["tenant"],
            investigation_run_id=created.run.id,
            limit=20,
            offset=0,
        )
        assert detail.result is not None
        assert page.total == 1 and len(page.items) == 1
        assert steps.total == 1 and steps.items[0].step_no == 1
        assert steps.items[0].tenant_id == ids["tenant"]
        assert steps.items[0].correlation_finding_id == ids["finding"]
        assert steps.items[0].detection_run_id == ids["run"]
        assert steps.items[0].file_version_id == ids["file"]
        assert (
            await db.scalar(
                select(func.count())
                .select_from(AuditLog)
                .where(AuditLog.action == "investigation.run_terminal")
            )
            == 1
        )


async def test_same_key_different_identity_conflicts_before_provider(
    engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
    clean_db: None,
) -> None:
    del clean_db
    async with engine.begin() as conn:
        ids = await _seed_candidate(conn)
    first = ScriptedLlmProvider([_terminate(sufficient=False)])
    await _run(session_factory, ids, key="f7-conflict-001", provider=first, tools=CountingTools())
    other_finding = uuid.uuid4()
    second = ScriptedLlmProvider([_terminate(sufficient=True)])
    with pytest.raises(InvestigationServiceError, match="Idempotency-Key"):
        await _run(
            session_factory,
            ids,
            key="f7-conflict-001",
            provider=second,
            tools=CountingTools(),
            seed=_seed(ids, finding_id=other_finding),
        )
    assert second.call_count == 0


@pytest.mark.parametrize(
    ("case", "expected", "step_count"),
    [
        ("unavailable", "unavailable", 0),
        ("failed", "failed", 0),
        ("insufficient", "insufficient", 1),
        ("max_steps", "max_steps", 1),
    ],
)
async def test_explicit_non_sufficient_terminal_outcomes(
    case: str,
    expected: str,
    step_count: int,
    engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
    clean_db: None,
) -> None:
    del clean_db
    async with engine.begin() as conn:
        ids = await _seed_candidate(conn)
    tools = CountingTools()
    if case == "unavailable":
        provider = None
        options = _options(disabled=True)
    elif case == "failed":
        provider = ScriptedLlmProvider([])
        options = _options()
    elif case == "insufficient":
        provider = ScriptedLlmProvider([_terminate(sufficient=False)])
        options = _options()
    else:
        provider = ScriptedLlmProvider([_tool_call()])
        options = _options(max_steps=1)
    result = await _run(
        session_factory,
        ids,
        key=f"f7-terminal-{case}",
        provider=provider,
        tools=tools,
        options=options,
    )
    assert result.result is not None
    assert result.result.outcome == expected
    assert result.result.evidence_sufficient is (False if case == "insufficient" else None)
    assert len(result.steps) == step_count


class SimulatedCrash(BaseException):
    pass


async def test_step_transaction_rolls_back_then_same_key_resumes(
    engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
    clean_db: None,
) -> None:
    del clean_db
    async with engine.begin() as conn:
        ids = await _seed_candidate(conn)

    def crash(stage: str) -> None:
        if stage == "step_written":
            raise SimulatedCrash

    with pytest.raises(SimulatedCrash):
        await _run(
            session_factory,
            ids,
            key="f7-crash-0001",
            provider=ScriptedLlmProvider([_tool_call()]),
            tools=CountingTools(),
            fault_hook=crash,
        )
    async with session_factory() as db:
        bind_tenant(db.sync_session, ids["tenant"])
        assert await db.scalar(select(func.count()).select_from(InvestigationRun)) == 1
        assert await db.scalar(select(func.count()).select_from(InvestigationRequest)) == 1
        assert await db.scalar(select(func.count()).select_from(EvidenceStep)) == 0
        assert await db.scalar(select(func.count()).select_from(InvestigationResult)) == 0

    resumed_provider = ScriptedLlmProvider([_terminate(sufficient=False)])
    resumed = await _run(
        session_factory,
        ids,
        key="f7-crash-0001",
        provider=resumed_provider,
        tools=CountingTools(),
    )
    assert resumed.result is not None and resumed.result.outcome == "insufficient"
    assert len(resumed.steps) == 1


async def test_first_run_persists_token_drafts_atomically_and_replay_deduplicates(
    engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
    clean_db: None,
) -> None:
    del clean_db
    async with engine.begin() as conn:
        ids = await _seed_candidate(conn)
    seed = _seed(ids, token_drafts=(_token_draft(), _token_draft()))

    created = await _run(
        session_factory,
        ids,
        key="f7-token-atomic-1",
        provider=ScriptedLlmProvider([_terminate(sufficient=False)]),
        tools=CountingTools(),
        seed=seed,
    )
    replayed = await _run(
        session_factory,
        ids,
        key="f7-token-atomic-1",
        provider=ScriptedLlmProvider([]),
        tools=CountingTools(),
        seed=seed,
    )
    assert replayed.reused_existing and replayed.run.id == created.run.id

    async with session_factory() as db:
        bind_tenant(db.sync_session, ids["tenant"])
        tokens = tuple((await db.scalars(select(PiiToken))).all())
        assert len(tokens) == 1
        assert tokens[0].source_hmac == HASH_A
        assert tokens[0].token == "EMP_v1_0123456789abcdef"


async def test_token_draft_fault_rolls_back_tokens_run_and_request_then_recovers(
    engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
    clean_db: None,
) -> None:
    del clean_db
    async with engine.begin() as conn:
        ids = await _seed_candidate(conn)
    seed = _seed(ids, token_drafts=(_token_draft(),))

    def crash(stage: str) -> None:
        if stage == "pii_tokens_written":
            raise SimulatedCrash

    with pytest.raises(SimulatedCrash):
        await _run(
            session_factory,
            ids,
            key="f7-token-rollback",
            provider=ScriptedLlmProvider([_terminate(sufficient=True)]),
            tools=CountingTools(),
            seed=seed,
            fault_hook=crash,
        )
    async with session_factory() as db:
        bind_tenant(db.sync_session, ids["tenant"])
        assert await db.scalar(select(func.count()).select_from(PiiToken)) == 0
        assert await db.scalar(select(func.count()).select_from(InvestigationRun)) == 0
        assert await db.scalar(select(func.count()).select_from(InvestigationRequest)) == 0

    recovered = await _run(
        session_factory,
        ids,
        key="f7-token-rollback",
        provider=ScriptedLlmProvider([_terminate(sufficient=True)]),
        tools=CountingTools(),
        seed=seed,
    )
    assert recovered.result is not None and recovered.result.outcome == "sufficient"


@pytest.mark.parametrize(
    ("stored_hmac", "stored_token"),
    [
        (HASH_A, "EMP_v1_fedcba9876543210"),
        (HASH_B, "EMP_v1_0123456789abcdef"),
    ],
)
async def test_token_source_or_display_collision_fails_closed_without_partial_run(
    stored_hmac: str,
    stored_token: str,
    engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
    clean_db: None,
) -> None:
    del clean_db
    async with engine.begin() as conn:
        ids = await _seed_candidate(conn)
    async with session_factory() as db:
        bind_tenant(db.sync_session, ids["tenant"])
        async with db.begin():
            db.add(
                PiiToken(
                    tenant_id=ids["tenant"],
                    token_version="v1",
                    pii_kind="employee_name",
                    source_hmac=stored_hmac,
                    token=stored_token,
                )
            )

    provider = ScriptedLlmProvider([_terminate(sufficient=True)])
    with pytest.raises(InvestigationServiceError) as collision:
        await _run(
            session_factory,
            ids,
            key=f"f7-token-collision-{stored_hmac[0]}",
            provider=provider,
            tools=CountingTools(),
            seed=_seed(ids, token_drafts=(_token_draft(),)),
        )
    assert collision.value.code == "PII_TOKEN_COLLISION"
    assert provider.call_count == 0
    async with session_factory() as db:
        bind_tenant(db.sync_session, ids["tenant"])
        assert await db.scalar(select(func.count()).select_from(PiiToken)) == 1
        assert await db.scalar(select(func.count()).select_from(InvestigationRun)) == 0
        assert await db.scalar(select(func.count()).select_from(InvestigationRequest)) == 0


@pytest.mark.parametrize("stage", ["result_written", "terminal_audit_written"])
async def test_result_and_terminal_audit_roll_back_together_then_business_fact_resumes(
    stage: str,
    engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
    clean_db: None,
) -> None:
    del clean_db
    async with engine.begin() as conn:
        ids = await _seed_candidate(conn)

    def crash(current: str) -> None:
        if current == stage:
            raise SimulatedCrash

    with pytest.raises(SimulatedCrash):
        await _run(
            session_factory,
            ids,
            key=f"f7-terminal-atomic-{stage}",
            provider=ScriptedLlmProvider([_terminate(sufficient=True)]),
            tools=CountingTools(),
            fault_hook=crash,
        )
    async with session_factory() as db:
        bind_tenant(db.sync_session, ids["tenant"])
        assert await db.scalar(select(func.count()).select_from(EvidenceStep)) == 1
        assert await db.scalar(select(func.count()).select_from(InvestigationResult)) == 0
        assert (
            await db.scalar(
                select(func.count())
                .select_from(AuditLog)
                .where(AuditLog.action == "investigation.run_terminal")
            )
            == 0
        )

    provider = ScriptedLlmProvider([])
    tools = CountingTools()
    checkpoint = RecordingReconciler()
    recovered = await _run(
        session_factory,
        ids,
        key=f"f7-terminal-atomic-{stage}",
        provider=provider,
        tools=tools,
        reconciler=checkpoint,
    )
    assert recovered.result is not None and recovered.result.outcome == "sufficient"
    assert provider.call_count == 0 and tools.call_count == 0
    assert [snapshot.terminal for snapshot in checkpoint.snapshots] == [False, True]
    async with session_factory() as db:
        bind_tenant(db.sync_session, ids["tenant"])
        assert await db.scalar(select(func.count()).select_from(InvestigationResult)) == 1
        assert (
            await db.scalar(
                select(func.count())
                .select_from(AuditLog)
                .where(AuditLog.action == "investigation.run_terminal")
            )
            == 1
        )


async def test_committed_max_step_fact_wins_over_current_provider_unavailability(
    engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
    clean_db: None,
) -> None:
    del clean_db
    async with engine.begin() as conn:
        ids = await _seed_candidate(conn)

    def crash(stage: str) -> None:
        if stage == "result_written":
            raise SimulatedCrash

    with pytest.raises(SimulatedCrash):
        await _run(
            session_factory,
            ids,
            key="f7-max-step-business-fact",
            provider=ScriptedLlmProvider([_tool_call()]),
            tools=CountingTools(),
            options=_options(max_steps=1),
            fault_hook=crash,
        )

    recovered = await _run(
        session_factory,
        ids,
        key="f7-max-step-business-fact",
        provider=None,
        tools=CountingTools(),
        options=_options(disabled=True),
    )
    assert recovered.result is not None
    assert recovered.result.outcome == "max_steps"
    assert len(recovered.steps) == 1


async def test_hard_process_exit_recovers_committed_terminal_without_provider_call(
    engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
    db_url: str,
    clean_db: None,
) -> None:
    del clean_db
    async with engine.begin() as conn:
        ids = await _seed_candidate(conn)
    worker = Path(__file__).parents[1] / "helpers" / "f7_kill_worker.py"
    backend_dir = Path(__file__).parents[2]
    environment = {
        key: os.environ[key] for key in ("PATH", "SYSTEMROOT", "TEMP", "TMP") if key in os.environ
    }
    environment.update(
        {
            "F7_KILL_ACTOR_ID": str(ids["user"]),
            "F7_KILL_DB_URL": db_url,
            "F7_KILL_FILE_ID": str(ids["file"]),
            "F7_KILL_FINDING_ID": str(ids["finding"]),
            "F7_KILL_RUN_ID": str(ids["run"]),
            "F7_KILL_TENANT_ID": str(ids["tenant"]),
            "PYTHONPATH": str(backend_dir),
        }
    )
    process = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, str(worker)],
        capture_output=True,
        check=False,
        env=environment,
        timeout=30,
    )
    assert process.returncode == 91, process.stderr.decode("utf-8", errors="replace")

    async with session_factory() as db:
        bind_tenant(db.sync_session, ids["tenant"])
        assert await db.scalar(select(func.count()).select_from(InvestigationRun)) == 1
        assert await db.scalar(select(func.count()).select_from(InvestigationRequest)) == 1
        assert await db.scalar(select(func.count()).select_from(EvidenceStep)) == 1
        assert await db.scalar(select(func.count()).select_from(InvestigationResult)) == 0

    provider = ScriptedLlmProvider([])
    recovered = await _run(
        session_factory,
        ids,
        key="f7-hard-kill-key",
        provider=provider,
        tools=CountingTools(),
    )
    assert recovered.result is not None and recovered.result.outcome == "sufficient"
    assert provider.call_count == 0
