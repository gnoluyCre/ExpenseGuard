import createClient from "openapi-fetch";

import type { components, paths } from "./schema";

/**
 * 类型化 API 客户端。
 *
 * 类型全部来自 `schema.d.ts`，而它由后端的 `openapi.json` 生成
 * （`npm run gen:api`）。因此后端改了字段、前端没重新生成时，
 * **CI 的 contract job 会红**，而不是等运行时拿到 undefined 才发现。
 *
 * 选 openapi-fetch 而非 openapi-typescript-codegen 的理由:后者只支持
 * OpenAPI 3.0，而 FastAPI + Pydantic v2 输出的是 **3.1**，第一天就会解析失败。
 */
/**
 * 请求基址。
 *
 * ⚠️ **不要**在这里拼 `/api` —— OpenAPI 里的路径本身就是 `/api/auth/login`，
 * 再加一次前缀会得到 `/api/api/auth/login`。
 *
 * 默认走**同源**（开发期由 Vite proxy 转发到后端，生产由反向代理托管），
 * 因此 baseUrl 取当前 origin 而不是空串:openapi-fetch 内部要 `new Request(url)`，
 * 而 Node/undici 的 Request 不接受相对 URL——空串会让所有测试直接报
 * "Failed to parse URL"。前后端分开部署时用 `VITE_API_BASE_URL` 覆盖。
 */
export const apiBaseUrl =
  import.meta.env.VITE_API_BASE_URL ||
  (typeof window === "undefined" ? "http://localhost" : window.location.origin);

export const api = createClient<paths>({
  baseUrl: apiBaseUrl,
  // 会话 cookie 是 HttpOnly 的，不带这个选项浏览器根本不会发它。
  // 开发期靠 Vite proxy 做到同源，所以这里不需要处理跨域。
  credentials: "include",
  // 延迟解析 fetch，而不是让 createClient 在模块导入时把 globalThis.fetch
  // 抓走。抓走的后果很隐蔽:测试里 `vi.stubGlobal("fetch", ...)` 完全不生效，
  // 请求会真的发出去、真的失败，于是「未登录应跳转」这类断言**因为网络
  // 错误而通过**——测试全绿，测的却不是它声称的东西。
  fetch: (request) => globalThis.fetch(request),
});

/** 当前登录用户。字段与后端 `CurrentUser` 模型同源，不重复定义。 */
export type CurrentUser = components["schemas"]["CurrentUser"];

/** 角色。取值域由后端枚举决定。 */
export type Role = components["schemas"]["Role"];

/** 就绪探针响应。 */
export type ReadinessResponse = components["schemas"]["ReadinessResponse"];

/** 批次列表项。 */
export type BatchSummary = components["schemas"]["BatchSummaryResponse"];

/** 批次导入响应。 */
export type BatchImportResponse = components["schemas"]["BatchImportResponse"];

/** 批次详情。 */
export type BatchDetail = components["schemas"]["BatchDetailResponse"];

/**
 * 后端权限标识。
 *
 * ⚠️ 前端的权限判断只用于**决定菜单和按钮是否显示**，它是体验而非安全边界。
 * 真正的鉴权在服务端每个端点上（`require_permission` 依赖）。任何把
 * 「前端没显示这个按钮」当作访问控制的写法都是错的。
 */
export const PERMISSIONS = {
  batchImport: "batch:import",
  batchRead: "batch:read",
  reportRead: "report:read",
  reportExport: "report:export",
  reviewRead: "review:read",
  reviewSubmit: "review:submit",
  configRead: "config:read",
  configWrite: "config:write",
} as const;

export type PermissionKey = (typeof PERMISSIONS)[keyof typeof PERMISSIONS];

export function hasPermission(user: CurrentUser, permission: PermissionKey): boolean {
  return user.permissions.includes(permission);
}
