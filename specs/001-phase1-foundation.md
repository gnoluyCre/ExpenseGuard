# Spec 001 · Phase 1 地基

**状态:** 已完成（CP0–CP4）。遗留 W0 spike 未做，见文末「已知问题」。
**最近更新:** 2026-07-27
**验收:** 后端 `49 passed, 1 skipped`；前端 `8 passed`；
`ruff` / `mypy app scripts` / `alembic check` / `oxlint` / `tsc` / `prettier --check`
/ `pre-commit run --all-files` 全绿。

---

## 已完成

### CP0 · 仓库重置

从模板仓库 `KhazP/vibe-coding-prompt-template` 转为独立的
`gnoluyCre/ExpenseGuard`,干净历史起步(单个根提交),模板脚手架全部清除。

`.gitignore` 从**第一个 commit** 起就排除 `tenants/`、`data/private/`、`.env`
—— 事后补无法清除 git 历史。正反两面均已验证:7 个敏感路径命中,
5 个必须提交的文件(`uv.lock` / `package-lock.json` / `openapi.json` /
`.env.example` / `data/synthetic/*.xlsx`)无一误伤。

### CP1 · 后端地基

- **工具链:** uv + Python 3.13,`uv sync` 全程无源码编译
- **18 张表**,分两个迁移(0001 全保真手写 / 0002 骨架 autogenerate)
- **Alembic 与 LangGraph checkpoint 三层隔离**,`alembic check` 零漂移
- 往返 `downgrade base → upgrade head` 后仍零漂移,`langgraph` schema 存活

### CP2 · 幂等原语与恢复测试

19 个测试全绿。核心资产:

| 文件 | 作用 |
|---|---|
| `app/core/orchestration/idempotency.py` | `process_row_once` / `upsert_row_result` |
| `app/core/orchestration/checkpointer.py` | LangGraph checkpoint 连接池 |
| `tests/conftest.py` | 双 fixture 策略(rollback / 真提交) |
| `tests/integration/test_migrations.py` | 受保护约束的守门员(直查 `pg_constraint`) |
| `tests/integration/test_idempotency.py` | 5 条幂等测试 |
| `tests/integration/test_recovery.py` | checkpoint 隔离 + 节点重放三重断言 |

### CP3 · 认证、RBAC 与租户隔离

34 个测试全绿。核心资产:

| 文件 | 作用 |
|---|---|
| `app/core/security/password.py` | argon2 封装 + `verify_dummy()` 防用户名枚举 |
| `app/core/security/session_service.py` | PG 会话:token 只存 SHA-256、双重过期、写节流 |
| `app/core/security/permissions.py` | 权限矩阵（数据而非 if-else） |
| `app/core/security/auth_service.py` | 登录 + 审计留痕 |
| `app/core/tenancy/scope.py` | 租户过滤强制注入（fail-closed） |
| `app/api/deps.py` | 依赖链 `get_db → get_auth → get_tenant_db` |
| `app/__main__.py` | 服务器入口（绕开 uvicorn 硬编码的 ProactorEventLoop） |

#### 测试发现的真实漏洞:3 张表绕过了租户过滤

`test_跨租户查询返回空而非他人数据` 一上来就红了:租户 A 的上下文
查到了租户 B 的数据。

根因是 `issubclass(FileVersion, TenantScopedMixin)` 为 **False** ——
`FileVersion` / `AppUser` / `UserSession` 当初手写了 `tenant_id` 列
而没继承 mixin。`with_loader_criteria` 靠 `issubclass` 匹配实体，
匹配不上就**静默不过滤**:列写得再对也没用。

这正是那种「代码看起来完全正确、评审也看不出问题」的漏洞。
改为继承 mixin 后 `alembic check` 仍零漂移（mixin 生成的列与手写的
逐字节一致），说明这是纯粹的元数据问题，与 schema 无关 ——
也就更难靠看 DDL 发现。

**防复发:** `test_migrations.py::test_all_business_tables_have_tenant_id`
断言所有业务表都有该列；上面那条跨租户测试则守住 mixin 继承关系。

#### 认证路径的鸡生蛋问题

守卫要求查询时已有租户上下文，但「查会话表」正是确定租户的唯一途径。
解法是显式的 `skip_tenant_filter` execution option，全系统只用于两处:

