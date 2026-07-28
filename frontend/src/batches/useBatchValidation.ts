import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  api,
  type CreateRevisionResponse,
  type FindingsResponse,
  type RevisionReason,
  type ValidationSummary,
} from "@/api/client";
import { batchParsingKey } from "@/batches/useBatchParsing";
import { BATCHES_KEY } from "@/batches/useBatches";

export type FindingVerdict = "flagged" | "manual_review";

export const batchValidationKey = (fileVersionId: string) =>
  [...BATCHES_KEY, fileVersionId, "validation"] as const;

export const batchFindingsKey = (fileVersionId: string) =>
  [...BATCHES_KEY, fileVersionId, "findings"] as const;

function extractErrorMessage(error: unknown): string | undefined {
  if (typeof error !== "object" || error === null) return undefined;
  const wrapped = (error as { error?: unknown }).error;
  if (typeof wrapped !== "object" || wrapped === null) return undefined;
  const message = (wrapped as { message?: unknown }).message;
  return typeof message === "string" ? message : undefined;
}

function isErrorCode(error: unknown, code: string): boolean {
  if (typeof error !== "object" || error === null) return false;
  const wrapped = (error as { error?: unknown }).error;
  return (
    typeof wrapped === "object" && wrapped !== null && (wrapped as { code?: unknown }).code === code
  );
}

async function invalidateValidationWorkspace(
  queryClient: ReturnType<typeof useQueryClient>,
  fileVersionId: string,
): Promise<void> {
  await Promise.all([
    queryClient.invalidateQueries({ queryKey: BATCHES_KEY, exact: true }),
    queryClient.invalidateQueries({ queryKey: [...BATCHES_KEY, fileVersionId] }),
    queryClient.invalidateQueries({ queryKey: batchParsingKey(fileVersionId) }),
    queryClient.invalidateQueries({ queryKey: batchValidationKey(fileVersionId) }),
    queryClient.invalidateQueries({ queryKey: batchFindingsKey(fileVersionId) }),
    queryClient.invalidateQueries({ queryKey: ["rules"] }),
  ]);
}

export function useBatchValidation(fileVersionId: string) {
  return useQuery({
    queryKey: batchValidationKey(fileVersionId),
    queryFn: async (): Promise<ValidationSummary | null> => {
      const { data, response, error } = await api.GET("/api/batches/{file_version_id}/validation", {
        params: { path: { file_version_id: fileVersionId } },
      });
      if (data) return data;
      if (response.status === 404 && isErrorCode(error, "VALIDATION_NOT_FOUND")) return null;
      throw new Error(extractErrorMessage(error) ?? `读取校验摘要失败（HTTP ${response.status}）`);
    },
  });
}

export function useBatchFindings(
  fileVersionId: string,
  page: number,
  verdict: FindingVerdict,
  enabled = true,
) {
  return useQuery({
    queryKey: [...batchFindingsKey(fileVersionId), verdict, page],
    enabled,
    queryFn: async (): Promise<FindingsResponse> => {
      const { data, response, error } = await api.GET("/api/batches/{file_version_id}/findings", {
        params: {
          path: { file_version_id: fileVersionId },
          query: { page, page_size: 50, verdict },
        },
      });
      if (!data) {
        throw new Error(
          extractErrorMessage(error) ?? `读取校验发现失败（HTTP ${response.status}）`,
        );
      }
      return data;
    },
  });
}

export function useValidateBatch(fileVersionId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (): Promise<ValidationSummary> => {
      const { data, response, error } = await api.POST("/api/batches/{file_version_id}/validate", {
        params: { path: { file_version_id: fileVersionId } },
      });
      if (!data) {
        throw new Error(extractErrorMessage(error) ?? `执行校验失败（HTTP ${response.status}）`);
      }
      return data;
    },
    onSuccess: async () => invalidateValidationWorkspace(queryClient, fileVersionId),
  });
}

export interface CreateRevisionInput {
  idempotencyKey: string;
  reason: RevisionReason;
}

export function useCreateBatchRevision(fileVersionId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({
      reason,
      idempotencyKey,
    }: CreateRevisionInput): Promise<CreateRevisionResponse> => {
      const { data, response, error } = await api.POST("/api/batches/{file_version_id}/revisions", {
        params: {
          path: { file_version_id: fileVersionId },
          header: { "Idempotency-Key": idempotencyKey },
        },
        body: { reason },
      });
      if (!data) {
        throw new Error(
          extractErrorMessage(error) ?? `创建派生版本失败（HTTP ${response.status}）`,
        );
      }
      return data;
    },
    onSuccess: async () => invalidateValidationWorkspace(queryClient, fileVersionId),
  });
}
