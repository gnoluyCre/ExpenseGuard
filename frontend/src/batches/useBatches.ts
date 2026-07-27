import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  api,
  apiBaseUrl,
  type BatchDetail,
  type BatchImportResponse,
  type BatchSummary,
} from "@/api/client";

export const BATCHES_KEY = ["batches"] as const;

export function useBatches() {
  return useQuery({
    queryKey: BATCHES_KEY,
    queryFn: async (): Promise<BatchSummary[]> => {
      const { data, response, error } = await api.GET("/api/batches");
      if (!data) {
        throw new Error(extractErrorMessage(error) ?? `读取批次失败（HTTP ${response.status}）`);
      }
      return data;
    },
  });
}

export function useBatchDetail(fileVersionId: string | null) {
  return useQuery({
    queryKey: [...BATCHES_KEY, fileVersionId],
    enabled: fileVersionId !== null,
    queryFn: async (): Promise<BatchDetail> => {
      if (fileVersionId === null) throw new Error("缺少批次 ID");
      const { data, response, error } = await api.GET("/api/batches/{file_version_id}", {
        params: { path: { file_version_id: fileVersionId } },
      });
      if (!data) {
        throw new Error(
          extractErrorMessage(error) ?? `读取批次详情失败（HTTP ${response.status}）`,
        );
      }
      return data;
    },
  });
}

export function useImportBatch() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (file: File): Promise<BatchImportResponse> => {
      const formData = new FormData();
      formData.append("file", file);
      const response = await globalThis.fetch(`${apiBaseUrl}/api/batches`, {
        method: "POST",
        credentials: "include",
        body: formData,
      });
      const payload: unknown = await response.json();
      if (!response.ok) {
        throw new Error(extractErrorMessage(payload) ?? `导入失败（HTTP ${response.status}）`);
      }
      return payload as BatchImportResponse;
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: BATCHES_KEY });
    },
  });
}

function extractErrorMessage(error: unknown): string | undefined {
  if (typeof error !== "object" || error === null) return undefined;
  const wrapped = (error as { error?: unknown }).error;
  if (typeof wrapped === "object" && wrapped !== null) {
    const message = (wrapped as { message?: unknown }).message;
    if (typeof message === "string") return message;
  }
  const detail = (error as { detail?: unknown }).detail;
  if (typeof detail === "string") return detail;
  if (typeof detail === "object" && detail !== null) {
    const message = (detail as { message?: unknown }).message;
    if (typeof message === "string") return message;
  }
  const message = (error as { message?: unknown }).message;
  return typeof message === "string" ? message : undefined;
}
