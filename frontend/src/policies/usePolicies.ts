import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  api,
  apiErrorMessage,
  type BindingCandidate,
  type BindingHistory,
  type PolicyDocument,
  type PolicyFamily,
} from "@/api/client";
import type { components } from "@/api/schema";

export const POLICIES_KEY = ["policies"] as const;

export function usePolicyFamilies() {
  return useQuery({
    queryKey: POLICIES_KEY,
    queryFn: async (): Promise<PolicyFamily[]> => {
      const { data, error, response } = await api.GET("/api/policies/families");
      if (!data)
        throw new Error(apiErrorMessage(error) ?? `读取制度失败（HTTP ${response.status}）`);
      return [...data];
    },
  });
}

export function usePolicyDocument(documentId: string | null) {
  return useQuery({
    queryKey: [...POLICIES_KEY, "document", documentId],
    enabled: documentId !== null,
    queryFn: async (): Promise<PolicyDocument> => {
      if (!documentId) throw new Error("缺少制度文档 ID");
      const { data, error, response } = await api.GET("/api/policies/documents/{document_id}", {
        params: { path: { document_id: documentId } },
      });
      if (!data)
        throw new Error(apiErrorMessage(error) ?? `读取制度详情失败（HTTP ${response.status}）`);
      return data;
    },
  });
}

export function useCreatePolicyFamily() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: async (body: components["schemas"]["CreatePolicyFamilyRequest"]) => {
      const { data, error, response } = await api.POST("/api/policies/families", { body });
      if (!data)
        throw new Error(apiErrorMessage(error) ?? `创建制度族失败（HTTP ${response.status}）`);
      return data;
    },
    onSuccess: () => void client.invalidateQueries({ queryKey: POLICIES_KEY }),
  });
}

interface UploadPolicyInput {
  familyId: string;
  title: string;
  version: string;
  effectiveDate: string;
  expiryDate?: string;
  file: File;
}

export function useUploadPolicy() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: async (input: UploadPolicyInput) => {
      const body: components["schemas"]["Body_policies_upload_document"] = {
        family_id: input.familyId,
        title: input.title,
        version: input.version,
        effective_date: input.effectiveDate,
        expiry_date: input.expiryDate || null,
        file: input.file as unknown as string,
      };
      const { data, error, response } = await api.POST("/api/policies/documents", {
        body,
        bodySerializer: (value) => {
          const form = new FormData();
          form.set("family_id", value.family_id);
          form.set("title", value.title);
          form.set("version", value.version);
          form.set("effective_date", value.effective_date);
          if (value.expiry_date) form.set("expiry_date", value.expiry_date);
          form.set("file", input.file);
          return form;
        },
      });
      if (!data)
        throw new Error(apiErrorMessage(error) ?? `上传制度失败（HTTP ${response.status}）`);
      return data;
    },
    onSuccess: () => void client.invalidateQueries({ queryKey: POLICIES_KEY }),
  });
}

export function usePublishPolicy() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: async (documentId: string) => {
      const { data, error, response } = await api.POST(
        "/api/policies/documents/{document_id}/publish",
        { params: { path: { document_id: documentId } } },
      );
      if (!data)
        throw new Error(apiErrorMessage(error) ?? `发布制度失败（HTTP ${response.status}）`);
      return data;
    },
    onSuccess: () => void client.invalidateQueries({ queryKey: POLICIES_KEY }),
  });
}

export function usePolicyCandidates(ruleId: string | null, expenseDate: string) {
  return useQuery({
    queryKey: ["policy-candidates", ruleId, expenseDate],
    enabled: ruleId !== null && expenseDate !== "",
    queryFn: async (): Promise<BindingCandidate[]> => {
      if (!ruleId) return [];
      const { data, error, response } = await api.GET(
        "/api/rules/{rule_config_id}/policy-candidates",
        { params: { path: { rule_config_id: ruleId }, query: { expense_date: expenseDate } } },
      );
      if (!data)
        throw new Error(apiErrorMessage(error) ?? `检索条款失败（HTTP ${response.status}）`);
      return [...data];
    },
  });
}

export function useBindingHistory(ruleId: string | null) {
  return useQuery({
    queryKey: ["policy-bindings", ruleId],
    enabled: ruleId !== null,
    queryFn: async (): Promise<BindingHistory[]> => {
      if (!ruleId) return [];
      const { data, error, response } = await api.GET(
        "/api/rules/{rule_config_id}/policy-bindings",
        { params: { path: { rule_config_id: ruleId } } },
      );
      if (!data)
        throw new Error(apiErrorMessage(error) ?? `读取绑定失败（HTTP ${response.status}）`);
      return [...data];
    },
  });
}

export function useSaveBinding(ruleId: string | null) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: async (body: components["schemas"]["SaveBindingsRequest"]) => {
      if (!ruleId) throw new Error("缺少规则版本 ID");
      const { data, error, response } = await api.POST(
        "/api/rules/{rule_config_id}/policy-bindings",
        { params: { path: { rule_config_id: ruleId } }, body },
      );
      if (!data)
        throw new Error(apiErrorMessage(error) ?? `保存绑定失败（HTTP ${response.status}）`);
      return data;
    },
    onSuccess: () => void client.invalidateQueries({ queryKey: ["policy-bindings", ruleId] }),
  });
}
