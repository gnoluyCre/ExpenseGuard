import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { FindingItem, FindingsResponse, ValidationSummary } from "@/api/client";
import { BatchValidationView } from "@/batches/BatchValidationView";
import { makeUser, renderWithProviders } from "@/test/utils";

const BATCH_ID = "11111111-1111-1111-1111-111111111111";
const REVISION_ID = "33333333-3333-3333-3333-333333333333";
const MAPPING_ID = "22222222-2222-2222-2222-222222222222";

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function summary(reusedExisting = false): ValidationSummary {
  return {
    file_version_id: BATCH_ID,
    mapping_version_id: MAPPING_ID,
    ruleset_fingerprint: "f".repeat(64),
    total_row_count: 100,
    evaluated_row_count: 98,
    passed_count: 80,
    flagged_count: 12,
    manual_review_count: 6,
    parse_failed_count: 2,
    reused_existing: reusedExisting,
  };
}

function finding(verdict: "flagged" | "manual_review", rowNo: number): FindingItem {
  return {
    id: `${rowNo}`.padStart(8, "0") + "-0000-0000-0000-000000000000",
    row_no: rowNo,
    verdict,
    rule_id: "expense.limit.domestic.hotel.with.a.very.long.identifier",
    rule_kind: "limit",
    rule_version: "7",
    outcome: verdict === "flagged" ? "flagged" : "unavailable",
    reason_code: verdict === "flagged" ? "limit_exceeded" : "MISSING_REQUIRED_FIELD",
    reasoning: verdict === "flagged" ? "金额超过已冻结规则阈值" : "缺少规则求值所需字段",
    evidence: {
      schema_version: 1,
      rule_kind: "limit",
      outcome: verdict === "flagged" ? "flagged" : "unavailable",
      reason_code: verdict === "flagged" ? "limit_exceeded" : "MISSING_REQUIRED_FIELD",
      required_fields: ["amount", "expense_type"],
      provenance: {},
      operator: "gt",
      amount: "1688.00",
      max_amount: "1200.00",
      currency: "CNY",
      expense_type: "酒店",
    },
  };
}

function findings(verdict: "flagged" | "manual_review", page: number): FindingsResponse {
  return {
    file_version_id: BATCH_ID,
    page,
    page_size: 50,
    total: verdict === "flagged" ? 51 : 1,
    items: [finding(verdict, verdict === "flagged" ? page : 88)],
  };
}

afterEach(() => vi.unstubAllGlobals());

describe("BatchValidationView", () => {
  it("把 VALIDATION_NOT_FOUND 呈现为空态，并在校验成功后刷新为摘要", async () => {
    let validated = false;
    const calls: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const request = input instanceof Request ? input : new Request(String(input), init);
        const url = new URL(request.url);
        calls.push(`${request.method} ${url.pathname}`);
        if (request.method === "GET" && url.pathname.endsWith("/validation")) {
          return validated
            ? json(summary())
            : json({ error: { code: "VALIDATION_NOT_FOUND", message: "尚未执行校验" } }, 404);
        }
        if (request.method === "POST" && url.pathname.endsWith("/validate")) {
          validated = true;
          return json(summary(false));
        }
        if (request.method === "GET" && url.pathname.endsWith("/findings")) {
          return json(findings("flagged", 1));
        }
        if (request.method === "GET" && url.pathname === "/api/batches") return json([]);
        throw new Error(`未覆盖请求：${request.method} ${url.pathname}`);
      }),
    );

    const user = userEvent.setup();
    renderWithProviders(<BatchValidationView fileVersionId={BATCH_ID} user={makeUser()} />);
    expect(await screen.findByText("尚无确定性校验快照")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "执行确定性校验" }));

    expect(await screen.findByText("确定性校验已完成")).toBeInTheDocument();
    expect(await screen.findByText("80")).toBeInTheDocument();
    expect(calls).toContain(`POST /api/batches/${BATCH_ID}/validate`);
    expect(calls.filter((call) => call.endsWith("/validation")).length).toBeGreaterThan(1);
  });

  it("viewer 可读摘要、证据与筛选，但看不到校验和派生操作", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const request = input instanceof Request ? input : new Request(String(input), init);
        const url = new URL(request.url);
        if (url.pathname.endsWith("/validation")) return json(summary());
        if (url.pathname.endsWith("/findings")) {
          const verdict = url.searchParams.get("verdict") as "flagged" | "manual_review";
          return json(findings(verdict, 1));
        }
        throw new Error(`未覆盖请求：${request.method} ${url.pathname}`);
      }),
    );

    const user = userEvent.setup();
    renderWithProviders(
      <BatchValidationView
        fileVersionId={BATCH_ID}
        user={makeUser({ role: "viewer", permissions: ["batch:read"] })}
      />,
    );

    expect(await screen.findByText("金额超过已冻结规则阈值")).toBeInTheDocument();
    expect(screen.getByText(/1688.00/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /再次校验/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "应用新规则集" })).not.toBeInTheDocument();

    await user.click(screen.getByRole("tab", { name: "需人工复核" }));
    expect(await screen.findByText("缺少规则求值所需字段")).toBeInTheDocument();
  });

  it("支持 findings 翻页，并为两类派生请求发送不同的合法 Idempotency-Key", async () => {
    const revisionRequests: { key: string | null; reason: unknown }[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const request = input instanceof Request ? input : new Request(String(input), init);
        const url = new URL(request.url);
        if (request.method === "GET" && url.pathname.endsWith("/validation"))
          return json(summary());
        if (request.method === "GET" && url.pathname.endsWith("/findings")) {
          const verdict = url.searchParams.get("verdict") as "flagged" | "manual_review";
          return json(findings(verdict, Number(url.searchParams.get("page"))));
        }
        if (request.method === "POST" && url.pathname.endsWith("/revisions")) {
          const body: unknown = await request.json();
          revisionRequests.push({ key: request.headers.get("Idempotency-Key"), reason: body });
          return json({
            file_version_id: REVISION_ID,
            source_file_version_id: BATCH_ID,
            root_file_version_id: BATCH_ID,
            revision_no: revisionRequests.length + 1,
            reason: (body as { reason: string }).reason,
            parse_status: "unparsed",
            mapping_version_id: null,
            reused_existing: false,
          });
        }
        if (request.method === "GET" && url.pathname === "/api/batches") return json([]);
        throw new Error(`未覆盖请求：${request.method} ${url.pathname}`);
      }),
    );

    const user = userEvent.setup();
    renderWithProviders(<BatchValidationView fileVersionId={BATCH_ID} user={makeUser()} />);
    expect(await screen.findByText("金额超过已冻结规则阈值")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /下一页/ }));
    await waitFor(() => expect(screen.getByText(/第 2 \/ 2 页/)).toBeInTheDocument());

    await user.click(screen.getByRole("button", { name: "应用新规则集" }));
    await waitFor(() => expect(revisionRequests).toHaveLength(1));
    await user.click(screen.getByRole("button", { name: "重新映射字段" }));
    await waitFor(() => expect(revisionRequests).toHaveLength(2));

    expect(revisionRequests.map((item) => item.reason)).toEqual([
      { reason: "ruleset_change" },
      { reason: "mapping_change" },
    ]);
    expect(revisionRequests.every((item) => (item.key?.length ?? 0) >= 8)).toBe(true);
    expect(revisionRequests[0]?.key).not.toBe(revisionRequests[1]?.key);
  });
});
