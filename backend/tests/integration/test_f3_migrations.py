"""CP-F3.1 migration contract tests against the PostgreSQL catalog and data."""

import uuid
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

pytestmark = pytest.mark.integration

BACKEND_DIR = Path(__file__).resolve().parents[2]


async def _seed_tenant(conn: AsyncConnection, label: str) -> tuple[uuid.UUID, uuid.UUID]:
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    await conn.execute(
        text("INSERT INTO tenant (id, slug, name) VALUES (:id, :slug, :name)"),
        {"id": tenant_id, "slug": f"f3-{label}-{tenant_id.hex[:8]}", "name": label},
    )
    await conn.execute(
        text(
            "INSERT INTO app_user "
            "(id, tenant_id, username, password_hash, role, is_active) "
            "VALUES (:id, :tenant_id, :username, 'test', 'auditor', true)"
        ),
        {"id": user_id, "tenant_id": tenant_id, "username": f"user-{label}"},
    )
    return tenant_id, user_id


async def _seed_mapping(
    conn: AsyncConnection, tenant_id: uuid.UUID, user_id: uuid.UUID, version: int = 1
) -> uuid.UUID:
    mapping_id = uuid.uuid4()
    await conn.execute(
        text(
            "INSERT INTO schema_mapping_version "
            "(id, tenant_id, header_signature, version, config_fingerprint, "
            "availability_thresholds, currency_aliases, inference_config, "
            "backfilled_legacy, created_by) VALUES "
            "(:id, :tenant_id, :signature, :version, :fingerprint, "
            "CAST('{}' AS jsonb), CAST('{}' AS jsonb), CAST('{}' AS jsonb), false, :user_id)"
        ),
        {
            "id": mapping_id,
            "tenant_id": tenant_id,
            "signature": f"{version:064x}",
            "version": version,
            "fingerprint": f"{version + 100:064x}",
            "user_id": user_id,
        },
    )
    return mapping_id


async def _seed_file(
    conn: AsyncConnection,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    content_hash: str,
) -> uuid.UUID:
    file_id = uuid.uuid4()
    await conn.execute(
        text(
            "INSERT INTO file_version "
            "(id, tenant_id, filename, content_hash, uploaded_by) "
            "VALUES (:id, :tenant_id, 'fixture.xlsx', :content_hash, :user_id)"
        ),
        {
            "id": file_id,
            "tenant_id": tenant_id,
            "content_hash": content_hash,
            "user_id": user_id,
        },
    )
    return file_id


async def _expect_integrity_error(
    conn: AsyncConnection, statement: str, params: dict[str, Any]
) -> None:
    with pytest.raises(IntegrityError):
        async with conn.begin_nested():
            await conn.execute(text(statement), params)


async def test_f3_tables_and_columns_exist(engine: AsyncEngine) -> None:
    expected = {
        "validation_run": {
            "id",
            "tenant_id",
            "file_version_id",
            "mapping_version_id",
            "ruleset_fingerprint",
            "ruleset_manifest",
            "status",
            "total_row_count",
            "evaluated_row_count",
            "passed_count",
            "flagged_count",
            "manual_review_count",
            "parse_failed_count",
            "completed_at",
            "triggered_by",
            "created_at",
        },
        "validation_dependency": {
            "id",
            "tenant_id",
            "validation_run_id",
            "depended_file_version_id",
            "created_at",
        },
        "file_version": {
            "revision_no",
            "source_file_version_id",
            "root_file_version_id",
            "revision_reason",
            "revision_request_key_hash",
            "revision_request_fingerprint",
        },
        "rule_config": {
            "config_fingerprint",
            "created_by",
            "backfilled_legacy",
        },
        "finding": {
            "validation_run_id",
            "rule_kind",
            "rule_config_id",
            "evidence_json",
        },
    }
    async with engine.connect() as conn:
        for table, required in expected.items():
            rows = await conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND table_name = :table"
                ),
                {"table": table},
            )
            actual = {row[0] for row in rows}
            assert required <= actual, f"{table} missing columns: {sorted(required - actual)}"


