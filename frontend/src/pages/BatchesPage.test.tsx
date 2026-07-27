import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Route, Routes } from "react-router";

import type {
  BatchDetail,
  BatchImportResponse,
  BatchSummary,
  FieldAvailabilityResponse,
  ParseBatchResponse,
  ParseErrorsResponse,
  SaveSchemaMappingResponse,
  SchemaMappings,
} from "@/api/client";
import { CURRENT_USER_KEY } from "@/auth/useAuth";
import { BatchesPage } from "@/pages/BatchesPage";
import { createTestQueryClient, makeUser, renderWithProviders } from "@/test/utils";

const BATCH_ID = "11111111-1111-1111-1111-111111111111";
const MAPPING_ID = "22222222-2222-2222-2222-222222222222";

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function notParsed() {
  return json({ error: { code: "BATCH_NOT_PARSED", message: "批次尚未解析" } }, 409);
}

function makeBatch(overrides: Partial<BatchSummary> = {}): BatchSummary {
  return {
    file_version_id: BATCH_ID,
    filename: "2026-07.xlsx",
    content_hash: "abcdef1234567890",
    row_count: 500,
    uploaded_at: "2026-07-27T09:30:00Z",
    uploaded_by: "00000000-0000-0000-0000-000000000001",
    ...overrides,
  };
}

function makeDetail(batch: BatchSummary): BatchDetail {
  return {
    ...batch,
    rows: [
      {
        row_no: 2,
        raw_json: { 员工: "张三", 金额: 120, 费用日期: "2026-07-01" },
        parse_error: null,
      },
    ],
  };
}

function makeMappings(): SchemaMappings {
  return {
    file_version_id: BATCH_ID,
    header_signature: "a".repeat(64),
    source_columns: ["员工", "金额", "费用日期"],
    versions: [
      {
        id: MAPPING_ID,
        version: 3,
        created_at: "2026-07-27T10:00:00Z",
        created_by: "00000000-0000-0000-0000-000000000001",
        is_current_for_batch: false,
        mappings: [
          { source_column: "金额", target_field: "amount" },
          { source_column: "费用日期", target_field: "expense_date" },
        ],
        availability_thresholds: {
          available_min_non_null_rate: "0.8000",
          inferred_min_success_rate: "0.8000",
        },
        currency_aliases: {},
        inference_rules: [],
      },
    ],
  };
}

function makeErrors(): ParseErrorsResponse {
  return {
    file_version_id: BATCH_ID,
    mapping_version_id: MAPPING_ID,
    total: 1,
    offset: 0,
    limit: 50,
    items: [
      {
        row_no: 8,
        raw_json: { 金额: "abc", 费用日期: null },
        parse_error_code: "ROW_VALIDATION_FAILED",
        parse_error: "该行有 2 个字段无法解析",
        parse_error_detail: {
          schema_version: 1,
          mapping_version_id: MAPPING_ID,
          errors: [
            {
              code: "AMOUNT_INVALID_FORMAT",
              field: "amount",
              message: "金额格式无效",
              source_column: "金额",
            },
          ],
        },
      },
    ],
  };
}

function makeAvailability(): FieldAvailabilityResponse {
  return {
    file_version_id: BATCH_ID,
    mapping_version_id: MAPPING_ID,
    items: [
      {
        field_name: "amount",
        status: "available",
        evidence: {
          schema_version: 1,
          mapping_version_id: MAPPING_ID,
          total_rows: 500,
          selected_basis: "direct",
          direct: {
            configured: true,
            source_columns: ["金额"],
            non_null_count: 499,
            non_null_rate: "0.9980",
            threshold: "0.8000",
          },
          inference: {
            configured: false,
            rule_ids: [],
            success_count: 0,
            success_rate: "0.0000",
            threshold: "0.8000",
          },
        },
      },
    ],
  };
}

interface BatchFetchOptions {
  parsed?: boolean;
  calls?: string[];
  onSave?: (body: unknown) => void;
}

