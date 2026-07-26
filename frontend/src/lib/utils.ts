import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

/**
 * 合并 className。
 *
 * `clsx` 负责条件拼接，`twMerge` 负责消解 Tailwind 的冲突类
 * （后写的 `px-4` 覆盖先写的 `px-2`，而不是两条都留在 class 串里由
 * CSS 顺序碰运气决定）。shadcn 全部组件都依赖这个函数。
 */
export function cn(...inputs: ClassValue[]): string {
  return twMerge(clsx(inputs));
}
