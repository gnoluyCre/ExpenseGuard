import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  api,
  apiErrorMessage,
  type ReportItemPage,
  type ReportOverview,
  type ReportParseErrorPage,
  type ReportSummary,
  type ReportExport,
} from "@/api/client";

export const reportKey = (fileVersionId: string) => ["report", fileVersionId] as const;

export function useBatchReport(fileVersionId: string, enabled = true) {
  return useQuery({
    queryKey: reportKey(fileVersionId),
    enabled,
    retry: false,
    queryFn: async (): Promise<ReportOverview | null> => {
      const { data, error, response } = await api.GET("/api/batches/{file_version_id}/report", {
        params: { path: { file_version_id: fileVersionId } },
      });
      if (response.status === 404) return null;
      if (!data)
        throw new Error(apiErrorMessage(error) ?? `读取报告失败（HTTP ${response.status}）`);
      return data;
    },
  });
}

export function useGenerateReport(fileVersionId: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: async (): Promise<ReportSummary> => {
      const key = crypto.randomUUID();
      const { data, error, response } = await api.POST("/api/batches/{file_version_id}/reports", {
        params: {
          path: { file_version_id: fileVersionId },
          header: { "Idempotency-Key": key },
        },
      });
      if (!data)
        throw new Error(apiErrorMessage(error) ?? `生成报告失败（HTTP ${response.status}）`);
      return data;
    },
    onSuccess: () => void client.invalidateQueries({ queryKey: reportKey(fileVersionId) }),
  });
}

export function useReportItems(reportId: string | null, offset: number) {
  return useQuery({
    queryKey: ["report-items", reportId, offset],
    enabled: reportId !== null,
    queryFn: async (): Promise<ReportItemPage> => {
      if (!reportId) throw new Error("缺少报告 ID");
      const { data, error, response } = await api.GET("/api/reports/{report_id}/items", {
        params: {
          path: { report_id: reportId },
          query: { limit: 25, offset, sort_by: "default", direction: "asc" },
        },
      });
      if (!data)
        throw new Error(apiErrorMessage(error) ?? `读取关注项失败（HTTP ${response.status}）`);
      return data;
    },
  });
}

export function useReportParseErrors(reportId: string | null) {
  return useQuery({
    queryKey: ["report-parse-errors", reportId],
    enabled: reportId !== null,
    queryFn: async (): Promise<ReportParseErrorPage> => {
      if (!reportId) throw new Error("缺少报告 ID");
      const { data, error, response } = await api.GET("/api/reports/{report_id}/parse-errors", {
        params: { path: { report_id: reportId }, query: { limit: 200, offset: 0 } },
      });
      if (!data)
        throw new Error(apiErrorMessage(error) ?? `读取解析错误失败（HTTP ${response.status}）`);
      return data;
    },
  });
}

export function useCreateReportExport(reportId: string | null) {
  return useMutation({
    mutationFn: async (): Promise<ReportExport> => {
      if (!reportId) throw new Error("缺少报告 ID");
      const { data, error, response } = await api.POST("/api/reports/{report_id}/exports", {
        params: {
          path: { report_id: reportId },
          header: { "Idempotency-Key": crypto.randomUUID() },
        },
      });
      if (!data)
        throw new Error(apiErrorMessage(error) ?? `生成 XLSX 失败（HTTP ${response.status}）`);
      return data;
    },
  });
}

export async function downloadReportExport(exportId: string): Promise<void> {
  const { data, response } = await api.GET("/api/report-exports/{export_id}/download", {
    params: { path: { export_id: exportId } },
    parseAs: "blob",
  });
  const payload: unknown = data;
  if (!response.ok || !(payload instanceof Blob)) {
    throw new Error(`下载 XLSX 失败（HTTP ${response.status}）`);
  }
  const disposition = response.headers.get("Content-Disposition") ?? "";
  const encoded = disposition.match(/filename\*=UTF-8''([^;]+)/i)?.[1];
  const filename = encoded ? decodeURIComponent(encoded) : `expenseguard-${exportId}.xlsx`;
  const url = URL.createObjectURL(payload);
  try {
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = filename;
    anchor.click();
  } finally {
    URL.revokeObjectURL(url);
  }
}
