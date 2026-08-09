import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { CURRENT_USER_KEY } from "@/auth/useAuth";
import { DetectionConfigPage } from "@/pages/DetectionConfigPage";
import { createTestQueryClient, makeUser, renderWithProviders } from "@/test/utils";

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function renderPage(canWrite = true) {
  const queryClient = createTestQueryClient();
  queryClient.setQueryData(
    CURRENT_USER_KEY,
    makeUser({ permissions: canWrite ? ["config:read", "config:write"] : ["config:read"] }),
  );
  return renderWithProviders(<DetectionConfigPage />, { queryClient });
}

afterEach(() => vi.unstubAllGlobals());

describe("DetectionConfigPage", () => {
  it("配置缺失时展示四类 detector，并经确认追加 v1", async () => {
    const user = userEvent.setup();
    let submitted: unknown;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const request = input instanceof Request ? input : new Request(String(input), init);
        if (request.method === "GET")
          return json({ current: null, history: [], total: 0, limit: 50, offset: 0 });
        submitted = await request.json();
        const body = submitted as { definition: unknown };
        return json(
          {
            id: "11111111-1111-1111-1111-111111111111",
            version: 1,
            definition: body.definition,
            config_fingerprint: "a".repeat(64),
            algorithm_bundle_version: "correlation-v1",
            created_by: "22222222-2222-2222-2222-222222222222",
            created_at: "2026-08-01T00:00:00Z",
            change_reason: "初始化",
            reused_existing: false,
          },
          201,
        );
      }),
    );

    renderPage();
    expect(await screen.findByText("未配置")).toBeInTheDocument();
    expect(screen.getByText("拆单候选")).toBeInTheDocument();
    expect(screen.getByText("时空冲突 Tier 0")).toBeInTheDocument();
    await user.type(screen.getByLabelText("变更原因"), "初始化");
    await user.click(screen.getByLabelText("确认历史不可变"));
    await user.click(screen.getByRole("button", { name: "追加不可变版本" }));
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("已创建配置 v1"));
    expect(submitted).toMatchObject({ expected_current_version: 0, change_reason: "初始化" });
  });

  it("只读账号没有保存表单", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => json({ current: null, history: [], total: 0, limit: 50, offset: 0 })),
    );
    renderPage(false);
    expect(await screen.findByText(/只可读取 profile/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "追加不可变版本" })).not.toBeInTheDocument();
  });
});
