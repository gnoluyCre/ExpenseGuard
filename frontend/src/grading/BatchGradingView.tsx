import { useEffect, useMemo, useState } from "react";
import { AlertOctagon, ArrowUpRight, CircleDashed, Fingerprint, Play, Scale } from "lucide-react";

import {
  hasPermission,
  PERMISSIONS,
  type CurrentUser,
  type DetectorKind,
  type GradingDisposition,
  type GradingItem,
  type GradingSourceKind,
  type InvestigationGradingOutcome,
  type RuleKind,
} from "@/api/client";
import { useBatchValidation } from "@/batches/useBatchValidation";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { useBatchDetection } from "@/detection/useDetection";
import {
  GradingRequestError,
  type GradingFilters,
  useBatchGrading,
  useCreateBatchGrading,
  useGradingItem,
  useGradingItems,
  useGradingRows,
} from "@/grading/useBatchGrading";
import { cn } from "@/lib/utils";

const RULES = [
  "limit",
  "invoice_type",
  "timeliness",
  "invoice_title",
  "invoice_duplicate",
] as const;
const DETECTORS = [
  "split_invoice",
  "sequential_invoice",
  "frequency_anomaly",
  "spatiotemporal_tier0",
] as const;
const DISPOSITIONS = ["high_attention", "manual_attention", "cleared"] as const;
const F7_OUTCOMES = [
  "sufficient",
  "insufficient",
  "unavailable",
  "max_steps",
  "failed",
  "not_run",
] as const;

const RULE_LABELS: Record<RuleKind, string> = {
  limit: "限额",
  invoice_type: "票种",
  timeliness: "时效",
  invoice_title: "抬头",
  invoice_duplicate: "发票号查重",
};
const DETECTOR_LABELS: Record<DetectorKind, string> = {
  split_invoice: "拆票",
  sequential_invoice: "连号发票",
  frequency_anomaly: "频次异常",
  spatiotemporal_tier0: "时空冲突",
};
const DISPOSITION_LABELS: Record<GradingDisposition, string> = {
  high_attention: "高关注",
  manual_attention: "人工关注",
  cleared: "放行",
};
const F7_LABELS: Record<InvestigationGradingOutcome, string> = {
  sufficient: "证据充分",
  insufficient: "证据不足",
  unavailable: "调查不可用",
  max_steps: "达到步数上限",
  failed: "调查失败",
  not_run: "未调查",
};

interface FilterState {
  sourceKind: GradingSourceKind | "";
  ruleKind: RuleKind | "";
  detector: DetectorKind | "";
  impact: "" | "0" | "1" | "2" | "3";
  confidence: "" | "0" | "1" | "2" | "3";
  disposition: GradingDisposition | "";
  f7Outcome: InvestigationGradingOutcome | "";
}

const EMPTY_FILTERS: FilterState = {
  sourceKind: "",
  ruleKind: "",
  detector: "",
  impact: "",
  confidence: "",
  disposition: "",
  f7Outcome: "",
};

function dispositionClass(value: GradingDisposition): string {
  if (value === "high_attention") return "border-rose-300 bg-rose-50 text-rose-800";
  if (value === "manual_attention") return "border-amber-300 bg-amber-50 text-amber-900";
  return "border-emerald-300 bg-emerald-50 text-emerald-800";
}

function axisClass(level: number, axis: "impact" | "confidence"): string {
  if (level === 3)
    return axis === "impact" ? "bg-rose-700 text-white" : "bg-emerald-700 text-white";
  if (level === 2)
    return axis === "impact" ? "bg-orange-100 text-orange-900" : "bg-sky-100 text-sky-900";
  if (level === 1)
    return axis === "impact" ? "bg-amber-50 text-amber-900" : "bg-indigo-50 text-indigo-900";
  return "bg-slate-100 text-slate-600";
}

function shortFingerprint(value: string): string {
  return `${value.slice(0, 10)}…${value.slice(-6)}`;
}

