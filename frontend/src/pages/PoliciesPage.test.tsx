import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { CURRENT_USER_KEY } from "@/auth/useAuth";
import { PoliciesPage } from "@/pages/PoliciesPage";
import { createTestQueryClient, makeUser, renderWithProviders } from "@/test/utils";

function json(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "content-type": "application/json" },
  });
}

afterEach(() => vi.unstubAllGlobals());

describe("PoliciesPage", () => {
  it("以文本呈现制度原文，viewer 不显示写入表单", async () => {
    const longStableKey =
      "travel.policy.with.a.deliberately.long.stable.key.for.desktop.overflow.gate";
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const request = input instanceof Request ? input : new Request(String(input), init);
        const url = new URL(request.url);
        if (url.pathname === "/api/policies/families")
          return json([
            {
              id: "11111111-1111-1111-1111-111111111111",
              stable_key: longStableKey,
              display_name: "差旅制度",
              created_at: "2026-07-29T05:00:00Z",
              documents: [
                {
                  id: "22222222-2222-2222-2222-222222222222",
                  title: "差旅制度<script>",
                  version: "2026.1",
                  effective_date: "2026-01-01",
                  expiry_date: null,
                  content_sha256: "a".repeat(64),
                  status: "published",
                  index_status: "completed",
                  index_completed_points: 2,
                  index_expected_points: 2,
                  failure_code: null,
                  created_at: "2026-07-29T05:00:00Z",
                },
              ],
            },
          ]);
        if (url.pathname === "/api/rules") return json([]);
        if (url.pathname.includes("/api/policies/documents/"))
          return json({
            id: "22222222-2222-2222-2222-222222222222",
            family_id: "11111111-1111-1111-1111-111111111111",
            family_stable_key: "travel",
            title: "差旅制度<script>",
            version: "2026.1",
            effective_date: "2026-01-01",
            expiry_date: null,
            content_sha256: "a".repeat(64),
            status: "published",
            index_status: "completed",
            index_completed_points: 2,
            index_expected_points: 2,
            failure_code: null,
            created_at: "2026-07-29T05:00:00Z",
            source_filename: "policy.txt",
            mime_type: "text/plain",
            size_bytes: 100,
            clauses: [
              {
                id: "33333333-3333-3333-3333-333333333333",
                clause_no: "第十条",
                hierarchy_path: null,
                text: "<script>alert(1)</script>报销不得超过标准",
                text_sha256: "b".repeat(64),
                ordinal: 1,
                source_locator: {},
              },
            ],
          });
        throw new Error(`未覆盖请求：${url.pathname}`);
      }),
    );
    const queryClient = createTestQueryClient();
    queryClient.setQueryData(
      CURRENT_USER_KEY,
      makeUser({ role: "viewer", permissions: ["config:read", "report:read"] }),
    );
    const user = userEvent.setup();
    renderWithProviders(<PoliciesPage />, { queryClient });
    const documentButton = await screen.findByRole("button", { name: /差旅制度<script>/ });
    expect(documentButton).toBeInTheDocument();
    expect(documentButton).toHaveClass("min-w-0", "w-full");
    expect(screen.getByText(longStableKey)).toHaveClass("break-all");
    expect(screen.queryByText("01 · 建立制度族")).not.toBeInTheDocument();
    await user.click(documentButton);
    expect(
      await screen.findByText("<script>alert(1)</script>报销不得超过标准"),
    ).toBeInTheDocument();
    expect(document.querySelector("script")).toBeNull();
  });
});
