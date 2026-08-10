"""CP-F7.1 migration, tenant identity, and immutable-fact tests."""

import importlib
import uuid
from types import ModuleType

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, make_url
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

pytestmark = pytest.mark.integration

HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64
HASH_D = "d" * 64
HASH_E = "e" * 64
HASH_F = "f" * 64


def _migration_module() -> ModuleType:
    return importlib.import_module("app.db.migrations.versions.0009_f7_anomaly_investigation")


def _migration_config(database_url: str) -> Config:
    config = Config("alembic.ini")
    config.set_main_option("script_location", "app/db/migrations")
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def _evidence_catalog_snapshot(conn: Connection) -> dict[str, list[tuple[object, ...]]]:
    return {
        "columns": [
            tuple(row)
            for row in conn.execute(
                text(
                    "SELECT column_name, data_type, udt_name, is_nullable, column_default "
                    "FROM information_schema.columns WHERE table_schema='public' "
                    "AND table_name='evidence_step' ORDER BY column_name"
                )
            )
        ],
        "constraints": [
            tuple(row)
            for row in conn.execute(
                text(
                    "SELECT conname, contype, pg_get_constraintdef(oid) FROM pg_constraint "
                    "WHERE conrelid='evidence_step'::regclass ORDER BY conname"
                )
            )
        ],
        "indexes": [
            tuple(row)
            for row in conn.execute(
                text(
                    "SELECT indexname,indexdef FROM pg_indexes WHERE schemaname='public' "
                    "AND tablename='evidence_step' ORDER BY indexname"
                )
            )
        ],
    }


def test_0009_upgrade_calls_preflight_before_any_ddl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = _migration_module()
    calls: list[str] = []

    def reject_legacy() -> None:
        calls.append("preflight")
        raise RuntimeError("legacy")

    def record_ddl() -> None:
        calls.append("ddl")

    monkeypatch.setattr(migration, "_preflight_empty_evidence_step", reject_legacy)
    for helper_name in (
        "_create_investigation_run",
        "_create_investigation_request",
        "_enhance_evidence_step",
        "_create_investigation_result",
        "_create_pii_token",
        "_create_immutability_triggers",
    ):
        monkeypatch.setattr(migration, helper_name, record_ddl)

    with pytest.raises(RuntimeError, match="legacy"):
        migration.upgrade()
    assert calls == ["preflight"]


