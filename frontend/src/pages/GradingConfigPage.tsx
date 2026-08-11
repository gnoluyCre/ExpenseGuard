import { useMemo, useState } from "react";
import {
  AlertTriangle,
  ArrowRight,
  Fingerprint,
  History,
  RefreshCw,
  Save,
  ShieldCheck,
} from "lucide-react";

import { hasPermission, PERMISSIONS } from "@/api/client";
import { useCurrentUser } from "@/auth/useAuth";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import {
  CAPABILITY_STATUSES,
  calculateDispositionCell,
  DETECTOR_KINDS,
  generateDispositionMatrix,
  gradingConfigSchema,
  INVESTIGATION_OUTCOMES,
  RULE_KINDS,
  type CapabilityStatus,
  type DetectorKind,
  type Disposition,
  type InvestigationOutcome,
  type RuleKind,
  type SeverityLevel,
  type ValidGradingConfig,
} from "@/grading/configSchemas";
import {
  GradingConfigApiError,
  useCreateGradingConfig,
  useCurrentGradingConfig,
  useGradingConfigHistory,
} from "@/grading/useGradingConfig";
import { cn } from "@/lib/utils";

const LEVELS = [0, 1, 2, 3] as const satisfies readonly SeverityLevel[];
const PAGE_SIZE = 20;

const RULE_LABELS: Record<RuleKind, string> = {
  limit: "限额",
  invoice_type: "票种",
  timeliness: "时效",
  invoice_title: "抬头",
  invoice_duplicate: "发票号查重",
};

const DETECTOR_LABELS: Record<DetectorKind, string> = {
  split_invoice: "拆单候选",
  sequential_invoice: "发票连号",
  frequency_anomaly: "频次离群",
  spatiotemporal_tier0: "时空冲突 Tier 0",
};

const OUTCOME_LABELS: Record<InvestigationOutcome, string> = {
  sufficient: "证据充分",
  insufficient: "证据不足",
  unavailable: "F7 能力不可用",
  max_steps: "达到步数上限",
  failed: "执行失败",
  not_run: "未执行 F7",
};

const CAPABILITY_LABELS: Record<CapabilityStatus, string> = {
  enabled: "能力正常",
  degraded: "能力降级",
  unavailable: "F6 能力不可用",
};

const DISPOSITION_LABELS: Record<Disposition, string> = {
  high_attention: "高关注",
  manual_attention: "人工复核",
  cleared: "放行",
};

interface Draft {
  rules: Record<RuleKind, string>;
  detectors: Record<DetectorKind, string>;
  outcomes: Record<InvestigationOutcome, string>;
  caps: Record<CapabilityStatus, string>;
  impactCosts: [string, string, string, string];
  probabilities: [string, string, string, string];
  falseNegativeMultiplier: string;
  falsePositiveCost: string;
  manualReviewCost: string;
}

function blankDraft(): Draft {
  return {
    rules: Object.fromEntries(RULE_KINDS.map((kind) => [kind, ""])) as Record<RuleKind, string>,
    detectors: Object.fromEntries(DETECTOR_KINDS.map((kind) => [kind, ""])) as Record<
      DetectorKind,
      string
    >,
    outcomes: Object.fromEntries(
      INVESTIGATION_OUTCOMES.map((outcome) => [outcome, outcome === "not_run" ? "0" : ""]),
    ) as Record<InvestigationOutcome, string>,
    caps: Object.fromEntries(
      CAPABILITY_STATUSES.map((status) => [status, status === "unavailable" ? "0" : ""]),
    ) as Record<CapabilityStatus, string>,
    impactCosts: ["", "", "", ""],
    probabilities: ["", "", "", ""],
    falseNegativeMultiplier: "",
    falsePositiveCost: "",
    manualReviewCost: "",
  };
}

