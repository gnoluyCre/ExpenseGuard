import { useEffect, useMemo, useState, type FormEvent, type ReactNode } from "react";
import {
  AlertTriangle,
  Check,
  CheckCircle2,
  ChevronLeft,
  ChevronRight,
  CircleDashed,
  ClipboardCheck,
  Fingerprint,
  RefreshCw,
  Save,
  ShieldAlert,
} from "lucide-react";

import {
  hasPermission,
  PERMISSIONS,
  type CurrentUser,
  type FindingDecisionRequest,
  type ReviewItemEvidence,
  type ReviewQueueItem,
  type SamplingConfig,
  type SamplingDecisionRequest,
} from "@/api/client";
import { useCurrentUser } from "@/auth/useAuth";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { cn } from "@/lib/utils";
import {
  findingDecisionSchema,
  optionalNote,
  samplingConfigSchema,
  samplingDecisionSchema,
} from "@/reviews/reviewSchemas";
import {
  ReviewApiError,
  useCreateReviewPlan,
  useFindingReviewDetail,
  useReviewPlan,
  useReviewQueue,
  useReviewSummary,
  useSampleReviewDetail,
  useSamplingConfig,
  useSaveSamplingConfig,
  useSubmitFindingDecision,
  useSubmitSampleDecision,
} from "@/reviews/useReviews";

const PAGE_SIZE = 25;