def test_0009_downgrade_guard_queries_all_f7_fact_tables(
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
    with pytest.raises(RuntimeError, match="cannot downgrade 0009"):
        migration._guard_downgrade()
    for table_name in (
        "investigation_run",
        "investigation_request",
        "evidence_step",
        "investigation_result",
        "pii_token",
    ):
        assert f"FROM {table_name}" in captured_sql


async def _expect_integrity_error(
    conn: AsyncConnection, statement: str, params: dict[str, object]
) -> None:
    with pytest.raises(IntegrityError):
        async with conn.begin_nested():
            await conn.execute(text(statement), params)


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
            "detection_run",
            "finding",
            "investigation_run",
            "request",
            "step",
            "result",
            "token",
        )
    }
    await conn.execute(
        text(
            "INSERT INTO tenant (id, slug, name) VALUES "
            "(:tenant, :slug, 'F7'), (:other_tenant, :other_slug, 'Other')"
        ),
        {
            **ids,
            "slug": f"f7-{ids['tenant'].hex[:8]}",
            "other_slug": f"f7-other-{ids['other_tenant'].hex[:8]}",
        },
    )
    await conn.execute(
        text(
            "INSERT INTO app_user "
            "(id, tenant_id, username, password_hash, role, is_active) VALUES "
            "(:user, :tenant, 'f7-user', 'test', 'auditor', true), "
            "(:other_user, :other_tenant, 'other-user', 'test', 'auditor', true)"
        ),
        ids,
    )
    await conn.execute(
        text(
            "INSERT INTO file_version "
            "(id, tenant_id, filename, content_hash, row_count, uploaded_by, "
            "parse_status, parsed_at, revision_no) VALUES "
            "(:file, :tenant, 'f7.xlsx', :content_hash, 2, :user, 'parsed', now(), 1)"
        ),
        {**ids, "content_hash": HASH_A},
    )
    await conn.execute(
        text(
            "INSERT INTO detection_config "
            "(id, tenant_id, version, definition, definition_canonical, schema_version, "
            "algorithm_bundle_version, config_fingerprint, created_by, change_reason, "
            "idempotency_key_hash, request_fingerprint) VALUES "
            "(:config, :tenant, 1, '{}'::jsonb, '{}', 1, 'correlation-v1', :config_hash, "
            ":user, 'f7 fixture', :key_hash, :request_hash)"
        ),
        {**ids, "config_hash": HASH_A, "key_hash": HASH_B, "request_hash": HASH_C},
    )
    await conn.execute(
        text(
            "INSERT INTO detection_run "
            "(id, tenant_id, file_version_id, detection_config_id, config_version, "
            "config_fingerprint, algorithm_bundle_version, input_fingerprint, "
            "run_fingerprint, source_row_count, parsed_row_count, error_row_count, "
            "finding_count, created_by, completed_at) VALUES "
            "(:detection_run, :tenant, :file, :config, 1, :config_hash, 'correlation-v1', "
            ":input_hash, :run_hash, 2, 2, 0, 1, :user, now())"
        ),
        {**ids, "config_hash": HASH_A, "input_hash": HASH_D, "run_hash": HASH_E},
    )
    await conn.execute(
        text(
            "INSERT INTO correlation_finding "
            "(id, tenant_id, file_version_id, detection_run_id, detector, detector_version, "
            "finding_key, evidence_schema_version, evidence_json, reasoning_snapshot, "
            "severity_impact, severity_confidence) VALUES "
            "(:finding, :tenant, :file, :detection_run, 'split_invoice', "
            "'split-window-v1', :finding_key, 1, '{}'::jsonb, 'candidate', 0, 0)"
        ),
        {**ids, "finding_key": HASH_F},
    )
    return ids


async def _insert_run(conn: AsyncConnection, ids: dict[str, uuid.UUID]) -> None:
    await conn.execute(
        text(
            "INSERT INTO investigation_run "
            "(id, tenant_id, correlation_finding_id, detection_run_id, file_version_id, "
            "actor_id, agent_version, action_schema_version, prompt_template_version, "
            "provider_kind, provider_model, max_steps, timeout_seconds, redaction_version, "
            "input_fingerprint, config_fingerprint) VALUES "
            "(:investigation_run, :tenant, :finding, :detection_run, :file, :user, "
            "'react-v1', 1, 'prompt-v1', 'disabled', 'unconfigured', 6, 30, 'v1', "
            ":input_hash, :config_hash)"
        ),
        {**ids, "input_hash": HASH_A, "config_hash": HASH_B},
    )