function stubBatchFetch(options: BatchFetchOptions = {}): void {
  const batch = makeBatch();
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const request = input instanceof Request ? input : new Request(String(input), init);
      const url = new URL(request.url);
      options.calls?.push(`${request.method} ${url.pathname}`);

      if (request.method === "GET" && url.pathname === "/api/batches") return json([batch]);
      if (request.method === "GET" && url.pathname === `/api/batches/${BATCH_ID}`)
        return json(makeDetail(batch));
      if (request.method === "GET" && url.pathname === "/api/schema-mappings")
        return json(makeMappings());
      if (request.method === "GET" && url.pathname.endsWith("/parse-errors"))
        return options.parsed ? json(makeErrors()) : notParsed();
      if (request.method === "GET" && url.pathname.endsWith("/field-availability"))
        return options.parsed ? json(makeAvailability()) : notParsed();
      if (request.method === "PUT" && url.pathname === "/api/schema-mappings") {
        options.onSave?.(await request.json());
        const response: SaveSchemaMappingResponse = {
          ...makeMappings().versions[0]!,
          id: "33333333-3333-3333-3333-333333333333",
          version: 4,
          mappings: [
            { source_column: "员工", target_field: "employee" },
            { source_column: "金额", target_field: "amount" },
            { source_column: "费用日期", target_field: "expense_date" },
          ],
          reused_existing: false,
        };
        return json(response, 201);
      }
      if (request.method === "POST" && url.pathname.endsWith("/parse")) {
        const response: ParseBatchResponse = {
          file_version_id: BATCH_ID,
          mapping_version_id: MAPPING_ID,
          mapping_version: 3,
          status: "parsed_with_errors",
          total_rows: 500,
          success_count: 499,
          error_count: 1,
          parsed_at: "2026-07-27T10:05:00Z",
          reused_existing: false,
        };
        options.parsed = true;
        return json(response);
      }
      throw new Error(`测试桩未覆盖的请求: ${request.method} ${url.pathname}`);
    }),
  );
}

function renderBatchesPage(user = makeUser()) {
  const queryClient = createTestQueryClient();
  queryClient.setQueryData(CURRENT_USER_KEY, user);
  return renderWithProviders(
    <Routes>
      <Route path="/batches" element={<BatchesPage />} />
    </Routes>,
    { route: "/batches", queryClient },
  );
}

