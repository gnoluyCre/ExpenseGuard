"""F2 统一报销记录、映射配置和解析结果模型。"""

from __future__ import annotations

import uuid
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class UnifiedField(StrEnum):
    """固定的 12 个统一报销字段，顺序也是稳定输出顺序。"""

    AMOUNT = "amount"
    EXPENSE_DATE = "expense_date"
    EMPLOYEE = "employee"
    EXPENSE_TYPE = "expense_type"
    INVOICE_TYPE = "invoice_type"
    INVOICE_NO = "invoice_no"
    MERCHANT = "merchant"
    INVOICE_TITLE = "invoice_title"
    SUBMISSION_DATE = "submission_date"
    LOCATION = "location"
    CURRENCY = "currency"
    DESCRIPTION = "description"


UNIFIED_FIELDS: tuple[UnifiedField, ...] = tuple(UnifiedField)
REQUIRED_FIELDS = frozenset({UnifiedField.AMOUNT, UnifiedField.EXPENSE_DATE})


class StrictModel(BaseModel):
    """解析配置和结果的共同严格边界。"""

    model_config = ConfigDict(extra="forbid", frozen=True)


class MappingEntry(StrictModel):
    """一个源列到统一字段的直接映射。"""

    source_column: str = Field(min_length=1, max_length=255)
    target_field: UnifiedField

    @field_validator("source_column")
    @classmethod
    def source_column_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("source_column 不能为空")
        return value


class AvailabilityThresholds(StrictModel):
    """字段可用性阈值。Decimal 避免配置指纹和比较出现浮点漂移。"""

    available_min_non_null_rate: Decimal = Field(default=Decimal("0.80"), ge=0, le=1)
    inferred_min_success_rate: Decimal = Field(default=Decimal("0.80"), ge=0, le=1)


class LiteralLookupCase(StrictModel):
    """字面量包含匹配的一项；列表顺序决定优先级。"""

    literal: str = Field(min_length=1)
    value: str = Field(min_length=1)


class ConstantInferenceRule(StrictModel):
    """固定值推断；首版只允许推断币种。"""

    rule_id: str = Field(min_length=1, max_length=128)
    type: Literal["constant"]
    target_field: Literal[UnifiedField.CURRENCY]
    value: str = Field(min_length=1)


class LiteralLookupInferenceRule(StrictModel):
    """从已直接映射字段做 NFKC 字面量包含匹配。"""

    rule_id: str = Field(min_length=1, max_length=128)
    type: Literal["literal_lookup"]
    target_field: UnifiedField
    source_fields: tuple[UnifiedField, ...] = Field(min_length=1)
    cases: tuple[LiteralLookupCase, ...] = Field(min_length=1)


InferenceRule = Annotated[
    ConstantInferenceRule | LiteralLookupInferenceRule,
    Field(discriminator="type"),
]


class InferenceConfig(StrictModel):
    """映射版本内不可变的确定性推断配置。"""

    rules: tuple[InferenceRule, ...] = ()


class MappingVersionConfig(StrictModel):
    """执行解析所需的完整不可变映射版本。"""

    id: uuid.UUID
    version: int = Field(ge=1)
    header_signature: str = Field(pattern=r"^[0-9a-f]{64}$")
    mappings: tuple[MappingEntry, ...]
    availability_thresholds: AvailabilityThresholds = Field(default_factory=AvailabilityThresholds)
    currency_aliases: dict[str, str] = Field(default_factory=dict)
    inference_config: InferenceConfig = Field(default_factory=InferenceConfig)


class ProvenanceMode(StrEnum):
    """规范化字段的取值来源。"""

    MAPPED = "mapped"
    INFERRED = "inferred"


class FieldProvenance(StrictModel):
    """不复制原始值的字段证据。"""

    mode: ProvenanceMode
    source_columns: tuple[str, ...]
    inference_rule_id: str | None = None

    @model_validator(mode="after")
    def validate_mode_details(self) -> FieldProvenance:
        if self.mode is ProvenanceMode.MAPPED and (
            not self.source_columns or self.inference_rule_id is not None
        ):
            raise ValueError("mapped provenance 必须有源列且不能有推断规则")
        if self.mode is ProvenanceMode.INFERRED and self.inference_rule_id is None:
            raise ValueError("inferred provenance 必须有推断规则 ID")
        return self