async def test_f7_schema_constraints_fks_and_triggers_exist(engine: AsyncEngine) -> None:
    expected_tables = {
        "investigation_run",
        "investigation_request",
        "investigation_result",
        "pii_token",
    }
    expected_constraints = {
        "uq_investigation_run_identity",
        "fk_investigation_run_correlation_identity",
        "fk_investigation_run_actor_tenant",
        "uq_investigation_request_tenant_idempotency_key",
        "fk_investigation_request_run_identity",
        "uq_evidence_step_run_step_no",
        "fk_evidence_step_run_identity",
        "uq_investigation_result_run_id",
        "fk_investigation_result_run_identity",
        "uq_pii_token_source_identity",
        "uq_pii_token_tenant_token",
        "ck_investigation_result_outcome_sufficiency_consistent",
        "ck_evidence_step_action_tool_consistent",
    }
    fact_tables = {
        "investigation_run",
        "investigation_request",
        "evidence_step",
        "investigation_result",
        "pii_token",
    }
    async with engine.connect() as conn:
        tables = set(
            (
                await conn.execute(
                    text(
                        "SELECT tablename FROM pg_tables "
                        "WHERE schemaname = 'public' AND tablename = ANY(:names)"
                    ),
                    {"names": list(expected_tables)},
                )
            ).scalars()
        )
        constraints = set(
            (
                await conn.execute(
                    text("SELECT conname FROM pg_constraint WHERE conname = ANY(:names)"),
                    {"names": list(expected_constraints)},
                )
            ).scalars()
        )
        trigger_tables = set(
            (
                await conn.execute(
                    text(
                        "SELECT c.relname FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid "
                        "WHERE NOT t.tgisinternal AND t.tgname LIKE 'trg_%_immutable' "
                        "AND c.relname = ANY(:names)"
                    ),
                    {"names": list(fact_tables)},
                )
            ).scalars()
        )
        f7_fks = (
            await conn.execute(
                text(
                    "SELECT conname, confdeltype FROM pg_constraint "
                    "WHERE contype='f' AND conrelid::regclass::text = ANY(:tables)"
                ),
                {"tables": list(fact_tables)},
            )
        ).all()
        evidence_columns = set(
            (
                await conn.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_schema='public' AND table_name='evidence_step'"
                    )
                )
            ).scalars()
        )

    assert tables == expected_tables
    assert constraints == expected_constraints
    assert trigger_tables == fact_tables
    assert f7_fks and all(delete_type == "r" for _, delete_type in f7_fks)
    assert "finding_id" not in evidence_columns
    assert {
        "investigation_run_id",
        "correlation_finding_id",
        "detection_run_id",
        "file_version_id",
        "action_kind",
        "model_action",
    } <= evidence_columns


