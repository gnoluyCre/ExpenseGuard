import { useEffect, useMemo, useState, type FormEvent } from "react";
import { Fingerprint, History, RefreshCw, Save, ShieldCheck } from "lucide-react";

import { hasPermission, PERMISSIONS } from "@/api/client";
import type { components } from "@/api/schema";
import { useCurrentUser } from "@/auth/useAuth";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { cn } from "@/lib/utils";
import { useRules, useSaveRule, type RuleVersion } from "@/rules/useRules";

type RuleDefinition = components["schemas"]["RuleDefinition"];
type RuleKind = components["schemas"]["RuleKind"];
type ExemptionGroup = components["schemas"]["ExemptionGroup"];

const RULE_KINDS = [
  "limit",
  "invoice_type",
  "timeliness",
  "invoice_title",
  "invoice_duplicate",
] as const satisfies readonly RuleKind[];

const RULE_META: Record<RuleKind, { label: string; eyebrow: string; description: string }> = {
  limit: { label: "费用限额", eyebrow: "LIMIT", description: "按费用类型与币种设置金额上限" },
  invoice_type: {
    label: "票种合规",
    eyebrow: "INVOICE TYPE",
    description: "限定各费用类型可接受的票种",
  },
  timeliness: { label: "报销时效", eyebrow: "TIMELINESS", description: "控制提交日距费用日的天数" },
  invoice_title: {
    label: "发票抬头",
    eyebrow: "INVOICE TITLE",
    description: "维护允许逐字匹配的企业抬头",
  },
  invoice_duplicate: {
    label: "发票号查重",
    eyebrow: "DUPLICATE",
    description: "检测批次内及历史批次的重复发票号",
  },
};

interface EditorDraft {
  ruleId: string;
  effectiveFrom: string;
  enabled: boolean;
  requireDirect: boolean;
  definitionLines: string;
  exemptionLines: string;
}

function today(): string {
  return new Date().toISOString().slice(0, 10);
}

function latestForKind(versions: RuleVersion[], kind: RuleKind): RuleVersion | undefined {
  return versions
    .filter((version) => version.definition.kind === kind)
    .sort((left, right) => right.version - left.version)[0];
}

function definitionLines(definition: RuleDefinition): string {
  switch (definition.kind) {
    case "limit":
      return definition.thresholds
        .map((item) => `${item.expense_type} | ${item.currency} | ${item.max_amount}`)
        .join("\n");
    case "invoice_type":
      return definition.allowances
        .map((item) => `${item.expense_type} | ${item.allowed_invoice_types.join(", ")}`)
        .join("\n");
    case "timeliness":
      return definition.policies
        .map((item) => `${item.expense_type} | ${item.max_calendar_days}`)
        .join("\n");
    case "invoice_title":
      return definition.allowed_titles.join("\n");
    case "invoice_duplicate":
      return "";
  }
}

function exemptionLines(exemptions: ExemptionGroup[]): string {
  return exemptions
    .map(
      (group) =>
        `${group.exemption_id} | ${group.all.map((item) => `${item.field}=${item.value}`).join(", ")}`,
    )
    .join("\n");
}

function draftFromVersion(kind: RuleKind, version?: RuleVersion): EditorDraft {
  return {
    ruleId: version?.rule_id ?? `${kind}-policy`,
    effectiveFrom: version?.effective_from ?? today(),
    enabled: version?.definition.enabled ?? true,
    requireDirect: version?.definition.require_direct ?? false,
    definitionLines: version ? definitionLines(version.definition) : "",
    exemptionLines: version ? exemptionLines(version.definition.exemptions) : "",
  };
}

function nonEmptyLines(value: string): string[] {
  return value
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean);
}

function parseExemptions(value: string): ExemptionGroup[] {
  return nonEmptyLines(value).map((line) => {
    const [id = "", conditions = ""] = line.split("|").map((part) => part.trim());
    return {
      exemption_id: id,
      all: conditions
        .split(",")
        .map((condition) => condition.trim())
        .filter(Boolean)
        .map((condition) => {
          const [field = "", ...rest] = condition.split("=");
          return {
            field: field.trim() as components["schemas"]["ExemptionField"],
            value: rest.join("=").trim(),
          };
        }),
    };
  });
}

