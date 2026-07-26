import { useQuery } from "@tanstack/react-query";

import { api, type ReadinessResponse } from "@/api/client";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";

/**
 * 就绪探针页面。
 *
 * 后端未就绪时返回 **503 并带完整的依赖明细**，所以这里不能把非 2xx
 * 一律当成「请求失败」——那恰好会丢掉最需要看的信息（哪个依赖挂了）。
 */
function useReadiness() {
  return useQuery({
    queryKey: ["health", "ready"],
    queryFn: async (): Promise<ReadinessResponse> => {
      const { data, response } = await api.GET("/api/health/ready");
      if (data) return data;
      throw new Error(`就绪探针无响应体（HTTP ${response.status}）`);
    },
    refetchInterval: 15_000,
  });
}

export function HealthPage() {
  const { data, isPending, isError, error } = useReadiness();

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold">系统状态</h1>
        <p className="text-sm text-muted-foreground">每 15 秒自动刷新</p>
      </div>

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-3">
            整体就绪
            {data ? (
              <Badge variant={data.ready ? "secondary" : "destructive"}>
                {data.ready ? "就绪" : "未就绪"}
              </Badge>
            ) : null}
          </CardTitle>
          <CardDescription>各依赖逐个探测，任一不可用即整体未就绪</CardDescription>
        </CardHeader>
        <CardContent>
          {isPending && <p className="text-sm text-muted-foreground">探测中…</p>}
          {isError && (
            <p role="alert" className="text-sm text-destructive">
              {error.message}
            </p>
          )}
          {data && (
            <ul className="divide-y">
              {data.dependencies.map((dependency) => (
                <li key={dependency.name} className="flex items-center gap-3 py-2.5">
                  <span className="w-28 font-mono text-sm">{dependency.name}</span>
                  <Badge variant={dependency.status === "up" ? "secondary" : "destructive"}>
                    {dependency.status}
                  </Badge>
                  {dependency.detail && (
                    <span className="text-sm text-muted-foreground">{dependency.detail}</span>
                  )}
                </li>
              ))}
            </ul>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
