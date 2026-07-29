import { useState } from "react";
import { AlertTriangle, ChevronDown, FileOutput, RefreshCw, ShieldCheck } from "lucide-react";

import { hasPermission, PERMISSIONS, type CurrentUser, type ReportItem } from "@/api/client";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  downloadReportExport,
  useBatchReport,
  useCreateReportExport,
  useGenerateReport,
  useReportItems,
  useReportParseErrors,
} from "@/reports/useReports";

export function BatchReportView({
  fileVersionId,
  user,
  onOpenRow,
}: {
  fileVersionId: string;
  user: CurrentUser;
  onOpenRow: (rowNo: number) => void;
}) {
  const canGenerate = hasPermission(user, PERMISSIONS.batchImport);
  const canExport = hasPermission(user, PERMISSIONS.reportExport);
  const report = useBatchReport(fileVersionId);
  const generate = useGenerateReport(fileVersionId);
  const [offset, setOffset] = useState(0);
  const reportId = report.data?.summary.report_run_id ?? null;
  const items = useReportItems(reportId, offset);
  const parseErrors = useReportParseErrors(reportId);
  const createExport = useCreateReportExport(reportId);
  const [exportError, setExportError] = useState<string | null>(null);

  if (report.isLoading) return <StatePanel text="读取冻结报告快照" loading />;
  if (report.isError) return <StatePanel text={report.error.message} error />;
  if (report.data === null) {
    return (
      <div className="grid min-h-96 place-items-center bg-[linear-gradient(135deg,#f8fafc_25%,transparent_25%),linear-gradient(315deg,#f8fafc_25%,transparent_25%)] bg-[size:24px_24px] p-8">
        <div className="max-w-md border-2 border-slate-900 bg-white p-7 shadow-[8px_8px_0_#f59e0b]">
          <p className="font-mono text-xs uppercase tracking-[0.22em] text-amber-700">
            Report not assembled
          </p>
          <h3 className="mt-3 text-xl font-semibold">尚未生成预审报告</h3>
          <p className="mt-2 text-sm leading-6 text-muted-foreground">
            完成确定性校验后，可冻结规则、制度绑定与原始证据。生成过程同步且原子，不展示半成品。
          </p>
          {canGenerate ? (
            <Button
              className="mt-5"
              disabled={generate.isPending}
              onClick={() => generate.mutate()}
            >
              {generate.isPending ? (
                <RefreshCw className="animate-spin" aria-hidden="true" />
              ) : (
                <ShieldCheck aria-hidden="true" />
              )}
              生成冻结报告
            </Button>
          ) : (
            <p className="mt-4 text-sm text-muted-foreground">
              当前角色可查看已完成报告，但不能触发生成。
            </p>
          )}
          {generate.isError ? (
            <p role="alert" className="mt-3 text-sm text-destructive">
              {generate.error.message}
            </p>
          ) : null}
        </div>
      </div>
    );
  }
  if (!report.data) return null;
  const summary = report.data.summary;
  const attentionTotal = summary.high_attention_row_count + summary.manual_attention_row_count;
  return (
    <div className="grid gap-0">
      <section className="border-b bg-slate-950 px-6 py-5 text-slate-50">
        <div className="flex items-start justify-between gap-6">
          <div>
            <p className="font-mono text-xs uppercase tracking-[0.25em] text-amber-400">
              Immutable report snapshot
            </p>
            <h3 className="mt-2 text-xl font-semibold">预审证据报告</h3>
            <p className="mt-1 font-mono text-[11px] text-slate-400">
              {summary.report_fingerprint}
            </p>
          </div>
          <div className="flex items-center gap-2">
            <Badge className="border-emerald-700 bg-emerald-950 text-emerald-200">已冻结</Badge>
            {canExport ? (
              <Button
                size="sm"
                variant="secondary"
                disabled={createExport.isPending}
                onClick={() => {
                  setExportError(null);
                  createExport.mutate(undefined, {
                    onSuccess: (artifact) => {
                      void downloadReportExport(artifact.export_id).catch((error: unknown) =>
                        setExportError(error instanceof Error ? error.message : "下载 XLSX 失败"),
                      );
                    },
                    onError: (error) => setExportError(error.message),
                  });
                }}
              >
                {createExport.isPending ? (
                  <RefreshCw className="animate-spin" aria-hidden="true" />
                ) : (
                  <FileOutput aria-hidden="true" />
                )}
                {createExport.isPending ? "生成 artifact" : "导出 XLSX"}
              </Button>
            ) : null}
          </div>
        </div>
        {exportError ? (
          <p role="alert" className="mt-3 text-sm text-red-300">
            {exportError}
          </p>
        ) : null}
        <div className="mt-5 grid grid-cols-6 gap-px overflow-hidden border border-slate-700 bg-slate-700">
          <Metric label="批次总行" value={summary.stored_row_count} />
          <Metric label="需关注行" value={attentionTotal} tone="amber" />
          <Metric label="高关注" value={summary.high_attention_row_count} tone="red" />
          <Metric label="人工确认" value={summary.manual_attention_row_count} />
          <Metric label="判定项" value={summary.report_item_count} />
          <Metric
            label="引用不可用"
            value={summary.unavailable_citation_count}
            {...(summary.unavailable_citation_count > 0 ? { tone: "amber" as const } : {})}
          />
        </div>
      </section>

      <div className="grid grid-cols-[minmax(0,1fr)_250px]">
        <section className="min-w-0 border-r p-5">
          <div className="mb-3 flex items-center justify-between">
            <h4 className="font-semibold">关注项证据链</h4>
            <span className="text-xs text-muted-foreground">
              {items.data?.total ?? 0} 项 · 稳定排序
            </span>
          </div>
          {items.isLoading ? <StatePanel text="读取关注项" loading /> : null}
          {items.isError ? <StatePanel text={items.error.message} error /> : null}
          <div className="grid gap-3">
            {items.data?.items.map((item) => (
              <ReportItemCard key={item.id} item={item} onOpenRow={onOpenRow} />
            ))}
          </div>
          {items.data?.total === 0 ? (
            <StatePanel text="本批次无关注项，所有成功解析行均已通过" />
          ) : null}
          {items.data && items.data.total > items.data.limit ? (
            <div className="mt-4 flex justify-end gap-2">
              <Button
                size="sm"
                variant="outline"
                disabled={offset === 0}
                onClick={() => setOffset((value) => Math.max(0, value - 25))}
              >
                上一页
              </Button>
              <Button
                size="sm"
                variant="outline"
                disabled={offset + items.data.limit >= items.data.total}
                onClick={() => setOffset((value) => value + 25)}
              >
                下一页
              </Button>
            </div>
          ) : null}
        </section>
        <aside className="grid content-start gap-5 bg-slate-50 p-5">
          <div>
            <p className="text-xs font-medium uppercase tracking-widest text-muted-foreground">
              快照指纹
            </p>
            <dl className="mt-3 grid gap-3 text-xs">
              <Fingerprint label="规则集" value={summary.ruleset_fingerprint} />
              <Fingerprint label="制度报告" value={summary.report_fingerprint} />
              <Fingerprint label="映射版本" value={summary.mapping_version_id} />
            </dl>
          </div>
          <div className="border-t pt-4">
            <p className="text-xs font-medium uppercase tracking-widest text-muted-foreground">
              解析错误
            </p>
            <p className="mt-2 text-2xl font-semibold tabular-nums">
              {summary.parse_error_row_count}
            </p>
            {parseErrors.data?.items.map((error) => (
              <button
                type="button"
                onClick={() => onOpenRow(error.row_no)}
                key={error.id}
                className="mt-2 block w-full truncate text-left text-xs text-red-700"
                title={error.message}
              >
                行 {error.row_no} · {error.error_code}
              </button>
            ))}
          </div>
        </aside>
      </div>
    </div>
  );
}

