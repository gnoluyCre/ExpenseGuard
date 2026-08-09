import { useEffect, useMemo, useState } from "react";
import { Fingerprint, History, RefreshCw, Save, ShieldCheck } from "lucide-react";

import { hasPermission, PERMISSIONS, type DetectionProfileDefinition } from "@/api/client";
import { useCurrentUser } from "@/auth/useAuth";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { detectionProfileSchema } from "@/detection/configSchemas";
import { useCreateDetectionConfig, useDetectionConfigs } from "@/detection/useDetection";
import { cn } from "@/lib/utils";

const DETECTOR_META = {
  split_invoice: {
    label: "拆单候选",
    version: "split-window-v1",
    dependencies: "amount · expense_date · employee · merchant · currency*",
  },
  sequential_invoice: {
    label: "发票连号",
    version: "invoice-sequence-v1",
    dependencies: "invoice_no · configured partition fields",
  },
  frequency_anomaly: {
    label: "频次离群",
    version: "frequency-mad-v1",
    dependencies: "employee · expense_date",
  },
  spatiotemporal_tier0: {
    label: "时空冲突 Tier 0",
    version: "spatiotemporal-pair-v1",
    dependencies: "employee · expense_date · location",
  },
} as const;

const DEFAULT_PROFILE: DetectionProfileDefinition = {
  schema_version: 1,
  algorithm_bundle_version: "correlation-v1",
  detectors: [
    {
      type: "split_invoice",
      enabled: true,
      min_eligible_rows: 2,
      min_eligible_rate_bps: 8000,
      approval_thresholds: { CNY: "1000" },
      currency_mode: "field",
      fixed_currency: null,
      aggregate_operator: "gte",
      individual_floor_bps: 8000,
      date_window_days: 3,
      min_rows: 2,
      merchant_aliases: {},
    },
    {
      type: "sequential_invoice",
      enabled: true,
      min_eligible_rows: 2,
      min_eligible_rate_bps: 8000,
      min_sequence_length: 3,
      numeric_suffix_min_digits: 3,
      numeric_suffix_max_digits: 12,
      partition_fields: ["employee", "merchant"],
    },
    {
      type: "frequency_anomaly",
      enabled: true,
      min_eligible_rows: 3,
      min_eligible_rate_bps: 8000,
      period: "calendar_month",
      min_population: 5,
      absolute_min_count: 5,
      mad_multiplier: "3",
      mad_floor: "1",
    },
    {
      type: "spatiotemporal_tier0",
      enabled: false,
      min_eligible_rows: 2,
      min_eligible_rate_bps: 8000,
      location_aliases: {},
      incompatible_zone_pairs: [],
      min_zone_mapping_rate_bps: 8000,
    },
  ],
};

function formatDateTime(value: string): string {
  return new Intl.DateTimeFormat("zh-CN", { dateStyle: "medium", timeStyle: "short" }).format(
    new Date(value),
  );
}

function pretty(value: unknown): string {
  return JSON.stringify(value, null, 2);
}

