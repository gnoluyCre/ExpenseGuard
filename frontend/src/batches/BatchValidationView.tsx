import { useEffect, useState } from "react";
import {
  AlertTriangle,
  CheckCircle2,
  ChevronLeft,
  ChevronRight,
  CopyPlus,
  RefreshCw,
  ScanSearch,
  ShieldCheck,
} from "lucide-react";

import {
  hasPermission,
  PERMISSIONS,
  type CurrentUser,
  type FindingItem,
  type RevisionReason,
} from "@/api/client";
import {
  type FindingVerdict,
  useBatchFindings,
  useBatchValidation,
  useCreateBatchRevision,
  useValidateBatch,
} from "@/batches/useBatchValidation";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

const VERDICT_LABELS: Record<FindingItem["verdict"], string> = {
  passed: "已通过",
  flagged: "已标记",
  manual_review: "需人工复核",
};

const REVISION_LABELS: Record<RevisionReason, string> = {
  ruleset_change: "应用新规则集",
  mapping_change: "重新映射字段",
  policy_change: "应用新制度绑定",
};

function makeIdempotencyKey(): string {
  return `revision-${globalThis.crypto.randomUUID()}`;
}

export interface BatchValidationViewProps {
  fileVersionId: string;
  user: CurrentUser;
  onRevisionCreated?: ((fileVersionId: string) => void) | undefined;
}

