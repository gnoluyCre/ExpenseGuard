"""ORM 模型汇总。

Alembic 的 autogenerate 依赖本模块把全部模型导入到 `Base.metadata`，
因此**新增模型后必须在这里导出**，否则迁移会漏表。
"""

from app.db.models.audit import AuditLog
from app.db.models.batch import (
    ExpenseRow,
    FieldAvailability,
    FieldStatus,
    FileVersion,
    ParseStatus,
    RevisionReason,
    RowResult,
)
from app.db.models.config import RuleConfig, SchemaMapping, SchemaMappingVersion
from app.db.models.findings import (
    CapabilityDeclaration,
    CapabilityStatus,
    CorrelationFinding,
    EvidenceStep,
    Finding,
    Review,
    ReviewDecision,
    RuleKind,
    SamplingAudit,
)
from app.db.models.policy import PolicyClause, PolicyDocument
from app.db.models.tenancy import AppUser, Role, Tenant, UserSession
from app.db.models.validation import ValidationDependency, ValidationRun, ValidationRunStatus

__all__ = [
    "AppUser",
    "AuditLog",
    "CapabilityDeclaration",
    "CapabilityStatus",
    "CorrelationFinding",
    "EvidenceStep",
    "ExpenseRow",
    "FieldAvailability",
    "FieldStatus",
    "FileVersion",
    "Finding",
    "ParseStatus",
    "PolicyClause",
    "PolicyDocument",
    "Review",
    "ReviewDecision",
    "RevisionReason",
    "Role",
    "RowResult",
    "RuleConfig",
    "RuleKind",
    "SamplingAudit",
    "SchemaMapping",
    "SchemaMappingVersion",
    "Tenant",
    "UserSession",
    "ValidationDependency",
    "ValidationRun",
    "ValidationRunStatus",
]