async def test_f7_composite_identity_checks_and_all_facts_are_immutable(
    engine: AsyncEngine, clean_db: None
) -> None:
    del clean_db
    async with engine.begin() as conn:
        ids = await _seed_candidate(conn)
        await _insert_run(conn, ids)
        await conn.execute(
            text(
                "INSERT INTO investigation_request "
                "(id, tenant_id, investigation_run_id, correlation_finding_id, "
                "detection_run_id, file_version_id, idempotency_key_hash, request_fingerprint) "
                "VALUES (:request, :tenant, :investigation_run, :finding, :detection_run, "
                ":file, :key_hash, :request_hash)"
            ),
            {**ids, "key_hash": HASH_C, "request_hash": HASH_D},
        )
        await conn.execute(
            text(
                "INSERT INTO evidence_step "
                "(id, tenant_id, investigation_run_id, correlation_finding_id, "
                "detection_run_id, file_version_id, step_no, action_kind, "
                "action_schema_version, model_action, decision_summary, payload_fingerprint, "
                "tool_name, tool_input, tool_output) VALUES "
                "(:step, :tenant, :investigation_run, :finding, :detection_run, :file, 1, "
                "'tool_call', 1, '{\"action\":\"tool_call\"}'::jsonb, 'read rows', "
                ":payload_hash, 'get_correlation_rows', '{}'::jsonb, '{}'::jsonb)"
            ),
            {**ids, "payload_hash": HASH_E},
        )
        await conn.execute(
            text(
                "INSERT INTO investigation_result "
                "(id, tenant_id, investigation_run_id, correlation_finding_id, "
                "detection_run_id, file_version_id, outcome, evidence_sufficient, summary, "
                "reason_code, citations_json, result_fingerprint, completed_at) VALUES "
                "(:result, :tenant, :investigation_run, :finding, :detection_run, :file, "
                "'insufficient', false, 'needs review', 'EVIDENCE_INSUFFICIENT', '[]'::jsonb, "
                ":result_hash, now())"
            ),
            {**ids, "result_hash": HASH_F},
        )
        await conn.execute(
            text(
                "INSERT INTO pii_token "
                "(id, tenant_id, token_version, pii_kind, source_hmac, token) VALUES "
                "(:token, :tenant, 'v1', 'employee_id', :source_hmac, "
                "'EMPLOYEE_ID_v1_aaaaaaaaaaaaaaaa')"
            ),
            {**ids, "source_hmac": HASH_A},
        )

        await _expect_integrity_error(
            conn,
            "INSERT INTO evidence_step "
            "(id, tenant_id, investigation_run_id, correlation_finding_id, detection_run_id, "
            "file_version_id, step_no, action_kind, action_schema_version, model_action, "
            "decision_summary, payload_fingerprint) VALUES "
            "(:id, :tenant, :investigation_run, :finding, :detection_run, :file, 1, "
            "'terminate', 1, '{}'::jsonb, 'done', :hash)",
            {**ids, "id": uuid.uuid4(), "hash": HASH_A},
        )
        await _expect_integrity_error(
            conn,
            "INSERT INTO investigation_result "
            "(id, tenant_id, investigation_run_id, correlation_finding_id, detection_run_id, "
            "file_version_id, outcome, evidence_sufficient, summary, reason_code, "
            "citations_json, result_fingerprint, completed_at) VALUES "
            "(:id, :tenant, :investigation_run, :finding, :detection_run, :file, "
            "'unavailable', false, 'bad', 'BAD', '[]'::jsonb, :hash, now())",
            {**ids, "id": uuid.uuid4(), "hash": HASH_A},
        )
        await _expect_integrity_error(
            conn,
            "INSERT INTO investigation_run "
            "(id, tenant_id, correlation_finding_id, detection_run_id, file_version_id, "
            "actor_id, agent_version, action_schema_version, prompt_template_version, "
            "provider_kind, provider_model, max_steps, timeout_seconds, redaction_version, "
            "input_fingerprint, config_fingerprint) VALUES "
            "(:id, :other_tenant, :finding, :detection_run, :file, :other_user, 'react-v1', "
            "1, 'prompt-v1', 'disabled', 'unconfigured', 6, 30, 'v1', :hash_a, :hash_b)",
            {**ids, "id": uuid.uuid4(), "hash_a": HASH_A, "hash_b": HASH_B},
        )

        fact_statements = (
            (
                ids["investigation_run"],
                "UPDATE investigation_run SET created_at=created_at WHERE id=:id",
                "DELETE FROM investigation_run WHERE id=:id",
            ),
            (
                ids["request"],
                "UPDATE investigation_request SET created_at=created_at WHERE id=:id",
                "DELETE FROM investigation_request WHERE id=:id",
            ),
            (
                ids["step"],
                "UPDATE evidence_step SET created_at=created_at WHERE id=:id",
                "DELETE FROM evidence_step WHERE id=:id",
            ),
            (
                ids["result"],
                "UPDATE investigation_result SET created_at=created_at WHERE id=:id",
                "DELETE FROM investigation_result WHERE id=:id",
            ),
            (
                ids["token"],
                "UPDATE pii_token SET created_at=created_at WHERE id=:id",
                "DELETE FROM pii_token WHERE id=:id",
            ),
        )
        for fact_id, update_sql, delete_sql in fact_statements:
            with pytest.raises(DBAPIError, match="immutable"):
                async with conn.begin_nested():
                    await conn.execute(text(update_sql), {"id": fact_id})
            with pytest.raises(DBAPIError, match="immutable"):
                async with conn.begin_nested():
                    await conn.execute(text(delete_sql), {"id": fact_id})