function sourceLabel(item: GradingItem): string {
  if (item.source_kind === "deterministic") {
    return item.rule_kind ? `F3 · ${RULE_LABELS[item.rule_kind]}` : "F3 · 确定性规则";
  }
  return item.detector ? `F6 · ${DETECTOR_LABELS[item.detector]}` : "F6 · 关联候选";
}

function queryFilters(filters: FilterState, offset: number): GradingFilters {
  return {
    ...(filters.sourceKind ? { sourceKind: filters.sourceKind } : {}),
    ...(filters.ruleKind ? { ruleKind: filters.ruleKind } : {}),
    ...(filters.detector ? { detector: filters.detector } : {}),
    ...(filters.impact ? { severityImpact: Number(filters.impact) } : {}),
    ...(filters.confidence ? { severityConfidence: Number(filters.confidence) } : {}),
    ...(filters.disposition ? { disposition: filters.disposition } : {}),
    ...(filters.f7Outcome ? { f7Outcome: filters.f7Outcome } : {}),
    offset,
    limit: 50,
  };
}

export function BatchGradingView({
  fileVersionId,
  user,
}: {
  fileVersionId: string;
  user: CurrentUser;
}) {
  const canRun = hasPermission(user, PERMISSIONS.batchImport);
  const validation = useBatchValidation(fileVersionId);
  const detection = useBatchDetection(fileVersionId);
  const grading = useBatchGrading(fileVersionId);
  const createRun = useCreateBatchGrading(fileVersionId);
  const [filters, setFilters] = useState<FilterState>(EMPTY_FILTERS);
  const [offset, setOffset] = useState(0);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [rowOffset, setRowOffset] = useState(0);
  const [message, setMessage] = useState<string | null>(null);
  const runId = grading.data?.run?.id ?? null;
  const items = useGradingItems(runId, queryFilters(filters, offset));
  const detail = useGradingItem(selectedId);
  const rows = useGradingRows(selectedId, rowOffset);

  useEffect(() => {
    setOffset(0);
    setSelectedId(null);
    setRowOffset(0);
  }, [filters]);

  useEffect(() => {
    const first = items.data?.items[0]?.id ?? null;
    setSelectedId((current) =>
      current && items.data?.items.some((item) => item.id === current) ? current : first,
    );
  }, [items.data]);

  const readiness = useMemo(() => {
    const validationRunId = validation.data?.validation_run_id ?? null;
    const detectionRunId = detection.data?.run?.id ?? null;
    const gradingConfigId = grading.data?.current_config_id ?? null;
    return { validationRunId, detectionRunId, gradingConfigId };
  }, [detection.data, grading.data, validation.data]);

  const capabilities = detection.data?.capabilities ?? [];
  const degraded = capabilities.filter((item) => item.status === "degraded");
  const unavailable = capabilities.filter((item) => item.status === "unavailable");

  function run(): void {
    if (!readiness.validationRunId || !readiness.detectionRunId || !readiness.gradingConfigId)
      return;
    if (
      !window.confirm(
        "确认以当前 F3、F6 和分级配置创建不可变快照？全部 F6 候选将明确记录为 F7 未调查。",
      )
    )
      return;
    setMessage(null);
    createRun.mutate(
      {
        validationRunId: readiness.validationRunId,
        detectionRunId: readiness.detectionRunId,
        gradingConfigId: readiness.gradingConfigId,
      },
      {
        onSuccess: (result) =>
          setMessage(
            result.reused_existing ? "已复用完全相同的综合分级快照" : "综合分级快照已创建",
          ),
        onError: (error) => setMessage(error.message),
      },
    );
  }

  if (validation.isLoading || detection.isLoading || grading.isLoading) {
    return <StateMessage>正在对齐 F3、F6 与二维分级基准…</StateMessage>;
  }
  if (validation.isError) return <StateMessage error>{validation.error.message}</StateMessage>;
  if (detection.isError) return <StateMessage error>{detection.error.message}</StateMessage>;
  if (grading.isError) return <StateMessage error>{grading.error.message}</StateMessage>;
  if (!grading.data || !detection.data) return null;

  const runSnapshot = grading.data.run;
  const configMissing = grading.data.current_config_id === null;
  const sourcesMissing = !readiness.validationRunId || !readiness.detectionRunId;
  const isConflict =
    createRun.error instanceof GradingRequestError &&
    (createRun.error.code?.includes("CONFLICT") ?? false);

  return (
    <div className="grid gap-4 bg-[linear-gradient(135deg,rgba(15,23,42,0.025)_25%,transparent_25%,transparent_75%,rgba(15,23,42,0.025)_75%)] bg-[length:18px_18px] p-5">
      <header className="overflow-hidden rounded-xl border border-slate-800 bg-slate-950 text-slate-50 shadow-[0_16px_40px_-30px_rgba(15,23,42,0.9)]">
        <div className="grid grid-cols-[minmax(0,1fr)_auto] gap-5 p-5">
          <div>
            <div className="flex items-center gap-2 text-xs font-semibold tracking-[0.2em] text-cyan-300">
              <Scale className="size-4" aria-hidden="true" />
              TWO-AXIS GRADING CONTROL
            </div>
            <h2 className="mt-2 font-serif text-xl">综合分级 · 影响 × 置信</h2>
            <p className="mt-2 max-w-3xl text-sm leading-6 text-slate-300">
              F3 确定性发现与 F6
              统计候选在同一代价矩阵中分列评估；分级是不可变证据快照，不替代人工复核。
            </p>
          </div>
          <div className="flex items-start gap-2">
            {runSnapshot ? (
              <Badge variant="secondary">RUN {runSnapshot.id.slice(0, 8)}</Badge>
            ) : (
              <Badge variant="outline">RUN ABSENT</Badge>
            )}
            {canRun ? (
              <Button
                size="sm"
                onClick={run}
                disabled={createRun.isPending || configMissing || sourcesMissing}
              >
                <Play aria-hidden="true" />
                {createRun.isPending
                  ? "读取全量候选并分级…"
                  : runSnapshot
                    ? "重新分级 / 复用"
                    : "创建分级"}
              </Button>
            ) : (
              <span className="rounded border border-slate-700 px-2 py-1 text-xs text-slate-400">
                只读
              </span>
            )}
          </div>
        </div>
        <div className="grid grid-cols-3 border-t border-slate-800 bg-slate-900/70">
          <Baseline
            label="CONFIG 基准"
            stale={grading.data.config_stale}
            value={grading.data.current_config_id}
          />
          <Baseline
            label="F3 基准"
            stale={grading.data.validation_run_stale}
            value={grading.data.current_validation_run_id}
          />
          <Baseline
            label="F6 基准"
            stale={grading.data.detection_run_stale}
            value={grading.data.current_detection_run_id}
          />
        </div>
      </header>

      <div className="rounded-lg border border-cyan-200 bg-cyan-50 px-4 py-3 text-sm text-cyan-950">
        <strong>本次 manifest 策略：</strong>分页读取当前 F6 运行的全部候选（最多
        20,000），逐项明确写入
        <code className="mx-1 rounded bg-white/80 px-1 py-0.5 text-xs">
          not_run / INVESTIGATION_NOT_RUN
        </code>
        。系统不会隐式猜测调查结果；如需纳入已完成 F7，请使用后续显式选择流程重建快照。
      </div>

      {configMissing ? (
        <StateBanner tone="danger" title="缺少二维分级配置">
          配置管理员需先发布当前配置；在此之前不会构造默认阈值或代价矩阵。
        </StateBanner>
      ) : null}
      {sourcesMissing ? (
        <StateBanner tone="warning" title="上游运行尚未齐备">
          {!readiness.validationRunId ? "缺少 F3 校验运行。" : ""}
          {!readiness.detectionRunId ? "缺少 F6 关联检测运行。" : ""} 请先在对应视图显式执行。
        </StateBanner>
      ) : null}
      {degraded.length > 0 ? (
        <StateBanner tone="warning" title="部分 F6 能力降级">
          {degraded.map((item) => DETECTOR_LABELS[item.detector]).join("、")}
          ；分级会保留能力状态并应用置信上限。
        </StateBanner>
      ) : null}
      {unavailable.length > 0 ? (
        <StateBanner tone="danger" title="部分 F6 能力不可用">
          {unavailable.map((item) => DETECTOR_LABELS[item.detector]).join("、")}
          ；相关结果必须转人工，不会猜测结论。
        </StateBanner>
      ) : null}
      {runSnapshot &&
      (grading.data.config_stale ||
        grading.data.validation_run_stale ||
        grading.data.detection_run_stale) ? (
        <StateBanner tone="warning" title="当前快照已过期">
          上方三项基准中带 STALE 的来源已变化。历史结果仍可审计；具备权限的账号可显式重新分级。
        </StateBanner>
      ) : null}
      {message ? (
        <p
          role={createRun.isError ? "alert" : "status"}
          className={cn(
            "rounded-md border px-3 py-2 text-sm",
            createRun.isError
              ? "border-rose-200 bg-rose-50 text-rose-800"
              : "border-emerald-200 bg-emerald-50 text-emerald-800",
          )}
        >
          {isConflict ? "运行冲突：" : ""}
          {message}
        </p>
      ) : null}

      {!runSnapshot ? (
        <StateMessage>
          {configMissing || sourcesMissing
            ? "满足上游与配置前置条件后，具备权限的账号可创建首个不可变分级快照。"
            : "尚无综合分级运行。具备权限的账号可显式创建；只读账号不会看到任何写入控件。"}
        </StateMessage>
      ) : (
        <>
          <section className="grid grid-cols-4 gap-3" aria-label="综合分级摘要">
            <SummaryMetric label="高关注" value={runSnapshot.high_attention_count} accent="rose" />
            <SummaryMetric
              label="人工关注"
              value={runSnapshot.manual_attention_count}
              accent="amber"
            />
            <SummaryMetric label="放行" value={runSnapshot.cleared_count} accent="emerald" />
            <SummaryMetric
              label="来源项目"
              value={runSnapshot.deterministic_item_count + runSnapshot.correlation_item_count}
              detail={`F3 ${runSnapshot.deterministic_item_count} · F6 ${runSnapshot.correlation_item_count}`}
              accent="slate"
            />
          </section>

          <section className="grid grid-cols-[minmax(320px,0.78fr)_minmax(0,1.45fr)] overflow-hidden rounded-xl border bg-background">
            <div className="min-w-0 border-r">
              <Filters filters={filters} setFilters={setFilters} />
              {items.isLoading ? (
                <StateMessage compact>正在按默认风险顺序读取项目…</StateMessage>
              ) : null}
              {items.isError ? (
                <StateMessage compact error>
                  {items.error.message}
                </StateMessage>
              ) : null}
              {items.data?.items.map((item) => (
                <button
                  key={item.id}
                  type="button"
                  onClick={() => {
                    setSelectedId(item.id);
                    setRowOffset(0);
                  }}
                  className={cn(
                    "grid w-full gap-2 border-b p-3 text-left transition-colors hover:bg-slate-50",
                    selectedId === item.id && "bg-cyan-50/70 shadow-[inset_3px_0_0_#0891b2]",
                  )}
                >
                  <span className="flex items-center justify-between gap-2">
                    <span className="truncate text-sm font-semibold">{sourceLabel(item)}</span>
                    <Badge className={dispositionClass(item.disposition)}>
                      {DISPOSITION_LABELS[item.disposition]}
                    </Badge>
                  </span>
                  <span className="flex items-center gap-2 text-xs">
                    <Axis value={item.severity_impact} axis="impact" />
                    <span className="text-slate-300">×</span>
                    <Axis value={item.severity_confidence} axis="confidence" />
                    <span className="ml-auto text-muted-foreground">首行 {item.first_row_no}</span>
                  </span>
                  <span className="flex items-center justify-between font-mono text-[10px] text-muted-foreground">
                    <span>
                      {item.f7_outcome ? `F7 ${F7_LABELS[item.f7_outcome]}` : "F7 不适用"}
                    </span>
                    <span>{shortFingerprint(item.item_fingerprint)}</span>
                  </span>
                </button>
              ))}
              {items.data && items.data.total === 0 ? (
                <StateMessage compact>
                  当前筛选下没有分级项目。这是有效的空结果，不代表运行失败。
                </StateMessage>
              ) : null}
              {items.data ? (
                <Pagination
                  offset={offset}
                  limit={items.data.limit}
                  total={items.data.total}
                  onChange={(next) => {
                    setOffset(next);
                    setSelectedId(null);
                  }}
                  label="分级项目"
                />
              ) : null}
            </div>
            <ItemDetail
              item={detail.data ?? null}
              loading={detail.isLoading}
              error={detail.isError ? detail.error.message : null}
              rows={rows.data ?? null}
              rowsLoading={rows.isLoading}
              rowsError={rows.isError ? rows.error.message : null}
              rowOffset={rowOffset}
              setRowOffset={setRowOffset}
            />
          </section>

          <section className="grid grid-cols-4 gap-2 rounded-lg border bg-slate-50 p-3 text-[11px]">
            <FingerprintMetric label="INPUT" value={runSnapshot.input_fingerprint} />
            <FingerprintMetric label="F3 MANIFEST" value={runSnapshot.f3_manifest_fingerprint} />
            <FingerprintMetric label="F6 MANIFEST" value={runSnapshot.f6_manifest_fingerprint} />
            <FingerprintMetric label="F7 MANIFEST" value={runSnapshot.f7_manifest_fingerprint} />
          </section>
        </>
      )}
    </div>
  );
}

