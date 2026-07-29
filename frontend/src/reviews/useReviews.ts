import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  api,
  apiErrorCode,
  apiErrorMessage,
  type ClearanceReviewDetail,
  type FindingDecisionRequest,
  type FindingReviewDetail,
  type FindingReviewResult,
  type ReviewQueuePage,
  type ReviewSummary,
  type SamplingConfig,
  type SamplingConfigCreateRequest,
  type SamplingConfigHistory,
  type SamplingDecisionRequest,
  type SamplingPlanResponse,
  type SamplingReviewResult,
} from "@/api/client";

export class ReviewApiError extends Error {
  readonly code: string | undefined;
  readonly status: number;

  constructor(message: string, code: string | undefined, status: number) {
    super(message);
    this.name = "ReviewApiError";
    this.code = code;
    this.status = status;
  }
}

function reviewApiError(error: unknown, response: Response, fallback: string): ReviewApiError {
  return new ReviewApiError(
    apiErrorMessage(error) ?? `${fallback}（HTTP ${response.status}）`,
    apiErrorCode(error),
    response.status,
  );
}

export interface ReviewQueueFilters {
  status: "pending" | "completed";
  kind: "finding" | "clearance_sample" | null;
  reportId: string | null;
  fileVersionId: string | null;
  limit: number;
  offset: number;
}

export const reviewKeys = {
  all: ["reviews"] as const,
  config: ["reviews", "sampling-config"] as const,
  queue: (filters: ReviewQueueFilters) => ["reviews", "queue", filters] as const,
  detail: (kind: "finding" | "clearance_sample", targetId: string | null) =>
    ["reviews", "detail", kind, targetId] as const,
  summary: (reportId: string | null) => ["reviews", "summary", reportId] as const,
  plan: (reportId: string | null) => ["reviews", "plan", reportId] as const,
};

export function useSamplingConfig(enabled = true) {
  return useQuery({
    queryKey: reviewKeys.config,
    enabled,
    queryFn: async (): Promise<SamplingConfigHistory> => {
      const { data, error, response } = await api.GET("/api/review/sampling-config");
      if (!data) throw reviewApiError(error, response, "读取抽样配置失败");
      return data;
    },
  });
}

export function useSaveSamplingConfig() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: async ({
      request,
      idempotencyKey,
    }: {
      request: SamplingConfigCreateRequest;
      idempotencyKey: string;
    }): Promise<SamplingConfig> => {
      const { data, error, response } = await api.PUT("/api/review/sampling-config", {
        params: { header: { "Idempotency-Key": idempotencyKey } },
        body: request,
      });
      if (!data) throw reviewApiError(error, response, "保存抽样配置失败");
      return data;
    },
    onSuccess: () => void client.invalidateQueries({ queryKey: reviewKeys.config }),
    onError: (error) => {
      if (error instanceof ReviewApiError && error.status === 409) {
        void client.invalidateQueries({ queryKey: reviewKeys.config });
      }
    },
  });
}

export function useReviewQueue(filters: ReviewQueueFilters, enabled = true) {
  return useQuery({
    queryKey: reviewKeys.queue(filters),
    enabled,
    queryFn: async (): Promise<ReviewQueuePage> => {
      const { data, error, response } = await api.GET("/api/reviews/queue", {
        params: {
          query: {
            status: filters.status,
            kind: filters.kind,
            report_id: filters.reportId,
            file_version_id: filters.fileVersionId,
            sort_by: "default",
            limit: filters.limit,
            offset: filters.offset,
          },
        },
      });
      if (!data) throw reviewApiError(error, response, "读取复核队列失败");
      return data;
    },
  });
}

export function useFindingReviewDetail(targetId: string | null) {
  return useQuery({
    queryKey: reviewKeys.detail("finding", targetId),
    enabled: targetId !== null,
    queryFn: async (): Promise<FindingReviewDetail> => {
      if (!targetId) throw new Error("缺少 finding 复核目标");
      const { data, error, response } = await api.GET("/api/reviews/findings/{report_item_id}", {
        params: { path: { report_item_id: targetId } },
      });
      if (!data) throw reviewApiError(error, response, "读取 finding 详情失败");
      return data;
    },
  });
}

