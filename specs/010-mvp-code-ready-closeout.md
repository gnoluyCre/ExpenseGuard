# Spec 010 —— 代码就绪 MVP 收尾

**状态：** CP-MVP.0–CP-MVP.5 已完成
**范围：** 阶段 3 全局打磨 + 阶段 4 代码就绪交付，不新增业务能力
**前置：** F1–F8 已闭包；F8 提交 `149cf21`

## 1. 完成口径

本规格的完成态是“配置真实环境参数后可部署和运行的代码就绪 MVP”，不是生产验收完成。以下项目必须保持 `external_validation_pending`，不得用 mock 或合成数据冒充真实验收：

- 真实 OpenAI-compatible 云 API 冒烟与价格校准；
- 真实本地 embedding/rerank 模型、客户离线权重包和资源占用；
- 真实客户 PII 审批与真实月度批次；
- 生产主机、生产密钥、备份介质、告警接收人与正式发布。

自动门禁必须零真实模型调用、零真实客户数据、零外部模型 HTTP；测试只使用 deterministic/scripted provider。

## 2. 不变式

1. 不修改已有 Alembic 迁移；新增迁移必须可升级、可安全降级并通过 `alembic check`。
2. 不弱化 `row_result`、`sampling_audit`、`audit_log`、F6/F7/F8 不可变事实及租户复合外键。
3. 未脱敏 PII 不进入云 LLM；检索、Qdrant、embedding/rerank 仍限定内网路径。
4. 关停不得伪造成功：停止接收新的写任务；已开始的 F7 步完成持久化并写 checkpoint 后才在步边界退出；超时由平台强制终止后仍依赖既有幂等恢复。
5. 日志、trace 和错误响应不得包含密钥、原始 PII、完整 prompt、工具原始输入或隐藏推理。
6. 单机 Compose 绑定最小端口面；`.env` 无默认密钥，不提交私有数据或凭据。

## 3. Checkpoints

### CP-MVP.0 —— 规格与基线

- 固化本文件、现有缺口和外部验收边界。
- 记录 F8 基线：后端 `594 passed, 1 skipped`；前端 `92 passed`；5000 行 F1→F8 `230.203843s`。
- F6 当前主机复测 `3.763388s / 3.541288s` 作为诊断项；不篡改旧证据。

### CP-MVP.1 —— 运行边界与统一错误处理

- JSON 结构化日志，至少包含时间、级别、事件、logger、request_id；任务日志额外包含 `task_id`/阶段。
- 请求 ID 接受安全格式的 `X-Request-ID` 或服务端生成；响应回传，错误响应仍保持稳定公开 shape。
- 未处理异常统一收敛为无内部细节的 500；日志只记录异常类型和安全上下文。
- 全部响应接入 CSP、`nosniff`、frame/referrer/permissions policy；敏感 API 保持 `private, no-store`。
- 单进程有界滥用防护：登录按客户端与租户/用户名身份，认证写操作按会话身份；超限返回稳定 429，不记录原始凭据或会话 token。

### CP-MVP.2 —— 优雅退出与可观测归因

- 进程维护显式 drain 状态；关停后拒绝新写任务，读探针可继续报告 draining。
- Uvicorn 配置有界 graceful timeout。
- F7 每个模型/工具步骤完成数据库提交与 PostgreSQL checkpoint reconcile 后检查 drain；若已请求退出，追加显式人工终态，不再发下一次模型调用。
- 每个 F7 模型步骤记录可聚合的 `investigation_run_id`、`file_version_id`、step、provider/model、延迟、input/output/total token 与估算成本；价格未配置时成本为 `null` 而不是猜测。
- tracing 关闭时零 exporter 网络调用；配置缺失时显式 unavailable。

### CP-MVP.3 —— 安全红队与性能

- 离线 prompt 注入集合覆盖报销行、制度条款、prior steps、工具输出中的指令注入、数据外传、任意 URL/SQL/写工具请求。
- promptfoo 配置只能调用仓库内 deterministic provider，不访问真实模型；结构化 schema、工具白名单、PII 出站扫描和逐字引用测试必须同时通过。
- gitleaks 全历史、`pip-audit --strict`、完整/生产 `npm audit` 通过。
- 固定 seed 5000 行 F1→F8 总耗时 ≤ 900 秒；F8 交互 p95 ≤ 2 秒；记录并解释 F6 当前主机复测结果，若可复现代码热点则修复并加回归门禁。

### CP-MVP.4 —— 单机部署与运维文档

- 新增后端、前端生产镜像与 Compose 应用层；前端同源反代 `/api`，容器非 root，健康检查与停止宽限期明确。
- Compose 配置解析、镜像构建和无真实模型 smoke 通过；真实 embedding/rerank 与云 API 只验证配置 fail-closed，不调用。
- 部署指南覆盖首次安装、迁移、配置、健康检查、日志、备份、升级、回滚、故障恢复和外部验收清单。
- 回滚不得自动降级包含不可逆业务事实的迁移；默认策略是应用镜像回退 + 数据库前滚修复或从已验证备份恢复。

