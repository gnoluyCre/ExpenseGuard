"""CP-F4.1 migration, tenant-closure, and protected-invariant tests."""

import uuid
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, make_url
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

pytestmark = pytest.mark.integration

BACKEND_DIR = Path(__file__).resolve().parents[2]
HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64


async def _expect_integrity_error(
    conn: AsyncConnection, statement: str, params: dict[str, Any]
) -> None:
    with pytest.raises(IntegrityError):
        async with conn.begin_nested():
            await conn.execute(text(statement), params)


async def _seed_actor(conn: AsyncConnection, label: str) -> tuple[uuid.UUID, uuid.UUID]:
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    await conn.execute(
        text("INSERT INTO tenant (id, slug, name) VALUES (:id, :slug, :name)"),
        {"id": tenant_id, "slug": f"f4-{label}-{tenant_id.hex[:8]}", "name": label},
    )
    await conn.execute(
        text(
            "INSERT INTO app_user "
            "(id, tenant_id, username, password_hash, role, is_active) "
            "VALUES (:id, :tenant_id, :username, 'test', 'configurator', true)"
        ),
        {"id": user_id, "tenant_id": tenant_id, "username": f"user-{label}"},
    )
    return tenant_id, user_id


async def _seed_policy_roots(
    conn: AsyncConnection,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    label: str,
) -> tuple[uuid.UUID, uuid.UUID]:
    family_id = uuid.uuid4()
    blob_id = uuid.uuid4()
    await conn.execute(
        text(
            "INSERT INTO policy_family "
            "(id, tenant_id, stable_key, display_name, created_by) "
            "VALUES (:id, :tenant_id, :stable_key, :display_name, :created_by)"
        ),
        {
            "id": family_id,
            "tenant_id": tenant_id,
            "stable_key": f"policy-{label}",
            "display_name": f"Policy {label}",
            "created_by": user_id,
        },
    )
    await conn.execute(
        text(
            "INSERT INTO policy_source_blob "
            "(id, tenant_id, storage_key, mime_type, size_bytes, content_sha256, created_by) "
            "VALUES (:id, :tenant_id, :storage_key, 'text/plain', 10, :hash, :created_by)"
        ),
        {
            "id": blob_id,
            "tenant_id": tenant_id,
            "storage_key": f"policies/{label}.txt",
            "hash": f"{int(label[-1], 16):064x}" if label[-1] in "0123456789abcdef" else HASH_A,
            "created_by": user_id,
        },
    )
    return family_id, blob_id


async def _insert_document(
    conn: AsyncConnection,
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    family_id: uuid.UUID,
    blob_id: uuid.UUID,
    version: str,
    effective_date: date,
    expiry_date: date | None,
    status: str = "published",
) -> uuid.UUID:
    document_id = uuid.uuid4()
    await conn.execute(
        text(
            "INSERT INTO policy_document "
            "(id, tenant_id, title, version, effective_date, expiry_date, source_filename, "
            "family_id, source_blob_id, content_sha256, mime_type, size_bytes, "
            "extracted_text_sha256, parser_version, chunker_version, status, "
            "created_by, published_by, published_at) VALUES "
            "(:id, :tenant_id, :title, :version, :effective_date, :expiry_date, 'policy.txt', "
            ":family_id, :blob_id, :hash, 'text/plain', 10, :hash, 'parser-v1', "
            "'chunker-v1', :status, :user_id, :published_by, "
            ":published_at)"
        ),
        {
            "id": document_id,
            "tenant_id": tenant_id,
            "title": f"Policy {version}",
            "version": version,
            "effective_date": effective_date,
            "expiry_date": expiry_date,
            "family_id": family_id,
            "blob_id": blob_id,
            "hash": HASH_A,
            "status": status,
            "user_id": user_id,
            "published_by": user_id if status == "published" else None,
            "published_at": datetime.now(UTC) if status == "published" else None,
        },
    )
    return document_id


