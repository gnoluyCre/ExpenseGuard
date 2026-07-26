"""foundation tier A tables

地基层（全保真）:tenant / app_user / user_session / file_version /
expense_row / row_result / audit_log。

⚠️ 本迁移建立的唯一约束属于 AGENTS.md 的**受保护区域**。
后续 feature 迁移只允许 ADD COLUMN / ADD INDEX，
**不得 DROP 或放宽本文件中的任何 UNIQUE / CHECK 约束。**

其中最关键的两条:
  - row_result 的 unique(file_version_id, row_no) —— 行级幂等的基石。
    没有它，LangGraph 节点重放会导致同一行副作用重复执行。
  - audit_log 的追加写触发器 —— 把「不可静默修改」从代码纪律
    变成数据库不变式，且可被测试直接断言。

Revision ID: 0001
Revises:
Create Date: 2026-07-27

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """建立地基层 schema。"""

    # ------------------------------------------------------------------
    # LangGraph checkpoint 的独立 schema
    #
    # 表本身由 PostgresSaver.setup() 创建，不归 Alembic 管；
    # 但 schema 是基础设施，由迁移创建才有确定的存在时机。
    # 这是 Alembic 与 checkpoint 表隔离三层防御中的第一层。
    # ------------------------------------------------------------------
    op.execute("CREATE SCHEMA IF NOT EXISTS langgraph")

    # ------------------------------------------------------------------
    # tenant —— MVP 单租户运行，但隔离方案在架构层内建
    # ------------------------------------------------------------------
    op.create_table(
        "tenant",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("slug", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tenant")),
        sa.UniqueConstraint("slug", name=op.f("uq_tenant_slug")),
    )

    # ------------------------------------------------------------------
    # app_user —— 表名不用 user:那是 PostgreSQL 保留字
    # ------------------------------------------------------------------
    op.create_table(
        "app_user",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("username", sa.String(length=128), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column(
            "role",
            sa.Enum("auditor", "configurator", "viewer", name="role_enum", native_enum=False),
            nullable=False,
        ),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenant.id"],
            name=op.f("fk_app_user_tenant_id_tenant"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_app_user")),
        sa.UniqueConstraint("tenant_id", "username", name=op.f("uq_app_user_tenant_id_username")),
    )
    op.create_index(op.f("ix_app_user_tenant_id"), "app_user", ["tenant_id"], unique=False)

    # ------------------------------------------------------------------
    # user_session —— session 存 PostgreSQL，不引入 Redis。
    # token_hash 存 sha256 摘要，明文 token 只出现在 Set-Cookie 里。
    # ------------------------------------------------------------------
    op.create_table(
        "user_session",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.LargeBinary(length=32), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "last_seen_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenant.id"],
            name=op.f("fk_user_session_tenant_id_tenant"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["app_user.id"],
            name=op.f("fk_user_session_user_id_app_user"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_user_session")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_user_session_token_hash")),
    )
    op.create_index("ix_user_session_expires_at", "user_session", ["expires_at"], unique=False)
    op.create_index("ix_user_session_user_id", "user_session", ["user_id"], unique=False)
    op.create_index(op.f("ix_user_session_tenant_id"), "user_session", ["tenant_id"], unique=False)

    # ------------------------------------------------------------------
    # file_version —— 一次 Excel 导入
    #
    # 两条唯一约束各有其用:
    #   uq(tenant_id, content_hash) —— 内容哈希去重，同文件重复上传复用批次
    #   uq(id, tenant_id)           —— 冗余约束，供子表的复合外键引用
    # ------------------------------------------------------------------
    op.create_table(
        "file_version",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("filename", sa.String(length=512), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("row_count", sa.Integer(), nullable=True),
        sa.Column("uploaded_by", sa.Uuid(), nullable=False),
        sa.Column(
            "uploaded_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenant.id"],
            name=op.f("fk_file_version_tenant_id_tenant"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["uploaded_by"],
            ["app_user.id"],
            name=op.f("fk_file_version_uploaded_by_app_user"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_file_version")),
        sa.UniqueConstraint("id", "tenant_id", name="uq_file_version_id_tenant_id"),
        sa.UniqueConstraint(
            "tenant_id", "content_hash", name=op.f("uq_file_version_tenant_id_content_hash")
        ),
    )
    op.create_index(op.f("ix_file_version_tenant_id"), "file_version", ["tenant_id"], unique=False)

    # ------------------------------------------------------------------
    # expense_row —— 解析后的单行记录
    #
    # Phase 1 只落 raw_json。类型化列（金额/日期/发票号/商户）由 F2 加列补——
    # 那时才知道要哪些字段。这是「骨架 vs 细节」分层的样板。
    #
    # parse_error 非空即表示该行进了错误清单:PRD 硬性要求解析失败不得静默丢弃。
    # ------------------------------------------------------------------
    op.create_table(
        "expense_row",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("file_version_id", sa.Uuid(), nullable=False),
        sa.Column("row_no", sa.Integer(), nullable=False),
        sa.Column("raw_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("parse_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        # 复合外键:把「冗余 tenant_id 可能漂移」从代码纪律变成数据库不变式
        sa.ForeignKeyConstraint(
            ["file_version_id", "tenant_id"],
            ["file_version.id", "file_version.tenant_id"],
            name=op.f("fk_expense_row_file_version_id_tenant_id_file_version"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenant.id"],
            name=op.f("fk_expense_row_tenant_id_tenant"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_expense_row")),
        sa.UniqueConstraint(
            "file_version_id", "row_no", name=op.f("uq_expense_row_file_version_id_row_no")
        ),
    )
    op.create_index(
        op.f("ix_expense_row_file_version_id"), "expense_row", ["file_version_id"], unique=False
    )
    op.create_index(op.f("ix_expense_row_tenant_id"), "expense_row", ["tenant_id"], unique=False)

    # ==================================================================
    # row_result —— 幂等核心表
    #
    # ⚠️⚠️ uq(file_version_id, row_no) 是本项目最高优先级的数据库约束。
    #
    # 唯一键是业务键 (file_version_id, row_no) 而非自增 id，
    # 因此即使 LangGraph 节点重放、并发写入、或进程崩溃后重启，
    # 同一行的副作用最多发生一次。
    #
    # 恢复语义:
    #   命中 → 直接返回已有结果，不重复执行任何副作用
    #   未命中 → 执行 → INSERT ... ON CONFLICT DO NOTHING
    #
    # 反向验证:把这条约束注释掉，tests/integration/test_idempotency.py
    # 的用例 ① ② 必须变红。若仍然通过，说明测试测的不是这个约束。
    # ==================================================================
    op.create_table(
        "row_result",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("file_version_id", sa.Uuid(), nullable=False),
        sa.Column("row_no", sa.Integer(), nullable=False),
        sa.Column("verdict", sa.String(length=64), nullable=False),
        # 可复现性的锚点:相同输入 + 相同规则版本 → 相同输出
        sa.Column("rule_version", sa.String(length=64), nullable=False),
        sa.Column(
            "computed_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["file_version_id", "tenant_id"],
            ["file_version.id", "file_version.tenant_id"],
            name=op.f("fk_row_result_file_version_id_tenant_id_file_version"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenant.id"],
            name=op.f("fk_row_result_tenant_id_tenant"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_row_result")),
        # ↓↓↓ 幂等基石 ↓↓↓
        sa.UniqueConstraint(
            "file_version_id", "row_no", name=op.f("uq_row_result_file_version_id_row_no")
        ),
    )
    op.create_index(
        "ix_row_result_file_version_id", "row_result", ["file_version_id"], unique=False
    )
    op.create_index(op.f("ix_row_result_tenant_id"), "row_result", ["tenant_id"], unique=False)

    # ------------------------------------------------------------------
    # audit_log —— 追加写
    # ------------------------------------------------------------------
    op.create_table(
        "audit_log",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("actor_id", sa.Uuid(), nullable=True),
        sa.Column("action", sa.String(length=128), nullable=False),
        sa.Column("target_type", sa.String(length=64), nullable=True),
        sa.Column("target_id", sa.String(length=64), nullable=True),
        sa.Column("payload_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["actor_id"],
            ["app_user.id"],
            name=op.f("fk_audit_log_actor_id_app_user"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenant.id"],
            name=op.f("fk_audit_log_tenant_id_tenant"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_log")),
    )
    op.create_index("ix_audit_log_tenant_id_at", "audit_log", ["tenant_id", "at"], unique=False)
    op.create_index(op.f("ix_audit_log_tenant_id"), "audit_log", ["tenant_id"], unique=False)

    # ==================================================================
    # 追加写触发器
    #
    # 把「审计日志不可静默修改」从代码纪律变成**数据库不变式**。
    # 好处有二:
    #   1. 绕过 ORM 的手写 SQL 同样受约束
    #   2. 可被测试直接断言（test_audit_log_is_append_only）
    # ==================================================================
    op.execute(
        """
        CREATE OR REPLACE FUNCTION audit_log_append_only() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'audit_log is append-only: % is not permitted', TG_OP;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_audit_log_append_only
        BEFORE UPDATE OR DELETE ON audit_log
        FOR EACH ROW EXECUTE FUNCTION audit_log_append_only();
        """
    )


def downgrade() -> None:
    """回退地基层 schema。"""
    op.execute("DROP TRIGGER IF EXISTS trg_audit_log_append_only ON audit_log")
    op.execute("DROP FUNCTION IF EXISTS audit_log_append_only()")

    op.drop_table("audit_log")
    op.drop_table("row_result")
    op.drop_table("expense_row")
    op.drop_table("file_version")
    op.drop_table("user_session")
    op.drop_table("app_user")
    op.drop_table("tenant")

    # ⚠️ 刻意**不**执行 DROP SCHEMA langgraph。
    #
    # 那个 schema 里装的是 LangGraph 的 checkpoint 数据，不归本迁移管。
    # 一次 downgrade 就把断点数据全部抹掉，是不可接受的副作用——
    # 尤其考虑到 downgrade 常常发生在「回滚一次失败发布」这种
    # 最不希望丢数据的时刻。
    #
    # 留下一个空 schema 的代价为零。
