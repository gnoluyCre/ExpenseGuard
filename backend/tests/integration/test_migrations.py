"""迁移产出的 schema 校验。

这些测试直接查 PostgreSQL 的系统目录（pg_constraint / information_schema），
**不看 SQLAlchemy 的元数据**。区别很重要:元数据反映的是「模型怎么写的」，
系统目录反映的是「数据库里实际有什么」。受保护区域的约束必须在后者存在，
所以断言必须打在后者上。

它们是受保护区域的守门员:任何未来削弱这些约束的迁移都会让它们变红。
"""

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = pytest.mark.integration

#: 迁移应建立的全部业务表
EXPECTED_TABLES = {
    "app_user",
    "audit_log",
    "capability_declaration",
    "correlation_finding",
    "evidence_step",
    "expense_row",
    "field_availability",
    "file_version",
    "finding",
    "policy_clause",
    "policy_document",
    "review",
    "review_plan_request",
    "review_sampling_config",
    "review_sampling_plan",
    "row_result",
    "rule_config",
    "sampling_audit",
    "sampling_review",
    "schema_mapping",
    "schema_mapping_version",
    "tenant",
    "user_session",
    "validation_dependency",
    "validation_run",
}


async def test_all_business_tables_exist(engine: AsyncEngine) -> None:
    """各阶段已交付的业务表齐备。"""
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"
            )
        )
        actual = {r[0] for r in rows}

    missing = EXPECTED_TABLES - actual
    assert not missing, f"迁移漏建了这些表: {sorted(missing)}"


async def test_row_result_idempotency_constraint_exists(engine: AsyncEngine) -> None:
    """⚠️ 幂等基石:row_result 的 unique(file_version_id, row_no) 必须存在。

    这是本项目最高优先级的数据库约束。没有它，LangGraph 节点重放
    会导致同一行的副作用重复执行，而数据污染极难事后察觉。

    直接查 pg_constraint 而非 ORM 元数据 —— 断言的是数据库的真实状态。
    """
    async with engine.connect() as conn:
        row = await conn.execute(
            text(
                """
                SELECT c.contype, array_agg(a.attname ORDER BY a.attname) AS cols
                FROM pg_constraint c
                JOIN pg_class t ON t.oid = c.conrelid
                JOIN unnest(c.conkey) AS k(attnum) ON TRUE
                JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = k.attnum
                WHERE t.relname = 'row_result'
                  AND c.conname = 'uq_row_result_file_version_id_row_no'
                GROUP BY c.contype
                """
            )
        )
        result = row.one_or_none()

    assert result is not None, (
        "row_result 上找不到 uq_row_result_file_version_id_row_no —— "
        "行级幂等的基石缺失。这是 AGENTS.md 受保护区域的约束，不得删除或改名。"
    )
    contype, cols = result
    assert contype == "u", f"约束类型应为 UNIQUE，实际是 {contype!r}"
    assert set(cols) == {"file_version_id", "row_no"}, f"约束列不对: {cols}"


@pytest.mark.parametrize(
    ("table", "constraint"),
    [
        # 冗余唯一约束:供子表的复合外键引用
        ("file_version", "uq_file_version_id_tenant_id"),
        # 防重复复核:复核结论是回流评测集唯一的真实标签来源
        ("review", "uq_review_finding_id"),
        # 规则版本化:「相同输入 + 相同规则版本 → 相同输出」的前提
        ("rule_config", "uq_rule_config_tenant_id_rule_id_version"),
        # ReAct 循环同样会被重放
        ("evidence_step", "uq_evidence_step_finding_id_step_no"),
        # 被放行样本抽检:漏放率唯一的可测来源
        ("sampling_audit", "uq_sampling_audit_file_version_id_row_no"),
        # F2 必须保留 0002 的映射骨架唯一约束。
        ("schema_mapping", "uq_schema_mapping_tenant_id_source_column_version"),
    ],
)
async def test_protected_unique_constraints_exist(
    engine: AsyncEngine, table: str, constraint: str
) -> None:
    """受保护区域的其余唯一约束逐一确认存在。"""
    async with engine.connect() as conn:
        exists = await conn.scalar(
            text(
                """
                SELECT EXISTS (
                    SELECT 1 FROM pg_constraint c
                    JOIN pg_class t ON t.oid = c.conrelid
                    WHERE t.relname = :table AND c.conname = :constraint AND c.contype = 'u'
                )
                """
            ),
            {"table": table, "constraint": constraint},
        )
    assert exists, f"{table} 上缺少受保护的唯一约束 {constraint}"


async def test_f2_schema_parsing_columns_exist(engine: AsyncEngine) -> None:
    """F2 物化列必须由 0003 建立，而不是只存在于 ORM metadata。"""
    expected = {
        "file_version": {"mapping_version_id", "parse_status", "parsed_at"},
        "expense_row": {"normalized_json", "parse_error_code", "parse_error_detail"},
        "schema_mapping": {"mapping_version_id"},
    }
    async with engine.connect() as conn:
        for table, required_columns in expected.items():
            rows = await conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND table_name = :table"
                ),
                {"table": table},
            )
            actual = {row[0] for row in rows}
            assert required_columns <= actual, (
                f"{table} 缺少 F2 列: {sorted(required_columns - actual)}"
            )