async def test_f4_schema_constraints_and_restrict_actions_exist(engine: AsyncEngine) -> None:
    expected_tables = {
        "policy_family",
        "policy_source_blob",
        "policy_chunk",
        "policy_index_generation",
        "policy_document_index",
        "policy_index_job",
        "rule_policy_binding",
        "report_run",
        "report_item",
        "report_parse_error",
        "report_citation",
        "report_export",
    }
    expected_constraints = {
        "ex_policy_document_published_effective_interval": "x",
        "fk_policy_clause_document_tenant": "f",
        "fk_finding_clause_tenant": "f",
        "fk_policy_chunk_clause_tenant_document": "f",
        "fk_policy_index_job_chunk_tenant_document": "f",
        "fk_rule_policy_binding_document_tenant_family": "f",
        "fk_report_run_validation_tenant_file": "f",
        "fk_report_item_finding_tenant_file": "f",
        "fk_report_citation_binding_identity": "f",
        "fk_report_export_report_tenant": "f",
        "uq_rule_policy_binding_citation_identity": "u",
        "uq_report_run_file_version_id": "u",
        "ck_report_run_counts_nonnegative": "c",
        "ck_report_export_xlsx_only": "c",
    }
    expected_indexes = {
        "uq_policy_index_generation_one_active_per_tenant",
        "ix_policy_index_job_claim",
        "ix_report_item_report_attention_order",
        "ix_report_parse_error_report_order",
    }
    async with engine.connect() as conn:
        tables = {
            row[0]
            for row in await conn.execute(
                text(
                    "SELECT tablename FROM pg_tables "
                    "WHERE schemaname = 'public' AND tablename = ANY(:names)"
                ),
                {"names": list(expected_tables)},
            )
        }
        constraints = {
            row[0]: row[1]
            for row in await conn.execute(
                text(
                    "SELECT conname, contype FROM pg_constraint c "
                    "JOIN pg_namespace n ON n.oid = c.connamespace "
                    "WHERE n.nspname = 'public' AND conname = ANY(:names)"
                ),
                {"names": list(expected_constraints)},
            )
        }
        restrict_actions = {
            row[0]: row[1]
            for row in await conn.execute(
                text("SELECT conname, confdeltype FROM pg_constraint WHERE conname = ANY(:names)"),
                {
                    "names": [
                        "fk_policy_clause_document_tenant",
                        "fk_finding_clause_tenant",
                        "fk_report_citation_binding_identity",
                    ]
                },
            )
        }
        old_fks = await conn.scalar(
            text("SELECT count(*) FROM pg_constraint WHERE conname = ANY(:names)"),
            {
                "names": [
                    "fk_policy_clause_document_id_policy_document",
                    "fk_finding_clause_id_policy_clause",
                ]
            },
        )
        has_btree_gist = await conn.scalar(
            text("SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'btree_gist')")
        )
        indexes = {
            row[0]
            for row in await conn.execute(
                text(
                    "SELECT indexname FROM pg_indexes "
                    "WHERE schemaname = 'public' AND indexname = ANY(:names)"
                ),
                {"names": list(expected_indexes)},
            )
        }
        forbidden_job_columns = await conn.scalar(
            text(
                "SELECT count(*) FROM information_schema.columns "
                "WHERE table_name = 'policy_index_job' "
                "AND column_name = ANY(:names)"
            ),
            {"names": ["text", "clause_text", "exception_text", "token", "secret"]},
        )
    assert tables == expected_tables
    assert constraints == expected_constraints
    assert restrict_actions == {
        "fk_policy_clause_document_tenant": "r",
        "fk_finding_clause_tenant": "r",
        "fk_report_citation_binding_identity": "r",
    }
    assert old_fks == 0
    assert has_btree_gist is True
    assert indexes == expected_indexes
    assert forbidden_job_columns == 0