export function DetectionConfigPage() {
  const { data: user } = useCurrentUser();
  const [historyOffset, setHistoryOffset] = useState(0);
  const configs = useDetectionConfigs(50, historyOffset);
  const createConfig = useCreateDetectionConfig();
  const canWrite = user ? hasPermission(user, PERMISSIONS.configWrite) : false;
  const [draftText, setDraftText] = useState(pretty(DEFAULT_PROFILE));
  const [reason, setReason] = useState("");
  const [confirmed, setConfirmed] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const current = configs.data?.current;
  const currentDefinition = current?.definition;

  useEffect(() => {
    if (currentDefinition) setDraftText(pretty(currentDefinition));
  }, [currentDefinition]);

  const validation = useMemo(() => {
    try {
      return detectionProfileSchema.safeParse(JSON.parse(draftText) as unknown);
    } catch {
      return { success: false as const, error: null };
    }
  }, [draftText]);

  function save(): void {
    setMessage(null);
    if (!validation.success) {
      setMessage(validation.error?.issues[0]?.message ?? "配置 JSON 不是有效对象");
      return;
    }
    if (!confirmed) {
      setMessage("请先确认新配置不会修改历史运行快照");
      return;
    }
    createConfig.mutate(
      {
        expected_current_version: configs.data?.current?.version ?? 0,
        definition: validation.data,
        change_reason: reason.trim(),
      },
      {
        onSuccess: (result) => {
          setMessage(
            result.reused_existing
              ? `已复用配置 v${result.version}`
              : `已创建配置 v${result.version}`,
          );
          setConfirmed(false);
          setReason("");
        },
        onError: (error) => setMessage(error.message),
      },
    );
  }

  return (
    <div className="grid gap-4">
      <header className="flex items-end justify-between gap-6 border-b border-border/70 pb-4">
        <div>
          <div className="mb-2 flex items-center gap-2 text-[11px] font-semibold tracking-[0.22em] text-muted-foreground">
            <ShieldCheck className="size-4 text-primary" aria-hidden="true" />
            CORRELATION CONTROL PLANE
          </div>
          <h1 className="text-2xl font-semibold tracking-tight">关联检测配置</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            四类统计 detector 共享一个不可变 profile；精确匹配值不做模糊推断。
          </p>
        </div>
        <Button
          variant="outline"
          onClick={() => void configs.refetch()}
          disabled={configs.isFetching}
        >
          <RefreshCw aria-hidden="true" />
          刷新
        </Button>
      </header>

      {configs.isError ? (
        <p
          role="alert"
          className="rounded-lg border border-destructive/30 bg-destructive/5 p-3 text-sm text-destructive"
        >
          {configs.error.message}
        </p>
      ) : null}
      {configs.isLoading ? (
        <p className="text-sm text-muted-foreground">正在读取关联检测配置…</p>
      ) : null}

      <Card className="overflow-hidden">
        <CardContent className="grid grid-cols-[0.55fr_1.6fr_0.8fr_0.8fr] gap-4 bg-slate-950 p-5 text-slate-50">
          <Metric label="CURRENT" value={current ? `v${current.version}` : "未配置"} />
          <Metric label="PROFILE FINGERPRINT" value={current?.config_fingerprint ?? "—"} mono />
          <Metric label="CREATED BY" value={current?.created_by ?? "—"} mono />
          <Metric label="CREATED AT" value={current ? formatDateTime(current.created_at) : "—"} />
        </CardContent>
      </Card>

      <div className="grid grid-cols-4 gap-3">
        {(current?.definition.detectors ?? DEFAULT_PROFILE.detectors).map((detector) => (
          <Card key={detector.type} className="min-w-0 border-t-4 border-t-slate-800">
            <CardHeader className="gap-1">
              <div className="flex items-center justify-between gap-2">
                <CardTitle className="text-base">{DETECTOR_META[detector.type].label}</CardTitle>
                <Badge variant={detector.enabled ? "default" : "outline"}>
                  {detector.enabled ? "启用" : "停用"}
                </Badge>
              </div>
              <code className="text-[10px] text-muted-foreground">
                {DETECTOR_META[detector.type].version}
              </code>
            </CardHeader>
            <CardContent className="grid gap-3 text-xs">
              <p className="min-h-10 text-muted-foreground">
                依赖：{DETECTOR_META[detector.type].dependencies}
              </p>
              <div className="grid grid-cols-2 gap-2 border-t pt-3">
                <Metric label="MIN ROWS" value={String(detector.min_eligible_rows)} />
                <Metric
                  label="MIN RATE"
                  value={`${(detector.min_eligible_rate_bps / 100).toFixed(2)}%`}
                />
              </div>
              <ExactTables detector={detector} />
            </CardContent>
          </Card>
        ))}
      </div>

      <div className="grid grid-cols-[minmax(0,1.45fr)_minmax(320px,0.65fr)] gap-4">
        <Card>
          <CardHeader className="border-b">
            <CardTitle>新 profile 草案</CardTitle>
            <p className="text-sm text-muted-foreground">
              完整 profile 经 Zod 和后端 Pydantic 双重校验；未知字段与缺失 detector 将被拒绝。
            </p>
          </CardHeader>
          <CardContent className="grid gap-4">
            {canWrite ? (
              <>
                <textarea
                  aria-label="关联检测配置 JSON"
                  spellCheck={false}
                  value={draftText}
                  onChange={(event) => setDraftText(event.target.value)}
                  className="min-h-[390px] w-full resize-y rounded-lg border bg-slate-950 p-4 font-mono text-xs leading-5 text-slate-100 outline-none focus-visible:ring-2 focus-visible:ring-ring"
                />
                <div className="flex items-center justify-between rounded-lg border p-3 text-sm">
                  <span>Canonical 校验</span>
                  <Badge variant={validation.success ? "default" : "destructive"}>
                    {validation.success ? "结构有效" : "结构无效"}
                  </Badge>
                </div>
                <label className="grid gap-1.5 text-sm font-medium">
                  变更原因
                  <Input
                    aria-label="变更原因"
                    value={reason}
                    maxLength={500}
                    onChange={(event) => setReason(event.target.value)}
                  />
                </label>
                <label className="flex items-start gap-2 rounded-lg border border-amber-200 bg-amber-50 p-3 text-sm text-amber-950">
                  <input
                    aria-label="确认历史不可变"
                    type="checkbox"
                    checked={confirmed}
                    onChange={(event) => setConfirmed(event.target.checked)}
                    className="mt-0.5 size-4 accent-amber-800"
                  />
                  <span>
                    确认 expected current version 为 v{current?.version ?? 0}
                    ，保存只追加新版本，不修改任何历史 run。
                  </span>
                </label>
                <div className="flex items-center gap-3">
                  <Button
                    onClick={save}
                    disabled={createConfig.isPending || reason.trim().length === 0}
                  >
                    <Save aria-hidden="true" />
                    追加不可变版本
                  </Button>
                  {message ? (
                    <span
                      role={createConfig.isError ? "alert" : "status"}
                      className={cn(
                        "text-sm",
                        createConfig.isError ? "text-destructive" : "text-muted-foreground",
                      )}
                    >
                      {message}
                    </span>
                  ) : null}
                </div>
              </>
            ) : (
              <p className="rounded-lg border border-dashed p-4 text-sm text-muted-foreground">
                当前账号只可读取 profile；保存入口需要 config:write 权限。
              </p>
            )}
          </CardContent>
        </Card>

        <Card className="self-start">
          <CardHeader className="border-b">
            <CardTitle className="flex items-center gap-2">
              <History className="size-4" aria-hidden="true" />
              版本历史
            </CardTitle>
          </CardHeader>
          <CardContent className="grid gap-2">
            {(configs.data?.history ?? []).map((item) => (
              <article key={item.id} className="rounded-lg border p-3">
                <button
                  type="button"
                  className="flex w-full items-center justify-between gap-3 text-left"
                  onClick={() => setExpandedId(expandedId === item.id ? null : item.id)}
                  aria-expanded={expandedId === item.id}
                >
                  <span className="font-semibold">v{item.version}</span>
                  <Badge variant={item.id === current?.id ? "default" : "outline"}>
                    {item.id === current?.id ? "current" : "history"}
                  </Badge>
                </button>
                <div className="mt-2 flex min-w-0 items-start gap-2 rounded bg-muted/40 p-2">
                  <Fingerprint className="mt-0.5 size-3.5 shrink-0" aria-hidden="true" />
                  <code className="break-all text-[10px]">{item.config_fingerprint}</code>
                </div>
                {expandedId === item.id ? (
                  <pre className="mt-3 max-h-72 overflow-auto whitespace-pre-wrap break-all rounded bg-slate-950 p-3 text-[10px] leading-4 text-slate-100">
                    {pretty(item.definition)}
                  </pre>
                ) : null}
              </article>
            ))}
            {!configs.isLoading && (configs.data?.history.length ?? 0) === 0 ? (
              <p className="rounded-lg border border-dashed p-5 text-center text-sm text-muted-foreground">
                尚无 profile；首次保存将创建 v1。
              </p>
            ) : null}
            {configs.data ? (
              <div className="flex items-center justify-between border-t pt-3 text-xs text-muted-foreground">
                <span>共 {configs.data.total} 个版本</span>
                <div className="flex gap-2">
                  <Button
                    size="sm"
                    variant="outline"
                    disabled={historyOffset === 0}
                    onClick={() => setHistoryOffset(Math.max(0, historyOffset - 50))}
                  >
                    上一页
                  </Button>
                  <Button
                    size="sm"
                    variant="outline"
                    disabled={historyOffset + configs.data.limit >= configs.data.total}
                    onClick={() => setHistoryOffset(historyOffset + 50)}
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
  );
}

type Detector = NonNullable<
  NonNullable<ReturnType<typeof useDetectionConfigs>["data"]>["current"]
>["definition"]["detectors"][number];

function ExactTables({ detector }: { detector: Detector }) {
  if (detector.type === "split_invoice") {
    const entries = Object.entries(detector.merchant_aliases);
    return (
      <ExactTable
        label="EXACT MERCHANT → CANONICAL"
        rows={entries.map(([alias, canonical]) => [alias, canonical])}
      />
    );
  }
  if (detector.type === "spatiotemporal_tier0") {
    const aliases = Object.entries(detector.location_aliases);
    return (
      <div className="grid gap-2">
        <ExactTable
          label="EXACT LOCATION → ZONE"
          rows={aliases.map(([alias, zone]) => [alias, zone])}
        />
        <ExactTable label="INCOMPATIBLE ZONE PAIRS" rows={detector.incompatible_zone_pairs} />
      </div>
    );
  }
  if (detector.type === "sequential_invoice")
    return (
      <p className="break-words text-muted-foreground">
        partition：{detector.partition_fields.join(" · ") || "无"}
        <br />
        suffix：{detector.numeric_suffix_min_digits}–{detector.numeric_suffix_max_digits} digits
      </p>
    );
  return (
    <p className="break-words text-muted-foreground">
      period：{detector.period}
      <br />
      MAD × {detector.mad_multiplier}
    </p>
  );
}

function ExactTable({ label, rows }: { label: string; rows: readonly (readonly string[])[] }) {
  return (
    <div className="min-w-0 rounded border bg-background/70">
      <div className="border-b px-2 py-1 text-[9px] font-semibold tracking-[0.08em] text-muted-foreground">
        {label}
      </div>
      {rows.length === 0 ? (
        <div className="px-2 py-1.5 text-[10px] text-muted-foreground">未配置 exact 映射</div>
      ) : (
        rows.slice(0, 4).map((row, index) => (
          <div
            key={`${row.join("\u0000")}-${index}`}
            className="grid grid-cols-2 gap-2 border-b px-2 py-1 font-mono text-[9px] last:border-b-0"
          >
            <span className="break-all">{row[0]}</span>
            <span className="break-all text-muted-foreground">{row[1]}</span>
          </div>
        ))
      )}
      {rows.length > 4 ? (
        <div className="border-t px-2 py-1 text-[9px] text-muted-foreground">
          另有 {rows.length - 4} 项
        </div>
      ) : null}
    </div>
  );
}

function Metric({ label, value, mono = false }: { label: string; value: string; mono?: boolean }) {
  return (
    <div className="min-w-0">
      <div className="text-[10px] font-semibold tracking-[0.12em] opacity-60">{label}</div>
      <div className={cn("mt-1 break-all text-sm", mono && "font-mono text-xs")}>{value}</div>
    </div>
  );
}
