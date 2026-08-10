import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api, apiErrorCode, apiErrorMessage } from "@/api/client";

export const investigationCapabilityKey = ["investigations", "capability"] as const;
export const investigationHistoryKey = (detectionRunId: string, findingId: string) =>
  ["investigations", detectionRunId, findingId, "history"] as const;

function requestKey(): string {
  return `investigation-${crypto.randomUUID()}`;
}

export function useInvestigationCapability() {
  return useQuery({
    queryKey: investigationCapabilityKey,
    queryFn: async () => {
      const { data, error, response } = await api.GET("/api/v1/investigations/capability");
      if (!data)
        throw new Error(
          apiErrorMessage(error) ?? `读取异常取证能力失败（HTTP ${response.status}）`,
        );
      return data;
    },
  });
}

export function useInvestigationHistory(detectionRunId: string, findingId: string) {
  return useQuery({
    queryKey: investigationHistoryKey(detectionRunId, findingId),
    enabled: detectionRunId.length > 0 && findingId.length > 0,
    queryFn: async () => {
      const { data, error, response } = await api.GET(
        "/api/v1/detection-runs/{detection_run_id}/findings/{correlation_finding_id}/investigations",
        {
          params: {
            path: {
              detection_run_id: detectionRunId,
              correlation_finding_id: findingId,
            },
            query: { limit: 20, offset: 0 },
          },
        },
      );
      if (!data)
        throw new Error(apiErrorMessage(error) ?? `读取调查历史失败（HTTP ${response.status}）`);
      return data;
    },
  });
}

export function useRunInvestigation(detectionRunId: string, findingId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async () => {
      const { data, error, response } = await api.POST(
        "/api/v1/detection-runs/{detection_run_id}/findings/{correlation_finding_id}/investigations",
        {
          params: {
            path: {
              detection_run_id: detectionRunId,
              correlation_finding_id: findingId,
            },
            header: { "Idempotency-Key": requestKey() },
          },
        },
      );
      if (!data) {
        const code = apiErrorCode(error);
        throw new Error(
          apiErrorMessage(error) ??
            `${code ? `${code}：` : ""}启动异常取证失败（HTTP ${response.status}）`,
        );
      }
      return data;
    },
    onSuccess: async () =>
      queryClient.invalidateQueries({
        queryKey: investigationHistoryKey(detectionRunId, findingId),
      }),
  });
}