async def test_policy_interval_tenant_closure_and_restrict_are_enforced(
    engine: AsyncEngine, clean_db: None
) -> None:
    del clean_db
    async with engine.begin() as conn:
        tenant_a, user_a = await _seed_actor(conn, "a")
        tenant_b, user_b = await _seed_actor(conn, "b")
        await _expect_integrity_error(
            conn,
            "INSERT INTO policy_document "
            "(id, tenant_id, title, version, effective_date) "
            "VALUES (:id, :tenant_id, 'No status', '1', DATE '2020-01-01')",
            {"id": uuid.uuid4(), "tenant_id": tenant_a},
        )
        with pytest.raises(DBAPIError, match="reserved for the 0005 backfill"):
            async with conn.begin_nested():
                await conn.execute(
                    text(
                        "INSERT INTO policy_document "
                        "(id, tenant_id, title, version, effective_date, status) "
                        "VALUES (:id, :tenant_id, 'Fake legacy', '1', "
                        "DATE '2020-01-01', 'legacy_unpublished')"
                    ),
                    {"id": uuid.uuid4(), "tenant_id": tenant_a},
                )
        family_a, blob_a = await _seed_policy_roots(conn, tenant_a, user_a, "a")
        family_b, blob_b = await _seed_policy_roots(conn, tenant_b, user_b, "b")
        document_a = await _insert_document(
            conn,
            tenant_id=tenant_a,
            user_id=user_a,
            family_id=family_a,
            blob_id=blob_a,
            version="1",
            effective_date=date(2026, 1, 1),
            expiry_date=date(2026, 2, 1),
        )
        adjacent_document = await _insert_document(
            conn,
            tenant_id=tenant_a,
            user_id=user_a,
            family_id=family_a,
            blob_id=blob_a,
            version="2",
            effective_date=date(2026, 2, 1),
            expiry_date=date(2026, 3, 1),
        )
        assert adjacent_document != document_a

        with pytest.raises(IntegrityError):
            async with conn.begin_nested():
                await _insert_document(
                    conn,
                    tenant_id=tenant_a,
                    user_id=user_a,
                    family_id=family_a,
                    blob_id=blob_a,
                    version="overlap",
                    effective_date=date(2026, 1, 15),
                    expiry_date=date(2026, 2, 15),
                )

        await _expect_integrity_error(
            conn,
            "INSERT INTO policy_document "
            "(id, tenant_id, title, version, effective_date, family_id, source_blob_id, "
            "content_sha256, mime_type, size_bytes, extracted_text_sha256, parser_version, "
            "chunker_version, status, created_by) VALUES "
            "(:id, :tenant_id, 'Mismatch', '1', DATE '2027-01-01', :family_id, :blob_id, "
            ":hash, 'text/plain', 10, :hash, 'parser-v1', 'chunker-v1', 'draft', :user_id)",
            {
                "id": uuid.uuid4(),
                "tenant_id": tenant_a,
                "family_id": family_b,
                "blob_id": blob_b,
                "hash": HASH_A,
                "user_id": user_a,
            },
        )

        clause_id = uuid.uuid4()
        await conn.execute(
            text(
                "INSERT INTO policy_clause "
                "(id, tenant_id, document_id, family_id, clause_no, text, ordinal, "
                "text_sha256, source_locator_json) VALUES "
                "(:id, :tenant_id, :document_id, :family_id, '1', 'Exact text', 1, "
                ":hash, CAST('{\"page\": 1}' AS jsonb))"
            ),
            {
                "id": clause_id,
                "tenant_id": tenant_a,
                "document_id": document_a,
                "family_id": family_a,
                "hash": HASH_B,
            },
        )
        await _expect_integrity_error(
            conn,
            "INSERT INTO policy_clause "
            "(id, tenant_id, document_id, family_id, clause_no, text, ordinal, "
            "text_sha256, source_locator_json) VALUES "
            "(:id, :tenant_id, :document_id, :family_id, 'bad', 'Bad', 2, "
            ":hash, CAST('{}' AS jsonb))",
            {
                "id": uuid.uuid4(),
                "tenant_id": tenant_b,
                "document_id": document_a,
                "family_id": family_a,
                "hash": HASH_C,
            },
        )

        await _expect_integrity_error(
            conn,
            "INSERT INTO policy_chunk "
            "(id, tenant_id, document_id, clause_id, chunk_no, start_offset, end_offset, "
            "text, text_sha256, chunker_version) VALUES "
            "(:id, :tenant_id, :document_id, :clause_id, 1, 0, 5, 'Exact', :hash, 'v1')",
            {
                "id": uuid.uuid4(),
                "tenant_id": tenant_b,
                "document_id": document_a,
                "clause_id": clause_id,
                "hash": HASH_C,
            },
        )
        chunk_id = uuid.uuid4()
        await conn.execute(
            text(
                "INSERT INTO policy_chunk "
                "(id, tenant_id, document_id, clause_id, chunk_no, start_offset, end_offset, "
                "text, text_sha256, chunker_version) VALUES "
                "(:id, :tenant_id, :document_id, :clause_id, 1, 0, 5, "
                "'Exact', :hash, 'chunker-v1')"
            ),
            {
                "id": chunk_id,
                "tenant_id": tenant_a,
                "document_id": document_a,
                "clause_id": clause_id,
                "hash": HASH_C,
            },
        )

        generation_sql = (
            "INSERT INTO policy_index_generation "
            "(id, tenant_id, generation, manifest_revision, collection_name, "
            "collection_alias, vector_size, distance, embedding_model_family, "
            "embedding_model_id, embedding_model_revision, embedding_model_fingerprint, "
            "rerank_model_family, rerank_model_id, rerank_model_revision, "
            "rerank_model_fingerprint, parser_version, chunker_version, "
            "source_manifest_fingerprint, expected_point_count, completed_point_count, "
            "status, created_by) VALUES "
            "(:id, :tenant_id, :generation, 1, :collection, :alias, 768, 'cosine', "
            "'embedding', 'embedding-id', 'r1', :hash, 'rerank', 'rerank-id', 'r1', "
            ":hash, 'parser-v1', 'chunker-v1', :hash, 1, 0, 'building', :user_id)"
        )
        generation_a = uuid.uuid4()
        generation_b = uuid.uuid4()
        for generation_id, tenant_id, user_id, generation in (
            (generation_a, tenant_a, user_a, 1),
            (generation_b, tenant_b, user_b, 1),
        ):
            await conn.execute(
                text(generation_sql),
                {
                    "id": generation_id,
                    "tenant_id": tenant_id,
                    "generation": generation,
                    "collection": f"collection-{tenant_id.hex[:8]}",
                    "alias": f"alias-{tenant_id.hex[:8]}",
                    "hash": HASH_A,
                    "user_id": user_id,
                },
            )

        await _expect_integrity_error(
            conn,
            "INSERT INTO policy_document_index "
            "(id, tenant_id, document_id, index_generation_id, status, "
            "expected_point_count, completed_point_count, manifest_fingerprint) VALUES "
            "(:id, :tenant_id, :document_id, :generation_id, 'pending', 1, 0, :hash)",
            {
                "id": uuid.uuid4(),
                "tenant_id": tenant_a,
                "document_id": document_a,
                "generation_id": generation_b,
                "hash": HASH_A,
            },
        )
        job_sql = (
            "INSERT INTO policy_index_job "
            "(id, tenant_id, document_id, chunk_id, index_generation_id, operation, status, "
            "attempt_count, attempt_limit, available_at) VALUES "
            "(:id, :tenant_id, :document_id, :chunk_id, :generation_id, "
            "'upsert', 'pending', 0, 3, now())"
        )
        await _expect_integrity_error(
            conn,
            job_sql,
            {
                "id": uuid.uuid4(),
                "tenant_id": tenant_a,
                "document_id": document_a,
                "chunk_id": chunk_id,
                "generation_id": generation_b,
            },
        )
        await conn.execute(
            text(job_sql),
            {
                "id": uuid.uuid4(),
                "tenant_id": tenant_a,
                "document_id": document_a,
                "chunk_id": chunk_id,
                "generation_id": generation_a,
            },
        )
        await _expect_integrity_error(
            conn,
            job_sql,
            {
                "id": uuid.uuid4(),
                "tenant_id": tenant_a,
                "document_id": document_a,
                "chunk_id": chunk_id,
                "generation_id": generation_a,
            },
        )

        rule_b = uuid.uuid4()
        await conn.execute(
            text(
                "INSERT INTO rule_config "
                "(id, tenant_id, rule_id, definition, version, is_active, "
                "config_fingerprint, created_by, backfilled_legacy) VALUES "
                "(:id, :tenant_id, 'rule.b', CAST('{}' AS jsonb), 1, true, "
                ":hash, :user_id, false)"
            ),
            {"id": rule_b, "tenant_id": tenant_b, "hash": HASH_A, "user_id": user_b},
        )
        await _expect_integrity_error(
            conn,
            "INSERT INTO rule_policy_binding "
            "(id, tenant_id, rule_config_id, policy_family_id, policy_document_id, "
            "policy_clause_id, quote_start, quote_end, quote, quote_sha256, "
            "clause_text_sha256, citation_order, binding_fingerprint, created_by) VALUES "
            "(:id, :tenant_id, :rule_id, :family_id, :document_id, :clause_id, "
            "0, 5, 'Exact', :quote_hash, :clause_hash, 1, :fingerprint, :user_id)",
            {
                "id": uuid.uuid4(),
                "tenant_id": tenant_a,
                "rule_id": rule_b,
                "family_id": family_a,
                "document_id": document_a,
                "clause_id": clause_id,
                "quote_hash": HASH_C,
                "clause_hash": HASH_B,
                "fingerprint": HASH_A,
                "user_id": user_a,
            },
        )
        await _expect_integrity_error(
            conn,
            "DELETE FROM policy_index_generation WHERE id = :id",
            {"id": generation_a},
        )


