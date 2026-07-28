import { useEffect, useMemo, useState } from "react";
import { AlertTriangle, CheckCircle2, CircleDashed, RefreshCw, Save, Upload } from "lucide-react";

import {
  hasPermission,
  PERMISSIONS,
  type BatchSummary,
  type MappingVersion,
  type UnifiedField,
} from "@/api/client";
import { useCurrentUser } from "@/auth/useAuth";
import {
  useFieldAvailability,
  useParseBatch,
  useParseErrors,
  useSaveSchemaMapping,
  useSchemaMappings,
} from "@/batches/useBatchParsing";
import { BatchValidationView } from "@/batches/BatchValidationView";
import { useBatchDetail, useBatches, useImportBatch } from "@/batches/useBatches";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { cn } from "@/lib/utils";

const UNIFIED_FIELDS = [
  "amount",
  "expense_date",
  "employee",
  "expense_type",
  "invoice_type",
  "invoice_no",
  "merchant",
  "invoice_title",
  "submission_date",
  "location",
  "currency",
  "description",
] as const satisfies readonly UnifiedField[];

const FIELD_LABELS: Record<UnifiedField, string> = {
  amount: "金额",
  expense_date: "费用日期",
  employee: "员工",
  expense_type: "费用类型",
  invoice_type: "票种",
  invoice_no: "发票号",
  merchant: "商户",
  invoice_title: "抬头",
  submission_date: "提交日期",
  location: "地点",
  currency: "币种",
  description: "费用说明",
};

type WorkspaceTab = "raw" | "mapping" | "errors" | "availability" | "validation";
type MappingDraft = Record<string, UnifiedField | "">;

function formatDateTime(value: string): string {
  return new Intl.DateTimeFormat("zh-CN", {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(value));
}

function shortId(value: string): string {
  return value.slice(0, 8);
}

function rawPreview(raw: Record<string, unknown>): string {
  return Object.entries(raw)
    .slice(0, 5)
    .map(([key, value]) => `${key}: ${String(value ?? "")}`)
    .join(" | ");
}

function isUnifiedField(value: string): value is UnifiedField {
  return (UNIFIED_FIELDS as readonly string[]).includes(value);
}

function draftFromVersion(sourceColumns: string[], version: MappingVersion | null): MappingDraft {
  const mapped = new Map(
    (version?.mappings ?? []).map((entry) => [entry.source_column, entry.target_field]),
  );
  return Object.fromEntries(sourceColumns.map((column) => [column, mapped.get(column) ?? ""]));
}

function sameMapping(draft: MappingDraft, version: MappingVersion | null): boolean {
  if (!version) return Object.values(draft).every((target) => target === "");
  const entries = Object.entries(draft).filter((entry) => entry[1] !== "");
  if (entries.length !== version.mappings.length) return false;
  return entries.every(([sourceColumn, targetField]) =>
    version.mappings.some(
      (entry) => entry.source_column === sourceColumn && entry.target_field === targetField,
    ),
  );
}

function statusTone(status: "available" | "inferred" | "missing"): string {
  if (status === "available") return "border-emerald-200 bg-emerald-50 text-emerald-800";
  if (status === "inferred") return "border-amber-200 bg-amber-50 text-amber-800";
  return "border-slate-200 bg-slate-50 text-slate-600";
}

function statusLabel(status: "available" | "inferred" | "missing"): string {
  return { available: "可用", inferred: "推断", missing: "缺失" }[status];
}

export function BatchesPage() {
  const { data: user } = useCurrentUser();
  const canImport = user ? hasPermission(user, PERMISSIONS.batchImport) : false;
  const batches = useBatches();
  const importBatch = useImportBatch();
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);

  const selectedBatch = useMemo(
    () => batches.data?.find((batch) => batch.file_version_id === selectedId) ?? null,
    [batches.data, selectedId],
  );

  useEffect(() => {
    if (selectedId === null && batches.data?.[0]) {
      setSelectedId(batches.data[0].file_version_id);
    }
  }, [batches.data, selectedId]);

  return (
    <div className="grid gap-4">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">批次工作台</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            从原始证据到结构化结果，一处完成映射、解析与质量核验
          </p>
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
                importBatch.mutate(input.files[0], {
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

      <div className="grid grid-cols-[minmax(300px,0.72fr)_minmax(0,1.8fr)] gap-4">
        <Card className="self-start">
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
                  onClick={() => setSelectedId(batch.file_version_id)}
                  className={cn(
                    "grid gap-1 rounded-lg border p-3 text-left text-sm transition-colors hover:bg-muted",
                    selectedId === batch.file_version_id
                      ? "border-foreground bg-muted shadow-sm"
                      : "border-border",
                  )}
                >
                  <span className="font-medium">{batch.filename}</span>
                  <span className="text-muted-foreground">
                    {batch.row_count} 行 · {formatDateTime(batch.uploaded_at)}
                  </span>
                  <span className="font-mono text-xs text-muted-foreground">
                    {batch.content_hash.slice(0, 12)}
                  </span>
                </button>
              ))}
              {!batches.isLoading && (batches.data ?? []).length === 0 ? (
                <p className="text-sm text-muted-foreground">暂无批次</p>
              ) : null}
            </div>
          </CardContent>
        </Card>

        {selectedBatch && user ? (
          <BatchWorkspace key={selectedBatch.file_version_id} batch={selectedBatch} user={user} />
        ) : (
          <Card>
            <CardContent className="flex min-h-72 items-center justify-center">
              <div className="text-center text-muted-foreground">
                <CircleDashed className="mx-auto mb-3 size-8" aria-hidden="true" />
                <p className="text-sm">选择左侧批次进入解析工作流</p>
              </div>
            </CardContent>
          </Card>
        )}
      </div>
    </div>
  );
}

