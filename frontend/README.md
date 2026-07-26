# ExpenseGuard 前端

React 19 + Vite 8 + TypeScript(strict) + Tailwind v4 + shadcn/ui。

阶段 1 只交付**垂直切片**：登录、路由守卫、三角色外壳、系统状态页。
不含任何 F1–F8 业务页面。

## 命令

```bash
npm install
npm run dev          # 需要后端已在 127.0.0.1:8000 运行（见下）
npm run lint         # oxlint，--max-warnings=0
npm run format       # prettier 写入；format:check 只校验
npm run typecheck    # tsc -b --noEmit
npm run test         # vitest run（必须带 run，否则 CI 会挂在 watch 模式）
npm run build
npm run gen:api      # 从 ../openapi.json 重新生成 src/api/schema.d.ts
```

## 几个容易踩的地方

**别名要配三处。** `@/*` 必须同时出现在 `vite.config.ts` 的 `resolve.alias`、
`tsconfig.app.json` 的 `paths`、以及 `tsconfig.json` 的 `paths`。前两处缺一
会「tsc 过但 build 挂」或反之；第三处是 shadcn CLI 读的——缺了它
`npx shadcn add` 会把组件写进一个名叫 `@` 的真实目录，且不报任何错。

**Tailwind v4 没有 `tailwind.config.js`。** 主题在 `src/index.css` 里用
`@theme` 声明。照 v3 教程建那个配置文件不会报错，只会静默不生效。

**开发期依赖 Vite proxy。** `/api` 被代理到 `http://127.0.0.1:8000`，使前后端
同源，`HttpOnly` 会话 cookie 因此无需处理 CORS + SameSite。后端要先起：

```bash
cd backend && uv run python -m app
```

**契约来自后端。** `src/api/schema.d.ts` 由 `../openapi.json` 生成，两者都提交
进仓库。后端改了 schema 而没同步，CI 的 contract job 会红。

**`src/components/ui/` 是 shadcn registry 的产物**，会被 `shadcn add` 覆盖，
所以别在里面写业务逻辑。oxlint 对该目录关掉了 `only-export-components`。

**TypeScript 锁在 5.9。** `openapi-typescript` 的 peer 是 `^5.x`；升到 TS 6
会让类型生成直接失败。等上游放行后再抬。

**`js-yaml` 的 override 锁死 4.3.0。** 它是 `@redocly/openapi-core`（
`openapi-typescript` 的依赖）的传递依赖，升到 5 会让类型生成启动即
TypeError。见 `package.json` 里的 `_overrides_note`。