function Filters({
  filters,
  setFilters,
}: {
  filters: FilterState;
  setFilters: (value: FilterState) => void;
}) {
  function update<Key extends keyof FilterState>(key: Key, value: FilterState[Key]): void {
    setFilters({ ...filters, [key]: value });
  }
  return (
    <div className="grid grid-cols-2 gap-2 border-b bg-slate-50/70 p-3">
      <FilterSelect
        label="来源"
        value={filters.sourceKind}
        onChange={(v) => update("sourceKind", v as FilterState["sourceKind"])}
      >
        <option value="">全部来源</option>
        <option value="deterministic">F3 确定性</option>
        <option value="correlation">F6 关联</option>
      </FilterSelect>
      <FilterSelect
        label="处置"
        value={filters.disposition}
        onChange={(v) => update("disposition", v as FilterState["disposition"])}
      >
        <option value="">全部处置</option>
        {DISPOSITIONS.map((v) => (
          <option key={v} value={v}>
            {DISPOSITION_LABELS[v]}
          </option>
        ))}
      </FilterSelect>
      <FilterSelect
        label="影响"
        value={filters.impact}
        onChange={(v) => update("impact", v as FilterState["impact"])}
      >
        <option value="">全部影响</option>
        {[0, 1, 2, 3].map((v) => (
          <option key={v} value={v}>
            影响 {v}
          </option>
        ))}
      </FilterSelect>
      <FilterSelect
        label="置信"
        value={filters.confidence}
        onChange={(v) => update("confidence", v as FilterState["confidence"])}
      >
        <option value="">全部置信</option>
        {[0, 1, 2, 3].map((v) => (
          <option key={v} value={v}>
            置信 {v}
          </option>
        ))}
      </FilterSelect>
      <FilterSelect
        label="F3 规则"
        value={filters.ruleKind}
        onChange={(v) => update("ruleKind", v as FilterState["ruleKind"])}
      >
        <option value="">全部规则</option>
        {RULES.map((v) => (
          <option key={v} value={v}>
            {RULE_LABELS[v]}
          </option>
        ))}
      </FilterSelect>
      <FilterSelect
        label="F6 detector"
        value={filters.detector}
        onChange={(v) => update("detector", v as FilterState["detector"])}
      >
        <option value="">全部 detector</option>
        {DETECTORS.map((v) => (
          <option key={v} value={v}>
            {DETECTOR_LABELS[v]}
          </option>
        ))}
      </FilterSelect>
      <FilterSelect
        label="F7 结果"
        value={filters.f7Outcome}
        onChange={(v) => update("f7Outcome", v as FilterState["f7Outcome"])}
      >
        <option value="">全部 F7</option>
        {F7_OUTCOMES.map((v) => (
          <option key={v} value={v}>
            {F7_LABELS[v]}
          </option>
        ))}
      </FilterSelect>
      <Button size="sm" variant="outline" onClick={() => setFilters(EMPTY_FILTERS)}>
        清除筛选
      </Button>
      <p className="col-span-2 text-[10px] text-muted-foreground">
        排序：处置 → 影响 → 置信 → 首行 → 稳定标识（服务端 default）
      </p>
    </div>
  );
}