function BatchWorkspace({
  batch,
  user,
}: {
  batch: BatchSummary;
  user: NonNullable<ReturnType<typeof useCurrentUser>["data"]>;
}) {
  const canReadMapping = hasPermission(user, PERMISSIONS.configRead);
  const canWriteMapping = hasPermission(user, PERMISSIONS.configWrite);
  const canParse = hasPermission(user, PERMISSIONS.batchImport);
  const [activeTab, setActiveTab] = useState<WorkspaceTab>("raw");
  const [errorOffset, setErrorOffset] = useState(0);
  const [selectedMappingId, setSelectedMappingId] = useState<string | null>(null);
  const [draft, setDraft] = useState<MappingDraft>({});
  const [actionMessage, setActionMessage] = useState<string | null>(null);
  const detail = useBatchDetail(batch.file_version_id);
  const mappings = useSchemaMappings(batch.file_version_id, canReadMapping);
  const parseErrors = useParseErrors(batch.file_version_id, errorOffset);
  const availability = useFieldAvailability(batch.file_version_id);
  const saveMapping = useSaveSchemaMapping(batch.file_version_id);
  const parseBatch = useParseBatch(batch.file_version_id);

  const selectedVersion = useMemo(
    () => mappings.data?.versions.find((version) => version.id === selectedMappingId) ?? null,
    [mappings.data, selectedMappingId],
  );
  const currentVersion = mappings.data?.versions.find((version) => version.is_current_for_batch);
  const parsed = parseErrors.data !== null && parseErrors.data !== undefined;
  const errorCount = parseErrors.data?.total ?? 0;
  const successCount = parsed ? batch.row_count - errorCount : null;
  const availabilityCounts = { available: 0, inferred: 0, missing: 0 };
  for (const item of availability.data?.items ?? []) availabilityCounts[item.status] += 1;
  const draftDirty = !sameMapping(draft, selectedVersion);

  useEffect(() => {
    if (!mappings.data) return;
    const candidate =
      mappings.data.versions.find((version) => version.is_current_for_batch) ??
      mappings.data.versions[0] ??
      null;
    setSelectedMappingId((current) =>
      current && mappings.data.versions.some((version) => version.id === current)
        ? current
        : (candidate?.id ?? null),
    );
    if (!candidate) setDraft(draftFromVersion(mappings.data.source_columns, null));
  }, [mappings.data]);

  useEffect(() => {
    if (!mappings.data || !selectedVersion) return;
    setDraft(draftFromVersion(mappings.data.source_columns, selectedVersion));
  }, [mappings.data, selectedVersion]);

  function saveDraft(): void {
    if (!mappings.data) return;
    const targets = Object.values(draft).filter((value): value is UnifiedField => value !== "");
    if (!targets.includes("amount") || !targets.includes("expense_date")) {
      setActionMessage("金额和费用日期必须映射后才能保存");
      return;
    }
    if (new Set(targets).size !== targets.length) {
      setActionMessage("同一统一字段不能由多个源列直接映射");
      return;
    }
    saveMapping.mutate(
      {
        file_version_id: batch.file_version_id,
        mappings: Object.entries(draft)
          .filter((entry): entry is [string, UnifiedField] => entry[1] !== "")
          .map(([sourceColumn, targetField]) => ({
            source_column: sourceColumn,
            target_field: targetField,
          })),
        availability_thresholds: selectedVersion?.availability_thresholds ?? {
          available_min_non_null_rate: "0.8000",
          inferred_min_success_rate: "0.8000",
        },
        currency_aliases: selectedVersion?.currency_aliases ?? {},
        inference_rules: (selectedVersion?.inference_rules ?? []).map((rule) => ({ ...rule })),
      },
      {
        onSuccess: (result) => {
          setActionMessage(
            result.reused_existing
              ? `已复用映射 v${result.version}`
              : `已保存映射 v${result.version}`,
          );
          setSelectedMappingId(result.id);
        },
        onError: (error) => setActionMessage(error.message),
      },
    );
  }

  function runParse(): void {
    if (!selectedMappingId) {
      setActionMessage("请先选择一个已保存的映射版本");
      return;
    }
    if (draftDirty) {
      setActionMessage("字段映射有未保存改动，请先保存新版本");
      return;
    }
    parseBatch.mutate(selectedMappingId, {
      onSuccess: (result) => {
        setActionMessage(
          `${result.reused_existing ? "已复用解析结果" : "解析完成"}：成功 ${result.success_count} 行，失败 ${result.error_count} 行`,
        );
      },
      onError: (error) => setActionMessage(error.message),
    });
  }

  const tabs: { id: WorkspaceTab; label: string; count: number | undefined }[] = [
    { id: "raw", label: "原始数据", count: batch.row_count },
    { id: "mapping", label: "字段映射", count: mappings.data?.versions.length },
    { id: "errors", label: "错误清单", count: parseErrors.data?.total },
    { id: "availability", label: "字段可用性", count: availability.data?.items.length },
    { id: "validation", label: "确定性校验", count: undefined },
  ];

  return (
    <Card className="min-w-0 overflow-hidden">
      <CardHeader className="border-b bg-muted/30">
        <div className="flex items-start justify-between gap-4">
          <div>
            <CardTitle>{batch.filename}</CardTitle>
            <p className="mt-1 font-mono text-xs text-muted-foreground">
              {shortId(batch.file_version_id)} · {batch.row_count} 行
            </p>
          </div>
          <div className="flex items-center gap-2">
            {currentVersion ? (
              <Badge variant="secondary">映射 v{currentVersion.version}</Badge>
            ) : null}
            <Badge variant={parsed && errorCount > 0 ? "outline" : "secondary"}>
              {parsed ? (errorCount > 0 ? "解析有错误" : "解析完成") : "尚未解析"}
            </Badge>
          </div>
        </div>

        <div className="mt-4 grid grid-cols-4 gap-2" aria-label="解析摘要">
          <Metric label="总行数" value={batch.row_count} />
          <Metric label="解析成功" value={successCount ?? "—"} tone="success" />
          <Metric
            label="解析失败"
            value={parsed ? errorCount : "—"}
            tone={errorCount > 0 ? "danger" : undefined}
          />
          <Metric
            label="字段状态"
            value={
              availability.data ? `${availabilityCounts.available}/${UNIFIED_FIELDS.length}` : "—"
            }
            detail={
              availability.data
                ? `推断 ${availabilityCounts.inferred} · 缺失 ${availabilityCounts.missing}`
                : undefined
            }
          />
        </div>
      </CardHeader>

      <div className="flex items-center justify-between border-b px-6">
        <div role="tablist" aria-label="批次视图" className="flex gap-5">
          {tabs.map((tab) => (
            <button
              key={tab.id}
              type="button"
              role="tab"
              aria-selected={activeTab === tab.id}
              onClick={() => setActiveTab(tab.id)}
              className={cn(
                "border-b-2 py-3 text-sm font-medium transition-colors",
                activeTab === tab.id
                  ? "border-foreground text-foreground"
                  : "border-transparent text-muted-foreground hover:text-foreground",
              )}
            >
              {tab.label}
              {tab.count !== undefined ? <span className="ml-1.5 text-xs">{tab.count}</span> : null}
            </button>
          ))}
        </div>
        {canParse ? (
          <Button
            size="sm"
            onClick={runParse}
            disabled={parseBatch.isPending || !selectedMappingId}
          >
            {parseBatch.isPending ? (
              <RefreshCw className="animate-spin" aria-hidden="true" />
            ) : (
              <CheckCircle2 aria-hidden="true" />
            )}
            触发解析
          </Button>
        ) : null}
      </div>

      {actionMessage ? (
        <div role="status" className="border-b bg-slate-50 px-6 py-2 text-sm text-slate-700">
          {actionMessage}
        </div>
      ) : null}

      <CardContent className="min-h-96 p-0">
        {activeTab === "raw" ? <RawRowsView detail={detail} parsed={parsed} /> : null}
        {activeTab === "mapping" ? (
          <MappingView
            canRead={canReadMapping}
            canWrite={canWriteMapping}
            mappings={mappings}
            selectedMappingId={selectedMappingId}
            selectedVersion={selectedVersion}
            draft={draft}
            setDraft={setDraft}
            setSelectedMappingId={setSelectedMappingId}
            onSave={saveDraft}
            isSaving={saveMapping.isPending}
            draftDirty={draftDirty}
          />
        ) : null}
        {activeTab === "errors" ? (
          <ErrorsView query={parseErrors} offset={errorOffset} setOffset={setErrorOffset} />
        ) : null}
        {activeTab === "availability" ? <AvailabilityView query={availability} /> : null}
        {activeTab === "validation" ? (
          <BatchValidationView fileVersionId={batch.file_version_id} user={user} />
        ) : null}
      </CardContent>
    </Card>
  );
}

