"""认证路由。"""

from fastapi import APIRouter, Response
from pydantic import BaseModel, Field

from app.api.deps import AuthDep, DbDep, SettingsDep
from app.core.security.auth_service import authenticate, write_audit
from app.core.security.permissions import ROLE_PERMISSIONS
from app.core.security.session_service import (
    SESSION_COOKIE_NAME,
    create_session,
    revoke_session,
)
from app.db.models.tenancy import Role

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginRequest(BaseModel):
    """登录请求。"""

    tenant_slug: str = Field(default="default", max_length=64)
    username: str = Field(max_length=128)
    password: str = Field(max_length=256)


class CurrentUser(BaseModel):
    """当前登录用户。"""

    user_id: str
    tenant_id: str
    role: Role
    #: 该角色的权限清单，前端据此决定菜单与按钮的可见性。
    #: 注意:这是**便利**而非安全边界——真正的鉴权在服务端每个端点上。
    permissions: list[str]


def _set_session_cookie(response: Response, token: str, settings: SettingsDep) -> None:
    """写会话 cookie。

    - `httponly`  JS 读不到，XSS 也偷不走
    - `samesite=lax`  挡掉绝大多数 CSRF，同时不影响正常的顶层导航
    - `secure`  prod 必须为 true；dev 走 http 故可配
    - `max_age` 与绝对过期时间对齐，浏览器侧同步清理
    """
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="lax",
        max_age=settings.session_absolute_timeout_seconds,
        path="/",
    )


@router.post("/login", response_model=CurrentUser, name="login")
async def login(
    payload: LoginRequest,
    response: Response,
    db: DbDep,
    settings: SettingsDep,
) -> CurrentUser:
    """登录并下发会话 cookie。"""
    user = await authenticate(
        db,
        tenant_slug=payload.tenant_slug,
        username=payload.username,
        password=payload.password,
    )
    token, _ = await create_session(db, user=user, settings=settings)
    _set_session_cookie(response, token, settings)

    return CurrentUser(
        user_id=str(user.id),
        tenant_id=str(user.tenant_id),
        role=user.role,
        permissions=sorted(p.value for p in ROLE_PERMISSIONS.get(user.role, frozenset())),
    )


@router.post("/logout", status_code=204, name="logout")
async def logout(
    response: Response,
    db: DbDep,
    auth: AuthDep,
    settings: SettingsDep,
) -> None:
    """登出。

    只撤销**本次请求所用的那一个**会话（`auth.session_id` 由认证依赖
    精确解析得出），因此同一用户在其它设备上的登录不受影响。

    服务端标记 `revoked_at`（UPDATE 而非 DELETE —— 留痕），
    同时清掉浏览器 cookie。
    """
    await revoke_session(db, session_id=auth.session_id)
    await write_audit(db, tenant_id=auth.tenant_id, action="auth.logout", actor_id=auth.user_id)
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")


@router.get("/me", response_model=CurrentUser, name="me")
async def me(auth: AuthDep) -> CurrentUser:
    """返回当前登录用户。前端用它做路由守卫。"""
    return CurrentUser(
        user_id=str(auth.user_id),
        tenant_id=str(auth.tenant_id),
        role=auth.role,
        permissions=sorted(p.value for p in ROLE_PERMISSIONS.get(auth.role, frozenset())),
    )