async def test_f3_constraints_and_composite_foreign_keys_exist(engine: AsyncEngine) -> None:
    expected = {
        "uq_app_user_id_tenant_id": "u",
        "uq_file_version_tenant_id_content_hash_revision_no": "u",
        "ck_file_version_revision_no_positive": "c",
        "ck_file_version_revision_reason_values": "c",
        "ck_file_version_revision_lineage": "c",
        "fk_file_version_source_file_version_tenant": "f",
        "fk_file_version_root_file_version_tenant": "f",
        "uq_rule_config_id_tenant_id": "u",
        "fk_rule_config_created_by_tenant": "f",
        "uq_validation_run_file_version_id": "u",
        "uq_validation_run_id_tenant_id": "u",
        "fk_validation_run_file_version_tenant": "f",
        "fk_validation_run_mapping_version_tenant": "f",
        "fk_validation_run_triggered_by_tenant": "f",
        "ck_validation_run_counts_nonnegative": "c",
        "ck_validation_run_total_count_consistent": "c",
        "ck_validation_run_evaluated_count_consistent": "c",
        "ck_validation_run_completion_consistent": "c",
        "uq_validation_dependency_run_file": "u",
        "fk_validation_dependency_validation_run_tenant": "f",
        "fk_validation_dependency_file_version_tenant": "f",
        "fk_finding_validation_run_tenant": "f",
        "fk_finding_rule_config_tenant": "f",
        "ck_finding_rule_kind_values": "c",
    }
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT conname, contype FROM pg_constraint c "
                "JOIN pg_namespace n ON n.oid = c.connamespace "
                "WHERE n.nspname = 'public' AND conname = ANY(:names)"
            ),
            {"names": list(expected)},
        )
        actual = {row[0]: row[1] for row in rows}
    assert actual == expected


async def test_f3_partial_unique_indexes_exist(engine: AsyncEngine) -> None:
    expected_predicates = {
        "uq_file_version_revision_one_content_hash": "revision_no = 1",
        "uq_file_version_source_request_key": "revision_request_key_hash IS NOT NULL",
        "uq_finding_deterministic_rule": "validation_run_id IS NOT NULL",
    }
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT indexname, indexdef FROM pg_indexes "
                "WHERE schemaname = 'public' AND indexname = ANY(:names)"
            ),
            {"names": list(expected_predicates)},
        )
        actual = {row[0]: row[1] for row in rows}
    assert set(actual) == set(expected_predicates)
    for name, predicate in expected_predicates.items():
        assert "CREATE UNIQUE INDEX" in actual[name]
        assert predicate in actual[name]


async def test_rule_config_legacy_storage_contract_and_new_default(
    engine: AsyncEngine, clean_db: None
) -> None:
    del clean_db
    async with engine.begin() as conn:
        tenant_id, _ = await _seed_tenant(conn, "legacy")
        rule_id = uuid.uuid4()
        await conn.execute(
            text(
                "INSERT INTO rule_config "
                "(id, tenant_id, rule_id, definition, version, effective_from, is_active) "
                "VALUES (:id, :tenant_id, 'legacy.rule', CAST('{}' AS jsonb), 1, NULL, true)"
            ),
            {"id": rule_id, "tenant_id": tenant_id},
        )
        row = (
            await conn.execute(
                text(
                    "SELECT config_fingerprint, created_by, backfilled_legacy "
                    "FROM rule_config WHERE id = :id"
                ),
                {"id": rule_id},
            )
        ).one()
        nullable = await conn.execute(
            text(
                "SELECT column_name, is_nullable, column_default "
                "FROM information_schema.columns WHERE table_name = 'rule_config' "
                "AND column_name IN "
                "('config_fingerprint', 'created_by', 'backfilled_legacy')"
            )
        )
        metadata = {item[0]: (item[1], item[2]) for item in nullable}

    assert row == (None, None, False)
    assert metadata["config_fingerprint"][0] == "YES"
    assert metadata["created_by"][0] == "YES"
    assert metadata["backfilled_legacy"][0] == "NO"
    assert metadata["backfilled_legacy"][1] == "false"