- `authenticate()` —— 按 `(tenant_slug, username)` 精确查用户
- `resolve_session()` —— 按 token 的 SHA-256 精确查会话

做成**显式选项**而非「守卫在某些情况下自动放行」，是为了让绕过行为
可 grep、可评审。`get_auth` 解析出租户后立即 `bind_tenant`，
因此下游（含登出撤销）全部自动受保护，不需要更多口子。

#### 失败登录的审计日志曾被回滚

`get_db` 在异常传播时会 `rollback()`，导致「登录失败」的审计记录
跟着异常一起消失 —— 结果是暴力破解尝试一条都不留痕，而这恰恰是
最需要留痕的场景。修法是在抛异常前立即 `commit()`（此刻事务里
只有那一条审计记录，单独提交是安全的）。

### CP4 · 前端垂直切片 + OpenAPI 契约 + CI

后端 49 passed + 1 skipped（较 CP3 的 34 增加 15 条合成数据测试 + 1 条
baseline 守门 + 1 条常驻 skip 的评测门禁），前端 8 passed。核心资产:

| 文件 | 作用 |
|---|---|
| `backend/scripts/export_openapi.py` | 稳定序列化的契约导出（`sort_keys=True`）+ `--check` 门禁模式 |
| `openapi.json` / `frontend/src/api/schema.d.ts` | 前后端契约，**两个生成物都进仓库**，供 CI 做漂移比对 |
| `frontend/src/api/client.ts` | openapi-fetch 类型化客户端 |
| `frontend/src/auth/useAuth.ts` | 身份的唯一事实来源是服务端会话，不做本地副本 |
| `frontend/src/auth/RequireAuth.tsx` | 路由守卫（体验层，非安全边界） |
| `frontend/src/components/AppShell.tsx` | 三角色外壳，菜单由**权限**驱动而非角色 if-else |
| `backend/app/synth/` | 合成数据生成器:确定性、八类登记、标签物理分离 |
| `backend/evals/baseline.json` + `tests/eval/test_eval_gate.py` | 数据驱动的评测门禁占位 |
| `.pre-commit-config.yaml` / `.github/workflows/ci.yml` | 提交前与 CI 双层门禁 |

#### 与计划书的三处偏离（生态已变，计划书前提失效）

| 项 | 计划书 | 实际 | 原因 |
|---|---|---|---|
| React | 18 | **19** | 计划书锁 18 的理由是「shadcn CLI 按 19 生成会有 peer dep 冲突」。如今 create-vite 9 默认就是 19、shadcn 也原生面向 19，锁 18 反而**制造**了那个冲突 |
| 前端 lint | ESLint + Prettier | **oxlint + Prettier** | create-vite 9 官方模板已改用 oxlint；ESLint 需额外拉一整套 typescript-eslint。AGENTS.md 的编码约定已同步修改 |
| 路由 | `react-router-dom` | **`react-router` v8** | `react-router-dom` 最新版落在 CVE 区间（7.12.0–8.2.0，RSC CSRF），且 v8 已把它并入主包 |

两处偏离都已回写 AGENTS.md / MEMORY.md —— **偏离要落到事实来源里，
而不是留成「文档说 A、代码是 B」**。

#### 反向验证（已实测 2026-07-27）

前端守卫与菜单过滤各改一行制造缺陷:

- `RequireAuth` 的 `if (isError || !user)` → `if (false)`
- `AppShell` 的 `MENU.filter(...)` → `MENU`

结果 3 条测试立刻变红（`未登录时跳转到登录页`、`auditor 看不到规则配置`、
`viewer 只看到只读入口`），随后完整复原。

私有路径拦截也实测过:`git add -f data/private/2026-06.csv` 后
`pre-commit run --all-files` 报 `block-private-paths` 失败。
这条很重要——**`.gitignore` 挡不住 `git add -f`**。

#### 这一轮真正花时间的地方:测试桩没生效，测试却是绿的

前端测试第一版里 `RequireAuth` 的「未登录应跳转」是**通过**的，但它
通过的原因不是守卫工作正常，而是 `vi.stubGlobal("fetch", ...)` 根本没
生效——`createClient()` 在模块导入时就把 `globalThis.fetch` 抓走了，
测试里的替换来得太晚。于是请求真的发了出去、真的失败，query 进入
`isError`，守卫跳转，断言通过。

