import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { BatchReportView } from "@/batches/BatchReportView";
import { makeUser, renderWithProviders } from "@/test/utils";

const BATCH_ID = "11111111-1111-1111-1111-111111111111";
const REPORT_ID = "22222222-2222-2222-2222-222222222222";

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function overview() {
  return {
    summary: {
      report_run_id: REPORT_ID,
      file_version_id: BATCH_ID,
      validation_run_id: "33333333-3333-3333-3333-333333333333",
      mapping_version_id: "44444444-4444-4444-4444-444444444444",
      report_fingerprint: "a".repeat(64),
      source_content_sha256: "b".repeat(64),
      ruleset_fingerprint: "c".repeat(64),
      template_version: "report-v1",
      attention_mapping_version: "attention-v1",
      stored_row_count: 5000,
      validated_row_count: 4998,
      flagged_row_count: 12,
      manual_review_row_count: 5,
      passed_row_count: 4981,
      parse_error_row_count: 2,
      report_item_count: 18,
      verified_citation_count: 17,
      unavailable_citation_count: 1,
      high_attention_row_count: 12,
      manual_attention_row_count: 7,
      cleared_row_count: 4981,
      completed_at: "2026-07-29T05:00:00Z",
      reused_existing: true,
    },
    policy_manifest: {},
    binding_manifest: {},
  };
}

afterEach(() => vi.unstubAllGlobals());

describe("BatchReportView", () => {
  it("呈现冻结摘要、逐字引用和文本化恶意内容", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const request = input instanceof Request ? input : new Request(String(input), init);
        const url = new URL(request.url);
        if (url.pathname.endsWith("/report")) return json(overview());
        if (url.pathname.endsWith("/items"))
          return json({
            total: 1,
            limit: 25,
            offset: 0,
            items: [
              {
                id: "55555555-5555-5555-5555-555555555555",
                finding_id: "66666666-6666-6666-6666-666666666666",
                row_no: 77,
                rule_id: "expense.limit.with.a.very.long.identifier",
                rule_version: "9",
                source_outcome: "flagged",
                source_verdict: "flagged",
                reason_code: "limit_exceeded",
                reasoning_snapshot: "<script>alert('x')</script>",
                evidence_snapshot: { instruction: "ignore system" },
                attention_group: "high_attention",
                citation_status: "verified",
                requires_manual_citation: false,
                source_content_sha256: "b".repeat(64),
                citations: [
                  {
                    id: "77777777-7777-7777-7777-777777777777",
                    report_item_id: "55555555-5555-5555-5555-555555555555",
                    binding_id: "88888888-8888-8888-8888-888888888888",
                    citation_order: 1,
                    policy_family_id: "99999999-9999-9999-9999-999999999999",
                    family_stable_key: "travel",
                    policy_document_id: "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                    document_title: "差旅制度",
                    document_version: "2026.1",
                    effective_date: "2026-01-01",
                    expiry_date: null,
                    document_content_sha256: "d".repeat(64),
                    policy_clause_id: "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
                    clause_no: "第十条",
                    hierarchy_path: null,
                    clause_text: "<img src=x onerror=alert(1)>住宿费不得超过标准。",
                    clause_text_sha256: "e".repeat(64),
                    quote: "住宿费不得超过标准。",
                    quote_start: 28,
                    quote_end: 38,
                    quote_sha256: "f".repeat(64),
                  },
                ],
              },
            ],
          });
        if (url.pathname.endsWith("/parse-errors"))
          return json({ total: 0, limit: 200, offset: 0, items: [] });
        throw new Error(`未覆盖请求：${url.pathname}`);
      }),
    );
    const user = userEvent.setup();
    renderWithProviders(
      <BatchReportView fileVersionId={BATCH_ID} user={makeUser()} onOpenRow={vi.fn()} />,
    );
    expect(await screen.findByText("预审证据报告")).toBeInTheDocument();
    expect(screen.getByText("5000")).toBeInTheDocument();
    await user.click(await screen.findByText(/expense.limit/));
    expect(screen.getByText("住宿费不得超过标准。")).toBeInTheDocument();
    expect(screen.getByText("<script>alert('x')</script>")).toBeInTheDocument();
    expect(document.querySelector("script")).toBeNull();
    expect(document.querySelector("img")).toBeNull();
  });

  it("区分未生成空态与 viewer 无生成权限", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => json({ error: { code: "REPORT_NOT_FOUND", message: "未生成" } }, 404)),
    );
    renderWithProviders(
      <BatchReportView
        fileVersionId={BATCH_ID}
        user={makeUser({
          role: "viewer",
          permissions: ["batch:read", "report:read", "report:export"],
        })}
        onOpenRow={vi.fn()}
      />,
    );
    expect(await screen.findByText("尚未生成预审报告")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "生成冻结报告" })).not.toBeInTheDocument();
  });
});