function Metric({
  label,
  value,
  detail,
  tone,
}: {
  label: string;
  value: string | number;
  detail?: string | undefined;
  tone?: "success" | "danger" | undefined;
}) {
  return (
    <div className="rounded-md border bg-background px-3 py-2">
      <p className="text-xs text-muted-foreground">{label}</p>
      <p
        className={cn(
          "mt-1 text-lg font-semibold tabular-nums",
          tone === "success" && "text-emerald-700",
          tone === "danger" && "text-red-700",
        )}
      >
        {value}
      </p>
      {detail ? <p className="mt-0.5 text-[11px] text-muted-foreground">{detail}</p> : null}
    </div>
  );
}

function RawRowsView({
  detail,
  parsed,
}: {
  detail: ReturnType<typeof useBatchDetail>;
  parsed: boolean;
}) {
  if (detail.isLoading) return <ViewMessage>加载原始数据中</ViewMessage>;
  if (detail.isError) return <ViewMessage error>{detail.error.message}</ViewMessage>;
  if (!detail.data) return null;
  return (
    <div className="overflow-x-auto px-6 py-4">
      <div className="grid min-w-[680px] grid-cols-[70px_1fr_110px] border-b pb-2 text-xs font-medium text-muted-foreground">
        <span>行号</span>
        <span>原始值证据</span>
        <span>解析状态</span>
      </div>
      {detail.data.rows.slice(0, 30).map((row) => (
        <div
          key={row.row_no}
          className="grid min-w-[680px] grid-cols-[70px_1fr_110px] items-start gap-2 border-b py-2.5 text-sm"
        >
          <span className="font-mono">{row.row_no}</span>
          <span className="truncate pr-4 text-muted-foreground" title={rawPreview(row.raw_json)}>
            {rawPreview(row.raw_json)}
          </span>
          <span className={row.parse_error ? "text-red-700" : "text-muted-foreground"}>
            {row.parse_error ? "失败" : parsed ? "成功" : "未解析"}
          </span>
        </div>
      ))}
      {detail.data.rows.length > 30 ? (
        <p className="pt-3 text-xs text-muted-foreground">
          当前预览前 30 行，原始行总数 {detail.data.row_count}。
        </p>
      ) : null}
    </div>
  );
}

