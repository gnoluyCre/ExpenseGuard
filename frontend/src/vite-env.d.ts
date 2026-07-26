/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** 前后端分开部署时的 API 基址。留空则走同源（Vite proxy / 反向代理）。 */
  readonly VITE_API_BASE_URL?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
