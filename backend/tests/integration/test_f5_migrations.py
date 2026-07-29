"""CP-F5.1 migration, tenant identity, and immutable-evidence tests."""

import uuid

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


async def _seed_f5_graph(conn: AsyncConnection) -> dict[str, uuid.UUID]:
    ids = {
        name: uuid.uuid4()
        for name in (
            "tenant",
            "other_tenant",
            "user",
            "other_user",
            "mapping",
            "file",
            "validation",
            "finding",
            "report",
            "item",
            "config",
            "plan",
            "sample",
            "review",
            "sampling_review",
            "request",
        )
    }
    await conn.execute(
        text(
            "INSERT INTO tenant (id, slug, name) VALUES "
            "(:tenant, :slug, 'F5'), (:other_tenant, :other_slug, 'Other')"
        ),
        {
            "tenant": ids["tenant"],
            "slug": f"f5-{ids['tenant'].hex[:8]}",
            "other_tenant": ids["other_tenant"],
            "other_slug": f"other-{ids['other_tenant'].hex[:8]}",
        },
    )
    await conn.execute(
        text(
            "INSERT INTO app_user "
            "(id, tenant_id, username, password_hash, role, is_active) VALUES "
            "(:user, :tenant, 'f5-user', 'test', 'configurator', true), "
            "(:other_user, :other_tenant, 'other-user', 'test', 'configurator', true)"
        ),
        ids,
    )
    await conn.execute(
        text(
            "INSERT INTO schema_mapping_version "
            "(id, tenant_id, header_signature, version, config_fingerprint, "
            "availability_thresholds, currency_aliases, inference_config, created_by) "
            "VALUES (:mapping, :tenant, :hash, 1, :hash, '{}'::jsonb, '{}'::jsonb, "
            "'{}'::jsonb, :user)"
        ),
        {**ids, "hash": HASH_A},
    )
    await conn.execute(
        text(
            "INSERT INTO file_version "
            "(id, tenant_id, filename, content_hash, row_count, uploaded_by, mapping_version_id, "
            "parse_status, parsed_at, revision_no) VALUES "
            "(:file, :tenant, 'f5.xlsx', :hash, 2, :user, :mapping, 'parsed', now(), 1)"
        ),
        {**ids, "hash": HASH_B},
    )
    await conn.execute(
        text(
            "INSERT INTO expense_row (id, tenant_id, file_version_id, row_no, raw_json) VALUES "
            "(:passed_row, :tenant, :file, 2, '{}'::jsonb), "
            "(:flagged_row, :tenant, :file, 3, '{}'::jsonb)"
        ),
        {**ids, "passed_row": uuid.uuid4(), "flagged_row": uuid.uuid4()},
    )
    await conn.execute(
        text(
            "INSERT INTO row_result "
            "(id, tenant_id, file_version_id, row_no, verdict, rule_version) VALUES "
            "(:passed_result, :tenant, :file, 2, 'passed', :hash), "
            "(:flagged_result, :tenant, :file, 3, 'flagged', :hash)"
        ),
        {
            **ids,
            "passed_result": uuid.uuid4(),
            "flagged_result": uuid.uuid4(),
            "hash": HASH_C,
        },
    )
    await conn.execute(
        text(
            "INSERT INTO validation_run "
            "(id, tenant_id, file_version_id, mapping_version_id, ruleset_fingerprint, "
            "ruleset_manifest, status, total_row_count, evaluated_row_count, passed_count, "
            "flagged_count, manual_review_count, parse_failed_count, completed_at, triggered_by) "
            "VALUES (:validation, :tenant, :file, :mapping, :hash, '{}'::jsonb, 'completed', "
            "2, 2, 1, 1, 0, 0, now(), :user)"
        ),
        {**ids, "hash": HASH_C},
    )
    await conn.execute(
        text(
            "INSERT INTO finding "
            "(id, tenant_id, file_version_id, row_no, kind, severity_impact, "
            "severity_confidence, validation_run_id) VALUES "
            "(:finding, :tenant, :file, 3, 'limit_exceeded', 2, 3, :validation)"
        ),
        ids,
    )
    await conn.execute(
        text(
            "INSERT INTO report_run "
            "(id, tenant_id, file_version_id, validation_run_id, mapping_version_id, status, "
            "report_fingerprint, request_fingerprint, idempotency_key_hash, "
            "source_content_sha256, ruleset_fingerprint, template_version, "
            "attention_mapping_version, policy_manifest, binding_manifest, stored_row_count, "
            "validated_row_count, flagged_row_count, manual_review_row_count, passed_row_count, "
            "parse_error_row_count, report_item_count, verified_citation_count, "
            "unavailable_citation_count, high_attention_row_count, "
            "manual_attention_row_count, cleared_row_count, created_by, completed_at) VALUES "
            "(:report, :tenant, :file, :validation, :mapping, 'completed', :report_hash, "
            ":request_hash, :key_hash, :source_hash, :ruleset_hash, 'report-v1', "
            "'attention-v1', '{}'::jsonb, '{}'::jsonb, 2, 2, 1, 0, 1, 0, 1, 0, 1, "
            "1, 0, 1, :user, now())"
        ),
        {
            **ids,
            "report_hash": HASH_A,
            "request_hash": HASH_B,
            "key_hash": HASH_C,
            "source_hash": HASH_B,
            "ruleset_hash": HASH_C,
        },
    )
    await conn.execute(
        text(
            "INSERT INTO report_item "
            "(id, tenant_id, report_run_id, finding_id, file_version_id, row_no, rule_id, "
            "rule_version, source_outcome, source_verdict, reason_code, attention_group, "
            "citation_status, requires_manual_citation, source_content_sha256) VALUES "
            "(:item, :tenant, :report, :finding, :file, 3, 'limit', '1', 'flagged', "
            "'flagged', 'LIMIT_EXCEEDED', 'high_attention', 'unavailable', true, :hash)"
        ),
        {**ids, "hash": HASH_B},
    )
    await conn.execute(
        text(
            "INSERT INTO review_sampling_config "
            "(id, tenant_id, version, rate_bps, min_sample_size, max_sample_size, "
            "algorithm_version, config_fingerprint, idempotency_key_hash, "
            "request_fingerprint, created_by, change_reason) VALUES "
            "(:config, :tenant, 1, 10000, 1, 1, 'sha256-rank-v1', :config_hash, "
            ":config_key, :config_request, :user, 'initial')"
        ),
        {
            **ids,
            "config_hash": HASH_C,
            "config_key": HASH_D,
            "config_request": HASH_E,
        },
    )
    await conn.execute(
        text(
            "INSERT INTO review_sampling_plan "
            "(id, tenant_id, report_run_id, file_version_id, sampling_config_id, "
            "config_version, config_fingerprint, rate_bps, min_sample_size, max_sample_size, "
            "algorithm_version, seed_hex, eligible_count, sample_size, created_by) VALUES "
            "(:plan, :tenant, :report, :file, :config, 1, :config_hash, 10000, 1, 1, "
            "'sha256-rank-v1', :seed, 1, 1, :user)"
        ),
        {**ids, "config_hash": HASH_C, "seed": HASH_F},
    )
    await conn.execute(
        text(
            "INSERT INTO sampling_audit "
            "(id, tenant_id, sampling_plan_id, report_run_id, file_version_id, row_no, "
            "selection_rank, selection_score_sha256) VALUES "
            "(:sample, :tenant, :plan, :report, :file, 2, 1, :score)"
        ),
        {**ids, "score": HASH_A},
    )
    await conn.execute(
        text(
            "INSERT INTO review "
            "(id, tenant_id, report_run_id, report_item_id, file_version_id, finding_id, "
            "decision, reviewer_id, idempotency_key_hash, request_fingerprint) VALUES "
            "(:review, :tenant, :report, :item, :file, :finding, 'confirmed', :user, "
            ":review_key, :review_request)"
        ),
        {**ids, "review_key": HASH_A, "review_request": HASH_B},
    )
    await conn.execute(
        text(
            "INSERT INTO sampling_review "
            "(id, tenant_id, sampling_audit_id, sampling_plan_id, report_run_id, "
            "file_version_id, decision, reviewer_id, idempotency_key_hash, "
            "request_fingerprint) VALUES "
            "(:sampling_review, :tenant, :sample, :plan, :report, :file, "
            "'clearance_confirmed', :user, :sampling_key, :sampling_request)"
        ),
        {**ids, "sampling_key": HASH_B, "sampling_request": HASH_C},
    )
    await conn.execute(
        text(
            "INSERT INTO review_plan_request "
            "(id, tenant_id, report_run_id, sampling_plan_id, idempotency_key_hash, "
            "request_fingerprint) VALUES "
            "(:request, :tenant, :report, :plan, :request_key, :request_fingerprint)"
        ),
        {**ids, "request_key": HASH_C, "request_fingerprint": HASH_D},
    )
    return ids