function buildDefinition(kind: RuleKind, draft: EditorDraft): RuleDefinition {
  const common = {
    schema_version: 1 as const,
    enabled: draft.enabled,
    require_direct: draft.requireDirect,
    exemptions: parseExemptions(draft.exemptionLines),
  };
  const lines = nonEmptyLines(draft.definitionLines);
  switch (kind) {
    case "limit":
      return {
        ...common,
        kind,
        thresholds: lines.map((line) => {
          const [expenseType = "", currency = "", maxAmount = ""] = line
            .split("|")
            .map((part) => part.trim());
          return { expense_type: expenseType, currency, max_amount: maxAmount };
        }),
      };
    case "invoice_type":
      return {
        ...common,
        kind,
        allowances: lines.map((line) => {
          const [expenseType = "", types = ""] = line.split("|").map((part) => part.trim());
          return {
            expense_type: expenseType,
            allowed_invoice_types: types
              .split(",")
              .map((item) => item.trim())
              .filter(Boolean),
          };
        }),
      };
    case "timeliness":
      return {
        ...common,
        kind,
        policies: lines.map((line) => {
          const [expenseType = "", maxDays = ""] = line.split("|").map((part) => part.trim());
          return { expense_type: expenseType, max_calendar_days: Number(maxDays) };
        }),
      };
    case "invoice_title":
      return { ...common, kind, allowed_titles: lines };
    case "invoice_duplicate":
      return { ...common, kind };
  }
}

function editorHint(kind: RuleKind): string {
  switch (kind) {
    case "limit":
      return "每行：费用类型 | 币种 | 最大金额";
    case "invoice_type":
      return "每行：费用类型 | 允许票种 1, 允许票种 2";
    case "timeliness":
      return "每行：费用类型 | 最大自然日";
    case "invoice_title":
      return "每行一个允许的发票抬头（逐字匹配）";
    case "invoice_duplicate":
      return "此规则无需额外决策表";
  }
}

function formatDateTime(value: string): string {
  return new Intl.DateTimeFormat("zh-CN", { dateStyle: "medium", timeStyle: "short" }).format(
    new Date(value),
  );
}

