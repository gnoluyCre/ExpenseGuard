import { Navigate, Route, Routes } from "react-router";

import { RequireAuth } from "@/auth/RequireAuth";
import { useCurrentUser } from "@/auth/useAuth";
import { AppShell } from "@/components/AppShell";
import { BatchesPage } from "@/pages/BatchesPage";
import { HealthPage } from "@/pages/HealthPage";
import { LoginPage } from "@/pages/LoginPage";
import { PlaceholderPage } from "@/pages/PlaceholderPage";

/** 受保护区域的外壳。`RequireAuth` 已保证此处 user 必然存在。 */
function ProtectedLayout() {
  const { data: user } = useCurrentUser();
  if (!user) return null;
  return <AppShell user={user} />;
}

export function App() {
  return (
    <Routes>
      <Route path="/login" element={<LoginPage />} />

      <Route
        element={
          <RequireAuth>
            <ProtectedLayout />
          </RequireAuth>
        }
      >
        <Route index element={<Navigate to="/health" replace />} />
        <Route path="health" element={<HealthPage />} />
        <Route path="batches" element={<BatchesPage />} />
        <Route
          path="review"
          element={<PlaceholderPage title="复核台" feature="F5 · 人工复核台" />}
        />
        <Route
          path="rules"
          element={<PlaceholderPage title="规则配置" feature="F3 · 确定性校验" />}
        />
        {/* 兜底也放在受保护区内:未登录访问任意未知路径应先走登录，
            而不是先看到 404 再被踢走 */}
        <Route path="*" element={<Navigate to="/health" replace />} />
      </Route>
    </Routes>
  );
}