async def test_file_revision_uniqueness_and_tenant_foreign_keys(
    engine: AsyncEngine, clean_db: None
) -> None:
    del clean_db
    derived_insert = (
        "INSERT INTO file_version "
        "(id, tenant_id, filename, content_hash, uploaded_by, revision_no, "
        "source_file_version_id, root_file_version_id, revision_reason, "
        "revision_request_key_hash, revision_request_fingerprint) VALUES "
        "(:id, :tenant_id, 'derived.xlsx', :content_hash, :user_id, :revision_no, "
        ":source_id, :root_id, 'ruleset_change', :key_hash, :fingerprint)"
    )
    async with engine.begin() as conn:
        tenant_a, user_a = await _seed_tenant(conn, "revision-a")
        tenant_b, user_b = await _seed_tenant(conn, "revision-b")
        root_a = await _seed_file(conn, tenant_a, user_a, "a" * 64)
        root_b = await _seed_file(conn, tenant_b, user_b, "b" * 64)

        await _expect_integrity_error(
            conn,
            "INSERT INTO file_version "
            "(id, tenant_id, filename, content_hash, uploaded_by) "
            "VALUES (:id, :tenant_id, 'duplicate.xlsx', :hash, :user_id)",
            {"id": uuid.uuid4(), "tenant_id": tenant_a, "hash": "a" * 64, "user_id": user_a},
        )

        derived_a = uuid.uuid4()
        base_params = {
            "tenant_id": tenant_a,
            "content_hash": "a" * 64,
            "user_id": user_a,
            "source_id": root_a,
            "root_id": root_a,
            "key_hash": "k" * 64,
            "fingerprint": "f" * 64,
        }
        await conn.execute(
            text(derived_insert),
            {**base_params, "id": derived_a, "revision_no": 2},
        )
        await _expect_integrity_error(
            conn,
            derived_insert,
            {**base_params, "id": uuid.uuid4(), "revision_no": 2, "key_hash": "x" * 64},
        )
        await _expect_integrity_error(
            conn,
            derived_insert,
            {**base_params, "id": uuid.uuid4(), "revision_no": 3},
        )
        await _expect_integrity_error(
            conn,
            derived_insert,
            {
                **base_params,
                "id": uuid.uuid4(),
                "tenant_id": tenant_b,
                "content_hash": "c" * 64,
                "user_id": user_b,
                "root_id": root_b,
                "revision_no": 2,
            },
        )
        await _expect_integrity_error(
            conn,
            derived_insert,
            {
                **base_params,
                "id": uuid.uuid4(),
                "tenant_id": tenant_b,
                "content_hash": "d" * 64,
                "user_id": user_b,
                "source_id": root_b,
                "revision_no": 2,
            },
        )


