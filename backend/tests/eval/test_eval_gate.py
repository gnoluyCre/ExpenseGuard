"""评测门禁 —— Phase 1 的占位实现。

## 为什么占位也要是**真实存在**的用例

CI 的 eval-gate job 跑 `pytest -m eval`。若这个 marker 一个用例都选不中，
pytest 的退出码是 **5（no tests collected）**，job 会假红——然后大概率被
某次「修 CI」顺手加上 `|| true`，门禁从此永久失效。

所以这里有一个货真价实的 `@pytest.mark.eval` 用例，它在 Phase 1 主动
`skip`，Phase 2 只要 `evals/baseline.json` 里填了 thresholds 就自动开始阻断。

## 为什么「填了阈值却没有实测值」是失败而不是跳过

声明阈值意味着团队已经承诺了一条质量下界。此时找不到 metrics 只有两种可能:
评测没跑，或者产物路径错了。两种都必须红。静默跳过等于把「未验证」
渲染成「已通过」，在审计系统里这是最危险的失败形态。
"""

import json
from pathlib import Path
from typing import Any

import pytest

#: backend/tests/eval/test_eval_gate.py → backend/
_BACKEND_DIR = Path(__file__).resolve().parents[2]
BASELINE_PATH = _BACKEND_DIR / "evals" / "baseline.json"


def _load_baseline() -> dict[str, Any]:
    return json.loads(BASELINE_PATH.read_text(encoding="utf-8"))


def test_baseline_文件存在且形状正确() -> None:
    """这条不带 eval marker——它是 baseline.json 本身的守门员，
    每次普通 pytest 都要跑。文件被误删或写坏时立刻发现。"""
    baseline = _load_baseline()
    assert isinstance(baseline["thresholds"], dict)
    assert isinstance(baseline["metrics_path"], str)


@pytest.mark.eval
def test_评测指标不低于基线阈值() -> None:
    baseline = _load_baseline()
    thresholds: dict[str, dict[str, float]] = baseline["thresholds"]

    if not thresholds:
        pytest.skip(
            "Phase 1 无评测集:evals/baseline.json 的 thresholds 为空，门禁处于待命状态。"
            "Phase 2 填入阈值后本用例自动开始阻断，无需改动任何 workflow。"
        )

    metrics_path = _BACKEND_DIR / baseline["metrics_path"]
    assert metrics_path.exists(), (
        f"baseline.json 声明了阈值，却找不到实测指标 {metrics_path}。"
        "评测没跑或产物路径不对——两种情况都必须阻断，不得跳过。"
    )
    metrics: dict[str, float] = json.loads(metrics_path.read_text(encoding="utf-8"))

    failures: list[str] = []
    for name, bound in thresholds.items():
        if name not in metrics:
            failures.append(f"{name}: 有阈值但无实测值")
            continue
        value = metrics[name]
        if "min" in bound and value < bound["min"]:
            failures.append(f"{name}={value:.4f} 低于下界 {bound['min']}")
        if "max" in bound and value > bound["max"]:
            failures.append(f"{name}={value:.4f} 高于上界 {bound['max']}")

    assert not failures, "评测门禁未通过:\n  " + "\n  ".join(failures)