function draftFromDefinition(input: unknown): Draft | null {
  const parsed = gradingConfigSchema.safeParse(input);
  if (!parsed.success) return null;
  const definition = parsed.data;
  return {
    rules: Object.fromEntries(
      RULE_KINDS.map((kind) => [kind, String(definition.impact_by_rule_kind[kind])]),
    ) as Record<RuleKind, string>,
    detectors: Object.fromEntries(
      DETECTOR_KINDS.map((kind) => [kind, String(definition.impact_by_detector[kind])]),
    ) as Record<DetectorKind, string>,
    outcomes: Object.fromEntries(
      INVESTIGATION_OUTCOMES.map((outcome) => [
        outcome,
        String(definition.confidence_by_investigation_outcome[outcome]),
      ]),
    ) as Record<InvestigationOutcome, string>,
    caps: Object.fromEntries(
      CAPABILITY_STATUSES.map((status) => [
        status,
        String(definition.capability_confidence_cap[status]),
      ]),
    ) as Record<CapabilityStatus, string>,
    impactCosts: definition.impact_cost_units.map(String) as Draft["impactCosts"],
    probabilities: definition.confidence_issue_probability_bps.map(
      String,
    ) as Draft["probabilities"],
    falseNegativeMultiplier: String(definition.false_negative_multiplier_bps),
    falsePositiveCost: String(definition.false_positive_cost_units),
    manualReviewCost: String(definition.manual_review_cost_units),
  };
}

function integer(value: string): number | null {
  return /^(0|[1-9]\d*)$/.test(value) ? Number(value) : null;
}

function buildDefinition(draft: Draft) {
  const values = [
    ...Object.values(draft.rules),
    ...Object.values(draft.detectors),
    ...Object.values(draft.outcomes),
    ...Object.values(draft.caps),
    ...draft.impactCosts,
    ...draft.probabilities,
    draft.falseNegativeMultiplier,
    draft.falsePositiveCost,
    draft.manualReviewCost,
  ];
  if (values.some((value) => integer(value) === null)) return null;

  const numberRecord = <K extends string>(record: Record<K, string>): Record<K, number> =>
    Object.fromEntries(
      Object.entries(record).map(([key, value]) => [key, Number(value)]),
    ) as Record<K, number>;
  const impactCosts = draft.impactCosts.map(Number) as [number, number, number, number];
  const probabilities = draft.probabilities.map(Number) as [number, number, number, number];
  const costs = {
    impact_cost_units: impactCosts,
    confidence_issue_probability_bps: probabilities,
    false_negative_multiplier_bps: Number(draft.falseNegativeMultiplier),
    false_positive_cost_units: Number(draft.falsePositiveCost),
    manual_review_cost_units: Number(draft.manualReviewCost),
  };
  const candidate = {
    schema_version: 1,
    algorithm_version: "cost-matrix-v1",
    impact_by_rule_kind: numberRecord(draft.rules),
    impact_by_detector: numberRecord(draft.detectors),
    confidence_by_investigation_outcome: numberRecord(draft.outcomes),
    capability_confidence_cap: numberRecord(draft.caps),
    ...costs,
    disposition_matrix: generateDispositionMatrix(costs),
  };
  return gradingConfigSchema.safeParse(candidate);
}

function formatDateTime(value: string): string {
  return new Intl.DateTimeFormat("zh-CN", { dateStyle: "medium", timeStyle: "short" }).format(
    new Date(value),
  );
}

