# AGENTS.md —— ExpenseGuard 主计划

<!--
本文件是本项目所有 AI 编码助手的唯一事实来源,Codex 原生读取。
保持精简 —— 细节位于文末的「上下文文件」中。构建过程中请持续更新「当前状态」与「路线图」。
-->

## 项目概览与技术栈
**应用名:** ExpenseGuard
**概述:** 面向内部财务团队的费用报销预审系统。把每月数百至数千行报销数据收敛成真正需要人看的少数几十行,且每条判定都附带可追溯的制度条款逐字引用与原始数据行证据链。形态为 **agent-in-workflow**:确定性 workflow 为主干(可复现、可审计),仅在「异常取证」环节使用单点 ReAct agent。当前为单一客户交付(非 SaaS),但所有本可硬编码之处(字段映射、规则阈值、制度条款)一律数据驱动,为接入第二家企业预留。
**技术栈:** React + Vite + TypeScript + Tailwind + shadcn/ui(前端) · Python + FastAPI + Pydantic + LangGraph(后端) · PostgreSQL(业务 / checkpoint / 审计) · Qdrant(制度向量) · 本地自托管 embedding/rerank · 云 API 强模型(vLLM 可切换) · Docker Compose 单机部署
**关键约束(不可妥协):**
- **私有化(硬约束):** 向量库与模型层必须保留本地自托管路径;检索层数据不出内网。强模型走云 API 时,PII 进入 LLM 前必须脱敏。
- **可复现 + 可审计:** 相同输入 + 相同规则版本 → 相同输出;规则版本、判定依据、复核动作、配置变更全部留痕。
- **行级幂等(最高优先级):** 唯一约束 `(file_version_id, row_no)`;LangGraph 节点重放 / 崩溃重启后,同一行副作用最多发生一次。
- **数据驱动而非硬编码:** 字段映射、规则阈值、制度条款皆为配置 / 数据。
- **脱敏切面:** `tenants/` 与 `data/private/` 从第一次 commit 即在 `.gitignore`;脱敏 = 删除这两个目录,而非事后重写。
- **仅桌面浏览器端:** 不做移动端与平板适配。
- **严格类型:** 后端 Python 全量 type hints + Pydantic;前端 strict TypeScript。

## 环境搭建与命令
执行以下命令进行标准开发流程。不要发明新的包管理器命令。
- **依赖安装:** `docker compose up -d`(postgres/qdrant/embedding/trace)+ 后端 `uv sync`(或 `pip install -e .`)+ 前端 `npm install`
- **本地开发:** 后端 `uv run python -m app` · 前端 `npm run dev`
  > ⚠️ 后端**不要**直接用 `uvicorn app.main:app`：uvicorn 在 Windows 硬编码
  > ProactorEventLoop，而 psycopg 异步模式无法在其上运行（不带 `--reload` 时
  > 数据库调用会全部挂起至超时）。详见 `app/asyncio_compat.py`。
- **测试:** 后端 `pytest` · 前端 `npm run test`
- **代码检查与格式化:** 后端 `ruff check . && ruff format .` · 前端 `npm run lint && npm run format`
- **类型检查:** 后端 `mypy app scripts` · 前端 `npm run typecheck`
- **契约同步:** 后端 `python scripts/export_openapi.py` → 前端 `npm run gen:api`(改了 Pydantic 模型必做,否则 CI 的 contract job 会红)
- **构建:** 前端 `npm run build`(阶段 1 尚无 Dockerfile,`docker compose build` 待阶段 4 部署时补)

> 具体命令名以脚手架落地时的 `pyproject.toml` / `package.json` 为准;上表为约定基线,不要发明新的包管理器命令。

## 受保护区域 🛡️
未经人工明确批准,不得修改以下内容:
- **密钥:** 绝不提交 `.env` 文件,绝不硬编码 API key、token、密码。使用环境变量(`.env.example` 只含键名)。pre-commit secret 扫描必须启用。
- **私有数据与租户:** `tenants/` 与 `data/private/` 永不进仓库(从第一次 commit 起在 `.gitignore`)。真实报销数据只落 `data/private/` 或本地卷。
- **基础设施:** `docker-compose.yml`、Dockerfile、`.github/workflows/`。
- **数据库迁移:** 已有的 Alembic 迁移文件。
- **幂等与审计:** `row_result`、`sampling_audit`、`audit_log` 的唯一约束与追加写语义不得弱化。

## 编码约定
- **格式化:** 后端 Ruff(lint + format) · 前端 **oxlint + Prettier** —— 新代码不得有告警(`npm run lint` 带 `--max-warnings=0`,告警即失败)。
  > 前端 lint 器在 CP4 由 ESLint 改为 oxlint:create-vite 9 的官方模板已改用它,而 ESLint 需要额外拉一整套 typescript-eslint 依赖。格式化仍由 Prettier 负责(oxlint 不做格式化)。
