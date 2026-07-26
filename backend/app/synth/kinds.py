"""违规类型枚举与注入器登记表。

## 为什么八类全部登记、只实现一类

Phase 1 交付的是**契约与确定性**，不是覆盖度。但如果只把已实现的那一类
写进枚举，F3 / F6 开发时就会「静默漏掉」剩下七类——没人会去翻文档确认
自己少测了什么。

因此登记表 `INJECTORS` 对 `ViolationKind` 的**每一个**成员都有条目，
未实现的那七个显式 `raise NotImplementedError`。效果是:

- `test_registry_covers_every_kind` 能机械地断言「无遗漏」
- 真去生成未实现的类型时会**当场炸**，而不是悄悄产出一批全是干净行的
  「负样本齐备」数据集——后者会让召回率指标虚高，且极难发现
"""

from __future__ import annotations

import random
from collections.abc import Callable, Mapping
from decimal import Decimal
from enum import StrEnum
from typing import Any

#: 注入器:就地改写一行草稿，返回该行的「违规细节」用于标签文件。
#: 之所以返回细节而不只返回布尔值，是因为复核台要展示「超了多少」，
#: 而评测要按类型分别统计召回。
Injector = Callable[[random.Random, dict[str, Any]], dict[str, Any]]


class ViolationKind(StrEnum):
    """合成数据可注入的违规模式。

    取值与 `agent_docs/testing.md` 列出的八类一一对应。
    """

    OVER_LIMIT = "over_limit"  # 超限额        ← Phase 1 唯一实现
    WRONG_INVOICE_TYPE = "wrong_invoice_type"  # 错票种
    STALE_CLAIM = "stale_claim"  # 超时效
    WRONG_TITLE = "wrong_title"  # 抬头错
    SEQUENTIAL_INVOICE = "sequential_invoice"  # 连号
    SPLIT_CLAIM = "split_claim"  # 拆单
    DUPLICATE_INVOICE = "duplicate_invoice"  # 重复报销
    SPACETIME_CONFLICT = "spacetime_conflict"  # 时空冲突


#: 各费用类型的单笔限额。
#:
#: ⚠️ 这是**夹具的假设**，不是规则的事实来源。F3 的阈值一律来自
#: `rule_config` 表（数据驱动硬约束）。这份副本只用来生成「已知超限」的
#: 样本，并把当时用的限额原样写进标签文件——评测比对的是标签里的数值，
#: 不是这里的常量，因此二者漂移不会制造静默的错误标注。
CATEGORY_LIMITS: Mapping[str, Decimal] = {
    "交通": Decimal("500.00"),
    "住宿": Decimal("800.00"),
    "餐饮": Decimal("200.00"),
    "办公用品": Decimal("1000.00"),
    "差旅补贴": Decimal("300.00"),
}


def _inject_over_limit(rng: random.Random, row: dict[str, Any]) -> dict[str, Any]:
    """把金额抬到该费用类型限额之上。

    倍数从 1.05 起而不是从 2 起:贴着阈值的样本才是分级模型真正难判的
    部分，全是「超十倍」的样本会让召回率虚高。
    """
    category = str(row["费用类型"])
    limit = CATEGORY_LIMITS[category]
    multiplier = Decimal(str(round(rng.uniform(1.05, 3.0), 4)))
    amount = (limit * multiplier).quantize(Decimal("0.01"))
    row["金额"] = amount
    return {
        "category": category,
        "limit": str(limit),
        "amount": str(amount),
        "excess": str(amount - limit),
    }


def _not_implemented(kind: ViolationKind) -> Injector:
    """为尚未实现的违规类型生成一个「当场报错」的注入器。"""

    def _raise(rng: random.Random, row: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError(
            f"违规类型 {kind.value} 尚未实现。Phase 1 的合成数据只交付契约与确定性，"
            f"只真正实现 {ViolationKind.OVER_LIMIT.value} 一类。"
            "需要它时请在 app/synth/kinds.py 补实现并补标签字段，"
            "不要在调用方 try/except 跳过——跳过会产出看似齐备实则缺样本的数据集。"
        )

    return _raise


#: Phase 1 真正能生成的类型。
IMPLEMENTED_KINDS: frozenset[ViolationKind] = frozenset({ViolationKind.OVER_LIMIT})

#: 登记表必须覆盖 `ViolationKind` 的每一个成员，由测试机械断言。
INJECTORS: Mapping[ViolationKind, Injector] = {
    ViolationKind.OVER_LIMIT: _inject_over_limit,
    ViolationKind.WRONG_INVOICE_TYPE: _not_implemented(ViolationKind.WRONG_INVOICE_TYPE),
    ViolationKind.STALE_CLAIM: _not_implemented(ViolationKind.STALE_CLAIM),
    ViolationKind.WRONG_TITLE: _not_implemented(ViolationKind.WRONG_TITLE),
    ViolationKind.SEQUENTIAL_INVOICE: _not_implemented(ViolationKind.SEQUENTIAL_INVOICE),
    ViolationKind.SPLIT_CLAIM: _not_implemented(ViolationKind.SPLIT_CLAIM),
    ViolationKind.DUPLICATE_INVOICE: _not_implemented(ViolationKind.DUPLICATE_INVOICE),
    ViolationKind.SPACETIME_CONFLICT: _not_implemented(ViolationKind.SPACETIME_CONFLICT),
}
