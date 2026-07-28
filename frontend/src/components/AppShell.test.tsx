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

describe("AppShell 的权限菜单", () => {
  it("configurator 能看到规则配置", () => {
    renderShell(CONFIGURATOR);
    expect(screen.getByRole("link", { name: "规则配置" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "复核台" })).toBeInTheDocument();
    expect(screen.getByText("配置管理员")).toBeInTheDocument();
  });

  it("有 config:read 的 auditor 能查看规则配置", () => {
    renderShell(makeUser());
    expect(screen.getByRole("link", { name: "规则配置" })).toBeInTheDocument();
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
