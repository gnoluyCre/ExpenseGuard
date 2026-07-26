import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";

/**
 * 尚未实现的功能页占位。
 *
 * 明确写出「属于哪个功能编号、在哪个阶段做」，而不是画一个假的空列表。
 * 空列表会被误读成「功能已上线但没有数据」——在给客户演示时这是很贵的误会。
 */
export function PlaceholderPage({ feature, title }: { feature: string; title: string }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>{title}</CardTitle>
        <CardDescription>{feature} · 尚未实现</CardDescription>
      </CardHeader>
      <CardContent className="text-sm text-muted-foreground">
        阶段 1 只交付地基（认证、幂等、多租户、CI）。本页所属功能在阶段 2 按 F1→F2→F3→F4→F5
        的串行依赖链依次实现，届时此占位会被替换。
      </CardContent>
    </Card>
  );
}
