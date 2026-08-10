import { useEffect, useState } from "react";
import { AlertTriangle, CheckCircle2, CircleDashed, Play, ShieldAlert } from "lucide-react";

import {
  hasPermission,
  PERMISSIONS,
  type CapabilityStatus,
  type CurrentUser,
  type DetectorKind,
} from "@/api/client";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  useBatchDetection,
  useCorrelationFinding,
  useCorrelationFindings,
  useRunDetection,
} from "@/detection/useDetection";
import { InvestigationPanel } from "@/investigations/InvestigationPanel";
import { cn } from "@/lib/utils";

const DETECTORS = [
  "split_invoice",
  "sequential_invoice",
  "frequency_anomaly",
  "spatiotemporal_tier0",
] as const satisfies readonly DetectorKind[];

const LABELS: Record<DetectorKind, string> = {
  split_invoice: "拆单候选",
  sequential_invoice: "发票连号",
  frequency_anomaly: "频次离群",
  spatiotemporal_tier0: "时空冲突 T0",
};

const STATUS_LABELS: Record<CapabilityStatus, string> = {
  enabled: "可用",
  degraded: "降级",
  unavailable: "不可用",
};

function statusClass(status: CapabilityStatus): string {
  if (status === "enabled") return "border-emerald-300 bg-emerald-50 text-emerald-900";
  if (status === "degraded") return "border-amber-300 bg-amber-50 text-amber-950";
  return "border-slate-300 bg-slate-100 text-slate-700";
}

