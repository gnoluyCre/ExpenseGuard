"""确定性的合成报销批次生成器。

## 三条不可妥协的性质

1. **确定性** —— 同一个 seed 必然产出逐字节相同的逻辑行。实现上用
   `random.Random(seed)` 的**显式实例**，绝不碰模块级的全局 `random`:
   全局状态会被任何第三方库的一次 `random.seed()` 悄悄污染，届时
   「同 seed 不同结果」这种 bug 几乎无法定位。
2. **不读时钟** —— 日期一律以 `anchor_date` 为基准偏移。任何
   `date.today()` 都会让今天生成的夹具明天重放不出来。
3. **标签与数据物理分离** —— 本模块只产出「行数据」与「标签」两个独立
   对象，由 `writer` 写到两个文件。`testing.md` 点名标签泄漏是合成数据
   三大陷阱之一，而结构上分开是零成本的预防:模型侧只拿得到 `.xlsx`。

## 覆盖度不是 Phase 1 的目标

只真正注入「超限额」一类违规，其余七类在 `kinds.py` 里显式抛
`NotImplementedError`。理由见该模块 docstring。
"""

from __future__ import annotations

import hashlib
import json
import random
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field

from app.synth.kinds import CATEGORY_LIMITS, IMPLEMENTED_KINDS, INJECTORS, ViolationKind

#: 生成器契约版本。改动列集合或字段语义时必须递增——
#: manifest 里记着它，才能判断一份旧夹具是否还能与当前解析器对齐。
GENERATOR_VERSION = "1"

#: 夹具的列集合。刻意模仿国内财务系统的中文导出表头，
#: 因为 F2 的列名映射配置正是要处理这种真实形态。
DATA_COLUMNS: tuple[str, ...] = (
    "报销单号",
    "员工工号",
    "员工姓名",
    "部门",
    "费用类型",
    "费用发生日期",
    "提交日期",
    "金额",
    "币种",
    "发票号码",
    "发票类型",
    "发票抬头",
    "商户名称",
    "发生地点",
    "事由",
)

#: 不读时钟。改这个常量会改变所有已生成夹具的重放结果，等同于换 seed。
DEFAULT_ANCHOR_DATE = date(2026, 6, 30)

_DEPARTMENTS = ("研发部", "市场部", "销售部", "财务部", "人力资源部")
_SURNAMES = ("张", "王", "李", "赵", "陈", "刘", "杨", "黄", "周", "吴")
_GIVEN_NAMES = ("伟", "芳", "娜", "敏", "静", "磊", "洋", "勇", "艳", "杰")
_CITIES = ("北京", "上海", "深圳", "杭州", "成都", "武汉", "西安")
_INVOICE_TYPES = ("增值税专用发票", "增值税普通发票", "电子发票")
_LEGAL_TITLE = "示例科技（北京）有限公司"
_MERCHANTS = {
    "交通": ("滴滴出行", "首汽约车", "中国铁路"),
    "住宿": ("如家酒店", "全季酒店", "汉庭酒店"),
    "餐饮": ("海底捞", "西贝莜面村", "员工食堂"),
    "办公用品": ("得力办公", "晨光文具", "京东企业购"),
    "差旅补贴": ("差旅补贴", "出差津贴"),
}
_PURPOSES = ("客户拜访", "项目出差", "团队会议", "日常办公", "培训学习")


class RowLabel(BaseModel):
    """单行的真值标签。**永远不写进 `.xlsx`。**"""

    row_no: int
    is_violation: bool
    #: 该行被注入的违规类型。干净行为空列表。
    kinds: list[ViolationKind] = Field(default_factory=list)
    #: 按类型索引的细节（超了多少、用的哪条限额……），供评测与复核台使用
    details: dict[str, Any] = Field(default_factory=dict)


class BatchManifest(BaseModel):
    """重放这批数据所需的全部信息。

    ⚠️ `rows_digest` 校验的是**逻辑行**而非 `.xlsx` 字节。openpyxl 会把
    生成时刻写进 `docProps/core.xml`，zip 条目也带时间戳，所以同 seed 两次
    导出的文件字节必然不同。把「确定性」定义在逻辑行上是这里唯一诚实的做法,
    声称字节级可复现会是假承诺。
    """

    generator_version: str
    seed: int
    row_count: int
    violation_rate: float
    anchor_date: date
    columns: list[str]
    kinds_requested: list[ViolationKind]
    kinds_implemented: list[ViolationKind]
    kinds_not_implemented: list[ViolationKind]
    #: 逻辑行的 SHA-256，见上方说明
    rows_digest: str


@dataclass(frozen=True, slots=True)
class SyntheticBatch:
    """一批合成数据:行、标签、清单三者分离。"""

    rows: list[dict[str, Any]]
    labels: list[RowLabel]
    manifest: BatchManifest