function formatDateTime(value: string): string {
  return new Intl.DateTimeFormat("zh-CN", {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(value));
}

function shortId(value: string): string {
  return value.slice(0, 8);
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : "操作失败，请重试";
}

function isConflict(error: unknown): boolean {
  return error instanceof ReviewApiError && error.status === 409;
}

export function ReviewPage() {
  const { data: user } = useCurrentUser();
  if (!user || !hasPermission(user, PERMISSIONS.reviewRead)) return <ReviewUnavailable />;
  return <ReviewWorkspace user={user} />;
}

function ReviewWorkspace({ user }: { user: CurrentUser }) {
  const canSubmit = hasPermission(user, PERMISSIONS.reviewSubmit);
  const canConfigure = hasPermission(user, PERMISSIONS.configWrite);
  const [status, setStatus] = useState<"pending" | "completed">("pending");
  const [kind, setKind] = useState<"finding" | "clearance_sample" | null>(null);
  const [reportFilter, setReportFilter] = useState<string | null>(null);
  const [offset, setOffset] = useState(0);
  const [selected, setSelected] = useState<ReviewQueueItem | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const filters = useMemo(
    () => ({
      status,
      kind,
      reportId: reportFilter,
      fileVersionId: null,
      limit: PAGE_SIZE,
      offset,
    }),
    [kind, offset, reportFilter, status],
  );
  const queue = useReviewQueue(filters);
  const config = useSamplingConfig();
  const reportId = selected?.report_run_id ?? reportFilter;
  const summary = useReviewSummary(reportId);
  const plan = useReviewPlan(reportId);
  const createPlan = useCreateReviewPlan(reportId);

  useEffect(() => {
    const items = queue.data?.items ?? [];
    if (!items.length) {
      setSelected(null);
      return;
    }
    if (!selected || !items.some((item) => item.target_id === selected.target_id)) {
      setSelected(items[0] ?? null);
    }
  }, [queue.data, selected]);

  function resetQueueAfterMutation(message: string): void {
    setNotice(message);
    setOffset(0);
    setSelected(null);
  }

  return (
    <div className="review-canvas -m-8 min-h-[calc(100vh-3.5rem)] overflow-hidden bg-[#ebe9e2] text-slate-950">
      <header className="border-b-4 border-slate-950 bg-[#f5f2e9] px-7 py-5">
        <div className="flex items-end justify-between gap-8">
          <div>
            <p className="font-mono text-[11px] uppercase tracking-[0.32em] text-amber-800">
              Human review / immutable evidence desk
            </p>
            <h1 className="mt-2 text-4xl font-black tracking-[-0.045em]">人工复核台</h1>
            <p className="mt-2 max-w-2xl text-sm leading-6 text-slate-600">
              机器快照保持不变；人工只追加一次最终标签。队列按冻结风险分组与批次时间稳定排序。
            </p>
          </div>
          <div className="grid min-w-[330px] grid-cols-2 gap-px border-2 border-slate-950 bg-slate-950 shadow-[6px_6px_0_#c47b12]">
            <HeaderDatum label="访问模式" value={canSubmit ? "可提交" : "只读"} />
            <HeaderDatum label="队列状态" value={status === "pending" ? "待复核" : "已完成"} />
          </div>
        </div>
      </header>

      <section className="grid grid-cols-[1.2fr_1fr_1fr_1fr] gap-px border-b-2 border-slate-950 bg-slate-950">
        <SummaryDatum
          label="Finding coverage"
          value={
            summary.data
              ? `${summary.data.finding_review_coverage.completed}/${summary.data.finding_review_coverage.total}`
              : "—"
          }
          detail={summary.data ? `${summary.data.finding_pending} 待复核` : "选择队列项后显示"}
        />
        <SummaryDatum
          label="Sample coverage"
          value={
            summary.data
              ? `${summary.data.sample_review_coverage.completed}/${summary.data.sample_review_coverage.total}`
              : "—"
          }
          detail={
            summary.data
              ? `${summary.data.sample_missed_issue} 个漏放样本`
              : "原始计数，不推断召回率"
          }
          alert={Boolean(summary.data?.sample_missed_issue)}
        />
        <SummaryDatum
          label="Sampling config"
          value={config.data?.current ? `V${config.data.current.version}` : "未配置"}
          detail={
            config.data?.current
              ? `${config.data.current.rate_bps} bps · ${config.data.current.min_sample_size}–${config.data.current.max_sample_size}`
              : "首批报告前必须创建"
          }
          alert={config.data?.current === null}
        />
        <SummaryDatum
          label="Sampling plan"
          value={
            plan.isLoading
              ? "读取中"
              : plan.data?.status === "completed"
                ? "已冻结"
                : plan.data?.status === "legacy_not_initialized"
                  ? "历史未初始化"
                  : "—"
          }
          detail={
            plan.data?.status === "completed"
              ? `${plan.data.plan.sample_size}/${plan.data.plan.eligible_count} 行入样`
              : plan.isError
                ? errorMessage(plan.error)
                : "选择报告后显示"
          }
          alert={plan.data?.status === "legacy_not_initialized" || plan.isError}
        />
      </section>

      {notice ? (
        <div className="flex items-center justify-between border-b border-slate-950 bg-amber-100 px-7 py-2 text-sm">
          <span>{notice}</span>
          <button
            type="button"
            className="font-mono text-xs underline"
            onClick={() => setNotice(null)}
          >
            关闭
          </button>
        </div>
      ) : null}

      <div className="grid min-h-[720px] grid-cols-[340px_minmax(0,1fr)_310px]">
        <QueueRail
          queue={queue}
          status={status}
          kind={kind}
          reportFilter={reportFilter}
          selected={selected}
          offset={offset}
          onStatus={(value) => {
            setStatus(value);
            setOffset(0);
            setSelected(null);
          }}
          onKind={(value) => {
            setKind(value);
            setOffset(0);
            setSelected(null);
          }}
          onReportFilter={(value) => {
            setReportFilter(value);
            setOffset(0);
            setSelected(null);
          }}
          onSelect={setSelected}
          onOffset={setOffset}
        />

        <EvidenceWorkspace
          selected={selected}
          canSubmit={canSubmit}
          onCommitted={resetQueueAfterMutation}
        />

        <aside className="min-w-0 border-l-2 border-slate-950 bg-[#f5f2e9]">
          <ConfigPanel
            current={config.data?.current ?? null}
            loading={config.isLoading}
            error={config.isError ? errorMessage(config.error) : null}
            canConfigure={canConfigure}
            onSaved={(version) => setNotice(`抽样配置 V${version} 已追加保存`)}
          />
          <div className="border-t-2 border-slate-950 p-5">
            <p className="font-mono text-[10px] uppercase tracking-[0.25em] text-slate-500">
              Current report
            </p>
            <p className="mt-2 break-all font-mono text-[11px]">{reportId ?? "尚未选择报告"}</p>
            {reportId && reportFilter !== reportId ? (
              <Button
                size="sm"
                variant="outline"
                className="mt-3 w-full"
                onClick={() => {
                  setReportFilter(reportId);
                  setOffset(0);
                  setSelected(null);
                }}
              >
                仅查看此批次
              </Button>
            ) : null}
            {reportFilter ? (
              <Button
                size="sm"
                variant="ghost"
                className="mt-1 w-full"
                onClick={() => {
                  setReportFilter(null);
                  setOffset(0);
                }}
              >
                清除批次筛选
              </Button>
            ) : null}
            {plan.data?.status === "legacy_not_initialized" && canSubmit ? (
              <div className="mt-4 border-2 border-amber-700 bg-amber-50 p-3">
                <p className="text-sm font-semibold">历史报告尚无抽检计划</p>
                <p className="mt-1 text-xs leading-5 text-amber-900">
                  显式创建后样本永久冻结，刷新页面不会重抽。
                </p>
                <Button
                  size="sm"
                  className="mt-3 w-full"
                  disabled={createPlan.isPending}
                  onClick={() =>
                    createPlan.mutate(undefined, {
                      onSuccess: () => setNotice("历史报告抽检计划已创建"),
                      onError: (error) => setNotice(errorMessage(error)),
                    })
                  }
                >
                  {createPlan.isPending ? <RefreshCw className="animate-spin" /> : <Fingerprint />}
                  {createPlan.isPending ? "正在冻结计划" : "创建不可变计划"}
                </Button>
              </div>
            ) : null}
          </div>
        </aside>
      </div>
    </div>
  );
}

function QueueRail({
  queue,
  status,
  kind,
  reportFilter,
  selected,
  offset,
  onStatus,
  onKind,
  onReportFilter,
  onSelect,
  onOffset,
}: {
  queue: ReturnType<typeof useReviewQueue>;
  status: "pending" | "completed";
  kind: "finding" | "clearance_sample" | null;
  reportFilter: string | null;
  selected: ReviewQueueItem | null;
  offset: number;
  onStatus: (value: "pending" | "completed") => void;
  onKind: (value: "finding" | "clearance_sample" | null) => void;
  onReportFilter: (value: string | null) => void;
  onSelect: (value: ReviewQueueItem) => void;
  onOffset: (value: number) => void;
}) {
  return (
    <aside className="min-w-0 border-r-2 border-slate-950 bg-[#dedbd1]">
      <div className="border-b-2 border-slate-950 p-4">
        <div className="grid grid-cols-2 gap-1">
          {(["pending", "completed"] as const).map((value) => (
            <button
              key={value}
              type="button"
              className={cn(
                "border-2 border-slate-950 px-3 py-2 font-mono text-xs uppercase",
                status === value ? "bg-slate-950 text-white" : "bg-[#f5f2e9] hover:bg-white",
              )}
              onClick={() => onStatus(value)}
            >
              {value === "pending" ? "待复核" : "已完成"}
            </button>
          ))}
        </div>
        <div className="mt-2 grid grid-cols-3 gap-1">
          {(
            [
              [null, "全部"],
              ["finding", "关注项"],
              ["clearance_sample", "抽检"],
            ] as const
          ).map(([value, label]) => (
            <button
              key={label}
              type="button"
              className={cn(
                "border px-2 py-1.5 text-xs",
                kind === value
                  ? "border-amber-800 bg-amber-100 font-semibold"
                  : "border-slate-400 bg-[#f5f2e9]",
              )}
              onClick={() => onKind(value)}
            >
              {label}
            </button>
          ))}
        </div>
        {reportFilter ? (
          <div className="mt-2 flex items-center justify-between border border-slate-400 bg-white px-2 py-1.5 font-mono text-[10px]">
            <span className="truncate">报告 {shortId(reportFilter)}</span>
            <button type="button" className="underline" onClick={() => onReportFilter(null)}>
              清除
            </button>
          </div>
        ) : null}
      </div>

      <div className="flex items-center justify-between border-b border-slate-400 px-4 py-2 font-mono text-[10px] uppercase tracking-wider">
        <span>Stable queue</span>
        <span>{queue.data?.total ?? 0} items</span>
      </div>
      {queue.isLoading ? <RailState loading>正在读取联合队列</RailState> : null}
      {queue.isError ? <RailState error>{errorMessage(queue.error)}</RailState> : null}
      {queue.data?.items.length === 0 ? <RailState>当前筛选下没有复核项</RailState> : null}
      <div className="divide-y divide-slate-400">
        {queue.data?.items.map((item, index) => (
          <QueueCard
            key={`${item.kind}:${item.target_id}`}
            item={item}
            sequence={offset + index + 1}
            active={selected?.target_id === item.target_id}
            onClick={() => onSelect(item)}
          />
        ))}
      </div>
      {queue.data && queue.data.total > queue.data.limit ? (
        <div className="flex items-center justify-between border-t-2 border-slate-950 p-3">
          <Button
            size="sm"
            variant="outline"
            disabled={offset === 0}
            onClick={() => onOffset(Math.max(0, offset - PAGE_SIZE))}
          >
            <ChevronLeft /> 上页
          </Button>
          <span className="font-mono text-[10px]">
            {offset + 1}–{Math.min(offset + PAGE_SIZE, queue.data.total)}
          </span>
          <Button
            size="sm"
            variant="outline"
            disabled={offset + queue.data.limit >= queue.data.total}
            onClick={() => onOffset(offset + PAGE_SIZE)}
          >
            下页 <ChevronRight />
          </Button>
        </div>
      ) : null}
    </aside>
  );
}

function QueueCard({
  item,
  sequence,
  active,
  onClick,
}: {
  item: ReviewQueueItem;
  sequence: number;
  active: boolean;
  onClick: () => void;
}) {
  const high = item.kind === "finding" && item.attention_group === "high_attention";
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        "relative grid w-full grid-cols-[36px_minmax(0,1fr)] gap-3 p-4 text-left transition-colors",
        active ? "bg-white shadow-[inset_5px_0_0_#0f172a]" : "hover:bg-[#f5f2e9]",
      )}
    >
      <span
        className={cn(
          "grid size-8 place-items-center border-2 border-slate-950 font-mono text-[10px]",
          high
            ? "bg-red-700 text-white"
            : item.kind === "clearance_sample"
              ? "bg-emerald-700 text-white"
              : "bg-amber-300",
        )}
      >
        {String(sequence).padStart(2, "0")}
      </span>
      <span className="min-w-0">
        <span className="flex items-center justify-between gap-2">
          <span className="font-mono text-xs font-bold">ROW {item.row_no}</span>
          <Badge variant="outline" className="shrink-0 rounded-none bg-white text-[10px]">
            {item.kind === "finding"
              ? high
                ? "高关注"
                : "人工关注"
              : `抽检 #${item.selection_rank}`}
          </Badge>
        </span>
        <span className="mt-1 block truncate text-xs text-slate-600">
          {item.kind === "finding"
            ? `${item.rule_id} · ${item.rule_version ?? "无版本"}`
            : "系统原判：passed"}
        </span>
        <span className="mt-2 block font-mono text-[9px] text-slate-500">
          {formatDateTime(item.report_completed_at)} · {shortId(item.report_run_id)}
        </span>
      </span>
    </button>
  );
}