async def test_protected_idempotency_and_audit_invariants_survive_0005(
    engine: AsyncEngine, clean_db: None
) -> None:
    del clean_db
    async with engine.begin() as conn:
        tenant_id, user_id = await _seed_actor(conn, "protected")
        file_id = uuid.uuid4()
        await conn.execute(
            text(
                "INSERT INTO file_version "
                "(id, tenant_id, filename, content_hash, uploaded_by) "
                "VALUES (:id, :tenant_id, 'protected.xlsx', :hash, :user_id)"
            ),
            {"id": file_id, "tenant_id": tenant_id, "hash": HASH_A, "user_id": user_id},
        )
        row_result = (
            "INSERT INTO row_result "
            "(id, tenant_id, file_version_id, row_no, verdict, rule_version) "
            "VALUES (:id, :tenant_id, :file_id, 2, 'passed', :rule_version)"
        )
        await conn.execute(
            text(row_result),
            {
                "id": uuid.uuid4(),
                "tenant_id": tenant_id,
                "file_id": file_id,
                "rule_version": HASH_B,
            },
        )
        await _expect_integrity_error(
            conn,
            row_result,
            {
                "id": uuid.uuid4(),
                "tenant_id": tenant_id,
                "file_id": file_id,
                "rule_version": HASH_C,
            },
        )
        sampling = (
            "INSERT INTO sampling_audit (id, tenant_id, file_version_id, row_no) "
            "VALUES (:id, :tenant_id, :file_id, 2)"
        )
        await conn.execute(
            text(sampling),
            {"id": uuid.uuid4(), "tenant_id": tenant_id, "file_id": file_id},
        )
        await _expect_integrity_error(
            conn,
            sampling,
            {"id": uuid.uuid4(), "tenant_id": tenant_id, "file_id": file_id},
        )
        audit_id = uuid.uuid4()
        await conn.execute(
            text(
                "INSERT INTO audit_log (id, tenant_id, actor_id, action) "
                "VALUES (:id, :tenant_id, :actor_id, 'f4.protected')"
            ),
            {"id": audit_id, "tenant_id": tenant_id, "actor_id": user_id},
        )
        with pytest.raises(DBAPIError):
            async with conn.begin_nested():
                await conn.execute(
                    text("UPDATE audit_log SET action = 'tampered' WHERE id = :id"),
                    {"id": audit_id},
                )


