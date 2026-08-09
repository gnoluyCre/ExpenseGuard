import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  api,
  apiErrorMessage,
  type CapabilityStatus,
  type DetectionConfigCreateRequest,
  type DetectorKind,
} from "@/api/client";
import { BATCHES_KEY } from "@/batches/useBatches";

export const detectionConfigKey = ["detection", "configs"] as const;
export const batchDetectionKey = (fileVersionId: string) =>
  [...BATCHES_KEY, fileVersionId, "detection"] as const;

function requestKey(prefix: string): string {
  return `${prefix}-${crypto.randomUUID()}`;
}

export function useDetectionConfigs(limit = 50, offset = 0) {
  return useQuery({
    queryKey: [...detectionConfigKey, limit, offset],
    queryFn: async () => {
      const { data, error, response } = await api.GET("/api/detection/configs", {
        params: { query: { limit, offset } },
      });
      if (!data)
        throw new Error(
          apiErrorMessage(error) ?? `读取关联检测配置失败（HTTP ${response.status}）`,
        );
      return data;
    },
  });
}

export function useCreateDetectionConfig() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (body: DetectionConfigCreateRequest) => {
      const { data, error, response } = await api.PUT("/api/detection/configs", {
        params: { header: { "Idempotency-Key": requestKey("profile") } },
        body,
      });
      if (!data)
        throw new Error(
          apiErrorMessage(error) ?? `保存关联检测配置失败（HTTP ${response.status}）`,
        );
      return data;
    },
    onSuccess: async () => queryClient.invalidateQueries({ queryKey: detectionConfigKey }),
  });
}

export function useBatchDetection(fileVersionId: string) {
  return useQuery({
    queryKey: batchDetectionKey(fileVersionId),
    queryFn: async () => {
      const { data, error, response } = await api.GET("/api/batches/{file_version_id}/detection", {
        params: { path: { file_version_id: fileVersionId } },
      });
      if (!data)
        throw new Error(
          apiErrorMessage(error) ?? `读取关联检测快照失败（HTTP ${response.status}）`,
        );
      return data;
    },
  });
}

export function useRunDetection(fileVersionId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async () => {
      const { data, error, response } = await api.POST("/api/batches/{file_version_id}/detect", {
        params: {
          path: { file_version_id: fileVersionId },
          header: { "Idempotency-Key": requestKey("run") },
        },
      });
      if (!data)
        throw new Error(apiErrorMessage(error) ?? `执行关联检测失败（HTTP ${response.status}）`);
      return data;
    },
    onSuccess: async () =>
      queryClient.invalidateQueries({ queryKey: batchDetectionKey(fileVersionId) }),
  });
}

export interface FindingFilters {
  detector?: DetectorKind;
  capabilityStatus?: CapabilityStatus;
  offset: number;
  limit: number;
}

export function useCorrelationFindings(runId: string | null, filters: FindingFilters) {
  return useQuery({
    queryKey: ["detection", "runs", runId, "findings", filters],
    enabled: runId !== null,
    queryFn: async () => {
      if (!runId) throw new Error("缺少关联检测运行标识");
      const { data, error, response } = await api.GET("/api/detection-runs/{run_id}/findings", {
        params: {
          path: { run_id: runId },
          query: {
            detector: filters.detector ?? null,
            capability_status: filters.capabilityStatus ?? null,
            sort_by: "default",
            limit: filters.limit,
            offset: filters.offset,
          },
        },
      });
      if (!data)
        throw new Error(apiErrorMessage(error) ?? `读取统计候选失败（HTTP ${response.status}）`);
      return data;
    },
  });
}

export function useCorrelationFinding(findingId: string | null, rowOffset: number, rowLimit = 50) {
  return useQuery({
    queryKey: ["detection", "findings", findingId, rowOffset, rowLimit],
    enabled: findingId !== null,
    queryFn: async () => {
      if (!findingId) throw new Error("缺少统计候选标识");
      const { data, error, response } = await api.GET("/api/correlation-findings/{finding_id}", {
        params: {
          path: { finding_id: findingId },
          query: { row_limit: rowLimit, row_offset: rowOffset },
        },
      });
      if (!data)
        throw new Error(apiErrorMessage(error) ?? `读取候选证据失败（HTTP ${response.status}）`);
      return data;
    },
  });
}
