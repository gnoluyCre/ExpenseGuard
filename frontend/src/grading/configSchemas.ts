import { z } from "zod";

export const RULE_KINDS = [
  "limit",
  "invoice_type",
  "timeliness",
  "invoice_title",
  "invoice_duplicate",
] as const;

export const DETECTOR_KINDS = [
  "split_invoice",
  "sequential_invoice",
  "frequency_anomaly",
  "spatiotemporal_tier0",
] as const;

export const INVESTIGATION_OUTCOMES = [
  "sufficient",
  "insufficient",
  "unavailable",
  "max_steps",
  "failed",
  "not_run",
] as const;

export const CAPABILITY_STATUSES = ["enabled", "degraded", "unavailable"] as const;

export const DISPOSITIONS = ["high_attention", "manual_attention", "cleared"] as const;

export type RuleKind = (typeof RULE_KINDS)[number];
export type DetectorKind = (typeof DETECTOR_KINDS)[number];
export type InvestigationOutcome = (typeof INVESTIGATION_OUTCOMES)[number];
export type CapabilityStatus = (typeof CAPABILITY_STATUSES)[number];
export type Disposition = (typeof DISPOSITIONS)[number];
export type SeverityLevel = 0 | 1 | 2 | 3;

export type LevelVector = readonly [number, number, number, number];
export type DispositionRow = readonly [Disposition, Disposition, Disposition, Disposition];
export type DispositionMatrix = readonly [
  DispositionRow,
  DispositionRow,
  DispositionRow,
  DispositionRow,
];

export interface GradingCostParameters {
  readonly impact_cost_units: LevelVector;
  readonly confidence_issue_probability_bps: LevelVector;
  readonly false_negative_multiplier_bps: number;
  readonly false_positive_cost_units: number;
  readonly manual_review_cost_units: number;
}

export interface MatrixCellCalculation {
  readonly impact: SeverityLevel;
  readonly confidence: SeverityLevel;
  readonly clearLoss: bigint;
  readonly flagLoss: bigint;
  readonly reviewLoss: bigint;
  readonly selectedDisposition: Disposition;
  readonly disposition: Disposition;
}

const LEVELS = [0, 1, 2, 3] as const satisfies readonly SeverityLevel[];
const TEN_THOUSAND = 10_000n;
const SIGNED_64_MAX = 9_223_372_036_854_775_807n;

const severityLevelSchema = z.union([z.literal(0), z.literal(1), z.literal(2), z.literal(3)]);
const costUnitSchema = z.number().int().min(0).max(1_000_000);
const positiveCostUnitSchema = z.number().int().min(1).max(1_000_000);
const basisPointsSchema = z.number().int().min(0).max(10_000);

const impactByRuleKindSchema = z
  .object({
    limit: severityLevelSchema,
    invoice_type: severityLevelSchema,
    timeliness: severityLevelSchema,
    invoice_title: severityLevelSchema,
    invoice_duplicate: severityLevelSchema,
  })
  .strict();

const impactByDetectorSchema = z
  .object({
    split_invoice: severityLevelSchema,
    sequential_invoice: severityLevelSchema,
    frequency_anomaly: severityLevelSchema,
    spatiotemporal_tier0: severityLevelSchema,
  })
  .strict();

const confidenceByInvestigationOutcomeSchema = z
  .object({
    sufficient: severityLevelSchema,
    insufficient: severityLevelSchema,
    unavailable: severityLevelSchema,
    max_steps: severityLevelSchema,
    failed: severityLevelSchema,
    not_run: z.literal(0),
  })
  .strict();

const capabilityConfidenceCapSchema = z
  .object({
    enabled: severityLevelSchema,
    degraded: severityLevelSchema.refine((value) => value <= 2, "degraded cap 不得高于 2"),
    unavailable: z.literal(0),
  })
  .strict();

const levelCostVectorSchema = z.tuple([
  costUnitSchema,
  costUnitSchema,
  costUnitSchema,
  costUnitSchema,
]);
const probabilityVectorSchema = z.tuple([
  basisPointsSchema,
  basisPointsSchema,
  basisPointsSchema,
  basisPointsSchema,
]);
const dispositionSchema = z.enum(DISPOSITIONS);
const dispositionRowSchema = z.tuple([
  dispositionSchema,
  dispositionSchema,
  dispositionSchema,
  dispositionSchema,
]);
const dispositionMatrixSchema = z.tuple([
  dispositionRowSchema,
  dispositionRowSchema,
  dispositionRowSchema,
  dispositionRowSchema,
]);

function isStrictlyIncreasing(values: LevelVector): boolean {
  return values.every((value, index) => index === 0 || value > values[index - 1]!);
}