export function RulesPage() {
  const { data: user } = useCurrentUser();
  const rules = useRules();
  const saveRule = useSaveRule();
  const [selectedKind, setSelectedKind] = useState<RuleKind>("limit");
  const [draft, setDraft] = useState<EditorDraft>(() => draftFromVersion("limit"));
  const [message, setMessage] = useState<string | null>(null);
  const canWrite = user ? hasPermission(user, PERMISSIONS.configWrite) : false;

  const grouped = useMemo(
    () =>
      Object.fromEntries(
        RULE_KINDS.map((kind) => [
          kind,
          (rules.data ?? [])
            .filter((version) => version.definition.kind === kind)
            .sort((left, right) => right.version - left.version),
        ]),
      ) as Record<RuleKind, RuleVersion[]>,
    [rules.data],
  );
  const latest = grouped[selectedKind][0];

  useEffect(() => {
    setDraft(draftFromVersion(selectedKind, latestForKind(rules.data ?? [], selectedKind)));
    setMessage(null);
  }, [rules.data, selectedKind]);

  function submit(event: FormEvent<HTMLFormElement>): void {
    event.preventDefault();
    setMessage(null);
    saveRule.mutate(
      {
        rule_id: draft.ruleId.trim(),
        effective_from: draft.effectiveFrom,
        definition: buildDefinition(selectedKind, draft),
      },
      {
        onSuccess: ({ rule, created }) => {
          setMessage(
            created
              ? `已创建 ${rule.rule_id} v${rule.version}`
              : `配置未变化，已复用 ${rule.rule_id} v${rule.version}`,
          );
        },
        onError: (error) => setMessage(error.message),
      },
    );
  }

  return (
    <div className="grid gap-4">
      <header className="flex items-end justify-between gap-6 border-b border-border/70 pb-4">
        <div className="min-w-0">
          <div className="mb-2 flex items-center gap-2 text-[11px] font-semibold tracking-[0.22em] text-muted-foreground">
            <ShieldCheck className="size-4 text-primary" aria-hidden="true" />
            DETERMINISTIC POLICY CONTROL
          </div>
          <h1 className="text-2xl font-semibold tracking-tight">规则版本控制台</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            五类确定性规则均以不可变版本追加；历史判定始终保留其原始规则指纹。
          </p>
        </div>
        <Button variant="outline" onClick={() => void rules.refetch()} disabled={rules.isFetching}>
          <RefreshCw aria-hidden="true" />
          刷新
        </Button>
      </header>

      {rules.isError ? (
        <p
          role="alert"
          className="rounded-lg border border-destructive/30 bg-destructive/5 p-3 text-sm text-destructive"
        >
          {rules.error.message}
        </p>
      ) : null}

      <div className="grid grid-cols-5 gap-2" aria-label="规则类型">
        {RULE_KINDS.map((kind) => {
          const current = grouped[kind]?.[0];
          const active = selectedKind === kind;
          return (
            <button
              key={kind}
              type="button"
              onClick={() => setSelectedKind(kind)}
              className={cn(
                "min-w-0 rounded-xl border p-3 text-left transition-colors",
                active
                  ? "border-primary/40 bg-primary/[0.06] shadow-sm"
                  : "border-border bg-card hover:border-primary/25 hover:bg-muted/30",
              )}
            >
              <span className="block text-[10px] font-semibold tracking-[0.16em] text-muted-foreground">
                {RULE_META[kind].eyebrow}
              </span>
              <span className="mt-1 block truncate font-medium" title={RULE_META[kind].label}>
                {RULE_META[kind].label}
              </span>
              <span className="mt-2 flex items-center justify-between text-xs text-muted-foreground">
                <span>{grouped[kind]?.length ?? 0} 个版本</span>
                <span>{current ? `v${current.version}` : "未配置"}</span>
              </span>
            </button>
          );
        })}
      </div>

      <div className="grid grid-cols-[minmax(0,1.45fr)_minmax(300px,0.8fr)] gap-4">
        <Card>
          <CardHeader className="border-b">
            <CardTitle className="flex items-center justify-between gap-3">
              <span>{RULE_META[selectedKind].label}</span>
              {latest ? (
                <Badge variant={latest.definition.enabled ? "default" : "secondary"}>
                  {latest.definition.enabled ? "已启用" : "已停用"}
                </Badge>
              ) : (
                <Badge variant="outline">待配置</Badge>
              )}
            </CardTitle>
            <p className="text-sm text-muted-foreground">{RULE_META[selectedKind].description}</p>
          </CardHeader>
          <CardContent>
            {rules.isLoading ? (
              <p className="text-sm text-muted-foreground">正在读取规则版本…</p>
            ) : null}
            {latest ? (
              <div className="mb-4 grid grid-cols-[0.65fr_0.65fr_1.7fr] gap-3 rounded-lg border bg-muted/20 p-3">
                <Metric label="当前版本" value={`v${latest.version}`} />
                <Metric label="生效日" value={latest.effective_from} />
                <div className="min-w-0">
                  <div className="text-[11px] text-muted-foreground">配置指纹</div>
                  <div
                    className="mt-1 break-all font-mono text-xs leading-relaxed"
                    title={latest.config_fingerprint}
                  >
                    {latest.config_fingerprint}
                  </div>
                </div>
              </div>
            ) : null}

            {canWrite ? (
              <form className="grid gap-4" onSubmit={submit}>
                <div className="grid grid-cols-2 gap-3">
                  <label className="grid gap-1.5 text-sm font-medium">
                    规则标识
                    <Input
                      aria-label="规则标识"
                      value={draft.ruleId}
                      required
                      onChange={(event) => setDraft({ ...draft, ruleId: event.target.value })}
                    />
                  </label>
                  <label className="grid gap-1.5 text-sm font-medium">
                    生效日期
                    <Input
                      aria-label="生效日期"
                      type="date"
                      value={draft.effectiveFrom}
                      required
                      onChange={(event) =>
                        setDraft({ ...draft, effectiveFrom: event.target.value })
                      }
                    />
                  </label>
                </div>

                <div className="flex gap-6 rounded-lg border border-dashed p-3">
                  <CheckField
                    label="启用规则"
                    checked={draft.enabled}
                    onChange={(enabled) => setDraft({ ...draft, enabled })}
                  />
                  <CheckField
                    label="仅接受直接映射字段"
                    checked={draft.requireDirect}
                    onChange={(requireDirect) => setDraft({ ...draft, requireDirect })}
                  />
                </div>

                {selectedKind !== "invoice_duplicate" ? (
                  <label className="grid gap-1.5 text-sm font-medium">
                    {editorHint(selectedKind)}
                    <textarea
                      aria-label="规则决策表"
                      className="min-h-32 w-full resize-y rounded-lg border border-input bg-transparent px-3 py-2 font-mono text-sm leading-6 outline-none focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50"
                      value={draft.definitionLines}
                      onChange={(event) =>
                        setDraft({ ...draft, definitionLines: event.target.value })
                      }
                    />
                  </label>
                ) : (
                  <p className="rounded-lg bg-muted/40 p-3 text-sm text-muted-foreground">
                    {editorHint(selectedKind)}；保存时仅记录启停、字段来源要求和例外条件。
                  </p>
                )}

                <label className="grid gap-1.5 text-sm font-medium">
                  例外条件（可选）
                  <textarea
                    aria-label="例外条件"
                    placeholder="例外标识 | expense_type=差旅, currency=CNY"
                    className="min-h-20 w-full resize-y rounded-lg border border-input bg-transparent px-3 py-2 font-mono text-sm leading-6 outline-none focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50"
                    value={draft.exemptionLines}
                    onChange={(event) => setDraft({ ...draft, exemptionLines: event.target.value })}
                  />
                </label>

                <div className="flex min-w-0 items-center gap-3 border-t pt-4">
                  <Button type="submit" disabled={saveRule.isPending}>
                    <Save aria-hidden="true" />
                    追加新版本
                  </Button>
                  {message ? (
                    <span
                      role={saveRule.isError ? "alert" : "status"}
                      className={cn(
                        "min-w-0 break-words text-sm",
                        saveRule.isError ? "text-destructive" : "text-muted-foreground",
                      )}
                    >
                      {message}
                    </span>
                  ) : null}
                </div>
              </form>
            ) : (
              <p className="rounded-lg border border-dashed p-4 text-sm text-muted-foreground">
                当前账号仅可查看规则版本；保存入口需要 config:write 权限。
              </p>
            )}
          </CardContent>
        </Card>

        <Card className="self-start">
          <CardHeader className="border-b">
            <CardTitle className="flex items-center gap-2">
              <History className="size-4" aria-hidden="true" />
              历史版本
            </CardTitle>
          </CardHeader>
          <CardContent className="grid gap-3">
            {grouped[selectedKind].length === 0 && !rules.isLoading ? (
              <div className="rounded-lg border border-dashed p-5 text-center text-sm text-muted-foreground">
                尚无版本。首次保存将创建 v1。
              </div>
            ) : null}
            {grouped[selectedKind].map((version, index) => (
              <article key={version.id} className="min-w-0 rounded-lg border p-3">
                <div className="flex items-start justify-between gap-3">
                  <div className="min-w-0">
                    <div className="flex items-center gap-2">
                      <span className="font-semibold">v{version.version}</span>
                      {index === 0 ? <Badge variant="outline">最新</Badge> : null}
                    </div>
                    <div
                      className="mt-1 break-words text-sm font-medium [overflow-wrap:anywhere]"
                      title={version.rule_id}
                    >
                      {version.rule_id}
                    </div>
                  </div>
                  <Badge variant={version.definition.enabled ? "secondary" : "outline"}>
                    {version.definition.enabled ? "启用" : "停用"}
                  </Badge>
                </div>
                <dl className="mt-3 grid gap-2 border-t pt-3 text-xs">
                  <div className="flex justify-between gap-3">
                    <dt className="text-muted-foreground">生效日</dt>
                    <dd>{version.effective_from}</dd>
                  </div>
                  <div className="flex justify-between gap-3">
                    <dt className="text-muted-foreground">创建时间</dt>
                    <dd>{formatDateTime(version.created_at)}</dd>
                  </div>
                </dl>
                <div className="mt-3 flex min-w-0 items-start gap-2 rounded bg-muted/40 p-2">
                  <Fingerprint
                    className="mt-0.5 size-3.5 shrink-0 text-muted-foreground"
                    aria-hidden="true"
                  />
                  <code
                    className="min-w-0 break-all text-[10px] leading-relaxed"
                    title={version.config_fingerprint}
                  >
                    {version.config_fingerprint}
                  </code>
                </div>
              </article>
            ))}
          </CardContent>
        </Card>
      </div>
    </div>
  );
}

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div className="min-w-0">
      <div className="text-[11px] text-muted-foreground">{label}</div>
      <div className="mt-1 truncate font-medium" title={value}>
        {value}
      </div>
    </div>
  );
}

function CheckField({
  label,
  checked,
  onChange,
}: {
  label: string;
  checked: boolean;
  onChange: (checked: boolean) => void;
}) {
  return (
    <label className="flex items-center gap-2 text-sm font-medium">
      <input
        type="checkbox"
        checked={checked}
        onChange={(event) => onChange(event.target.checked)}
        className="size-4 accent-primary"
      />
      {label}
    </label>
  );
}