**一个什么都没测到的测试，和一个测对了的测试，绿灯长得一模一样。**
是「已登录应放行」那条同时红了才暴露出来——它无法靠网络错误蒙混过关。
修法是让客户端延迟解析 fetch（`fetch: (request) => globalThis.fetch(request)`）。

同类问题还有一处:测试桩把 `fetch` 的入参当字符串处理，而 openapi-fetch
传的是 **Request 对象**，`String(request)` 得到 `"[object Request]"`，
匹配不上任何 handler。症状是「登录失败」，很容易被误判成业务代码的 bug。

---

## 关键设计决策

### 双 fixture:为什么幂等测试不能用 rollback

常见做法是「每个测试包在最外层事务里,结束 rollback」。对幂等测试这是灾难性的:

1. rollback 掉的数据从未提交过,而幂等要验证的恰恰是
   「跨事务、跨进程的**已提交**副作用最多发生一次」—— 被测性质根本不存在
2. 并发测试需要两条独立连接抢同一个键,包在单一事务里做不到
3. 最坏情况是**静默通过**:上一轮残留的行会让 `ON CONFLICT DO NOTHING`
   恰好表现得「正确」,于是本该失败的测试变绿

因此凡断言副作用次数的测试一律用 `clean_db`(真提交 + 前置 TRUNCATE)。

### schema 用迁移建,不用 `metadata.create_all()`

`create_all()` 绕过迁移文件直接按模型建表,那样「唯一约束到底有没有写进迁移」
永远得不到验证 —— 而那正是受保护区域的核心资产。

### TRUNCATE 与追加写触发器的关系

`audit_log` 的追加写触发器是 `BEFORE UPDATE OR DELETE`,所以 `DELETE` 会被拒绝,
但 `TRUNCATE` 触发的是 TRUNCATE 触发器(未建),因此测试可以用它清库。

这也是该保证的真实边界:TRUNCATE 确实能绕过触发器,但它需要表 owner 权限
且是全表级操作 —— 无法选择性篡改某几条记录,与「悄悄改掉一条审计记录」
的威胁模型不是一回事。

---

## 反向验证记录

反向验证比任何自动化断言都重要:它证明测试测的是真东西。

### 幂等约束(已实测 2026-07-27)

```sql
ALTER TABLE row_result DROP CONSTRAINT uq_row_result_file_version_id_row_no;
```

结果:`test_idempotency.py` 5 个测试中 **4 个变红**,报错:

```
psycopg.errors.InvalidColumnReference:
there is no unique or exclusion constraint matching the ON CONFLICT specification
```

第 5 个只碰 `audit_log`,正确地保持通过。

值得注意的是失败模式:缺约束时 `ON CONFLICT` 是 SQL 层**硬错误**,
而不是「静默产生两行重复数据」。硬错误远好于静默降级 —— 后者才是
审计系统里真正危险的失败形态。

恢复:

```sql
ALTER TABLE row_result ADD CONSTRAINT uq_row_result_file_version_id_row_no
    UNIQUE (file_version_id, row_no);
```

### 租户过滤（已实测 2026-07-27）

在 conftest 里于 `install_tenant_guard()` 之后调用 `uninstall_tenant_guard()`，
两条测试立即变红:

- `test_跨租户查询返回空而非他人数据` —— 租户 A 看到了 B 的数据
- `test_未绑定租户时查询直接报错` —— `DID NOT RAISE TenantScopeMissingError`

证明这两条测试确实在验证守卫机制，而不是碰巧通过。

---

## 踩过的坑（按发现顺序）

