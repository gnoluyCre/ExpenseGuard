"""租户上下文 —— 一次请求内「我是谁、属于哪个租户」的载体。"""

import uuid

from pydantic import BaseModel, ConfigDict

from app.db.models.tenancy import Role


class TenantContext(BaseModel):
    """当前请求的租户与身份上下文。

    由 `app.api.deps.get_auth_context` 从会话解析得出，
    随后注入到数据库会话，驱动自动租户过滤。
    """

    model_config = ConfigDict(frozen=True)

    tenant_id: uuid.UUID
    user_id: uuid.UUID
    role: Role
    #: 本次请求所用的会话 ID。
    #:
    #: 带上它是为了让登出能精确撤销**这一个**会话。
    #: 若不带，登出就只能去猜（比如「该用户最近活跃的那个会话」），
    #: 在同一用户多端登录时会撤销错对象。
    session_id: uuid.UUID


#: 存放在 `Session.info` 里的键名。
#: 用 `Session.info` 而非全局变量 / ContextVar，是因为过滤器需要的是
#: 「这个会话属于哪个租户」，而不是「当前线程在处理谁的请求」——
#: 后者在 async 并发下极易串台。
SESSION_TENANT_KEY = "tenant_id"