async def test_f5_schema_constraints_and_triggers_exist(engine: AsyncEngine) -> None:
    expected_tables = {
        "review_sampling_config",
        "review_sampling_plan",
        "sampling_review",
        "review_plan_request",
    }
    expected_constraints = {
        "uq_review_finding_id",
        "uq_sampling_audit_file_version_id_row_no",
        "uq_report_item_review_identity",
        "uq_expense_row_file_row_tenant",
        "uq_row_result_file_row_tenant",
        "fk_review_report_item_identity",
        "fk_sampling_audit_expense_row_identity",
        "fk_sampling_audit_row_result_identity",
        "fk_sampling_review_sample_identity",
    }
    expected_trigger_tables = {
        "review_sampling_config",
        "review_sampling_plan",
        "review",
        "sampling_audit",
        "sampling_review",
        "review_plan_request",
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
        non_restrict_fks = (
            (
                await conn.execute(
                    text(
                        "SELECT conname FROM pg_constraint "
                        "WHERE contype = 'f' AND conrelid::regclass::text = ANY(:tables) "
                        "AND confdeltype <> 'r'"
                    ),
                    {
                        "tables": [
                            "review_sampling_config",
                            "review_sampling_plan",
                            "review",
                            "sampling_audit",
                            "sampling_review",
                            "review_plan_request",
                        ]
                    },
                )
            )
            .scalars()
            .all()
        )
    assert tables == expected_tables
    assert constraints == expected_constraints
    assert triggers == expected_trigger_tables
    assert non_restrict_fks == []


async def test_f5_identity_checks_and_all_evidence_is_immutable(
    clean_db: None, engine: AsyncEngine
) -> None:
    async with engine.begin() as conn:
        ids = await _seed_f5_graph(conn)
        await _expect_integrity_error(
            conn,
            "INSERT INTO review_sampling_config "
            "(id, tenant_id, version, rate_bps, min_sample_size, max_sample_size, "
            "algorithm_version, config_fingerprint, idempotency_key_hash, "
            "request_fingerprint, created_by, change_reason) VALUES "
            "(:id, :tenant, 2, 100, 1, 1, 'sha256-rank-v1', :hash, :key_hash, "
            ":request_hash, :other_user, 'wrong tenant')",
            {
                "id": uuid.uuid4(),
                "tenant": ids["tenant"],
                "other_user": ids["other_user"],
                "hash": HASH_A,
                "key_hash": "1" * 64,
                "request_hash": "2" * 64,
            },
        )
        await _expect_integrity_error(
            conn,
            "INSERT INTO review_sampling_config "
            "(id, tenant_id, version, rate_bps, min_sample_size, max_sample_size, "
            "algorithm_version, config_fingerprint, idempotency_key_hash, "
            "request_fingerprint, created_by, change_reason) VALUES "
            "(:id, :tenant, 2, 0, 1, 1, 'sha256-rank-v1', :hash, :key_hash, "
            ":request_hash, :user, 'invalid rate')",
            {
                "id": uuid.uuid4(),
                "tenant": ids["tenant"],
                "user": ids["user"],
                "hash": HASH_A,
                "key_hash": "3" * 64,
                "request_hash": "4" * 64,
            },
        )
    async with engine.connect() as conn:
        for table_name in (
            "review_sampling_config",
            "review_sampling_plan",
            "review",
            "sampling_audit",
            "sampling_review",
            "review_plan_request",
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


def _seed_0006_legacy_roots(conn: Connection) -> dict[str, uuid.UUID]:
    ids = {name: uuid.uuid4() for name in ("tenant", "user", "file", "finding", "review")}
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
    conn.execute(
        text(
            "INSERT INTO finding "
            "(id, tenant_id, file_version_id, row_no, kind, severity_impact, "
            "severity_confidence) VALUES (:finding, :tenant, :file, 2, 'legacy', 0, 0)"
        ),
        ids,
    )
    return ids


def test_0007_preflight_round_trip_and_safe_downgrade(db_url: str) -> None:
    database_name = f"expenseguard_f51_{uuid.uuid4().hex[:12]}"
    source_url = make_url(db_url)
    admin_url = source_url.set(database="postgres")
    temporary_url = source_url.set(database=database_name)
    admin_engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    temporary_engine = None
    with admin_engine.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{database_name}"'))
    try:
        cfg = _migration_config(temporary_url.render_as_string(hide_password=False))
        command.upgrade(cfg, "0006")
        temporary_engine = create_engine(temporary_url)
        with temporary_engine.begin() as conn:
            ids = _seed_0006_legacy_roots(conn)
            conn.execute(
                text(
                    "INSERT INTO sampling_audit "
                    "(id, tenant_id, file_version_id, row_no) "
                    "VALUES (:id, :tenant, :file, 2)"
                ),
                {**ids, "id": uuid.uuid4()},
            )
        with pytest.raises(RuntimeError, match="legacy review or sampling_audit"):
            command.upgrade(cfg, "0007")
        with temporary_engine.connect() as conn:
            assert conn.scalar(text("SELECT version_num FROM alembic_version")) == "0006"
            assert conn.scalar(text("SELECT to_regclass('review_sampling_config')")) is None

        with temporary_engine.begin() as conn:
            conn.execute(text("TRUNCATE sampling_audit"))
            conn.execute(
                text(
                    "INSERT INTO review "
                    "(id, tenant_id, finding_id, decision, reviewer_id) "
                    "VALUES (:review, :tenant, :finding, 'confirmed', :user)"
                ),
                ids,
            )
        with pytest.raises(RuntimeError, match="legacy review or sampling_audit"):
            command.upgrade(cfg, "0007")
        with temporary_engine.connect() as conn:
            assert conn.scalar(text("SELECT version_num FROM alembic_version")) == "0006"

        with temporary_engine.begin() as conn:
            conn.execute(text("TRUNCATE review"))
        command.upgrade(cfg, "0007")
        command.downgrade(cfg, "0006")
        command.upgrade(cfg, "0007")
        with temporary_engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO review_sampling_config "
                    "(id, tenant_id, version, rate_bps, min_sample_size, max_sample_size, "
                    "algorithm_version, config_fingerprint, idempotency_key_hash, "
                    "request_fingerprint, created_by, change_reason) VALUES "
                    "(:id, :tenant, 1, 100, 1, 10, 'sha256-rank-v1', :hash, "
                    ":key_hash, :request_hash, :user, 'guard')"
                ),
                {
                    **ids,
                    "id": uuid.uuid4(),
                    "hash": HASH_B,
                    "key_hash": HASH_C,
                    "request_hash": HASH_D,
                },
            )
        with pytest.raises(RuntimeError, match="cannot downgrade 0007"):
            command.downgrade(cfg, "0006")
        with temporary_engine.connect() as conn:
            assert conn.scalar(text("SELECT version_num FROM alembic_version")) == "0007"
            assert conn.scalar(text("SELECT count(*) FROM review_sampling_config")) == 1
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
