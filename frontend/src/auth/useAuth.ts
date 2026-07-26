import { useMutation, useQuery, useQueryClient, type UseQueryResult } from "@tanstack/react-query";

import { api, type CurrentUser } from "@/api/client";

/** 当前用户查询的缓存键。登录/登出后据此失效。 */
export const CURRENT_USER_KEY = ["auth", "me"] as const;

/**
 * 读取当前登录用户。
 *
 * 身份的**唯一事实来源是服务端会话**，不是 localStorage 里的一份副本。
 * 会话是 HttpOnly cookie，JS 读不到，因此「我是谁」只能问 `/api/auth/me`。
 * 这同时消除了「本地缓存说已登录、服务端会话早已过期」这类状态撕裂。
 *
 * 401 不重试:未登录是**确定的答案**而非瞬时故障，重试三次只会拖慢跳转。
 */
export function useCurrentUser(): UseQueryResult<CurrentUser | null> {
  return useQuery({
    queryKey: CURRENT_USER_KEY,
    queryFn: async (): Promise<CurrentUser | null> => {
      const { data, response } = await api.GET("/api/auth/me");
      if (response.status === 401) return null;
      if (!data) throw new Error(`获取当前用户失败（HTTP ${response.status}）`);
      return data;
    },
    retry: false,
    staleTime: 30_000,
  });
}

export interface LoginInput {
  tenantSlug: string;
  username: string;
  password: string;
}

export function useLogin() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (input: LoginInput): Promise<CurrentUser> => {
      const { data, error, response } = await api.POST("/api/auth/login", {
        body: {
          tenant_slug: input.tenantSlug,
          username: input.username,
          password: input.password,
        },
      });
      if (!data) {
        // 后端对「用户不存在」与「密码错误」返回同一个错误，前端也
        // 原样透传——把它们区分开等于给撞库者送一个用户名枚举接口。
        throw new Error(extractErrorMessage(error) ?? `登录失败（HTTP ${response.status}）`);
      }
      return data;
    },
    onSuccess: (user) => {
      queryClient.setQueryData(CURRENT_USER_KEY, user);
    },
  });
}

export function useLogout() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (): Promise<void> => {
      await api.POST("/api/auth/logout");
    },
    // 用 onSettled 而非 onSuccess:即便登出请求失败（会话早已过期是最常见
    // 的原因），本地也必须丢弃身份——否则界面会停在一个已经不存在的会话上。
    onSettled: () => {
      queryClient.setQueryData(CURRENT_USER_KEY, null);
      void queryClient.invalidateQueries({ queryKey: CURRENT_USER_KEY });
    },
  });
}

/** 从后端错误响应里取出可展示的消息，取不到就返回 undefined。 */
function extractErrorMessage(error: unknown): string | undefined {
  if (typeof error !== "object" || error === null) return undefined;
  const detail = (error as { detail?: unknown }).detail;
  if (typeof detail === "string") return detail;
  if (typeof detail === "object" && detail !== null) {
    const message = (detail as { message?: unknown }).message;
    if (typeof message === "string") return message;
  }
  const message = (error as { message?: unknown }).message;
  return typeof message === "string" ? message : undefined;
}
