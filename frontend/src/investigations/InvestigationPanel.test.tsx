import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { InvestigationPanel } from "@/investigations/InvestigationPanel";
import { makeUser, renderWithProviders } from "@/test/utils";

const DETECTION_ID = "11111111-1111-1111-1111-111111111111";
const FINDING_ID = "22222222-2222-2222-2222-222222222222";

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function capability(status: "enabled" | "unavailable") {
  return {
    status,
    reason_code: status === "enabled" ? "CONFIGURED_NOT_PROBED" : "API_KEY_MISSING",
    reason: status === "enabled" ? "配置就绪但未探测" : "模型凭据尚未配置",
    provider_kind: "openai_compatible",
    provider_model: status === "enabled" ? "model-a" : null,
    max_steps: 6,
    policy_retrieval_status: "enabled",
  };
}

function investigation(outcome: string, index: number) {
  const runId = `30000000-0000-0000-0000-${String(index).padStart(12, "0")}`;
  return {
    run: {
      id: runId,
      correlation_finding_id: FINDING_ID,
      detection_run_id: DETECTION_ID,
      file_version_id: "40000000-0000-0000-0000-000000000001",
      provider_kind: "openai_compatible",
      provider_model: "model-a",
      max_steps: 6,
      timeout_seconds: 30,
      redaction_version: "v1",
      agent_version: "react-v1",
      action_schema_version: 1,
      prompt_template_version: "prompt-v1",
      created_at: `2026-08-10T00:00:0${index}Z`,
    },
    steps: [
      {
        id: `50000000-0000-0000-0000-${String(index).padStart(12, "0")}`,
        investigation_run_id: runId,
        step_no: 1,
        action_kind: "terminate",
        model_action: { kind: "terminate", untrusted: "<img src=x onerror=alert(1)>" },
        decision_summary: "<img src=x onerror=alert(1)>",
        tool_name: null,
        tool_input: null,
        tool_output: null,
        created_at: `2026-08-10T00:00:0${index}Z`,
      },
    ],
    result: {
      id: `60000000-0000-0000-0000-${String(index).padStart(12, "0")}`,
      investigation_run_id: runId,
      outcome,
      evidence_sufficient:
        outcome === "sufficient" ? true : outcome === "insufficient" ? false : null,
      summary: `结果 ${outcome}`,
      reason_code: outcome.toUpperCase(),
      citations: [],
      completed_at: `2026-08-10T00:00:1${index}Z`,
    },
    reused_existing: false,
  };
}

afterEach(() => vi.unstubAllGlobals());

describe("InvestigationPanel", () => {
  it("展示不可用原因，viewer 不渲染任何可触发 POST 的控件", async () => {
    const fetcher = vi.fn(async (input: RequestInfo | URL) => {
      const path = new URL(input instanceof Request ? input.url : String(input)).pathname;
      if (path.endsWith("/capability")) return json(capability("unavailable"));
      return json({ items: [], total: 0, limit: 20, offset: 0 });
    });
    vi.stubGlobal("fetch", fetcher);

    renderWithProviders(
      <InvestigationPanel
        detectionRunId={DETECTION_ID}
        findingId={FINDING_ID}
        user={makeUser({ role: "viewer", permissions: ["batch:read"] })}
      />,
    );

    expect(await screen.findByText("API_KEY_MISSING", { exact: false })).toBeInTheDocument();
    expect(screen.getByText("尚无调查记录")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "启动取证" })).not.toBeInTheDocument();
    expect(fetcher.mock.calls.every((call) => new Request(call[0]).method === "GET")).toBe(true);
  });

  it("展示五种终态与恶意文本，但不创建非模块图片节点", async () => {
    const items = ["sufficient", "insufficient", "unavailable", "max_steps", "failed"].map(
      investigation,
    );
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const path = new URL(input instanceof Request ? input.url : String(input)).pathname;
        if (path.endsWith("/capability")) return json(capability("enabled"));
        return json({ items, total: items.length, limit: 20, offset: 0 });
      }),
    );

    const { container } = renderWithProviders(
      <InvestigationPanel detectionRunId={DETECTION_ID} findingId={FINDING_ID} user={makeUser()} />,
    );

    for (const label of ["证据充分", "证据不足", "能力不可用", "达到步数上限", "调查失败"])
      expect((await screen.findAllByText(label)).length).toBeGreaterThan(0);
    expect(screen.getByText("<img src=x onerror=alert(1)>")).toBeInTheDocument();
    expect(container.querySelectorAll("img")).toHaveLength(0);
    expect(screen.getByRole("button", { name: "启动取证" })).toBeInTheDocument();
  });

  it("空状态可启动；409 冲突作为显式错误展示", async () => {
    vi.stubGlobal(
      "confirm",
      vi.fn(() => true),
    );
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const request = input instanceof Request ? input : new Request(String(input), init);
        if (request.method === "POST")
          return json(
            { error: { code: "IDEMPOTENCY_KEY_REUSED", message: "请求键已绑定其他调查" } },
            409,
          );
        if (new URL(request.url).pathname.endsWith("/capability"))
          return json(capability("enabled"));
        return json({ items: [], total: 0, limit: 20, offset: 0 });
      }),
    );
    renderWithProviders(
      <InvestigationPanel detectionRunId={DETECTION_ID} findingId={FINDING_ID} user={makeUser()} />,
    );
    await screen.findByText("尚无调查记录");
    await userEvent.click(screen.getByRole("button", { name: "启动取证" }));
    await waitFor(() => expect(screen.getByText("请求键已绑定其他调查")).toBeInTheDocument());
  });

  it("读取失败进入稳定错误态", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => json({ error: { code: "FAILED", message: "调查历史读取失败" } }, 500)),
    );
    renderWithProviders(
      <InvestigationPanel detectionRunId={DETECTION_ID} findingId={FINDING_ID} user={makeUser()} />,
    );
    expect((await screen.findAllByRole("alert")).length).toBeGreaterThan(0);
    expect((await screen.findAllByText("调查历史读取失败")).length).toBeGreaterThan(0);
  });
});
