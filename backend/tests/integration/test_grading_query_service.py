"""PostgreSQL integration coverage for the CP-F8.3 grading query service."""

from __future__ import annotations

import copy
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass

import pytest
from sqlalchemy import event, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.core.detection.config_service import create_detection_config
from app.core.detection.models import DetectionProfileDefinition
from app.core.detection.run_service import run_detection
from app.core.grading.config_service import create_grading_config
from app.core.grading.errors import GradingInputError, GradingInternalError, GradingNotFoundError
from app.core.grading.models import (
    DISPOSITION_ORDER,
    SOURCE_KIND_ORDER,
    SourceKind,
)
from app.core.grading.query_service import (
    get_batch_grading,
    get_grading_item,
    get_grading_run,
    list_grading_item_rows,
    list_grading_items,
)
from app.core.grading.run_service import run_grading
from app.core.grading.service_models import F7ManifestRequest, GradingRunView
from app.core.tenancy.scope import bind_tenant
from app.db.models.batch import ExpenseRow
from app.db.models.grading import GradingItem
from tests.integration.test_detection_services import _seed_batch
from tests.integration.test_grading_manifest_builder import _seed_complete_sources
from tests.unit.detection.helpers import profile_data
from tests.unit.grading.helpers import config_payload

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("clean_db")]


@dataclass(frozen=True)
class _Snapshot:
    tenant_id: uuid.UUID
    actor_id: uuid.UUID
    file_id: uuid.UUID
    validation_id: uuid.UUID
    detection_id: uuid.UUID
    config_id: uuid.UUID
    f7_requests: tuple[F7ManifestRequest, ...]
    run: GradingRunView


async def _seed_grading_snapshot(
    session_factory: async_sessionmaker[AsyncSession],
) -> _Snapshot:
    (
        tenant_id,
        actor_id,
        file_id,
        validation_id,
        detection_id,
        requests,
    ) = await _seed_complete_sources(session_factory)
    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        grading_config = await create_grading_config(
            session,
            session_factory,
            tenant_id=tenant_id,
            actor_id=actor_id,
            expected_current_version=0,
            definition=config_payload(),
            change_reason="query integration baseline",
            idempotency_key="f8-query-config-key-1",
        )
        await session.commit()
    run = await run_grading(
        session_factory,
        tenant_id=tenant_id,
        actor_id=actor_id,
        file_version_id=file_id,
        validation_run_id=validation_id,
        detection_run_id=detection_id,
        grading_config_id=grading_config.id,
        f7_requests=requests,
        idempotency_key="f8-query-run-key-1",
    )
    return _Snapshot(
        tenant_id=tenant_id,
        actor_id=actor_id,
        file_id=file_id,
        validation_id=validation_id,
        detection_id=detection_id,
        config_id=grading_config.id,
        f7_requests=requests,
        run=run,
    )


