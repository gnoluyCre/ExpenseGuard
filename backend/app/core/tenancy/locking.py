"""租户级并发串行化原语。"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import TenantScopeMissingError
from app.core.tenancy.scope import current_tenant
from app.db.models.tenancy import Tenant


async def lock_tenant_nowait(session: AsyncSession, tenant_id: uuid.UUID) -> Tenant:
    """立即获取租户父行锁，并校验会话租户上下文。

    PostgreSQL ``55P03`` 原样交给调用方，使不同领域服务可以映射为各自
    稳定的冲突错误码。Tenant 本身不是 tenant-scoped 模型，因此必须先
    显式核对 session 上下文，不能只依赖查询条件。
    """
    bound_tenant_id = current_tenant(session.sync_session)
    if bound_tenant_id is None:
        raise TenantScopeMissingError
    if bound_tenant_id != tenant_id:
        raise RuntimeError("租户锁请求与会话租户上下文不一致")

    tenant = await session.scalar(
        select(Tenant).where(Tenant.id == tenant_id).with_for_update(nowait=True)
    )
    if tenant is None:
        raise RuntimeError("会话绑定的租户不存在")
    return tenant