function Metric({ label, value, tone }: { label: string; value: number; tone?: "amber" | "red" }) {
  return (
    <div className="bg-slate-950 px-4 py-3">
      <p className="text-[11px] text-slate-400">{label}</p>
      <p
        className={
          tone === "red"
            ? "mt-1 text-2xl font-semibold text-red-300"
            : tone === "amber"
              ? "mt-1 text-2xl font-semibold text-amber-300"
              : "mt-1 text-2xl font-semibold"
        }
      >
        {value}
      </p>
    </div>
  );
}

function Fingerprint({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <dt className="text-muted-foreground">{label}</dt>
      <dd className="mt-1 break-all font-mono text-[10px]">{value}</dd>
    </div>
  );
}

function ReportItemCard({
  item,
  onOpenRow,
}: {
  item: ReportItem;
  onOpenRow: (rowNo: number) => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const high = item.attention_group === "high_attention";
  return (
    <article
      className={
        high
          ? "overflow-hidden border-l-4 border-l-red-600 bg-white shadow-sm ring-1 ring-slate-200"
          : "overflow-hidden border-l-4 border-l-amber-500 bg-white shadow-sm ring-1 ring-slate-200"
      }
    >
      <button
        type="button"
        onClick={() => setExpanded((value) => !value)}
        className="grid w-full grid-cols-[82px_minmax(0,1fr)_auto] items-center gap-3 p-4 text-left"
      >
        <span className="font-mono text-sm font-semibold">ROW {item.row_no}</span>
        <span className="min-w-0">
          <span className="block truncate text-sm font-medium">
            {item.rule_id} · {item.reason_code}
          </span>
          <span className="mt-1 block truncate text-xs text-muted-foreground">
            {item.reasoning_snapshot ?? "无补充说明"}
          </span>
        </span>
        <span className="flex items-center gap-2">
          <Badge variant={item.citation_status === "verified" ? "secondary" : "outline"}>
            {item.citation_status === "verified"
              ? `${item.citations.length} 条逐字引用`
              : "引用待人工补齐"}
          </Badge>
          <ChevronDown
            className={
              expanded ? "size-4 rotate-180 transition-transform" : "size-4 transition-transform"
            }
          />
        </span>
      </button>
      {expanded ? (
        <div className="grid grid-cols-2 gap-4 border-t bg-slate-50/70 p-4">
          <div>
            <div className="flex items-center justify-between">
              <p className="text-xs font-medium uppercase tracking-wider text-muted-foreground">
                判定证据
              </p>
              <Button size="sm" variant="ghost" onClick={() => onOpenRow(item.row_no)}>
                查看原始行
              </Button>
            </div>
            <pre className="mt-2 max-h-56 overflow-auto whitespace-pre-wrap break-words rounded bg-slate-900 p-3 text-[11px] text-slate-200">
              {JSON.stringify(item.evidence_snapshot, null, 2)}
            </pre>
          </div>
          <div>
            <p className="text-xs font-medium uppercase tracking-wider text-muted-foreground">
              制度逐字引用
            </p>
            {item.citations.length ? (
              item.citations.map((citation) => (
                <blockquote
                  key={citation.id}
                  className="mt-2 border-l-2 border-emerald-600 pl-3 text-sm leading-6"
                >
                  <p className="whitespace-pre-wrap break-words">{citation.quote}</p>
                  <footer className="mt-2 font-mono text-[10px] text-muted-foreground">
                    {citation.document_title} · {citation.clause_no} · [{citation.quote_start},
                    {citation.quote_end})
                  </footer>
                </blockquote>
              ))
            ) : (
              <div className="mt-2 flex gap-2 rounded border border-amber-200 bg-amber-50 p-3 text-sm text-amber-900">
                <AlertTriangle className="mt-0.5 size-4 shrink-0" />
                <span>判定保持不变；制度引用未满足完整逐字校验，需配置员补齐 binding。</span>
              </div>
            )}
          </div>
        </div>
      ) : null}
    </article>
  );
}

function StatePanel({
  text,
  loading = false,
  error = false,
}: {
  text: string;
  loading?: boolean;
  error?: boolean;
}) {
  return (
    <div
      role={error ? "alert" : undefined}
      className={
        error
          ? "flex min-h-48 items-center justify-center p-8 text-sm text-destructive"
          : "flex min-h-48 items-center justify-center p-8 text-sm text-muted-foreground"
      }
    >
      {loading ? <RefreshCw className="mr-2 size-4 animate-spin" /> : null}
      {text}
    </div>
  );
}