@pytest.mark.parametrize(
    ("table", "constraint", "constraint_type"),
    [
        (
            "schema_mapping_version",
            "uq_schema_mapping_version_tenant_id_version",
            "u",
        ),
        (
            "schema_mapping_version",
            "uq_schema_mapping_version_id_tenant_id",
            "u",
        ),
        (
            "schema_mapping",
            "uq_schema_mapping_mapping_version_id_source_column",
            "u",
        ),
        (
            "schema_mapping",
            "uq_schema_mapping_mapping_version_id_target_field",
            "u",
        ),
        (
            "schema_mapping",
            "fk_schema_mapping_mapping_version_tenant",
            "f",
        ),
        (
            "file_version",
            "fk_file_version_mapping_version_tenant",
            "f",
        ),
        ("file_version", "ck_file_version_parse_status_values", "c"),
    ],
)
async def test_f2_schema_parsing_constraints_exist(
    engine: AsyncEngine,
    table: str,
    constraint: str,
    constraint_type: str,
) -> None:
    """F2 的版本、租户一致性和状态域约束必须落在 PostgreSQL。"""
    async with engine.connect() as conn:
        exists = await conn.scalar(
            text(
                """
                SELECT EXISTS (
                    SELECT 1 FROM pg_constraint c
                    JOIN pg_class t ON t.oid = c.conrelid
                    WHERE t.relname = :table
                      AND c.conname = :constraint
                      AND c.contype = :constraint_type
                )
                """
            ),
            {
                "table": table,
                "constraint": constraint,
                "constraint_type": constraint_type,
            },
        )
    assert exists, f"{table} 缺少 F2 约束 {constraint}"


async def test_finding_has_two_severity_dimensions(engine: AsyncEngine) -> None:
    """二维分级的两个维度必须分列存储，且各带 0..3 的 CHECK。

    规格明写二维分级。事后把单列拆成两列需要迁移数据，
    所以这个结构必须在地基阶段就对。
    """
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'finding' AND column_name LIKE 'severity%'"
            )
        )
        cols = {r[0] for r in rows}

        checks = await conn.execute(
            text(
                "SELECT conname FROM pg_constraint c JOIN pg_class t ON t.oid = c.conrelid "
                "WHERE t.relname = 'finding' AND c.contype = 'c'"
            )
        )
        check_names = {r[0] for r in checks}

    assert cols == {"severity_impact", "severity_confidence"}, f"severity 列不对: {cols}"
    assert "ck_finding_severity_impact_range" in check_names
    assert "ck_finding_severity_confidence_range" in check_names


async def test_langgraph_schema_exists(engine: AsyncEngine) -> None:
    """langgraph schema 必须由迁移创建。

    它是 checkpoint 表隔离三层防御的第一层:checkpointer 的连接串
    带 `search_path=langgraph`，若 schema 不存在，`setup()` 建的表
    会落回 public，进而被 Alembic 的 autogenerate 当成多余表删掉。

    这里**只断言 schema 存在，不断言它是空的**。里面有没有表取决于
    本轮是否跑过 `checkpointer.setup()`（`test_recovery.py` 会跑），
    断言「空」会制造测试间的执行顺序依赖——那种测试在单独跑时绿、
    全量跑时红，是最难排查的一类脆弱测试。

    checkpoint 表的落位由 `test_recovery.py::test_checkpoint_表落在_langgraph_schema`
    负责断言，那里才有明确的前置条件。
    """
    async with engine.connect() as conn:
        exists = await conn.scalar(
            text("SELECT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = 'langgraph')")
        )

    assert exists, "迁移应创建 langgraph schema（checkpoint 表隔离的第一层）"


async def test_all_business_tables_have_tenant_id(engine: AsyncEngine) -> None:
    """所有业务表都带 tenant_id。

    这不只是数据模型的整齐:`core.tenancy.scope` 的自动租户过滤
    靠 `with_loader_criteria` 挂在带 tenant_id 的模型上——
    没有这一列，那张表的查询就不会被过滤，成为跨租户泄漏的缺口。
    """
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(
                """
                SELECT t.table_name
                FROM information_schema.tables t
                WHERE t.table_schema = 'public'
                  AND t.table_type = 'BASE TABLE'
                  AND t.table_name <> 'alembic_version'
                  AND t.table_name <> 'tenant'
                  AND NOT EXISTS (
                      SELECT 1 FROM information_schema.columns c
                      WHERE c.table_schema = 'public'
                        AND c.table_name = t.table_name
                        AND c.column_name = 'tenant_id'
                  )
                """
            )
        )
        missing = [r[0] for r in rows]

    assert not missing, f"这些业务表缺少 tenant_id，会绕过租户过滤: {missing}"
