import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api } from "@/api/client";
import type { components } from "@/api/schema";

export type RuleVersion = components["schemas"]["RuleVersionResponse"];
export type SaveRuleRequest = components["schemas"]["SaveRuleRequest"];
export type SaveRuleResponse = components["schemas"]["SaveRuleResponse"];

export const RULES_KEY = ["rules"] as const;

export function useRules() {
  return useQuery({
    queryKey: RULES_KEY,
    queryFn: async (): Promise<RuleVersion[]> => {
      const { data, error, response } = await api.GET("/api/rules", {
        params: { query: { latest_only: false } },
      });
      if (!data) {
        throw new Error(errorMessage(error) ?? `读取规则失败（HTTP ${response.status}）`);
      }
      return data;
    },
  });
}

export interface SaveRuleResult {
  rule: SaveRuleResponse;
  created: boolean;
}

export function useSaveRule() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (body: SaveRuleRequest): Promise<SaveRuleResult> => {
      const { data, error, response } = await api.PUT("/api/rules", { body });
      if (!data) {
        throw new Error(errorMessage(error) ?? `保存规则失败（HTTP ${response.status}）`);
      }
      return { rule: data, created: response.status === 201 };
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: RULES_KEY });
    },
  });
}

function errorMessage(error: unknown): string | undefined {
  if (typeof error !== "object" || error === null) return undefined;

  const wrapped = (error as { error?: unknown }).error;
  if (typeof wrapped === "object" && wrapped !== null) {
    const message = (wrapped as { message?: unknown }).message;
    if (typeof message === "string") return message;
  }

  const detail = (error as { detail?: unknown }).detail;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    const messages = detail.flatMap((entry) => {
      if (typeof entry !== "object" || entry === null) return [];
      const message = (entry as { msg?: unknown }).msg;
      return typeof message === "string" ? [message] : [];
    });
    if (messages.length > 0) return messages.join("；");
  }
  return undefined;
}