function FilterSelect({
  label,
  value,
  onChange,
  children,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  children: React.ReactNode;
}) {
  return (
    <label className="grid gap-1 text-[11px] font-semibold text-slate-600">
      {label}
      <select
        aria-label={`${label}筛选`}
        className="h-8 min-w-0 rounded-md border bg-white px-2 text-xs text-slate-900"
        value={value}
        onChange={(event) => onChange(event.target.value)}
      >
        {children}
      </select>
    </label>
  );
}

function ItemDetail({
  item,
  loading,
  error,
  rows,
  rowsLoading,
  rowsError,
  rowOffset,
  setRowOffset,
}: {
  item: GradingItem | null;
  loading: boolean;
  error: string | null;
  rows: ReturnType<typeof useGradingRows>["data"] | null;
  rowsLoading: boolean;
  rowsError: string | null;
  rowOffset: number;
  setRowOffset: (value: number) => void;
}) {
  if (loading) return <StateMessage>正在读取类型化证据…</StateMessage>;
  if (error) return <StateMessage error>{error}</StateMessage>;
  if (!item) return <StateMessage>选择左侧项目查看双轴判定、原因码与全部参与行。</StateMessage>;
  const evidence = item.evidence_snapshot;
  return (
    <article className="min-w-0 p-5">
      <div className="flex flex-wrap items-start justify-between gap-3 border-b pb-4">
        <div>
          <p className="text-xs font-semibold tracking-[0.15em] text-muted-foreground">
            EVIDENCE DOSSIER
          </p>
          <h3 className="mt-1 text-lg font-semibold">{sourceLabel(item)}</h3>
          <p className="mt-1 text-xs text-muted-foreground">
            首行 {item.first_row_no} · {item.source_kind}
          </p>
        </div>
        <div className="flex items-center gap-2">
          <Axis value={item.severity_impact} axis="impact" />
          <span>×</span>
          <Axis value={item.severity_confidence} axis="confidence" />
          <Badge className={dispositionClass(item.disposition)}>
            {DISPOSITION_LABELS[item.disposition]}
          </Badge>
        </div>
      </div>
      <dl className="mt-4 grid grid-cols-3 gap-2 text-xs">
        <EvidenceFact label="映射影响" value={String(evidence.mapped_impact)} />
        <EvidenceFact label="置信（上限前）" value={String(evidence.confidence_before_cap)} />
        <EvidenceFact label="置信（上限后）" value={String(evidence.confidence_after_cap)} />
        <EvidenceFact label="来源结论" value={evidence.source_outcome} />
        <EvidenceFact label="能力状态" value={evidence.capability_status ?? "不适用"} />
        <EvidenceFact label="矩阵处置" value={DISPOSITION_LABELS[evidence.matrix_disposition]} />
      </dl>
      <div className="mt-3 grid grid-cols-3 gap-2 rounded-lg border bg-slate-950 p-3 text-center text-xs text-slate-200">
        <EvidenceFact dark label="放行损失" value={String(evidence.losses.clear_loss)} />
        <EvidenceFact dark label="人工损失" value={String(evidence.losses.review_loss)} />
        <EvidenceFact dark label="标记损失" value={String(evidence.losses.flag_loss)} />
      </div>
      <section className="mt-4">
        <h4 className="text-xs font-semibold tracking-wide text-muted-foreground">
          REASON CODES · 稳定顺序
        </h4>
        <div className="mt-2 flex flex-wrap gap-1.5">
          {item.reason_codes.map((code) => (
            <code key={code} className="rounded border bg-slate-50 px-2 py-1 text-[10px]">
              {code}
            </code>
          ))}
        </div>
      </section>
      <section className="mt-4 grid gap-2 rounded-lg border p-3 text-xs">
        <EvidenceFact label="项目指纹" value={item.item_fingerprint} mono />
        <EvidenceFact label="F3 finding" value={item.finding_id ?? "不适用"} mono />
        <EvidenceFact label="F6 finding" value={item.correlation_finding_id ?? "不适用"} mono />
        <EvidenceFact
          label="F7 outcome"
          value={item.f7_outcome ? F7_LABELS[item.f7_outcome] : "不适用"}
        />
        <div className="flex items-center justify-between gap-3">
          <span className="text-muted-foreground">Investigation</span>
          {item.investigation_run_id ? (
            <a
              className="inline-flex items-center gap-1 font-mono text-cyan-700 underline underline-offset-2"
              href={`/api/v1/investigations/${item.investigation_run_id}/steps?limit=50&offset=0`}
              target="_blank"
              rel="noreferrer"
            >
              {item.investigation_run_id}
              <ArrowUpRight className="size-3" aria-hidden="true" />
            </a>
          ) : (
            <span>未运行</span>
          )}
        </div>
      </section>
      <section className="mt-5">
        <div className="flex items-center justify-between">
          <h4 className="text-xs font-semibold tracking-wide text-muted-foreground">全部参与行</h4>
          {rows ? <span className="text-xs text-muted-foreground">{rows.total} 行</span> : null}
        </div>
        {rowsLoading ? <StateMessage compact>正在读取参与行…</StateMessage> : null}
        {rowsError ? (
          <StateMessage compact error>
            {rowsError}
          </StateMessage>
        ) : null}
        <div className="mt-2 grid gap-2">
          {rows?.items.map((row) => (
            <div key={row.id} className="rounded-md border bg-slate-50 p-3 text-xs">
              <div className="flex items-center justify-between">
                <strong>
                  #{row.ordinal} · 原始行 {row.row_no}
                </strong>
                <code title={row.source_row_fingerprint}>
                  {shortFingerprint(row.source_row_fingerprint)}
                </code>
              </div>
              <p className="mt-2 break-all font-mono text-[10px] leading-4 text-slate-600">
                {JSON.stringify(row.raw)}
              </p>
            </div>
          ))}
        </div>
        {rows ? (
          <Pagination
            offset={rowOffset}
            limit={rows.limit}
            total={rows.total}
            onChange={setRowOffset}
            label="参与行"
          />
        ) : null}
      </section>
    </article>
  );
}

