"""合成数据生成器的命令行入口。

    uv run python -m app.synth --seed 42 --rows 50 --out ../data/synthetic --stem baseline-50

提交策略:生成器与 manifest（含 seed）进仓库，配一份 ~50 行的小样本方便
新同学开箱即跑；大批次不进仓库，用 manifest 里的 seed 现场重放即可。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from app.synth.generator import DEFAULT_ANCHOR_DATE, generate_batch
from app.synth.kinds import ViolationKind
from app.synth.writer import write_batch


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="app.synth", description="生成合成报销批次")
    parser.add_argument("--seed", type=int, required=True, help="随机种子（决定全部输出）")
    parser.add_argument("--rows", type=int, default=50, help="行数，默认 50")
    parser.add_argument("--violation-rate", type=float, default=0.2, help="违规行占比，默认 0.2")
    parser.add_argument(
        "--kind",
        action="append",
        choices=[k.value for k in ViolationKind],
        help="允许注入的违规类型，可重复。默认只用已实现的 over_limit",
    )
    parser.add_argument("--out", type=Path, required=True, help="输出目录")
    parser.add_argument("--stem", default="batch", help="文件名主干，默认 batch")
    args = parser.parse_args(argv)

    kinds = tuple(ViolationKind(k) for k in args.kind) if args.kind else (ViolationKind.OVER_LIMIT,)

    batch = generate_batch(
        seed=args.seed,
        row_count=args.rows,
        violation_rate=args.violation_rate,
        kinds=kinds,
        anchor_date=DEFAULT_ANCHOR_DATE,
    )
    files = write_batch(batch, args.out, stem=args.stem)

    violations = sum(1 for label in batch.labels if label.is_violation)
    # 不用 print:ruff 的 T20 全局禁止 print，避免调试语句混进生产路径。
    sys.stderr.write(
        f"已生成 {batch.manifest.row_count} 行（违规 {violations} 行）\n"
        f"  数据 {files.data}\n"
        f"  标签 {files.labels}\n"
        f"  清单 {files.manifest}\n"
        f"  摘要 {batch.manifest.rows_digest}\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