export function GradingConfigPage() {
  const { data: user } = useCurrentUser();
  const canWrite = user ? hasPermission(user, PERMISSIONS.configWrite) : false;
  const [historyOffset, setHistoryOffset] = useState(0);
  const currentQuery = useCurrentGradingConfig();
  const historyQuery = useGradingConfigHistory(PAGE_SIZE, historyOffset);
  const createConfig = useCreateGradingConfig();
  const [draft, setDraft] = useState<Draft>(blankDraft);
  const [reason, setReason] = useState("");
  const [message, setMessage] = useState<string | null>(null);
  const [conflict, setConflict] = useState(false);
  const validation = useMemo(() => buildDefinition(draft), [draft]);
  const current = currentQuery.data ?? null;
  const isLoading = currentQuery.isLoading || historyQuery.isLoading;
  const loadError = currentQuery.error ?? historyQuery.error;

  function copyCurrent(): void {
    if (!current) return;
    const nextDraft = draftFromDefinition(current.definition);
    if (!nextDraft) {
      setMessage("current 配置未通过本地严格 schema 校验，无法作为新版本起点");
      return;
    }
    setDraft(nextDraft);
    setMessage(`已将 current v${current.version} 复制为未保存草案`);
    setConflict(false);
  }

  function save(): void {
    setMessage(null);
    setConflict(false);
    if (!validation?.success) {
      setMessage(
        validation?.error.issues[0]?.message ?? "请完整填写所有字段，并修正二维分级配置校验错误",
      );
      return;
    }
    createConfig.mutate(
      {
        expected_current_version: current?.version ?? 0,
        definition: validation.data,
        change_reason: reason.trim(),
      },
      {
        onSuccess: (result) => {
          setMessage(
            result.reused_existing
              ? `幂等重放：已复用配置 v${result.version}`
              : `已追加不可变配置 v${result.version}`,
          );
          setReason("");
        },
        onError: (error) => {
          const isConflict = error instanceof GradingConfigApiError && error.status === 409;
          setConflict(isConflict);
          setMessage(
            isConflict
              ? "版本冲突：current 已被其他人更新。请刷新并重新以最新版本为起点。"
              : error.message,
          );
        },
      },
    );
  }

  return (
    <div className="grid gap-5">
      <header className="flex items-end justify-between gap-6 border-b border-border/70 pb-4">
        <div>
          <div className="mb-2 flex items-center gap-2 text-[11px] font-semibold tracking-[0.22em] text-muted-foreground">
            <ShieldCheck className="size-4 text-emerald-700" aria-hidden="true" />
            TWO-AXIS DECISION CONTROL
          </div>
          <h1 className="text-2xl font-semibold tracking-tight">二维分级配置</h1>
          <p className="mt-1 max-w-3xl text-sm text-muted-foreground">
            将影响程度与证据置信度分开映射，再用整数损失函数机械生成 16 格处置矩阵。
          </p>
        </div>
        <Button
          variant="outline"
          disabled={currentQuery.isFetching || historyQuery.isFetching}
          onClick={() => {
            void currentQuery.refetch();
            void historyQuery.refetch();
          }}
        >
          <RefreshCw aria-hidden="true" />
          刷新
        </Button>
      </header>

      {loadError ? (
        <div
          role="alert"
          className="rounded-lg border border-destructive/30 bg-destructive/5 p-4 text-sm text-destructive"
        >
          {loadError.message}
        </div>
      ) : null}
      {isLoading ? <p className="text-sm text-muted-foreground">正在读取二维分级控制面…</p> : null}

      {!isLoading && !loadError ? (
        <Card className="overflow-hidden border-slate-800 bg-slate-950 text-slate-50">
          <CardContent className="grid grid-cols-[0.45fr_0.8fr_1.55fr_0.9fr] gap-5 p-5">
            <Metric label="CURRENT" value={current ? `v${current.version}` : "未配置"} />
            <Metric label="ALGORITHM" value={current?.algorithm_version ?? "待显式配置"} mono />
            <Metric label="CONFIG FINGERPRINT" value={current?.config_fingerprint ?? "—"} mono />
            <Metric label="CREATED AT" value={current ? formatDateTime(current.created_at) : "—"} />
          </CardContent>
        </Card>
      ) : null}

      {!isLoading && !loadError && !current ? (
        <div className="flex items-start gap-3 rounded-lg border border-amber-300 bg-amber-50 p-4 text-sm text-amber-950">
          <AlertTriangle className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
          <div>
            <p className="font-semibold">尚无 current 配置</p>
            <p className="mt-1">
              系统不会代填任何业务默认。首次创建前必须显式输入全部映射、能力上限、成本与概率。
            </p>
          </div>
        </div>
      ) : null}

      <div className="grid grid-cols-[minmax(0,1.35fr)_minmax(390px,0.8fr)] gap-5">
        <div className="grid gap-5">
          <Card>
            <CardHeader className="flex-row items-start justify-between gap-4 border-b">
              <div>
                <CardTitle>下一版映射草案</CardTitle>
                <p className="mt-1 text-sm text-muted-foreground">所有值均为 0–3 的整数等级。</p>
              </div>
              {current && canWrite ? (
                <Button variant="outline" size="sm" onClick={copyCurrent}>
                  以 current v{current.version} 为起点
                  <ArrowRight aria-hidden="true" />
                </Button>
              ) : null}
            </CardHeader>
            <CardContent className="grid gap-6 pt-5">
              {canWrite ? (
                <>
                  <MappingSection
                    title="F3 RULE → IMPACT"
                    description="五类确定性规则的基础影响等级"
                  >
                    {RULE_KINDS.map((kind) => (
                      <LevelField
                        key={kind}
                        label={RULE_LABELS[kind]}
                        value={draft.rules[kind]}
                        onChange={(value) =>
                          setDraft((state) => ({
                            ...state,
                            rules: { ...state.rules, [kind]: value },
                          }))
                        }
                      />
                    ))}
                  </MappingSection>
                  <MappingSection
                    title="F6 DETECTOR → IMPACT"
                    description="四类跨行候选的基础影响等级"
                  >
                    {DETECTOR_KINDS.map((kind) => (
                      <LevelField
                        key={kind}
                        label={DETECTOR_LABELS[kind]}
                        value={draft.detectors[kind]}
                        onChange={(value) =>
                          setDraft((state) => ({
                            ...state,
                            detectors: { ...state.detectors, [kind]: value },
                          }))
                        }
                      />
                    ))}
                  </MappingSection>
                  <MappingSection
                    title="F7 OUTCOME → CONFIDENCE"
                    description="六种取证终态的置信等级"
                  >
                    {INVESTIGATION_OUTCOMES.map((outcome) => (
                      <LevelField
                        key={outcome}
                        label={OUTCOME_LABELS[outcome]}
                        value={draft.outcomes[outcome]}
                        fixed={outcome === "not_run"}
                        fixedValue="0"
                        onChange={(value) =>
                          setDraft((state) => ({
                            ...state,
                            outcomes: { ...state.outcomes, [outcome]: value },
                          }))
                        }
                      />
                    ))}
                  </MappingSection>
                  <MappingSection
                    title="CAPABILITY → CONFIDENCE CAP"
                    description="能力状态对置信等级施加硬上限"
                  >
                    {CAPABILITY_STATUSES.map((status) => (
                      <LevelField
                        key={status}
                        label={CAPABILITY_LABELS[status]}
                        value={draft.caps[status]}
                        fixed={status === "unavailable"}
                        fixedValue="0"
                        onChange={(value) =>
                          setDraft((state) => ({
                            ...state,
                            caps: { ...state.caps, [status]: value },
                          }))
                        }
                      />
                    ))}
                  </MappingSection>
                </>
              ) : (
                <ReadOnlyDefinition definition={current?.definition ?? null} />
              )}
            </CardContent>
          </Card>

          {canWrite ? (
            <Card>
              <CardHeader className="border-b">
                <CardTitle>整数成本与概率</CardTitle>
                <p className="text-sm text-muted-foreground">
                  全部损失使用 BigInt 机械计算，不经过浮点数；概率单位为 basis point。
                </p>
              </CardHeader>
              <CardContent className="grid gap-5 pt-5">
                <VectorFields
                  title="影响成本（level 0–3）"
                  values={draft.impactCosts}
                  onChange={(level, value) =>
                    setDraft((state) => ({
                      ...state,
                      impactCosts: replaceAt(state.impactCosts, level, value),
                    }))
                  }
                />
                <VectorFields
                  title="问题概率 bps（confidence 0–3）"
                  values={draft.probabilities}
                  onChange={(level, value) =>
                    setDraft((state) => ({
                      ...state,
                      probabilities: replaceAt(state.probabilities, level, value),
                    }))
                  }
                />
                <div className="grid grid-cols-3 gap-3">
                  <NumberField
                    label="漏判倍率 bps"
                    value={draft.falseNegativeMultiplier}
                    onChange={(value) =>
                      setDraft((state) => ({ ...state, falseNegativeMultiplier: value }))
                    }
                  />
                  <NumberField
                    label="误报成本单位"
                    value={draft.falsePositiveCost}
                    onChange={(value) =>
                      setDraft((state) => ({ ...state, falsePositiveCost: value }))
                    }
                  />
                  <NumberField
                    label="人工复核成本单位"
                    value={draft.manualReviewCost}
                    onChange={(value) =>
                      setDraft((state) => ({ ...state, manualReviewCost: value }))
                    }
                  />
                </div>
              </CardContent>
            </Card>
          ) : null}
        </div>

        <div className="grid content-start gap-5">
          <MatrixPreview validation={validation} />

          <Card>
            <CardHeader className="border-b">
              <CardTitle className="flex items-center gap-2">
                <History className="size-4" aria-hidden="true" />
                版本历史
              </CardTitle>
            </CardHeader>
            <CardContent className="grid gap-3 pt-4">
              {(historyQuery.data?.items ?? []).map((item) => (
                <article key={item.id} className="rounded-lg border p-3">
                  <div className="flex items-center justify-between gap-3">
                    <div className="flex items-center gap-2">
                      <span className="font-semibold">v{item.version}</span>
                      <Badge variant={item.id === current?.id ? "default" : "outline"}>
                        {item.id === current?.id ? "current" : "archived"}
                      </Badge>
                    </div>
                    <time className="text-xs text-muted-foreground">
                      {formatDateTime(item.created_at)}
                    </time>
                  </div>
                  <p className="mt-2 text-sm">{item.change_reason}</p>
                  <div className="mt-2 flex min-w-0 items-start gap-2 rounded bg-muted/50 p-2">
                    <Fingerprint className="mt-0.5 size-3.5 shrink-0" aria-hidden="true" />
                    <code className="break-all text-[10px]">{item.config_fingerprint}</code>
                  </div>
                </article>
              ))}
              {!historyQuery.isLoading && (historyQuery.data?.items.length ?? 0) === 0 ? (
                <p className="rounded-lg border border-dashed p-5 text-center text-sm text-muted-foreground">
                  尚无版本历史。
                </p>
              ) : null}
              {historyQuery.data ? (
                <div className="flex items-center justify-between border-t pt-3 text-xs text-muted-foreground">
                  <span>共 {historyQuery.data.total} 个版本</span>
                  <div className="flex gap-2">
                    <Button
                      size="sm"
                      variant="outline"
                      disabled={historyOffset === 0}
                      onClick={() => setHistoryOffset(Math.max(0, historyOffset - PAGE_SIZE))}
                    >
                      上一页
                    </Button>
                    <Button
                      size="sm"
                      variant="outline"
                      disabled={historyOffset + PAGE_SIZE >= historyQuery.data.total}
                      onClick={() => setHistoryOffset(historyOffset + PAGE_SIZE)}
                    >
                      下一页
                    </Button>
                  </div>
                </div>
              ) : null}
            </CardContent>
          </Card>
        </div>
      </div>

      <Card className="border-t-4 border-t-slate-900">
        <CardContent className="flex items-end gap-4 pt-5">
          {canWrite ? (
            <>
              <label className="grid min-w-0 flex-1 gap-1.5 text-sm font-medium">
                变更原因
                <Input
                  aria-label="变更原因"
                  maxLength={500}
                  value={reason}
                  onChange={(event) => setReason(event.target.value)}
                  placeholder="说明为什么需要这个不可变版本"
                />
              </label>
              <Button
                onClick={save}
                disabled={
                  createConfig.isPending || !validation?.success || reason.trim().length === 0
                }
              >
                <Save aria-hidden="true" />
                {createConfig.isPending ? "正在写入…" : "追加不可变版本"}
              </Button>
            </>
          ) : (
            <p className="text-sm text-muted-foreground">
              当前账号为只读视图；提交需要 config:write 权限。
            </p>
          )}
          {message ? (
            <p
              role={conflict || createConfig.isError ? "alert" : "status"}
              className={cn(
                "max-w-xl text-sm",
                conflict || createConfig.isError ? "text-destructive" : "text-muted-foreground",
              )}
            >
              {message}
            </p>
          ) : null}
        </CardContent>
      </Card>
    </div>
  );
}

