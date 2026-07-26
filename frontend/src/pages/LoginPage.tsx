import { useState, type FormEvent } from "react";
import { Navigate, useLocation, useNavigate } from "react-router";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { useCurrentUser, useLogin } from "@/auth/useAuth";

interface LocationState {
  from?: string;
}

export function LoginPage() {
  const navigate = useNavigate();
  const location = useLocation();
  const login = useLogin();
  const { data: user, isPending: isCheckingSession } = useCurrentUser();

  const [tenantSlug, setTenantSlug] = useState("default");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");

  // 已登录还停在登录页时直接送回去，避免出现「两个登录态」的错觉
  if (!isCheckingSession && user) {
    return <Navigate to="/health" replace />;
  }

  const from = (location.state as LocationState | null)?.from ?? "/health";

  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    login.mutate(
      { tenantSlug, username, password },
      { onSuccess: () => void navigate(from, { replace: true }) },
    );
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-muted/30 px-6">
      <Card className="w-full max-w-sm">
        <CardHeader>
          <CardTitle>登录 ExpenseGuard</CardTitle>
          <CardDescription>费用报销预审系统 · 内部财务团队</CardDescription>
        </CardHeader>
        <CardContent>
          <form onSubmit={handleSubmit} className="space-y-4">
            <div className="space-y-1.5">
              <label htmlFor="tenant" className="text-sm font-medium">
                租户
              </label>
              <Input
                id="tenant"
                value={tenantSlug}
                onChange={(event) => setTenantSlug(event.target.value)}
                autoComplete="organization"
                required
              />
            </div>

            <div className="space-y-1.5">
              <label htmlFor="username" className="text-sm font-medium">
                用户名
              </label>
              <Input
                id="username"
                value={username}
                onChange={(event) => setUsername(event.target.value)}
                autoComplete="username"
                required
              />
            </div>

            <div className="space-y-1.5">
              <label htmlFor="password" className="text-sm font-medium">
                密码
              </label>
              <Input
                id="password"
                type="password"
                value={password}
                onChange={(event) => setPassword(event.target.value)}
                autoComplete="current-password"
                required
              />
            </div>

            {login.isError && (
              // role="alert" 让屏幕阅读器立刻播报，而不是等用户主动移动焦点
              <p role="alert" className="text-sm text-destructive">
                {login.error.message}
              </p>
            )}

            <Button type="submit" className="w-full" disabled={login.isPending}>
              {login.isPending ? "登录中…" : "登录"}
            </Button>
          </form>
        </CardContent>
      </Card>
    </div>
  );
}
