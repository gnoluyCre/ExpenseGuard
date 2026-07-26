import { screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Route, Routes } from "react-router";

import { RequireAuth } from "@/auth/RequireAuth";
import { renderWithProviders, makeUser } from "@/test/utils";

/** 用 fetch 桩模拟 `/api/auth/me` 的响应。 */
function stubMe(status: number, body?: unknown) {
  const fetchStub = vi.fn(
    async () =>
      new Response(body === undefined ? null : JSON.stringify(body), {
        status,
        headers: { "content-type": "application/json" },
      }),
  );
  vi.stubGlobal("fetch", fetchStub);
  return fetchStub;
}

function renderGuarded(route = "/health") {
  return renderWithProviders(
    <Routes>
      <Route path="/login" element={<div>登录页</div>} />
      <Route
        path="/health"
        element={
          <RequireAuth>
            <div>受保护内容</div>
          </RequireAuth>
        }
      />
    </Routes>,
    { route },
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("RequireAuth", () => {
  it("未登录时跳转到登录页", async () => {
    stubMe(401);
    renderGuarded();

    await waitFor(() => expect(screen.getByText("登录页")).toBeInTheDocument());
    expect(screen.queryByText("受保护内容")).not.toBeInTheDocument();
  });

  it("已登录时渲染受保护内容", async () => {
    stubMe(200, makeUser());
    renderGuarded();

    await waitFor(() => expect(screen.getByText("受保护内容")).toBeInTheDocument());
    expect(screen.queryByText("登录页")).not.toBeInTheDocument();
  });

  it("会话校验期间既不放行也不跳转", () => {
    // 永不 resolve 的 fetch = 停在 pending 状态
    vi.stubGlobal(
      "fetch",
      vi.fn(() => new Promise<Response>(() => {})),
    );
    renderGuarded();

    expect(screen.getByText(/正在校验会话/)).toBeInTheDocument();
    // 这一条是防「刷新时闪一下登录页」的回归:pending 期间跳转会让
    // 已登录用户每次刷新都看到登录页一闪而过
    expect(screen.queryByText("登录页")).not.toBeInTheDocument();
    expect(screen.queryByText("受保护内容")).not.toBeInTheDocument();
  });
});