function MappingView({
  canRead,
  canWrite,
  mappings,
  selectedMappingId,
  selectedVersion,
  draft,
  setDraft,
  setSelectedMappingId,
  onSave,
  isSaving,
  draftDirty,
}: {
  canRead: boolean;
  canWrite: boolean;
  mappings: ReturnType<typeof useSchemaMappings>;
  selectedMappingId: string | null;
  selectedVersion: MappingVersion | null;
  draft: MappingDraft;
  setDraft: React.Dispatch<React.SetStateAction<MappingDraft>>;
  setSelectedMappingId: (id: string) => void;
  onSave: () => void;
  isSaving: boolean;
  draftDirty: boolean;
}) {
  if (!canRead) return <ViewMessage>当前角色仅可查看解析结果，不能读取字段映射配置。</ViewMessage>;
  if (mappings.isLoading) return <ViewMessage>匹配同表头映射中</ViewMessage>;
  if (mappings.isError) return <ViewMessage error>{mappings.error.message}</ViewMessage>;
  if (!mappings.data) return null;
  return (
    <div className="grid gap-4 p-6">
      <div className="flex items-end justify-between gap-4 rounded-lg border bg-slate-50 p-4">
        <label className="grid min-w-64 gap-1.5 text-sm font-medium">
          已保存版本
          <select
            className="h-9 rounded-md border bg-background px-3 text-sm"
            value={selectedMappingId ?? ""}
            onChange={(event) => setSelectedMappingId(event.target.value)}
            disabled={mappings.data.versions.length === 0}
          >
            {mappings.data.versions.length === 0 ? <option value="">尚无匹配版本</option> : null}
            {mappings.data.versions.map((version) => (
              <option key={version.id} value={version.id}>
                v{version.version}
                {version.is_current_for_batch ? " · 当前解析版本" : ""}
              </option>
            ))}
          </select>
        </label>
        <div className="text-right text-xs text-muted-foreground">
          <Badge variant="outline">表头精确匹配</Badge>
          <p className="mt-2 font-mono">{shortId(mappings.data.header_signature)}</p>
        </div>
      </div>

      <div className="overflow-hidden rounded-lg border">
        <div className="grid grid-cols-[1fr_1fr] bg-muted/50 px-4 py-2 text-xs font-medium text-muted-foreground">
          <span>Excel 源列</span>
          <span>统一字段</span>
        </div>
        {mappings.data.source_columns.map((sourceColumn) => (
          <div
            key={sourceColumn}
            className="grid grid-cols-[1fr_1fr] items-center border-t px-4 py-2.5 text-sm"
          >
            <span className="font-medium">{sourceColumn}</span>
            {canWrite ? (
              <select
                aria-label={`${sourceColumn} 映射字段`}
                className="h-9 rounded-md border bg-background px-3 text-sm"
                value={draft[sourceColumn] ?? ""}
                onChange={(event) => {
                  const value = event.target.value;
                  if (value === "" || isUnifiedField(value))
                    setDraft((current) => ({ ...current, [sourceColumn]: value }));
                }}
              >
                <option value="">不映射</option>
                {UNIFIED_FIELDS.map((field) => (
                  <option key={field} value={field}>
                    {FIELD_LABELS[field]} · {field}
                  </option>
                ))}
              </select>
            ) : (
              <span>
                {draft[sourceColumn]
                  ? `${FIELD_LABELS[draft[sourceColumn] as UnifiedField]} · ${draft[sourceColumn]}`
                  : "—"}
              </span>
            )}
          </div>
        ))}
      </div>

      <div className="flex items-center justify-between">
        <p className="text-xs text-muted-foreground">
          金额与费用日期为必填映射。保存会创建不可变的新版本。
        </p>
        {canWrite ? (
          <Button onClick={onSave} disabled={isSaving || !draftDirty}>
            <Save aria-hidden="true" />
            保存新版本
          </Button>
        ) : (
          <p className="text-sm text-muted-foreground">当前角色可复用版本，但不能修改</p>
        )}
      </div>
      {selectedVersion?.inference_rules.length ? (
        <p className="text-xs text-muted-foreground">
          该版本包含 {selectedVersion.inference_rules.length}{" "}
          条确定性推断配置；本视图保留并随新版本提交。
        </p>
      ) : null}
    </div>
  );
}