function matricesMatch(left: DispositionMatrix, right: DispositionMatrix): boolean {
  return LEVELS.every((impact) =>
    LEVELS.every((confidence) => left[impact][confidence] === right[impact][confidence]),
  );
}

export function calculateDispositionCell(
  config: GradingCostParameters,
  impact: SeverityLevel,
  confidence: SeverityLevel,
): MatrixCellCalculation {
  const probability = BigInt(config.confidence_issue_probability_bps[confidence]);
  const clearLoss =
    BigInt(config.impact_cost_units[impact]) *
    BigInt(config.false_negative_multiplier_bps) *
    probability;
  const flagLoss =
    BigInt(config.false_positive_cost_units) * TEN_THOUSAND * (TEN_THOUSAND - probability);
  const reviewLoss = BigInt(config.manual_review_cost_units) * TEN_THOUSAND * TEN_THOUSAND;

  // 遍历顺序即保守破平优先级：high > manual > clear；只在严格更小时替换。
  let selectedDisposition: Disposition = "high_attention";
  let selectedLoss = flagLoss;
  if (reviewLoss < selectedLoss) {
    selectedDisposition = "manual_attention";
    selectedLoss = reviewLoss;
  }
  if (clearLoss < selectedLoss) {
    selectedDisposition = "cleared";
  }

  let disposition = selectedDisposition;
  if (disposition === "cleared" && impact === 3) {
    disposition = "manual_attention";
  }
  if (disposition === "cleared" && confidence === 0) {
    disposition = "manual_attention";
  }

  return {
    impact,
    confidence,
    clearLoss,
    flagLoss,
    reviewLoss,
    selectedDisposition,
    disposition,
  };
}

export function generateDispositionMatrix(config: GradingCostParameters): DispositionMatrix {
  const row = (impact: SeverityLevel): DispositionRow => [
    calculateDispositionCell(config, impact, 0).disposition,
    calculateDispositionCell(config, impact, 1).disposition,
    calculateDispositionCell(config, impact, 2).disposition,
    calculateDispositionCell(config, impact, 3).disposition,
  ];
  return [row(0), row(1), row(2), row(3)];
}

const gradingConfigShapeSchema = z
  .object({
    schema_version: z.literal(1),
    algorithm_version: z.literal("cost-matrix-v1"),
    impact_by_rule_kind: impactByRuleKindSchema,
    impact_by_detector: impactByDetectorSchema,
    confidence_by_investigation_outcome: confidenceByInvestigationOutcomeSchema,
    capability_confidence_cap: capabilityConfidenceCapSchema,
    impact_cost_units: levelCostVectorSchema,
    confidence_issue_probability_bps: probabilityVectorSchema,
    false_negative_multiplier_bps: positiveCostUnitSchema,
    false_positive_cost_units: positiveCostUnitSchema,
    manual_review_cost_units: positiveCostUnitSchema,
    disposition_matrix: dispositionMatrixSchema,
  })
  .strict();

export const gradingConfigSchema = gradingConfigShapeSchema.superRefine((value, context) => {
  if (value.impact_cost_units[0] !== 0 || !isStrictlyIncreasing(value.impact_cost_units)) {
    context.addIssue({
      code: "custom",
      path: ["impact_cost_units"],
      message: "impact cost 必须从 0 开始并严格递增",
    });
  }
  if (
    value.confidence_issue_probability_bps[0] !== 0 ||
    !isStrictlyIncreasing(value.confidence_issue_probability_bps)
  ) {
    context.addIssue({
      code: "custom",
      path: ["confidence_issue_probability_bps"],
      message: "confidence probability 必须从 0 开始并严格递增",
    });
  }

  const cells = LEVELS.flatMap((impact) =>
    LEVELS.map((confidence) => calculateDispositionCell(value, impact, confidence)),
  );
  if (
    cells.some(
      ({ clearLoss, flagLoss, reviewLoss }) =>
        clearLoss > SIGNED_64_MAX || flagLoss > SIGNED_64_MAX || reviewLoss > SIGNED_64_MAX,
    )
  ) {
    context.addIssue({
      code: "custom",
      path: ["disposition_matrix"],
      message: "matrix loss 超出 signed 64-bit 范围",
    });
  }

  const generated = generateDispositionMatrix(value);
  if (!matricesMatch(value.disposition_matrix, generated)) {
    context.addIssue({
      code: "custom",
      path: ["disposition_matrix"],
      message: "disposition matrix 与机械生成结果不一致",
    });
  }
});

export type ValidGradingConfig = z.infer<typeof gradingConfigSchema>;
