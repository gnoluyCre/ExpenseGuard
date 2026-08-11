import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { CURRENT_USER_KEY } from "@/auth/useAuth";
import { generateDispositionMatrix, gradingConfigSchema } from "@/grading/configSchemas";
import { GradingConfigPage } from "@/pages/GradingConfigPage";
import { createTestQueryClient, makeUser, renderWithProviders } from "@/test/utils";

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

const costs = {
  impact_cost_units: [0, 10, 50, 200] as const,
  confidence_issue_probability_bps: [0, 1000, 5000, 9000] as const,
  false_negative_multiplier_bps: 10_000,
  false_positive_cost_units: 5,
  manual_review_cost_units: 10,
};

const definition = gradingConfigSchema.parse({
  schema_version: 1,
  algorithm_version: "cost-matrix-v1",
  impact_by_rule_kind: {
    limit: 2,
    invoice_type: 1,
    timeliness: 1,
    invoice_title: 2,
    invoice_duplicate: 3,
  },
  impact_by_detector: {
    split_invoice: 2,
    sequential_invoice: 2,
    frequency_anomaly: 1,
    spatiotemporal_tier0: 3,
  },
  confidence_by_investigation_outcome: {
    sufficient: 3,
    insufficient: 1,
    unavailable: 0,
    max_steps: 1,
    failed: 0,
    not_run: 0,
  },
  capability_confidence_cap: { enabled: 3, degraded: 2, unavailable: 0 },
  ...costs,
  disposition_matrix: generateDispositionMatrix(costs),
});

function config(version = 1, reusedExisting = false) {
  return {
    id: `11111111-1111-1111-1111-${String(version).padStart(12, "0")}`,
    tenant_id: "22222222-2222-2222-2222-222222222222",
    version,
    schema_version: 1,
    algorithm_version: "cost-matrix-v1",
    definition,
    config_fingerprint: String(version).repeat(64),
    created_by: "33333333-3333-3333-3333-333333333333",
    created_at: "2026-08-10T00:00:00Z",
    change_reason: version === 1 ? "初始化" : "调整成本",
    reused_existing: reusedExisting,
  };
}

function renderPage(canWrite = true) {
  const queryClient = createTestQueryClient();
  queryClient.setQueryData(
    CURRENT_USER_KEY,
    makeUser({ permissions: canWrite ? ["config:read", "config:write"] : ["config:read"] }),
  );
  return renderWithProviders(<GradingConfigPage />, { queryClient });
}

function stubConfigApi(options?: {
  current?: ReturnType<typeof config> | null;
  history?: ReturnType<typeof config>[];
  post?: Response;
  capture?: (request: Request) => Promise<void>;
}) {
  const current = options?.current ?? null;
  const history = options?.history ?? (current ? [current] : []);
  return vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const request = input instanceof Request ? input : new Request(String(input), init);
    if (request.method === "GET" && request.url.endsWith("/current")) {
      return current
        ? json(current)
        : json({ error: { code: "GRADING_CONFIG_NOT_FOUND", message: "未配置" } }, 404);
    }
    if (request.method === "GET") {
      return json({ items: history, total: history.length, limit: 20, offset: 0 });
    }
    await options?.capture?.(request);
    return options?.post ?? json(config(1), 201);
  });
}

async function fillDefinition(user: ReturnType<typeof userEvent.setup>): Promise<void> {
  const levels: Record<string, string> = {
    限额: "2",
    票种: "1",
    时效: "1",
    抬头: "2",
    发票号查重: "3",
    拆单候选: "2",
    发票连号: "2",
    频次离群: "1",
    "时空冲突 Tier 0": "3",
    证据充分: "3",
    证据不足: "1",
    "F7 能力不可用": "0",
    达到步数上限: "1",
    执行失败: "0",
    能力正常: "3",
    能力降级: "2",
  };
  for (const [label, value] of Object.entries(levels)) {
    const element = screen.queryByLabelText(label);
    if (element && !(element as HTMLSelectElement).disabled)
      await user.selectOptions(element, value);
  }

  const numbers: Record<string, string> = {
    "影响成本（level 0–3） L0": "0",
    "影响成本（level 0–3） L1": "10",
    "影响成本（level 0–3） L2": "50",
    "影响成本（level 0–3） L3": "200",
    "问题概率 bps（confidence 0–3） L0": "0",
    "问题概率 bps（confidence 0–3） L1": "1000",
    "问题概率 bps（confidence 0–3） L2": "5000",
    "问题概率 bps（confidence 0–3） L3": "9000",
    "漏判倍率 bps": "10000",
    误报成本单位: "5",
    人工复核成本单位: "10",
  };
  for (const [label, value] of Object.entries(numbers)) {
    await user.type(screen.getByLabelText(label), value);
  }
}

