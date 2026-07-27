# 系统记忆与上下文 🧠
<!--
AGENTS:在每个重要里程碑、结构性变更或修复 bug 后更新本文件。
若历史上下文仍然相关,请勿删除;对较早的已完成项进行压缩。
-->

## 🏗️ 当前阶段与目标
**当前任务:阶段 2 F3 · CP-F3.0 规格已完成。** 五类确定性规则、不可变规则快照、行级 verdict、租户全历史发票号查重、并发/幂等、API、权限、审计和 `0004` 迁移边界已在 `specs/004-phase2-f3-deterministic-validation.md` 固化。

- CP0 仓库重置(干净历史、`.gitignore` 脱敏排除)
- CP1 后端地基(uv + 18 张表 + Alembic 三层隔离)
- CP2 幂等原语与恢复测试(项目 #1 优先级,含反向验证)
- CP3 认证、RBAC、租户隔离(含反向验证)
- CP4 前端垂直切片 + OpenAPI 契约门禁 + pre-commit/CI + 合成数据生成器(含反向验证)

**下一步:CP-F3.1 · 持久化 schema 与 ORM。** 新增 `0004_f3_deterministic_validation.py`、不可变 validation run/dependency、file revision、规则/finding 持久化字段与租户复合约束；强类型 Pydantic 判别联合属于 CP-F3.2，不修改 `0001`–`0003`，不提前进入 F4/F5。
`process_row_once` 至今**一个生产调用方都没有** —— 第一个是 F3,刻意如此。

**开工前必读的两件事:**
1. 契约同步是硬要求:改了 Pydantic 模型 → `cd backend && uv run python scripts/export_openapi.py` + `cd frontend && npm run gen:api`,两个生成物都要提交,否则 CI 的 contract job 直接红。
2. 评测门禁已就位但处于待命:`backend/evals/baseline.json` 的 `thresholds` 一填数值就自动开始阻断,**不需要改任何 workflow YAML**。

**遗留缺口（不阻塞 F3，但别忘）:** W0 spike 未做 —— `docker-compose.models.yml` 的 embedding 镜像未实测、离线模型供给路径未验证;GitHub CI 远端状态待确认。

## 📂 架构决策
*(把构建过程中做出的具体选择记录在此,便于后续 agent 遵循)*
- 2026-07-28 — **F3 CP-F3.0–F3.5 共用 `specs/004-phase2-f3-deterministic-validation.md` 一份规范。** §1–§12 是唯一业务/接口定义，§13 给出五个实施契约，§14 只追加已经完成的落地事实；不为各 CP 创建重复规格，独立运维 runbook 也不得成为第二事实来源。
- 2026-07-27 — **F3 使用五类强类型规则配置，不开放任意 JSON Logic。** 限额、票种、时效、抬头和发票号查重分别使用冻结的 Pydantic 判别联合；阈值、允许集合和 OR-of-AND 精确例外均为数据，未知字段/运算符直接拒绝。
- 2026-07-27 — **F3 首次成功校验冻结规则集快照。** 每行按 `expense_date` 选择生效版本，规则集 manifest 同时绑定 `mapping_version_id`；同批重复调用只复用，新规则或新映射必须通过显式派生 `file_version` revision 应用，普通重复上传仍复用 revision 1。`row_result.rule_version` 存规则集指纹，finding 存具体规则 ID/版本。
- 2026-07-27 — **F3 缺依赖显式转人工，发票号按租户全历史精确查重。** inferred 默认可参与并携带 provenance，规则可要求 direct；verdict 优先级为 `flagged > manual_review > passed`。重复首条按 `(uploaded_at, file_version.id, row_no)` 稳定确定，连号/模糊匹配留在 F6。
- 2026-07-27 — **默认开发库已在可恢复备份后从 `0002` 升级到 `0003`。** 私有备份包含完整库、public schema 和 F2 定向数据三份 custom archive，均通过 `pg_restore --list` 与 SHA-256 交叉验证；升级后 `alembic check`、F2 外键、行数守恒、受保护唯一约束和审计触发器全部通过。
- 2026-07-27 — **F2 推断只读取已直接映射的统一字段，不读取任意原始列，也不支持推断链。** `constant` 首版只允许 `currency`；`literal_lookup` 使用严格判别联合，按配置顺序做 NFKC 字面量包含匹配并取首个命中。推断目标必须是未直接映射的可选字段。规格 §6.2 的 direct+inference 同时配置示例与 §5.1/§6.1 正文冲突，实现以正文互斥规则为准。
- 2026-07-27 — **F2 可用性按字段观察结果计数，而非按整行成功计数。** 某行因金额失败时，该行已成功规范化的日期仍计入日期 non-null_count；所有失败行仍在固定 `row_count` 分母中。这样既不排除坏行虚高比例，也不因无关字段错误低估目标字段质量。
- 2026-07-27 — **解析服务使用调用方外层事务 + 内部 SAVEPOINT。** `FOR UPDATE NOWAIT`、行结果替换、12 项可用性覆盖和 `file_version` 更新处于同一原子单元；API 依赖负责最终 commit/rollback。首次系统失败后的 `failed` 状态与失败审计需要独立短事务，留到 CP-F2.3，不在失败事务中勉强续写。
- 2026-07-27 — **F2 映射 PUT 使用租户父行锁串行分配全局版本号。** 规范化映射、四位小数阈值、币种别名和有序推断配置组成 canonical JSON 指纹；同表头最新指纹相同则返回 200 并复用，不创建版本或审计，内容变化才返回 201 追加版本。
- 2026-07-27 — **F2 解析审计与业务事务边界已固定。** 首次成功应用映射时，`batch.parse` 与解析结果同事务提交；同版本复用不重复审计。未分类系统异常先回滚解析事务，再用独立短事务追加 `batch.parse_failed`，payload 只含批次/映射 ID 与稳定错误分类，不含异常文本或报销 PII。
- 2026-07-27 — **API 输入校验统一返回 `{error:{code,message}}`。** FastAPI/Pydantic 请求校验使用 `REQUEST_VALIDATION_ERROR`；映射领域校验继续返回规格中的稳定 `MAPPING_*` 码，冲突和系统错误分别使用 409/500。
- 2026-07-27 — **F2 映射版本号采用租户内全局递增。** 原因是 0002 的 `unique(tenant_id, source_column, version)` 属于受保护约束；父版本号全局化后，子条目的兼容 `version` 可与父版本一致且不阻塞不同表头复用相同源列。映射版本不可变，legacy `confidence` 仅保留历史值，新写入为 null。
- 2026-07-27 — **`.gitignore` 的模型权重规则必须精确放行 `backend/app/db/models/*.py`。** 旧的通用 `models/` 规则误伤 ORM 目录，曾导致模型文件完全不受 Git 跟踪；现已增加窄范围例外，不放行下载权重或私有数据。
- 2026-07-27 — **开发工具入口从 ClaudeCode 切换为 Codex。** `AGENTS.md` 继续作为唯一事实来源,Codex 原生读取;ClaudeCode 专用的 `.claude/` 与 `CLAUDE.md` 退役。后续新会话按 `AGENTS.md` → `MEMORY.md` → 当前 `specs/` → `agent_docs/` 的顺序加载上下文。
- 2026-07-27 — **F1 保持纯原始证据链导入,不启动 LangGraph workflow。** Excel 第一行只作为原始列头,数据行按物理行号写入 `expense_row.raw_json`;`parse_error` 默认为空。字段映射、金额/日期归一化、字段可用性探测与正式解析失败语义全部留到 F2。
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

- CP-F2.5 实测：`cd backend && uv run python -m pytest --basetemp <可写临时目录>` 为 **142 passed, 1 skipped**（skip 的是常驻待命的评测门禁）；其中 CP-F2.2 纯逻辑 **51 passed**、解析服务 PostgreSQL 集成 **6 passed**、CP-F2.3 API 集成 **14 passed**，解析包定向覆盖率 **91%**；迁移目录测试为 **20 passed**。
- CP-F2.5 实测：`cd frontend && npm run test` 为 **14 passed**；其中 CP-F2.4 批次工作流定向测试 **6 passed**。
- CP-F2.5 其余门禁：Ruff lint/格式检查、strict mypy、OpenAPI 导出与客户端生成、前端 typecheck/oxlint/Prettier/生产构建及测试库 `alembic check` 全部通过；契约生成物无漂移。

### F1 验证统计

- 已通过:后端 F1 单元测试、后端 F1 集成测试、后端 unit/eval 回归、`ruff check`、`mypy app scripts`、OpenAPI check、前端批次页测试、前端全量测试、`npm run typecheck`、`npm run lint`、`npm run build`。

若数量对不上,说明环境或代码有问题,先排查再继续。

## 📜 已完成阶段
- [x] 初始脚手架（monorepo + uv + Vite）
- [x] 数据库 schema 创建（19 张表,含 `schema_mapping_version`、`row_result` 幂等表与 `audit_log` 追加写触发器）
- [x] F2 CP-F2.1（映射版本、结构化解析持久化列、legacy 回填与双向迁移）
- [x] F2 CP-F2.2（严格归一化、映射/推断校验、12 字段可用性、原子解析/复用/重解析）
- [x] F2 CP-F2.3（五个 API、RBAC、租户隔离、无 PII 审计与失败独立事务）
- [x] F2 CP-F2.4（批次页原始数据/字段映射/错误清单/字段可用性四视图、三角色权限、映射复用/保存、解析触发与缓存刷新）
- [x] F2 CP-F2.5（全量测试、契约同步、静态检查、生产构建与 Alembic 零漂移）
- [x] F3 CP-F3.0（五类强类型规则、不可变快照、verdict、全历史查重、并发/幂等、API 与迁移边界规格）
- [x] 认证集成（server-side session + RBAC 三角色 + 租户过滤 fail-closed）
- [x] 前端垂直切片（登录 / 路由守卫 / 三角色外壳 / 系统状态页）
- [x] OpenAPI 契约门禁 + pre-commit + GitHub Actions CI
- [x] 合成数据生成器骨架（确定性 + 标签物理分离）