function EvidenceWorkspace({
  selected,
  canSubmit,
  onCommitted,
}: {
  selected: ReviewQueueItem | null;
  canSubmit: boolean;
  onCommitted: (message: string) => void;
}) {
  const findingId = selected?.kind === "finding" ? selected.target_id : null;
  const sampleId = selected?.kind === "clearance_sample" ? selected.target_id : null;
  const finding = useFindingReviewDetail(findingId);
  const sample = useSampleReviewDetail(sampleId);

  if (!selected)
    return (
      <WorkspaceState
        icon={<ClipboardCheck className="size-8" />}
        title="选择一个复核目标"
        text="左侧队列保持稳定排序；提交后会回到第一页，避免跳过收缩后的 pending 项。"
      />
    );
  const detail = selected.kind === "finding" ? finding : sample;
  if (detail.isLoading)
    return (
      <WorkspaceState
        icon={<RefreshCw className="size-8 animate-spin" />}
        title="装载冻结证据"
        text="只读取 F4 快照与原始行，不重新运行规则或检索。"
      />
    );
  if (detail.isError)
    return (
      <WorkspaceState
        error
        icon={<ShieldAlert className="size-8" />}
        title="详情读取失败"
        text={errorMessage(detail.error)}
      />
    );
  if (selected.kind === "finding" && finding.data)
    return (
      <FindingWorkspace
        detail={finding.data}
        targetId={selected.target_id}
        canSubmit={canSubmit}
        onCommitted={onCommitted}
      />
    );
  if (selected.kind === "clearance_sample" && sample.data)
    return (
      <SampleWorkspace
        detail={sample.data}
        targetId={selected.target_id}
        canSubmit={canSubmit}
        onCommitted={onCommitted}
      />
    );
  return null;
}

