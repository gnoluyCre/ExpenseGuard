# 系统记忆与上下文 🧠
<!--
AGENTS:在每个重要里程碑、结构性变更或修复 bug 后更新本文件。
若历史上下文仍然相关,请勿删除;对较早的已完成项进行压缩。
-->

## 🏗️ 当前阶段与目标
**当前任务:阶段 2 F5 已完成 CP-F5.0–CP-F5.4。** `specs/006-phase2-f5-human-review.md` 是 F5 的唯一规范来源；下一实施点为 CP-F5.5 契约与交付门禁。

- CP0 仓库重置(干净历史、`.gitignore` 脱敏排除)
- CP1 后端地基(uv + 18 张表 + Alembic 三层隔离)
- CP2 幂等原语与恢复测试(项目 #1 优先级,含反向验证)
- CP3 认证、RBAC、租户隔离(含反向验证)
- CP4 前端垂直切片 + OpenAPI 契约门禁 + pre-commit/CI + 合成数据生成器(含反向验证)

**下一步:CP-F5.5 · 契约与交付门禁。** 完成 F5 全量后端/前端、双库 Alembic、OpenAPI 二次生成、pre-commit/gitleaks、依赖审计、固定 seed 5000 行 auto-plan/交互性能与 1440×1000 全状态视觉证据；不提前进入 F6/F7/F8。
`process_row_once` 的首个生产调用方现为 `app.core.validation.batch_service.validate_batch`；行内 finding 与 `row_result` 使用同一 session/事务，`row_result.rule_version` 固定保存规则集指纹。

**开工前必读的两件事:**
1. 契约同步是硬要求:改了 Pydantic 模型 → `cd backend && uv run python scripts/export_openapi.py` + `cd frontend && npm run gen:api`,两个生成物都要提交,否则 CI 的 contract job 直接红。
2. 评测门禁已就位但处于待命:`backend/evals/baseline.json` 的 `thresholds` 一填数值就自动开始阻断,**不需要改任何 workflow YAML**。

**遗留缺口:** W0 运行态仍未闭环 —— 2026-07-28 已验证代码侧 fake/HTTP 双路径、prod 禁 fake、模型/Qdrant 主机白名单和真实 Qdrant；但 pinned Infinity 镜像拉取停在 registry layer，`docker manifest inspect` 也超时，按有界策略终止。官方支持把预下载模型目录挂入容器并以容器内路径作为 `--model-id`，但客户离线权重包、镜像可达性、实际 embed/rerank 质量与资源占用仍需外部网络/权重输入后实测。GitHub CI 远端状态也待确认。

## 📂 架构决策
*(把构建过程中做出的具体选择记录在此,便于后续 agent 遵循)*
- 2026-07-29 — **F5 CP-F5.4 桌面复核台闭包完成。** `/review` 占位页替换为权限驱动的工业审计工作台：顶部原始 coverage/config/plan 状态，左侧稳定联合队列与筛选分页，中部原始行/规范化投影/冻结 reasoning/evidence/rule/version/citation 同屏，右侧 config/legacy plan 控制，底部两类不可变 decision 二次确认。前端只消费 CP-F5.3 生成类型；新增 Zod 作为表单运行时边界，保留正常 Unicode 并拒绝控制字符，`false_positive|missed_issue` 条件必填非空白 note。mutation 成功或 409 冲突均失效相应 review/config 查询，队列回 offset 0，冲突后展示服务端最终事实/最新 config 而不伪造成功；raw/note/quote 不写 URL、storage 或 analytics。新增 22 项 F5 前端测试，前端全量 `45 passed`，typecheck/oxlint/Prettier/build/npm audit 全绿。真实 Google Chrome 1440×1000 覆盖 auditor finding、auditor pure-passed sample、configurator、viewer 共 4 场景：页面级横向溢出、脚本/图片执行、浏览器存储与 viewer review API 请求均为 0；私有证据位于 `data/private/cp-f5.4/`。未修改后端、Pydantic/OpenAPI、迁移、基础设施或 F6/F8。
- 2026-07-29 — **F5 CP-F5.3 API 与 OpenAPI 契约闭包完成。** 新增 10 个 config/plan/queue/detail/decision/summary 端点，全部经现有 F5 服务层且无路由 SQL；queue 与 plan/decision 使用 Pydantic discriminator，config history 显式区分 current/history，legacy queue/plan 明示 `legacy_not_initialized`。RBAC 沿用 permission 数据，auditor/configurator 可读写复核、仅 configurator 可写 config、viewer 无 review 权限；跨租户 report/finding/sample 稳定 404。所有 F5 响应使用 `private, no-store`，CORS 显式允许 `Idempotency-Key`；note 条件校验、幂等/已完成冲突和错误 shape 使用稳定 code。修正 sample detail 规则集指纹来源为 frozen report，并用 `{completed,total}` 暴露两类精确 coverage。CP-F5.3/F5 定向 `36 passed`，后端全量 `391 passed, 1 skipped`；Ruff、157 文件 format、strict mypy（115 源文件）、OpenAPI/client 连续二次哈希稳定通过。前端 23 tests、typecheck/oxlint/Prettier/build 全绿；未新增依赖、迁移、UI 或 F6/F8。
- 2026-07-29 — **F5 CP-F5.2 抽样核心、计划与复核服务闭包完成。** 新增 `app.core.reviews` 严格类型领域层：sampling config canonical fingerprint 只绑定 schema/algorithm/rate/min/max，request fingerprint 另绑定 tenant/expected version/reason hash；`sha256-rank-v1` 严格使用 32-byte seed、RFC 4122 UUID bytes 与 unsigned big-endian 8-byte row 编码，整数样本量公式及 golden vectors 固定。新报告在现有 Tenant→FileVersion NOWAIT 事务中于 completed 前原子写 plan/sample/`sampling.plan_create`，缺 config 在 report 写入前失败；plan 故障只留一条带稳定 sampling reason 的 `batch.report_failed`。legacy completed report 通过追加式 key ledger 显式补建/复用，重放不访问 CSPRNG、当前 config、Qdrant 或模型，独立失败写 `sampling.plan_failed`。联合 queue/detail/summary 只读 F4 snapshot 与原始行；finding/clearance 两类 decision 分表一次性追加，note 只入业务表，审计仅含 enum/ID/hash，故障通过独立事务写无 PII failed audit。39 项 CP-F5.2/F4 定向测试、后端全量 `386 passed, 1 skipped`、Ruff/format（155 文件）与 strict mypy（114 源文件）通过；未新增依赖、迁移、API/UI 或 OpenAPI 变更。
- 2026-07-29 — **F5 CP-F5.1 持久化闭包完成。** 只新增 `0007_f5_human_review.py`，未修改 `0001`–`0006`；落地 `review_sampling_config`/`review_sampling_plan`/`sampling_review`/`review_plan_request`，强化 `review` 与 `sampling_audit`。config 快照参数通过宽复合 FK 与 plan 物理一致；review target 通过 report item/finding/report/file/tenant 单条复合身份闭合；sample 同时复合绑定 plan/report/expense_row/row_result/tenant。为支持完整 FK，只增强地追加 `report_item`/`expense_row`/`row_result` 冗余唯一键，原 `row_result(file_version_id,row_no)` 与 `sampling_audit(file_version_id,row_no)` 受保护约束保持不变。六类 F5 事实均由 DB 触发器拒绝 UPDATE/DELETE，全部 FK 使用 RESTRICT；legacy review/sample 非空时 upgrade 在任何 DDL 前 fail closed，存在 F5 数据时 downgrade 同样在 DDL 前拒绝。默认库 pre-0007 备份位于 `data/private/backups/cp-f5.1/pre-0007-20260729-160753/`；full/schema/affected-data 归档的 SHA-256 分别为 `6c9baa69e6abe88ced0810f9cd510540c821bd160d94094765395a363cea3cd4`、`53b1a21488b6f609d9ce8d085f15229b579cade332f18f89ef8f75ea0baf80e0`、`188ea6f89bbcf9a5e15747c4e53ee828155f30ec7d06713ac2426798ea71c9cd`，均通过 `pg_restore --list` 与容器/本地 hash 交叉校验。迁移定向 `36 passed`，后端全量 `355 passed, 1 skipped`；Ruff、strict mypy（106 源文件）与默认/测试双库 `0007` Alembic 零漂移通过。
- 2026-07-29 — **F5 CP-F5.0 规格固化完成。** `specs/006-phase2-f5-human-review.md` 成为 CP-F5.0–F5.5 唯一规范来源。finding 复核保持 `confirmed|false_positive`，被放行抽检改用语义独立的 `clearance_confirmed|missed_issue`；现有 `sampling_audit` 只承载不可变选择事实，legacy decision/reviewer/reviewed_at 不再写入，结论追加到独立 `sampling_review`。抽样由版本化数据配置驱动，使用一次性 CSPRNG seed 与冻结的 `sha256-rank-v1` 稳定排序，可按保存的 config/seed/score/rank 机械复算。F5 上线后的新 report 必须在同一成功事务内创建 plan/sample，缺 config 即在写入前失败；只有上线前 legacy completed report 可通过显式幂等 POST 补建，所有 GET 保持只读。队列只消费 F4 immutable snapshot，排序为 high attention → manual attention → clearance sample，不读取 F6、不计算 F8、不改写机器结论。
- 2026-07-29 — **F4 CP-F4.5 契约与交付门禁完成，F4 状态推进为已完成。** F4/幂等/恢复/迁移定向 `94 passed`；pytest 9.1.1 下后端全量 `352 passed, 1 skipped`，Ruff、142 文件 format check、strict mypy（105 源文件）、默认/测试双库 `0006 (head)` 与 Alembic 零漂移全部通过。`pip-audit` 发现 pytest 8.4.2 的 `PYSEC-2026-1845` 后，将 dev 约束提升为 `pytest>=9.0.3,<10` 并锁定 9.1.1；升级后全量回归与审计零漏洞。OpenAPI/client 连续两轮及前后哈希一致；前端 8 文件/23 tests、typecheck/oxlint/Prettier/build/npm audit 与 pre-commit/gitleaks 全绿。固定 seed=3500 的 5000 行 F1→F2→F3→F4→XLSX 总耗时 `108.260723s`，报告 `8.331935s`/3150 SQL（SQL 累计 `4.496375s`），XLSX `4.990587s`/13 SQL，artifact `345735` bytes；1045 finding 全部形成 item + verified citation，0 unavailable，低于 900 秒硬上限。精确 1440×1000 Chrome 覆盖 report/policy 的 normal/empty/loading/error 与 viewer/configurator 权限共 8 场景，页面级横向溢出、脚本/图片注入均为 0；发现并修复制度账本长 stable key 的内部裁切，修复后仅保留显式 title truncate。私有性能/视觉证据位于 `data/private/cp-f4.5/`。
- 2026-07-29 — **F4 CP-F4.4 API、桌面工作流与 XLSX 闭包完成。** policy family/document/publish、候选检索、正式 binding/history、report generate/read/items/parse-errors 与 export create/download 全部通过服务层暴露为强类型、权限驱动、租户隔离 API；`policy_change` 已接入 revision API。报告查询固定筛选/分页/排序，前端新增制度证据库与批次不可变报告视图，候选明确标注“仅供配置”，正式报告只展示已冻结 binding/citation。XLSX 固定 5 张表与列序，公式/DDE/超链接/宏/外链/对象 fail closed，单元格注入和 32767 字符边界机械处理；artifact 生成、重放、篡改检测、下载审计分离，文件只落 `data/private/`。定向后端 34 passed、后端全量 352 passed/1 skipped、前端 23 passed；Ruff、strict mypy（105 源文件）、双库 Alembic、OpenAPI/client 连续二次无漂移、typecheck/oxlint/Prettier/build 与 pre-commit/gitleaks 均通过。真实 Chrome 1418px 视口验证报告/制度页无横向溢出或 alert，截图保存在 gitignored `data/private/visual-cp-f4-4/`。
- 2026-07-28 — **F4 CP-F4.3 Binding、exact quote 与原子报告闭包完成。** 正式 binding 仅由 configurator 在 tenant NOWAIT 锁内保存 1–3 条连续有序引用，PG 校验 published/[effective,expiry)/family-document-clause 身份与 frozen hash；exact verifier 只接受调用方必填的 Python Unicode code point、end-exclusive 连续切片，失败候选通过 Pydantic `hide_input_in_errors` 与安全异常保证不进入 DB/audit/log。报告沿用 `Tenant → FileVersion NOWAIT`，显式校验 actor tenant scope，并在冻结引用前机械重算 binding fingerprint；只从 completed F3 + PG binding 装配 report/item/parse-error/citation/count/成功审计。任一引用失败整条 item citation unavailable、零部分引用，失败事务全回滚后独立写无 PII 审计，completed replay/read 不访问 Qdrant/模型/当前 binding。新增 `0006`（不改 0001–0005）扩展 `policy_change` 并增加 append-only `report_request` key ledger，解决 completed report 新 key 复用与同 key 异请求永久冲突；`policy_change` 复制原始/解析快照但不复制 F3/report 副作用。pre-0006 私有 full/schema/affected-data 备份目录为 `data/private/backups/cp-f4.3/pre-0006-20260728-190828/`，三份 SHA-256 分别为 `96614363fa9a5471c232535b5ecf69bedc6f1a0ef15e5bca3d0d7d55da08da3b`、`d7b4bb6cf55d03d734c992bbf9a5df8bb2a6635ff6f284ee0d420c0e1d0e5452`、`eb2f85a491edf4922fa6b68bf598da17babe83db367cd75959f1763aaf5c816a`，均通过 `pg_restore --list` 与容器/本地 hash 交叉验证。CP-F4.3 定向 65 passed；exact verifier statement/branch 100%；后端全量 318 passed/1 skipped；Ruff、strict mypy（99 源文件）、双库 0006/Alembic、OpenAPI/client 二次无漂移、pre-commit/gitleaks、pip-audit、前端 20 tests/typecheck/lint/format/build 全绿。
- 2026-07-28 — **F4 CP-F4.2 本地制度检索闭包完成。** 私有源文件使用 tenant/SHA-256 内容寻址；解析只接受 PDF 文本层、DOCX 段落和 UTF-8 TXT 的显式编号条款，禁止 OCR/猜测边界。Qdrant 只产生候选，PG 对 tenant/date/generation/provenance/hash/连续切片做二次校验。本地模型端点与 Qdrant 都受显式主机白名单保护，prod 禁用 fake provider。outbox 使用 generation-aware lease，支持 Qdrant side effect 后崩溃重放、terminal/manual retry、前序 expiry payload 刷新和 building manifest delta。未实现 binding/report/API/UI。后端全量 269 passed/1 skipped，Ruff、strict mypy（93 源文件）、双库 Alembic、OpenAPI 二次生成、pre-commit/gitleaks 与 pip-audit 全绿。W0 镜像拉取因 registry/manifest 超时未完成，保持显式外部缺口。
- 2026-07-28 — **F4 CP-F4.1 持久化闭包完成。** `0005_f4_policy_reports.py` 是唯一新增迁移：legacy document 仅一次性回填 `legacy_unpublished` 且新写入不得伪装 legacy；旧 policy CASCADE/SET NULL FK 被复合 tenant `RESTRICT` 替换；published family interval 由 `btree_gist` 半开区间排斥约束保证；source blob、published policy/clause/chunk、binding、report snapshot 与 completed export 由数据库不可变触发器保护。binding→family/document/clause 与 citation→binding identity 使用完整复合 FK，避免同租户 ID 拼接错配。默认库 pre-0005 三份 custom archive 已通过 `pg_restore --list` 与 SHA-256 交叉验证；默认/测试双库均为 0005 且 Alembic 零漂移。CP-F4.1 定向 4 passed、迁移/恢复组 39 passed、后端全量 244 passed/1 skipped，Ruff 与 strict mypy 通过。详见 spec §17。
- 2026-07-28 — **F4 CP-F4.0 规格固化完成，导出采用 XLSX-only。** 正式制度依据必须是 configurator-confirmed、版本化 `rule_policy_binding`，并以 PostgreSQL clause 原文、必填 end-exclusive offsets 做严格 Unicode code point 切片校验；Qdrant/本地 rerank 只提供候选，不能直接成为报告依据。F4 report path 不调用云 LLM，报告生成沿用 F3 的 Tenant→FileVersion 锁序并在单事务中提交 report/item/parse-error/citation/count/成功审计，失败整批回滚。citation 状态与 F3 attention group 正交；索引/model/chunker provenance 不参与 report identity。XLSX 固定 5 张表，artifact 生成与下载 API/审计分离。详见 `specs/005-phase2-f4-report-generation.md`。
- 2026-07-28 — **CP-F3.5 交付门禁完成，F3 状态推进为已完成。** 后端全量 240 passed/1 skipped、迁移定向 27 passed；前端 20 passed；静态检查、双库 Alembic、受保护约束、OpenAPI 二次生成、pre-commit/gitleaks 和生产构建全部通过。5000 行五类校验耗时 48.304265 秒、11,056 条 SQL、1,045 个 finding，低于 900 秒硬上限；该本机数值仅作证据，不成为更严格产品承诺。1440×1000 Chrome 覆盖 normal/empty/loading/error，无页面级横向溢出。无业务范围偏差、无受保护文件改动、无 F4/F5/F6 提前实现。
- 2026-07-28 — **CP-F3.4 的 API 与前端只消费同一组判别联合契约。** `SaveRuleRequest.definition` 直接使用 `RuleDefinition`，finding evidence 使用 `RuleEvidence`，关联 verdict 使用完整 `RowVerdict`；只有查询筛选仍限制为 `flagged|manual_review`。规则 PUT 的 Pydantic 边界错误统一映射 `RULE_CONFIG_INVALID`，前端不复制手写 DTO。
- 2026-07-28 — **F3 桌面入口按 permission 而非角色控制。** `CONFIG_READ` 显示规则版本页，`CONFIG_WRITE` 才显示保存；`BATCH_READ` 可查看 validation/findings，`BATCH_IMPORT` 才可 validate/派生。mutation 统一失效批次、解析、validation、findings 和规则缓存；两类派生请求各自携带 8–128 字符 Idempotency-Key。
- 2026-07-28 — **CP-F3.3 统一使用 `Tenant FOR UPDATE NOWAIT → FileVersion FOR UPDATE NOWAIT` 锁序。** 规则保存、批次校验和 F2 parse/reparse 共享租户锁；同租户冲突稳定返回领域 409，不同租户可并行。F2 在锁内拒绝已校验批次和被 `validation_dependency` 引用的来源，消除 dependency 检查与重解析交错提交窗口。
- 2026-07-28 — **查重 dependency 冻结完整候选来源，而非只冻结实际命中来源。** 快照时排除当前 root lineage，对每个其他 lineage 选择最高成功解析 revision 并全部写入 dependency；编排层只构造 `InvoiceOccurrence`，唯一首条仍由 CP-F3.2 `select_duplicate_match` 决定。
- 2026-07-28 — **F3 成功副作用整批一次提交，系统失败只保留独立无 PII 审计。** validation run、dependencies、findings、row_results、完成计数和 `batch.validate` 同一事务；任一故障先回滚主事务、释放锁，再用绑定租户的新 session 追加 `batch.validate_failed`。completed 重放在锁内早退且不新增任何副作用。
- 2026-07-28 — **派生 revision 的幂等请求只按规格约束 8–128 字符。** key 保存 SHA-256，canonical 请求指纹绑定 source/reason/schema；`ruleset_change` 复制解析快照，`mapping_change` 只复制原始证据。普通 F1 上传显式只查询 revision 1，派生版本不会改变默认内容哈希复用语义。
- 2026-07-28 — **Docker Desktop 未启动是可自动恢复的本地环境状态,不是默认人工阻塞。** Docker 相关开发/测试先用官方 `docker desktop start` + 最多 120 秒 `docker info` 轮询恢复引擎,再只启动最小 Compose 服务；当前数据库测试只启动 `postgres`。不自动启动 embedding/trace,不停止既有容器,不执行 `down`/删卷；仅提权、交互许可或有界启动失败时转人工。本次机械验证从 Desktop stopped 到 PostgreSQL healthy 约 28 秒,随后后端全量 `202 passed, 1 skipped`。
- 2026-07-28 — **CP-F3.2 把发票号“唯一首条”保留在纯规则核心。** CP-F3.3 只负责按租户/快照装载当前批与其他 lineage 的最高成功 revision occurrence；`select_duplicate_match` 排除当前 lineage 的历史候选，并按 root revision 1 的 `(uploaded_at, id, row_no)` 稳定选择，输入冲突时 fail closed，不猜测。
- 2026-07-28 — **F3 evidence 自身校验 kind/outcome/reason 与命中字段完整性。** `RULE_NOT_EFFECTIVE` 通过纯 selection-to-evaluation 路径生成；passed 固定无 evidence，exempted 不提升 verdict，大型允许集合仅写指纹。F2 `NormalizedExpenseRecord` 同步补上 ISO 日期语义校验，避免非法日历日期进入时效 evaluator 形成未分类异常。
- 2026-07-28 — **CP-F3.2 全量门禁完成。** 规则定向 52 passed、包覆盖率 93%；后端全量 202 passed/1 skipped；Ruff、格式、strict mypy 与 OpenAPI/前端客户端二次生成无漂移。首次全量因 Docker Desktop 未运行而等待 PostgreSQL,现已通过自动恢复引擎与最小 `postgres` 服务闭环；系统 Temp ACL 通过将 pytest 临时根指向 gitignored `data/private/` 解决,未修改或跳过测试。
- 2026-07-28 — **CP-F3.1 的 validation run 只持久化 `in_progress|completed`。** 系统失败整批回滚并用独立审计记录，不保留猜测性的 failed run；`ruleset_manifest` 与六项非负/恒等计数 CHECK 成为 CP-F3.2/3.3 的存储契约。新增 F3 引用统一 `ON DELETE RESTRICT`；通过新增 `app_user(id, tenant_id)` 冗余唯一约束，让规则创建人和校验触发人也使用租户复合外键。
- 2026-07-28 — **`0004` downgrade 对派生 revision fail closed。** 任何 DDL 前先检测 `revision_no > 1`，存在即拒绝且由事务保持 schema/数据不变；生产降级只允许从已验证 pre-0004 完整归档恢复到隔离库。发票号 JSONB 表达式索引暂不添加，只有真实性能门禁证明需要时才添加。
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

- CP-F5.3 实测：CP-F5.3/F5 定向 **36 passed**；后端全量 **391 passed, 1 skipped**；Ruff lint、157 文件 format、strict mypy（115 个源文件）与 OpenAPI `--check` 通过。OpenAPI/client 连续二次 SHA-256 稳定；前端 **23 passed**、typecheck/oxlint/Prettier/生产 build 通过。
- CP-F5.1 实测：迁移/受保护约束定向 **36 passed**；后端全量 **355 passed, 1 skipped**；Ruff lint/format、strict mypy（106 个源文件）、默认/测试双库 `0007 (head)` 与 `alembic check` 通过。
- CP-F4.4 实测：定向后端 **34 passed**；后端全量 **352 passed, 1 skipped**；Ruff lint/format、strict mypy（105 个源文件）、默认/测试双库 `alembic check`、OpenAPI/客户端连续二次生成无漂移、pre-commit/gitleaks 通过。前端 **8 个文件、23 passed**，typecheck/oxlint/Prettier/生产 build 通过；1418px Chrome 实际视口下报告页与制度页无页面级横向溢出或 alert。
- CP-F4.3 实测：定向 **65 passed**；strict exact verifier **23 passed** 且 statement/branch coverage **100%**；后端全量 **318 passed, 1 skipped**；Ruff lint/format、strict mypy（99 个源文件）、默认/测试双库 `0006 (head)` 与 `alembic check`、0006 往返/安全 downgrade、受保护约束回归、OpenAPI/客户端连续二次生成无漂移、pre-commit/gitleaks、`pip-audit --strict` 全部通过。前端 **20 passed**，typecheck/oxlint/Prettier/生产 build 通过。
- CP-F3.5 实测：后端全量 **240 passed, 1 skipped**、迁移定向 **27 passed**；前端 **6 个文件、20 passed**；Ruff/格式、strict mypy（79 个源文件）、双库 `alembic check`、受保护约束、OpenAPI/客户端连续二次生成、TypeScript/oxlint/Prettier/build、pre-commit/gitleaks 全部通过。5000 行五类校验 **48.304265 秒**、SQL **11,056**、finding **1,045**；1440×1000 Chrome 全状态复核无页面级横向溢出。
- CP-F3.1 实测：迁移目录 **27 passed**；后端全量 **149 passed, 1 skipped**；Ruff lint/格式、strict mypy（67 个源文件）、测试库与默认开发库 `alembic check` 全部通过。默认开发库已在三份 custom archive 经 `pg_restore --list` 与 SHA-256 验证后升级至 `0004`。
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
- [x] F3 CP-F3.1（`0004` 持久化 schema/ORM、legacy 回填、租户复合外键、安全 downgrade 与 pre-0004 备份）
- [x] F3 CP-F3.2（五类强类型规则、纯确定性 evaluator、canonical/规则集指纹、evidence/reasoning 与稳定查重首条）
- [x] F3 CP-F3.3（快照、编排、租户级并发、行级幂等、失败审计与派生 revision）
- [x] F3 CP-F3.4（类型化 API、OpenAPI/前端 schema、规则版本控制台与批次确定性校验视图）
- [x] F3 CP-F3.5（全量回归、迁移/受保护约束、契约、安全、5000 行性能与桌面视觉交付门禁）
- [x] F4 CP-F4.0（制度版本/本地检索候选/人工 binding/严格逐字引用/原子报告快照/XLSX-only 规格）
- [x] F4 CP-F4.1（0005 policy/index/binding/report/export 持久化、legacy 安全回填、复合租户 FK、GiST 与安全 downgrade）
- [x] F4 CP-F4.2（私有制度导入、确定性解析、Qdrant generation/outbox、本地候选检索与 PG 二次校验）
- [x] F4 CP-F4.3（configurator-confirmed binding、strict exact quote、原子 report snapshot、幂等/恢复与 policy_change）
- [x] F4 CP-F4.4（强类型 policy/binding/report/export API、制度证据库、批次报告视图、五表安全 XLSX）
- [x] 认证集成（server-side session + RBAC 三角色 + 租户过滤 fail-closed）
- [x] 前端垂直切片（登录 / 路由守卫 / 三角色外壳 / 系统状态页）
- [x] OpenAPI 契约门禁 + pre-commit + GitHub Actions CI
- [x] 合成数据生成器骨架（确定性 + 标签物理分离）
