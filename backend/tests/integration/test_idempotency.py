"""行级幂等测试 —— 项目 #1 优先级。

全部用 `clean_db` 夹具（真提交），**不用 `db_session`（rollback）**。
理由见 `tests/conftest.py` 的模块 docstring:rollback 掉的东西从未提交过，
而幂等要验证的正是「跨事务、跨进程的已提交副作用最多发生一次」。

## 「假节点」的设计

Phase 1 还没有任何业务判定逻辑（F3 才是第一个调用方），
但幂等语义已经可以被完整验证——因为 `compute` 是**注入**的。

测试注入的假节点同时做两件事:
  1. 递增一个 Python 计数器 → 观测 compute 被调用了几次
  2. 向 audit_log 插一条标记行 → 观测**数据库副作用**发生了几次

第二点是关键:它让副作用的可观测性完全不依赖任何业务功能。

## 反向验证（已实测，2026-07-27）

摘掉约束::

    ALTER TABLE row_result DROP CONSTRAINT uq_row_result_file_version_id_row_no;

结果:本文件 5 个测试中**有 4 个变红**，报错为::

    psycopg.errors.InvalidColumnReference:
    there is no unique or exclusion constraint matching the ON CONFLICT specification

这正是想要的结论 —— 测试依赖的是**数据库约束**，不是 Python 逻辑。
（第 5 个测试只碰 audit_log，不涉及 row_result，正确地保持通过。）

值得一提的是失败模式:缺约束时 `ON CONFLICT` 直接是 SQL 层硬错误，
而不是「静默产生两行重复数据」。硬错误比静默降级好得多——
后者才是审计系统里真正危险的失败形态。

恢复::

    ALTER TABLE row_result ADD CONSTRAINT uq_row_result_file_version_id_row_no
        UNIQUE (file_version_id, row_no);
"""

import asyncio
import uuid
from collections.abc import Callable

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.core.orchestration.idempotency import process_row_once, upsert_row_result
from app.core.tenancy.scope import bind_tenant
from app.db.models.audit import AuditLog
from app.db.models.batch import FileVersion, RowResult
from app.db.models.tenancy import AppUser, Role, Tenant

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("clean_db")]

RULE_VERSION = "v1"
#: 假节点写入 audit_log 的动作名，用于统计数据库副作用次数
SIDE_EFFECT_ACTION = "test.compute_side_effect"


class FakeNode:
    """假的行处理节点。

    同时产生一个 Python 侧计数和一个数据库侧副作用，
    使「compute 执行了几次」与「副作用落库了几次」可以分别断言——
    这两个数字在节点重放场景下会不相等，而那正是要证明的事。
    """

    def __init__(self, session: AsyncSession, tenant_id: uuid.UUID) -> None:
        self.session = session
        self.tenant_id = tenant_id
        self.calls = 0

    def for_row(self, row_no: int, *, fail: bool = False) -> Callable[[], object]:
        async def compute() -> tuple[str, str]:
            self.calls += 1
            # 数据库副作用，与 row_result 的 INSERT 在同一事务里
            self.session.add(
                AuditLog(
                    tenant_id=self.tenant_id,
                    action=SIDE_EFFECT_ACTION,
                    target_type="row",
                    target_id=str(row_no),
                )
            )
            await self.session.flush()
            if fail:
                raise RuntimeError(f"第 {row_no} 行处理失败（测试构造）")
            return ("ok", RULE_VERSION)

        return compute


async def _seed(session: AsyncSession) -> tuple[uuid.UUID, uuid.UUID]:
    """建一个租户 + 用户 + 批次，返回 (tenant_id, file_version_id)。"""
    tenant = Tenant(slug="t-idem", name="幂等测试租户")
    session.add(tenant)
    await session.flush()

    user = AppUser(
        tenant_id=tenant.id,
        username="tester",
        password_hash="x",
        role=Role.AUDITOR,
        is_active=True,
    )
    session.add(user)
    await session.flush()

    fv = FileVersion(
        tenant_id=tenant.id,
        filename="batch.xlsx",
        content_hash="h" * 64,
        uploaded_by=user.id,
    )
    session.add(fv)
    await session.commit()
    return tenant.id, fv.id


async def _count_row_results(session: AsyncSession, file_version_id: uuid.UUID) -> int:
    return (
        await session.scalar(
            select(func.count())
            .select_from(RowResult)
            .where(RowResult.file_version_id == file_version_id)
        )
    ) or 0


async def _count_side_effects(session: AsyncSession, row_no: int | None = None) -> int:
    stmt = select(func.count()).select_from(AuditLog).where(AuditLog.action == SIDE_EFFECT_ACTION)
    if row_no is not None:
        stmt = stmt.where(AuditLog.target_id == str(row_no))
    return (await session.scalar(stmt)) or 0