function FindingWorkspace({
  detail,
  targetId,
  canSubmit,
  onCommitted,
}: {
  detail: NonNullable<ReturnType<typeof useFindingReviewDetail>["data"]>;
  targetId: string;
  canSubmit: boolean;
  onCommitted: (message: string) => void;
}) {
  return (
    <div className="min-w-0 bg-white">
      <EvidenceHeader
        eyebrow="Finding review"
        rowNo={detail.report_item.row_no}
        title={`${detail.report_item.rule_id} · ${detail.report_item.reason_code}`}
        status={detail.report_item.attention_group === "high_attention" ? "高关注" : "人工关注"}
      />
      <EvidenceBody
        raw={detail.raw_row}
        normalized={detail.normalized_row}
        items={[detail.report_item]}
      />
      <DecisionDock
        kind="finding"
        targetId={targetId}
        existing={detail.existing_review}
        canSubmit={canSubmit}
        onCommitted={onCommitted}
      />
    </div>
  );
}

function SampleWorkspace({
  detail,
  targetId,
  canSubmit,
  onCommitted,
}: {
  detail: NonNullable<ReturnType<typeof useSampleReviewDetail>["data"]>;
  targetId: string;
  canSubmit: boolean;
  onCommitted: (message: string) => void;
}) {
  return (
    <div className="min-w-0 bg-white">
      <EvidenceHeader
        eyebrow="Clearance sampling"
        rowNo={detail.row_no}
        title="系统原判：passed"
        status="被放行抽检"
      />
      <div className="grid grid-cols-2 gap-px border-b-2 border-slate-950 bg-slate-950 font-mono text-[10px]">
        <FingerprintStrip label="规则集" value={detail.ruleset_fingerprint} />
        <FingerprintStrip label="报告" value={detail.report_fingerprint} />
      </div>
      <EvidenceBody
        raw={detail.raw_row}
        normalized={detail.normalized_row}
        items={detail.cleared_items}
        purePassed={detail.cleared_items.length === 0}
      />
      {detail.existing_review?.decision === "missed_issue" ? (
        <div className="mx-5 mb-0 border-2 border-red-700 bg-red-50 p-3 text-sm text-red-900">
          <strong>已发现漏放问题。</strong> 请按人工升级流程处理；F5 不会自动创建 finding
          或改写报告。
        </div>
      ) : null}
      <DecisionDock
        kind="clearance_sample"
        targetId={targetId}
        existing={detail.existing_review}
        canSubmit={canSubmit}
        onCommitted={onCommitted}
      />
    </div>
  );
}

function EvidenceHeader({
  eyebrow,
  rowNo,
  title,
  status,
}: {
  eyebrow: string;
  rowNo: number;
  title: string;
  status: string;
}) {
  return (
    <div className="flex items-start justify-between gap-5 border-b-2 border-slate-950 bg-slate-950 px-5 py-4 text-white">
      <div className="min-w-0">
        <p className="font-mono text-[10px] uppercase tracking-[0.25em] text-amber-400">
          {eyebrow}
        </p>
        <h2 className="mt-2 truncate text-xl font-bold">
          ROW {rowNo} / {title}
        </h2>
      </div>
      <Badge className="shrink-0 rounded-none border border-white/30 bg-white/10 text-white">
        {status}
      </Badge>
    </div>
  );
}

