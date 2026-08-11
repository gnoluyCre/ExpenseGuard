import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { BatchGrading, GradingItem } from "@/api/client";
import { BatchGradingView } from "@/grading/BatchGradingView";
import { makeUser, renderWithProviders } from "@/test/utils";

const FILE_ID = "11111111-1111-1111-1111-111111111111";
const VALIDATION_ID = "22222222-2222-2222-2222-222222222222";
const DETECTION_ID = "33333333-3333-3333-3333-333333333333";
const CONFIG_ID = "44444444-4444-4444-4444-444444444444";
const GRADING_ID = "55555555-5555-5555-5555-555555555555";
const ITEM_ID = "66666666-6666-6666-6666-666666666666";
const FINDING_A = "77777777-7777-7777-7777-777777777777";
const FINDING_B = "88888888-8888-8888-8888-888888888888";

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function validation() {
  return {
    file_version_id: FILE_ID,
    validation_run_id: VALIDATION_ID,
    mapping_version_id: "99999999-9999-9999-9999-999999999999",
    ruleset_fingerprint: "a".repeat(64),
    total_row_count: 2,
    evaluated_row_count: 2,
    passed_count: 1,
    flagged_count: 1,
    manual_review_count: 0,
    parse_failed_count: 0,
    reused_existing: false,
  };
}

function capability(detector: string, status: "enabled" | "degraded" | "unavailable") {
  return {
    detector,
    detector_version: `${detector}-v1`,
    status,
    reason: `${detector} ${status}`,
    details: {
      source_row_count: 2,
      eligible_row_count: status === "unavailable" ? 0 : 2,
      excluded_row_count: status === "unavailable" ? 2 : 0,
      configured: true,
      runtime: null,
    },
    finding_count: detector === "split_invoice" ? 2 : 0,
  };
}

function detection(options: { run?: boolean; degraded?: boolean } = {}) {
  return {
    run:
      options.run === false
        ? null
        : {
            id: DETECTION_ID,
            file_version_id: FILE_ID,
            detection_config_id: "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            config_version: 1,
            config_fingerprint: "b".repeat(64),
            algorithm_bundle_version: "correlation-v1",
            input_fingerprint: "c".repeat(64),
            run_fingerprint: "d".repeat(64),
            source_row_count: 2,
            parsed_row_count: 2,
            error_row_count: 0,
            finding_count: 2,
            created_by: "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
            created_at: "2026-08-10T00:00:00Z",
            completed_at: "2026-08-10T00:00:01Z",
            reused_existing: false,
          },
    capabilities: [
      capability("split_invoice", options.degraded ? "degraded" : "enabled"),
      capability("sequential_invoice", options.degraded ? "unavailable" : "enabled"),
      capability("frequency_anomaly", "enabled"),
      capability("spatiotemporal_tier0", "enabled"),
    ],
    current_config_fingerprint: "b".repeat(64),
    config_stale: false,
  };
}

function gradingRun(reused = false) {
  return {
    id: GRADING_ID,
    tenant_id: "cccccccc-cccc-cccc-cccc-cccccccccccc",
    file_version_id: FILE_ID,
    validation_run_id: VALIDATION_ID,
    detection_run_id: DETECTION_ID,
    grading_config_id: CONFIG_ID,
    config_version: 1,
    config_fingerprint: "e".repeat(64),
    algorithm_version: "cost-matrix-v1",
    input_fingerprint: "f".repeat(64),
    f3_manifest_fingerprint: "1".repeat(64),
    f6_manifest_fingerprint: "2".repeat(64),
    f7_manifest_fingerprint: "3".repeat(64),
    deterministic_item_count: 1,
    correlation_item_count: 1,
    high_attention_count: 1,
    manual_attention_count: 1,
    cleared_count: 0,
    created_by: "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
    created_at: "2026-08-10T00:00:00Z",
    completed_at: "2026-08-10T00:00:01Z",
    reused_existing: reused,
  };
}

function gradingSnapshot(
  options: { run?: boolean; config?: boolean; stale?: boolean } = {},
): BatchGrading {
  return {
    file_version_id: FILE_ID,
    run: options.run === false ? null : gradingRun(),
    current_config_id: options.config === false ? null : CONFIG_ID,
    current_validation_run_id: VALIDATION_ID,
    current_detection_run_id: DETECTION_ID,
    config_stale: options.stale ?? false,
    validation_run_stale: options.stale ?? false,
    detection_run_stale: options.stale ?? false,
  };
}

