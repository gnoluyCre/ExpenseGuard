import { fileURLToPath, URL } from "node:url";

import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// Tailwind v4 走 Vite 插件 + CSS-first 配置：不再需要 tailwind.config.js
// 与 postcss.config.js。这是与绝大多数 v3 教程最大的分歧点，照 v3 教程
// 建那两个文件不会报错，只会静默不生效。
export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      // 别名必须**同时**配在这里和 tsconfig.app.json 的 paths 里。
      // 只配 tsconfig 会「tsc 过但 build 挂」，只配这里则编辑器全红。
      "@": fileURLToPath(new URL("./src", import.meta.url)),
    },
  },
  server: {
    port: 5173,
    proxy: {
      // 开发期把 /api 代理到后端，使前后端**同源**。
      //
      // 这不是图方便：会话 cookie 是 HttpOnly + SameSite=Lax 的。跨源直连
      // 后端要同时满足 CORS 的 allow_credentials、cookie 的 SameSite=None
      // 且必须 Secure（于是 dev 还得上 HTTPS）——Phase 1 就陷进这套泥潭
      // 不值得。同源之下这些约束全部消失。
      //
      // 后端的 CORS 白名单仍然配着，供非 proxy 场景（如独立部署的前端）使用。
      "/api": {
        target: "http://127.0.0.1:8000",
        changeOrigin: false,
      },
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
    css: false,
  },
});
