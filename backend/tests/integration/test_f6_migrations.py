"""CP-F6.1 migration, tenant identity, and immutable-evidence tests."""

import importlib
import uuid
from types import ModuleType

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import column, create_engine, table, text
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
    return importlib.import_module("app.db.migrations.versions.0008_f6_cross_row_detection")


def _legacy_f6_catalog_snapshot(conn: Connection) -> dict[str, list[tuple[object, ...]]]:
    tables = ("capability_declaration", "correlation_finding")
    return {
        "columns": [
            tuple(row)
            for row in conn.execute(
                text(
                    "SELECT table_name, column_name, data_type, udt_name, is_nullable, "
                    "column_default FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND table_name = ANY(:tables) "
                    "ORDER BY table_name, column_name"
                ),
                {"tables": list(tables)},
            )
        ],
        "constraints": [
            tuple(row)
            for row in conn.execute(
                text(
                    "SELECT conrelid::regclass::text, conname, contype, "
                    "pg_get_constraintdef(oid) FROM pg_constraint "
                    "WHERE conrelid::regclass::text = ANY(:tables) ORDER BY 1, 2"
                ),
                {"tables": list(tables)},
            )
        ],
        "indexes": [
            tuple(row)
            for row in conn.execute(
                text(
                    "SELECT tablename, indexname, indexdef FROM pg_indexes "
                    "WHERE schemaname = 'public' AND tablename = ANY(:tables) "
                    "ORDER BY tablename, indexname"
                ),
                {"tables": list(tables)},
            )
        ],
        "triggers": [
            tuple(row)
            for row in conn.execute(
                text(
                    "SELECT c.relname, t.tgname FROM pg_trigger t "
                    "JOIN pg_class c ON c.oid = t.tgrelid "
                    "WHERE NOT t.tgisinternal AND c.relname = ANY(:tables) ORDER BY 1, 2"
                ),
                {"tables": list(tables)},
            )
        ],
    }


def test_0008_upgrade_calls_preflight_before_any_ddl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = _migration_module()
    calls: list[str] = []

    def reject_legacy() -> None:
        calls.append("preflight")
        raise RuntimeError("legacy")

    def record_ddl() -> None:
        calls.append("ddl")

    monkeypatch.setattr(migration, "_preflight_empty_legacy_f6_tables", reject_legacy)
    for helper_name in (
        "_create_detection_config",
        "_create_detection_run",
        "_create_detection_request",
        "_enhance_capability_declaration",
        "_enhance_correlation_finding",
        "_create_correlation_finding_row",
        "_create_immutability_triggers",
    ):
        monkeypatch.setattr(migration, helper_name, record_ddl)

    with pytest.raises(RuntimeError, match="legacy"):
        migration.upgrade()
    assert calls == ["preflight"]


