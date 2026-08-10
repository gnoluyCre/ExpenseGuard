import { useEffect, useState } from "react";
import { AlertTriangle, Bot, CheckCircle2, CircleDashed, Play, Search } from "lucide-react";

import { hasPermission, PERMISSIONS, type CurrentUser, type Investigation } from "@/api/client";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  useInvestigationCapability,
  useInvestigationHistory,
  useRunInvestigation,
} from "@/investigations/useInvestigations";
import { cn } from "@/lib/utils";

const OUTCOME_LABELS: Record<string, string> = {
  sufficient: "证据充分",
  insufficient: "证据不足",
  unavailable: "能力不可用",
  max_steps: "达到步数上限",
  failed: "调查失败",
};

export function InvestigationPanel({
  detectionRunId,
  findingId,
  user,
}: {
  detectionRunId: string;
  findingId: string;
  user: CurrentUser;
}) {
  const capability = useInvestigationCapability();
  const history = useInvestigationHistory(detectionRunId, findingId);
  const run = useRunInvestigation(detectionRunId, findingId);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const canRun = hasPermission(user, PERMISSIONS.batchImport);

  useEffect(() => {
    const first = history.data?.items[0]?.run.id ?? null;
    setSelectedId((current) =>
      current && history.data?.items.some((item) => item.run.id === current) ? current : first,
    );
  }, [history.data]);

  const selected =
    history.data?.items.find((item) => item.run.id === selectedId) ?? history.data?.items[0];

  function start(): void {
    if (!window.confirm("确认对当前统计候选启动一次新的只读异常取证？")) return;
    setMessage(null);
    run.mutate(undefined, {
      onSuccess: (result) => {
        setSelectedId(result.run.id);
        setMessage(result.reused_existing ? "已复用同一请求的调查记录" : "异常取证已形成终态");
      },
      onError: (error) => setMessage(error.message),
    });
  }

  return (
    <section className="mt-5 rounded-xl border border-indigo-200 bg-indigo-50/40 p-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <div className="flex items-center gap-2 text-xs font-semibold tracking-[0.14em] text-indigo-700">
            <Bot className="size-4" aria-hidden="true" />
            ANOMALY INVESTIGATION
          </div>
          <h4 className="mt-1 font-semibold">异常取证</h4>
          <p className="mt-1 text-xs text-muted-foreground">
            只读 Agent；所有模型输入均经过字段白名单与稳定 token 脱敏。
          </p>
        </div>
        <div className="flex items-center gap-2">
          {capability.data ? (
            <Badge variant={capability.data.status === "enabled" ? "secondary" : "outline"}>
              {capability.data.status === "enabled" ? "READY" : "UNAVAILABLE"}
            </Badge>
          ) : null}
          {canRun && capability.data?.status === "enabled" ? (
            <Button size="sm" onClick={start} disabled={run.isPending}>
              <Play aria-hidden="true" />
              {run.isPending ? "取证中…" : "启动取证"}
            </Button>
          ) : null}
        </div>
      </div>

      {capability.isLoading ? <PanelState>正在读取取证能力…</PanelState> : null}
      {capability.isError ? <PanelState error>{capability.error.message}</PanelState> : null}
      {capability.data?.status === "unavailable" ? (
        <div className="mt-3 rounded-lg border border-amber-200 bg-amber-50 p-3 text-sm text-amber-950">
          <strong>{capability.data.reason_code}</strong>：{capability.data.reason}
          。当前候选请转人工； 填写模型 API 与脱敏配置后即可启用。
        </div>
      ) : null}
      {message ? (
        <p className={cn("mt-3 text-sm", run.isError ? "text-destructive" : "text-emerald-800")}>
          {message}
        </p>
      ) : null}

      {history.isLoading ? <PanelState>正在读取调查历史…</PanelState> : null}
      {history.isError ? <PanelState error>{history.error.message}</PanelState> : null}
      {history.data?.items.length === 0 ? (
        <PanelState>
          <CircleDashed className="mr-2 size-4" aria-hidden="true" />
          尚无调查记录
        </PanelState>
      ) : null}
      {history.data && history.data.items.length > 0 ? (
        <div className="mt-4 grid gap-4 xl:grid-cols-[220px_minmax(0,1fr)]">
          <div className="space-y-2" aria-label="调查历史">
            {history.data.items.map((item) => (
              <button
                key={item.run.id}
                type="button"
                onClick={() => setSelectedId(item.run.id)}
                className={cn(
                  "w-full rounded-lg border bg-background p-3 text-left text-xs",
                  selected?.run.id === item.run.id && "border-indigo-500 ring-1 ring-indigo-200",
                )}
              >
                <span className="font-medium">
                  {OUTCOME_LABELS[item.result?.outcome ?? ""] ?? "进行中"}
                </span>
                <span className="mt-1 block font-mono text-[10px] text-muted-foreground">
                  {item.run.id}
                </span>
                <span className="mt-1 block text-muted-foreground">
                  {new Date(item.run.created_at).toLocaleString("zh-CN")}
                </span>
              </button>
            ))}
          </div>
          {selected ? <InvestigationDetailView investigation={selected} /> : null}
        </div>
      ) : null}
    </section>
  );
}

