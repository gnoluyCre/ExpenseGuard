"""F3 冻结规则配置、证据和纯求值结果模型。"""

from __future__ import annotations

import re
import unicodedata
import uuid
from datetime import date, datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationInfo,
    field_validator,
    model_validator,
)

from app.core.parsing.models import FieldProvenance, UnifiedField

MAX_CANONICAL_BYTES = 256 * 1024
MAX_VALUE_LENGTH = 512
_DECIMAL_RE = re.compile(r"^\d+(?:\.\d+)?$")
_WHITESPACE_RE = re.compile(r"\s+")


class RuleKind(StrEnum):
    LIMIT = "limit"
    INVOICE_TYPE = "invoice_type"
    TIMELINESS = "timeliness"
    INVOICE_TITLE = "invoice_title"
    INVOICE_DUPLICATE = "invoice_duplicate"


class RuleOutcome(StrEnum):
    PASSED = "passed"
    FLAGGED = "flagged"
    UNAVAILABLE = "unavailable"
    EXEMPTED = "exempted"


class RowVerdict(StrEnum):
    PASSED = "passed"
    MANUAL_REVIEW = "manual_review"
    FLAGGED = "flagged"


type ReasonCode = Literal[
    "limit_exceeded",
    "invoice_type_not_allowed",
    "claim_submitted_late",
    "invoice_title_not_allowed",
    "invoice_duplicate",
    "MISSING_REQUIRED_FIELD",
    "INFERRED_FIELD_NOT_ALLOWED",
    "RULE_NOT_EFFECTIVE",
    "RULE_DISABLED",
    "LIMIT_THRESHOLD_NOT_CONFIGURED",
    "INVOICE_TYPE_POLICY_NOT_CONFIGURED",
    "SUBMISSION_BEFORE_EXPENSE_DATE",
    "TIMELINESS_POLICY_NOT_CONFIGURED",
    "INVOICE_TITLE_MISSING",
    "INVOICE_NO_MISSING",
    "EXEMPTION_MATCHED",
]


class StrictRuleModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ExemptionField(StrEnum):
    EXPENSE_TYPE = "expense_type"
    INVOICE_TYPE = "invoice_type"
    CURRENCY = "currency"


def _normalize_text(value: str) -> str:
    normalized = _WHITESPACE_RE.sub(" ", unicodedata.normalize("NFKC", value)).strip()
    if not normalized:
        raise ValueError("值不能为空")
    if len(normalized) > MAX_VALUE_LENGTH:
        raise ValueError("值超过 512 个 Unicode code point")
    return normalized