function ErrorsView({
  query,
  offset,
  setOffset,
}: {
  query: ReturnType<typeof useParseErrors>;
  offset: number;
  setOffset: React.Dispatch<React.SetStateAction<number>>;
}) {
  if (query.isLoading) return <ViewMessage>加载错误清单中</ViewMessage>;
  if (query.isError) return <ViewMessage error>{query.error.message}</ViewMessage>;
  if (query.data === null)
    return <ViewMessage>批次尚未解析，触发解析后会在这里逐行展示失败原因。</ViewMessage>;
  if (!query.data) return null;
  return (
    <div className="p-6">
      {query.data.items.length === 0 ? (
        <div className="flex items-center gap-2 rounded-lg border border-emerald-200 bg-emerald-50 p-4 text-sm text-emerald-800">
          <CheckCircle2 className="size-4" aria-hidden="true" />
          本批次没有解析失败行
        </div>
      ) : (
        <div className="grid gap-3">
          {query.data.items.map((item) => (
            <div key={item.row_no} className="rounded-lg border border-red-100 bg-red-50/40 p-4">
              <div className="flex items-center justify-between">
                <span className="font-medium">第 {item.row_no} 行</span>
                <Badge variant="outline">{item.parse_error_code}</Badge>
              </div>
              <p className="mt-2 text-sm text-red-800">{item.parse_error}</p>
              <p
                className="mt-2 truncate text-xs text-muted-foreground"
                title={rawPreview(item.raw_json)}
              >
                {rawPreview(item.raw_json)}
              </p>
            </div>
          ))}
        </div>
      )}
      <div className="mt-4 flex items-center justify-between text-sm text-muted-foreground">
        <span>共 {query.data.total} 条错误</span>
        <div className="flex gap-2">
          <Button
            size="sm"
            variant="outline"
            disabled={offset === 0}
            onClick={() => setOffset((value) => Math.max(0, value - 50))}
          >
            上一页
          </Button>
          <Button
            size="sm"
            variant="outline"
            disabled={offset + query.data.limit >= query.data.total}
            onClick={() => setOffset((value) => value + 50)}
          >
            下一页
          </Button>
        </div>
      </div>
    </div>
  );
}

