import type {
  CapabilityStatus,
  DetectorKind,
  Disposition,
  InvestigationOutcome,
  RuleKind,
  SeverityLevel,
} from "@/grading/configSchemas";

export interface PresentationToken {
  readonly label: string;
  readonly color: string;
}

export const RULE_KIND_PRESENTATION = {
  limit: { label: "限额", color: "#b45309" },
  invoice_type: { label: "票种", color: "#7c3aed" },
  timeliness: { label: "时效", color: "#0369a1" },
  invoice_title: { label: "抬头", color: "#be123c" },
  invoice_duplicate: { label: "发票号查重", color: "#9f1239" },
} as const satisfies Record<RuleKind, PresentationToken>;

export const DETECTOR_KIND_PRESENTATION = {
  split_invoice: { label: "拆票", color: "#c2410c" },
  sequential_invoice: { label: "连号发票", color: "#a21caf" },
  frequency_anomaly: { label: "频次异常", color: "#1d4ed8" },
  spatiotemporal_tier0: { label: "时空冲突", color: "#be123c" },
} as const satisfies Record<DetectorKind, PresentationToken>;

export const INVESTIGATION_OUTCOME_PRESENTATION = {
  sufficient: { label: "证据充分", color: "#047857" },
  insufficient: { label: "证据不足", color: "#b45309" },
  unavailable: { label: "调查不可用", color: "#475569" },
  max_steps: { label: "达到步数上限", color: "#9a3412" },
  failed: { label: "调查失败", color: "#be123c" },
  not_run: { label: "未调查", color: "#64748b" },
} as const satisfies Record<InvestigationOutcome, PresentationToken>;

export const CAPABILITY_STATUS_PRESENTATION = {
  enabled: { label: "能力可用", color: "#047857" },
  degraded: { label: "能力降级", color: "#b45309" },
  unavailable: { label: "能力不可用", color: "#be123c" },
} as const satisfies Record<CapabilityStatus, PresentationToken>;

export const DISPOSITION_PRESENTATION = {
  high_attention: { label: "高关注", color: "#be123c" },
  manual_attention: { label: "人工关注", color: "#b45309" },
  cleared: { label: "放行", color: "#047857" },
} as const satisfies Record<Disposition, PresentationToken>;

export const IMPACT_PRESENTATION = {
  0: { label: "影响 0", color: "#64748b" },
  1: { label: "影响 1", color: "#0369a1" },
  2: { label: "影响 2", color: "#b45309" },
  3: { label: "影响 3", color: "#be123c" },
} as const satisfies Record<SeverityLevel, PresentationToken>;

export const CONFIDENCE_PRESENTATION = {
  0: { label: "置信 0", color: "#64748b" },
  1: { label: "置信 1", color: "#7c3aed" },
  2: { label: "置信 2", color: "#0369a1" },
  3: { label: "置信 3", color: "#047857" },
} as const satisfies Record<SeverityLevel, PresentationToken>;