export function BatchValidationView({
  fileVersionId,
  user,
  onRevisionCreated,
}: BatchValidationViewProps) {
  const canMutate = hasPermission(user, PERMISSIONS.batchImport);
  const [verdict, setVerdict] = useState<FindingVerdict>("flagged");
  const [page, setPage] = useState(1);
  const [message, setMessage] = useState<string | null>(null);
  const validation = useBatchValidation(fileVersionId);
  const findings = useBatchFindings(fileVersionId, page, verdict, Boolean(validation.data));
  const validateBatch = useValidateBatch(fileVersionId);
  const createRevision = useCreateBatchRevision(fileVersionId);

  useEffect(() => setPage(1), [verdict, fileVersionId]);

  function runValidation(): void {
    setMessage(null);
    validateBatch.mutate(undefined, {
      onSuccess: (result) =>
        setMessage(result.reused_existing ? "已复用现有校验结果" : "确定性校验已完成"),
      onError: (error) => setMessage(error.message),
    });
  }

  function deriveRevision(reason: RevisionReason): void {
    setMessage(null);
    createRevision.mutate(
      { reason, idempotencyKey: makeIdempotencyKey() },
      {
        onSuccess: (result) => {
          setMessage(
            result.reused_existing
              ? `已复用 revision ${result.revision_no}`
              : `已创建 revision ${result.revision_no}`,
          );
          onRevisionCreated?.(result.file_version_id);
        },
        onError: (error) => setMessage(error.message),
      },
    );
  }

  if (validation.isLoading) return <WorkspaceMessage>正在读取校验快照…</WorkspaceMessage>;
  if (validation.isError)
    return <WorkspaceMessage error>{validation.error.message}</WorkspaceMessage>;
  if (validation.data === undefined) return null;

  if (validation.data === null) {
    return (
      <section className="grid min-h-96 place-items-center bg-[linear-gradient(135deg,transparent_0_49%,hsl(var(--border)/.28)_49%_51%,transparent_51%_100%)] bg-[length:16px_16px] p-8">
        <div className="max-w-lg border bg-background p-7 shadow-[6px_6px_0_0_hsl(var(--border))]">
          <ScanSearch className="mb-5 size-9 text-muted-foreground" aria-hidden="true" />
          <h3 className="text-lg font-semibold">尚无确定性校验快照</h3>
          <p className="mt-2 text-sm leading-6 text-muted-foreground">
            校验会冻结当前映射与规则版本。后续规则或映射变化必须创建派生
            revision，已有结果不会被覆盖。
          </p>
          {canMutate ? (
            <Button className="mt-6" onClick={runValidation} disabled={validateBatch.isPending}>
              {validateBatch.isPending ? (
                <RefreshCw className="animate-spin" aria-hidden="true" />
              ) : (
                <ShieldCheck aria-hidden="true" />
              )}
              执行确定性校验
            </Button>
          ) : (
            <p className="mt-5 text-sm text-muted-foreground">
              当前角色可读取结果，但不能触发校验。
            </p>
          )}
          {message ? (
            <p role={validateBatch.isError ? "alert" : "status"} className="mt-4 text-sm">
              {message}
            </p>
          ) : null}
        </div>
      </section>
    );
  }

  const summary = validation.data;
  const pageCount = findings.data ? Math.max(1, Math.ceil(findings.data.total / 50)) : 1;

  return (
    <section className="min-w-0">
      <div className="border-b bg-slate-950 px-6 py-5 text-slate-50">
        <div className="flex items-start justify-between gap-5">
          <div className="min-w-0">
            <div className="flex items-center gap-2 text-xs font-semibold uppercase tracking-[0.18em] text-emerald-300">
              <CheckCircle2 className="size-4" aria-hidden="true" />
              validation snapshot
            </div>
            <p className="mt-3 text-xs text-slate-400">规则集指纹</p>
            <p className="mt-1 break-all font-mono text-sm leading-5 text-slate-100">
              {summary.ruleset_fingerprint}
            </p>
            <p className="mt-2 break-all font-mono text-xs text-slate-400">
              映射 {summary.mapping_version_id}
            </p>
          </div>
          {canMutate ? (
            <Button
              variant="outline"
              className="shrink-0 border-slate-600 bg-transparent text-slate-100 hover:bg-slate-800 hover:text-white"
              onClick={runValidation}
              disabled={validateBatch.isPending}
            >
              {validateBatch.isPending ? (
                <RefreshCw className="animate-spin" aria-hidden="true" />
              ) : (
                <ShieldCheck aria-hidden="true" />
              )}
              再次校验 / 复用
            </Button>
          ) : null}
        </div>
        <div className="mt-5 grid grid-cols-4 gap-px overflow-hidden border border-slate-700 bg-slate-700">
          <SnapshotMetric label="机械通过" value={summary.passed_count} tone="success" />
          <SnapshotMetric label="已标记" value={summary.flagged_count} tone="danger" />
          <SnapshotMetric label="人工复核" value={summary.manual_review_count} tone="warning" />
          <SnapshotMetric label="解析失败" value={summary.parse_failed_count} />
        </div>
        <p className="mt-3 text-right text-xs tabular-nums text-slate-400">
          已求值 {summary.evaluated_row_count} / 总计 {summary.total_row_count} 行
        </p>
      </div>

      {message ? (
        <div
          role={validateBatch.isError || createRevision.isError ? "alert" : "status"}
          className={cn(
            "border-b px-6 py-2.5 text-sm",
            validateBatch.isError || createRevision.isError
              ? "bg-red-50 text-red-800"
              : "bg-emerald-50 text-emerald-800",
          )}
        >
          {message}
        </div>
      ) : null}

      <div className="flex items-center justify-between gap-4 border-b bg-muted/20 px-6 py-3">
        <div className="flex gap-1" role="tablist" aria-label="校验判定筛选">
          {(["flagged", "manual_review"] as const).map((item) => (
            <button
              key={item}
              type="button"
              role="tab"
              aria-selected={verdict === item}
              onClick={() => setVerdict(item)}
              className={cn(
                "border px-3 py-1.5 text-sm font-medium transition-colors",
                verdict === item
                  ? "border-slate-900 bg-slate-900 text-white"
                  : "border-transparent text-muted-foreground hover:border-border hover:bg-background",
              )}
            >
              {VERDICT_LABELS[item]}
            </button>
          ))}
        </div>
        {canMutate ? (
          <div className="flex shrink-0 gap-2">
            {(Object.keys(REVISION_LABELS) as RevisionReason[]).map((reason) => (
              <Button
                key={reason}
                size="sm"
                variant="outline"
                disabled={createRevision.isPending}
                onClick={() => deriveRevision(reason)}
              >
                <CopyPlus aria-hidden="true" />
                {REVISION_LABELS[reason]}
              </Button>
            ))}
          </div>
        ) : null}
      </div>

      <FindingsList
        query={findings}
        verdict={verdict}
        page={page}
        pageCount={pageCount}
        setPage={setPage}
      />
    </section>
  );
}

function SnapshotMetric({
  label,
  value,
  tone,
}: {
  label: string;
  value: number;
  tone?: "success" | "warning" | "danger" | undefined;
}) {
  return (
    <div className="bg-slate-950 px-4 py-3">
      <p className="text-xs text-slate-400">{label}</p>
      <p
        className={cn(
          "mt-1 text-2xl font-semibold tabular-nums",
          tone === "success" && "text-emerald-300",
          tone === "warning" && "text-amber-300",
          tone === "danger" && "text-rose-300",
        )}
      >
        {value}
      </p>
    </div>
  );
}

