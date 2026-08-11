import { describe, expect, it } from "vitest";

import * as gradingModule from "@/grading/configSchemas";
import {
  CAPABILITY_STATUSES,
  DETECTOR_KINDS,
  DISPOSITIONS,
  INVESTIGATION_OUTCOMES,
  RULE_KINDS,
  calculateDispositionCell,
  generateDispositionMatrix,
  gradingConfigSchema,
  type GradingCostParameters,
} from "@/grading/configSchemas";
import {
  CAPABILITY_STATUS_PRESENTATION,
  CONFIDENCE_PRESENTATION,
  DETECTOR_KIND_PRESENTATION,
  DISPOSITION_PRESENTATION,
  IMPACT_PRESENTATION,
  INVESTIGATION_OUTCOME_PRESENTATION,
  RULE_KIND_PRESENTATION,
} from "@/grading/presentation";

const costParameters = {
  impact_cost_units: [0, 100, 1_000, 10_000],
  confidence_issue_probability_bps: [0, 1_000, 5_000, 9_000],
  false_negative_multiplier_bps: 10_000,
  false_positive_cost_units: 100,
  manual_review_cost_units: 50,
} as const satisfies GradingCostParameters;

function validConfig() {
  return {
    schema_version: 1,
    algorithm_version: "cost-matrix-v1",
    impact_by_rule_kind: {
      limit: 3,
      invoice_type: 2,
      timeliness: 2,
      invoice_title: 3,
      invoice_duplicate: 3,
    },
    impact_by_detector: {
      split_invoice: 3,
      sequential_invoice: 2,
      frequency_anomaly: 2,
      spatiotemporal_tier0: 3,
    },
    confidence_by_investigation_outcome: {
      sufficient: 3,
      insufficient: 1,
      unavailable: 0,
      max_steps: 0,
      failed: 0,
      not_run: 0,
    },
    capability_confidence_cap: {
      enabled: 3,
      degraded: 2,
      unavailable: 0,
    },
    ...costParameters,
    disposition_matrix: generateDispositionMatrix(costParameters),
  } as const;
}