### CP-MVP.5 —— 最终交接

- 后端全量 pytest、Ruff format/lint、strict mypy、双库 migration/head 与 Alembic 零漂移通过。
- 前端全量测试、typecheck、oxlint、Prettier、生产 build 通过；OpenAPI/client 连续两次生成哈希稳定。
- Chrome 1440×1000 核心路径、错误态、只读角色与恶意文本门禁通过。
- pre-commit、diff check、私有路径/密钥扫描通过。
- 更新 `AGENTS.md`、`MEMORY.md`、本规格落地记录和交接文档，提交并推送。

## 4. 暂停边界

仅沿用 `AGENTS.md` 的自动闭环暂停条件：破坏性/不可逆操作、真实凭据或付费服务、真实客户数据、改变安全硬约束、权限/UAC 外部阻塞，或同一阻塞连续三次且无安全推进路径。其余失败必须诊断、修复并继续。

## 5. 落地记录（2026-08-11）

### CP-MVP.0–CP-MVP.2

- 统一请求中间件已落地：安全 `X-Request-ID`、结构化 JSON 请求日志、安全响应头、登录/写操作有界限流、drain 后稳定拒绝新写请求；未处理异常仅返回无内部细节的公开 500。
- 生产配置 fail closed：Secure session cookie、trace endpoint 和价格字段均受强类型配置约束；空价格不伪造成本。
- drain controller、readiness `draining` 状态及 Uvicorn 有界 graceful timeout 已落地。真实 SIGTERM smoke 显示先记录 `runtime.drain_requested`，再清洁退出并可重新 ready。
- F7 在 evidence step 数据库提交及 PostgreSQL checkpoint reconcile 后检查 drain；测试机械确认 provider 只调用一次、步骤与 checkpoint 均已持久化，并追加 `SHUTDOWN_REQUESTED` 人工终态。
- 日志与 OTLP span 只使用安全字段白名单；模型步骤可按 task/file/run/step 归因 token、延迟和可选成本，tracing 未启用时无 exporter 网络调用。

### CP-MVP.3

- promptfoo `0.122.0` 通过隔离容器运行：`--network none`、只读根文件系统与只读用例挂载，8 类报销行/制度/prior-step/工具输出注入、外传、任意 URL/SQL/写工具攻击全部通过（8 passed，0 failed/error），未调用真实模型。
- promptfoo 不进入应用 npm 依赖；`security/promptfoo` 本身 audit 为 0。后端 `pip-audit --strict`、前端完整 npm audit 均为 0。
- 固定 seed=3500 的 5000 行 F1→F8 为 `144.984006s`（上限 900 秒）；F7 `12.075655s`，130 次 scripted provider，真实模型/外部 HTTP 为 0；F8 产出 1180 item/1916 row。
- F8 run/config/detail/list/replay/rows p95 为 `0.110630/0.037162/0.006338/0.051872/0.126245/0.010018s`。旧 F6 harness 在关闭额外健康探针后 initial 为 `2.068160s`，仅高于历史 2 秒门槛约 3.4%，未定位到可复现代码热点，故不改动既有 F6 事务与恢复语义。

### CP-MVP.4

- 后端/前端生产 Dockerfile、应用 Compose 叠加层和 Nginx 同源 `/api` 代理已落地；API 与前端均以非 root、只读根文件系统运行，健康/ready/proxy、安全头和 JSON 日志 smoke 通过。
- 后端默认生产镜像构建通过。Docker Hub 认证端点不可达导致默认 Node 24/Nginx 1.28 基础镜像未能重新拉取；已用本机缓存 Node 22/Nginx 1.27 验证完全相同的 Dockerfile 构建与运行逻辑。目标网络的默认版本构建保留为 `external_validation_pending`。
- `docs/DEPLOYMENT.md` 覆盖首次安装、配置、迁移、健康、日志、备份、升级、应用回退、前滚修复/备份恢复、故障排查和外部验收清单。

### CP-MVP.5

- 后端全量：`600 passed, 1 skipped`；Ruff lint、246 文件 format check、strict mypy 165 source 通过；默认库/测试库均为 `0010 (head)`，`alembic check` 无新操作。
- 前端全量：17 文件/92 passed，typecheck、oxlint、Prettier、生产 build 通过；OpenAPI/client 连续两次生成 SHA-256 稳定（OpenAPI `530e7193…ad6`，client `84cfab59…24f`）。
- 代码就绪范围内的部署、安全、性能与恢复门禁全部完成。真实云 API、本地 embedding/rerank、客户 PII/月度批次、目标主机构建、备份恢复演练、告警与正式发布继续明确标记为 `external_validation_pending`，不得将本记录解释为生产验收完成。