async def test_validation_dependency_and_finding_database_guards(
    engine: AsyncEngine, clean_db: None
) -> None:
    del clean_db
    validation_insert = (
        "INSERT INTO validation_run "
        "(id, tenant_id, file_version_id, mapping_version_id, ruleset_fingerprint, "
        "ruleset_manifest, status, total_row_count, evaluated_row_count, passed_count, "
        "flagged_count, manual_review_count, parse_failed_count, completed_at, triggered_by) "
        "VALUES (:id, :tenant_id, :file_id, :mapping_id, :fingerprint, CAST('{}' AS jsonb), "
        "'completed', 1, 1, 0, 1, 0, 0, now(), :user_id)"
    )
    finding_insert = (
        "INSERT INTO finding "
        "(id, tenant_id, file_version_id, row_no, kind, severity_impact, "
        "severity_confidence, rule_id, rule_version, validation_run_id, rule_kind, "
        "rule_config_id, evidence_json) VALUES "
        "(:id, :tenant_id, :file_id, 1, 'limit_exceeded', 0, 0, 'expense.limit', "
        "'1', :run_id, 'limit', :rule_id, CAST('{}' AS jsonb))"
    )
    async with engine.begin() as conn:
        tenant_a, user_a = await _seed_tenant(conn, "validation-a")
        tenant_b, user_b = await _seed_tenant(conn, "validation-b")
        mapping_a = await _seed_mapping(conn, tenant_a, user_a)
        mapping_b = await _seed_mapping(conn, tenant_b, user_b)
        file_a = await _seed_file(conn, tenant_a, user_a, "1" * 64)
        file_b = await _seed_file(conn, tenant_b, user_b, "2" * 64)
        run_a, run_b = uuid.uuid4(), uuid.uuid4()
        await conn.execute(
            text(validation_insert),
            {
                "id": run_a,
                "tenant_id": tenant_a,
                "file_id": file_a,
                "mapping_id": mapping_a,
                "fingerprint": "3" * 64,
                "user_id": user_a,
            },
        )
        await conn.execute(
            text(validation_insert),
            {
                "id": run_b,
                "tenant_id": tenant_b,
                "file_id": file_b,
                "mapping_id": mapping_b,
                "fingerprint": "4" * 64,
                "user_id": user_b,
            },
        )
        await _expect_integrity_error(
            conn,
            validation_insert,
            {
                "id": uuid.uuid4(),
                "tenant_id": tenant_b,
                "file_id": file_a,
                "mapping_id": mapping_b,
                "fingerprint": "5" * 64,
                "user_id": user_b,
            },
        )
        file_a_second = await _seed_file(conn, tenant_a, user_a, "7" * 64)
        await _expect_integrity_error(
            conn,
            validation_insert,
            {
                "id": uuid.uuid4(),
                "tenant_id": tenant_a,
                "file_id": file_a_second,
                "mapping_id": mapping_a,
                "fingerprint": "8" * 64,
                "user_id": user_b,
            },
        )
        await _expect_integrity_error(
            conn,
            validation_insert,
            {
                "id": uuid.uuid4(),
                "tenant_id": tenant_b,
                "file_id": file_b,
                "mapping_id": mapping_a,
                "fingerprint": "6" * 64,
                "user_id": user_b,
            },
        )

        dependency_id = uuid.uuid4()
        dependency_insert = (
            "INSERT INTO validation_dependency "
            "(id, tenant_id, validation_run_id, depended_file_version_id) "
            "VALUES (:id, :tenant_id, :run_id, :file_id)"
        )
        dependency_params = {
            "id": dependency_id,
            "tenant_id": tenant_a,
            "run_id": run_a,
            "file_id": file_b,
        }
        await _expect_integrity_error(conn, dependency_insert, dependency_params)
        dependency_params["file_id"] = file_a
        await conn.execute(text(dependency_insert), dependency_params)
        await _expect_integrity_error(
            conn, dependency_insert, {**dependency_params, "id": uuid.uuid4()}
        )

        rule_a, rule_b = uuid.uuid4(), uuid.uuid4()
        for rule_id, tenant_id, user_id in (
            (rule_a, tenant_a, user_a),
            (rule_b, tenant_b, user_b),
        ):
            await conn.execute(
                text(
                    "INSERT INTO rule_config "
                    "(id, tenant_id, rule_id, definition, version, effective_from, "
                    "is_active, config_fingerprint, created_by, backfilled_legacy) "
                    "VALUES (:id, :tenant_id, 'expense.limit', CAST('{}' AS jsonb), 1, "
                    "CURRENT_DATE, true, :fingerprint, :user_id, false)"
                ),
                {
                    "id": rule_id,
                    "tenant_id": tenant_id,
                    "fingerprint": rule_id.hex.ljust(64, "0"),
                    "user_id": user_id,
                },
            )
        await _expect_integrity_error(
            conn,
            "INSERT INTO rule_config "
            "(id, tenant_id, rule_id, definition, version, effective_from, "
            "is_active, config_fingerprint, created_by, backfilled_legacy) "
            "VALUES (:id, :tenant_id, 'cross.tenant', CAST('{}' AS jsonb), 1, "
            "CURRENT_DATE, true, :fingerprint, :user_id, false)",
            {
                "id": uuid.uuid4(),
                "tenant_id": tenant_a,
                "fingerprint": "9" * 64,
                "user_id": user_b,
            },
        )

        finding_params = {
            "id": uuid.uuid4(),
            "tenant_id": tenant_a,
            "file_id": file_a,
            "run_id": run_a,
            "rule_id": rule_a,
        }
        await conn.execute(text(finding_insert), finding_params)
        await _expect_integrity_error(conn, finding_insert, {**finding_params, "id": uuid.uuid4()})
        await _expect_integrity_error(
            conn,
            finding_insert,
            {
                **finding_params,
                "id": uuid.uuid4(),
                "tenant_id": tenant_b,
                "file_id": file_b,
                "run_id": run_b,
            },
        )
        await _expect_integrity_error(
            conn,
            finding_insert,
            {
                **finding_params,
                "id": uuid.uuid4(),
                "tenant_id": tenant_b,
                "file_id": file_b,
                "rule_id": rule_b,
            },
        )


