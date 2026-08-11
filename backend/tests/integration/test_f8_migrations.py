"""CP-F8.1 migration catalog and protected-fact tests."""

import importlib
import uuid
from types import ModuleType

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration

HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64


def _migration_module() -> ModuleType:
    return importlib.import_module("app.db.migrations.versions.0010_f8_two_dimensional_grading")


def test_0010_downgrade_guard_queries_all_f8_fact_tables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = _migration_module()
    captured_sql = ""

    class GuardBind:
        def scalar(self, statement: object) -> bool:
            nonlocal captured_sql
            captured_sql = str(statement)
            return True

    monkeypatch.setattr(migration.op, "get_bind", lambda: GuardBind())
    with pytest.raises(RuntimeError, match="cannot downgrade 0010"):
        migration._guard_downgrade()
    for table in (
        "grading_config",
        "grading_run",
        "grading_request",
        "grading_item",
        "grading_item_row",
    ):
        assert f"FROM {table}" in captured_sql


async def test_0010_catalog_has_restrict_fks_and_deferred_snapshot_triggers(
    db_session: AsyncSession,
) -> None:
    tables = set(
        (
            await db_session.execute(
                text(
                    "SELECT tablename FROM pg_tables WHERE schemaname='public' "
                    "AND tablename LIKE 'grading_%'"
                )
            )
        ).scalars()
    )
    assert {
        "grading_config",
        "grading_run",
        "grading_request",
        "grading_item",
        "grading_item_row",
    } <= tables
    delete_actions = set(
        (
            await db_session.execute(
                text(
                    "SELECT confdeltype FROM pg_constraint WHERE contype='f' "
                    "AND conrelid IN ('grading_config'::regclass,'grading_run'::regclass,"
                    "'grading_request'::regclass,'grading_item'::regclass,"
                    "'grading_item_row'::regclass)"
                )
            )
        ).scalars()
    )
    assert delete_actions == {"r"}
    deferred = {
        tuple(row)
        for row in (
            await db_session.execute(
                text(
                    "SELECT tgname,tgdeferrable,tginitdeferred FROM pg_trigger "
                    "WHERE tgname LIKE 'trg_grading_%_snapshot_consistent' ORDER BY tgname"
                )
            )
        )
    }
    assert deferred == {
        ("trg_grading_item_row_snapshot_consistent", True, True),
        ("trg_grading_item_snapshot_consistent", True, True),
        ("trg_grading_run_snapshot_consistent", True, True),
    }
    invalidation = set(
        (
            await db_session.execute(
                text(
                    "SELECT tgname FROM pg_trigger WHERE "
                    "tgname LIKE 'trg_grading_%_snapshot_invalidate'"
                )
            )
        ).scalars()
    )
    assert invalidation == {
        "trg_grading_run_snapshot_invalidate",
        "trg_grading_item_snapshot_invalidate",
        "trg_grading_item_row_snapshot_invalidate",
    }
    functions = set(
        (
            await db_session.execute(
                text(
                    "SELECT proname FROM pg_proc WHERE proname IN "
                    "('f8_prepare_validation_cache','f8_invalidate_grading_snapshot',"
                    "'f8_validate_grading_snapshot_once')"
                )
            )
        ).scalars()
    )
    assert functions == {
        "f8_prepare_validation_cache",
        "f8_invalidate_grading_snapshot",
        "f8_validate_grading_snapshot_once",
    }


async def test_0010_source_snapshot_uniques_and_legacy_severity_are_unchanged(
    db_session: AsyncSession,
) -> None:
    names = set(
        (
            await db_session.execute(
                text(
                    "SELECT conname FROM pg_constraint WHERE conname IN "
                    "('uq_finding_f8_source_identity','uq_correlation_finding_f8_snapshot',"
                    "'uq_capability_declaration_f8_snapshot',"
                    "'uq_investigation_result_f8_snapshot')"
                )
            )
        ).scalars()
    )
    assert names == {
        "uq_finding_f8_source_identity",
        "uq_correlation_finding_f8_snapshot",
        "uq_capability_declaration_f8_snapshot",
        "uq_investigation_result_f8_snapshot",
    }
    severity_check = await db_session.scalar(
        text(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conrelid='correlation_finding'::regclass "
            "AND conname='ck_correlation_finding_severity_ungraded'"
        )
    )
    assert severity_check is not None
    assert "severity_impact = 0" in str(severity_check)
    assert "severity_confidence = 0" in str(severity_check)


async def test_0010_reason_codes_are_known_unique_and_ordered(
    db_session: AsyncSession,
) -> None:
    assert await db_session.scalar(
        text("SELECT f8_reason_codes_valid(CAST(:codes AS jsonb))"),
        {"codes": '["IMPACT_RULE_MAPPING","CONFIDENCE_F3_FLAGGED","FINAL_CLEARED"]'},
    )
    for codes in (
        '["UNKNOWN"]',
        '["FINAL_CLEARED","IMPACT_RULE_MAPPING"]',
        '["IMPACT_RULE_MAPPING","IMPACT_RULE_MAPPING"]',
        "[]",
    ):
        assert not await db_session.scalar(
            text("SELECT f8_reason_codes_valid(CAST(:codes AS jsonb))"), {"codes": codes}
        )


async def test_0010_config_is_tenant_bound_immutable_and_guards_downgrade(
    db_session: AsyncSession,
) -> None:
    ids = {name: uuid.uuid4() for name in ("tenant", "other", "user", "config")}
    await db_session.execute(
        text(
            "INSERT INTO tenant(id,slug,name) VALUES "
            "(:tenant,:slug,'F8'),(:other,:other_slug,'Other')"
        ),
        {**ids, "slug": f"f8-{ids['tenant'].hex[:8]}", "other_slug": f"o-{ids['other'].hex[:8]}"},
    )
    await db_session.execute(
        text(
            "INSERT INTO app_user(id,tenant_id,username,password_hash,role,is_active) "
            "VALUES (:user,:tenant,'f8-user','test','configurator',true)"
        ),
        ids,
    )
    await db_session.execute(
        text(
            "INSERT INTO grading_config "
            "(id,tenant_id,version,schema_version,definition_json,canonical_definition,"
            "config_fingerprint,algorithm_version,created_by,change_reason,"
            "idempotency_key_hash,request_fingerprint) VALUES "
            "(:config,:tenant,1,1,'{}','{}',:a,'cost-matrix-v1',:user,'initial',:b,:c)"
        ),
        {**ids, "a": HASH_A, "b": HASH_B, "c": HASH_C},
    )
    with pytest.raises(DBAPIError, match="F8_IMMUTABLE_FACT"):
        async with db_session.begin_nested():
            await db_session.execute(
                text("UPDATE grading_config SET change_reason='changed' WHERE id=:config"), ids
            )
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await db_session.execute(
                text(
                    "INSERT INTO grading_config "
                    "(id,tenant_id,version,schema_version,definition_json,canonical_definition,"
                    "config_fingerprint,algorithm_version,created_by,change_reason,"
                    "idempotency_key_hash,request_fingerprint) VALUES "
                    "(gen_random_uuid(),:other,1,1,'{}','{}',:a,'cost-matrix-v1',:user,"
                    "'wrong tenant',:b,:c)"
                ),
                {**ids, "a": "d" * 64, "b": "e" * 64, "c": "f" * 64},
            )
