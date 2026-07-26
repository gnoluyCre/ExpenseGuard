import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, type RenderResult } from "@testing-library/react";
import type { ReactNode } from "react";
import { MemoryRouter } from "react-router";

import type { CurrentUser } from "@/api/client";

/**
 * 每个测试都要**新建** QueryClient。
 *
 * 共用一个实例时，上一个测试写进缓存的用户会被下一个测试读到，
 * 于是「未登录应跳转」这类断言会因为残留缓存而静默通过。
 * 关掉 retry 同理:重试会让失败路径的断言等到超时才成立。
 */
export function createTestQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: { retry: false, gcTime: 0, staleTime: 0 },
      mutations: { retry: false },
    },
  });
}

export function renderWithProviders(
  ui: ReactNode,
  options: { route?: string; queryClient?: QueryClient } = {},
): RenderResult & { queryClient: QueryClient } {
  const queryClient = options.queryClient ?? createTestQueryClient();
  const result = render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[options.route ?? "/"]}>{ui}</MemoryRouter>
    </QueryClientProvider>,
  );
  return { ...result, queryClient };
}

/** 构造一个测试用户。默认 auditor，按需覆盖字段。 */
export function makeUser(overrides: Partial<CurrentUser> = {}): CurrentUser {
  return {
    user_id: "00000000-0000-0000-0000-000000000001",
    tenant_id: "00000000-0000-0000-0000-0000000000ff",
    role: "auditor",
    permissions: [
      "batch:import",
      "batch:read",
      "report:read",
      "report:export",
      "review:read",
      "review:submit",
      "config:read",
    ],
    ...overrides,
  };
}
