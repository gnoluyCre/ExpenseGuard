import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Route, Routes } from "react-router";

import { LoginPage } from "@/pages/LoginPage";
import { makeUser, renderWithProviders } from "@/test/utils";

interface StubCall {
  url: string;
  credentials: RequestCredentials;
  body: string;
}

/**
 * 按 URL 路由的 fetch 桩，并记录每次调用供断言。
 *
 * openapi-fetch 传给 fetch 的是一个 **Request 对象**而非 (url, init) 二元组，
 * 所以这里必须从 Request 上取 url / credentials / body。当成字符串处理会得到
 * `"[object Request]"`，匹配不上任何 handler——而且症状是「登录失败」，
 * 很容易被误判成业务代码的 bug。
 */
function stubApi(handlers: Record<string, () => Response>) {
  const calls: StubCall[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const request = input instanceof Request ? input : new Request(String(input));
      calls.push({
        url: request.url,
        credentials: request.credentials,
        body: await request.clone().text(),
      });
      const handler = Object.entries(handlers).find(([path]) => request.url.includes(path))?.[1];
      if (!handler) throw new Error(`测试桩未覆盖的请求: ${request.url}`);
      return handler();
    }),
  );
  return calls;
}

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function renderLogin() {
  return renderWithProviders(
    <Routes>
      <Route path="/login" element={<LoginPage />} />
      <Route path="/health" element={<div>系统状态页</div>} />
    </Routes>,
    { route: "/login" },
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("LoginPage", () => {
  it("登录成功后跳转到受保护页", async () => {
    const user = userEvent.setup();
    const calls = stubApi({
      "/api/auth/me": () => json({ detail: "未登录" }, 401),
      "/api/auth/login": () => json(makeUser()),
    });

    renderLogin();

    await user.type(screen.getByLabelText("用户名"), "alice");
    await user.type(screen.getByLabelText("密码"), "correct-horse");
    await user.click(screen.getByRole("button", { name: "登录" }));

    await waitFor(() => expect(screen.getByText("系统状态页")).toBeInTheDocument());

    const loginCall = calls.find((call) => call.url.includes("/api/auth/login"));
    expect(loginCall).toBeDefined();
    // 会话 cookie 是 HttpOnly 的，不带 credentials 浏览器根本不会回传它。
    // 这条断言守的就是「有人顺手把 credentials 删了」这种回归。
    expect(loginCall?.credentials).toBe("include");
    expect(JSON.parse(String(loginCall?.body))).toEqual({
      tenant_slug: "default",
      username: "alice",
      password: "correct-horse",
    });
  });

  it("登录失败时显示错误且不跳转", async () => {
    const user = userEvent.setup();
    stubApi({
      "/api/auth/me": () => json({ detail: "未登录" }, 401),
      "/api/auth/login": () => json({ detail: { message: "用户名或密码错误" } }, 401),
    });

    renderLogin();

    await user.type(screen.getByLabelText("用户名"), "alice");
    await user.type(screen.getByLabelText("密码"), "wrong");
    await user.click(screen.getByRole("button", { name: "登录" }));

    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("用户名或密码错误"));
    expect(screen.queryByText("系统状态页")).not.toBeInTheDocument();
  });
});
