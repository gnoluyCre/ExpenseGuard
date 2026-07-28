import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { components } from "@/api/schema";
import { CURRENT_USER_KEY } from "@/auth/useAuth";
import { RulesPage } from "@/pages/RulesPage";
import { createTestQueryClient, makeUser, renderWithProviders } from "@/test/utils";

type RuleVersion = components["schemas"]["RuleVersionResponse"];
type RuleDefinition = components["schemas"]["RuleDefinition"];

const DEFINITIONS: Record<components["schemas"]["RuleKind"], RuleDefinition> = {
  limit: {
    kind: "limit",
    schema_version: 1,
    enabled: true,
    require_direct: false,
    exemptions: [],
    thresholds: [{ expense_type: "差旅", currency: "CNY", max_amount: "800.00" }],
  },
  invoice_type: {
    kind: "invoice_type",
    schema_version: 1,
    enabled: true,
    require_direct: true,
    exemptions: [],
    allowances: [{ expense_type: "餐饮", allowed_invoice_types: ["增值税电子普通发票"] }],
  },
  timeliness: {
    kind: "timeliness",
    schema_version: 1,
    enabled: true,
    require_direct: false,
    exemptions: [],
    policies: [{ expense_type: "差旅", max_calendar_days: 30 }],
  },
  invoice_title: {
    kind: "invoice_title",
    schema_version: 1,
    enabled: true,
    require_direct: false,
    exemptions: [],
    allowed_titles: ["示例科技有限公司"],
  },
  invoice_duplicate: {
    kind: "invoice_duplicate",
    schema_version: 1,
    enabled: true,
    require_direct: false,
    exemptions: [],
  },
};

function makeVersion(
  kind: components["schemas"]["RuleKind"],
  version = 1,
  overrides: Partial<RuleVersion> = {},
): RuleVersion {
  return {
    id: `${String(version).padStart(8, "0")}-1111-4111-8111-111111111111`,
    rule_id: `${kind}-policy-${"very-long-name-".repeat(4)}`,
    version,
    effective_from: "2026-07-01",
    definition: DEFINITIONS[kind],
    config_fingerprint: `${kind}-${"a".repeat(80)}`,
    created_at: "2026-07-28T08:00:00Z",
    created_by: "00000000-0000-0000-0000-000000000001",
    ...overrides,
  };
}

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function renderRulesPage(permissions = ["config:read", "config:write"]) {
  const queryClient = createTestQueryClient();
  queryClient.setQueryData(CURRENT_USER_KEY, makeUser({ role: "configurator", permissions }));
  return renderWithProviders(<RulesPage />, { queryClient });
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("RulesPage", () => {
  it("按五类展示最新与历史版本，并允许 configWrite 用户创建强类型新版本", async () => {
    const user = userEvent.setup();
    const versions = [
      makeVersion("limit", 2),
      makeVersion("limit", 1),
      makeVersion("invoice_type"),
      makeVersion("timeliness"),
      makeVersion("invoice_title"),
      makeVersion("invoice_duplicate"),
    ];
    let savedBody: unknown;
    let getCount = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const request = input instanceof Request ? input : new Request(String(input), init);
        if (request.method === "GET" && new URL(request.url).pathname === "/api/rules") {
          getCount += 1;
          return json(versions);
        }
        if (request.method === "PUT" && new URL(request.url).pathname === "/api/rules") {
          savedBody = await request.json();
          return json({ ...makeVersion("limit", 3), reused_existing: false }, 201);
        }
        throw new Error(`未覆盖的请求：${request.method} ${request.url}`);
      }),
    );

    renderRulesPage();

    await screen.findByDisplayValue(/差旅 \| CNY \| 800.00/);
    expect(await screen.findAllByText("费用限额")).toHaveLength(2);
    expect(screen.getByText("票种合规")).toBeInTheDocument();
    expect(screen.getByText("报销时效")).toBeInTheDocument();
    expect(screen.getByText("发票抬头")).toBeInTheDocument();
    expect(screen.getByText("发票号查重")).toBeInTheDocument();
    expect(screen.getByText("2 个版本")).toBeInTheDocument();
    expect(screen.getAllByText("v2").length).toBeGreaterThanOrEqual(2);

    await user.clear(screen.getByLabelText("规则决策表"));
    await user.type(screen.getByLabelText("规则决策表"), "住宿 | CNY | 1200.00");
    await user.click(screen.getByRole("button", { name: "追加新版本" }));

    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("已创建"));
    expect(savedBody).toMatchObject({
      definition: {
        kind: "limit",
        thresholds: [{ expense_type: "住宿", currency: "CNY", max_amount: "1200.00" }],
      },
    });
    await waitFor(() => expect(getCount).toBeGreaterThan(1));
  });

  it("200 响应提示幂等复用，422 显示服务端校验错误", async () => {
    const user = userEvent.setup();
    const current = makeVersion("limit", 2);
    let saves = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const request = input instanceof Request ? input : new Request(String(input), init);
        if (request.method === "GET") return json([current]);
        saves += 1;
        if (saves === 1) return json({ ...current, reused_existing: true }, 200);
        return json({ error: { code: "RULE_CONFIG_INVALID", message: "限额必须大于零" } }, 422);
      }),
    );

    renderRulesPage();
    await screen.findByDisplayValue(/差旅 \| CNY \| 800.00/);
    await user.click(screen.getByRole("button", { name: "追加新版本" }));
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("配置未变化，已复用"));

    await user.click(screen.getByRole("button", { name: "追加新版本" }));
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("限额必须大于零"));
  });

  it("没有 configWrite 权限时只读并隐藏保存入口", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => json([makeVersion("limit")])),
    );
    renderRulesPage(["config:read"]);

    expect(await screen.findByText(/当前账号仅可查看规则版本/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "追加新版本" })).not.toBeInTheDocument();
    expect(screen.queryByLabelText("规则标识")).not.toBeInTheDocument();
  });
});