def test_0008_downgrade_guard_queries_all_six_f6_fact_tables(
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
    with pytest.raises(RuntimeError, match="cannot downgrade 0008"):
        migration._guard_downgrade()
    for table_name in (
        "detection_config",
        "detection_run",
        "detection_request",
        "capability_declaration",
        "correlation_finding",
        "correlation_finding_row",
    ):
        assert f"FROM {table_name}" in captured_sql


def _migration_config(database_url: str) -> Config:
    config = Config("alembic.ini")
    config.set_main_option("script_location", "app/db/migrations")
    config.set_main_option("sqlalchemy.url", database_url)
    return config


async def _expect_integrity_error(
    conn: AsyncConnection, statement: str, params: dict[str, object]
) -> None:
    with pytest.raises(IntegrityError):
        async with conn.begin_nested():
            await conn.execute(text(statement), params)


async def _seed_f6_graph(conn: AsyncConnection) -> dict[str, uuid.UUID]:
    ids = {
        name: uuid.uuid4()
        for name in (
            "tenant",
            "other_tenant",
            "user",
            "other_user",
            "file",
            "file_two",
            "other_file",
            "config",
            "run",
            "request",
            "capability",
            "finding",
            "finding_row",
        )
    }
    await conn.execute(
        text(
            "INSERT INTO tenant (id, slug, name) VALUES "
            "(:tenant, :slug, 'F6'), (:other_tenant, :other_slug, 'Other')"
        ),
        {
            **ids,
            "slug": f"f6-{ids['tenant'].hex[:8]}",
            "other_slug": f"other-{ids['other_tenant'].hex[:8]}",
        },
    )
    await conn.execute(
        text(
            "INSERT INTO app_user "
            "(id, tenant_id, username, password_hash, role, is_active) VALUES "
            "(:user, :tenant, 'f6-user', 'test', 'configurator', true), "
            "(:other_user, :other_tenant, 'other-user', 'test', 'configurator', true)"
        ),
        ids,
    )
    await conn.execute(
        text(
            "INSERT INTO file_version "
            "(id, tenant_id, filename, content_hash, row_count, uploaded_by, "
            "parse_status, parsed_at, revision_no) VALUES "
            "(:file, :tenant, 'f6.xlsx', :hash_a, 2, :user, 'parsed', now(), 1), "
            "(:file_two, :tenant, 'f6-two.xlsx', :hash_b, 1, :user, "
            "'parsed', now(), 1), "
            "(:other_file, :other_tenant, 'other.xlsx', :hash_c, 1, :other_user, "
            "'parsed', now(), 1)"
        ),
        {**ids, "hash_a": HASH_A, "hash_b": HASH_B, "hash_c": HASH_C},
    )
    await conn.execute(
        text(
            "INSERT INTO expense_row "
            "(id, tenant_id, file_version_id, row_no, raw_json) VALUES "
            "(:row_one, :tenant, :file, 1, '{}'::jsonb), "
            "(:row_three, :tenant, :file, 3, '{}'::jsonb), "
            "(:row_four, :tenant, :file_two, 4, '{}'::jsonb), "
            "(:other_row, :other_tenant, :other_file, 2, '{}'::jsonb)"
        ),
        {
            **ids,
            "row_one": uuid.uuid4(),
            "row_three": uuid.uuid4(),
            "row_four": uuid.uuid4(),
            "other_row": uuid.uuid4(),
        },
    )
    await conn.execute(
        text(
            "INSERT INTO detection_config "
            "(id, tenant_id, version, definition, definition_canonical, schema_version, "
            "algorithm_bundle_version, config_fingerprint, created_by, change_reason, "
            "idempotency_key_hash, request_fingerprint) VALUES "
            "(:config, :tenant, 1, '{}'::jsonb, '{}', 1, 'correlation-v1', :config_hash, "
            ":user, 'initial profile', :key_hash, :request_hash)"
        ),
        {
            **ids,
            "config_hash": HASH_A,
            "key_hash": HASH_B,
            "request_hash": HASH_C,
        },
    )
    await conn.execute(
        text(
            "INSERT INTO detection_run "
            "(id, tenant_id, file_version_id, detection_config_id, config_version, "
            "config_fingerprint, algorithm_bundle_version, input_fingerprint, "
            "run_fingerprint, source_row_count, parsed_row_count, error_row_count, "
            "finding_count, created_by, completed_at) VALUES "
            "(:run, :tenant, :file, :config, 1, :config_hash, 'correlation-v1', "
            ":input_hash, :run_hash, 2, 2, 0, 1, :user, now())"
        ),
        {
            **ids,
            "config_hash": HASH_A,
            "input_hash": HASH_D,
            "run_hash": HASH_E,
        },
    )
    await conn.execute(
        text(
            "INSERT INTO detection_request "
            "(id, tenant_id, file_version_id, detection_run_id, idempotency_key_hash, "
            "request_fingerprint) VALUES "
            "(:request, :tenant, :file, :run, :key_hash, :request_hash)"
        ),
        {**ids, "key_hash": HASH_D, "request_hash": HASH_E},
    )
    await conn.execute(
        text(
            "INSERT INTO capability_declaration "
            "(id, tenant_id, file_version_id, detection_run_id, config_fingerprint, "
            "detector, detector_version, status, reason_code, reason, details_json, "
            "finding_count) VALUES "
            "(:capability, :tenant, :file, :run, :config_hash, 'split_invoice', "
            "'split-window-v1', 'enabled', 'READY', '检测能力可用', '{}'::jsonb, 1)"
        ),
        {**ids, "config_hash": HASH_A},
    )
    await conn.execute(
        text(
            "INSERT INTO correlation_finding "
            "(id, tenant_id, file_version_id, detection_run_id, detector, detector_version, "
            "finding_key, evidence_schema_version, evidence_json, reasoning_snapshot, "
            "severity_impact, severity_confidence) VALUES "
            "(:finding, :tenant, :file, :run, 'split_invoice', 'split-window-v1', "
            ":finding_key, 1, '{}'::jsonb, '稳定统计候选', 0, 0)"
        ),
        {**ids, "finding_key": HASH_F},
    )
    await conn.execute(
        text(
            "INSERT INTO correlation_finding_row "
            "(id, tenant_id, finding_id, detection_run_id, file_version_id, row_no, ordinal) "
            "VALUES (:finding_row, :tenant, :finding, :run, :file, 1, 1)"
        ),
        ids,
    )
    return ids


async def test_f6_schema_constraints_and_triggers_exist(engine: AsyncEngine) -> None:
    expected_tables = {
        "detection_config",
        "detection_run",
        "detection_request",
        "correlation_finding_row",
    }
    expected_constraints = {
        "uq_detection_config_tenant_version",
        "uq_detection_config_tenant_idempotency_key",
        "uq_detection_config_snapshot",
        "uq_detection_run_file_config_fingerprint",
        "uq_detection_request_tenant_idempotency_key",
        "uq_capability_declaration_run_detector",
        "uq_correlation_finding_run_detector_key",
        "uq_correlation_finding_row_finding_row",
        "uq_correlation_finding_row_finding_ordinal",
        "fk_detection_run_config_snapshot",
        "fk_detection_request_run_identity",
        "fk_capability_declaration_run_identity",
        "fk_correlation_finding_run_identity",
        "fk_correlation_finding_row_finding_identity",
        "fk_correlation_finding_row_expense_identity",
        "ck_correlation_finding_severity_ungraded",
    }
    expected_trigger_tables = {
        "detection_config",
        "detection_run",
        "detection_request",
        "capability_declaration",
        "correlation_finding",
        "correlation_finding_row",
    }
    expected_fks = {
        ("detection_config", "fk_detection_config_creator_tenant"): (
            ["created_by", "tenant_id"],
            "app_user",
            ["id", "tenant_id"],
        ),
        ("detection_config", "fk_detection_config_tenant_id_tenant"): (
            ["tenant_id"],
            "tenant",
            ["id"],
        ),
        ("detection_run", "fk_detection_run_file_tenant"): (
            ["file_version_id", "tenant_id"],
            "file_version",
            ["id", "tenant_id"],
        ),
        ("detection_run", "fk_detection_run_config_snapshot"): (
            [
                "detection_config_id",
                "tenant_id",
                "config_version",
                "config_fingerprint",
                "algorithm_bundle_version",
            ],
            "detection_config",
            [
                "id",
                "tenant_id",
                "version",
                "config_fingerprint",
                "algorithm_bundle_version",
            ],
        ),
        ("detection_run", "fk_detection_run_creator_tenant"): (
            ["created_by", "tenant_id"],
            "app_user",
            ["id", "tenant_id"],
        ),
        ("detection_run", "fk_detection_run_tenant_id_tenant"): (
            ["tenant_id"],
            "tenant",
            ["id"],
        ),
        ("detection_request", "fk_detection_request_run_identity"): (
            ["detection_run_id", "tenant_id", "file_version_id"],
            "detection_run",
            ["id", "tenant_id", "file_version_id"],
        ),
        ("detection_request", "fk_detection_request_tenant_id_tenant"): (
            ["tenant_id"],
            "tenant",
            ["id"],
        ),
        ("capability_declaration", "fk_capability_declaration_run_identity"): (
            ["detection_run_id", "tenant_id", "file_version_id", "config_fingerprint"],
            "detection_run",
            ["id", "tenant_id", "file_version_id", "config_fingerprint"],
        ),
        ("capability_declaration", "fk_capability_declaration_tenant_id_tenant"): (
            ["tenant_id"],
            "tenant",
            ["id"],
        ),
        ("correlation_finding", "fk_correlation_finding_run_identity"): (
            ["detection_run_id", "tenant_id", "file_version_id"],
            "detection_run",
            ["id", "tenant_id", "file_version_id"],
        ),
        ("correlation_finding", "fk_correlation_finding_tenant_id_tenant"): (
            ["tenant_id"],
            "tenant",
            ["id"],
        ),
        ("correlation_finding_row", "fk_correlation_finding_row_finding_identity"): (
            ["finding_id", "detection_run_id", "file_version_id", "tenant_id"],
            "correlation_finding",
            ["id", "detection_run_id", "file_version_id", "tenant_id"],
        ),
        ("correlation_finding_row", "fk_correlation_finding_row_expense_identity"): (
            ["file_version_id", "row_no", "tenant_id"],
            "expense_row",
            ["file_version_id", "row_no", "tenant_id"],
        ),
        ("correlation_finding_row", "fk_correlation_finding_row_tenant_id_tenant"): (
            ["tenant_id"],
            "tenant",
            ["id"],
        ),
    }
    expected_checks = {
        ("detection_config", "ck_detection_config_version_positive"),
        ("detection_config", "ck_detection_config_schema_version_value"),
        ("detection_config", "ck_detection_config_algorithm_bundle_version_value"),
        ("detection_config", "ck_detection_config_definition_object"),
        ("detection_config", "ck_detection_config_definition_canonical_matches"),
        ("detection_config", "ck_detection_config_definition_canonical_size"),
        ("detection_config", "ck_detection_config_config_fingerprint_format"),
        ("detection_config", "ck_detection_config_idempotency_key_hash_format"),
        ("detection_config", "ck_detection_config_request_fingerprint_format"),
        ("detection_config", "ck_detection_config_change_reason_valid"),
        ("detection_run", "ck_detection_run_config_version_positive"),
        ("detection_run", "ck_detection_run_algorithm_bundle_version_value"),
        ("detection_run", "ck_detection_run_config_fingerprint_format"),
        ("detection_run", "ck_detection_run_input_fingerprint_format"),
        ("detection_run", "ck_detection_run_run_fingerprint_format"),
        ("detection_run", "ck_detection_run_counts_non_negative"),
        ("detection_run", "ck_detection_run_source_count_consistent"),
        ("detection_run", "ck_detection_run_completed_after_created"),
        ("detection_request", "ck_detection_request_idempotency_key_hash_format"),
        ("detection_request", "ck_detection_request_request_fingerprint_format"),
        ("capability_declaration", "ck_capability_declaration_detector_version_pair"),
        ("capability_declaration", "ck_capability_declaration_config_fingerprint_format"),
        ("capability_declaration", "ck_capability_declaration_status_values"),
        ("capability_declaration", "ck_capability_declaration_reason_code_format"),
        ("capability_declaration", "ck_capability_declaration_reason_valid"),
        ("capability_declaration", "ck_capability_declaration_details_object"),
        ("capability_declaration", "ck_capability_declaration_finding_count_non_negative"),
        ("correlation_finding", "ck_correlation_finding_detector_version_pair"),
        ("correlation_finding", "ck_correlation_finding_finding_key_format"),
        ("correlation_finding", "ck_correlation_finding_evidence_schema_version_value"),
        ("correlation_finding", "ck_correlation_finding_evidence_object"),
        ("correlation_finding", "ck_correlation_finding_reasoning_snapshot_non_empty"),
        ("correlation_finding", "ck_correlation_finding_severity_ungraded"),
        ("correlation_finding_row", "ck_correlation_finding_row_row_no_positive"),
        ("correlation_finding_row", "ck_correlation_finding_row_ordinal_positive"),
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
        triggers = set(
            (
                await conn.execute(
                    text(
                        "SELECT c.relname FROM pg_trigger t "
                        "JOIN pg_class c ON c.oid = t.tgrelid "
                        "WHERE NOT t.tgisinternal AND t.tgname LIKE 'trg_%_immutable' "
                        "AND c.relname = ANY(:names)"
                    ),
                    {"names": list(expected_trigger_tables)},
                )
            ).scalars()
        )
        fks = (
            await conn.execute(
                text(
                    "SELECT c.conrelid::regclass::text, c.conname, c.confdeltype, "
                    "ARRAY(SELECT a.attname FROM unnest(c.conkey) WITH ORDINALITY "
                    "AS k(attnum, ord) JOIN pg_attribute a ON a.attrelid = c.conrelid "
                    "AND a.attnum = k.attnum ORDER BY k.ord), "
                    "c.confrelid::regclass::text, "
                    "ARRAY(SELECT a.attname FROM unnest(c.confkey) WITH ORDINALITY "
                    "AS k(attnum, ord) JOIN pg_attribute a ON a.attrelid = c.confrelid "
                    "AND a.attnum = k.attnum ORDER BY k.ord) "
                    "FROM pg_constraint c WHERE c.contype = 'f' "
                    "AND conrelid::regclass::text = ANY(:tables)"
                ),
                {"tables": list(expected_trigger_tables)},
            )
        ).all()
        checks = (
            await conn.execute(
                text(
                    "SELECT conrelid::regclass::text, conname FROM pg_constraint "
                    "WHERE contype = 'c' AND conrelid::regclass::text = ANY(:tables)"
                ),
                {"tables": list(expected_trigger_tables)},
            )
        ).all()
    assert tables == expected_tables
    assert constraints == expected_constraints
    assert triggers == expected_trigger_tables
    assert {(row[0], row[1]) for row in fks} == set(expected_fks)
    assert all(row[2] == "r" for row in fks)
    for owner, name, _, source_columns, target, target_columns in fks:
        assert (source_columns, target, target_columns) == expected_fks[(owner, name)]
    assert {(row[0], row[1]) for row in checks} == expected_checks


async def test_f6_identity_checks_and_all_evidence_is_immutable(
    clean_db: None, engine: AsyncEngine
) -> None:
    async with engine.begin() as conn:
        ids = await _seed_f6_graph(conn)
        await _expect_integrity_error(
            conn,
            "INSERT INTO detection_config "
            "(id, tenant_id, version, definition, definition_canonical, schema_version, "
            "algorithm_bundle_version, config_fingerprint, created_by, change_reason, "
            "idempotency_key_hash, request_fingerprint) VALUES "
            "(:id, :tenant, 2, '{}'::jsonb, '{}', 1, 'correlation-v1', :hash, "
            ":other_user, 'wrong tenant', :key_hash, :request_hash)",
            {
                **ids,
                "id": uuid.uuid4(),
                "hash": HASH_B,
                "key_hash": "1" * 64,
                "request_hash": "2" * 64,
            },
        )
        await _expect_integrity_error(
            conn,
            "INSERT INTO detection_config "
            "(id, tenant_id, version, definition, definition_canonical, schema_version, "
            "algorithm_bundle_version, config_fingerprint, created_by, change_reason, "
            "idempotency_key_hash, request_fingerprint) VALUES "
            "(:id, :tenant, 2, '{}'::jsonb, :canonical, 1, 'correlation-v1', :hash, "
            ":user, 'mismatch', :key_hash, :request_hash)",
            {
                **ids,
                "id": uuid.uuid4(),
                "hash": HASH_B,
                "key_hash": "3" * 64,
                "request_hash": "4" * 64,
                "canonical": '{"x":1}',
            },
        )
        await _expect_integrity_error(
            conn,
            "INSERT INTO detection_run "
            "(id, tenant_id, file_version_id, detection_config_id, config_version, "
            "config_fingerprint, algorithm_bundle_version, input_fingerprint, "
            "run_fingerprint, source_row_count, parsed_row_count, error_row_count, "
            "finding_count, created_by, completed_at) VALUES "
            "(:id, :tenant, :other_file, :config, 1, :config_hash, 'correlation-v1', "
            ":input_hash, :run_hash, 1, 1, 0, 0, :user, now())",
            {
                **ids,
                "id": uuid.uuid4(),
                "config_hash": HASH_A,
                "input_hash": HASH_B,
                "run_hash": HASH_C,
            },
        )
        await _expect_integrity_error(
            conn,
            "INSERT INTO detection_run "
            "(id, tenant_id, file_version_id, detection_config_id, config_version, "
            "config_fingerprint, algorithm_bundle_version, input_fingerprint, "
            "run_fingerprint, source_row_count, parsed_row_count, error_row_count, "
            "finding_count, created_by, completed_at) VALUES "
            "(:id, :other_tenant, :other_file, :config, 1, :config_hash, "
            "'correlation-v1', :input_hash, :run_hash, 1, 1, 0, 0, :other_user, now())",
            {
                **ids,
                "id": uuid.uuid4(),
                "config_hash": HASH_A,
                "input_hash": HASH_B,
                "run_hash": HASH_C,
            },
        )
        await _expect_integrity_error(
            conn,
            "INSERT INTO detection_run "
            "(id, tenant_id, file_version_id, detection_config_id, config_version, "
            "config_fingerprint, algorithm_bundle_version, input_fingerprint, "
            "run_fingerprint, source_row_count, parsed_row_count, error_row_count, "
            "finding_count, created_by, completed_at) VALUES "
            "(:id, :tenant, :file_two, :config, 1, :wrong_hash, 'correlation-v1', "
            ":input_hash, :run_hash, 1, 1, 0, 0, :user, now())",
            {
                **ids,
                "id": uuid.uuid4(),
                "wrong_hash": HASH_B,
                "input_hash": HASH_C,
                "run_hash": HASH_D,
            },
        )
        await _expect_integrity_error(
            conn,
            "INSERT INTO detection_request "
            "(id, tenant_id, file_version_id, detection_run_id, idempotency_key_hash, "
            "request_fingerprint) VALUES "
            "(:id, :tenant, :file_two, :run, :key_hash, :request_hash)",
            {
                **ids,
                "id": uuid.uuid4(),
                "key_hash": "5" * 64,
                "request_hash": "6" * 64,
            },
        )
        await _expect_integrity_error(
            conn,
            "INSERT INTO capability_declaration "
            "(id, tenant_id, file_version_id, detection_run_id, config_fingerprint, "
            "detector, detector_version, status, reason_code, reason, details_json, "
            "finding_count) VALUES "
            "(:id, :tenant, :file, :run, :wrong_hash, 'sequential_invoice', "
            "'invoice-sequence-v1', 'enabled', 'READY', '可用', '{}'::jsonb, 0)",
            {**ids, "id": uuid.uuid4(), "wrong_hash": HASH_B},
        )
        await _expect_integrity_error(
            conn,
            "INSERT INTO correlation_finding "
            "(id, tenant_id, file_version_id, detection_run_id, detector, detector_version, "
            "finding_key, evidence_schema_version, evidence_json, reasoning_snapshot, "
            "severity_impact, severity_confidence) VALUES "
            "(:id, :tenant, :file, :run, 'sequential_invoice', 'invoice-sequence-v1', "
            ":finding_key, 1, '{}'::jsonb, '候选', 1, 0)",
            {**ids, "id": uuid.uuid4(), "finding_key": HASH_A},
        )
        await _expect_integrity_error(
            conn,
            "INSERT INTO correlation_finding_row "
            "(id, tenant_id, finding_id, detection_run_id, file_version_id, row_no, ordinal) "
            "VALUES (:id, :tenant, :finding, :run, :file, 2, 2)",
            {**ids, "id": uuid.uuid4()},
        )
        await _expect_integrity_error(
            conn,
            "INSERT INTO correlation_finding_row "
            "(id, tenant_id, finding_id, detection_run_id, file_version_id, row_no, ordinal) "
            "VALUES (:id, :tenant, :finding, :run, :file_two, 4, 2)",
            {**ids, "id": uuid.uuid4()},
        )
        await _expect_integrity_error(
            conn,
            "INSERT INTO correlation_finding_row "
            "(id, tenant_id, finding_id, detection_run_id, file_version_id, row_no, ordinal) "
            "VALUES (:id, :tenant, :finding, :run, :file, 3, 1)",
            {**ids, "id": uuid.uuid4()},
        )
        await _expect_integrity_error(
            conn,
            "INSERT INTO correlation_finding_row "
            "(id, tenant_id, finding_id, detection_run_id, file_version_id, row_no, ordinal) "
            "VALUES (:id, :tenant, :finding, :run, :file, 1, 2)",
            {**ids, "id": uuid.uuid4()},
        )
    async with engine.connect() as conn:
        for table_name in (
            "detection_config",
            "detection_run",
            "detection_request",
            "capability_declaration",
            "correlation_finding",
            "correlation_finding_row",
        ):
            immutable_table = table(table_name, column("created_at"))
            with pytest.raises(DBAPIError, match="immutable"):
                async with conn.begin_nested():
                    await conn.execute(
                        immutable_table.update().values(created_at=immutable_table.c.created_at)
                    )
            with pytest.raises(DBAPIError, match="immutable"):
                async with conn.begin_nested():
                    await conn.execute(immutable_table.delete())
        with pytest.raises(IntegrityError):
            async with conn.begin_nested():
                await conn.execute(
                    text("DELETE FROM expense_row WHERE file_version_id = :file AND row_no = 1"),
                    ids,
                )


def _seed_0007_legacy_roots(conn: Connection) -> dict[str, uuid.UUID]:
    ids = {name: uuid.uuid4() for name in ("tenant", "user", "file")}
    conn.execute(
        text("INSERT INTO tenant (id, slug, name) VALUES (:tenant, :slug, 'Legacy')"),
        {**ids, "slug": f"legacy-{ids['tenant'].hex[:8]}"},
    )
    conn.execute(
        text(
            "INSERT INTO app_user "
            "(id, tenant_id, username, password_hash, role, is_active) "
            "VALUES (:user, :tenant, 'legacy-user', 'test', 'auditor', true)"
        ),
        ids,
    )
    conn.execute(
        text(
            "INSERT INTO file_version "
            "(id, tenant_id, filename, content_hash, uploaded_by, revision_no, parse_status) "
            "VALUES (:file, :tenant, 'legacy.xlsx', :hash, :user, 1, 'unparsed')"
        ),
        {**ids, "hash": HASH_A},
    )
    return ids


def test_0008_preflight_round_trip_and_safe_downgrade(db_url: str) -> None:
    database_name = f"expenseguard_f61_{uuid.uuid4().hex[:12]}"
    source_url = make_url(db_url)
    admin_url = source_url.set(database="postgres")
    temporary_url = source_url.set(database=database_name)
    admin_engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    temporary_engine = None
    with admin_engine.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{database_name}"'))
    try:
        cfg = _migration_config(temporary_url.render_as_string(hide_password=False))
        command.upgrade(cfg, "0007")
        temporary_engine = create_engine(temporary_url)
        with temporary_engine.begin() as conn:
            ids = _seed_0007_legacy_roots(conn)
            legacy_snapshot = _legacy_f6_catalog_snapshot(conn)
            conn.execute(
                text(
                    "INSERT INTO capability_declaration "
                    "(id, tenant_id, file_version_id, detector, status, reason) "
                    "VALUES (:id, :tenant, :file, 'legacy', 'unavailable', NULL)"
                ),
                {**ids, "id": uuid.uuid4()},
            )
        with pytest.raises(RuntimeError, match="legacy capability_declaration"):
            command.upgrade(cfg, "0008")
        with temporary_engine.connect() as conn:
            assert conn.scalar(text("SELECT version_num FROM alembic_version")) == "0007"
            assert conn.scalar(text("SELECT to_regclass('detection_config')")) is None
            assert conn.scalar(text("SELECT count(*) FROM capability_declaration")) == 1
            assert _legacy_f6_catalog_snapshot(conn) == legacy_snapshot

        with temporary_engine.begin() as conn:
            conn.execute(text("TRUNCATE capability_declaration"))
            conn.execute(
                text(
                    "INSERT INTO correlation_finding "
                    "(id, tenant_id, file_version_id, detector, participating_row_nos, "
                    "evidence_json, severity_impact, severity_confidence) VALUES "
                    "(:id, :tenant, :file, 'legacy', ARRAY[1,2], '{}'::jsonb, 0, 0)"
                ),
                {**ids, "id": uuid.uuid4()},
            )
        with pytest.raises(RuntimeError, match="legacy capability_declaration"):
            command.upgrade(cfg, "0008")
        with temporary_engine.connect() as conn:
            assert conn.scalar(text("SELECT version_num FROM alembic_version")) == "0007"
            assert conn.scalar(text("SELECT to_regclass('detection_run')")) is None
            assert conn.scalar(text("SELECT count(*) FROM correlation_finding")) == 1
            assert _legacy_f6_catalog_snapshot(conn) == legacy_snapshot

        with temporary_engine.begin() as conn:
            conn.execute(text("TRUNCATE correlation_finding"))
        command.upgrade(cfg, "0008")
        command.downgrade(cfg, "0007")
        with temporary_engine.connect() as conn:
            assert conn.scalar(text("SELECT version_num FROM alembic_version")) == "0007"
            assert _legacy_f6_catalog_snapshot(conn) == legacy_snapshot
            assert (
                conn.scalar(
                    text(
                        "SELECT count(*) FROM (VALUES "
                        "(to_regclass('detection_config')), "
                        "(to_regclass('detection_run')), "
                        "(to_regclass('detection_request')), "
                        "(to_regclass('correlation_finding_row'))) AS absent(value) "
                        "WHERE value IS NOT NULL"
                    )
                )
                == 0
            )
            assert conn.scalar(text("SELECT to_regprocedure('f6_reject_update_delete()') IS NULL"))
            assert (
                conn.scalar(
                    text(
                        "SELECT is_nullable FROM information_schema.columns "
                        "WHERE table_name = 'capability_declaration' AND column_name = 'reason'"
                    )
                )
                == "YES"
            )
            assert (
                conn.scalar(
                    text(
                        "SELECT data_type FROM information_schema.columns "
                        "WHERE table_name = 'correlation_finding' "
                        "AND column_name = 'participating_row_nos'"
                    )
                )
                == "ARRAY"
            )
            assert conn.scalar(
                text(
                    "SELECT EXISTS (SELECT 1 FROM pg_constraint "
                    "WHERE conname = 'uq_capability_declaration_file_version_id_detector')"
                )
            )
            cascade_count = conn.scalar(
                text(
                    "SELECT count(*) FROM pg_constraint "
                    "WHERE conrelid IN ('capability_declaration'::regclass, "
                    "'correlation_finding'::regclass) AND contype = 'f' AND confdeltype = 'c'"
                )
            )
            assert cascade_count == 2

        command.upgrade(cfg, "0008")
        with temporary_engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO detection_config "
                    "(id, tenant_id, version, definition, definition_canonical, schema_version, "
                    "algorithm_bundle_version, config_fingerprint, created_by, change_reason, "
                    "idempotency_key_hash, request_fingerprint) VALUES "
                    "(:id, :tenant, 1, '{}'::jsonb, '{}', 1, 'correlation-v1', :hash, "
                    ":user, 'guard', :key_hash, :request_hash)"
                ),
                {
                    **ids,
                    "id": uuid.uuid4(),
                    "hash": HASH_B,
                    "key_hash": HASH_C,
                    "request_hash": HASH_D,
                },
            )
        with pytest.raises(RuntimeError, match="cannot downgrade 0008"):
            command.downgrade(cfg, "0007")
        with temporary_engine.connect() as conn:
            assert conn.scalar(text("SELECT version_num FROM alembic_version")) == "0008"
            assert conn.scalar(text("SELECT count(*) FROM detection_config")) == 1
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
