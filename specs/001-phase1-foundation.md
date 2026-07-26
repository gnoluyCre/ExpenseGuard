# Spec 001 · Phase 1 地基

**状态:** 进行中（CP0–CP2 已完成，CP3–CP4 待做）
**最近更新:** 2026-07-27

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

### 待做:租户过滤反向验证

CP3 完成后需做:临时注释掉 `core/tenancy/scope.py` 的事件监听器,
跨租户测试必须变红。

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

---

## 待办

- [ ] CP3 · 认证、RBAC 与租户隔离
- [ ] CP4 · 前端垂直切片 + OpenAPI 契约 + CI
- [ ] 补 `docker-compose.models.yml`(embedding/rerank)与 `docker-compose.observability.yml`(Langfuse)
- [ ] 合成数据生成器 `app/synth/`
