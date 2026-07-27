# 系统记忆与上下文 🧠
<!--
AGENTS:在每个重要里程碑、结构性变更或修复 bug 后更新本文件。
若历史上下文仍然相关,请勿删除;对较早的已完成项进行压缩。
-->

## 🏗️ 当前阶段与目标
**当前任务:阶段 1 已完成(CP0–CP4)。** 后端 `49 passed, 1 skipped`,前端 `8 passed`;ruff / mypy strict(`app scripts`)/ alembic check / oxlint / tsc / prettier / `pre-commit run --all-files` 全绿。完整落地记录与踩坑清单见 `specs/001-phase1-foundation.md`。

- CP0 仓库重置(干净历史、`.gitignore` 脱敏排除)
- CP1 后端地基(uv + 18 张表 + Alembic 三层隔离)
- CP2 幂等原语与恢复测试(项目 #1 优先级,含反向验证)
- CP3 认证、RBAC、租户隔离(含反向验证)
- CP4 前端垂直切片 + OpenAPI 契约门禁 + pre-commit/CI + 合成数据生成器(含反向验证)

**下一步:阶段 2 F1 · Excel 导入与文件版本管理。** 串行依赖链 F1→F2→F3→F4→F5,不得跳步。
`process_row_once` 至今**一个生产调用方都没有** —— 第一个是 F3,刻意如此。

**开工前必读的两件事:**
1. 契约同步是硬要求:改了 Pydantic 模型 → `cd backend && uv run python scripts/export_openapi.py` + `cd frontend && npm run gen:api`,两个生成物都要提交,否则 CI 的 contract job 直接红。
2. 评测门禁已就位但处于待命:`backend/evals/baseline.json` 的 `thresholds` 一填数值就自动开始阻断,**不需要改任何 workflow YAML**。

**遗留缺口（不阻塞 F1，但别忘）:** W0 spike 未做 —— `docker-compose.models.yml` 的 embedding 镜像未实测、离线模型供给路径未验证;CI 尚未在 GitHub 上真实跑过（仓库未 push）。

## 📂 架构决策
*(把构建过程中做出的具体选择记录在此,便于后续 agent 遵循)*
- 2026-07-27 — **开发工具入口从 ClaudeCode 切换为 Codex。** `AGENTS.md` 继续作为唯一事实来源,Codex 原生读取;ClaudeCode 专用的 `.claude/` 与 `CLAUDE.md` 退役。后续新会话按 `AGENTS.md` → `MEMORY.md` → 当前 `specs/` → `agent_docs/` 的顺序加载上下文。
- 2026-07-27 — 形态选 **agent-in-workflow**:确定性 workflow 主干 + 单点 ReAct 取证 agent。原因:审计结论必须可复现、经得起内审质询,不能把整条链路交给概率性推理。不采用多智能体协作(研究阶段明确排除)。
- 2026-07-27 — 编排选 **LangGraph + PostgresSaver**。已知风险:节点从中断恢复时从头重放,`interrupt()` 前副作用会重复 → 用**行级幂等结果表**兜底(W1 最高优先级工程项,非可选优化)。
- 2026-07-27 — 向量库选 **Qdrant**。原因:检索必然带复合过滤(tenant + 制度版本 + 费用发生日落在生效区间),payload 过滤在 HNSW 遍历内执行是其相对优势场景。退路:`VectorStore` 接口抽象,可切回 PGVector。
- 2026-07-27 — 模型层用 **OpenAI 兼容抽象**(云 API ⇄ vLLM,切换成本=两个配置项)。embedding/rerank **真自托管**(检索层数据不出内网,硬承诺);强模型 MVP 走云 API,W0 做一次性自托管可行性验证后释放 GPU。「数据不出内网」在 LLM 层为**有条件成立**(依赖脱敏 + 合成数据),文档必须如实表述。
- 2026-07-27 — 后端选 **FastAPI + Pydantic**。决定性理由:同一个 Pydantic 模型既作 XGrammar/function calling 约束解码 schema,又作 API 响应模型,消除双定义漂移。
- 2026-07-27 — **脱敏切面**为 Project Structure 第一设计目标:`core/` 通用框架层与 `tenants/`+`data/private/` 客户数据层物理隔离,脱敏 = 删目录而非重写。
- 2026-07-27 — 后端包布局定为 **`backend/app/{api,core,db,synth}`**,比 TechDesign 结构图多一层 `app/`。理由:匹配 AGENTS.md 的 `uvicorn app.main:app`;单一可导入包让 Docker COPY 与 pytest rootdir 最简单;避免 `db`/`core` 这类通用顶层包名与 PyPI 同名包冲突。
- 2026-07-27 — 新增 `core/security/` 与 `core/tenancy/` 两个 TechDesign 未列的子模块。认证与租户过滤属通用框架层,放 `api/` 会违反「路由层只处理 request/response」。
- 2026-07-27 — **18 张表一次建完**,分 0001(7 张全保真)/ 0002(11 张骨架)两个迁移。AGENTS.md 禁的是「功能」不是「表」——建空表不产生任何用户可见行为。决定性理由是受保护区域条款:约束若推迟到功能 PR 才第一次出现,「发明约束」与「实现功能」会发生在同一次改动中,那恰是约束最容易被写弱的时刻。
- 2026-07-27 — 全部业务表**冗余 `tenant_id`** + 复合外键 `(file_version_id, tenant_id)`。冗余是必需的:租户过滤靠 `with_loader_criteria` 挂在带该列的模型上,没有列就挂不上过滤器。复合外键把「冗余值可能漂移」从代码纪律变成数据库不变式。
- 2026-07-27 — 测试库默认改用 docker-compose 预建的 `expenseguard_test`,testcontainers 降为 `USE_TESTCONTAINERS=1` 可选路径。原因见下方已知问题。
- 2026-07-27 — **所有业务表必须继承 `TenantScopedMixin`,而不是自己写一个同样的 `tenant_id` 列。** 租户过滤靠 `issubclass(Model, TenantScopedMixin)` 匹配实体,匹配不上就**静默不过滤**。CP3 时 `FileVersion`/`AppUser`/`UserSession` 就栽在这里——三张表列写得完全正确、DDL 无误、改继承后 `alembic check` 仍零漂移,但跨租户数据一直在泄漏。
- 2026-07-27 — 租户过滤的绕过做成**显式 execution option**(`skip_tenant_filter_options()`),而非「守卫在某些情况下自动放行」。全系统只用于两处(按 tenant_slug+username 查用户、按 token 哈希查会话)。目的是让绕过行为可 `grep`、可评审。
- 2026-07-27 — **Windows 上后端必须用 `uv run python -m app` 启动**,不能直接 `uvicorn app.main:app`。见下方已知问题。
- 2026-07-27（CP4）— **前端定为 React 19 + Vite 8 + TS 5.9 + oxlint + Prettier**,与批准过的计划书写的「React 18 + ESLint」不同。计划书锁 18 的理由是「shadcn CLI 按 19 生成会有 peer dep 冲突」,而 create-vite 9 与 shadcn 现在默认就是 19 —— 锁 18 反而**制造**那个冲突,前提已失效。lint 器同理:官方模板已换 oxlint。**AGENTS.md 的编码约定已同步修改** —— 偏离要落到事实来源里,不能留成「文档说 A、代码是 B」。
- 2026-07-27（CP4）— **前端 TypeScript 锁 `~5.9`,不跟 create-vite 升 TS 6。** `openapi-typescript` 的 peer 是 `^5.x`;用 `--legacy-peer-deps` 硬装的话,codegen 会在运行时撞上 TS 6 的 API 变更 —— 那正是契约门禁最不该出问题的地方。上游放行后再抬。
- 2026-07-27（CP4）— 路由用 **`react-router` v8**,不用 `react-router-dom`。后者最新版落在 CVE 区间(7.12.0–8.2.0,RSC CSRF),且 v8 已把它并入主包。
- 2026-07-27（CP4）— **契约（`openapi.json` + `frontend/src/api/schema.d.ts`）两个生成物都进仓库**,CI 重新生成后 `git diff --exit-code`。生成物进仓库通常是坏味道,这里是刻意的:它把「后端改了字段、前端没跟」从运行时错误变成流水线错误。导出脚本必须 `sort_keys=True`,否则 dict 顺序抖动会制造满屏噪音 diff,门禁随即被当成噪音关掉。
- 2026-07-27（CP4）— **前端菜单由权限驱动,不由角色 if-else 驱动。** 后端 `ROLE_PERMISSIONS` 已经是数据,前端再抄一份 `if (role === "configurator")` 就制造了第二份事实来源。前端只认 `/api/auth/me` 返回的 `permissions`,且这只是**体验**层——真正的鉴权在服务端每个端点上。
- 2026-07-27（CP4）— **评测门禁做成数据驱动的常驻 job**,不是 `if: false` 也不是注释掉。`backend/evals/baseline.json` 的 `thresholds` 为空则测试 `skip`,填了就自动阻断。⚠️ `pytest -m eval` 选中 0 个用例时退出码是 **5**,job 会假红 —— 所以 `tests/eval/test_eval_gate.py` 里必须始终有一个**真实存在**的 `@pytest.mark.eval` 用例。
- 2026-07-27（CP4）— **不装 `mixed-line-ending` 钩子**:`.gitattributes` 的 `* text=auto eol=lf` 已在入库时统一行尾,两者管的不是同一层(一个管仓库内容,一个管本地工作区),叠加只会制造大片无意义 diff。

## 🐛 已知问题与坑点
*(把当前 bug 或临时绕过方案记录在此)*
- **脱敏一致性陷阱:** 同一主体在不同行必须映射为**同一 token**,否则跨行关联检测失效 — 这是脱敏实现中最易出错的一点。
- **采样偏差(工程必读):** 若只标注被拦截样本,漏放率在数学上不可测量(reject inference / selection bias)。**从第一个批次起就必须对被放行样本随机抽检**(`sampling_audit`),否则「召回 ≥95%」核心指标不可测。
- **引用忠实 ≠ 引用正确:** RAG 场景存在 post-rationalized 引用(先凭参数记忆生成结论再补表面来源)。机械式逐字比对是工程上唯一低成本防线,不可省略。
- **阈值数值为初始假设:** 召回 ≥95% / 误拦 ≤20% / 人工下降 ≥50% 均非行业基准,须用自有复核数据按 Neyman-Pearson / 代价敏感方法重新标定,不按 F1 优化。
- **分级漏斗 60/15/20 为设计假设**,需第一个真实批次校准。

### Windows 环境特有的坑（CP1/CP2 实测,详见 `specs/001-phase1-foundation.md`）

- **psycopg 异步模式无法在 `ProactorEventLoop` 上运行**,而它是 Windows 上 asyncio 的默认事件循环 —— 不处理的话整个异步数据库层直接不可用。生产侧由 `app/asyncio_compat.py` 处理,测试侧用 pytest-asyncio 的 `pytest_asyncio_loop_factories` hook。
- **`.ini` 文件必须保持 ASCII**:configparser 用 locale 编码(此机 GBK)读取,UTF-8 中文注释会让 `alembic` 启动即崩。
- **testcontainers 在 Windows + Docker Desktop 下挂死**:容器正常起、迁移也跑完,但之后 pytest 无连接、无输出、无进展。根因未定位,已降级为可选路径。
- **不要用 rollback fixture 测幂等**:rollback 掉的东西从未提交过,而幂等验证的正是已提交副作用;更糟的是它会**静默通过**——上一轮残留数据会让本该失败的测试恰好变绿。
- **pydantic-settings 对 `list[str]` 字段先做 `json.loads`**,发生在 before-validator 之前;要支持逗号分隔需加 `NoDecode`。
- **uvicorn 在 Windows 硬编码 `ProactorEventLoop`**(`uvicorn/loops/asyncio.py`),psycopg 异步在其上无法工作。且 uvicorn 0.36+ 用 `asyncio.run(..., loop_factory=...)`,**`set_event_loop_policy` 对它完全无效**。这个坑只在**不带 `--reload`** 时出现(reload 走子进程 → SelectorEventLoop),是典型的「开发正常、生产挂死」。修法是 `app/__main__.py` 显式传循环工厂。
- **`get_db` 在异常传播时会 rollback**,所以任何「失败路径上的审计日志」都必须在抛异常前单独 `commit()`,否则最该留痕的场景反而一条都不留。

### 前端环境特有的坑（CP4 实测,详见 `specs/001-phase1-foundation.md`）

- **路径别名要配三处**:`vite.config.ts` 的 `resolve.alias`、`tsconfig.app.json` 的 `paths`、以及**根 `tsconfig.json` 的 `paths`**。前两处缺一会「tsc 过但 build 挂」或反之;第三处是 shadcn CLI 读的 —— 缺了它 `npx shadcn add` 会把组件写进一个名叫 `@` 的真实目录,且**不报任何错**。
- **Tailwind v4 没有 `tailwind.config.js`**,主题在 CSS 里用 `@theme` 声明。照 v3 教程建那个文件不会报错,只会静默不生效。
- **openapi-fetch 的 `createClient()` 在模块导入时就抓走 `globalThis.fetch`。** 后果极其隐蔽:测试里 `vi.stubGlobal("fetch", ...)` 完全不生效,请求真发出去、真失败,于是「未登录应跳转」这类断言**因为网络错误而通过** —— 一个什么都没测到的测试和一个测对了的测试，绿灯长得一模一样。修法是客户端延迟解析 fetch。
- **openapi-fetch 传给 fetch 的是 `Request` 对象**,不是 `(url, init)` 二元组。测试桩当字符串处理会得到 `"[object Request]"`。
- **`baseUrl` 不要拼 `/api`** —— schema 里的路径本身已含它,会变成 `/api/api/...`;但也不能留空 —— undici 的 `Request` 不接受相对 URL,jsdom 下全部报 `Failed to parse URL`。取 `window.location.origin`。
- **`js-yaml` 的 npm override 必须锁 `4.3.0`**,不能升 `^5`:`@redocly/openapi-core`(openapi-typescript 的依赖)用的是 v4 的 `types.merge` API。
- **`npm run test` 必须写成 `vitest run`**,不带 `run` 会在 CI 里进 watch 模式挂死。

### 已完成阶段的测试统计(便于新会话快速判断状态)

- `cd backend && uv run pytest` 应为 **49 passed, 1 skipped**（skip 的是常驻待命的评测门禁）
- `cd frontend && npm run test` 应为 **8 passed**

若数量对不上,说明环境或代码有问题,先排查再继续。

## 📜 已完成阶段
- [x] 初始脚手架（monorepo + uv + Vite）
- [x] 数据库 schema 创建（18 张表,含 `row_result` 幂等表与 `audit_log` 追加写触发器）
- [x] 认证集成（server-side session + RBAC 三角色 + 租户过滤 fail-closed）
- [x] 前端垂直切片（登录 / 路由守卫 / 三角色外壳 / 系统状态页）
- [x] OpenAPI 契约门禁 + pre-commit + GitHub Actions CI
- [x] 合成数据生成器骨架（确定性 + 标签物理分离）
