import type { ReactNode } from "react";
import { Navigate, useLocation } from "react-router";

import { useCurrentUser } from "./useAuth";

/**
 * 路由守卫:未登录则跳转到登录页，并记住原本想去的地址。
 *
 * ⚠️ 这是**体验**层的守卫，不是安全边界。绕过它只需要在控制台改一行状态；
 * 真正的访问控制在服务端每个端点上。前端守卫的作用是避免让未登录用户
 * 看到一个空壳页面然后被一堆 401 弹窗轰炸。
 *
 * 加载中必须渲染占位而不是直接跳转:首帧 `useCurrentUser` 尚未返回，
 * 此时跳转会让已登录用户每次刷新都被闪一下登录页。
 */
export function RequireAuth({ children }: { children: ReactNode }) {
  const location = useLocation();
  const { data: user, isPending, isError } = useCurrentUser();

  if (isPending) {
    return (
      <div className="flex min-h-screen items-center justify-center text-muted-foreground">
        正在校验会话…
      </div>
    );
  }

  if (isError || !user) {
    // state.from 让登录成功后能回到原本要去的页面
    return <Navigate to="/login" replace state={{ from: location.pathname }} />;
  }

  return <>{children}</>;
}
