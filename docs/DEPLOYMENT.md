# ExpenseGuard 单机部署与回滚

本文面向“代码就绪 MVP”的内网单机部署。真实云模型、本地权重、客户批次、PII 审批和生产发布必须按文末清单另行验收。

## 1. 前置条件

- Docker Engine / Docker Desktop 与 Compose v2；
- 至少 PostgreSQL、Qdrant 所需磁盘；使用真实 embedding/rerank 时另按模型要求准备 CPU/GPU 与离线权重；
- 内网 TLS 反向代理或受控终端入口。生产配置强制 Secure cookie，不能把明文 HTTP 当正式入口；
- 备份目录位于受控磁盘，不在仓库内。

复制 `.env.example` 为 `.env`。至少配置强 PostgreSQL 密码、`PUBLIC_ORIGIN`、PII token 密钥；启用云模型时再填 OpenAI-compatible 参数。不得把 `.env` 发到工单、聊天或 Git。

## 2. 配置校验与启动

```bash
docker compose -f docker-compose.yml -f docker-compose.app.yml config --quiet
docker compose -f docker-compose.yml -f docker-compose.app.yml build
docker compose -f docker-compose.yml -f docker-compose.app.yml up -d
```

如需真实本地 embedding/rerank，再叠加 `docker-compose.models.yml`；如需 Langfuse，再叠加 `docker-compose.observability.yml`。这两项不随默认应用栈隐式启动。

API 容器启动时先执行 `alembic upgrade head`，成功后才启动服务。检查：

```bash
curl --fail http://127.0.0.1:8000/api/health
curl --fail http://127.0.0.1:8000/api/health/ready
curl --fail http://127.0.0.1:8080/healthz
docker compose -f docker-compose.yml -f docker-compose.app.yml ps
```

`ready` 失败只摘流量，不应触发反复重启；`health` 失败才表示进程不可用。应用日志为单行 JSON，可按 `request_id`、`task_id`、`investigation_run_id` 聚合。模型步骤只记录 provider/model、token、延迟和可选估算成本，不记录 prompt、PII、密钥或工具原文。

## 3. 安全退出

应用收到 SIGTERM 后立即进入 drain：新写请求返回 `SERVICE_DRAINING`，就绪探针返回 503；在途 F7 当前步骤提交业务事实并完成 PostgreSQL checkpoint reconcile 后，不再调用下一次模型，追加 `SHUTDOWN_REQUESTED` 人工终态。Compose 给 API 330 秒停止宽限期，Uvicorn 内部等待上限为 300 秒。

若平台在宽限期后强杀，重启依赖既有 request ledger、唯一约束和 checkpoint 恢复；不得手工删除 `IN_PROGRESS` 事实。

## 4. 备份、升级与回滚

升级前必须：

1. 记录当前镜像 digest、Git commit 和 `alembic current`；
2. 用 `pg_dump -Fc` 备份业务库并执行 `pg_restore --list`；
3. 备份 Qdrant snapshot 与 `private_data` 卷，记录 SHA-256；
4. 在隔离数据库恢复 PostgreSQL 备份并验证迁移 head；
5. 再拉取/构建新镜像并执行升级。

默认回滚策略：

- **应用错误且 schema 向后兼容：** 把 Compose 镜像恢复到上一个已记录 digest，重新启动并跑健康/关键查询 smoke；
- **迁移已写入业务事实：** 不自动执行 `alembic downgrade`。采用前滚修复；若无法安全前滚，则停止写流量，从升级前已验证备份整体恢复 PostgreSQL、Qdrant 和私有卷；
- **仅配置错误：** 恢复上一版受控 `.env`，重建容器；密钥疑似泄漏必须轮换，不能只回退文件；
- **模型不可用：** 把强模型能力显式设为 disabled/unavailable 并转人工，不得切到未批准公网服务。embedding/rerank 不得出内网。

恢复后核对审计日志、F1–F8 最新完成 run、row_result 唯一性、抽样计划、调查 checkpoint 和二维分级 snapshot。禁止用删除事实表或修改已有迁移的方式“修复”状态。

## 5. 正式发布前外部验收

以下全部保持 `external_validation_pending`，直到负责人留下真实证据：

- 云 API 凭据、脱敏后真实冒烟、token/延迟/成本与限额；
- 客户批准的本地 embedding/rerank 权重、召回质量、资源占用和镜像可达性；
- 客户 PII 字段清单、出站审批与 prompt 注入复核；
- 一个真实月度批次端到端手工验证及被放行样本抽检；
- 生产 TLS、备份介质、恢复演练、监控告警接收人与发布/回滚负责人。

未完成上述项目时，只能称“代码就绪 MVP”，不得宣称生产验收完成。