function replaceAt(
  values: Draft["impactCosts"],
  index: SeverityLevel,
  value: string,
): Draft["impactCosts"] {
  const next = [...values] as Draft["impactCosts"];
  next[index] = value;
  return next;
}

function MappingSection({
  title,
  description,
  children,
}: {
  title: string;
  description: string;
  children: React.ReactNode;
}) {
  return (
    <section>
      <div className="mb-3 flex items-end justify-between border-b pb-2">
        <h2 className="font-mono text-xs font-semibold tracking-[0.12em]">{title}</h2>
        <p className="text-xs text-muted-foreground">{description}</p>
      </div>
      <div className="grid grid-cols-3 gap-3">{children}</div>
    </section>
  );
}

function LevelField({
  label,
  value,
  fixed = false,
  fixedValue,
  onChange,
}: {
  label: string;
  value: string;
  fixed?: boolean;
  fixedValue?: string;
  onChange: (value: string) => void;
}) {
  const displayed = fixed ? (value === "" ? (fixedValue ?? "0") : value) : value;
  return (
    <label className="grid gap-1.5 text-xs font-medium">
      {label}
      <select
        aria-label={label}
        value={displayed}
        disabled={fixed}
        onChange={(event) => onChange(event.target.value)}
        className="h-9 rounded-md border bg-background px-3 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-70"
      >
        <option value="">请选择</option>
        {LEVELS.map((level) => (
          <option key={level} value={String(level)}>
            Level {level}
          </option>
        ))}
      </select>
      {fixed ? <span className="font-normal text-muted-foreground">安全边界固定为 0</span> : null}
    </label>
  );
}