class NormalizedExpenseRecord(StrictModel):
    """写入 ``expense_row.normalized_json`` 的唯一 schema。"""

    schema_version: Literal[1] = 1
    mapping_version_id: uuid.UUID
    amount: str = Field(pattern=r"^-?(?:0|[1-9]\d*)(?:\.\d+)?$")
    expense_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    employee: str | None = None
    expense_type: str | None = None
    invoice_type: str | None = None
    invoice_no: str | None = None
    merchant: str | None = None
    invoice_title: str | None = None
    submission_date: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    location: str | None = None
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    description: str | None = None
    field_provenance: dict[UnifiedField, FieldProvenance]

    @model_validator(mode="after")
    def provenance_must_match_non_null_fields(self) -> NormalizedExpenseRecord:
        populated = {field for field in UNIFIED_FIELDS if getattr(self, field.value) is not None}
        if set(self.field_provenance) != populated:
            raise ValueError("field_provenance 必须且只能覆盖非空统一字段")
        return self


FieldErrorCode = Literal[
    "REQUIRED_VALUE_MISSING",
    "AMOUNT_INVALID_FORMAT",
    "AMOUNT_OUT_OF_RANGE",
    "DATE_INVALID_FORMAT",
    "DATE_OUT_OF_RANGE",
    "TEXT_TOO_LONG",
    "CURRENCY_INVALID",
]


class FieldError(StrictModel):
    """一个稳定、用户安全的字段解析错误。"""

    field: UnifiedField
    code: FieldErrorCode
    source_column: str
    message: str


class RowErrorDetail(StrictModel):
    """失败行写入 ``parse_error_detail`` 的版本化结构。"""

    schema_version: Literal[1] = 1
    mapping_version_id: uuid.UUID
    errors: tuple[FieldError, ...]


class RowParseResult(StrictModel):
    """一行纯逻辑解析结果，成功与失败严格互斥。"""

    normalized: NormalizedExpenseRecord | None = None
    error_detail: RowErrorDetail | None = None
    observed_provenance: dict[UnifiedField, FieldProvenance] = Field(default_factory=dict)

    @model_validator(mode="after")
    def success_and_error_are_exclusive(self) -> RowParseResult:
        if (self.normalized is None) == (self.error_detail is None):
            raise ValueError("行解析结果必须且只能是成功或失败之一")
        return self


class DirectAvailabilityEvidence(StrictModel):
    """直接映射的非空率证据。"""

    configured: bool
    source_columns: tuple[str, ...]
    non_null_count: int = Field(ge=0)
    non_null_rate: str = Field(pattern=r"^\d\.\d{4}$")
    threshold: str = Field(pattern=r"^\d\.\d{4}$")


class InferenceAvailabilityEvidence(StrictModel):
    """确定性推断的成功率证据。"""

    configured: bool
    rule_ids: tuple[str, ...]
    success_count: int = Field(ge=0)
    success_rate: str = Field(pattern=r"^\d\.\d{4}$")
    threshold: str = Field(pattern=r"^\d\.\d{4}$")


class AvailabilityEvidence(StrictModel):
    """不含样本值的版本化字段可用性证据。"""

    schema_version: Literal[1] = 1
    mapping_version_id: uuid.UUID
    total_rows: int = Field(gt=0)
    direct: DirectAvailabilityEvidence
    inference: InferenceAvailabilityEvidence
    selected_basis: Literal["direct", "inference", "none"]


class AvailabilityResult(StrictModel):
    """一个统一字段的三级可用性结果。"""

    field_name: UnifiedField
    status: Literal["available", "inferred", "missing"]
    evidence: AvailabilityEvidence


class BatchParseResult(StrictModel):
    """批次解析服务结果。"""

    file_version_id: uuid.UUID
    mapping_version_id: uuid.UUID
    mapping_version: int
    status: Literal["parsed", "parsed_with_errors"]
    total_rows: int
    success_count: int
    error_count: int
    parsed_at: str
    reused_existing: bool
