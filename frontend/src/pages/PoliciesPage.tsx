import { useMemo, useState } from "react";
import { BookOpenCheck, FileCheck2, Link2, Search, Upload } from "lucide-react";

import { hasPermission, PERMISSIONS } from "@/api/client";
import { useCurrentUser } from "@/auth/useAuth";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import {
  useBindingHistory,
  useCreatePolicyFamily,
  usePolicyCandidates,
  usePolicyDocument,
  usePolicyFamilies,
  usePublishPolicy,
  useSaveBinding,
  useUploadPolicy,
} from "@/policies/usePolicies";
import { useRules } from "@/rules/useRules";

function shortHash(value: string | null | undefined): string {
  return value ? value.slice(0, 12) : "—";
}

function statusLabel(value: string): string {
  return (
    {
      draft: "草稿",
      indexing: "索引中",
      published: "已发布",
      failed: "失败",
      completed: "完成",
      pending: "待处理",
    }[value] ?? value
  );
}

export function PoliciesPage() {
  const { data: user } = useCurrentUser();
  const writable = user ? hasPermission(user, PERMISSIONS.configWrite) : false;
  const families = usePolicyFamilies();
  const rules = useRules();
  const createFamily = useCreatePolicyFamily();
  const uploadPolicy = useUploadPolicy();
  const publishPolicy = usePublishPolicy();
  const [selectedDocumentId, setSelectedDocumentId] = useState<string | null>(null);
  const [selectedRuleId, setSelectedRuleId] = useState<string | null>(null);
  const [expenseDate, setExpenseDate] = useState("");
  const [candidateId, setCandidateId] = useState<string | null>(null);
  const [quoteStart, setQuoteStart] = useState(0);
  const [quoteEnd, setQuoteEnd] = useState(0);
  const [message, setMessage] = useState<string | null>(null);
  const document = usePolicyDocument(selectedDocumentId);
  const candidates = usePolicyCandidates(selectedRuleId, expenseDate);
  const history = useBindingHistory(selectedRuleId);
  const saveBinding = useSaveBinding(selectedRuleId);
  const candidate = useMemo(
    () => candidates.data?.find((item) => item.clause_id === candidateId) ?? null,
    [candidateId, candidates.data],
  );
  const quote = candidate?.clause_text.slice(quoteStart, quoteEnd) ?? "";

  return (
    <div className="grid gap-5">
      <div className="flex items-end justify-between gap-4 border-b-2 border-slate-900 pb-4">
        <div>
          <p className="font-mono text-xs uppercase tracking-[0.28em] text-amber-700">
            Policy ledger / exact evidence
          </p>
          <h1 className="mt-2 text-3xl font-semibold tracking-tight">制度证据库</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            发布不可变制度版本，并把规则逐字绑定到可审计条款。
          </p>
        </div>
        <Badge variant="outline">候选仅供配置 · 报告只认确认绑定</Badge>
      </div>

      {writable ? (
        <div className="grid grid-cols-2 gap-4">
          <Card>
            <CardHeader>
              <CardTitle>01 · 建立制度族</CardTitle>
            </CardHeader>
            <CardContent>
              <form
                className="grid grid-cols-[1fr_1.4fr_auto] gap-2"
                onSubmit={(event) => {
                  event.preventDefault();
                  const data = new FormData(event.currentTarget);
                  createFamily.mutate(
                    {
                      stable_key: String(data.get("stable_key")),
                      display_name: String(data.get("display_name")),
                    },
                    {
                      onSuccess: () => setMessage("制度族已保存"),
                      onError: (error) => setMessage(error.message),
                    },
                  );
                }}
              >
                <Input name="stable_key" placeholder="travel-policy" required />
                <Input name="display_name" placeholder="差旅费用制度" required />
                <Button type="submit">保存</Button>
              </form>
            </CardContent>
          </Card>
          <Card>
            <CardHeader>
              <CardTitle>02 · 上传不可变版本</CardTitle>
            </CardHeader>
            <CardContent>
              <form
                className="grid grid-cols-2 gap-2"
                onSubmit={(event) => {
                  event.preventDefault();
                  const form = event.currentTarget;
                  const data = new FormData(form);
                  const file = data.get("file");
                  if (!(file instanceof File) || file.size === 0)
                    return setMessage("请选择制度文件");
                  uploadPolicy.mutate(
                    {
                      familyId: String(data.get("family_id")),
                      title: String(data.get("title")),
                      version: String(data.get("version")),
                      effectiveDate: String(data.get("effective_date")),
                      ...(String(data.get("expiry_date") ?? "")
                        ? { expiryDate: String(data.get("expiry_date")) }
                        : {}),
                      file,
                    },
                    {
                      onSuccess: (value) => {
                        setSelectedDocumentId(value.document.id);
                        setMessage("制度已解析，等待发布");
                        form.reset();
                      },
                      onError: (error) => setMessage(error.message),
                    },
                  );
                }}
              >
                <select
                  name="family_id"
                  required
                  className="h-9 rounded-md border bg-background px-3 text-sm"
                >
                  <option value="">选择制度族</option>
                  {(families.data ?? []).map((item) => (
                    <option key={item.id} value={item.id}>
                      {item.display_name}
                    </option>
                  ))}
                </select>
                <Input name="title" placeholder="文档标题" required />
                <Input name="version" placeholder="版本，例如 2026.1" required />
                <Input name="effective_date" type="date" required />
                <Input name="expiry_date" type="date" aria-label="失效日期" />
                <Input name="file" type="file" accept=".pdf,.docx,.txt" required />
                <Button type="submit" className="col-span-2">
                  <Upload aria-hidden="true" />
                  上传并解析
                </Button>
              </form>
            </CardContent>
          </Card>
        </div>
      ) : null}

      {message ? (
        <div role="status" className="border-l-4 border-amber-500 bg-amber-50 px-4 py-3 text-sm">
          {message}
        </div>
      ) : null}

      <div className="grid grid-cols-[minmax(300px,.72fr)_minmax(0,1.7fr)] gap-4">
        <Card className="min-w-0 self-start overflow-hidden">
          <CardHeader className="bg-slate-950 text-white">
            <CardTitle>制度版本账本</CardTitle>
          </CardHeader>
          <CardContent className="grid min-w-0 gap-4 p-4">
            {families.isLoading ? <p className="text-sm">加载制度中</p> : null}
            {families.isError ? (
              <p role="alert" className="text-sm text-destructive">
                {families.error.message}
              </p>
            ) : null}
            {(families.data ?? []).map((family) => (
              <section key={family.id} className="grid min-w-0 gap-2">
                <div className="min-w-0">
                  <p className="font-semibold">{family.display_name}</p>
                  <p className="break-all font-mono text-xs text-muted-foreground">
                    {family.stable_key}
                  </p>
                </div>
                {family.documents.map((item) => (
                  <button
                    key={item.id}
                    type="button"
                    onClick={() => setSelectedDocumentId(item.id)}
                    className="min-w-0 w-full rounded-md border p-3 text-left hover:border-slate-500 hover:bg-slate-50"
                  >
                    <div className="flex min-w-0 items-center justify-between gap-2">
                      <span className="min-w-0 truncate text-sm font-medium">
                        {item.title} · {item.version}
                      </span>
                      <Badge variant="outline">{statusLabel(item.status)}</Badge>
                    </div>
                    <p className="mt-1 font-mono text-[11px] text-muted-foreground">
                      {item.effective_date} → {item.expiry_date ?? "持续有效"} ·{" "}
                      {shortHash(item.content_sha256)}
                    </p>
                    {item.index_status ? (
                      <p className="mt-1 text-xs text-muted-foreground">
                        索引 {statusLabel(item.index_status)} · {item.index_completed_points ?? 0}/
                        {item.index_expected_points ?? 0}
                      </p>
                    ) : null}
                  </button>
                ))}
              </section>
            ))}
          </CardContent>
        </Card>

        <div className="grid min-w-0 gap-4">
          <Card className="min-w-0 overflow-hidden">
            <CardHeader className="flex-row items-center justify-between border-b">
              <CardTitle>条款原文预览</CardTitle>
              {document.data?.status === "draft" && writable ? (
                <Button
                  size="sm"
                  onClick={() =>
                    publishPolicy.mutate(document.data!.id, {
                      onSuccess: () => setMessage("已进入索引队列"),
                      onError: (error) => setMessage(error.message),
                    })
                  }
                >
                  <FileCheck2 aria-hidden="true" />
                  发布
                </Button>
              ) : null}
            </CardHeader>
            <CardContent className="max-h-[420px] overflow-y-auto p-0">
              {!selectedDocumentId ? <Empty text="从左侧选择制度版本" /> : null}
              {document.isLoading ? <Empty text="载入条款原文" /> : null}
              {document.isError ? <Empty text={document.error.message} error /> : null}
              {document.data?.clauses.map((clause) => (
                <article
                  key={clause.id}
                  className="grid grid-cols-[130px_1fr] gap-4 border-b px-5 py-4"
                >
                  <div>
                    <Badge variant="secondary">{clause.clause_no}</Badge>
                    <p className="mt-2 font-mono text-[10px] text-muted-foreground">
                      {shortHash(clause.text_sha256)}
                    </p>
                  </div>
                  <p className="min-w-0 whitespace-pre-wrap break-words text-sm leading-6">
                    {clause.text}
                  </p>
                </article>
              ))}
            </CardContent>
          </Card>

          <Card className="min-w-0 overflow-hidden">
            <CardHeader>
              <CardTitle className="flex items-center gap-2">
                <Link2 className="size-4" />
                规则 ↔ 条款逐字绑定
              </CardTitle>
            </CardHeader>
            <CardContent className="grid gap-4">
              <div className="grid grid-cols-[1fr_180px_auto] gap-2">
                <select
                  className="h-9 rounded-md border bg-background px-3 text-sm"
                  value={selectedRuleId ?? ""}
                  onChange={(event) => {
                    setSelectedRuleId(event.target.value || null);
                    setCandidateId(null);
                  }}
                >
                  <option value="">选择规则版本</option>
                  {(rules.data ?? []).map((rule) => (
                    <option key={rule.id} value={rule.id}>
                      {rule.rule_id} · v{rule.version}
                    </option>
                  ))}
                </select>
                <Input
                  type="date"
                  value={expenseDate}
                  onChange={(event) => setExpenseDate(event.target.value)}
                  aria-label="费用发生日"
                />
                <Button
                  variant="outline"
                  onClick={() => void candidates.refetch()}
                  disabled={!selectedRuleId || !expenseDate}
                >
                  <Search aria-hidden="true" />
                  检索
                </Button>
              </div>
              {candidates.isError ? (
                <p role="alert" className="text-sm text-destructive">
                  {candidates.error.message}
                </p>
              ) : null}
              <div className="grid gap-2">
                {(candidates.data ?? []).map((item) => (
                  <button
                    key={item.chunk_id}
                    type="button"
                    onClick={() => {
                      setCandidateId(item.clause_id);
                      setQuoteStart(0);
                      setQuoteEnd(item.clause_text.length);
                    }}
                    className="rounded-md border p-3 text-left hover:bg-slate-50"
                  >
                    <div className="flex justify-between gap-3">
                      <span className="text-sm font-medium">
                        {item.document_title} · {item.clause_no}
                      </span>
                      <span className="font-mono text-xs">
                        rerank {item.rerank_score.toFixed(3)}
                      </span>
                    </div>
                    <p className="mt-2 line-clamp-3 whitespace-pre-wrap text-xs leading-5 text-muted-foreground">
                      {item.clause_text}
                    </p>
                  </button>
                ))}
              </div>
              {candidate && writable ? (
                <div className="grid gap-3 border-l-4 border-emerald-600 bg-emerald-50/60 p-4">
                  <p className="text-sm font-medium">框选连续 Unicode code point 区间</p>
                  <div className="flex items-center gap-2">
                    <Input
                      type="number"
                      min={0}
                      max={candidate.clause_text.length}
                      value={quoteStart}
                      onChange={(event) => setQuoteStart(Number(event.target.value))}
                    />
                    <span>至</span>
                    <Input
                      type="number"
                      min={1}
                      max={candidate.clause_text.length}
                      value={quoteEnd}
                      onChange={(event) => setQuoteEnd(Number(event.target.value))}
                    />
                  </div>
                  <blockquote className="max-h-36 overflow-auto whitespace-pre-wrap break-words border-l-2 border-emerald-700 pl-3 text-sm">
                    {quote || "当前区间为空"}
                  </blockquote>
                  <Button
                    disabled={!quote.trim()}
                    onClick={() =>
                      saveBinding.mutate(
                        {
                          expense_date: expenseDate,
                          selections: [
                            {
                              policy_document_id: candidate.document_id,
                              policy_clause_id: candidate.clause_id,
                              quote_start: quoteStart,
                              quote_end: quoteEnd,
                              exact_quote: quote,
                              citation_order: 1,
                            },
                          ],
                        },
                        {
                          onSuccess: () => setMessage("逐字引用已验证并保存"),
                          onError: (error) => setMessage(error.message),
                        },
                      )
                    }
                  >
                    <BookOpenCheck aria-hidden="true" />
                    机械校验并保存
                  </Button>
                </div>
              ) : null}
              {(history.data ?? []).length > 0 ? (
                <div className="border-t pt-3">
                  <p className="text-xs font-medium uppercase tracking-wider text-muted-foreground">
                    绑定历史
                  </p>
                  {history.data?.map((item) => (
                    <p key={item.id} className="mt-2 truncate text-sm" title={item.quote}>
                      #{item.citation_order} {item.document_title} · {item.clause_no} · “
                      {item.quote}”
                    </p>
                  ))}
                </div>
              ) : null}
            </CardContent>
          </Card>
        </div>
      </div>
    </div>
  );
}

function Empty({ text, error = false }: { text: string; error?: boolean }) {
  return (
    <div
      role={error ? "alert" : undefined}
      className={
        error
          ? "p-8 text-center text-sm text-destructive"
          : "p-8 text-center text-sm text-muted-foreground"
      }
    >
      {text}
    </div>
  );
}
