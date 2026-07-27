import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  api,
  type FieldAvailabilityResponse,
  type ParseBatchResponse,
  type ParseErrorsResponse,
  type SaveSchemaMappingRequest,
  type SaveSchemaMappingResponse,
  type SchemaMappings,
} from "@/api/client";
import { BATCHES_KEY } from "@/batches/useBatches";

export const batchParsingKey = (fileVersionId: string) =>
  [...BATCHES_KEY, fileVersionId, "parsing"] as const;

function extractErrorMessage(error: unknown): string | undefined {
  if (typeof error !== "object" || error === null) return undefined;
  const wrapped = (error as { error?: unknown }).error;
  if (typeof wrapped === "object" && wrapped !== null) {
    const message = (wrapped as { message?: unknown }).message;
    if (typeof message === "string") return message;
  }
  return undefined;
}

function isErrorCode(error: unknown, code: string): boolean {
  if (typeof error !== "object" || error === null) return false;
  const wrapped = (error as { error?: unknown }).error;
  return (
    typeof wrapped === "object" && wrapped !== null && (wrapped as { code?: unknown }).code === code
  );
}

export function useSchemaMappings(fileVersionId: string, enabled: boolean) {
  return useQuery({
    queryKey: [...batchParsingKey(fileVersionId), "mappings"],
    enabled,
    queryFn: async (): Promise<SchemaMappings> => {
      const { data, response, error } = await api.GET("/api/schema-mappings", {
        params: { query: { file_version_id: fileVersionId } },
      });
      if (!data) {
        throw new Error(
          extractErrorMessage(error) ?? `读取字段映射失败（HTTP ${response.status}）`,
        );
      }
      return data;
    },
  });
}

export function useParseErrors(fileVersionId: string, offset: number) {
  return useQuery({
    queryKey: [...batchParsingKey(fileVersionId), "errors", offset],
    queryFn: async (): Promise<ParseErrorsResponse | null> => {
      const { data, response, error } = await api.GET(
        "/api/batches/{file_version_id}/parse-errors",
        {
          params: { path: { file_version_id: fileVersionId }, query: { offset, limit: 50 } },
        },
      );
      if (data) return data;
      if (response.status === 409 && isErrorCode(error, "BATCH_NOT_PARSED")) return null;
      throw new Error(extractErrorMessage(error) ?? `读取错误清单失败（HTTP ${response.status}）`);
    },
  });
}

export function useFieldAvailability(fileVersionId: string) {
  return useQuery({
    queryKey: [...batchParsingKey(fileVersionId), "availability"],
    queryFn: async (): Promise<FieldAvailabilityResponse | null> => {
      const { data, response, error } = await api.GET(
        "/api/batches/{file_version_id}/field-availability",
        { params: { path: { file_version_id: fileVersionId } } },
      );
      if (data) return data;
      if (response.status === 409 && isErrorCode(error, "BATCH_NOT_PARSED")) return null;
      throw new Error(
        extractErrorMessage(error) ?? `读取字段可用性失败（HTTP ${response.status}）`,
      );
    },
  });
}

export function useSaveSchemaMapping(fileVersionId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (body: SaveSchemaMappingRequest): Promise<SaveSchemaMappingResponse> => {
      const { data, response, error } = await api.PUT("/api/schema-mappings", { body });
      if (!data) {
        throw new Error(
          extractErrorMessage(error) ?? `保存字段映射失败（HTTP ${response.status}）`,
        );
      }
      return data;
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({
        queryKey: [...batchParsingKey(fileVersionId), "mappings"],
      });
    },
  });
}

export function useParseBatch(fileVersionId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (mappingVersionId: string): Promise<ParseBatchResponse> => {
      const { data, response, error } = await api.POST("/api/batches/{file_version_id}/parse", {
        params: { path: { file_version_id: fileVersionId } },
        body: { mapping_version_id: mappingVersionId },
      });
      if (!data) {
        throw new Error(extractErrorMessage(error) ?? `解析批次失败（HTTP ${response.status}）`);
      }
      return data;
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: batchParsingKey(fileVersionId) });
      void queryClient.invalidateQueries({ queryKey: [...BATCHES_KEY, fileVersionId] });
      void queryClient.invalidateQueries({ queryKey: BATCHES_KEY, exact: true });
    },
  });
}