describe("gradingConfigSchema", () => {
  it("接受包含全部映射和机械生成 16 格 matrix 的显式配置", () => {
    const parsed = gradingConfigSchema.parse(validConfig());

    expect(parsed.disposition_matrix.flat()).toHaveLength(16);
    expect(Object.keys(parsed.impact_by_rule_kind).sort()).toEqual([...RULE_KINDS].sort());
    expect(Object.keys(parsed.impact_by_detector).sort()).toEqual([...DETECTOR_KINDS].sort());
    expect(Object.keys(parsed.confidence_by_investigation_outcome).sort()).toEqual(
      [...INVESTIGATION_OUTCOMES].sort(),
    );
    expect(Object.keys(parsed.capability_confidence_cap).sort()).toEqual(
      [...CAPABILITY_STATUSES].sort(),
    );
  });

  it("用 bigint、保守破平顺序和两项通用安全覆盖生成 matrix", () => {
    const tieParameters = {
      impact_cost_units: [0, 10_000, 20_000, 30_000],
      confidence_issue_probability_bps: [0, 1_000, 5_000, 9_000],
      false_negative_multiplier_bps: 1,
      false_positive_cost_units: 2,
      manual_review_cost_units: 1,
    } as const satisfies GradingCostParameters;
    const tied = calculateDispositionCell(tieParameters, 2, 2);
    expect([tied.clearLoss, tied.flagLoss, tied.reviewLoss]).toEqual([
      100_000_000n,
      100_000_000n,
      100_000_000n,
    ]);
    expect(tied.selectedDisposition).toBe("high_attention");

    const clearFirst = {
      impact_cost_units: [0, 1, 2, 3],
      confidence_issue_probability_bps: [0, 1, 2, 3],
      false_negative_multiplier_bps: 1,
      false_positive_cost_units: 1_000_000,
      manual_review_cost_units: 1_000_000,
    } as const satisfies GradingCostParameters;
    expect(calculateDispositionCell(clearFirst, 1, 0)).toMatchObject({
      selectedDisposition: "cleared",
      disposition: "manual_attention",
    });
    expect(calculateDispositionCell(clearFirst, 3, 2)).toMatchObject({
      selectedDisposition: "cleared",
      disposition: "manual_attention",
    });
    const matrix = generateDispositionMatrix(clearFirst);
    expect(matrix.flat()).toHaveLength(16);
    expect(matrix.every((row) => row[0] !== "cleared")).toBe(true);
    expect(matrix[3].every((disposition) => disposition !== "cleared")).toBe(true);
  });

  it("在最大合法参数下保持 signed 64-bit，并保留 bigint 精度", () => {
    const boundary = {
      impact_cost_units: [0, 999_998, 999_999, 1_000_000],
      confidence_issue_probability_bps: [0, 9_998, 9_999, 10_000],
      false_negative_multiplier_bps: 1_000_000,
      false_positive_cost_units: 1_000_000,
      manual_review_cost_units: 1_000_000,
    } as const satisfies GradingCostParameters;
    const cell = calculateDispositionCell(boundary, 3, 3);

    expect(cell.clearLoss).toBe(10_000_000_000_000_000n);
    expect(cell.clearLoss).toBeLessThanOrEqual(9_223_372_036_854_775_807n);
    const config = { ...validConfig(), ...boundary };
    expect(
      gradingConfigSchema.safeParse({
        ...config,
        disposition_matrix: generateDispositionMatrix(boundary),
      }).success,
    ).toBe(true);
  });

  it("拒绝客户端提交的任意 matrix 漂移", () => {
    const config = validConfig();
    const dispositionMatrix = config.disposition_matrix.map((row, impact) =>
      row.map((disposition, confidence) =>
        impact === 1 && confidence === 1
          ? disposition === "cleared"
            ? "high_attention"
            : "cleared"
          : disposition,
      ),
    );

    expect(
      gradingConfigSchema.safeParse({ ...config, disposition_matrix: dispositionMatrix }).success,
    ).toBe(false);
  });

  it.each([
    [
      "severity bool",
      {
        ...validConfig(),
        impact_by_rule_kind: { ...validConfig().impact_by_rule_kind, limit: true },
      },
    ],
    [
      "severity float",
      {
        ...validConfig(),
        impact_by_rule_kind: { ...validConfig().impact_by_rule_kind, limit: 1.5 },
      },
    ],
    [
      "severity string",
      {
        ...validConfig(),
        impact_by_rule_kind: { ...validConfig().impact_by_rule_kind, limit: "1" },
      },
    ],
    [
      "not_run nonzero",
      {
        ...validConfig(),
        confidence_by_investigation_outcome: {
          ...validConfig().confidence_by_investigation_outcome,
          not_run: 1,
        },
      },
    ],
    [
      "degraded cap",
      {
        ...validConfig(),
        capability_confidence_cap: { ...validConfig().capability_confidence_cap, degraded: 3 },
      },
    ],
    [
      "unavailable cap",
      {
        ...validConfig(),
        capability_confidence_cap: { ...validConfig().capability_confidence_cap, unavailable: 1 },
      },
    ],
    ["cost bool", { ...validConfig(), false_positive_cost_units: true }],
    ["cost float", { ...validConfig(), false_positive_cost_units: 1.5 }],
    ["cost string", { ...validConfig(), false_positive_cost_units: "100" }],
    ["cost zero", { ...validConfig(), manual_review_cost_units: 0 }],
    ["cost over max", { ...validConfig(), false_negative_multiplier_bps: 1_000_001 }],
    [
      "probability bool",
      { ...validConfig(), confidence_issue_probability_bps: [0, true, 5_000, 9_000] },
    ],
    [
      "probability float",
      { ...validConfig(), confidence_issue_probability_bps: [0, 1.5, 5_000, 9_000] },
    ],
    [
      "probability string",
      { ...validConfig(), confidence_issue_probability_bps: [0, "1000", 5_000, 9_000] },
    ],
    [
      "probability over max",
      { ...validConfig(), confidence_issue_probability_bps: [0, 1_000, 5_000, 10_001] },
    ],
    ["cost not increasing", { ...validConfig(), impact_cost_units: [0, 100, 100, 10_000] }],
    [
      "probability not zero-based",
      { ...validConfig(), confidence_issue_probability_bps: [1, 1_000, 5_000, 9_000] },
    ],
    [
      "extra rule",
      {
        ...validConfig(),
        impact_by_rule_kind: { ...validConfig().impact_by_rule_kind, invented: 1 },
      },
    ],
    [
      "missing detector",
      {
        ...validConfig(),
        impact_by_detector: { split_invoice: 3, sequential_invoice: 2, frequency_anomaly: 2 },
      },
    ],
  ])("拒绝非法配置：%s", (_label, candidate) => {
    expect(gradingConfigSchema.safeParse(candidate).success).toBe(false);
  });

  it("缺少显式配置或 matrix 时失败，且模块不导出业务默认配置", () => {
    const { disposition_matrix: _matrix, ...withoutMatrix } = validConfig();

    expect(gradingConfigSchema.safeParse(undefined).success).toBe(false);
    expect(gradingConfigSchema.safeParse(withoutMatrix).success).toBe(false);
    expect("default" in gradingModule).toBe(false);
  });
});

describe("grading presentation maps", () => {
  it("为全部配置维度和 disposition 提供中文标签与颜色", () => {
    const maps = [
      [RULE_KINDS, RULE_KIND_PRESENTATION],
      [DETECTOR_KINDS, DETECTOR_KIND_PRESENTATION],
      [INVESTIGATION_OUTCOMES, INVESTIGATION_OUTCOME_PRESENTATION],
      [CAPABILITY_STATUSES, CAPABILITY_STATUS_PRESENTATION],
      [DISPOSITIONS, DISPOSITION_PRESENTATION],
    ] as const;

    for (const [keys, presentation] of maps) {
      expect(Object.keys(presentation).sort()).toEqual([...keys].sort());
      expect(Object.values(presentation).every(({ label, color }) => label && color)).toBe(true);
    }
    expect(Object.keys(IMPACT_PRESENTATION)).toEqual(["0", "1", "2", "3"]);
    expect(Object.keys(CONFIDENCE_PRESENTATION)).toEqual(["0", "1", "2", "3"]);
  });
});