| # | 现象 | 根因 | 处理 |
|---|---|---|---|
| 1 | autogenerate 生成 `op.drop_table('alembic_version')` | `version_table_schema="public"` 与反射结果(`schema=None`,因 `search_path=public`)不匹配,Alembic 的自动排除逻辑失效 | `include_object` 显式排除 + 移除多余配置 |
| 2 | `alembic revision` 直接 `UnicodeDecodeError` | configparser 用 **locale 编码**(此机 GBK)读 `.ini`,UTF-8 中文注释解码失败 | `alembic.ini` 保持 ASCII-only,并在文件内注明 |
| 3 | `CORS_ALLOWED_ORIGINS=http://...` 报 JSON 解析错误 | pydantic-settings 对 `list[str]` 字段先做 `json.loads`,发生在 before-validator 之前 | `Annotated[list[str], NoDecode]` |
| 4 | 所有异步 DB 测试 `InterfaceError` | **psycopg 异步模式无法在 `ProactorEventLoop` 上运行**,而它是 Windows 的默认事件循环 | 测试用 `pytest_asyncio_loop_factories` hook;生产用 `app/asyncio_compat.py` |
| 5 | ruff 报 62 个「歧义 Unicode」 | RUF001/002/003 把中文全角标点判为与 ASCII 形近 | 关闭这三条规则 |
| 6 | `test_langgraph_schema_..._is_empty` 单跑绿、全量跑红 | 断言「schema 为空」制造了测试间顺序依赖(`test_recovery` 会建 checkpoint 表) | 去掉「空」断言,只断言 schema 存在 |
| 7 | 3 张表静默绕过租户过滤 | `FileVersion`/`AppUser`/`UserSession` 手写 tenant_id 而未继承 `TenantScopedMixin`,`with_loader_criteria` 靠 `issubclass` 匹配,匹配不上就不过滤 | 改为继承 mixin(schema 零漂移) |
| 8 | 健康检查 postgres 探针 3 秒超时,但直连只要 0.03 秒 | **uvicorn 在 Windows 硬编码 `ProactorEventLoop`**(`uvicorn/loops/asyncio.py`),而 psycopg 异步无法在其上运行。且 uvicorn 0.36+ 用 `asyncio.run(loop_factory=...)`,**完全绕过事件循环策略** | 新增 `app/__main__.py`,把循环工厂显式传给 uvicorn |
| 9 | 登录失败的审计日志消失 | `get_db` 在异常传播时 rollback,把审计记录一起回滚 | 抛异常前立即 commit |
| 10 | cookie 名两处不一致 | 写用 `settings.session_cookie_name`,读硬编码 `"eg_session"`(FastAPI 的 `Cookie(alias=)` 在导入时求值,拿不到运行时配置) | 改为模块常量 `SESSION_COOKIE_NAME`,删掉该配置项 |
| 11 | `npx shadcn add` 把组件写进一个名叫 `@` 的**真实目录**,不报任何错 | shadcn CLI 读的是根 `tsconfig.json` 的 `paths`,而 create-vite 的根配置只做 project references、没有 `paths` | 根 `tsconfig.json` 也加一份 `paths`(于是别名共三处:vite / tsconfig.app / tsconfig 根) |
| 12 | `openapi-typescript` 装不上 | 它的 peer 是 `typescript@^5.x`,而 create-vite 9 默认给 TS 6 | 前端 TS 锁 `~5.9`。用 `--legacy-peer-deps` 硬装的话,codegen 会在运行时撞上 TS 6 的 API 变更 |
| 13 | 加了 `js-yaml: ^5` 的 override 后,`openapi-typescript` 启动即 TypeError | `@redocly/openapi-core` 用的是 v4 的 `types.merge` API | override 锁 `4.3.0`(仍在 CVE 修复版本内) |
| 14 | 所有 API 请求都是 `/api/api/...` | schema 里的路径本身已含 `/api`,客户端又配了 `baseUrl: "/api"` | baseUrl 只放 origin |
| 15 | jsdom 下所有请求报 `Failed to parse URL` | undici 的 `Request` 不接受相对 URL,而空 baseUrl 产出的正是相对 URL | baseUrl 默认取 `window.location.origin` |
| 16 | **测试桩完全没生效,测试却是绿的** | `createClient()` 在模块导入时抓走 `globalThis.fetch`,`vi.stubGlobal` 来得太晚;请求真发出去、真失败,于是「未登录应跳转」靠网络错误蒙混通过 | 客户端改为延迟解析 `fetch`。详见 CP4 章节 |
| 17 | `oxlint` 报一片 `react-in-jsx-scope` | 打开 `categories.correctness` 会连带启用这条规则,而它对 React 17+ 的自动 JSX 运行时不适用 | 显式 `"react/react-in-jsx-scope": "off"` |
| 18 | `check-json` 钩子在 tsconfig 上失败 | `tsconfig*.json` 是 JSONC(TS 官方允许注释) | 排除它们,而不是删掉配置里的解释性注释 |

