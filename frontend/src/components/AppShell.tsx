import { NavLink, Outlet, useNavigate } from "react-router";

import { hasPermission, PERMISSIONS, type CurrentUser, type PermissionKey } from "@/api/client";
import { useLogout } from "@/auth/useAuth";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

interface MenuItem {
  to: string;
  label: string;
  /** 需要的权限。缺少它则该菜单项不显示。 */
  permission: PermissionKey;
}

/**
 * 菜单由**权限**驱动，不是由角色 if-else 驱动。
 *
 * 后端的权限矩阵已经是数据（`ROLE_PERMISSIONS`），前端照抄一份
 * `if (role === "configurator")` 会立刻制造第二份事实来源——将来后端调整
 * 某个角色的权限，前端菜单会静默地不同步。这里只认 `/api/auth/me`
 * 返回的 `permissions`。
 */
const MENU: readonly MenuItem[] = [
  { to: "/batches", label: "批次", permission: PERMISSIONS.batchRead },
  { to: "/review", label: "复核台", permission: PERMISSIONS.reviewRead },
  { to: "/rules", label: "规则配置", permission: PERMISSIONS.configWrite },
  { to: "/health", label: "系统状态", permission: PERMISSIONS.batchRead },
];

const ROLE_LABELS: Record<CurrentUser["role"], string> = {
  auditor: "复核员",
  configurator: "配置管理员",
  viewer: "只读查看",
};

export function AppShell({ user }: { user: CurrentUser }) {
  const navigate = useNavigate();
  const logout = useLogout();
  const items = MENU.filter((item) => hasPermission(user, item.permission));

  return (
    <div className="min-h-screen bg-background">
      <header className="border-b">
        <div className="mx-auto flex h-14 max-w-7xl items-center gap-6 px-6">
          <span className="text-base font-semibold">ExpenseGuard</span>

          <nav className="flex items-center gap-1" aria-label="主导航">
            {items.map((item) => (
              <NavLink
                key={item.to}
                to={item.to}
                className={({ isActive }) =>
                  cn(
                    "rounded-md px-3 py-1.5 text-sm transition-colors",
                    isActive
                      ? "bg-muted font-medium text-foreground"
                      : "text-muted-foreground hover:text-foreground",
                  )
                }
              >
                {item.label}
              </NavLink>
            ))}
          </nav>

          <div className="ml-auto flex items-center gap-3">
            <Badge variant="secondary">{ROLE_LABELS[user.role]}</Badge>
            <Button
              variant="ghost"
              size="sm"
              disabled={logout.isPending}
              onClick={() => {
                logout.mutate(undefined, {
                  onSettled: () => void navigate("/login", { replace: true }),
                });
              }}
            >
              退出
            </Button>
          </div>
        </div>
      </header>

      <main className="mx-auto max-w-7xl px-6 py-8">
        <Outlet />
      </main>
    </div>
  );
}