afterEach(() => vi.unstubAllGlobals());

describe("GradingConfigPage", () => {
  it("缺少 current 时不填业务默认，完整显式输入后创建 v1", async () => {
    const user = userEvent.setup();
    let submitted: unknown;
    let idempotencyKey: string | null = null;
    vi.stubGlobal(
      "fetch",
      stubConfigApi({
        capture: async (request) => {
          submitted = await request.json();
          idempotencyKey = request.headers.get("Idempotency-Key");
        },
      }),
    );

    renderPage();
    expect(await screen.findByText("尚无 current 配置")).toBeInTheDocument();
    expect(screen.getByLabelText("限额")).toHaveValue("");
    expect(screen.getByRole("button", { name: "追加不可变版本" })).toBeDisabled();

    await fillDefinition(user);
    expect(screen.getByLabelText("16格处置矩阵")).toBeInTheDocument();
    await user.type(screen.getByLabelText("变更原因"), "初始化二维分级");
    await user.click(screen.getByRole("button", { name: "追加不可变版本" }));

    await waitFor(() =>
      expect(screen.getByRole("status")).toHaveTextContent("已追加不可变配置 v1"),
    );
    expect(submitted).toMatchObject({
      expected_current_version: 0,
      change_reason: "初始化二维分级",
    });
    expect(idempotencyKey).toMatch(/^grading-config-/);
  });

  it("配置不完整或无效时禁止 POST", async () => {
    const user = userEvent.setup();
    const fetchMock = stubConfigApi();
    vi.stubGlobal("fetch", fetchMock);
    renderPage();
    await screen.findByText("尚无 current 配置");
    await user.type(screen.getByLabelText("变更原因"), "不完整草案");
    expect(screen.getByRole("button", { name: "追加不可变版本" })).toBeDisabled();
    expect(
      fetchMock.mock.calls.filter(([input]) => new Request(input).method === "POST"),
    ).toHaveLength(0);
  });

  it("只读账号看到 current 与 archived 历史，但没有写入口", async () => {
    vi.stubGlobal("fetch", stubConfigApi({ current: config(2), history: [config(2), config(1)] }));
    renderPage(false);
    expect((await screen.findAllByText("v2")).length).toBeGreaterThan(0);
    expect(screen.getByText("current")).toBeInTheDocument();
    expect(screen.getByText("archived")).toBeInTheDocument();
    expect(screen.getByText(/当前账号为只读视图/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "追加不可变版本" })).not.toBeInTheDocument();
  });

  it("以 current 为起点后显示幂等重放结果", async () => {
    const user = userEvent.setup();
    vi.stubGlobal("fetch", stubConfigApi({ current: config(1), post: json(config(1, true), 200) }));
    renderPage();
    await user.click(await screen.findByRole("button", { name: /以 current v1 为起点/ }));
    await user.type(screen.getByLabelText("变更原因"), "相同配置重放");
    await user.click(screen.getByRole("button", { name: "追加不可变版本" }));
    await waitFor(() =>
      expect(screen.getByRole("status")).toHaveTextContent("幂等重放：已复用配置 v1"),
    );
  });

  it("CAS 冲突时要求刷新，不把失败伪装成保存成功", async () => {
    const user = userEvent.setup();
    vi.stubGlobal(
      "fetch",
      stubConfigApi({
        current: config(1),
        post: json(
          { error: { code: "GRADING_CONFIG_CONFLICT", message: "version mismatch" } },
          409,
        ),
      }),
    );
    renderPage();
    await user.click(await screen.findByRole("button", { name: /以 current v1 为起点/ }));
    await user.type(screen.getByLabelText("变更原因"), "并发更新");
    await user.click(screen.getByRole("button", { name: "追加不可变版本" }));
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("版本冲突"));
  });
});