const item = {
  id: ITEM_ID,
  tenant_id: "cccccccc-cccc-cccc-cccc-cccccccccccc",
  grading_run_id: GRADING_ID,
  file_version_id: FILE_ID,
  source_kind: "correlation",
  finding_id: null,
  correlation_finding_id: FINDING_A,
  rule_kind: null,
  detector: "split_invoice",
  f3_outcome: null,
  f6_capability_status: "degraded",
  f7_outcome: "not_run",
  investigation_run_id: null,
  severity_impact: 3,
  severity_confidence: 1,
  disposition: "manual_attention",
  evidence_snapshot: {
    source_kind: "correlation",
    rule_kind: null,
    detector: "split_invoice",
    source_outcome: "not_run",
    mapped_impact: 3,
    capability_status: "degraded",
    confidence_before_cap: 1,
    confidence_after_cap: 1,
    losses: { clear_loss: 30, review_loss: 5, flag_loss: 9 },
    matrix_disposition: "manual_attention",
  },
  reason_codes: ["IMPACT_DETECTOR_MAPPING", "CONFIDENCE_F7_NOT_RUN", "CAPABILITY_DEGRADED"],
  item_fingerprint: "4".repeat(64),
  first_row_no: 7,
  created_at: "2026-08-10T00:00:01Z",
} satisfies GradingItem;

interface StubOptions {
  grading?: ReturnType<typeof gradingSnapshot>;
  detection?: ReturnType<typeof detection>;
  validationMissing?: boolean;
  itemsEmpty?: boolean;
  gradingError?: boolean;
  postConflict?: boolean;
  calls?: string[];
  postBodies?: unknown[];
  rowTotal?: number;
}

