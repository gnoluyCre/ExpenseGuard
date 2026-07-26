"""合成数据生成器 —— 一等交付物，不是测试夹具的附属品。

真实报销数据受脱敏审批阻塞（见 `MEMORY.md` 的 W0 阻塞项），而 F3 的规则、
F6 的关联检测、以及召回率门禁都需要**已知真值**的数据才能开发和度量。
因此这个包与业务代码同等对待:有版本、有契约、有测试。

用法::

    from app.synth import generate_batch, write_batch

    batch = generate_batch(seed=42, row_count=50)
    write_batch(batch, Path("data/synthetic"), stem="baseline-50")

命令行::

    uv run python -m app.synth --seed 42 --rows 50 --out ../data/synthetic
"""

from app.synth.generator import (
    DATA_COLUMNS,
    DEFAULT_ANCHOR_DATE,
    GENERATOR_VERSION,
    BatchManifest,
    RowLabel,
    SyntheticBatch,
    generate_batch,
)
from app.synth.kinds import CATEGORY_LIMITS, IMPLEMENTED_KINDS, INJECTORS, ViolationKind
from app.synth.writer import BatchFiles, read_labels, write_batch

__all__ = [
    "CATEGORY_LIMITS",
    "DATA_COLUMNS",
    "DEFAULT_ANCHOR_DATE",
    "GENERATOR_VERSION",
    "IMPLEMENTED_KINDS",
    "INJECTORS",
    "BatchFiles",
    "BatchManifest",
    "RowLabel",
    "SyntheticBatch",
    "ViolationKind",
    "generate_batch",
    "read_labels",
    "write_batch",
]
