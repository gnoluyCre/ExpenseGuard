import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { BatchDetectionView } from "@/batches/BatchDetectionView";
import { makeUser, renderWithProviders } from "@/test/utils";

const FILE_ID = "11111111-1111-1111-1111-111111111111";
const RUN_ID = "22222222-2222-2222-2222-222222222222";
const FINDING_ID = "33333333-3333-3333-3333-333333333333";

function json(body: unknown): Response {
  return new Response(JSON.stringify(body), { headers: { "content-type": "application/json" } });
}

function capability(detector: string, status: string, findingCount: number) {
  return {
    detector,
    detector_version: `${detector}-v1`,
    status,
    reason_code: status === "enabled" ? "READY" : "REQUIRED_FIELD_MISSING",
    reason: status === "enabled" ? "能力就绪" : "缺少字段",
    finding_count: findingCount,
    details: {
      source_row_count: 20,
      parsed_row_count: 19,
      eligible_row_count: status === "unavailable" ? 0 : 18,
      excluded_row_count: status === "unavailable" ? 20 : 2,
      eligible_rate_bps: status === "unavailable" ? 0 : 9000,
      dependencies: [],
      causes: [],
      exclusion_counts: [],
      runtime: { detector },
    },
  };
}

afterEach(() => vi.unstubAllGlobals());