function InvestigationDetailView({ investigation }: { investigation: Investigation }) {
  const result = investigation.result;
  return (
    <div className="min-w-0 rounded-lg border bg-background p-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <p className="text-xs font-medium text-muted-foreground">
            {investigation.run.provider_kind} · {investigation.run.provider_model} · 最多
            {investigation.run.max_steps} 步
          </p>
          <p className="mt-1 font-mono text-[10px] text-muted-foreground">
            RUN {investigation.run.id} / FINDING {investigation.run.correlation_finding_id}
          </p>
        </div>
        <Badge variant={result?.outcome === "sufficient" ? "destructive" : "outline"}>
          {OUTCOME_LABELS[result?.outcome ?? ""] ?? "进行中"}
        </Badge>
      </div>
      {result ? (
        <div className="mt-3 rounded-lg border p-3 text-sm">
          <div className="flex items-center gap-2 font-medium">
            {result.evidence_sufficient ? (
              <CheckCircle2 className="size-4 text-rose-700" aria-hidden="true" />
            ) : (
              <AlertTriangle className="size-4 text-amber-700" aria-hidden="true" />
            )}
            {result.reason_code}
          </div>
          <p className="mt-2 whitespace-pre-wrap break-words text-muted-foreground">
            {result.summary}
          </p>
          {result.citations.map((citation, index) => (
            <pre
              key={index}
              className="mt-2 overflow-auto whitespace-pre-wrap break-all rounded bg-muted p-2 text-xs"
            >
              {JSON.stringify(citation, null, 2)}
            </pre>
          ))}
        </div>
      ) : null}
      <ol className="mt-4 space-y-3" aria-label="取证步骤时间线">
        {investigation.steps.map((step) => (
          <li key={step.id} className="rounded-lg border p-3 text-sm">
            <div className="flex items-center gap-2 font-medium">
              <Search className="size-4 text-indigo-700" aria-hidden="true" />
              步骤 {step.step_no} · {step.tool_name ?? "终止判断"}
            </div>
            <p className="mt-1 whitespace-pre-wrap break-words text-muted-foreground">
              {step.decision_summary}
            </p>
            {step.tool_input ? (
              <pre className="mt-2 overflow-auto whitespace-pre-wrap break-all rounded bg-muted p-2 text-xs">
                {JSON.stringify({ input: step.tool_input, output: step.tool_output }, null, 2)}
              </pre>
            ) : null}
          </li>
        ))}
      </ol>
    </div>
  );
}

function PanelState({ children, error = false }: { children: React.ReactNode; error?: boolean }) {
  return (
    <div
      role={error ? "alert" : undefined}
      className={cn(
        "mt-3 flex min-h-20 items-center justify-center text-sm",
        error && "text-destructive",
      )}
    >
      {children}
    </div>
  );
}
