"""行级幂等原语 —— 本项目最高优先级的工程资产。

## 要解决的问题

LangGraph 从中断恢复时，**整个节点会从头重新执行**。`interrupt()` 之前
发生过的副作用会再发生一次。这不是可以靠小心编码规避的边界情况，
而是编排框架的既定语义。

在报销审计场景中，重复副作用意味着重复的判定记录、重复的审计日志、
重复的抽检样本——而且这类数据污染极难事后察觉。

## 解决方式

把「这一行处理过了吗」的判断下沉到**数据库唯一约束**:
`row_result` 表上的 `unique(file_version_id, row_no)`。

    workflow thread_id = file_version_id
    每行处理前:SELECT row_result WHERE (file_version_id, row_no)
      命中 → 直接返回已有结果，compute 根本不执行
      未命中 → 执行 compute → INSERT ... ON CONFLICT DO NOTHING

唯一键是**业务键**而非自增 id，因此即使节点重放、并发写入、
或进程崩溃后重启，同一行的副作用最多发生一次。

## ⚠️ 语义契约（调用方必须遵守）

`compute()` 内部的所有**数据库副作用必须与 `row_result` 的 INSERT
处于同一事务**。二者原子提交，「至多一次」才成立。

**非事务性副作用不受此保护**——LLM 调用、外部 HTTP 请求、文件写入
都不会因为事务回滚而撤销。这类操作必须自身幂等，或者在执行前
先把「即将执行」的意图落库。

这条边界必须显式声明，否则调用方会误以为把任何东西塞进 `compute`
都自动获得了幂等保证。
"""

import uuid
from collections.abc import Awaitable, Callable

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.batch import RowResult


class RowOutcome(BaseModel):
    """单行处理的结果。

    同一个模型将来既作 API 响应模型，也可作 LLM 结构化输出的约束 schema，
    避免两套定义漂移。
    """

    model_config = ConfigDict(frozen=True)

    file_version_id: uuid.UUID
    row_no: int
    verdict: str
    rule_version: str
    #: True = 本次调用新建；False = 命中已有结果，未执行任何副作用
    created: bool


#: 计算单行判定的回调。返回 (verdict, rule_version)。
#:
#: 做成注入式而非写死，有两个好处:
#:   1. Phase 1 还没有任何业务判定逻辑（F3 才是第一个调用方），
#:      但幂等语义已经可以被完整测试——测试注入一个「假节点」即可
#:   2. 副作用的可观测性完全不依赖业务功能
ComputeFn = Callable[[], Awaitable[tuple[str, str]]]


async def fetch_row_result(
    session: AsyncSession,
    *,
    file_version_id: uuid.UUID,
    row_no: int,
) -> RowResult | None:
    """按业务键查已有结果。"""
    stmt = select(RowResult).where(
        RowResult.file_version_id == file_version_id,
        RowResult.row_no == row_no,
    )
    # 显式标注:AsyncSession.scalar 的返回类型是 Any，
    # 直接 return 会让 mypy strict 报 no-any-return
    result: RowResult | None = await session.scalar(stmt)
    return result


async def upsert_row_result(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    file_version_id: uuid.UUID,
    row_no: int,
    verdict: str,
    rule_version: str,
) -> RowOutcome:
    """写入行级结果，已存在则原样返回。

    用 `ON CONFLICT DO NOTHING` 而不是先查后插:后者在并发下有
    检查-使用竞态（两个协程同时查到「不存在」，然后都去插入，
    其中一个撞唯一约束抛 `IntegrityError`）。让数据库自己处理冲突，
    调用方永远不会看到 `IntegrityError`。
    """
    stmt = (
        pg_insert(RowResult)
        .values(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            file_version_id=file_version_id,
            row_no=row_no,
            verdict=verdict,
            rule_version=rule_version,
        )
        .on_conflict_do_nothing(index_elements=["file_version_id", "row_no"])
        .returning(RowResult.id)
    )
    inserted_id = await session.scalar(stmt)

    if inserted_id is not None:
        return RowOutcome(
            file_version_id=file_version_id,
            row_no=row_no,
            verdict=verdict,
            rule_version=rule_version,
            created=True,
        )

    # 冲突了 —— 说明别处已经写入。返回既有值，而不是本次算出来的值。
    existing = await fetch_row_result(session, file_version_id=file_version_id, row_no=row_no)
    if existing is None:  # pragma: no cover —— 唯一约束保证不会走到这里
        raise RuntimeError(
            f"ON CONFLICT 未插入但也查不到既有行 "
            f"(file_version_id={file_version_id}, row_no={row_no})"
        )
    return RowOutcome(
        file_version_id=existing.file_version_id,
        row_no=existing.row_no,
        verdict=existing.verdict,
        rule_version=existing.rule_version,
        created=False,
    )


async def process_row_once(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    file_version_id: uuid.UUID,
    row_no: int,
    compute: ComputeFn,
) -> RowOutcome:
    """处理一行，保证副作用至多发生一次。

    命中已有结果时 **`compute` 根本不会被调用** —— 这一点是幂等的关键:
    仅仅「不重复写 row_result」是不够的，`compute` 内部的其它数据库写入
    （审计日志、抽检样本、finding）同样不能重复发生。

    Args:
        session: 数据库会话。`compute` 的副作用必须用同一个会话，
            否则不在同一事务里，原子性不成立。
        tenant_id: 租户。
        file_version_id: 批次 ID，同时是 LangGraph 的 thread_id。
        row_no: Excel 行号。
        compute: 计算该行判定的回调，返回 (verdict, rule_version)。

    Returns:
        `created=True` 表示本次新建；`created=False` 表示跳过。
    """
    existing = await fetch_row_result(session, file_version_id=file_version_id, row_no=row_no)
    if existing is not None:
        # 提前返回 —— compute 不执行，其内部副作用一次都不会发生
        return RowOutcome(
            file_version_id=existing.file_version_id,
            row_no=existing.row_no,
            verdict=existing.verdict,
            rule_version=existing.rule_version,
            created=False,
        )

    verdict, rule_version = await compute()

    return await upsert_row_result(
        session,
        tenant_id=tenant_id,
        file_version_id=file_version_id,
        row_no=row_no,
        verdict=verdict,
        rule_version=rule_version,
    )