def _migration_config(url: str) -> Config:
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "app" / "db" / "migrations"))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


def _seed_legacy_policy(conn: Connection) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, str, str]:
    tenant_id, user_id, document_id, clause_id, finding_id, file_id = (
        uuid.uuid4() for _ in range(6)
    )
    quote = "legacy exact"
    reasoning = "legacy reasoning"
    conn.execute(
        text("INSERT INTO tenant (id, slug, name) VALUES (:id, 'legacy-f4', 'Legacy F4')"),
        {"id": tenant_id},
    )
    conn.execute(
        text(
            "INSERT INTO app_user "
            "(id, tenant_id, username, password_hash, role, is_active) "
            "VALUES (:id, :tenant_id, 'legacy-f4', 'test', 'auditor', true)"
        ),
        {"id": user_id, "tenant_id": tenant_id},
    )
    conn.execute(
        text(
            "INSERT INTO file_version "
            "(id, tenant_id, filename, content_hash, uploaded_by) "
            "VALUES (:id, :tenant_id, 'legacy.xlsx', :hash, :user_id)"
        ),
        {"id": file_id, "tenant_id": tenant_id, "hash": HASH_A, "user_id": user_id},
    )
    conn.execute(
        text(
            "INSERT INTO policy_document "
            "(id, tenant_id, title, version, effective_date, source_filename) "
            "VALUES (:id, :tenant_id, 'Legacy Policy', 'legacy-v1', "
            "DATE '2025-01-01', 'legacy.txt')"
        ),
        {"id": document_id, "tenant_id": tenant_id},
    )
    conn.execute(
        text(
            "INSERT INTO policy_clause "
            "(id, tenant_id, document_id, clause_no, text) "
            "VALUES (:id, :tenant_id, :document_id, 'L1', 'legacy exact source')"
        ),
        {"id": clause_id, "tenant_id": tenant_id, "document_id": document_id},
    )
    conn.execute(
        text(
            "INSERT INTO finding "
            "(id, tenant_id, file_version_id, row_no, kind, severity_impact, "
            "severity_confidence, clause_id, quote, reasoning) VALUES "
            "(:id, :tenant_id, :file_id, 2, 'legacy', 0, 0, :clause_id, :quote, :reasoning)"
        ),
        {
            "id": finding_id,
            "tenant_id": tenant_id,
            "file_id": file_id,
            "clause_id": clause_id,
            "quote": quote,
            "reasoning": reasoning,
        },
    )
    return document_id, clause_id, finding_id, quote, reasoning