---

## 已知问题

### testcontainers 在 Windows + Docker Desktop 下挂死

**现象:** `USE_TESTCONTAINERS=1 pytest` 会无限挂起。

**已确认的事实:**
- 容器正常启动,PostgreSQL 日志显示 `database system is ready to accept connections`
- **迁移完整跑完**(容器内 19 张表齐备)
- 之后 pytest 既无数据库连接(`pg_stat_activity` 为空),也无任何输出
- 进程存活但无进展,超过 10 分钟

**根因未定位。** 迁移之后、测试执行之前的某个环节卡住,与数据库无关。

**处理:** 默认测试库改为 docker-compose 预建的 `expenseguard_test`。
本项目开发本来就要 `docker compose up`(应用自身要连 postgres 和 qdrant),
所以这不构成额外负担。testcontainers 保留为 `USE_TESTCONTAINERS=1` 的
可选路径 —— 它在 Linux CI 上工作正常,且隔离性更好。

**若要重新排查:** 装 `py-spy` 抓挂起时的 Python 栈是最快的路径。

### Windows 上必须用 `python -m app` 启动服务

**不能**直接 `uvicorn app.main:app`。uvicorn 在 Windows 硬编码 ProactorEventLoop，
而 psycopg 异步模式无法在其上运行。

这个坑最阴险的地方在于**只在不带 `--reload` 时出现**:reload 走子进程模式
(`use_subprocess=True`)会拿到 SelectorEventLoop，于是「开发正常、生产挂死」。

且 `asyncio.set_event_loop_policy()` 对此**无效** —— uvicorn 0.36+ 改用
`asyncio.run(..., loop_factory=...)`，该路径完全绕过事件循环策略。
唯一可靠的做法是把工厂显式传进去，见 `app/__main__.py`。

Linux 部署（Docker）不受影响。

### W0 spike 未做 —— 模型层镜像**未经实测**

`docker-compose.models.yml` 已写好，但**没有拉起来验证过**。文件里选的是
Infinity（单容器同时挂 embed + rerank，正好对上本项目的双需求），备选方案
HF TEI 以注释形式留在文件末尾。**这个选择必须靠实测确认，不能靠文档定。**

同一次 spike 里必须一并验证的还有:**客户内网大概率访问不了 HuggingFace。**
要确认离线模型供给路径（预下载权重挂载 / 权重烤进自建镜像）。这个问题
留到上线周才发现会很贵。

Langfuse 那边同理:`docker-compose.observability.yml` 写的是 v2（单容器 +
已由 init 脚本预建的 `langfuse` 库），未起过。升 v3 不要手写——服务集变成
web + worker + ClickHouse + Redis + MinIO，必须从上游固定 tag 复制官方 compose。

### CI 只在本地做过等价验证

`.github/workflows/ci.yml` 的每一步都在本地跑通过（ruff / mypy / pytest /
`alembic check` / 契约导出与比对 / oxlint / tsc / vitest / build /
`pre-commit run --all-files`），但**流水线本身尚未在 GitHub 上真实跑过一次**
—— 仓库还没 push。首次 push 后要确认的点:服务容器的 `CREATE DATABASE`
步骤、`ghcr.io/gitleaks/gitleaks:v8.30.1` 镜像可拉取、setup-uv/setup-node
的缓存键。

### 阶段 1 无 Dockerfile

`docker compose build` 目前无事可做（compose 里只有 postgres/qdrant 两个
现成镜像）。CI 因此没有 build job —— 与其写一个假的，不如把它留到阶段 4
「Docker Compose 单机实跑验证」时连同 Dockerfile 一起补。

---

## 待办

- [ ] **W0 spike**:实测 embedding 镜像（Infinity vs TEI）+ 离线模型供给路径，结论回写本文件
- [ ] **W0 spike**:起一次 Langfuse、发一条测试 span、记录版本与内存占用，然后 down
- [ ] push 后确认 CI 首次运行全绿
- [ ] 阶段 2 F1 · Excel 导入与文件版本管理（`process_row_once` 的第一个生产调用方在 F3）