def _random_name(rng: random.Random) -> str:
    return rng.choice(_SURNAMES) + rng.choice(_GIVEN_NAMES)


def _clean_row(rng: random.Random, row_no: int, anchor: date) -> dict[str, Any]:
    """生成一行**合规**记录。违规由注入器在此基础上改写。"""
    category = rng.choice(tuple(CATEGORY_LIMITS))
    limit = CATEGORY_LIMITS[category]
    # 合规金额:限额的 10%–90%，留出与阈值的安全距离
    amount = (limit * Decimal(str(round(rng.uniform(0.1, 0.9), 4)))).quantize(Decimal("0.01"))

    occurred = anchor - timedelta(days=rng.randrange(1, 60))
    submitted = occurred + timedelta(days=rng.randrange(0, 10))
    city = rng.choice(_CITIES)

    return {
        "报销单号": f"EXP-{anchor:%Y%m}-{row_no:05d}",
        "员工工号": f"E{rng.randrange(10000, 99999)}",
        "员工姓名": _random_name(rng),
        "部门": rng.choice(_DEPARTMENTS),
        "费用类型": category,
        "费用发生日期": occurred,
        "提交日期": submitted,
        "金额": amount,
        "币种": "CNY",
        "发票号码": f"{rng.randrange(10**7, 10**8)}",
        "发票类型": rng.choice(_INVOICE_TYPES),
        "发票抬头": _LEGAL_TITLE,
        "商户名称": rng.choice(_MERCHANTS[category]),
        "发生地点": city,
        "事由": f"{city}{rng.choice(_PURPOSES)}",
    }


def _rows_digest(rows: Sequence[dict[str, Any]]) -> str:
    """逻辑行的稳定摘要。

    `default=str` 让 `date` / `Decimal` 有确定的文本形式；`sort_keys=True`
    使摘要不受列顺序调整影响。
    """
    canonical = json.dumps(list(rows), sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def generate_batch(
    *,
    seed: int,
    row_count: int = 50,
    violation_rate: float = 0.2,
    kinds: Sequence[ViolationKind] = (ViolationKind.OVER_LIMIT,),
    anchor_date: date = DEFAULT_ANCHOR_DATE,
) -> SyntheticBatch:
    """生成一批合成报销数据。

    Args:
        seed: 随机种子。同 seed + 同参数 → 同逻辑行。
        row_count: 行数。
        violation_rate: 违规行占比，[0, 1]。
        kinds: 允许注入的违规类型。含未实现类型时会抛 `NotImplementedError`,
            这是刻意的——见 `kinds.py`。
        anchor_date: 日期基准。不读系统时钟。

    Raises:
        ValueError: 参数越界。
        NotImplementedError: `kinds` 里含 Phase 1 未实现的类型。
    """
    if row_count < 1:
        raise ValueError("row_count 必须 ≥ 1")
    if not 0.0 <= violation_rate <= 1.0:
        raise ValueError("violation_rate 必须落在 [0, 1]")
    if not kinds:
        raise ValueError("kinds 不能为空——不注入任何违规请把 violation_rate 设为 0")

    rng = random.Random(seed)  # noqa: S311  (夹具生成，非密码学用途)

    rows: list[dict[str, Any]] = []
    labels: list[RowLabel] = []

    for row_no in range(1, row_count + 1):
        row = _clean_row(rng, row_no, anchor_date)
        # 先掷骰子再决定类型，两次抽样都走同一个 rng，保证整条序列可复现
        if rng.random() < violation_rate:
            kind = rng.choice(tuple(kinds))
            detail = INJECTORS[kind](rng, row)
            labels.append(
                RowLabel(
                    row_no=row_no,
                    is_violation=True,
                    kinds=[kind],
                    details={kind.value: detail},
                )
            )
        else:
            labels.append(RowLabel(row_no=row_no, is_violation=False))
        rows.append(row)

    requested = list(dict.fromkeys(kinds))
    manifest = BatchManifest(
        generator_version=GENERATOR_VERSION,
        seed=seed,
        row_count=row_count,
        violation_rate=violation_rate,
        anchor_date=anchor_date,
        columns=list(DATA_COLUMNS),
        kinds_requested=requested,
        kinds_implemented=sorted(IMPLEMENTED_KINDS),
        # 显式列出「本生成器还不会造什么」——下游读 manifest 就知道
        # 自己的召回率是在哪几类上测出来的，不会误以为覆盖齐全
        kinds_not_implemented=sorted(set(ViolationKind) - IMPLEMENTED_KINDS),
        rows_digest=_rows_digest(rows),
    )

    return SyntheticBatch(rows=rows, labels=labels, manifest=manifest)