function EvidenceBody({
  raw,
  normalized,
  items,
  purePassed = false,
}: {
  raw: Record<string, unknown>;
  normalized: Record<string, unknown> | null;
  items: ReviewItemEvidence[];
  purePassed?: boolean;
}) {
  return (
    <div className="grid min-h-[490px] grid-cols-[0.95fr_1.05fr]">
      <section className="min-w-0 border-r border-slate-300 p-5">
        <SectionLabel index="01" title="原始行证据" />
        <JsonEvidence value={raw} />
        {normalized ? (
          <>
            <p className="mt-5 text-xs font-semibold">规范化投影</p>
            <JsonEvidence value={normalized} compact />
          </>
        ) : (
          <p className="mt-4 border border-amber-300 bg-amber-50 p-3 text-xs text-amber-900">
            本行没有规范化投影；仅展示原始证据。
          </p>
        )}
      </section>
      <section className="min-w-0 p-5">
        <SectionLabel index="02" title="机器判定与制度依据" />
        {purePassed ? (
          <div className="mt-4 border-2 border-emerald-700 bg-emerald-50 p-5">
            <CheckCircle2 className="size-6 text-emerald-700" />
            <h3 className="mt-3 font-semibold">系统未产生关注项</h3>
            <p className="mt-1 text-sm leading-6 text-emerald-950">
              该行以 passed 进入随机抽检；系统没有 finding、理由或制度引用。界面不会补造证据。
            </p>
          </div>
        ) : (
          items.map((item) => <EvidenceItem key={item.id} item={item} />)
        )}
      </section>
    </div>
  );
}

function EvidenceItem({ item }: { item: ReviewItemEvidence }) {
  return (
    <article className="mt-4 border-2 border-slate-950">
      <div className="flex items-center justify-between gap-3 border-b border-slate-950 bg-[#f5f2e9] px-3 py-2">
        <span className="font-mono text-xs font-bold">
          {item.rule_id} / {item.rule_version ?? "NO VERSION"}
        </span>
        <Badge
          variant={item.citation_status === "verified" ? "secondary" : "outline"}
          className="rounded-none"
        >
          {item.citation_status === "verified" ? "逐字引用已验证" : "制度依据未完成"}
        </Badge>
      </div>
      <div className="p-3">
        <p className="text-sm leading-6">{item.reasoning_snapshot ?? "没有补充理由"}</p>
        <p className="mt-3 font-mono text-[10px] uppercase text-slate-500">
          Evidence / {item.source_outcome}
        </p>
        <pre className="mt-1 max-h-44 overflow-auto whitespace-pre-wrap break-words bg-slate-950 p-3 text-[11px] leading-5 text-slate-200">
          {JSON.stringify(item.evidence_snapshot, null, 2)}
        </pre>
        {item.citations.length ? (
          item.citations.map((citation) => (
            <blockquote
              key={citation.id}
              className="mt-3 border-l-4 border-emerald-700 bg-emerald-50 px-3 py-2 text-sm leading-6"
            >
              <p className="whitespace-pre-wrap break-words">{citation.quote}</p>
              <footer className="mt-2 break-all font-mono text-[9px] text-emerald-900">
                {citation.document_title} · {citation.document_version} · {citation.clause_no} · [
                {citation.quote_start},{citation.quote_end})
              </footer>
            </blockquote>
          ))
        ) : (
          <div className="mt-3 flex gap-2 border border-amber-400 bg-amber-50 p-3 text-sm text-amber-950">
            <AlertTriangle className="mt-0.5 size-4 shrink-0" />
            <span>制度依据未完成。可依据当前证据人工判断，但不得把缺失引用伪装成已验证。</span>
          </div>
        )}
      </div>
    </article>
  );
}

type ExistingDecision = {
  decision: string;
  note: string | null;
  reviewer_id: string;
  reviewed_at: string;
} | null;