def _require_text(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("值必须是字符串")
    return value


class ExemptionCondition(StrictRuleModel):
    field: ExemptionField
    value: str = Field(min_length=1, max_length=MAX_VALUE_LENGTH)

    @field_validator("value", mode="before")
    @classmethod
    def normalize_value(cls, value: object, info: ValidationInfo) -> str:
        normalized = _normalize_text(_require_text(value))
        # 这里不能读取租户币种别名；F2 规范化币种的稳定边界是三字母大写代码。
        if info.data.get("field") is ExemptionField.CURRENCY:
            normalized = normalized.upper()
            if re.fullmatch(r"[A-Z]{3}", normalized) is None:
                raise ValueError("currency 例外值必须是三字母大写代码")
        return normalized


class ExemptionGroup(StrictRuleModel):
    exemption_id: str = Field(min_length=1, max_length=128)
    all: tuple[ExemptionCondition, ...] = Field(min_length=1, max_length=3)

    @field_validator("exemption_id")
    @classmethod
    def id_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("exemption_id 不能为空")
        return value

    @model_validator(mode="after")
    def conditions_are_unique(self) -> ExemptionGroup:
        fields = [condition.field for condition in self.all]
        if len(fields) != len(set(fields)):
            raise ValueError("同一例外组内字段不得重复")
        ordered = tuple(sorted(self.all, key=lambda item: item.field.value))
        return self.model_copy(update={"all": ordered})


class RuleDefinitionBase(StrictRuleModel):
    schema_version: Literal[1] = 1
    enabled: bool = True
    require_direct: bool = False
    exemptions: tuple[ExemptionGroup, ...] = Field(default=(), max_length=100)

    @model_validator(mode="after")
    def exemptions_are_unique(self) -> RuleDefinitionBase:
        ids = [group.exemption_id for group in self.exemptions]
        signatures = [
            tuple((item.field, item.value) for item in group.all) for group in self.exemptions
        ]
        if len(ids) != len(set(ids)):
            raise ValueError("exemption_id 不得重复")
        if len(signatures) != len(set(signatures)):
            raise ValueError("例外条件组不得重复")
        return self.model_copy(
            update={
                "exemptions": tuple(sorted(self.exemptions, key=lambda item: item.exemption_id))
            }
        )


class LimitThreshold(StrictRuleModel):
    expense_type: str = Field(min_length=1, max_length=MAX_VALUE_LENGTH)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    max_amount: str

    @field_validator("expense_type", mode="before")
    @classmethod
    def normalize_expense_type(cls, value: object) -> str:
        return _normalize_text(_require_text(value))

    @field_validator("currency", mode="before")
    @classmethod
    def normalize_currency(cls, value: object) -> str:
        return unicodedata.normalize("NFKC", _require_text(value)).strip().upper()

    @field_validator("max_amount")
    @classmethod
    def normalize_max_amount(cls, value: str) -> str:
        if _DECIMAL_RE.fullmatch(value) is None:
            raise ValueError("max_amount 必须是非指数十进制字符串")
        integer, separator, fraction = value.partition(".")
        integer = integer.lstrip("0") or "0"
        fraction = fraction.rstrip("0")
        normalized = f"{integer}.{fraction}" if separator and fraction else integer
        if normalized == "0":
            raise ValueError("max_amount 必须大于零")
        return normalized


class LimitRuleDefinition(RuleDefinitionBase):
    kind: Literal[RuleKind.LIMIT]
    thresholds: tuple[LimitThreshold, ...] = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def thresholds_are_unique(self) -> LimitRuleDefinition:
        keys = [(item.expense_type, item.currency) for item in self.thresholds]
        if len(keys) != len(set(keys)):
            raise ValueError("限额决策键不得重复")
        return self.model_copy(
            update={
                "thresholds": tuple(
                    sorted(self.thresholds, key=lambda item: (item.expense_type, item.currency))
                )
            }
        )


class InvoiceTypeAllowance(StrictRuleModel):
    expense_type: str = Field(min_length=1, max_length=MAX_VALUE_LENGTH)
    allowed_invoice_types: tuple[str, ...] = Field(min_length=1, max_length=500)

    @field_validator("expense_type", mode="before")
    @classmethod
    def normalize_expense_type(cls, value: object) -> str:
        return _normalize_text(_require_text(value))

    @field_validator("allowed_invoice_types")
    @classmethod
    def normalize_allowed_types(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(_normalize_text(value) for value in values)
        if len(normalized) != len(set(normalized)):
            raise ValueError("允许票种不得重复")
        return tuple(sorted(normalized))


class InvoiceTypeRuleDefinition(RuleDefinitionBase):
    kind: Literal[RuleKind.INVOICE_TYPE]
    allowances: tuple[InvoiceTypeAllowance, ...] = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def allowances_are_unique(self) -> InvoiceTypeRuleDefinition:
        keys = [item.expense_type for item in self.allowances]
        if len(keys) != len(set(keys)):
            raise ValueError("票种决策键不得重复")
        return self.model_copy(
            update={
                "allowances": tuple(sorted(self.allowances, key=lambda item: item.expense_type))
            }
        )


class TimelinessPolicy(StrictRuleModel):
    expense_type: str = Field(min_length=1, max_length=MAX_VALUE_LENGTH)
    max_calendar_days: int = Field(ge=0, le=3660)

    @field_validator("expense_type", mode="before")
    @classmethod
    def normalize_expense_type(cls, value: object) -> str:
        return _normalize_text(_require_text(value))


class TimelinessRuleDefinition(RuleDefinitionBase):
    kind: Literal[RuleKind.TIMELINESS]
    policies: tuple[TimelinessPolicy, ...] = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def policies_are_unique(self) -> TimelinessRuleDefinition:
        keys = [item.expense_type for item in self.policies]
        if len(keys) != len(set(keys)):
            raise ValueError("时效决策键不得重复")
        return self.model_copy(
            update={"policies": tuple(sorted(self.policies, key=lambda item: item.expense_type))}
        )


class InvoiceTitleRuleDefinition(RuleDefinitionBase):
    kind: Literal[RuleKind.INVOICE_TITLE]
    allowed_titles: tuple[str, ...] = Field(min_length=1, max_length=500)

    @field_validator("allowed_titles")
    @classmethod
    def normalize_titles(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(_normalize_text(value) for value in values)
        if len(normalized) != len(set(normalized)):
            raise ValueError("允许抬头不得重复")
        return tuple(sorted(normalized))


class InvoiceDuplicateRuleDefinition(RuleDefinitionBase):
    kind: Literal[RuleKind.INVOICE_DUPLICATE]


type RuleDefinition = Annotated[
    LimitRuleDefinition
    | InvoiceTypeRuleDefinition
    | TimelinessRuleDefinition
    | InvoiceTitleRuleDefinition
    | InvoiceDuplicateRuleDefinition,
    Field(discriminator="kind"),
]
RULE_DEFINITION_ADAPTER: TypeAdapter[RuleDefinition] = TypeAdapter(RuleDefinition)


class RuleVersion(StrictRuleModel):
    id: uuid.UUID
    rule_id: str = Field(min_length=1, max_length=128)
    version: int = Field(ge=1)
    effective_from: date
    config_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    definition: RuleDefinition

    @field_validator("rule_id")
    @classmethod
    def rule_id_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("rule_id 不能为空")
        return value


class RuleSelection(StrictRuleModel):
    rule_id: str = Field(min_length=1, max_length=128)
    rule_kind: RuleKind
    selected: RuleVersion | None = None
    reason_code: Literal["RULE_NOT_EFFECTIVE"] | None = None

    @model_validator(mode="after")
    def selected_xor_reason(self) -> RuleSelection:
        if (self.selected is None) == (self.reason_code is None):
            raise ValueError("规则选择必须且只能包含版本或未生效原因")
        if self.selected is not None and (
            self.selected.rule_id != self.rule_id
            or self.selected.definition.kind is not self.rule_kind
        ):
            raise ValueError("选中版本必须属于声明的 rule family")
        return self


class SelectedRuleVersion(StrictRuleModel):
    version: int = Field(ge=1)
    config_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")


class RuleFamilyManifest(StrictRuleModel):
    rule_id: str = Field(min_length=1, max_length=128)
    kind: RuleKind
    selected_versions: tuple[SelectedRuleVersion, ...]

    @field_validator("rule_id")
    @classmethod
    def rule_id_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("rule_id 不能为空")
        return value

    @field_validator("selected_versions")
    @classmethod
    def selected_versions_are_unique_and_sorted(
        cls, values: tuple[SelectedRuleVersion, ...]
    ) -> tuple[SelectedRuleVersion, ...]:
        keys = [(item.version, item.config_fingerprint) for item in values]
        if len(keys) != len(set(keys)):
            raise ValueError("manifest selected_versions 不得重复")
        return tuple(sorted(values, key=lambda item: (item.version, item.config_fingerprint)))


class EvidenceBase(StrictRuleModel):
    schema_version: Literal[1] = 1
    outcome: Literal[RuleOutcome.FLAGGED, RuleOutcome.UNAVAILABLE, RuleOutcome.EXEMPTED]
    rule_kind: RuleKind
    reason_code: ReasonCode
    required_fields: tuple[UnifiedField, ...]
    provenance: dict[UnifiedField, FieldProvenance]
    exemption_id: str | None = None

    @field_validator("required_fields")
    @classmethod
    def required_fields_are_unique(
        cls, values: tuple[UnifiedField, ...]
    ) -> tuple[UnifiedField, ...]:
        if not values or len(values) != len(set(values)):
            raise ValueError("required_fields 必须非空且不得重复")
        return values

    @model_validator(mode="after")
    def reason_matches_kind_and_outcome(self) -> EvidenceBase:
        flagged = {
            RuleKind.LIMIT: "limit_exceeded",
            RuleKind.INVOICE_TYPE: "invoice_type_not_allowed",
            RuleKind.TIMELINESS: "claim_submitted_late",
            RuleKind.INVOICE_TITLE: "invoice_title_not_allowed",
            RuleKind.INVOICE_DUPLICATE: "invoice_duplicate",
        }
        unavailable: dict[RuleKind, frozenset[str]] = {
            RuleKind.LIMIT: frozenset({"LIMIT_THRESHOLD_NOT_CONFIGURED"}),
            RuleKind.INVOICE_TYPE: frozenset({"INVOICE_TYPE_POLICY_NOT_CONFIGURED"}),
            RuleKind.TIMELINESS: frozenset(
                {"SUBMISSION_BEFORE_EXPENSE_DATE", "TIMELINESS_POLICY_NOT_CONFIGURED"}
            ),
            RuleKind.INVOICE_TITLE: frozenset({"INVOICE_TITLE_MISSING"}),
            RuleKind.INVOICE_DUPLICATE: frozenset({"INVOICE_NO_MISSING"}),
        }
        generic = frozenset(
            {
                "MISSING_REQUIRED_FIELD",
                "INFERRED_FIELD_NOT_ALLOWED",
                "RULE_NOT_EFFECTIVE",
                "RULE_DISABLED",
            }
        )
        if self.outcome is RuleOutcome.FLAGGED and self.reason_code != flagged[self.rule_kind]:
            raise ValueError("flagged reason code 与 rule kind 不一致")
        if self.outcome is RuleOutcome.UNAVAILABLE and self.reason_code not in (
            generic | unavailable[self.rule_kind]
        ):
            raise ValueError("unavailable reason code 与 rule kind 不一致")
        if self.outcome is RuleOutcome.EXEMPTED:
            if self.reason_code != "EXEMPTION_MATCHED" or self.exemption_id is None:
                raise ValueError("exempted 必须携带例外 ID")
        elif self.exemption_id is not None:
            raise ValueError("只有 exempted 可以携带例外 ID")
        if not set(self.provenance).issubset(self.required_fields):
            raise ValueError("evidence provenance 只能包含机械判定所需字段")
        return self


class LimitEvidence(EvidenceBase):
    rule_kind: Literal[RuleKind.LIMIT]
    amount: str | None = None
    expense_type: str | None = None
    currency: str | None = None
    operator: Literal["gt"] = "gt"
    max_amount: str | None = None

    @model_validator(mode="after")
    def flagged_values_are_complete(self) -> LimitEvidence:
        if self.outcome is RuleOutcome.FLAGGED and None in (
            self.amount,
            self.expense_type,
            self.currency,
            self.max_amount,
        ):
            raise ValueError("limit flagged evidence 不完整")
        return self


class InvoiceTypeEvidence(EvidenceBase):
    rule_kind: Literal[RuleKind.INVOICE_TYPE]
    expense_type: str | None = None
    invoice_type: str | None = None
    allowed_invoice_types_fingerprint: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def flagged_values_are_complete(self) -> InvoiceTypeEvidence:
        if self.outcome is RuleOutcome.FLAGGED and None in (
            self.expense_type,
            self.invoice_type,
            self.allowed_invoice_types_fingerprint,
        ):
            raise ValueError("invoice_type flagged evidence 不完整")
        return self


class TimelinessEvidence(EvidenceBase):
    rule_kind: Literal[RuleKind.TIMELINESS]
    expense_type: str | None = None
    expense_date: str | None = None
    submission_date: str | None = None
    actual_calendar_days: int | None = None
    max_calendar_days: int | None = None

    @model_validator(mode="after")
    def flagged_values_are_complete(self) -> TimelinessEvidence:
        if self.outcome is RuleOutcome.FLAGGED and None in (
            self.expense_type,
            self.expense_date,
            self.submission_date,
            self.actual_calendar_days,
            self.max_calendar_days,
        ):
            raise ValueError("timeliness flagged evidence 不完整")
        return self


class InvoiceTitleEvidence(EvidenceBase):
    rule_kind: Literal[RuleKind.INVOICE_TITLE]
    invoice_title: str | None = None
    allowed_titles_fingerprint: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def flagged_values_are_complete(self) -> InvoiceTitleEvidence:
        if self.outcome is RuleOutcome.FLAGGED and None in (
            self.invoice_title,
            self.allowed_titles_fingerprint,
        ):
            raise ValueError("invoice_title flagged evidence 不完整")
        return self


class InvoiceDuplicateEvidence(EvidenceBase):
    rule_kind: Literal[RuleKind.INVOICE_DUPLICATE]
    invoice_no: str | None = None
    duplicate_of_file_version_id: uuid.UUID | None = None
    duplicate_of_root_file_version_id: uuid.UUID | None = None
    duplicate_of_row_no: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def flagged_values_are_complete(self) -> InvoiceDuplicateEvidence:
        if self.outcome is RuleOutcome.FLAGGED and None in (
            self.invoice_no,
            self.duplicate_of_file_version_id,
            self.duplicate_of_root_file_version_id,
            self.duplicate_of_row_no,
        ):
            raise ValueError("invoice_duplicate flagged evidence 不完整")
        return self


type RuleEvidence = Annotated[
    LimitEvidence
    | InvoiceTypeEvidence
    | TimelinessEvidence
    | InvoiceTitleEvidence
    | InvoiceDuplicateEvidence,
    Field(discriminator="rule_kind"),
]


class RuleEvaluation(StrictRuleModel):
    outcome: RuleOutcome
    reason_code: ReasonCode | None = None
    evidence: RuleEvidence | None = None

    @model_validator(mode="after")
    def evidence_matches_outcome(self) -> RuleEvaluation:
        if self.outcome is RuleOutcome.PASSED:
            if self.reason_code is not None or self.evidence is not None:
                raise ValueError("passed 不得携带 reason 或 evidence")
        elif self.reason_code is None or self.evidence is None:
            raise ValueError("非 passed 必须携带 reason 和 evidence")
        elif self.reason_code != self.evidence.reason_code or self.outcome != self.evidence.outcome:
            raise ValueError("求值结果必须与 evidence 一致")
        return self


class DuplicateMatch(StrictRuleModel):
    file_version_id: uuid.UUID
    root_file_version_id: uuid.UUID
    row_no: int = Field(ge=1)


class InvoiceOccurrence(StrictRuleModel):
    """数据库编排层装载后交给纯核心的一个稳定发票号出现位置。"""

    file_version_id: uuid.UUID
    root_file_version_id: uuid.UUID
    root_uploaded_at: datetime
    row_no: int = Field(ge=1)
    invoice_no: str = Field(min_length=1, max_length=128)

    @field_validator("root_uploaded_at")
    @classmethod
    def uploaded_at_must_have_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("root_uploaded_at 必须带时区")
        return value