export function BatchDetectionView({
  fileVersionId,
  user,
}: {
  fileVersionId: string;
  user: CurrentUser;
}) {
  const snapshot = useBatchDetection(fileVersionId);
  const runDetection = useRunDetection(fileVersionId);
  const canRun = hasPermission(user, PERMISSIONS.batchImport);
  const [detector, setDetector] = useState<DetectorKind | undefined>();
  const [status, setStatus] = useState<CapabilityStatus | undefined>();
  const [offset, setOffset] = useState(0);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [rowOffset, setRowOffset] = useState(0);
  const [message, setMessage] = useState<string | null>(null);
  const runId = snapshot.data?.run?.id ?? null;
  const findings = useCorrelationFindings(runId, {
    ...(detector ? { detector } : {}),
    ...(status ? { capabilityStatus: status } : {}),
    offset,
    limit: 50,
  });
  const detail = useCorrelationFinding(selectedId, rowOffset);

  useEffect(() => {
    setOffset(0);
    setSelectedId(null);
  }, [detector, status]);

  useEffect(() => {
    const first = findings.data?.items[0]?.id ?? null;
    setSelectedId((current) =>
      current && findings.data?.items.some((item) => item.id === current) ? current : first,
    );
  }, [findings.data]);

  function run(): void {
    if (!window.confirm("确认创建或复用一个不可变关联检测快照？运行不会修改历史结果。")) return;
    setMessage(null);
    runDetection.mutate(undefined, {
      onSuccess: (result) =>
        setMessage(result.reused_existing ? "已复用相同输入与配置的运行快照" : "关联检测完成"),
      onError: (error) => setMessage(error.message),
    });
  }

  if (snapshot.isLoading) return <StateMessage>正在读取关联检测补充快照…</StateMessage>;
  if (snapshot.isError) return <StateMessage error>{snapshot.error.message}</StateMessage>;
  if (!snapshot.data) return null;
  const { run: runSnapshot, capabilities, config_stale: stale } = snapshot.data;

  return (
    <div className="grid gap-4 p-5">
      <div className="flex items-start justify-between gap-4 rounded-xl border bg-slate-950 p-4 text-slate-50">
        <div className="min-w-0">
          <div className="flex items-center gap-2 text-xs font-semibold tracking-[0.16em] text-slate-400">
            <ShieldAlert className="size-4" aria-hidden="true" />
            CORRELATION SUPPLEMENT
          </div>
          <p className="mt-2 text-sm text-slate-300">
            仅展示确定性统计候选；候选尚未经过人工或 Agent 取证，不代表已确认违规。
          </p>
        </div>
        <div className="flex shrink-0 items-center gap-2">
          {runSnapshot ? (
            <Badge variant={stale ? "destructive" : "secondary"}>
              {stale ? "CONFIG STALE" : "CURRENT"}
            </Badge>
          ) : (
            <Badge variant="outline">RUN ABSENT</Badge>
          )}
          {canRun ? (
            <Button size="sm" onClick={run} disabled={runDetection.isPending}>
              <Play aria-hidden="true" />
              {runDetection.isPending ? "运行中…" : "执行检测"}
            </Button>
          ) : (
            <span className="text-xs text-slate-400">只读</span>
          )}
        </div>
      </div>
      {message ? (
        <p
          role={runDetection.isError ? "alert" : "status"}
          className={cn(
            "text-sm",
            runDetection.isError ? "text-destructive" : "text-muted-foreground",
          )}
        >
          {message}
        </p>
      ) : null}

      {!runSnapshot ? (
        <StateMessage>
          {snapshot.data.current_config_fingerprint
            ? "尚无运行快照。具备权限的账号可显式触发检测。"
            : "尚未配置关联检测 profile；请先由配置管理员创建 v1。"}
        </StateMessage>
      ) : (
        <>
          <div className="grid grid-cols-[0.8fr_1fr_1fr_0.55fr_0.55fr_0.55fr] gap-3 rounded-lg border p-3">
            <Metric label="PROFILE" value={`v${runSnapshot.config_version}`} />
            <Metric label="RUN FINGERPRINT" value={runSnapshot.run_fingerprint} mono />
            <Metric label="INPUT FINGERPRINT" value={runSnapshot.input_fingerprint} mono />
            <Metric label="SOURCE" value={String(runSnapshot.source_row_count)} />
            <Metric label="PARSED" value={String(runSnapshot.parsed_row_count)} />
            <Metric label="FINDINGS" value={String(runSnapshot.finding_count)} />
          </div>

          <div className="grid grid-cols-4 gap-3">
            {capabilities.map((capability) => (
              <article
                key={capability.detector}
                className={cn("rounded-lg border p-3", statusClass(capability.status))}
              >
                <div className="flex items-start justify-between gap-2">
                  <div>
                    <p className="text-sm font-semibold">{LABELS[capability.detector]}</p>
                    <code className="text-[10px] opacity-70">{capability.detector_version}</code>
                  </div>
                  <Badge variant="outline">{STATUS_LABELS[capability.status]}</Badge>
                </div>
                <p className="mt-2 min-h-9 text-xs">{capability.reason}</p>
                <dl className="mt-3 grid grid-cols-4 gap-1 border-t pt-2 text-center text-[10px]">
                  <Count label="源" value={capability.details.source_row_count} />
                  <Count label="可用" value={capability.details.eligible_row_count} />
                  <Count label="排除" value={capability.details.excluded_row_count} />
                  <Count label="候选" value={capability.finding_count} />
                </dl>
                {capability.status === "enabled" && capability.finding_count === 0 ? (
                  <p className="mt-2 text-[11px] font-medium">检测能力正常，本次零候选</p>
                ) : null}
              </article>
            ))}
          </div>

          <div className="grid grid-cols-[minmax(300px,0.72fr)_minmax(0,1.4fr)] overflow-hidden rounded-xl border">
            <section className="border-r bg-muted/15">
              <div className="grid grid-cols-2 gap-2 border-b p-3">
                <label className="grid gap-1 text-xs font-medium">
                  Detector
                  <select
                    aria-label="候选 detector 筛选"
                    className="h-9 rounded-md border bg-background px-2 text-sm"
                    value={detector ?? ""}
                    onChange={(event) =>
                      setDetector(
                        event.target.value ? (event.target.value as DetectorKind) : undefined,
                      )
                    }
                  >
                    <option value="">全部</option>
                    {DETECTORS.map((kind) => (
                      <option key={kind} value={kind}>
                        {LABELS[kind]}
                      </option>
                    ))}
                  </select>
                </label>
                <label className="grid gap-1 text-xs font-medium">
                  Capability
                  <select
                    aria-label="候选能力状态筛选"
                    className="h-9 rounded-md border bg-background px-2 text-sm"
                    value={status ?? ""}
                    onChange={(event) =>
                      setStatus(
                        event.target.value ? (event.target.value as CapabilityStatus) : undefined,
                      )
                    }
                  >
                    <option value="">全部</option>
                    <option value="enabled">可用</option>
                    <option value="degraded">降级</option>
                    <option value="unavailable">不可用</option>
                  </select>
                </label>
              </div>
              {findings.isLoading ? <StateMessage>正在读取候选…</StateMessage> : null}
              {findings.isError ? (
                <StateMessage error>{findings.error.message}</StateMessage>
              ) : null}
              {findings.data?.items.map((item) => (
                <button
                  key={item.id}
                  type="button"
                  onClick={() => {
                    setSelectedId(item.id);
                    setRowOffset(0);
                  }}
                  className={cn(
                    "grid w-full gap-1 border-b p-3 text-left text-sm hover:bg-muted/50",
                    selectedId === item.id && "bg-primary/[0.06]",
                  )}
                >
                  <span className="flex items-center justify-between gap-2 font-medium">
                    <span>{LABELS[item.detector]}</span>
                    <Badge variant="outline">{item.participating_row_count} 行</Badge>
                  </span>
                  <span className="line-clamp-2 text-xs text-muted-foreground">
                    {item.reasoning}
                  </span>
                  <code className="truncate text-[10px] text-muted-foreground">
                    {item.finding_key}
                  </code>
                </button>
              ))}
              {findings.data && findings.data.items.length === 0 ? (
                <StateMessage>
                  <CheckCircle2 className="mr-2 size-4 text-emerald-700" aria-hidden="true" />
                  当前筛选下没有统计候选
                </StateMessage>
              ) : null}
              {findings.data ? (
                <div className="flex items-center justify-between border-t p-3 text-xs text-muted-foreground">
                  <span>共 {findings.data.total} 项</span>
                  <div className="flex gap-2">
                    <Button
                      size="sm"
                      variant="outline"
                      disabled={offset === 0}
                      onClick={() => setOffset(Math.max(0, offset - 50))}
                    >
                      上一页
                    </Button>
                    <Button
                      size="sm"
                      variant="outline"
                      disabled={offset + findings.data.limit >= findings.data.total}
                      onClick={() => setOffset(offset + 50)}
                    >
                      下一页
                    </Button>
                  </div>
                </div>
              ) : null}
            </section>
            <FindingDetailPane
              query={detail}
              rowOffset={rowOffset}
              setRowOffset={setRowOffset}
              detectionRunId={runId ?? ""}
              user={user}
            />
          </div>
        </>
      )}
    </div>
  );
}

