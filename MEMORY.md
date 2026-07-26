# 系统记忆与上下文 🧠
<!--
AGENTS:在每个重要里程碑、结构性变更或修复 bug 后更新本文件。
若历史上下文仍然相关,请勿删除;对较早的已完成项进行压缩。
-->

## 🏗️ 当前阶段与目标
**当前任务:** 阶段 1 —— 地基。CP0(仓库重置)、CP1(后端地基 + 18 张表)、CP2(幂等原语与恢复测试)**已完成**,19 个测试全绿。落地记录见 `specs/001-phase1-foundation.md`。
**下一步:**
1. CP3 —— 认证(argon2 + PG session 表)、RBAC 三角色、租户过滤的 API 层接线与集成测试(含跨租户隔离的反向验证)。
2. CP4 —— 前端登录垂直切片、OpenAPI 类型化客户端与漂移门禁、pre-commit + CI。

## 📂 架构决策
*(把构建过程中做出的具体选择记录在此,便于后续 agent 遵循)*
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

## 📜 已完成阶段
- [ ] 初始脚手架
- [ ] 数据库 schema 创建(含 `row_result` 幂等表)
- [ ] 认证集成(session + RBAC 三角色)