function FindingsList({
  query,
  verdict,
  page,
  pageCount,
  setPage,
}: {
  query: ReturnType<typeof useBatchFindings>;
  verdict: FindingVerdict;
  page: number;
  pageCount: number;
  setPage: React.Dispatch<React.SetStateAction<number>>;
}) {
  if (query.isLoading) return <WorkspaceMessage>正在读取证据链…</WorkspaceMessage>;
  if (query.isError) return <WorkspaceMessage error>{query.error.message}</WorkspaceMessage>;
  if (!query.data) return null;

  return (
    <div className="p-6">
      {query.data.items.length === 0 ? (
        <div className="border border-dashed p-8 text-center text-sm text-muted-foreground">
          当前筛选没有{VERDICT_LABELS[verdict]} findings。
        </div>
      ) : (
        <div className="grid gap-3">
          {query.data.items.map((finding) => (
            <FindingCard key={finding.id} finding={finding} />
          ))}
        </div>
      )}
      <div className="mt-5 flex items-center justify-between border-t pt-4 text-sm text-muted-foreground">
        <span className="tabular-nums">
          共 {query.data.total} 条 · 第 {page} / {pageCount} 页
        </span>
        <div className="flex gap-2">
          <Button
            size="sm"
            variant="outline"
            disabled={page <= 1 || query.isFetching}
            onClick={() => setPage((current) => Math.max(1, current - 1))}
          >
            <ChevronLeft aria-hidden="true" />
            上一页
          </Button>
          <Button
            size="sm"
            variant="outline"
            disabled={page >= pageCount || query.isFetching}
            onClick={() => setPage((current) => current + 1)}
          >
            下一页
            <ChevronRight aria-hidden="true" />
          </Button>
        </div>
      </div>
    </div>
  );
}

function FindingCard({ finding }: { finding: FindingItem }) {
  const manual = finding.verdict === "manual_review";
  return (
    <article className="min-w-0 overflow-hidden border bg-background">
      <div
        className={cn(
          "grid grid-cols-[76px_minmax(0,1fr)_auto] items-center gap-3 border-b px-4 py-2.5",
          manual ? "bg-amber-50" : "bg-rose-50",
        )}
      >
        <span className="font-mono text-sm font-semibold">行 {finding.row_no}</span>
        <div className="min-w-0">
          <p className="break-all font-mono text-xs font-semibold">{finding.rule_id}</p>
          <p className="mt-0.5 break-all text-xs text-muted-foreground">
            {finding.rule_kind} · v{finding.rule_version ?? "未生效"}
          </p>
        </div>
        <div className="flex flex-wrap justify-end gap-1.5">
          <Badge variant="outline">{finding.outcome}</Badge>
          <Badge variant={manual ? "outline" : "destructive"}>
            {VERDICT_LABELS[finding.verdict]}
          </Badge>
        </div>
      </div>
      <div className="grid min-w-0 grid-cols-[minmax(0,0.8fr)_minmax(0,1.2fr)]">
        <div className="min-w-0 border-r p-4">
          <p className="break-all font-mono text-xs font-semibold text-muted-foreground">
            {finding.reason_code}
          </p>
          <p className="mt-2 break-words text-sm leading-6">{finding.reasoning}</p>
        </div>
        <div className="min-w-0 bg-slate-950 p-4 text-slate-200">
          <p className="mb-2 text-[10px] font-semibold uppercase tracking-[0.16em] text-slate-500">
            structured evidence
          </p>
          <pre className="max-h-56 overflow-auto whitespace-pre-wrap break-all font-mono text-[11px] leading-5">
            {JSON.stringify(finding.evidence, null, 2)}
          </pre>
        </div>
      </div>
    </article>
  );
}

function WorkspaceMessage({
  children,
  error = false,
}: {
  children: React.ReactNode;
  error?: boolean;
}) {
  return (
    <div
      className={cn(
        "flex min-h-72 items-center justify-center p-8 text-sm text-muted-foreground",
        error && "text-destructive",
      )}
      role={error ? "alert" : undefined}
    >
      {error ? <AlertTriangle className="mr-2 size-4" aria-hidden="true" /> : null}
      {children}
    </div>
  );
}
