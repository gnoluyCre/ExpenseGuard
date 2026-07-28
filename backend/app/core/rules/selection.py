"""按费用发生日选择不可变规则版本。"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date

from app.core.rules.models import RuleKind, RuleSelection, RuleVersion


def select_effective_rule_version(
    *,
    rule_id: str,
    rule_kind: RuleKind,
    candidates: Sequence[RuleVersion],
    expense_date: date,
) -> RuleSelection:
    """从一个逻辑 rule family 中稳定选择版本，不访问当前时间或数据库。"""
    for candidate in candidates:
        if candidate.rule_id != rule_id or candidate.definition.kind is not rule_kind:
            raise ValueError("候选版本不属于指定 rule family")

    eligible = [candidate for candidate in candidates if candidate.effective_from <= expense_date]
    if not eligible:
        return RuleSelection(
            rule_id=rule_id,
            rule_kind=rule_kind,
            reason_code="RULE_NOT_EFFECTIVE",
        )
    selected = max(eligible, key=lambda item: (item.effective_from, item.version))
    return RuleSelection(rule_id=rule_id, rule_kind=rule_kind, selected=selected)
