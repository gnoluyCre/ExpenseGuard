import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api, apiErrorCode, apiErrorMessage, type GradingConfigCreateRequest } from "@/api/client";

export const gradingConfigKey = ["grading", "configs"] as const;

export class GradingConfigApiError extends Error {
  readonly status: number;
  readonly code: string | undefined;

  constructor(message: string, status: number, code?: string) {
    super(message);
    this.name = "GradingConfigApiError";
    this.status = status;
    this.code = code;
  }
}

function idempotencyKey(): string {
  return `grading-config-${crypto.randomUUID()}`;
}

export function useCurrentGradingConfig() {
  return useQuery({
    queryKey: [...gradingConfigKey, "current"],
    queryFn: async () => {
      const { data, error, response } = await api.GET("/api/v1/grading-configs/current");
      if (response.status === 404) return null;
      if (!data) {
        throw new GradingConfigApiError(
          apiErrorMessage(error) ?? `读取当前二维分级配置失败（HTTP ${response.status}）`,
          response.status,
          apiErrorCode(error),
        );
      }
      return data;
    },
  });
}

export function useGradingConfigHistory(limit = 20, offset = 0) {
  return useQuery({
    queryKey: [...gradingConfigKey, "history", limit, offset],
    queryFn: async () => {
      const { data, error, response } = await api.GET("/api/v1/grading-configs", {
        params: { query: { limit, offset } },
      });
      if (!data) {
        throw new GradingConfigApiError(
          apiErrorMessage(error) ?? `读取二维分级配置历史失败（HTTP ${response.status}）`,
          response.status,
          apiErrorCode(error),
        );
      }
      return data;
    },
  });
}

export function useCreateGradingConfig() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (body: GradingConfigCreateRequest) => {
      const { data, error, response } = await api.POST("/api/v1/grading-configs", {
        params: { header: { "Idempotency-Key": idempotencyKey() } },
        body,
      });
      if (!data) {
        throw new GradingConfigApiError(
          apiErrorMessage(error) ?? `保存二维分级配置失败（HTTP ${response.status}）`,
          response.status,
          apiErrorCode(error),
        );
      }
      return data;
    },
    onSuccess: async () => queryClient.invalidateQueries({ queryKey: gradingConfigKey }),
  });
}