export function useSampleReviewDetail(targetId: string | null) {
  return useQuery({
    queryKey: reviewKeys.detail("clearance_sample", targetId),
    enabled: targetId !== null,
    queryFn: async (): Promise<ClearanceReviewDetail> => {
      if (!targetId) throw new Error("缺少抽检目标");
      const { data, error, response } = await api.GET("/api/reviews/samples/{sampling_audit_id}", {
        params: { path: { sampling_audit_id: targetId } },
      });
      if (!data) throw reviewApiError(error, response, "读取抽检详情失败");
      return data;
    },
  });
}

export function useReviewSummary(reportId: string | null) {
  return useQuery({
    queryKey: reviewKeys.summary(reportId),
    enabled: reportId !== null,
    queryFn: async (): Promise<ReviewSummary> => {
      if (!reportId) throw new Error("缺少报告 ID");
      const { data, error, response } = await api.GET("/api/reviews/summary", {
        params: { query: { report_id: reportId } },
      });
      if (!data) throw reviewApiError(error, response, "读取复核汇总失败");
      return data;
    },
  });
}

export function useReviewPlan(reportId: string | null) {
  return useQuery({
    queryKey: reviewKeys.plan(reportId),
    enabled: reportId !== null,
    queryFn: async (): Promise<SamplingPlanResponse> => {
      if (!reportId) throw new Error("缺少报告 ID");
      const { data, error, response } = await api.GET("/api/reports/{report_id}/review-plan", {
        params: { path: { report_id: reportId } },
      });
      if (!data) throw reviewApiError(error, response, "读取抽样计划失败");
      return data;
    },
  });
}

export function useCreateReviewPlan(reportId: string | null) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: async (): Promise<SamplingPlanResponse> => {
      if (!reportId) throw new Error("缺少报告 ID");
      const { data, error, response } = await api.POST("/api/reports/{report_id}/review-plan", {
        params: {
          path: { report_id: reportId },
          header: { "Idempotency-Key": crypto.randomUUID() },
        },
      });
      if (!data) throw reviewApiError(error, response, "创建抽样计划失败");
      return data;
    },
    onSuccess: () => void client.invalidateQueries({ queryKey: reviewKeys.all }),
    onError: (error) => {
      if (error instanceof ReviewApiError && error.status === 409) invalidateReviewState(client);
    },
  });
}

function invalidateReviewState(client: ReturnType<typeof useQueryClient>): void {
  void client.invalidateQueries({ queryKey: reviewKeys.all });
}

export function useSubmitFindingDecision(targetId: string | null) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: async (request: FindingDecisionRequest): Promise<FindingReviewResult> => {
      if (!targetId) throw new Error("缺少 finding 复核目标");
      const { data, error, response } = await api.POST(
        "/api/reviews/findings/{report_item_id}/decision",
        {
          params: {
            path: { report_item_id: targetId },
            header: { "Idempotency-Key": crypto.randomUUID() },
          },
          body: request,
        },
      );
      if (!data) throw reviewApiError(error, response, "提交 finding 结论失败");
      return data;
    },
    onSuccess: () => invalidateReviewState(client),
    onError: (error) => {
      if (error instanceof ReviewApiError && error.status === 409) invalidateReviewState(client);
    },
  });
}

export function useSubmitSampleDecision(targetId: string | null) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: async (request: SamplingDecisionRequest): Promise<SamplingReviewResult> => {
      if (!targetId) throw new Error("缺少抽检目标");
      const { data, error, response } = await api.POST(
        "/api/reviews/samples/{sampling_audit_id}/decision",
        {
          params: {
            path: { sampling_audit_id: targetId },
            header: { "Idempotency-Key": crypto.randomUUID() },
          },
          body: request,
        },
      );
      if (!data) throw reviewApiError(error, response, "提交抽检结论失败");
      return data;
    },
    onSuccess: () => invalidateReviewState(client),
    onError: (error) => {
      if (error instanceof ReviewApiError && error.status === 409) invalidateReviewState(client);
    },
  });
}
