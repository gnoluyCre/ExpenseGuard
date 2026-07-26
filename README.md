# ExpenseGuard

面向内部财务团队的**费用报销预审系统**。把每月数百至数千行报销数据收敛成真正需要人看的少数几十行,且每条判定都附带可追溯的制度条款逐字引用与原始数据行证据链。

形态为 **agent-in-workflow**:确定性 workflow 为主干(保证可复现、可审计),仅在「异常取证」环节使用单点 ReAct agent。审计结论必须经得起内审质询,因此不把整条链路交给概率性推理。

> **当前为单一客户交付,按生产系统标准建设,不是 SaaS。** 不做自助注册、计费、租户管理后台,部署为单租户单机。但所有本可硬编码之处——字段映射、规则阈值、制度条款——一律数据驱动,为接入第二家企业预留。

## 核心能力

| 能力 | 说明 |
|---|---|
| **跨行关联分析** | 拆单、发票连号、频次异常、时空冲突——这些异常只在跨行关联中显现,单行审核看不出来 |
| **可解释证据链** | 每条判定含规则/条款 ID + 条款逐字引用 + 原始数据行号。LLM 产出的引用须通过**机械式逐字校验**方可呈现 |
| **制度文档自适应** | 按**费用发生日**匹配当时生效的制度版本——2 月的费用不会被 5 月的新制度误判 |
| **能力声明** | 系统不假装什么都知道。每个检测器运行前自检字段依赖,如实声明 `enabled` / `degraded` / `unavailable` |

## 技术栈

| 层 | 选型 |
|---|---|
| 前端 | React 19 + Vite 8 + TypeScript 5.9(strict) + Tailwind v4 + shadcn/ui — **仅桌面浏览器端** |
| 后端 | Python 3.13 + FastAPI + Pydantic v2 |
| 编排 | LangGraph + PostgresSaver(checkpoint) |
| 数据库 | PostgreSQL(业务 / checkpoint / 审计) |
| 向量库 | Qdrant(制度条款,payload 复合过滤) |
| 模型 | OpenAI 兼容抽象:embedding/rerank **本地自托管**;强模型云 API,vLLM 可切换 |
| 规则引擎 | JSON Logic / 决策表 — 规则即数据 |
| 可观测 | Langfuse + OpenTelemetry |
| 部署 | Docker Compose 单机 |

## 快速开始

```bash
# 1. 起依赖服务
cp .env.example .env          # 填入本地配置
docker compose up -d          # postgres + qdrant

# 2. 后端
cd backend
uv sync
uv run alembic upgrade head
uv run python -m app          # 不要用 uvicorn app.main:app —— 见下方说明

# 3. 前端
cd frontend
npm install
npm run dev
```

可选服务(不随默认 `up` 启动):

```bash
docker compose -f docker-compose.yml -f docker-compose.models.yml up -d          # embedding / rerank
docker compose -f docker-compose.yml -f docker-compose.observability.yml up -d   # Langfuse trace 面板
```

> 这两个文件都必须与 `docker-compose.yml` **叠加**使用（它们不自带
> postgres，也不重复声明网络）。可观测栈还要求 `.env` 里有
> `LANGFUSE_NEXTAUTH_SECRET` 与 `LANGFUSE_SALT`，缺了会直接拒绝启动。

> **Windows 注意:** 后端必须用 `python -m app` 启动，不能直接
> `uvicorn app.main:app`。uvicorn 在 Windows 硬编码 ProactorEventLoop，
> 而 psycopg 异步模式无法在其上运行——不带 `--reload` 时所有数据库调用
> 会挂起至超时。原因与修法见 `backend/app/asyncio_compat.py`。

## 常用命令

| 用途 | 后端 | 前端 |
|---|---|---|
| 测试 | `uv run pytest` | `npm run test` |
| 代码检查 | `uv run ruff check .` | `npm run lint` |
| 类型检查 | `uv run mypy app scripts` | `npm run typecheck` |
| 格式化 | `uv run ruff format .` | `npm run format` |
| 构建 | — | `npm run build` |

跨前后端的契约与提交前检查：

```bash
cd backend && uv run python scripts/export_openapi.py   # 重新导出 openapi.json
cd frontend && npm run gen:api                          # 重新生成 src/api/schema.d.ts
uv tool install pre-commit && pre-commit install        # 一次性
pre-commit run --all-files
```

改了后端的 Pydantic 模型就要重跑上面两条并提交结果，否则 CI 的
`contract` job 会红。

生成一批合成数据（同 seed 必然重放出同一批逻辑行）：

```bash
cd backend && uv run python -m app.synth --seed 20260727 --rows 50 --out ../data/synthetic --stem baseline-50
```

## 目录结构

```
├── backend/
│   ├── app/
│   │   ├── core/           # 通用框架层(教学项目保留)
│   │   ├── api/            # FastAPI 路由、依赖注入、RBAC
│   │   ├── db/             # SQLAlchemy 模型、Alembic 迁移
│   │   └── synth/          # 合成数据生成器(一等交付物)
│   └── tests/
├── frontend/               # React + Vite + TS
├── tenants/                # ⚠ 客户配置,已 gitignore,脱敏时整体删除
├── data/
│   ├── synthetic/          # 合成批次(进 git,可公开)
│   └── private/            # ⚠ 真实数据,已 gitignore,永不进仓库
├── docs/                   # PRD、技术设计
├── agent_docs/             # AI 助手的详细上下文文档
└── specs/                  # 各阶段功能规格与落地记录
```

## 硬约束(不可妥协)

- **私有化:** 向量库与模型层必须保留本地自托管路径;检索层数据不出内网。强模型走云 API 时,PII 进入 LLM 前必须脱敏
- **行级幂等:** 唯一约束 `(file_version_id, row_no)`。LangGraph 节点重放 / 崩溃重启后,同一行副作用最多发生一次
- **可复现 + 可审计:** 相同输入 + 相同规则版本 → 相同输出;规则版本、判定依据、复核动作、配置变更全部留痕
- **`tenants/` 与 `data/private/` 永不进仓库** — 脱敏 = 删除这两个目录

## 给 AI 编码助手

**先读 [`AGENTS.md`](AGENTS.md)** — 它是本项目的唯一事实来源:路线图、命令、工程规则。
实现细节位于 [`agent_docs/`](agent_docs/),编码前请查阅。
标记任务完成前,对照 [`REVIEW-CHECKLIST.md`](REVIEW-CHECKLIST.md)。