function VectorFields({
  title,
  values,
  onChange,
}: {
  title: string;
  values: Draft["impactCosts"];
  onChange: (level: SeverityLevel, value: string) => void;
}) {
  return (
    <fieldset>
      <legend className="mb-2 text-xs font-semibold tracking-wide text-muted-foreground">
        {title}
      </legend>
      <div className="grid grid-cols-4 gap-3">
        {LEVELS.map((level) => (
          <NumberField
            key={level}
            label={`${title} L${level}`}
            shortLabel={`L${level}`}
            value={values[level]}
            onChange={(value) => onChange(level, value)}
          />
        ))}
      </div>
    </fieldset>
  );
}

function NumberField({
  label,
  shortLabel,
  value,
  onChange,
}: {
  label: string;
  shortLabel?: string;
  value: string;
  onChange: (value: string) => void;
}) {
  return (
    <label className="grid gap-1.5 text-xs font-medium">
      {shortLabel ?? label}
      <Input
        aria-label={label}
        inputMode="numeric"
        value={value}
        onChange={(event) => onChange(event.target.value)}
        placeholder="显式输入"
      />
    </label>
  );
}

function MatrixPreview({ validation }: { validation: ReturnType<typeof buildDefinition> }) {
  if (!validation?.success) {
    return (
      <Card className="overflow-hidden">
        <CardHeader className="border-b bg-slate-950 text-slate-50">
          <CardTitle>4 × 4 决策矩阵</CardTitle>
        </CardHeader>
        <CardContent className="grid min-h-72 place-items-center p-8 text-center text-sm text-muted-foreground">
          <div>
            <p className="font-semibold text-foreground">等待完整且有效的参数</p>
            <p className="mt-2">矩阵不会使用隐式默认值生成。</p>
            {validation && !validation.success ? (
              <p role="alert" className="mt-3 text-destructive">
                {validation.error.issues[0]?.message}
              </p>
            ) : null}
          </div>
        </CardContent>
      </Card>
    );
  }

  const definition = validation.data;
  return (
    <Card className="overflow-hidden">
      <CardHeader className="border-b bg-slate-950 text-slate-50">
        <CardTitle>4 × 4 决策矩阵</CardTitle>
        <p className="text-xs text-slate-400">纵轴 impact · 横轴 confidence</p>
      </CardHeader>
      <CardContent className="p-4">
        <div className="grid grid-cols-[42px_repeat(4,1fr)] gap-1" aria-label="16格处置矩阵">
          <span />
          {LEVELS.map((confidence) => (
            <span
              key={confidence}
              className="py-1 text-center font-mono text-[10px] text-muted-foreground"
            >
              C{confidence}
            </span>
          ))}
          {LEVELS.map((impact) => (
            <MatrixRow key={impact} impact={impact} definition={definition} />
          ))}
        </div>
        <div className="mt-4 rounded-md border border-amber-300 bg-amber-50 p-3 text-xs text-amber-950">
          <p className="font-semibold">不可关闭的安全覆盖</p>
          <p className="mt-1">
            impact=3 或 confidence=0 时，机械结果不得为“放行”。平局优先级：高关注 → 人工复核 →
            放行。
          </p>
        </div>
      </CardContent>
    </Card>
  );
}

