import { useMemo, useState } from "react";
import { RefreshCw, Upload } from "lucide-react";

import { hasPermission, PERMISSIONS, type BatchSummary } from "@/api/client";
import { useCurrentUser } from "@/auth/useAuth";
import { useBatchDetail, useBatches, useImportBatch } from "@/batches/useBatches";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { cn } from "@/lib/utils";

function formatDateTime(value: string): string {
  return new Intl.DateTimeFormat("zh-CN", {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(value));
}

function shortHash(value: string): string {
  return value.slice(0, 12);
}

function rawPreview(raw: Record<string, unknown>): string {
  return Object.entries(raw)
    .slice(0, 4)
    .map(([key, value]) => `${key}: ${String(value ?? "")}`)
    .join(" | ");
}

export function BatchesPage() {
  const { data: user } = useCurrentUser();
  const canImport = user ? hasPermission(user, PERMISSIONS.batchImport) : false;
  const batches = useBatches();
  const importBatch = useImportBatch();
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const detail = useBatchDetail(selectedId);

  const selectedBatch = useMemo(
    () => batches.data?.find((batch) => batch.file_version_id === selectedId) ?? null,
    [batches.data, selectedId],
  );

  function selectBatch(batch: BatchSummary): void {
    setSelectedId(batch.file_version_id);
  }

  return (
    <div className="grid gap-4">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold">批次</h1>
          <p className="mt-1 text-sm text-muted-foreground">Excel 文件版本与原始行证据链</p>
        </div>
        <Button
          variant="outline"
          onClick={() => void batches.refetch()}
          disabled={batches.isFetching}
        >
          <RefreshCw aria-hidden="true" />
          刷新
        </Button>
      </div>

      {canImport ? (
        <Card>
          <CardHeader>
            <CardTitle>导入新批次</CardTitle>
          </CardHeader>
          <CardContent>
            <form
              className="flex items-center gap-3"
              onSubmit={(event) => {
                event.preventDefault();
                const form = event.currentTarget;
                const input = form.elements.namedItem("file");
                if (!(input instanceof HTMLInputElement) || !input.files?.[0]) {
                  setMessage("请选择 .xlsx 文件");
                  return;
                }
                const file = input.files[0];
                importBatch.mutate(file, {
                  onSuccess: (result) => {
                    setMessage(result.reused_existing ? "已复用既有批次" : "导入完成");
                    setSelectedId(result.file_version_id);
                    form.reset();
                  },
                  onError: (error) => setMessage(error.message),
                });
              }}
            >
              <Input
                name="file"
                type="file"
                accept=".xlsx"
                aria-label="Excel 文件"
                className="max-w-md"
              />
              <Button type="submit" disabled={importBatch.isPending}>
                <Upload aria-hidden="true" />
                导入
              </Button>
              {message ? (
                <span role="status" className="text-sm text-muted-foreground">
                  {message}
                </span>
              ) : null}
            </form>
          </CardContent>
        </Card>
      ) : null}

      <div className="grid grid-cols-[minmax(380px,0.9fr)_minmax(0,1.1fr)] gap-4">
        <Card>
          <CardHeader>
            <CardTitle>历史批次</CardTitle>
          </CardHeader>
          <CardContent>
            {batches.isLoading ? <p className="text-sm text-muted-foreground">加载中</p> : null}
            {batches.isError ? (
              <p role="alert" className="text-sm text-destructive">
                {batches.error.message}
              </p>
            ) : null}
            <div className="grid gap-2">
              {(batches.data ?? []).map((batch) => (
                <button
                  key={batch.file_version_id}
                  type="button"
                  onClick={() => selectBatch(batch)}
                  className={cn(
                    "grid gap-1 rounded-lg border p-3 text-left text-sm transition-colors hover:bg-muted",
                    selectedId === batch.file_version_id
                      ? "border-foreground bg-muted"
                      : "border-border",
                  )}
                >
                  <span className="font-medium">{batch.filename}</span>
                  <span className="text-muted-foreground">
                    {batch.row_count} 行 · {formatDateTime(batch.uploaded_at)}
                  </span>
                  <span className="font-mono text-xs text-muted-foreground">
                    {shortHash(batch.content_hash)}
                  </span>
                </button>
              ))}
              {!batches.isLoading && (batches.data ?? []).length === 0 ? (
                <p className="text-sm text-muted-foreground">暂无批次</p>
              ) : null}
            </div>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <div className="flex items-start justify-between gap-3">
              <CardTitle>{selectedBatch?.filename ?? "批次详情"}</CardTitle>
              {selectedBatch ? (
                <Badge variant="secondary">{selectedBatch.row_count} 行</Badge>
              ) : null}
            </div>
          </CardHeader>
          <CardContent>
            {selectedId === null ? (
              <p className="text-sm text-muted-foreground">选择左侧批次查看原始行</p>
            ) : null}
            {detail.isLoading ? <p className="text-sm text-muted-foreground">加载中</p> : null}
            {detail.isError ? (
              <p role="alert" className="text-sm text-destructive">
                {detail.error.message}
              </p>
            ) : null}
            {detail.data ? (
              <div className="grid gap-2">
                <div className="grid grid-cols-[90px_1fr_120px] border-b pb-2 text-xs font-medium text-muted-foreground">
                  <span>行号</span>
                  <span>原始值</span>
                  <span>解析状态</span>
                </div>
                {detail.data.rows.slice(0, 30).map((row) => (
                  <div
                    key={row.row_no}
                    className="grid grid-cols-[90px_1fr_120px] items-start gap-2 border-b py-2 text-sm"
                  >
                    <span className="font-mono">{row.row_no}</span>
                    <span className="truncate text-muted-foreground">
                      {rawPreview(row.raw_json)}
                    </span>
                    <span>{row.parse_error ? "失败" : "未解析"}</span>
                  </div>
                ))}
              </div>
            ) : null}
          </CardContent>
        </Card>
      </div>
    </div>
  );
}