@asynccontextmanager
async def _statement_counter(engine: AsyncEngine) -> AsyncIterator[Callable[[], int]]:
    count = 0

    def increment(
        connection: object,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        del connection, cursor, statement, parameters, context, executemany
        nonlocal count
        count += 1

    sync_engine: Engine = engine.sync_engine
    event.listen(sync_engine, "before_cursor_execute", increment)
    try:
        yield lambda: count
    finally:
        event.remove(sync_engine, "before_cursor_execute", increment)


async def test_tenant_scoped_run_item_and_file_queries_fail_as_not_found(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    snapshot = await _seed_grading_snapshot(session_factory)
    foreign_tenant, _, _ = await _seed_batch(
        session_factory, slug=f"f8-query-foreign-{uuid.uuid4().hex[:8]}"
    )
    async with session_factory() as owner:
        bind_tenant(owner.sync_session, snapshot.tenant_id)
        page = await list_grading_items(
            owner, tenant_id=snapshot.tenant_id, grading_run_id=snapshot.run.id
        )
        assert page.items
        item_id = page.items[0].id

    async with session_factory() as foreign:
        bind_tenant(foreign.sync_session, foreign_tenant)
        calls = (
            lambda: get_grading_run(
                foreign, tenant_id=foreign_tenant, grading_run_id=snapshot.run.id
            ),
            lambda: get_batch_grading(
                foreign, tenant_id=foreign_tenant, file_version_id=snapshot.file_id
            ),
            lambda: list_grading_items(
                foreign, tenant_id=foreign_tenant, grading_run_id=snapshot.run.id
            ),
            lambda: get_grading_item(foreign, tenant_id=foreign_tenant, grading_item_id=item_id),
            lambda: list_grading_item_rows(
                foreign, tenant_id=foreign_tenant, grading_item_id=item_id
            ),
        )
        for call in calls:
            with pytest.raises(GradingNotFoundError):
                await call()


async def test_current_view_returns_exact_baselines_and_stale_flags(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    snapshot = await _seed_grading_snapshot(session_factory)
    async with session_factory() as session:
        bind_tenant(session.sync_session, snapshot.tenant_id)
        current = await get_batch_grading(
            session, tenant_id=snapshot.tenant_id, file_version_id=snapshot.file_id
        )
    assert current.run is not None and current.run.id == snapshot.run.id
    assert current.current_config_id == snapshot.config_id
    assert current.current_validation_run_id == snapshot.validation_id
    assert current.current_detection_run_id == snapshot.detection_id
    assert current.config_stale is False
    assert current.validation_run_stale is False
    assert current.detection_run_stale is False

    async with session_factory() as session:
        bind_tenant(session.sync_session, snapshot.tenant_id)
        newer_config = await create_grading_config(
            session,
            session_factory,
            tenant_id=snapshot.tenant_id,
            actor_id=snapshot.actor_id,
            expected_current_version=1,
            definition=config_payload(false_positive_cost_units=200),
            change_reason="newer query comparison baseline",
            idempotency_key="f8-query-config-key-2",
        )
        await session.commit()

    changed_profile = copy.deepcopy(profile_data())
    detectors = changed_profile["detectors"]
    assert isinstance(detectors, list) and isinstance(detectors[0], dict)
    detectors[0]["date_window_days"] = 3
    async with session_factory() as session:
        bind_tenant(session.sync_session, snapshot.tenant_id)
        await create_detection_config(
            session,
            session_factory,
            tenant_id=snapshot.tenant_id,
            actor_id=snapshot.actor_id,
            expected_current_version=1,
            definition=DetectionProfileDefinition.model_validate(changed_profile),
            change_reason="newer query comparison profile",
            idempotency_key="f8-query-detection-config-key-2",
        )
        await session.commit()
    async with session_factory() as session:
        bind_tenant(session.sync_session, snapshot.tenant_id)
        newer_detection = await run_detection(
            session,
            session_factory,
            tenant_id=snapshot.tenant_id,
            actor_id=snapshot.actor_id,
            file_version_id=snapshot.file_id,
            idempotency_key="f8-query-detection-run-key-2",
        )
        await session.commit()
    async with session_factory() as session:
        bind_tenant(session.sync_session, snapshot.tenant_id)
        stale = await get_batch_grading(
            session, tenant_id=snapshot.tenant_id, file_version_id=snapshot.file_id
        )
    assert stale.run is not None and stale.run.id == snapshot.run.id
    assert stale.current_config_id == newer_config.id
    assert stale.current_validation_run_id == snapshot.validation_id
    assert stale.current_detection_run_id == newer_detection.id
    assert stale.config_stale is True
    assert stale.validation_run_stale is False
    assert stale.detection_run_stale is True


async def test_item_filters_stable_sort_exact_total_and_database_pagination(
    session_factory: async_sessionmaker[AsyncSession], engine: AsyncEngine
) -> None:
    snapshot = await _seed_grading_snapshot(session_factory)
    async with session_factory() as session:
        bind_tenant(session.sync_session, snapshot.tenant_id)
        run = await get_grading_run(
            session, tenant_id=snapshot.tenant_id, grading_run_id=snapshot.run.id
        )
        assert run == snapshot.run.model_copy(update={"reused_existing": True})
        full = await list_grading_items(
            session,
            tenant_id=snapshot.tenant_id,
            grading_run_id=snapshot.run.id,
            limit=200,
        )
        records = tuple(
            (
                await session.scalars(
                    select(GradingItem).where(GradingItem.grading_run_id == snapshot.run.id)
                )
            ).all()
        )
        disposition_rank = {item.value: rank for rank, item in enumerate(DISPOSITION_ORDER)}
        source_rank = {item.value: rank for rank, item in enumerate(SOURCE_KIND_ORDER)}
        expected = tuple(
            item.id
            for item in sorted(
                records,
                key=lambda item: (
                    disposition_rank[item.disposition],
                    -item.severity_impact,
                    -item.severity_confidence,
                    item.first_row_no,
                    source_rank[item.source_kind],
                    item.finding_id or item.correlation_finding_id,
                    item.id,
                ),
            )
        )
        assert (
            full.total == len(records) == run.deterministic_item_count + run.correlation_item_count
        )
        assert tuple(item.id for item in full.items) == expected
        assert {item.source_kind for item in full.items} == {
            SourceKind.DETERMINISTIC,
            SourceKind.CORRELATION,
        }

        first = await list_grading_items(
            session,
            tenant_id=snapshot.tenant_id,
            grading_run_id=snapshot.run.id,
            limit=1,
            offset=0,
        )
        second = await list_grading_items(
            session,
            tenant_id=snapshot.tenant_id,
            grading_run_id=snapshot.run.id,
            limit=1,
            offset=1,
        )
        assert first.total == second.total == full.total
        assert tuple(item.id for item in (*first.items, *second.items)) == expected[:2]

        filter_cases: list[tuple[str, object]] = []
        for field in (
            "source_kind",
            "rule_kind",
            "detector",
            "severity_impact",
            "severity_confidence",
            "disposition",
            "f7_outcome",
        ):
            values = {
                getattr(item, field) for item in full.items if getattr(item, field) is not None
            }
            assert values, field
            filter_cases.extend(
                (field, SourceKind(value) if field == "source_kind" else value) for value in values
            )
        for field, value in filter_cases:
            filtered = await list_grading_items(
                session,
                tenant_id=snapshot.tenant_id,
                grading_run_id=snapshot.run.id,
                limit=200,
                **{field: value},
            )
            expected_count = sum(getattr(item, field) == value for item in full.items)
            assert filtered.total == expected_count
            assert len(filtered.items) == expected_count
            assert all(getattr(item, field) == value for item in filtered.items)

        for arguments in (
            {"limit": 0},
            {"limit": 201},
            {"limit": True},
            {"offset": -1},
            {"offset": False},
            {"sort_by": "impact"},
            {"severity_impact": -1},
            {"severity_impact": 4},
            {"severity_impact": True},
            {"severity_confidence": -1},
            {"severity_confidence": 4},
            {"severity_confidence": False},
        ):
            with pytest.raises(GradingInputError) as caught:
                await list_grading_items(
                    session,
                    tenant_id=snapshot.tenant_id,
                    grading_run_id=snapshot.run.id,
                    **arguments,
                )
            assert caught.value.code == "GRADING_QUERY_INVALID"

        async with _statement_counter(engine) as statements:
            bounded = await list_grading_items(
                session,
                tenant_id=snapshot.tenant_id,
                grading_run_id=snapshot.run.id,
                limit=1,
            )
        assert bounded.total == full.total and statements() == 3


async def test_row_pagination_fingerprint_readback_drift_and_bounded_queries(
    session_factory: async_sessionmaker[AsyncSession], engine: AsyncEngine
) -> None:
    snapshot = await _seed_grading_snapshot(session_factory)
    async with session_factory() as session:
        bind_tenant(session.sync_session, snapshot.tenant_id)
        items = await list_grading_items(
            session,
            tenant_id=snapshot.tenant_id,
            grading_run_id=snapshot.run.id,
            source_kind=SourceKind.CORRELATION,
            limit=200,
        )
        item = max(items.items, key=lambda candidate: candidate.first_row_no)
        detail = await get_grading_item(
            session, tenant_id=snapshot.tenant_id, grading_item_id=item.id
        )
        assert detail == item
        with pytest.raises(GradingInputError):
            await list_grading_item_rows(
                session,
                tenant_id=snapshot.tenant_id,
                grading_item_id=item.id,
                limit=0,
            )

        async with _statement_counter(engine) as statements:
            rows = await list_grading_item_rows(
                session,
                tenant_id=snapshot.tenant_id,
                grading_item_id=item.id,
                limit=200,
            )
        assert statements() == 3
        assert rows.total == len(rows.items) >= 2
        assert tuple((row.ordinal, row.row_no, row.id) for row in rows.items) == tuple(
            sorted((row.ordinal, row.row_no, row.id) for row in rows.items)
        )
        assert all(row.normalized.mapping_version_id is not None for row in rows.items)
        first = await list_grading_item_rows(
            session,
            tenant_id=snapshot.tenant_id,
            grading_item_id=item.id,
            limit=1,
            offset=0,
        )
        second = await list_grading_item_rows(
            session,
            tenant_id=snapshot.tenant_id,
            grading_item_id=item.id,
            limit=1,
            offset=1,
        )
        assert first.total == second.total == rows.total
        assert tuple(row.id for row in (*first.items, *second.items)) == tuple(
            row.id for row in rows.items[:2]
        )

        drifted = rows.items[0].normalized.model_copy(update={"amount": "9999.00"})
        await session.execute(
            update(ExpenseRow)
            .where(
                ExpenseRow.file_version_id == snapshot.file_id,
                ExpenseRow.row_no == rows.items[0].row_no,
            )
            .values(normalized_json=drifted.model_dump(mode="json"))
        )
        await session.flush()
        with pytest.raises(GradingInternalError) as caught:
            await list_grading_item_rows(
                session,
                tenant_id=snapshot.tenant_id,
                grading_item_id=item.id,
                limit=200,
            )
        assert caught.value.code == "GRADING_SOURCE_ROW_DRIFT"