function MatrixRow({
  impact,
  definition,
}: {
  impact: SeverityLevel;
  definition: ValidGradingConfig;
}) {
  return (
    <>
      <span className="grid place-items-center font-mono text-[10px] text-muted-foreground">
        I{impact}
      </span>
      {LEVELS.map((confidence) => {
        const cell = calculateDispositionCell(definition, impact, confidence);
        return (
          <div
            key={confidence}
            title={`clear=${cell.clearLoss}; flag=${cell.flagLoss}; review=${cell.reviewLoss}`}
            className={cn(
              "grid min-h-16 place-items-center rounded border px-1 text-center text-[10px] font-semibold",
              dispositionClass(cell.disposition),
            )}
          >
            <span>{DISPOSITION_LABELS[cell.disposition]}</span>
          </div>
        );
      })}
    </>
  );
}

function dispositionClass(disposition: Disposition): string {
  if (disposition === "high_attention") return "border-rose-300 bg-rose-100 text-rose-950";
  if (disposition === "manual_attention") return "border-amber-300 bg-amber-100 text-amber-950";
  return "border-emerald-300 bg-emerald-100 text-emerald-950";
}

function ReadOnlyDefinition({ definition: input }: { definition: unknown }) {
  if (!input) return <p className="text-sm text-muted-foreground">当前没有可读取的配置。</p>;
  const parsed = gradingConfigSchema.safeParse(input);
  if (!parsed.success) {
    return (
      <p role="alert" className="text-sm text-destructive">
        current 配置未通过本地严格 schema 校验，请联系配置管理员。
      </p>
    );
  }
  const definition = parsed.data;
  return (
    <div className="grid grid-cols-2 gap-4 text-sm">
      <ReadOnlyMap
        title="F3 RULE → IMPACT"
        values={definition.impact_by_rule_kind}
        labels={RULE_LABELS}
      />
      <ReadOnlyMap
        title="F6 DETECTOR → IMPACT"
        values={definition.impact_by_detector}
        labels={DETECTOR_LABELS}
      />
      <ReadOnlyMap
        title="F7 OUTCOME → CONFIDENCE"
        values={definition.confidence_by_investigation_outcome}
        labels={OUTCOME_LABELS}
      />
      <ReadOnlyMap
        title="CAPABILITY → CAP"
        values={definition.capability_confidence_cap}
        labels={CAPABILITY_LABELS}
      />
    </div>
  );
}

function ReadOnlyMap<K extends string>({
  title,
  values,
  labels,
}: {
  title: string;
  values: Record<K, number>;
  labels: Record<K, string>;
}) {
  return (
    <section className="rounded-lg border p-3">
      <h3 className="mb-2 font-mono text-xs font-semibold">{title}</h3>
      {Object.entries(values).map(([key, value]) => (
        <div key={key} className="flex justify-between border-t py-1.5 text-xs first:border-t-0">
          <span>{labels[key as K]}</span>
          <span className="font-mono">L{String(value)}</span>
        </div>
      ))}
    </section>
  );
}

function Metric({ label, value, mono = false }: { label: string; value: string; mono?: boolean }) {
  return (
    <div className="min-w-0">
      <div className="text-[10px] font-semibold tracking-[0.12em] text-slate-400">{label}</div>
      <div className={cn("mt-1 break-all text-sm", mono && "font-mono text-xs")}>{value}</div>
    </div>
  );
}