# ======================================================================
# ① 同键调用两次 → 一行、created=False、compute 只跑一次
# ======================================================================
async def test_同键调用两次只产生一行(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        tenant_id, fv_id = await _seed(session)
        bind_tenant(session.sync_session, tenant_id)

        node = FakeNode(session, tenant_id)

        first = await process_row_once(
            session,
            tenant_id=tenant_id,
            file_version_id=fv_id,
            row_no=1,
            compute=node.for_row(1),  # type: ignore[arg-type]
        )
        await session.commit()

        second = await process_row_once(
            session,
            tenant_id=tenant_id,
            file_version_id=fv_id,
            row_no=1,
            compute=node.for_row(1),  # type: ignore[arg-type]
        )
        await session.commit()

        assert first.created is True
        assert second.created is False, "第二次调用应命中已有结果"
        assert node.calls == 1, "compute 不应被第二次调用 —— 否则其内部副作用会重复发生"
        assert await _count_row_results(session, fv_id) == 1
        assert await _count_side_effects(session) == 1, "数据库副作用也只能发生一次"


# ======================================================================
# ② 并发抢同一个键 → 恰好一行，且调用方看不到 IntegrityError
# ======================================================================
async def test_并发抢同一个键只产生一行(
    engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as setup:
        tenant_id, fv_id = await _seed(setup)

    async def writer(verdict: str) -> bool:
        """用**独立连接**写入 —— 并发语义要求，不能共用一个会话。"""
        async with session_factory() as s:
            bind_tenant(s.sync_session, tenant_id)
            outcome = await upsert_row_result(
                s,
                tenant_id=tenant_id,
                file_version_id=fv_id,
                row_no=7,
                verdict=verdict,
                rule_version=RULE_VERSION,
            )
            await s.commit()
            return outcome.created

    # 若 upsert 用「先查后插」而非 ON CONFLICT，这里会抛 IntegrityError
    results = await asyncio.gather(writer("a"), writer("b"))

    assert sum(results) == 1, f"应恰好一个协程创建成功，实际 {results}"

    async with session_factory() as check:
        bind_tenant(check.sync_session, tenant_id)
        assert await _count_row_results(check, fv_id) == 1


# ======================================================================
# ③ 中途失败 → 该行在 row_result 与 audit_log 中均无痕迹
# ======================================================================
async def test_处理失败时副作用整体回滚(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        tenant_id, fv_id = await _seed(session)
        bind_tenant(session.sync_session, tenant_id)
        node = FakeNode(session, tenant_id)

        # 先成功处理 1..4
        for row_no in range(1, 5):
            await process_row_once(
                session,
                tenant_id=tenant_id,
                file_version_id=fv_id,
                row_no=row_no,
                compute=node.for_row(row_no),  # type: ignore[arg-type]
            )
        await session.commit()

        # 第 5 行失败
        with pytest.raises(RuntimeError):
            await process_row_once(
                session,
                tenant_id=tenant_id,
                file_version_id=fv_id,
                row_no=5,
                compute=node.for_row(5, fail=True),  # type: ignore[arg-type]
            )
        await session.rollback()

    async with session_factory() as check:
        bind_tenant(check.sync_session, tenant_id)
        assert await _count_row_results(check, fv_id) == 4
        assert await _count_side_effects(check, row_no=5) == 0, (
            "失败行的审计副作用必须随事务一起回滚 —— "
            "这正是「compute 的副作用必须与 INSERT 同事务」这条契约的意义"
        )


# ======================================================================
# ④ 失败后重跑整批 → 每行恰好一条，已完成行不再执行 compute
# ======================================================================
async def test_失败后重跑整批为恰好一次(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    total = 10

    async with session_factory() as session:
        tenant_id, fv_id = await _seed(session)
        bind_tenant(session.sync_session, tenant_id)
        node = FakeNode(session, tenant_id)

        for row_no in range(1, 5):
            await process_row_once(
                session,
                tenant_id=tenant_id,
                file_version_id=fv_id,
                row_no=row_no,
                compute=node.for_row(row_no),  # type: ignore[arg-type]
            )
        await session.commit()

        with pytest.raises(RuntimeError):
            await process_row_once(
                session,
                tenant_id=tenant_id,
                file_version_id=fv_id,
                row_no=5,
                compute=node.for_row(5, fail=True),  # type: ignore[arg-type]
            )
        await session.rollback()

    # —— 模拟「重启后从头重跑整批」——
    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        node2 = FakeNode(session, tenant_id)

        for row_no in range(1, total + 1):
            await process_row_once(
                session,
                tenant_id=tenant_id,
                file_version_id=fv_id,
                row_no=row_no,
                compute=node2.for_row(row_no),  # type: ignore[arg-type]
            )
        await session.commit()

        assert await _count_row_results(session, fv_id) == total
        # 1..4 已完成 → 重跑时跳过；5..10 需要执行 → 6 次
        assert node2.calls == total - 4, f"已完成行不应重新执行 compute，实际调用 {node2.calls} 次"

        for row_no in range(1, total + 1):
            assert await _count_side_effects(session, row_no=row_no) == 1, (
                f"第 {row_no} 行的副作用应恰好一次"
            )


# ======================================================================
# ⑤ 审计日志追加写 —— 由数据库触发器强制
# ======================================================================
async def test_审计日志不可修改也不可删除(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        tenant_id, _ = await _seed(session)
        bind_tenant(session.sync_session, tenant_id)
        entry = AuditLog(tenant_id=tenant_id, action="auth.login")
        session.add(entry)
        await session.commit()
        entry_id = entry.id

    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        from sqlalchemy import delete, update

        with pytest.raises(DBAPIError, match="append-only"):
            await session.execute(
                update(AuditLog).where(AuditLog.id == entry_id).values(action="tampered")
            )
        await session.rollback()

    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        from sqlalchemy import delete

        with pytest.raises(DBAPIError, match="append-only"):
            await session.execute(delete(AuditLog).where(AuditLog.id == entry_id))
        await session.rollback()

    async with session_factory() as session:
        bind_tenant(session.sync_session, tenant_id)
        survivor = await session.scalar(select(AuditLog).where(AuditLog.id == entry_id))
        assert survivor is not None
        assert survivor.action == "auth.login", "记录内容不应被改动"