function Baseline({
  label,
  value,
  stale,
}: {
  label: string;
  value: string | null;
  stale: boolean;
}) {
  return (
    <div className="border-r border-slate-800 p-3 last:border-r-0">
      <span className="flex items-center justify-between text-[10px] font-semibold tracking-wider text-slate-400">
        <span>{label}</span>
        <span className={stale ? "text-amber-300" : "text-emerald-300"}>
          {stale ? "STALE" : value ? "CURRENT" : "ABSENT"}
        </span>
      </span>
      <code className="mt-1 block truncate text-[11px] text-slate-200" title={value ?? undefined}>
        {value ?? "—"}
      </code>
    </div>
  );
}
function Axis({ value, axis }: { value: number; axis: "impact" | "confidence" }) {
  return (
    <span
      className={cn("rounded px-2 py-1 text-[11px] font-bold tabular-nums", axisClass(value, axis))}
    >
      {axis === "impact" ? "I" : "C"}
      {value}
    </span>
  );
}
function EvidenceFact({
  label,
  value,
  mono = false,
  dark = false,
}: {
  label: string;
  value: string;
  mono?: boolean;
  dark?: boolean;
}) {
  return (
    <div className="min-w-0">
      <dt className={cn("text-muted-foreground", dark && "text-slate-400")}>{label}</dt>
      <dd className={cn("mt-1 break-all font-medium", mono && "font-mono text-[10px]")}>{value}</dd>
    </div>
  );
}
function FingerprintMetric({ label, value }: { label: string; value: string }) {
  return (
    <div className="min-w-0">
      <p className="flex items-center gap-1 font-semibold text-slate-500">
        <Fingerprint className="size-3" aria-hidden="true" />
        {label}
      </p>
      <code className="mt-1 block truncate" title={value}>
        {value}
      </code>
    </div>
  );
}
function SummaryMetric({
  label,
  value,
  detail,
  accent,
}: {
  label: string;
  value: number;
  detail?: string;
  accent: "rose" | "amber" | "emerald" | "slate";
}) {
  const tones = {
    rose: "border-rose-200 bg-rose-50",
    amber: "border-amber-200 bg-amber-50",
    emerald: "border-emerald-200 bg-emerald-50",
    slate: "border-slate-200 bg-slate-50",
  };
  return (
    <div className={cn("rounded-lg border p-3", tones[accent])}>
      <p className="text-xs text-muted-foreground">{label}</p>
      <p className="mt-1 text-2xl font-semibold tabular-nums">{value}</p>
      {detail ? <p className="text-[10px] text-muted-foreground">{detail}</p> : null}
    </div>
  );
}
function StateBanner({
  tone,
  title,
  children,
}: {
  tone: "warning" | "danger";
  title: string;
  children: React.ReactNode;
}) {
  return (
    <div
      role="status"
      className={cn(
        "flex items-start gap-3 rounded-lg border px-4 py-3 text-sm",
        tone === "warning"
          ? "border-amber-200 bg-amber-50 text-amber-950"
          : "border-rose-200 bg-rose-50 text-rose-900",
      )}
    >
      <AlertOctagon className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
      <div>
        <strong>{title}</strong>
        <p className="mt-0.5">{children}</p>
      </div>
    </div>
  );
}
function Pagination({
  offset,
  limit,
  total,
  onChange,
  label,
}: {
  offset: number;
  limit: number;
  total: number;
  onChange: (offset: number) => void;
  label: string;
}) {
  return (
    <div className="flex items-center justify-between border-t p-3 text-xs text-muted-foreground">
      <span>
        {label} {total} · {total === 0 ? 0 : offset + 1}–{Math.min(offset + limit, total)}
      </span>
      <div className="flex gap-2">
        <Button
          aria-label={`${label}上一页`}
          size="sm"
          variant="outline"
          disabled={offset === 0}
          onClick={() => onChange(Math.max(0, offset - limit))}
        >
          上一页
        </Button>
        <Button
          aria-label={`${label}下一页`}
          size="sm"
          variant="outline"
          disabled={offset + limit >= total}
          onClick={() => onChange(offset + limit)}
        >
          下一页
        </Button>
      </div>
    </div>
  );
}
function StateMessage({
  children,
  error = false,
  compact = false,
}: {
  children: React.ReactNode;
  error?: boolean;
  compact?: boolean;
}) {
  return (
    <div
      role={error ? "alert" : undefined}
      className={cn(
        "flex items-center justify-center p-8 text-sm text-muted-foreground",
        compact ? "min-h-28" : "min-h-72",
        error && "text-destructive",
      )}
    >
      {error ? (
        <AlertOctagon className="mr-2 size-4" aria-hidden="true" />
      ) : (
        <CircleDashed className="mr-2 size-4" aria-hidden="true" />
      )}
      {children}
    </div>
  );
}