function DecisionDock({
  kind,
  targetId,
  existing,
  canSubmit,
  onCommitted,
}: {
  kind: "finding" | "clearance_sample";
  targetId: string;
  existing: ExistingDecision;
  canSubmit: boolean;
  onCommitted: (message: string) => void;
}) {
  const submitFinding = useSubmitFindingDecision(kind === "finding" ? targetId : null);
  const submitSample = useSubmitSampleDecision(kind === "clearance_sample" ? targetId : null);
  const [decision, setDecision] = useState("");
  const [note, setNote] = useState("");
  const [validationError, setValidationError] = useState<string | null>(null);
  const [pendingRequest, setPendingRequest] = useState<
    FindingDecisionRequest | SamplingDecisionRequest | null
  >(null);
  const mutation = kind === "finding" ? submitFinding : submitSample;

  useEffect(() => {
    setDecision("");
    setNote("");
    setValidationError(null);
    setPendingRequest(null);
  }, [kind, targetId]);

  if (existing) return <CompletedDecision existing={existing} />;
  if (!canSubmit)
    return (
      <div className="sticky bottom-0 border-t-4 border-slate-950 bg-slate-100 p-5">
        <p className="font-semibold">只读模式</p>
        <p className="mt-1 text-sm text-slate-600">当前权限可以查看证据，但不能提交真实标签。</p>
      </div>
    );

  const choices =
    kind === "finding"
      ? (["confirmed", "false_positive"] as const)
      : (["clearance_confirmed", "missed_issue"] as const);
  const labels: Record<string, string> = {
    confirmed: "确认成立",
    false_positive: "确认误报",
    clearance_confirmed: "确认放行正确",
    missed_issue: "发现漏放问题",
  };

  function prepare(event: FormEvent): void {
    event.preventDefault();
    const maybeNote = optionalNote(note);
    const parsed =
      kind === "finding"
        ? findingDecisionSchema.safeParse({ kind, decision, note: maybeNote })
        : samplingDecisionSchema.safeParse({ kind, decision, note: maybeNote });
    if (!parsed.success) {
      setValidationError(parsed.error.issues[0]?.message ?? "请检查表单");
      return;
    }
    setValidationError(null);
    const parsedNote = parsed.data.note;
    if (parsed.data.kind === "finding") {
      setPendingRequest({
        kind: "finding",
        decision: parsed.data.decision,
        ...(parsedNote === undefined ? {} : { note: parsedNote }),
      });
    } else {
      setPendingRequest({
        kind: "clearance_sample",
        decision: parsed.data.decision,
        ...(parsedNote === undefined ? {} : { note: parsedNote }),
      });
    }
  }

  function commit(): void {
    if (!pendingRequest) return;
    const callbacks = {
      onSuccess: () => {
        setPendingRequest(null);
        onCommitted("最终结论已由服务端保存，队列已回到第一页");
      },
      onError: (error: Error) => {
        setPendingRequest(null);
        setValidationError(
          isConflict(error)
            ? `目标状态已变化：${error.message}。已请求刷新服务端事实。`
            : error.message,
        );
        if (isConflict(error)) onCommitted("并发冲突：正在刷新服务端最终结论");
      },
    };
    if (pendingRequest.kind === "finding") submitFinding.mutate(pendingRequest, callbacks);
    else submitSample.mutate(pendingRequest, callbacks);
  }

  return (
    <form
      className="sticky bottom-0 border-t-4 border-slate-950 bg-[#f5f2e9] p-5 shadow-[0_-10px_30px_rgba(15,23,42,0.12)]"
      onSubmit={prepare}
    >
      <div className="flex items-center justify-between gap-3">
        <div>
          <p className="font-mono text-[10px] uppercase tracking-[0.25em] text-slate-500">
            Final human label
          </p>
          <h3 className="mt-1 font-bold">提交一次性结论</h3>
        </div>
        <Badge variant="outline" className="rounded-none border-red-700 text-red-800">
          提交后不可修改
        </Badge>
      </div>
      <div className="mt-3 grid grid-cols-2 gap-2">
        {choices.map((value) => (
          <label
            key={value}
            className={cn(
              "flex cursor-pointer items-center gap-2 border-2 px-3 py-2 text-sm",
              decision === value
                ? "border-slate-950 bg-slate-950 text-white"
                : "border-slate-400 bg-white",
            )}
          >
            <input
              type="radio"
              name="decision"
              value={value}
              checked={decision === value}
              onChange={() => {
                setDecision(value);
                setPendingRequest(null);
              }}
            />
            {labels[value]}
          </label>
        ))}
      </div>
      <label className="mt-3 block text-xs font-semibold" htmlFor={`note-${targetId}`}>
        复核说明{" "}
        {decision === "false_positive" || decision === "missed_issue" ? "（必填）" : "（可选）"}
      </label>
      <textarea
        id={`note-${targetId}`}
        value={note}
        maxLength={2000}
        onChange={(event) => {
          setNote(event.target.value);
          setPendingRequest(null);
        }}
        className="mt-1 min-h-20 w-full resize-y border-2 border-slate-950 bg-white p-2 text-sm outline-none focus:ring-2 focus:ring-amber-500"
        placeholder="仅保存在业务库，不进入审计 payload、日志或模型"
      />
      <div className="mt-2 flex items-center justify-between gap-3">
        <span className="text-xs text-red-700" role={validationError ? "alert" : undefined}>
          {validationError}
        </span>
        <span className="font-mono text-[10px] text-slate-500">{note.length}/2000</span>
      </div>
      {pendingRequest ? (
        <div className="mt-3 flex items-center justify-between gap-3 border-2 border-red-700 bg-red-50 p-3">
          <p className="text-sm text-red-950">
            <strong>最终确认：</strong>该标签、复核人和服务端时间戳提交后不可编辑、撤销或覆盖。
          </p>
          <div className="flex shrink-0 gap-2">
            <Button type="button" variant="outline" onClick={() => setPendingRequest(null)}>
              返回检查
            </Button>
            <Button type="button" disabled={mutation.isPending} onClick={commit}>
              {mutation.isPending ? <RefreshCw className="animate-spin" /> : <Check />}确认永久提交
            </Button>
          </div>
        </div>
      ) : (
        <Button type="submit" className="mt-3 w-full" disabled={!decision}>
          复核并进入最终确认
        </Button>
      )}
    </form>
  );
}

