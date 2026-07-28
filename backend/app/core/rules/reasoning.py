"""只从强类型 evidence 生成稳定、无模型依赖的判定说明。"""

from __future__ import annotations

from app.core.rules.models import (
    InvoiceDuplicateEvidence,
    InvoiceTitleEvidence,
    InvoiceTypeEvidence,
    LimitEvidence,
    RuleEvidence,
    TimelinessEvidence,
)

_GENERIC_REASONING: dict[str, str] = {
    "MISSING_REQUIRED_FIELD": "规则依赖字段缺失，无法完成确定性判定。",
    "INFERRED_FIELD_NOT_ALLOWED": "规则要求直接映射字段，但至少一个依赖字段来自推断。",
    "RULE_NOT_EFFECTIVE": "费用发生日早于该规则的首个生效版本。",
    "RULE_DISABLED": "费用发生日对应的规则版本已显式停用。",
    "LIMIT_THRESHOLD_NOT_CONFIGURED": "未找到费用类型与币种对应的限额配置。",
    "INVOICE_TYPE_POLICY_NOT_CONFIGURED": "未找到费用类型对应的允许票种配置。",
    "TIMELINESS_POLICY_NOT_CONFIGURED": "未找到费用类型对应的报销时效配置。",
    "SUBMISSION_BEFORE_EXPENSE_DATE": "提交日期早于费用发生日期，无法完成时效判定。",
    "INVOICE_TITLE_MISSING": "发票抬头缺失，无法完成确定性判定。",
    "INVOICE_NO_MISSING": "发票号缺失，无法完成确定性查重。",
    "EXEMPTION_MATCHED": "该行精确命中已配置的规则例外。",
}


def render_reasoning(evidence: RuleEvidence) -> str:
    generic = _GENERIC_REASONING.get(evidence.reason_code)
    if generic is not None:
        return generic
    if isinstance(evidence, LimitEvidence):
        return f"金额 {evidence.amount} 大于配置限额 {evidence.max_amount}。"
    if isinstance(evidence, InvoiceTypeEvidence):
        return f"票种“{evidence.invoice_type}”不在该费用类型的精确允许集合中。"
    if isinstance(evidence, TimelinessEvidence):
        return (
            f"实际提交间隔 {evidence.actual_calendar_days} 个自然日，"
            f"大于配置上限 {evidence.max_calendar_days} 天。"
        )
    if isinstance(evidence, InvoiceTitleEvidence):
        return f"发票抬头“{evidence.invoice_title}”不在精确允许集合中。"
    if isinstance(evidence, InvoiceDuplicateEvidence):
        return (
            f"发票号与文件版本 {evidence.duplicate_of_file_version_id} "
            f"第 {evidence.duplicate_of_row_no} 行精确重复。"
        )
    raise ValueError(f"不支持的 reason code: {evidence.reason_code}")