function AvailabilityView({ query }: { query: ReturnType<typeof useFieldAvailability> }) {
  if (query.isLoading) return <ViewMessage>加载字段可用性中</ViewMessage>;
  if (query.isError) return <ViewMessage error>{query.error.message}</ViewMessage>;
  if (query.data === null) return <ViewMessage>批次尚未解析，暂无字段可用性证据。</ViewMessage>;
  if (!query.data) return null;
  return (
    <div className="grid grid-cols-2 gap-3 p-6">
      {query.data.items.map((item) => {
        const evidence = item.evidence;
        const rate =
          evidence.selected_basis === "direct"
            ? evidence.direct.non_null_rate
            : evidence.selected_basis === "inference"
              ? evidence.inference.success_rate
              : "0.0000";
        return (
          <div
            key={item.field_name}
            className={cn("rounded-lg border p-4", statusTone(item.status))}
          >
            <div className="flex items-center justify-between">
              <div>
                <p className="font-medium">
                  {isUnifiedField(item.field_name)
                    ? FIELD_LABELS[item.field_name]
                    : item.field_name}
                </p>
                <p className="font-mono text-xs opacity-70">{item.field_name}</p>
              </div>
              <Badge variant="outline">{statusLabel(item.status)}</Badge>
            </div>
            <div className="mt-3 flex items-center justify-between text-xs">
              <span>
                {evidence.selected_basis === "direct"
                  ? "直接映射非空率"
                  : evidence.selected_basis === "inference"
                    ? "确定性推断成功率"
                    : "无可用证据"}
              </span>
              <span className="font-mono">{rate}</span>
            </div>
          </div>
        );
      })}
    </div>
  );
}

function ViewMessage({ children, error = false }: { children: React.ReactNode; error?: boolean }) {
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
