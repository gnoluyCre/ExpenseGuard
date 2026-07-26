"""RBAC 权限矩阵。

**权限是数据，不是 if-else。** 把角色到权限的映射写成一个字典，
新增角色或调整权限只改这一处数据，不需要在散落各处的
`if role == "auditor"` 里追着改 —— 后者才是权限漏洞的温床。
"""

from enum import StrEnum

from app.db.models.tenancy import Role


class Permission(StrEnum):
    """细粒度权限。"""

    # —— 批次 ——
    BATCH_IMPORT = "batch:import"
    BATCH_READ = "batch:read"
    # —— 报告 ——
    REPORT_READ = "report:read"
    REPORT_EXPORT = "report:export"
    # —— 复核 ——
    REVIEW_READ = "review:read"
    REVIEW_SUBMIT = "review:submit"
    # —— 配置（规则阈值、schema 映射、制度文档）——
    CONFIG_READ = "config:read"
    CONFIG_WRITE = "config:write"


#: 角色 → 权限集合。
#:
#: 取自 TechDesign 的 RBAC 三角色定义:
#:   auditor      导入批次、查看报告、复核标记
#:   configurator 上述全部 + 规则配置、schema 映射、制度文档管理
#:   viewer       仅查看报告与汇总
_AUDITOR_PERMISSIONS = frozenset(
    {
        Permission.BATCH_IMPORT,
        Permission.BATCH_READ,
        Permission.REPORT_READ,
        Permission.REPORT_EXPORT,
        Permission.REVIEW_READ,
        Permission.REVIEW_SUBMIT,
        Permission.CONFIG_READ,
    }
)

ROLE_PERMISSIONS: dict[Role, frozenset[Permission]] = {
    Role.AUDITOR: _AUDITOR_PERMISSIONS,
    Role.CONFIGURATOR: _AUDITOR_PERMISSIONS | {Permission.CONFIG_WRITE},
    # viewer 是**只读负责人**:对内审/外审负责，看结论但不参与操作。
    # 刻意不给 REVIEW_SUBMIT —— 复核标记会进入回流评测集作为真实标签，
    # 让只读角色能写标签会污染评测数据的来源。
    Role.VIEWER: frozenset(
        {
            Permission.BATCH_READ,
            Permission.REPORT_READ,
            Permission.REPORT_EXPORT,
        }
    ),
}


def has_permission(role: Role, permission: Permission) -> bool:
    """判断角色是否拥有某项权限。

    未知角色返回 False（fail-closed）——若将来新增了角色但忘了在
    `ROLE_PERMISSIONS` 里登记，结果应该是「什么都不能做」，
    而不是「什么都能做」。
    """
    return permission in ROLE_PERMISSIONS.get(role, frozenset())
