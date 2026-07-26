import { screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { Route, Routes } from "react-router";

import type { CurrentUser } from "@/api/client";
import { AppShell } from "@/components/AppShell";
import { makeUser, renderWithProviders } from "@/test/utils";

const CONFIGURATOR = makeUser({
  role: "configurator",
  permissions: [
    "batch:import",
    "batch:read",
    "report:read",
    "report:export",
    "review:read",
    "review:submit",
    "config:read",
    "config:write",
  ],
});

const VIEWER = makeUser({
  role: "viewer",
  permissions: ["batch:read", "report:read", "report:export"],
});

function renderShell(user: CurrentUser) {
  return renderWithProviders(
    <Routes>
      <Route element={<AppShell user={user} />}>
        <Route path="/health" element={<div>内容</div>} />
      </Route>
    </Routes>,
    { route: "/health" },
  );
}

describe("AppShell 的角色菜单", () => {
  it("configurator 能看到规则配置", () => {
    renderShell(CONFIGURATOR);
    expect(screen.getByRole("link", { name: "规则配置" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "复核台" })).toBeInTheDocument();
    expect(screen.getByText("配置管理员")).toBeInTheDocument();
  });

  it("auditor 看不到规则配置", () => {
    // auditor 有 config:read 但没有 config:write。菜单按**权限**过滤，
    // 所以少一个 config:write 就少一个入口——不需要在前端复述角色规则。
    renderShell(makeUser());
    expect(screen.queryByRole("link", { name: "规则配置" })).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "复核台" })).toBeInTheDocument();
  });

  it("viewer 只看到只读入口", () => {
    renderShell(VIEWER);
    expect(screen.getByRole("link", { name: "批次" })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "复核台" })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "规则配置" })).not.toBeInTheDocument();
    expect(screen.getByText("只读查看")).toBeInTheDocument();
  });
});
