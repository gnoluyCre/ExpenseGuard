import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Route, Routes } from "react-router";

import type { BatchDetail, BatchImportResponse, BatchSummary } from "@/api/client";
import { CURRENT_USER_KEY } from "@/auth/useAuth";
import { BatchesPage } from "@/pages/BatchesPage";
import { createTestQueryClient, makeUser, renderWithProviders } from "@/test/utils";

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function makeBatch(overrides: Partial<BatchSummary> = {}): BatchSummary {
  return {
    file_version_id: "11111111-1111-1111-1111-111111111111",
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
        raw_json: { 员工: "张三", 金额: 120 },
        parse_error: null,
      },
    ],
  };
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
        const url = input instanceof Request ? input.url : String(input);
        const method = input instanceof Request ? input.method : (init?.method ?? "GET");
        calls.push(`${method} ${new URL(url).pathname}`);
        if (method === "GET" && url.endsWith("/api/batches")) {
          return json([]);
        }
        if (method === "GET" && url.includes(`/api/batches/${imported.file_version_id}`)) {
          return json(makeDetail(imported));
        }
        if (method === "POST" && url.includes("/api/batches")) {
          expect(init?.credentials).toBe("include");
          expect(init?.body).toBeInstanceOf(FormData);
          return json(imported);
        }
        throw new Error(`测试桩未覆盖的请求: ${method} ${url}`);
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
    const batch = makeBatch();
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const request = input instanceof Request ? input : new Request(String(input), init);
        if (request.method === "GET" && request.url.includes("/api/batches")) {
          return json([batch]);
        }
        throw new Error(`测试桩未覆盖的请求: ${request.method} ${request.url}`);
      }),
    );

    renderBatchesPage(
      makeUser({
        role: "viewer",
        permissions: ["batch:read", "report:read", "report:export"],
      }),
    );

    expect(screen.queryByRole("button", { name: "导入" })).not.toBeInTheDocument();
    await waitFor(() => expect(screen.getByText("2026-07.xlsx")).toBeInTheDocument());
  });

  it("点击批次后加载原始行详情", async () => {
    const user = userEvent.setup();
    const batch = makeBatch();
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const request = input instanceof Request ? input : new Request(String(input), init);
        if (request.method === "GET" && request.url.endsWith("/api/batches")) {
          return json([batch]);
        }
        if (
          request.method === "GET" &&
          request.url.includes(`/api/batches/${batch.file_version_id}`)
        ) {
          return json(makeDetail(batch));
        }
        throw new Error(`测试桩未覆盖的请求: ${request.method} ${request.url}`);
      }),
    );

    renderBatchesPage();

    await user.click(await screen.findByRole("button", { name: /2026-07.xlsx/ }));

    await waitFor(() => expect(screen.getByText(/员工: 张三/)).toBeInTheDocument());
    expect(screen.getByText("未解析")).toBeInTheDocument();
  });
});