- **架构:** 分层 / 面向服务 —— `core/`(通用框架层)与 `tenants/`、`data/private/`(客户数据层)物理隔离;传输层(FastAPI 路由 / React 组件)不含业务逻辑。
- **测试:** 所有新工具函数都要写单元测试。核心用户流程要写集成测试。幂等性与中断恢复是最高优先级测试项。
- **类型安全:** 严格类型。Python 全量 type hints + Pydantic 校验外部输入;前端禁用 `any`,用精确 interface 或 `unknown` + type guard。

## 我应如何思考 🧠
1. **先理解意图:** 回答前先弄清用户到底需要什么。
2. **不确定就问:** 若缺少关键信息,先提出一个具体问题再动手。
3. **先规划再编码:** 在改动超过一个文件前,提出简短的分步计划并等待批准。(若工具支持 plan/reflect 模式,请使用。)
4. **增量执行:** 一次只做一个功能。优先重构而非大段重写。尊重 F1→F2→F3→F4→F5 的串行依赖链。
5. **改动后验证:** 每次逻辑变更后跑测试 / 检查器或手动检查;修复失败后再继续(见 `REVIEW-CHECKLIST.md`)。对幂等 / 恢复类改动,必须跑中断恢复集成测试。
6. **说明取舍:** 推荐方案时简要提及备选。
7. **记录在文件里:** 把状态与决策写进 `MEMORY.md`,而非依赖聊天历史。
8. **善用子代理:** 若工具支持子代理或并行代理,分配角色并要求先出计划再编辑。
9. **不确定必须显性化:** AI 失败 / 超步数时的回退一律是「转人工 + 显式标注」,绝不是「猜一个结论」。这与能力声明机制(enabled/degraded/unavailable)一致。

## 禁止事项 ⛔
- 不得未经明确确认删除文件。
- 不得在无备份方案的情况下修改数据库 schema(幂等约束与审计表尤其敏感)。
- 不得添加不属于当前阶段的功能(P1/P2 特性不得抢在 P0 之前)。
- 不得以「改动简单」为由跳过测试。
- 不得绕过失败的测试或 pre-commit 钩子(含 secret 扫描)。
- 不得使用已废弃的库或写法。
- 不得让真实 PII 未经脱敏进入 LLM;不得把检索 / 模型层的「数据不出内网」承诺含糊成「全链路私有化」。
- 不得呈现未经机械式逐字校验的制度条款引用。
- 不得静默丢弃解析失败的行 —— 必须进错误清单并给出原因。
- 不得提交 `.env`、`tenants/`、`data/private/`。

## 工程约束 🏗️
- **类型安全:** 前端禁用 `any` —— 用 `unknown` + type guard;所有函数入参与返回均标注类型;外部输入用运行时 schema 校验(前端 Zod,后端 Pydantic)。后端同理:全量 type hints,Pydantic 模型既约束 LLM 结构化输出(XGrammar / function calling)又校验 API,单一 schema 避免漂移。
- **架构主权:** 路由 / UI 层只处理 request/response。业务逻辑住在 `backend/core/` 各服务模块。路由处理器不直接查数据库 —— 经服务层。多租户过滤通过依赖注入强制注入,不手写 WHERE。
- **依赖治理:** 新增依赖前先查 `pyproject.toml` / `package.json`。优先原生 API。数据获取方式以 `agent_docs/tech_stack.md` 为准。快速演进的库(LangGraph、Qdrant)锁次版本,升级走独立 PR + 完整评测门禁。
- **清晰沟通:** 简述问题并修复 —— 无道歉循环、无填充语。缺上下文就问一个具体问题。
- **流程纪律:** pre-commit 钩子(format / lint / secret 扫描)必须通过方可提交(或先询问再绕过)。CI 评测门禁:召回率低于基线阈值即阻断合并。

## 当前状态 📍
**最近更新:** 2026-07-27
**正在进行:** 阶段 1 已完成,准备进入阶段 2 F1(Excel 导入与文件版本管理)
**最近完成:** CP0 仓库重置 / CP1 后端地基与 18 张表 / CP2 幂等原语与恢复测试 / CP3 认证、RBAC 与租户隔离 / **CP4 前端垂直切片 + OpenAPI 契约 + CI**(后端 49 passed + 1 skipped,前端 8 passed)
**受阻于:** 无(W0 阻塞项:真实报销数据脱敏审批、制度文档到位 —— 见 `MEMORY.md`)
**已知缺口(三项,均不阻塞 F1):**
1. **W0 spike 未做** —— `docker-compose.models.yml` 的 embedding 镜像**未实测**;客户内网访问不了 HuggingFace 时的离线权重供给路径未验证。留到上线周才发现会很贵。
2. **CI 未在 GitHub 上真实跑过** —— 每一步都在本地跑通了等价命令,但仓库尚未 push。首次 push 后需确认:服务容器的 `CREATE DATABASE` 步骤、gitleaks 镜像可拉取、setup-uv/setup-node 的缓存键。
3. **开发工具接管完成** —— 已退役 ClaudeCode 专用入口;后续 Codex 以本文件 + `MEMORY.md` + 当前 `specs/` 为上下文入口。