function CompletedDecision({ existing }: { existing: NonNullable<ExistingDecision> }) {
  const label: Record<string, string> = {
    confirmed: "已确认成立",
    false_positive: "已确认误报",
    clearance_confirmed: "已确认放行正确",
    missed_issue: "已发现漏放问题",
  };
  return (
    <div className="sticky bottom-0 border-t-4 border-emerald-900 bg-emerald-50 p-5">
      <div className="flex items-start gap-3">
        <CheckCircle2 className="mt-0.5 size-5 text-emerald-800" />
        <div className="min-w-0">
          <p className="font-bold text-emerald-950">
            {label[existing.decision] ?? existing.decision}
          </p>
          <p className="mt-1 break-all font-mono text-[10px] text-emerald-900">
            Reviewer {existing.reviewer_id} · {formatDateTime(existing.reviewed_at)}
          </p>
          {existing.note ? (
            <p className="mt-3 whitespace-pre-wrap break-words border-l-2 border-emerald-700 pl-3 text-sm">
              {existing.note}
            </p>
          ) : null}
          <p className="mt-3 text-xs text-emerald-900">
            该结论为不可变最终事实，界面不提供编辑、撤销或覆盖。
          </p>
        </div>
      </div>
    </div>
  );
}

function ConfigPanel({
  current,
  loading,
  error,
  canConfigure,
  onSaved,
}: {
  current: SamplingConfig | null;
  loading: boolean;
  error: string | null;
  canConfigure: boolean;
  onSaved: (version: number) => void;
}) {
  const save = useSaveSamplingConfig();
  const [expanded, setExpanded] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);

  function submit(event: FormEvent<HTMLFormElement>): void {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    const parsed = samplingConfigSchema.safeParse({
      expected_current_version: current?.version ?? 0,
      rate_bps: Number(data.get("rate_bps")),
      min_sample_size: Number(data.get("min_sample_size")),
      max_sample_size: Number(data.get("max_sample_size")),
      change_reason: String(data.get("change_reason") ?? ""),
    });
    if (!parsed.success) {
      setFormError(parsed.error.issues[0]?.message ?? "请检查抽样配置");
      return;
    }
    save.mutate(
      { request: parsed.data, idempotencyKey: crypto.randomUUID() },
      {
        onSuccess: (result) => {
          setFormError(null);
          setExpanded(false);
          onSaved(result.version);
        },
        onError: (saveError) =>
          setFormError(
            isConflict(saveError)
              ? `配置已被其他用户更新：${saveError.message}`
              : errorMessage(saveError),
          ),
      },
    );
  }

  return (
    <section className="p-5">
      <div className="flex items-start justify-between gap-3">
        <div>
          <p className="font-mono text-[10px] uppercase tracking-[0.25em] text-slate-500">
            Sampling control
          </p>
          <h2 className="mt-1 font-bold">抽样配置</h2>
        </div>
        {current ? (
          <Badge className="rounded-none bg-slate-950">V{current.version}</Badge>
        ) : (
          <Badge variant="outline" className="rounded-none border-amber-700 text-amber-800">
            缺失
          </Badge>
        )}
      </div>
      {loading ? (
        <p className="mt-4 flex items-center gap-2 text-sm">
          <RefreshCw className="size-4 animate-spin" />
          读取配置
        </p>
      ) : null}
      {error ? (
        <p role="alert" className="mt-3 text-sm text-red-700">
          {error}
        </p>
      ) : null}
      {current ? (
        <dl className="mt-4 grid grid-cols-3 gap-px bg-slate-300 text-center">
          <MiniDatum label="比例" value={`${current.rate_bps} bps`} />
          <MiniDatum label="最小" value={String(current.min_sample_size)} />
          <MiniDatum label="最大" value={String(current.max_sample_size)} />
        </dl>
      ) : (
        <div className="mt-4 border-2 border-amber-700 bg-amber-50 p-3 text-sm leading-6 text-amber-950">
          没有隐式默认值。新报告会在任何写入前以 <code>SAMPLING_CONFIG_REQUIRED</code> 明确失败。
        </div>
      )}
      {canConfigure ? (
        <Button
          size="sm"
          variant="outline"
          className="mt-4 w-full"
          onClick={() => setExpanded((value) => !value)}
        >
          {expanded ? "收起配置" : current ? "追加新版本" : "创建 V1 配置"}
        </Button>
      ) : (
        <p className="mt-4 text-xs text-slate-600">只有配置管理员可以追加配置版本。</p>
      )}
      {expanded && canConfigure ? (
        <form className="mt-4 grid gap-3 border-t border-slate-400 pt-4" onSubmit={submit}>
          <label className="text-xs font-semibold">
            抽样比例（basis points）
            <Input
              name="rate_bps"
              type="number"
              min={1}
              max={10000}
              defaultValue={current?.rate_bps ?? 500}
              className="mt-1"
            />
          </label>
          <div className="grid grid-cols-2 gap-2">
            <label className="text-xs font-semibold">
              最小样本
              <Input
                name="min_sample_size"
                type="number"
                min={1}
                defaultValue={current?.min_sample_size ?? 10}
                className="mt-1"
              />
            </label>
            <label className="text-xs font-semibold">
              最大样本
              <Input
                name="max_sample_size"
                type="number"
                min={1}
                defaultValue={current?.max_sample_size ?? 100}
                className="mt-1"
              />
            </label>
          </div>
          <label className="text-xs font-semibold">
            变更说明
            <textarea
              name="change_reason"
              maxLength={500}
              className="mt-1 min-h-20 w-full border-2 border-slate-950 bg-white p-2 text-sm"
            />
          </label>
          {formError ? (
            <p role="alert" className="text-xs text-red-700">
              {formError}
            </p>
          ) : null}
          <Button type="submit" disabled={save.isPending}>
            {save.isPending ? <RefreshCw className="animate-spin" /> : <Save />}
            {save.isPending ? "保存中" : "追加不可变版本"}
          </Button>
        </form>
      ) : null}
    </section>
  );
}

