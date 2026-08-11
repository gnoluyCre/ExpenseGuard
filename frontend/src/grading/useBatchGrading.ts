import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  api,
  apiErrorCode,
  apiErrorMessage,
  type BatchGrading,
  type GradingDisposition,
  type GradingItem,
  type GradingItemPage,
  type GradingRun,
  type GradingRunCreateRequest,
  type GradingSourceKind,
  type InvestigationGradingOutcome,
  type DetectorKind,
  type RuleKind,
} from "@/api/client";
import { BATCHES_KEY } from "@/batches/useBatches";

export class GradingRequestError extends Error {
  readonly code: string | undefined;

  constructor(message: string, code?: string) {
    super(message);
    this.name = "GradingRequestError";
    this.code = code;
  }
}

export const batchGradingKey = (fileVersionId: string) =>
  [...BATCHES_KEY, fileVersionId, "grading"] as const;
function requestKey(): string {
  return `grading-${crypto.randomUUID()}`;
}

function failure(error: unknown, response: Response, fallback: string): GradingRequestError {
  return new GradingRequestError(
    apiErrorMessage(error) ?? `${fallback}（HTTP ${response.status}）`,
    apiErrorCode(error),
  );
}

export function useBatchGrading(fileVersionId: string) {
  return useQuery({
    queryKey: batchGradingKey(fileVersionId),
    queryFn: async (): Promise<BatchGrading> => {
      const { data, error, response } = await api.GET(
        "/api/v1/files/{file_version_id}/grading-runs/current",
        { params: { path: { file_version_id: fileVersionId } } },
      );
      if (!data) throw failure(error, response, "读取综合分级快照失败");
      return data;
    },
  });
}

async function loadAllCorrelationFindingIds(detectionRunId: string): Promise<string[]> {
  const ids: string[] = [];
  const limit = 200;
  let offset = 0;
  let expectedTotal: number | null = null;

  for (;;) {
    const { data, error, response } = await api.GET("/api/detection-runs/{run_id}/findings", {
      params: {
        path: { run_id: detectionRunId },
        query: {
          detector: null,
          capability_status: null,
          sort_by: "default",
          limit,
          offset,
        },
      },
    });
    if (!data) throw failure(error, response, "读取完整 F6 候选清单失败");
    if (expectedTotal === null) expectedTotal = data.total;
    if (data.total !== expectedTotal) {
      throw new GradingRequestError(
        "F6 候选清单在读取期间发生变化，请刷新后重试",
        "GRADING_SOURCE_CONFLICT",
      );
    }
    if (expectedTotal > 20_000) {
      throw new GradingRequestError(
        "F6 候选超过单次分级上限 20,000 项",
        "GRADING_MANIFEST_TOO_LARGE",
      );
    }
    ids.push(...data.items.map((item) => item.id));
    if (ids.length >= expectedTotal) break;
    if (data.items.length === 0) {
      throw new GradingRequestError("F6 候选分页不完整，请刷新后重试", "GRADING_SOURCE_CONFLICT");
    }
    offset += data.items.length;
  }

  if (new Set(ids).size !== ids.length || ids.length !== expectedTotal) {
    throw new GradingRequestError("F6 候选清单存在重复或缺失项", "GRADING_SOURCE_CONFLICT");
  }
  return ids.sort((left, right) => (left < right ? -1 : left > right ? 1 : 0));
}

export interface CreateBatchGradingInput {
  readonly validationRunId: string;
  readonly detectionRunId: string;
  readonly gradingConfigId: string;
}

export function useCreateBatchGrading(fileVersionId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (input: CreateBatchGradingInput): Promise<GradingRun> => {
      const findingIds = await loadAllCorrelationFindingIds(input.detectionRunId);
      const body: GradingRunCreateRequest = {
        validation_run_id: input.validationRunId,
        detection_run_id: input.detectionRunId,
        grading_config_id: input.gradingConfigId,
        f7_manifest: findingIds.map((correlationFindingId) => ({
          kind: "not_run",
          correlation_finding_id: correlationFindingId,
          reason_code: "INVESTIGATION_NOT_RUN",
        })),
      };
      const { data, error, response } = await api.POST(
        "/api/v1/files/{file_version_id}/grading-runs",
        {
          params: {
            path: { file_version_id: fileVersionId },
            header: { "Idempotency-Key": requestKey() },
          },
          body,
        },
      );
      if (!data) throw failure(error, response, "执行综合分级失败");
      return data;
    },
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: batchGradingKey(fileVersionId) });
    },
  });
}

export interface GradingFilters {
  readonly sourceKind?: GradingSourceKind;
  readonly ruleKind?: RuleKind;
  readonly detector?: DetectorKind;
  readonly severityImpact?: number;
  readonly severityConfidence?: number;
  readonly disposition?: GradingDisposition;
  readonly f7Outcome?: InvestigationGradingOutcome;
  readonly offset: number;
  readonly limit: number;
}

export function useGradingItems(runId: string | null, filters: GradingFilters) {
  return useQuery({
    queryKey: ["grading", "runs", runId, "items", filters],
    enabled: runId !== null,
    queryFn: async (): Promise<GradingItemPage> => {
      if (!runId) throw new Error("缺少综合分级运行标识");
      const { data, error, response } = await api.GET(
        "/api/v1/grading-runs/{grading_run_id}/items",
        {
          params: {
            path: { grading_run_id: runId },
            query: {
              source_kind: filters.sourceKind ?? null,
              rule_kind: filters.ruleKind ?? null,
              detector: filters.detector ?? null,
              severity_impact: filters.severityImpact ?? null,
              severity_confidence: filters.severityConfidence ?? null,
              disposition: filters.disposition ?? null,
              f7_outcome: filters.f7Outcome ?? null,
              sort_by: "default",
              limit: filters.limit,
              offset: filters.offset,
            },
          },
        },
      );
      if (!data) throw failure(error, response, "读取综合分级项目失败");
      return data;
    },
  });
}

export function useGradingItem(itemId: string | null) {
  return useQuery({
    queryKey: ["grading", "items", itemId],
    enabled: itemId !== null,
    queryFn: async (): Promise<GradingItem> => {
      if (!itemId) throw new Error("缺少综合分级项目标识");
      const { data, error, response } = await api.GET("/api/v1/grading-items/{grading_item_id}", {
        params: { path: { grading_item_id: itemId } },
      });
      if (!data) throw failure(error, response, "读取分级证据详情失败");
      return data;
    },
  });
}

export function useGradingRows(itemId: string | null, offset: number, limit = 50) {
  return useQuery({
    queryKey: ["grading", "items", itemId, "rows", offset, limit],
    enabled: itemId !== null,
    queryFn: async () => {
      if (!itemId) throw new Error("缺少综合分级项目标识");
      const { data, error, response } = await api.GET(
        "/api/v1/grading-items/{grading_item_id}/rows",
        {
          params: {
            path: { grading_item_id: itemId },
            query: { limit, offset },
          },
        },
      );
      if (!data) throw failure(error, response, "读取全部参与行失败");
      return data;
    },
  });
}