def test_0009_real_preflight_round_trip_and_safe_downgrade(db_url: str) -> None:
    database_name = f"expenseguard_f71_{uuid.uuid4().hex[:12]}"
    source_url = make_url(db_url)
    admin_url = source_url.set(database="postgres")
    temporary_url = source_url.set(database=database_name)
    admin_engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    temporary_engine = None
    with admin_engine.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{database_name}"'))
    try:
        cfg = _migration_config(temporary_url.render_as_string(hide_password=False))
        command.upgrade(cfg, "0008")
        temporary_engine = create_engine(temporary_url)
        ids = {
            name: uuid.uuid4()
            for name in ("tenant", "user", "file", "finding", "legacy_step", "token")
        }
        with temporary_engine.begin() as conn:
            conn.execute(
                text("INSERT INTO tenant (id,slug,name) VALUES (:tenant,:slug,'Legacy F7')"),
                {**ids, "slug": f"f7-legacy-{ids['tenant'].hex[:8]}"},
            )
            conn.execute(
                text(
                    "INSERT INTO app_user "
                    "(id,tenant_id,username,password_hash,role,is_active) VALUES "
                    "(:user,:tenant,'legacy-f7','test','auditor',true)"
                ),
                ids,
            )
            conn.execute(
                text(
                    "INSERT INTO file_version "
                    "(id,tenant_id,filename,content_hash,uploaded_by,revision_no,parse_status) "
                    "VALUES (:file,:tenant,'legacy-f7.xlsx',:hash,:user,1,'unparsed')"
                ),
                {**ids, "hash": HASH_A},
            )
            conn.execute(
                text(
                    "INSERT INTO finding "
                    "(id,tenant_id,file_version_id,kind,severity_impact,severity_confidence) "
                    "VALUES (:finding,:tenant,:file,'legacy',0,0)"
                ),
                ids,
            )
            legacy_snapshot = _evidence_catalog_snapshot(conn)
            conn.execute(
                text(
                    "INSERT INTO evidence_step "
                    "(id,tenant_id,finding_id,step_no,tool_name,tool_input,tool_output) "
                    "VALUES (:legacy_step,:tenant,:finding,1,'legacy','{}'::jsonb,'{}'::jsonb)"
                ),
                ids,
            )

        with pytest.raises(RuntimeError, match="legacy evidence_step"):
            command.upgrade(cfg, "0009")
        with temporary_engine.connect() as conn:
            assert conn.scalar(text("SELECT version_num FROM alembic_version")) == "0008"
            assert conn.scalar(text("SELECT to_regclass('investigation_run')")) is None
            assert conn.scalar(text("SELECT count(*) FROM evidence_step")) == 1
            assert _evidence_catalog_snapshot(conn) == legacy_snapshot

        with temporary_engine.begin() as conn:
            conn.execute(text("TRUNCATE evidence_step"))
        command.upgrade(cfg, "0009")
        command.downgrade(cfg, "0008")
        with temporary_engine.connect() as conn:
            assert conn.scalar(text("SELECT version_num FROM alembic_version")) == "0008"
            assert _evidence_catalog_snapshot(conn) == legacy_snapshot
            assert conn.scalar(text("SELECT to_regprocedure('f7_reject_update_delete()') IS NULL"))
            assert (
                conn.scalar(
                    text(
                        "SELECT count(*) FROM (VALUES "
                        "(to_regclass('investigation_run')),"
                        "(to_regclass('investigation_request')),"
                        "(to_regclass('investigation_result')),"
                        "(to_regclass('pii_token'))) AS absent(value) "
                        "WHERE value IS NOT NULL"
                    )
                )
                == 0
            )

        command.upgrade(cfg, "0009")
        with temporary_engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO pii_token "
                    "(id,tenant_id,token_version,pii_kind,source_hmac,token) VALUES "
                    "(:token,:tenant,'v1','employee_id',:hash,"
                    "'EMPLOYEE_ID_v1_bbbbbbbbbbbbbbbb')"
                ),
                {**ids, "hash": HASH_B},
            )
        with pytest.raises(RuntimeError, match="cannot downgrade 0009"):
            command.downgrade(cfg, "0008")
        with temporary_engine.connect() as conn:
            assert conn.scalar(text("SELECT version_num FROM alembic_version")) == "0009"
            assert conn.scalar(text("SELECT count(*) FROM pii_token")) == 1
    finally:
        if temporary_engine is not None:
            temporary_engine.dispose()
        with admin_engine.connect() as conn:
            conn.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname=:database_name AND pid<>pg_backend_pid()"
                ),
                {"database_name": database_name},
            )
            conn.execute(text(f'DROP DATABASE IF EXISTS "{database_name}"'))
        admin_engine.dispose()