function FindingDetailPane({
  query,
  rowOffset,
  setRowOffset,
  detectionRunId,
  user,
}: {
  query: ReturnType<typeof useCorrelationFinding>;
  rowOffset: number;
  setRowOffset: (value: number) => void;
  detectionRunId: string;
  user: CurrentUser;
}) {
  if (query.isLoading) return <StateMessage>正在读取候选证据…</StateMessage>;
  if (query.isError) return <StateMessage error>{query.error.message}</StateMessage>;
  if (!query.data)
    return (
      <StateMessage>
        <CircleDashed className="mr-2 size-4" aria-hidden="true" />
        选择左侧候选查看证据
      </StateMessage>
    );
  const item = query.data;
  return (
    <section className="min-w-0 p-4">
      <div className="flex items-start justify-between gap-3">
        <div>
          <p className="text-xs font-semibold tracking-[0.14em] text-muted-foreground">
            TYPED STATISTICAL EVIDENCE
          </p>
          <h3 className="mt-1 font-semibold">{LABELS[item.detector]}</h3>
        </div>
        <Badge variant="outline">
          {item.completed}/{item.total} 行
        </Badge>
      </div>
      <p className="mt-4 rounded-lg border border-amber-200 bg-amber-50 p-3 text-sm text-amber-950">
        {item.reasoning}
      </p>
      <pre className="mt-3 max-h-64 overflow-auto whitespace-pre-wrap break-all rounded-lg bg-slate-950 p-3 text-xs leading-5 text-slate-100">
        {JSON.stringify(item.evidence.facts, null, 2)}
      </pre>
      <InvestigationPanel detectionRunId={detectionRunId} findingId={item.id} user={user} />
      <div className="mt-4 overflow-x-auto rounded-lg border">
        <div className="min-w-[720px]">
          <div className="grid grid-cols-[60px_70px_1fr_1fr] bg-muted/50 px-3 py-2 text-xs font-medium text-muted-foreground">
            <span>序号</span>
            <span>行号</span>
            <span>原始证据</span>
            <span>规范化证据</span>
          </div>
          {item.rows.map((row) => (
            <div
              key={`${row.ordinal}-${row.row_no}`}
              className="grid grid-cols-[60px_70px_1fr_1fr] gap-2 border-t px-3 py-2 text-xs"
            >
              <span>{row.ordinal}</span>
              <span className="font-mono">{row.row_no}</span>
              <code className="break-all">{JSON.stringify(row.raw)}</code>
              <code className="break-all text-muted-foreground">
                {JSON.stringify(row.normalized)}
              </code>
            </div>
          ))}
        </div>
      </div>
      <div className="mt-3 flex justify-end gap-2">
        <Button
          size="sm"
          variant="outline"
          disabled={rowOffset === 0}
          onClick={() => setRowOffset(Math.max(0, rowOffset - 50))}
        >
          上一组行
        </Button>
        <Button
          size="sm"
          variant="outline"
          disabled={item.completed >= item.total}
          onClick={() => setRowOffset(rowOffset + 50)}
        >
          下一组行
        </Button>
      </div>
    </section>
  );
}

function Count({ label, value }: { label: string; value: number }) {
  return (
    <div>
      <dt className="opacity-60">{label}</dt>
      <dd className="mt-0.5 font-mono text-xs font-semibold">{value}</dd>
    </div>
  );
}
function Metric({ label, value, mono = false }: { label: string; value: string; mono?: boolean }) {
  return (
    <div className="min-w-0">
      <div className="text-[10px] font-semibold tracking-[0.12em] text-muted-foreground">
        {label}
      </div>
      <div className={cn("mt-1 break-all text-sm", mono && "font-mono text-[10px] leading-4")}>
        {value}
      </div>
    </div>
  );
}
function StateMessage({ children, error = false }: { children: React.ReactNode; error?: boolean }) {
  return (
    <div
      role={error ? "alert" : undefined}
      className={cn(
        "flex min-h-48 items-center justify-center p-6 text-sm text-muted-foreground",
        error && "text-destructive",
      )}
    >
      {error ? <AlertTriangle className="mr-2 size-4" aria-hidden="true" /> : null}
      {children}
    </div>
  );
}