async function openBatch(user: ReturnType<typeof userEvent.setup>): Promise<void> {
  await user.click(await screen.findByRole("button", { name: /2026-07.xlsx/ }));
  await screen.findByText(/员工: 张三/);
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("BatchesPage", () => {
  it("auditor 可上传 Excel 并看到导入结果", async () => {
    const user = userEvent.setup();
    const imported: BatchImportResponse = {
      ...makeBatch(),
      reused_existing: false,
      stored_rows: 500,
    };
    const calls: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const request = input instanceof Request ? input : new Request(String(input), init);
        const url = new URL(request.url);
        calls.push(`${request.method} ${url.pathname}`);
        if (request.method === "GET" && url.pathname === "/api/batches") return json([]);
        if (request.method === "POST" && url.pathname === "/api/batches") {
          expect(request.credentials).toBe("include");
          return json(imported);
        }
        if (request.method === "GET" && url.pathname === `/api/batches/${BATCH_ID}`)
          return json(makeDetail(imported));
        if (request.method === "GET" && url.pathname === "/api/schema-mappings")
          return json(makeMappings());
        if (request.method === "GET" && url.pathname.endsWith("/parse-errors")) return notParsed();
        if (request.method === "GET" && url.pathname.endsWith("/field-availability"))
          return notParsed();
        throw new Error(`测试桩未覆盖的请求: ${request.method} ${url.pathname}`);
      }),
    );

    renderBatchesPage();
    const file = new File(["fake"], "报销.xlsx", {
      type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    });
    await user.upload(screen.getByLabelText("Excel 文件"), file);
    await user.click(screen.getByRole("button", { name: "导入" }));

    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("导入完成"));
    expect(calls).toContain("POST /api/batches");
  });

  it("viewer 看不到上传入口但可以看批次列表", async () => {
    stubBatchFetch();
    renderBatchesPage(
      makeUser({
        role: "viewer",
        permissions: ["batch:read", "report:read", "report:export"],
      }),
    );

    expect(screen.queryByRole("button", { name: "导入" })).not.toBeInTheDocument();
    await waitFor(() => expect(screen.getByText("2026-07.xlsx")).toBeInTheDocument());
  });

  it("点击批次后加载原始行详情与四个视图", async () => {
    const user = userEvent.setup();
    stubBatchFetch();
    renderBatchesPage();

    await openBatch(user);
    expect(screen.getByText("未解析")).toBeInTheDocument();
    expect(screen.getAllByRole("tab").map((tab) => tab.textContent)).toEqual([
      "原始数据500",
      "字段映射1",
      "错误清单",
      "字段可用性",
    ]);
  });

  it("configurator 可编辑字段映射并保存不可变新版本", async () => {
    const user = userEvent.setup();
    let savedBody: unknown;
    stubBatchFetch({ onSave: (body) => (savedBody = body) });
    renderBatchesPage(
      makeUser({
        role: "configurator",
        permissions: ["batch:import", "batch:read", "config:read", "config:write"],
      }),
    );

    await openBatch(user);
    await user.click(screen.getByRole("tab", { name: /字段映射/ }));
    await user.selectOptions(screen.getByLabelText("员工 映射字段"), "employee");
    await user.click(screen.getByRole("button", { name: "保存新版本" }));

    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("已保存映射 v4"));
    expect(savedBody).toMatchObject({
      file_version_id: BATCH_ID,
      mappings: expect.arrayContaining([
        { source_column: "员工", target_field: "employee" },
        { source_column: "金额", target_field: "amount" },
        { source_column: "费用日期", target_field: "expense_date" },
      ]),
    });
  });

  it("auditor 只能复用映射，触发解析后刷新结果缓存", async () => {
    const user = userEvent.setup();
    const calls: string[] = [];
    stubBatchFetch({ calls });
    renderBatchesPage();

    await openBatch(user);
    await user.click(screen.getByRole("tab", { name: /字段映射/ }));
    expect(screen.queryByRole("button", { name: "保存新版本" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "触发解析" }));

    await waitFor(() =>
      expect(screen.getByRole("status")).toHaveTextContent("成功 499 行，失败 1 行"),
    );
    await waitFor(() => {
      expect(
        calls.filter((call) => call === `GET /api/batches/${BATCH_ID}/parse-errors`).length,
      ).toBeGreaterThan(1);
      expect(
        calls.filter((call) => call === `GET /api/batches/${BATCH_ID}/field-availability`).length,
      ).toBeGreaterThan(1);
    });
    expect(calls).toContain(`POST /api/batches/${BATCH_ID}/parse`);
  });

  it("viewer 仅查看解析结果并能读取逐行错误", async () => {
    const user = userEvent.setup();
    const calls: string[] = [];
    stubBatchFetch({ parsed: true, calls });
    renderBatchesPage(
      makeUser({
        role: "viewer",
        permissions: ["batch:read", "report:read", "report:export"],
      }),
    );

    await openBatch(user);
    expect(screen.queryByRole("button", { name: "触发解析" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("tab", { name: /错误清单/ }));
    expect(await screen.findByText("该行有 2 个字段无法解析")).toBeInTheDocument();
    await user.click(screen.getByRole("tab", { name: /字段映射/ }));
    expect(screen.getByText(/不能读取字段映射配置/)).toBeInTheDocument();
    expect(calls).not.toContain("GET /api/schema-mappings");
  });
});