def test_legacy_upgrade_round_trip_preflight_and_safe_downgrade(db_url: str) -> None:
    database_name = f"expenseguard_f41_{uuid.uuid4().hex[:12]}"
    source_url = make_url(db_url)
    admin_url = source_url.set(database="postgres")
    temporary_url = source_url.set(database=database_name)
    admin_engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    temporary_engine = None

    with admin_engine.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{database_name}"'))

    try:
        cfg = _migration_config(temporary_url.render_as_string(hide_password=False))
        command.upgrade(cfg, "0004")
        temporary_engine = create_engine(temporary_url)
        with temporary_engine.begin() as conn:
            document_id, clause_id, finding_id, quote, reasoning = _seed_legacy_policy(conn)

        command.upgrade(cfg, "0005")
        with temporary_engine.connect() as conn:
            document = conn.execute(
                text(
                    "SELECT status, family_id, source_blob_id, content_sha256, parser_version, "
                    "chunker_version, created_by FROM policy_document WHERE id = :id"
                ),
                {"id": document_id},
            ).one()
            clause = conn.execute(
                text(
                    "SELECT family_id, ordinal, text_sha256, source_locator_json, "
                    "source_start, source_end FROM policy_clause WHERE id = :id"
                ),
                {"id": clause_id},
            ).one()
            finding = conn.execute(
                text("SELECT clause_id, quote, reasoning FROM finding WHERE id = :id"),
                {"id": finding_id},
            ).one()
        assert document == ("legacy_unpublished", None, None, None, None, None, None)
        assert clause == (None, None, None, None, None, None)
        assert finding == (clause_id, quote, reasoning)

        command.downgrade(cfg, "0004")
        command.upgrade(cfg, "0005")
        with temporary_engine.connect() as conn:
            assert (
                conn.scalar(
                    text("SELECT status FROM policy_document WHERE id = :id"),
                    {"id": document_id},
                )
                == "legacy_unpublished"
            )

        command.downgrade(cfg, "0004")
        with temporary_engine.begin() as conn:
            other_tenant = uuid.uuid4()
            conn.execute(
                text(
                    "INSERT INTO tenant (id, slug, name) "
                    "VALUES (:id, 'legacy-mismatch', 'Mismatch')"
                ),
                {"id": other_tenant},
            )
            conn.execute(
                text("UPDATE policy_clause SET tenant_id = :tenant_id WHERE id = :id"),
                {"tenant_id": other_tenant, "id": clause_id},
            )
        with pytest.raises(RuntimeError, match="legacy policy tenant mismatch"):
            command.upgrade(cfg, "0005")
        with temporary_engine.connect() as conn:
            assert conn.scalar(text("SELECT version_num FROM alembic_version")) == "0004"
            assert (
                conn.scalar(
                    text(
                        "SELECT NOT EXISTS (SELECT 1 FROM information_schema.columns "
                        "WHERE table_name = 'policy_document' AND column_name = 'status')"
                    )
                )
                is True
            )
        with temporary_engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE policy_clause SET tenant_id = "
                    "(SELECT tenant_id FROM policy_document WHERE id = :document_id) "
                    "WHERE id = :clause_id"
                ),
                {"document_id": document_id, "clause_id": clause_id},
            )
        command.upgrade(cfg, "0005")

        with temporary_engine.begin() as conn:
            tenant_id, user_id = conn.execute(
                text("SELECT tenant_id, uploaded_by FROM file_version LIMIT 1")
            ).one()
            family_id = uuid.uuid4()
            blob_id = uuid.uuid4()
            conn.execute(
                text(
                    "INSERT INTO policy_family "
                    "(id, tenant_id, stable_key, display_name, created_by) "
                    "VALUES (:id, :tenant_id, 'guard-family', 'Guard', :user_id)"
                ),
                {"id": family_id, "tenant_id": tenant_id, "user_id": user_id},
            )
            conn.execute(
                text(
                    "INSERT INTO policy_source_blob "
                    "(id, tenant_id, storage_key, mime_type, size_bytes, "
                    "content_sha256, created_by) "
                    "VALUES (:id, :tenant_id, 'guard.txt', 'text/plain', 10, :hash, :user_id)"
                ),
                {
                    "id": blob_id,
                    "tenant_id": tenant_id,
                    "hash": HASH_B,
                    "user_id": user_id,
                },
            )
            conn.execute(
                text(
                    "INSERT INTO policy_document "
                    "(id, tenant_id, title, version, effective_date, family_id, source_blob_id, "
                    "content_sha256, mime_type, size_bytes, extracted_text_sha256, parser_version, "
                    "chunker_version, status, created_by, published_by, published_at) VALUES "
                    "(:id, :tenant_id, 'Guard Policy', '1', DATE '2030-01-01', :family_id, "
                    ":blob_id, :hash, 'text/plain', 10, :hash, 'parser-v1', 'chunker-v1', "
                    "'published', :user_id, :user_id, now())"
                ),
                {
                    "id": uuid.uuid4(),
                    "tenant_id": tenant_id,
                    "family_id": family_id,
                    "blob_id": blob_id,
                    "hash": HASH_B,
                    "user_id": user_id,
                },
            )
        with pytest.raises(RuntimeError, match="cannot downgrade 0005"):
            command.downgrade(cfg, "0004")
        with temporary_engine.connect() as conn:
            assert conn.scalar(text("SELECT version_num FROM alembic_version")) == "0005"
            assert (
                conn.scalar(text("SELECT count(*) FROM policy_document WHERE status = 'published'"))
                == 1
            )
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