describe("BatchDetectionView", () => {
  it("区分 enabled 零候选、degraded 与 unavailable，并把恶意文本当文本显示", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const request = input instanceof Request ? input : new Request(String(input), init);
        const url = new URL(request.url);
        if (url.pathname.endsWith("/detection"))
          return json({
            run: {
              id: RUN_ID,
              file_version_id: FILE_ID,
              detection_config_id: "44444444-4444-4444-4444-444444444444",
              config_version: 2,
              config_fingerprint: "a".repeat(64),
              algorithm_bundle_version: "correlation-v1",
              input_fingerprint: "b".repeat(64),
              run_fingerprint: "c".repeat(64),
              source_row_count: 20,
              parsed_row_count: 19,
              error_row_count: 1,
              finding_count: 1,
              created_by: "55555555-5555-5555-5555-555555555555",
              created_at: "2026-08-01T00:00:00Z",
              completed_at: "2026-08-01T00:00:01Z",
              reused_existing: true,
            },
            capabilities: [
              capability("split_invoice", "enabled", 0),
              capability("sequential_invoice", "degraded", 1),
              capability("frequency_anomaly", "unavailable", 0),
              capability("spatiotemporal_tier0", "enabled", 0),
            ],
            current_config_fingerprint: "a".repeat(64),
            config_stale: false,
          });
        if (url.pathname.endsWith("/findings"))
          return json({
            items: [
              {
                id: FINDING_ID,
                run_id: RUN_ID,
                detector: "sequential_invoice",
                detector_version: "invoice-sequence-v1",
                finding_key: "d".repeat(64),
                reasoning: "<img src=x onerror=alert(1)>",
                participating_row_count: 2,
                first_row_no: 3,
              },
            ],
            total: 1,
            limit: 50,
            offset: 0,
          });
        if (url.pathname.includes("correlation-findings"))
          return json({
            id: FINDING_ID,
            run_id: RUN_ID,
            file_version_id: FILE_ID,
            detector: "sequential_invoice",
            detector_version: "invoice-sequence-v1",
            finding_key: "d".repeat(64),
            evidence: {
              schema_version: 1,
              detector: "sequential_invoice",
              detector_version: "invoice-sequence-v1",
              profile_fingerprint: "a".repeat(64),
              group_key_fingerprint: "e".repeat(64),
              reason_code: "STATISTICAL_CANDIDATE",
              facts: {
                prefix_fingerprint: "f".repeat(64),
                suffix_width: 3,
                start_serial: "001",
                end_serial: "002",
                sequence_length: 2,
                ordered_row_nos: [3, 4],
              },
            },
            reasoning: "<img src=x onerror=alert(1)>",
            rows: [
              {
                ordinal: 1,
                row_no: 3,
                raw: { note: "javascript:alert(1)" },
                normalized: null,
                parse_error_code: null,
              },
            ],
            completed: 1,
            total: 2,
          });
        throw new Error(`未覆盖 ${url.pathname}`);
      }),
    );
    renderWithProviders(
      <BatchDetectionView
        fileVersionId={FILE_ID}
        user={makeUser({ role: "viewer", permissions: ["batch:read"] })}
      />,
    );
    expect(await screen.findAllByText("检测能力正常，本次零候选")).toHaveLength(2);
    expect(screen.getAllByText("降级").length).toBeGreaterThan(0);
    expect(screen.getAllByText("不可用").length).toBeGreaterThan(0);
    expect(await screen.findAllByText("<img src=x onerror=alert(1)>")).not.toHaveLength(0);
    expect(document.querySelector("img")).toBeNull();
    expect(screen.queryByRole("button", { name: "执行检测" })).not.toBeInTheDocument();
  });

  it("明确展示 stale，并在幂等复用后刷新不可变快照", async () => {
    const user = userEvent.setup();
    let snapshotReads = 0;
    vi.stubGlobal(
      "confirm",
      vi.fn(() => true),
    );
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const request = input instanceof Request ? input : new Request(String(input), init);
        const url = new URL(request.url);
        if (request.method === "POST") {
          return new Response(
            JSON.stringify({
              id: RUN_ID,
              file_version_id: FILE_ID,
              detection_config_id: "44444444-4444-4444-4444-444444444444",
              config_version: 1,
              config_fingerprint: "a".repeat(64),
              algorithm_bundle_version: "correlation-v1",
              input_fingerprint: "b".repeat(64),
              run_fingerprint: "c".repeat(64),
              source_row_count: 0,
              parsed_row_count: 0,
              error_row_count: 0,
              finding_count: 0,
              created_by: "55555555-5555-5555-5555-555555555555",
              created_at: "2026-08-01T00:00:00Z",
              completed_at: "2026-08-01T00:00:01Z",
              reused_existing: true,
            }),
            { status: 200, headers: { "content-type": "application/json" } },
          );
        }
        if (url.pathname.endsWith("/detection")) {
          snapshotReads += 1;
          return json({
            run: {
              id: RUN_ID,
              file_version_id: FILE_ID,
              detection_config_id: "44444444-4444-4444-4444-444444444444",
              config_version: 1,
              config_fingerprint: "a".repeat(64),
              algorithm_bundle_version: "correlation-v1",
              input_fingerprint: "b".repeat(64),
              run_fingerprint: "c".repeat(64),
              source_row_count: 0,
              parsed_row_count: 0,
              error_row_count: 0,
              finding_count: 0,
              created_by: "55555555-5555-5555-5555-555555555555",
              created_at: "2026-08-01T00:00:00Z",
              completed_at: "2026-08-01T00:00:01Z",
              reused_existing: true,
            },
            capabilities: [],
            current_config_fingerprint: "d".repeat(64),
            config_stale: true,
          });
        }
        if (url.pathname.endsWith("/findings")) {
          return json({ items: [], total: 0, limit: 50, offset: 0 });
        }
        throw new Error(`未覆盖 ${request.method} ${url.pathname}`);
      }),
    );

    renderWithProviders(<BatchDetectionView fileVersionId={FILE_ID} user={makeUser()} />);
    expect(await screen.findByText("CONFIG STALE")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "执行检测" }));
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("已复用"));
    await waitFor(() => expect(snapshotReads).toBeGreaterThan(1));
  });

  it("把运行冲突显式呈现为错误而不伪造成功", async () => {
    const user = userEvent.setup();
    vi.stubGlobal(
      "confirm",
      vi.fn(() => true),
    );
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const request = input instanceof Request ? input : new Request(String(input), init);
        const url = new URL(request.url);
        if (request.method === "POST") {
          return new Response(
            JSON.stringify({
              error: { code: "DETECTION_CONFLICT", message: "该批次正在执行关联检测" },
            }),
            { status: 409, headers: { "content-type": "application/json" } },
          );
        }
        if (url.pathname.endsWith("/detection")) {
          return json({
            run: null,
            capabilities: [],
            current_config_fingerprint: "a".repeat(64),
            config_stale: false,
          });
        }
        throw new Error(`未覆盖 ${request.method} ${url.pathname}`);
      }),
    );

    renderWithProviders(<BatchDetectionView fileVersionId={FILE_ID} user={makeUser()} />);
    expect(await screen.findByText(/尚无运行快照/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "执行检测" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("正在执行关联检测");
    expect(screen.queryByText("关联检测完成")).not.toBeInTheDocument();
  });
});