async def test_protected_constraints_and_audit_trigger_remain(engine: AsyncEngine) -> None:
    protected = {
        "uq_expense_row_file_version_id_row_no",
        "uq_row_result_file_version_id_row_no",
        "uq_sampling_audit_file_version_id_row_no",
    }
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT conname FROM pg_constraint c "
                "JOIN pg_namespace n ON n.oid = c.connamespace "
                "WHERE n.nspname = 'public' AND c.contype = 'u' "
                "AND conname = ANY(:names)"
            ),
            {"names": list(protected)},
        )
        actual = {row[0] for row in rows}
        trigger_enabled = await conn.scalar(
            text(
                "SELECT tgenabled <> 'D' FROM pg_trigger "
                "WHERE tgname = 'trg_audit_log_append_only' "
                "AND tgrelid = 'audit_log'::regclass AND NOT tgisinternal"
            )
        )
    assert actual == protected
    assert trigger_enabled is True


def test_legacy_upgrade_round_trip_and_derived_downgrade_guard(db_url: str) -> None:
    """Legacy data survives 0004; derived revisions make downgrade fail closed."""
    database_name = f"expenseguard_f31_{uuid.uuid4().hex[:12]}"
    source_url = make_url(db_url)
    admin_url = source_url.set(database="postgres")
    temporary_url = source_url.set(database=database_name)
    admin_engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    temporary_engine = None

    with admin_engine.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{database_name}"'))

    try:
        cfg = Config(str(BACKEND_DIR / "alembic.ini"))
        cfg.set_main_option("script_location", str(BACKEND_DIR / "app" / "db" / "migrations"))
        cfg.set_main_option("sqlalchemy.url", temporary_url.render_as_string(hide_password=False))
        command.upgrade(cfg, "0003")

        temporary_engine = create_engine(temporary_url)
        tenant_id, user_id, file_id, rule_id, finding_id = (uuid.uuid4() for _ in range(5))
        with temporary_engine.begin() as conn:
            conn.execute(
                text("INSERT INTO tenant (id, slug, name) VALUES (:id, 'legacy', 'Legacy')"),
                {"id": tenant_id},
            )
            conn.execute(
                text(
                    "INSERT INTO app_user "
                    "(id, tenant_id, username, password_hash, role, is_active) "
                    "VALUES (:id, :tenant_id, 'legacy', 'test', 'auditor', true)"
                ),
                {"id": user_id, "tenant_id": tenant_id},
            )
            conn.execute(
                text(
                    "INSERT INTO file_version "
                    "(id, tenant_id, filename, content_hash, uploaded_by) "
                    "VALUES (:id, :tenant_id, 'legacy.xlsx', :hash, :user_id)"
                ),
                {
                    "id": file_id,
                    "tenant_id": tenant_id,
                    "hash": "a" * 64,
                    "user_id": user_id,
                },
            )
            conn.execute(
                text(
                    "INSERT INTO rule_config "
                    "(id, tenant_id, rule_id, definition, version, is_active) "
                    "VALUES (:id, :tenant_id, 'legacy.rule', CAST('{}' AS jsonb), 1, true)"
                ),
                {"id": rule_id, "tenant_id": tenant_id},
            )
            conn.execute(
                text(
                    "INSERT INTO finding "
                    "(id, tenant_id, file_version_id, row_no, kind, "
                    "severity_impact, severity_confidence) "
                    "VALUES (:id, :tenant_id, :file_id, 1, 'legacy', 0, 0)"
                ),
                {"id": finding_id, "tenant_id": tenant_id, "file_id": file_id},
            )

        command.upgrade(cfg, "0004")
        with temporary_engine.connect() as conn:
            file_row = conn.execute(
                text(
                    "SELECT revision_no, source_file_version_id, root_file_version_id, "
                    "revision_reason, revision_request_key_hash, revision_request_fingerprint "
                    "FROM file_version WHERE id = :id"
                ),
                {"id": file_id},
            ).one()
            rule_row = conn.execute(
                text(
                    "SELECT config_fingerprint, created_by, backfilled_legacy "
                    "FROM rule_config WHERE id = :id"
                ),
                {"id": rule_id},
            ).one()
            finding_row = conn.execute(
                text(
                    "SELECT validation_run_id, rule_kind, rule_config_id, evidence_json "
                    "FROM finding WHERE id = :id"
                ),
                {"id": finding_id},
            ).one()
        assert file_row == (1, None, None, None, None, None)
        assert rule_row == (None, None, True)
        assert finding_row == (None, None, None, None)

        command.downgrade(cfg, "0003")
        command.upgrade(cfg, "0004")
        derived_id = uuid.uuid4()
        with temporary_engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO file_version "
                    "(id, tenant_id, filename, content_hash, uploaded_by, revision_no, "
                    "source_file_version_id, root_file_version_id, revision_reason, "
                    "revision_request_key_hash, revision_request_fingerprint) "
                    "VALUES (:id, :tenant_id, 'derived.xlsx', :hash, :user_id, 2, "
                    ":file_id, :file_id, 'ruleset_change', :key_hash, :fingerprint)"
                ),
                {
                    "id": derived_id,
                    "tenant_id": tenant_id,
                    "hash": "a" * 64,
                    "user_id": user_id,
                    "file_id": file_id,
                    "key_hash": "b" * 64,
                    "fingerprint": "c" * 64,
                },
            )
        with pytest.raises(RuntimeError, match="cannot downgrade 0004"):
            command.downgrade(cfg, "0003")
        with temporary_engine.connect() as conn:
            version = conn.scalar(text("SELECT version_num FROM alembic_version"))
            derived_exists = conn.scalar(
                text("SELECT EXISTS (SELECT 1 FROM file_version WHERE id = :id)"),
                {"id": derived_id},
            )
        assert version == "0004"
        assert derived_exists is True
    finally:
        if temporary_engine is not None:
            temporary_engine.dispose()
        with admin_engine.connect() as conn:
            conn.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :database_name AND pid <> pg_backend_pid()"
                ),
                {"database_name": database_name},
            )
            conn.execute(text(f'DROP DATABASE IF EXISTS "{database_name}"'))
        admin_engine.dispose()