function stubWorkspace(options: StubOptions = {}): void {
  let snapshot = options.grading ?? gradingSnapshot();
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const request = input instanceof Request ? input : new Request(String(input), init);
      const url = new URL(request.url);
      options.calls?.push(`${request.method} ${url.pathname}${url.search}`);
      if (request.method === "GET" && url.pathname.endsWith("/validation")) {
        return options.validationMissing
          ? json({ error: { code: "VALIDATION_NOT_FOUND", message: "校验不存在" } }, 404)
          : json(validation());
      }
      if (request.method === "GET" && url.pathname.endsWith("/detection")) {
        return json(options.detection ?? detection());
      }
      if (request.method === "GET" && url.pathname.endsWith("/grading-runs/current")) {
        return options.gradingError
          ? json({ error: { code: "GRADING_READ_FAILED", message: "分级读取失败" } }, 500)
          : json(snapshot);
      }
      if (request.method === "GET" && url.pathname.endsWith("/findings")) {
        return json({
          items: [
            { id: FINDING_B, run_id: DETECTION_ID },
            { id: FINDING_A, run_id: DETECTION_ID },
          ],
          total: 2,
          limit: 200,
          offset: 0,
        });
      }
      if (request.method === "POST" && url.pathname.endsWith("/grading-runs")) {
        options.postBodies?.push(await request.json());
        if (options.postConflict) {
          return json({ error: { code: "GRADING_RUN_CONFLICT", message: "分级运行冲突" } }, 409);
        }
        snapshot = gradingSnapshot();
        return json(gradingRun(false), 201);
      }
      if (request.method === "GET" && url.pathname.endsWith("/items")) {
        return json({
          items: options.itemsEmpty ? [] : [item],
          total: options.itemsEmpty ? 0 : 1,
          limit: 50,
          offset: Number(url.searchParams.get("offset") ?? 0),
        });
      }
      if (request.method === "GET" && url.pathname === `/api/v1/grading-items/${ITEM_ID}`) {
        return json(item);
      }
      if (request.method === "GET" && url.pathname.endsWith("/rows")) {
        const offset = Number(url.searchParams.get("offset") ?? 0);
        return json({
          items: [
            {
              id: `aaaaaaaa-aaaa-aaaa-aaaa-${String(offset).padStart(12, "0")}`,
              tenant_id: "cccccccc-cccc-cccc-cccc-cccccccccccc",
              grading_run_id: GRADING_ID,
              grading_item_id: ITEM_ID,
              file_version_id: FILE_ID,
              ordinal: offset + 1,
              row_no: offset + 7,
              raw: { note: "<img src=x onerror=alert(1)>" },
              normalized: {
                amount: "120.00",
                expense_date: "2026-08-01",
                employee: null,
                expense_type: null,
                invoice_type: null,
                invoice_no: null,
                merchant: null,
                invoice_title: null,
                submission_date: null,
                location: null,
                currency: null,
                description: null,
              },
              source_row_fingerprint: "5".repeat(64),
              created_at: "2026-08-10T00:00:01Z",
            },
          ],
          total: options.rowTotal ?? 1,
          limit: 50,
          offset,
        });
      }
      throw new Error(`未覆盖 ${request.method} ${url.pathname}`);
    }),
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("BatchGradingView", () => {
  it("auditor 显式分页收集全部 F6 IDs，并提交完整 not_run manifest", async () => {
    const user = userEvent.setup();
    const postBodies: unknown[] = [];
    vi.stubGlobal(
      "confirm",
      vi.fn(() => true),
    );
    stubWorkspace({ grading: gradingSnapshot({ run: false }), postBodies });
    renderWithProviders(<BatchGradingView fileVersionId={FILE_ID} user={makeUser()} />);

    await user.click(await screen.findByRole("button", { name: "创建分级" }));
    await waitFor(() => expect(postBodies).toHaveLength(1));
    expect(postBodies[0]).toEqual({
      validation_run_id: VALIDATION_ID,
      detection_run_id: DETECTION_ID,
      grading_config_id: CONFIG_ID,
      f7_manifest: [
        {
          kind: "not_run",
          correlation_finding_id: FINDING_A,
          reason_code: "INVESTIGATION_NOT_RUN",
        },
        {
          kind: "not_run",
          correlation_finding_id: FINDING_B,
          reason_code: "INVESTIGATION_NOT_RUN",
        },
      ],
    });
    expect(await screen.findByRole("status")).toHaveTextContent("综合分级快照已创建");
    expect(screen.getByText(/本次 manifest 策略/)).toBeInTheDocument();
  });

  it("viewer 只读查看全过滤、类型化证据和参与行分页，外部文本不会生成 DOM", async () => {
    const user = userEvent.setup();
    const calls: string[] = [];
    stubWorkspace({ calls, rowTotal: 51 });
    renderWithProviders(
      <BatchGradingView
        fileVersionId={FILE_ID}
        user={makeUser({ role: "viewer", permissions: ["batch:read"] })}
      />,
    );

    expect(await screen.findByText("综合分级 · 影响 × 置信")).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /^(创建分级|重新分级 \/ 复用)$/ }),
    ).not.toBeInTheDocument();
    expect(await screen.findByText("IMPACT_DETECTOR_MAPPING")).toBeInTheDocument();
    expect(await screen.findByText(/<img src=x onerror=alert\(1\)>/)).toBeInTheDocument();
    expect(document.querySelector("img")).toBeNull();

    await user.selectOptions(screen.getByLabelText("影响筛选"), "3");
    await waitFor(() =>
      expect(calls.some((call) => call.includes("severity_impact=3"))).toBe(true),
    );
    await user.click(await screen.findByRole("button", { name: "参与行下一页" }));
    await waitFor(() =>
      expect(calls.some((call) => call.includes(`/rows?limit=50&offset=50`))).toBe(true),
    );
  });

  it("明确展示 config missing、run absent、F6 degraded/unavailable 与上游缺失", async () => {
    stubWorkspace({
      grading: {
        ...gradingSnapshot({ run: false, config: false }),
        current_validation_run_id: null,
        current_detection_run_id: null,
      },
      detection: detection({ run: false, degraded: true }),
      validationMissing: true,
    });
    renderWithProviders(<BatchGradingView fileVersionId={FILE_ID} user={makeUser()} />);

    expect(await screen.findByText("缺少二维分级配置")).toBeInTheDocument();
    expect(screen.getByText("上游运行尚未齐备")).toBeInTheDocument();
    expect(screen.getByText("部分 F6 能力降级")).toBeInTheDocument();
    expect(screen.getByText("部分 F6 能力不可用")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "创建分级" })).toBeDisabled();
    expect(screen.getByText("RUN ABSENT")).toBeInTheDocument();
  });

  it("明确展示三基准 stale，并接受有效空项目页", async () => {
    stubWorkspace({ grading: gradingSnapshot({ stale: true }), itemsEmpty: true });
    renderWithProviders(<BatchGradingView fileVersionId={FILE_ID} user={makeUser()} />);

    expect(await screen.findAllByText("STALE")).toHaveLength(3);
    expect(screen.getByText("当前快照已过期")).toBeInTheDocument();
    expect(await screen.findByText(/有效的空结果/)).toBeInTheDocument();
  });

  it("读取错误与写冲突均显式呈现，不伪造成功", async () => {
    stubWorkspace({ gradingError: true });
    const errorView = renderWithProviders(
      <BatchGradingView fileVersionId={FILE_ID} user={makeUser()} />,
    );
    expect(await screen.findByRole("alert")).toHaveTextContent("分级读取失败");
    errorView.unmount();

    const user = userEvent.setup();
    vi.stubGlobal(
      "confirm",
      vi.fn(() => true),
    );
    stubWorkspace({ grading: gradingSnapshot({ run: false }), postConflict: true });
    renderWithProviders(<BatchGradingView fileVersionId={FILE_ID} user={makeUser()} />);
    await user.click(await screen.findByRole("button", { name: "创建分级" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("运行冲突：分级运行冲突");
    expect(screen.queryByText("综合分级快照已创建")).not.toBeInTheDocument();
  });
});