细节见 `specs/001-phase1-foundation.md` 的「已知问题」与「待办」。

## 路线图 🗺️

### 阶段 1:地基 ✅
- [x] 初始化 monorepo 结构(`backend/app/{core,api,db,synth}`、`frontend/`、`tenants/`、`data/`);`.gitignore` 从第一次 commit 即排除 `tenants/`、`data/private/`、`.env`
- [x] Docker Compose 起 postgres / qdrant;embedding 与 trace(Langfuse)拆为可选文件,不进默认 `up`
- [x] 数据库 schema + Alembic 迁移(18 张表,两个迁移);**行级幂等结果表 `row_result` 的唯一约束已落地并做过反向验证**
- [x] 自建账号密码 + server-side session + RBAC 三角色(auditor / configurator / viewer)
- [x] Pre-commit(ruff / gitleaks / 大文件与私有路径拦截)+ CI(backend / frontend / contract / secrets / eval-gate)
- [x] 合成数据生成器骨架(`backend/app/synth/`,一等交付物;只实现超限额一类,其余七类显式 `NotImplementedError`)

### 阶段 2:核心功能(P0 —— 串行依赖链)
- [ ] **F1 · Excel 导入与文件版本管理** —— .xlsx 500–5000 行;内容哈希去重生成 `file_version_id`;同文件重复导入幂等
- [ ] **F2 · Schema 映射与结构化解析** —— 列名映射配置可复用;金额 / 日期归一化;解析失败行进错误清单不静默丢弃;字段可用性三级自动探测(available/inferred/missing)
- [ ] **F3 · 确定性校验** —— 限额 / 票种 / 时效 / 抬头 / 发票号查重五类硬规则(JSON Logic 配置化);阈值白名单为配置项;命中记录 rule_id + rule_version;可复现
- [ ] **F4 · 报告生成(含制度条款引用)** —— 按风险分级;每条判定含规则 / 条款 ID + 逐字引用 + 原始行号;制度检索按费用发生日过滤生效版本;**LLM 引用须通过机械式逐字校验方可呈现**;报告可导出
- [ ] **F5 · 人工复核台** —— 按风险排序队列;同屏展示原始行 + 判定理由 + 条款引用;标记 confirmed / false_positive 带复核人与时间戳写审计日志;**触发被放行样本随机抽检**

### 阶段 3:差异化能力(P1)与打磨
- [ ] **F6 · 跨行关联检测(统计层)** —— 拆单 / 连号 / 频次异常 / 时空冲突;能力声明机制(enabled/degraded/unavailable)统一挂载;输出参与行号 + 证据链
- [ ] **F7 · 异常取证 Agent(ReAct)** —— 只读工具集;最大步数上限;每步落 `evidence_step`;终止给出「证据是否充分」显式判断
- [ ] **F8 · 二维分级** —— severity_impact / severity_confidence 分列;代价敏感阈值参数化
- [ ] 错误处理完善、性能达标(5000 行 ≤ 15 分钟)、优雅退出(SIGTERM 后完成当前行 + 写 checkpoint 再退出)

### 阶段 4:上线
- [ ] 安全审查(见 `REVIEW-CHECKLIST.md`)—— 脱敏一致性、prompt 注入红队(promptfoo)、secret 扫描
- [ ] Docker Compose 单机实跑验证 + 结构化日志 + 健康检查 + trace 可归因到 task 的 token / 延迟 / 成本
- [ ] 1 个真实月度批次端到端手工验证;被放行样本随机抽检机制已上线;回滚方案文档化

## 上下文文件 📚
按需加载 —— 渐进式披露以保持上下文精简:
- `agent_docs/tech_stack.md` —— 技术栈细节、库、安装命令、数据模型
- `agent_docs/code_patterns.md` —— 架构与代码风格规则
- `agent_docs/project_brief.md` —— 产品愿景与约定
- `agent_docs/product_requirements.md` —— 功能列表与用户故事
- `agent_docs/testing.md` —— 测试策略与命令
- `MEMORY.md` —— 会话记忆:决策、已知问题、当前目标
- `REVIEW-CHECKLIST.md` —— 标记工作完成前的「完成定义」
- `specs/` —— 构建过程中生成的功能规格与交接说明
- `docs/` —— research / PRD / TechDesign(完整背景)