function HeaderDatum({ label, value }: { label: string; value: string }) {
  return (
    <div className="bg-[#f5f2e9] px-4 py-3">
      <p className="font-mono text-[9px] uppercase tracking-widest text-slate-500">{label}</p>
      <p className="mt-1 text-sm font-bold">{value}</p>
    </div>
  );
}
function SummaryDatum({
  label,
  value,
  detail,
  alert = false,
}: {
  label: string;
  value: string;
  detail: string;
  alert?: boolean;
}) {
  return (
    <div className={cn("min-w-0 px-5 py-4", alert ? "bg-amber-100" : "bg-[#f5f2e9]")}>
      <p className="font-mono text-[9px] uppercase tracking-[0.2em] text-slate-500">{label}</p>
      <p className="mt-1 text-2xl font-black tabular-nums">{value}</p>
      <p className="mt-1 truncate text-xs text-slate-600" title={detail}>
        {detail}
      </p>
    </div>
  );
}
function MiniDatum({ label, value }: { label: string; value: string }) {
  return (
    <div className="bg-white px-2 py-2">
      <dt className="text-[9px] uppercase text-slate-500">{label}</dt>
      <dd className="mt-1 font-mono text-xs font-bold">{value}</dd>
    </div>
  );
}
function SectionLabel({ index, title }: { index: string; title: string }) {
  return (
    <div className="flex items-center gap-3">
      <span className="grid size-7 place-items-center bg-slate-950 font-mono text-[10px] text-white">
        {index}
      </span>
      <h3 className="font-bold">{title}</h3>
    </div>
  );
}
function JsonEvidence({
  value,
  compact = false,
}: {
  value: Record<string, unknown>;
  compact?: boolean;
}) {
  return (
    <pre
      className={cn(
        "mt-3 overflow-auto whitespace-pre-wrap break-words border-2 border-slate-950 bg-[#f5f2e9] p-3 font-mono text-[11px] leading-5",
        compact ? "max-h-40" : "max-h-72",
      )}
    >
      {JSON.stringify(value, null, 2)}
    </pre>
  );
}
function FingerprintStrip({ label, value }: { label: string; value: string }) {
  return (
    <div className="min-w-0 bg-slate-950 px-4 py-2 text-slate-300">
      <span className="text-slate-500">{label}: </span>
      <span className="break-all">{value}</span>
    </div>
  );
}
function RailState({
  children,
  loading = false,
  error = false,
}: {
  children: ReactNode;
  loading?: boolean;
  error?: boolean;
}) {
  return (
    <div
      role={error ? "alert" : undefined}
      className={cn(
        "grid min-h-40 place-items-center p-5 text-center text-sm",
        error ? "text-red-800" : "text-slate-600",
      )}
    >
      <span>
        {loading ? (
          <RefreshCw className="mx-auto mb-2 size-5 animate-spin" />
        ) : error ? (
          <AlertTriangle className="mx-auto mb-2 size-5" />
        ) : (
          <CircleDashed className="mx-auto mb-2 size-5" />
        )}
        {children}
      </span>
    </div>
  );
}
function WorkspaceState({
  icon,
  title,
  text,
  error = false,
}: {
  icon: ReactNode;
  title: string;
  text: string;
  error?: boolean;
}) {
  return (
    <div
      role={error ? "alert" : undefined}
      className="grid min-h-[650px] place-items-center bg-[linear-gradient(90deg,#e5e7eb_1px,transparent_1px),linear-gradient(#e5e7eb_1px,transparent_1px)] bg-[size:24px_24px] p-10"
    >
      <div
        className={cn(
          "max-w-md border-2 border-slate-950 bg-white p-7 text-center shadow-[8px_8px_0_#0f172a]",
          error && "border-red-800 shadow-[8px_8px_0_#991b1b]",
        )}
      >
        {icon}
        <h2 className="mt-4 text-xl font-bold">{title}</h2>
        <p className="mt-2 text-sm leading-6 text-slate-600">{text}</p>
      </div>
    </div>
  );
}
function ReviewUnavailable() {
  return (
    <div role="alert" className="border-2 border-slate-950 bg-amber-50 p-6">
      <ShieldAlert className="size-6" />
      <h1 className="mt-3 text-xl font-bold">复核台不可用</h1>
      <p className="mt-2 text-sm">
        当前会话没有 review:read 权限。请从有权限的账号进入；前端隐藏不替代服务端鉴权。
      </p>
    </div>
  );
}
