# Spec 006 — Phase 2 F5 人工复核台与被放行样本抽检

**状态：** CP-F5.0–CP-F5.4 已完成 ✅
**日期：** 2026-07-29
**前置检查点：** F4 / CP-F4.5 已完成
**下一实施检查点：** CP-F5.5 契约与交付门禁

---

## 1. 目标与范围

F5 把 F4 首次成功、不可变的报告快照转化为可执行的人在回路工作流，并从第一个正式批次起对系统判定为 passed 的行做随机抽检。其目标不是让人工改写历史报告，而是产生两类独立、可审计的真实标签：

1. 对 finding 的复核：`confirmed` 表示确认该 finding 成立，`false_positive` 表示确认该 finding 是误报。
2. 对被放行行的抽检：`clearance_confirmed` 表示确认原放行正确，`missed_issue` 表示发现系统漏放。

F5 的核心交付是以下可机械验证的不变量：

1. 复核只消费 completed F4 report/item/citation 快照与对应原始行，不重新运行规则、检索制度或改写历史证据。
2. 每个 finding 最多一个最终复核结论；每个抽检样本最多一个最终抽检结论。结论、复核人和服务端时间戳一经提交不可更新或删除。
3. 随机抽检只从该 report 对应的 `row_result.verdict = passed` 且解析成功的行中选择；选择算法、配置版本、seed、候选数、样本量、排序分数全部冻结，可离线复算。
4. 抽样选择记录与抽检结论分表追加：`sampling_audit` 只记录不可变的“被选中”事实，结论写入独立 `sampling_review`，不原地更新选择记录。
5. mutation 与无 PII 审计在同一事务提交；同 key 重放不重复标签或审计，同 key 异请求与新 key 重复决定均显式冲突。
6. 不因 citation unavailable、证据不充分或模型能力缺失而猜测结论；界面必须明确转人工，最终标签只能来自有 `REVIEW_SUBMIT` 权限的人。

### 1.1 明确不在 F5 范围内

- 不实现 F6 跨行关联检测，不读取或展示 `correlation_finding`。
- 不实现 F7 ReAct agent，不新增或展示 `evidence_step` 工具循环。
- 不实现 F8 二维分级、风险分数或代价敏感阈值；队列只使用 F4 冻结的 `attention_group`。
- 不自动修改规则、policy binding、finding、row_result、report、citation 或历史 XLSX。
- `missed_issue` 不自动制造 finding 或派生 revision；它形成真实标签并明确提示人工升级处理。
- 不做自动评测集导出、阈值重标定、置信区间仪表盘或模型训练；这些属于 PRD P2/后续评测闭环。
- 不做任务分派、认领、批量判定、四眼复核、结论撤销/更正、评论线程、通知或 SLA。
- 不把 parse error 伪装成 finding 复核项；解析错误继续在 F4 错误清单处理。
- 不做移动端、平板或新的报告导出格式。
- 不修改 `0001`–`0006`、`docker-compose.yml`、Dockerfile 或 CI 基础设施。

### 1.2 对旧设计与骨架的覆盖决定

- 覆盖 `sampling_audit.decision` 复用 `ReviewDecision` 的骨架设计：finding 与 clearance 的真假语义不同。F5 不写该 legacy 可空 decision/reviewer/reviewed_at 三列；新增 `sampling_review` 使用 `clearance_confirmed|missed_issue`。
- 覆盖“在 GET 队列时随机抽样”：GET 必须只读。F5 上线后新报告在报告成功事务内自动创建抽检计划；上线前已存在的 completed report 只能通过显式、带 Idempotency-Key 的 POST 补建，避免刷新页面改变样本或产生隐式副作用。
- 覆盖“每次用数据库 random() 取样”：选择必须冻结 seed 与算法版本，能够机械复算；数据库执行计划或物理行顺序不得影响样本。
- 扩展 F4 §10.2 的新报告成功事务：F5 上线后，在 report 首次 completed 提交前追加 plan/sample 与 `sampling.plan_create`；report fingerprint、snapshot 内容和 completed replay 的只读语义不变。历史 completed report 重放仍不得隐式补 plan。
- 覆盖“按 row 复核 finding”：同一行可有多个 finding，finding 结论必须逐 `report_item/finding` 提交，不能把一行多个判定折叠为一个标签。
- 覆盖“复核后更新原 finding/report”：历史判断与人工标签并列保存，任何人工结论都不回写机器产物。
- 覆盖 TechDesign 中模糊的“抽检结论与复核结论合并”：两类标签在读取模型中可联合展示，但存储、值域、分母和指标解释严格分离。

---

## 2. 术语与不变量

| 术语 | 定义 |
|---|---|
| finding review | 对一个 F4 report item 所指向 finding 的最终人工判断 |
| clearance sample | 从系统判为 passed 的行中按冻结计划选中的抽检行 |
| sampling config | 租户级、追加版本的抽样参数 |
| sampling plan | 一个 completed report 的首次成功、不可变抽样计划 |
| review queue | finding review 与 clearance sample 的联合只读投影，不是可任意编辑的任务表 |
| pending | 目标尚无最终人工结论 |
| completed | 已存在不可变人工结论 |

全局不变量：

- tenant 只从认证 session 注入；API 不接受调用方 tenant ID。
- finding review 必须同时绑定同租户的 report run、report item、finding 与 file version，禁止只凭裸 finding UUID 拼接。
- clearance sample 必须同时绑定同租户的 sampling plan、report run、file version、expense row 与 row result。
- 一个 file revision 最多一个 completed F4 report；一个 report 最多一个 sampling plan。
- `review.unique(finding_id)` 与 `sampling_audit.unique(file_version_id,row_no)` 保留并加强，绝不删除或放宽。
- `review`、`sampling_plan`、`sampling_audit`、`sampling_review` 与成功请求账本均为追加/不可变事实；数据库拒绝 UPDATE/DELETE。
- 服务端数据库时间是 `reviewed_at/sampled_at/created_at` 的唯一来源，客户端时间仅用于显示，不进入判定身份。
- note 是私有人工输入，可存业务库但不进入 audit payload、普通日志、trace、模型或向量库。

---

## 3. 用户与数据流程

### 3.1 配置抽样参数

1. configurator 打开复核台设置区，读取当前 sampling config。
2. 提交 `expected_current_version`、`rate_bps`、`min_sample_size`、`max_sample_size`、变更说明与 Idempotency-Key。
3. 服务在 tenant NOWAIT 锁内校验并创建下一不可变版本，保存 canonical fingerprint。
4. 同一事务追加 `sampling.config_create` 审计；历史 config 不可修改。同 key 同请求返回既有版本，同 key 异请求 409；锁内当前版本与 `expected_current_version` 不同则 409，不从陈旧页面静默追加版本。
5. 创建 sampling plan 时只读取当时最新版本并冻结其 ID、版本、参数与 fingerprint；后续配置不会改变历史计划。

系统不提供隐式业务默认值。上线/首批运行门禁必须先由 configurator 创建 version 1，缺配置时计划创建返回显式 `SAMPLING_CONFIG_REQUIRED`。

### 3.2 创建复核与抽检计划

F5 上线后的主路径：

1. report 生成在既有 Tenant → FileVersion NOWAIT 锁和同一未提交事务内完成 F4 snapshot 装配。
2. 在 report 标记 completed 与 `batch.report_generate` 提交前，F5 服务读取最新 sampling config，并从对应 revision 中取全部解析成功且 `row_result.verdict = passed` 的行。
3. 生成一次 CSPRNG seed，按 §5 的算法计算稳定分数与样本量。
4. 同一事务写入 sampling plan、全部 `sampling_audit` 选择记录与一次 `sampling.plan_create` 审计。
5. report/item/citation、plan/sample 与两项 success audit 一起 commit；任一步失败全部回滚，不能出现“新 completed report 没有 plan”或 partial sample。
6. 失败仍沿用 F4 独立、无 PII 的 `batch.report_failed` 路径；若根因属于 sampling，再额外记录稳定 sampling reason code，不写两条含义重复的 failed audit。

finding 队列始终由 report item 动态投影，不复制或改写 F4 快照。缺 sampling config 时新 report 生成在任何持久化写入前返回 409 `SAMPLING_CONFIG_REQUIRED`，因此首个 F5 批次不能绕过抽检。

上线前已经存在且没有 plan 的 completed report 是唯一兼容例外：auditor/configurator 可显式调用 plan POST 补建。该请求使用相同锁序与原子 plan/sample/audit 事务，但不修改或重放 F4 report。已存在 plan 时，同 key 同请求返回 200；新 key 返回 200 并追加 key→plan 请求账本，不产生第二组样本或第二次成功审计。同 key 异请求返回 409。

### 3.3 复核 finding

1. 审核员打开默认 pending 队列，按 F4 attention group 查看 finding。
2. 详情同屏展示原始行、F4 reasoning/evidence、rule/version、citation 状态与逐字引用。
3. citation unavailable 必须显式显示“制度依据未完成”，但不阻止人工根据现有证据提交结论。
4. 审核员选择 `confirmed` 或 `false_positive`；`false_positive` 必须填写 1–2000 字符 note，`confirmed` note 可选。
5. 服务在锁内重新验证目标仍 pending，写入唯一 review 与同事务 `review.submit` 审计。
6. UI 刷新队列并显示服务端 reviewer 与 reviewed_at；不得乐观伪造成功。

### 3.4 复核被放行样本

1. 审核员打开 clearance sample，看到原始行、冻结 ruleset/report fingerprint、passed 结论及该行可用的 cleared item 证据。
2. 纯 passed 行没有 finding/citation 时，界面明确显示“系统未产生关注项”，不得生成伪理由或伪条款。
3. 审核员选择 `clearance_confirmed` 或 `missed_issue`；`missed_issue` 必须填写 1–2000 字符 note，前者 note 可选。
4. 服务追加唯一 sampling_review 与同事务 `sampling.review_submit` 审计，不更新 sampling_audit。
5. `missed_issue` 显示需要人工升级处理，但 F5 不自动改写报告、创建 finding 或调整规则。

### 3.5 中断与并发

- sampling plan 的 plan/sample/success audit 单事务提交；崩溃后是全有或全无。
- review/sampling_review 与各自 success audit 单事务提交；丢失响应后同 key 重放返回既有结果。
- 两名审核员并发提交同一目标，只有一个插入成功；另一方稳定得到 409，不覆盖首个结论。
- 不同 tenant 可并行；同 tenant 的 mutation 通过 NOWAIT 锁与唯一约束 fail closed，不等待形成死锁。

---

## 4. 队列范围、状态与稳定排序

### 4.1 Finding review 范围

finding review 队列只包含 completed report 的 `report_item`，且：

- `attention_group = high_attention` 或 `manual_attention`；
- 不包含 `cleared` finding；cleared 行只可能通过随机抽检进入工作台；
- 一个 report item 对应一个 finding queue item；同一 row 的多个 finding 分别保留；
- parse error 没有 finding，不进入 decision 队列，但复核台显示该 report 的解析错误计数与跳转入口。

### 4.2 Clearance sample 范围

候选总体精确定义为：

```text
report.status = completed
AND row_result.file_version_id = report.file_version_id
AND row_result.verdict = 'passed'
AND expense_row(file_version_id,row_no) exists
AND expense_row.parse_error IS NULL
```

以下对象不得进入候选总体：flagged、manual_review、parse error、其他 revision、其他 tenant、未完成 validation/report、F6 correlation candidate。

### 4.3 Queue 状态

- finding item pending：不存在 `review(finding_id)`。
- finding item completed：存在唯一 review。
- sample item pending：不存在 `sampling_review(sampling_audit_id)`。
- sample item completed：存在唯一 sampling_review。
- sampling plan 未创建只允许出现在 F5 上线前的历史 completed report；finding 队列仍可只读查看，但 UI/API 必须显式标记 `sampling_status=legacy_not_initialized`，不能声称该批次复核闭环完成。

### 4.4 稳定排序

默认 pending 队列使用固定顺序：

1. finding `high_attention`
2. finding `manual_attention`
3. clearance sample
4. report `completed_at` ASC（先处理最早批次，避免饥饿）
5. `row_no` ASC
6. finding 使用 `rule_id`、`rule_version NULLS FIRST`、`finding_id`
7. sample 使用 `selection_rank`、`sampling_audit.id`

completed 历史按 `reviewed_at DESC` 后接上述稳定 tie-break。API 使用白名单 filter/sort 和 `{items,total,limit,offset}`；任何 mutation 后客户端必须失效查询并回到 offset 0，避免 pending 集合收缩造成跳项。

---

## 5. 随机抽检算法与可复算性

### 5.1 配置

sampling config 为租户级追加版本，字段至少包括：

- `version`：从 1 连续递增；unique `(tenant_id,version)`。
- `rate_bps`：1–10000，使用整数基点避免浮点漂移。
- `min_sample_size`：至少 1。
- `max_sample_size`：大于等于 min。
- `algorithm_version`：F5 固定 `sha256-rank-v1`。
- `config_fingerprint`：canonical JSON 的 SHA-256。
- `idempotency_key_hash/request_fingerprint`：配置 mutation 的幂等身份。
- `created_by/created_at/change_reason`。

`change_reason` 必填 1–500 字符，只保存在配置记录；审计只保存 reason 的 SHA-256，不保存原文。

### 5.2 样本量

设 eligible count 为 `N`：

```text
N = 0  => sample_size = 0
N > 0  => sample_size = min(
  N,
  max_sample_size,
  max(min_sample_size, ceil(N * rate_bps / 10000))
)
```

全程使用整数运算：`ceil(N * rate_bps / 10000) = (N * rate_bps + 9999) // 10000`。不得使用 binary float。

### 5.3 选择算法

plan 创建时由操作系统 CSPRNG 生成 32-byte seed，并保存 lowercase 64-char hex。对每个 eligible row 计算：

```text
score = SHA256(
  UTF8("expenseguard:f5:sampling:sha256-rank-v1\0")
  || seed_bytes
  || tenant_uuid.bytes
  || report_run_uuid.bytes
  || row_no.to_bytes(8, "big", signed=False)
)
```

按 `(score bytes ASC,row_no ASC)` 取前 `sample_size` 个。每个选择记录保存 `selection_rank`（从 1 连续）、`selection_score_sha256` 与 plan ID。输入 UUID 使用 RFC 4122 的 16-byte network order；禁止字符串大小写、数据库 locale、Python hash seed 或 SQL `random()` 参与结果。

seed 不是 secret，可由 `REVIEW_READ` 用户读取以复算；它不得进入普通日志。算法 version、候选计数、样本量、config fingerprint、eligible row_no 有序集合共同决定结果。复算不读取当前配置或当前规则。

### 5.4 一致性验证

plan 提交前必须机械验证：

- eligible count 与冻结查询一致；
- sample count 等于公式结果；
- rank 为 `1..sample_size` 连续且唯一；
- 所有样本属于 eligible 集合且无重复；
- 保存 score 与相同输入重算一致；
- 同 plan 重放不访问 CSPRNG、不重新选择、不新增 success audit。

---

## 6. 结论语义与指标边界

### 6.1 Finding decision

| 值 | 精确定义 |
|---|---|
| `confirmed` | 人工确认机器提出的该 finding 成立 |
| `false_positive` | 人工确认机器提出的该 finding 不成立 |

`false_positive` 的分母只能是已完成复核的 finding，不能与未复核 finding 或 clearance sample 混算。

### 6.2 Sampling decision

| 值 | 精确定义 |
|---|---|
| `clearance_confirmed` | 人工检查后未发现应被系统拦截的问题，原 passed 判定得到确认 |
| `missed_issue` | 人工发现至少一个应被关注但系统未提出的问题，构成漏放样本 |

禁止把 `missed_issue` 存为 `false_positive`：前者是系统对负类的漏检，后者是系统对正类的误报，统计方向相反。

### 6.3 F5 可报告的原始量

F5 summary 可以给出：

- finding pending/completed/confirmed/false_positive count；
- sample eligible/selected/pending/completed/clearance_confirmed/missed_issue count；
- finding review coverage 与 sample review coverage；
- 当前 batch 是否具备 completed sampling plan。

F5 不直接宣称“召回率 ≥95%”。漏放率估计、有限总体修正、置信区间与代价敏感阈值重标定必须由后续评测模块基于冻结 plan/config/decision 计算，且必须披露样本量与抽样设计。

---

## 7. 持久化计划（CP-F5.1 输入）

### 7.1 迁移策略

- 只新增 `0007_f5_human_review.py`，禁止改写 `0001`–`0006`。
- 迁移前对默认开发库做可恢复的私有 full/schema/affected-data 备份并校验 `pg_restore --list` 与 SHA-256；测试库不承载真实数据。
- 由于 F5 尚未上线，`review` 与 `sampling_audit` 预期为空。升级必须在任何 DDL 前 preflight；若任一已有行，fail closed，要求人工提供 provenance 映射方案，禁止猜测回填 report/config/seed。
- downgrade 若存在任何 F5 config/plan/review/sample/request 数据，必须在 DDL 前拒绝；生产降级只允许从已验证 pre-0007 备份恢复到隔离库。
- 迁移后机械反向验证 `row_result`、`sampling_audit(file_version_id,row_no)`、`audit_log`、F3/F4 unique/FK/check/immutable triggers 未弱化。

### 7.2 新增实体

| 表 | 语义 |
|---|---|
| `review_sampling_config` | 租户级不可变抽样配置版本 |
| `review_sampling_plan` | completed report 的不可变抽样计划 |
| `sampling_review` | 一个 sampling_audit 的一次性人工结论 |
| `review_plan_request` | plan 创建的追加式幂等 key 账本 |

`review` 与 `sampling_audit` 在新增迁移中增强，不创建平行替代表。

### 7.3 Review 增强

`review` 保留 unique `finding_id`，并新增：

- `report_run_id/report_item_id/file_version_id`；
- `idempotency_key_hash/request_fingerprint`；
- unique `report_item_id`、unique `(tenant_id,idempotency_key_hash)`；
- report item ↔ finding ↔ file ↔ tenant 的完整复合 FK；
- decision/note 长度、hash 长度与 identity consistency CHECK；
- INSERT 后 UPDATE/DELETE 全拒绝的数据库触发器。

旧单列 CASCADE finding FK 与单列 reviewer FK 替换为同租户复合 `RESTRICT` FK。标签不能因删除 finding/user/file 而消失。

### 7.4 Sampling plan 与选择记录

`review_sampling_plan` 至少保存：

- tenant、report_run、file_version、sampling_config IDs；
- config version/fingerprint 与参数快照；
- algorithm version、seed hex；
- eligible/sample counts；
- created_by/created_at；
- unique `report_run_id`；plan 本身不保存 Idempotency-Key，所有 plan key 统一由 `review_plan_request` 账本承载；
- 完整复合 tenant/file/report/config FK、count/hash/参数一致性 CHECK；
- UPDATE/DELETE 拒绝触发器。

`sampling_audit` 新增 `sampling_plan_id/report_run_id/selection_rank/selection_score_sha256`，保留 unique `(file_version_id,row_no)`，并新增 unique `(sampling_plan_id,selection_rank)`、unique `(sampling_plan_id,row_no)` 与完整复合 FK。legacy decision/reviewer/reviewed_at 列保持 NULL、服务层禁止写入；CHECK 固定三列全 NULL，表级 UPDATE/DELETE 全拒绝。

### 7.5 Sampling review

`sampling_review` 至少保存：

- sampling_audit/plan/report/file IDs；
- `decision = clearance_confirmed|missed_issue`；
- reviewer/reviewed_at/note；
- idempotency key hash/request fingerprint；
- unique `sampling_audit_id`、unique `(tenant_id,idempotency_key_hash)`；
- 完整复合 tenant/plan/report/file/reviewer FK 与 decision/note/hash CHECK；
- INSERT 后 UPDATE/DELETE 全拒绝触发器。

### 7.6 Config 与不可变性

`review_sampling_config` unique `(tenant_id,version)`、unique `(tenant_id,idempotency_key_hash)`；允许未来显式回退到与历史版本相同的参数，因此 config fingerprint 不做唯一约束。version 必须在 tenant NOWAIT 锁内连续创建，request fingerprint 绑定 expected version、全部参数与 change reason SHA-256。config/plan/sample/review/request 全部 `ON DELETE RESTRICT`；所有业务记录的不可变性由数据库触发器保证，不只依赖 ORM 纪律。

`review_plan_request` 保存 tenant/report/plan IDs、`idempotency_key_hash`、`request_fingerprint` 与 created_at；unique `(tenant_id,idempotency_key_hash)`，通过完整复合 FK 指向 report/plan，且 UPDATE/DELETE 全拒绝。自动随新 report 创建的 plan 使用服务内部 canonical request identity，不伪造客户端 key，也不写 request ledger；ledger 只记录显式历史补建/复用请求。

---

## 8. 服务、事务、幂等与恢复

### 8.1 分层

- 纯模型/算法：`backend/app/core/reviews/models.py`、`sampling.py`。
- 数据服务：`config_service.py`、`plan_service.py`、`query_service.py`、`decision_service.py`。
- FastAPI 路由只校验 request/response、注入 auth/db 并调用服务，不直接查库。
- 前端只消费 OpenAPI 生成类型；表单边界使用 Zod，不引入 `any`。

### 8.2 锁序

所有 F5 mutation 复用现有顺序：

```text
Tenant FOR UPDATE NOWAIT
  → FileVersion FOR UPDATE NOWAIT
  → 具体 report/item/sample（需要时，稳定 UUID 顺序）
```

sampling config 只锁 Tenant。禁止反序获取。锁冲突稳定返回 409 `REVIEW_CONFLICT`，不阻塞到请求超时。

### 8.3 Decision 原子事务

1. 锁内读取 completed report 与不可变 target snapshot，校验 actor tenant、permission、target pending。
2. canonical request fingerprint 绑定 tenant、target kind/ID、decision 与 note SHA-256；原 note 不进入 fingerprint payload 之外的日志。
3. 写唯一 decision 与 `review.submit` 或 `sampling.review_submit` audit。
4. commit 后返回服务端 reviewer/reviewed_at。
5. 任一步失败回滚 decision 与 success audit；业务冲突返回稳定 409，未分类系统失败用独立短事务写无 PII failed audit。

### 8.4 Idempotency-Key

所有 config/plan/decision mutation 要求 8–128 字符 key，只存 SHA-256：

- 同 key + 同 canonical 请求：200 返回原结果，不新增 decision/audit。
- 同 key + 不同请求：409 `IDEMPOTENCY_KEY_REUSED`。
- target 已完成 + 新 key：409 `REVIEW_ALREADY_COMPLETED` 或 `SAMPLE_ALREADY_REVIEWED`。
- 新 plan key + 已有 completed plan：200 复用，通过 `review_plan_request` 追加 key→plan 映射；不重新抽样或新增成功审计。

并发唯一冲突必须在同事务 savepoint/领域异常中归一化，不能向 API 泄漏数据库错误或目标内容。

### 8.5 失败与恢复

- plan 崩溃：全事务回滚；重试重新生成 seed 是允许的，因为未有任何 committed plan。首次 committed seed 即永久冻结。
- decision 崩溃：全事务回滚或完整提交；同 key 重放辨认已提交结果。
- completed plan/read/decision replay 不访问 Qdrant、embedding、rerank、云 LLM、当前 rules/policy/config 或当前时间来重算身份。
- 不以删除 partial row、更新历史 decision 或跳过失败测试作为恢复手段。

---

## 9. API 契约

所有错误统一 `{error:{code,message}}`；跨租户资源返回 404。

### 9.1 Sampling config 与 plan

| Method | Path | Permission | 语义 |
|---|---|---|---|
| GET | `/api/review/sampling-config` | `REVIEW_READ` | 当前与历史配置版本 |
| PUT | `/api/review/sampling-config` | `CONFIG_WRITE` | 以 expected version 创建下一不可变版本；要求 Idempotency-Key |
| POST | `/api/reports/{report_id}/review-plan` | `REVIEW_SUBMIT` | 仅为 legacy completed report 创建/复用抽检计划；要求 Idempotency-Key |
| GET | `/api/reports/{report_id}/review-plan` | `REVIEW_READ` | 读取 plan/config/seed/count/status |

### 9.2 Queue 与 detail

| Method | Path | Permission | 语义 |
|---|---|---|---|
| GET | `/api/reviews/queue` | `REVIEW_READ` | 联合队列摘要；按 kind/status/report/file 过滤 |
| GET | `/api/reviews/findings/{report_item_id}` | `REVIEW_READ` | finding 原始行、冻结理由/evidence/citations 与现有结论 |
| GET | `/api/reviews/samples/{sampling_audit_id}` | `REVIEW_READ` | sample 原始行、passed/cleared 证据、plan 与现有结论 |
| POST | `/api/reviews/findings/{report_item_id}/decision` | `REVIEW_SUBMIT` | 提交 confirmed/false_positive；要求 Idempotency-Key |
| POST | `/api/reviews/samples/{sampling_audit_id}/decision` | `REVIEW_SUBMIT` | 提交 clearance_confirmed/missed_issue；要求 Idempotency-Key |
| GET | `/api/reviews/summary` | `REVIEW_READ` | 只返回 §6.3 原始计数与覆盖率 |

queue 默认 `status=pending`，`limit` 为 1–200，offset 非负；filter/sort 使用枚举，不接受任意列名。detail 返回 raw/normalized row 仅给同租户 REVIEW_READ 用户，响应标记为 private/no-store。

### 9.3 状态码与稳定错误

| HTTP | code 示例 |
|---|---|
| 401 | `AUTH_REQUIRED` |
| 403 | `PERMISSION_DENIED` |
| 404 | `REPORT_NOT_FOUND`, `REVIEW_TARGET_NOT_FOUND`, `SAMPLE_NOT_FOUND` |
| 409 | `SAMPLING_CONFIG_REQUIRED`, `SAMPLING_CONFIG_VERSION_CONFLICT`, `REVIEW_PLAN_IN_PROGRESS`, `REVIEW_CONFLICT`, `REVIEW_ALREADY_COMPLETED`, `SAMPLE_ALREADY_REVIEWED`, `IDEMPOTENCY_KEY_REUSED` |
| 422 | `SAMPLING_CONFIG_INVALID`, `REVIEW_DECISION_INVALID`, `REVIEW_NOTE_REQUIRED`, `REQUEST_VALIDATION_ERROR` |
| 500 | `REVIEW_PLAN_FAILED`, `REVIEW_SUBMIT_FAILED` |

### 9.4 OpenAPI 与缓存

- Pydantic schema 是前后端唯一 API 事实来源；finding/sample 使用判别联合，禁止裸 JSON decision DTO。
- 修改模型后执行后端 OpenAPI 导出与前端 client 生成，连续二次运行无 diff。
- mutation 成功后前端失效 queue/detail/summary/plan/config 查询；权限变化继续由 `/api/auth/me` permissions 驱动。
- raw row、note、review detail 响应使用 `Cache-Control: private, no-store`；不得进入持久化浏览器缓存。

---

## 10. 权限、审计与安全

### 10.1 RBAC

- auditor：`REVIEW_READ` + `REVIEW_SUBMIT`，可建 plan、查看并提交两类复核。
- configurator：auditor 全部能力 + `CONFIG_WRITE` 创建 sampling config。
- viewer：没有 review 权限；只保留 F4 report read/export，不能看复核 note/raw detail、不能提交真实标签。

沿用现有 permission 数据，不新增按角色 if-else。前端导航/按钮只按 permission 控制；后端每个 endpoint 独立鉴权。

### 10.2 审计白名单

至少包括：

- `sampling.config_create`
- `sampling.plan_create`
- `sampling.plan_failed`
- `review.submit`
- `review.submit_failed`
- `sampling.review_submit`
- `sampling.review_submit_failed`

payload 只含 tenant 内对象 ID、decision enum、版本、hash/fingerprint、计数、稳定 reason code。禁止 raw/normalized row、note 原文、reasoning/evidence/quote、员工/商户/发票信息、异常全文、seed、Idempotency-Key 明文、storage path 或 secret。

### 10.3 输入与 PII

- note 在 Pydantic/Zod 边界做长度与控制字符校验；保留正常 Unicode，不做会改变证据含义的归一化。
- React 只以文本节点展示 raw row、note、reasoning 与 quote，不使用未净化 HTML。
- F5 不调用任何 LLM、Qdrant、embedding 或 rerank；真实 PII 不离开 PostgreSQL/API/浏览器的授权内网路径。
- API/log/trace 错误不能回显 row、note、quote 或原始数据库异常。
- 前端不得把复核详情写入 localStorage/sessionStorage、URL query 或 analytics。

---

## 11. 桌面工作流

### 11.1 复核台布局

仅桌面浏览器，目标视口 1440×1000：

- 顶部：pending finding、pending sample、completed、sampling plan 状态与覆盖计数。
- 左侧：稳定排序队列、类型/状态/批次筛选、分页。
- 主区：原始行、机器判定理由/evidence、rule/version、citation 状态与逐字引用同屏。
- 右侧或底部固定操作区：互斥 decision、note、确认提交；提交后显示 reviewer 与服务端时间。

同屏要求指无需离开当前复核详情路由即可看到三类证据，不要求把所有长文本压进首屏。长 raw JSON/evidence/quote 可在同页滚动/展开，但关键 citation unavailable 与 decision 状态始终可见。

### 11.2 状态

必须覆盖：

- config missing / plan not initialized / plan creating / plan ready / plan error；
- queue normal / empty / loading / error；
- finding pending/completed/conflict；
- sample pending/completed/missed_issue；
- citation verified/unavailable；
- pure passed sample 无 finding/citation；
- permission read-only/hidden；
- mutation submitting/success/error。

提交 final decision 前显示二次确认，明确“提交后不可修改”。前端不提供编辑、撤销或覆盖按钮。

### 11.3 视觉与安全门禁

- 精确 1440×1000 Chrome 覆盖长 ID、长 note、长 quote、5000 行批次计数与两类队列项。
- document 与关键容器无页面级横向溢出；长 token/UUID 可复制且不会遮挡操作区。
- `<script>`、`<img onerror>`、`javascript:`、公式前缀和伪系统指令只显示为文本，不形成 DOM/导航/执行。
- auditor/configurator/viewer 的导航、配置按钮、提交按钮与直接路由访问均符合权限矩阵。

---

## 12. 验收场景

### 12.1 Config 与 sampling core

- rate/min/max 边界、整数 ceil、小总体、N=0、min>N、max<N。
- canonical config 顺序稳定，任一有效字段变化改变 fingerprint。
- 固定 seed/UUID/row 输入产生固定 score/rank；UUID bytes、big-endian row 编码 golden vectors。
- 候选顺序、SQL 物理顺序、Python hash seed 改变不影响样本。
- flagged/manual/parse-error/跨 revision/跨 tenant 行均不能入样。
- config 变更不改变历史 plan；plan seed/score/rank 可完整复算。

### 12.2 Plan 幂等、并发与恢复

- 无 config、report 未完成、跨租户、锁冲突显式失败。
- 新 report 自动 plan 与 report 同事务全有或全无；legacy 补建的同 key 同请求、同 report 新 key、同 key 异请求、并发双建语义符合 §8.4。
- 在 plan、部分 sample、success audit 各故障点 kill，重启后全有或全无且最多一个 plan/样本集合/成功审计。
- completed plan 重放不调用 CSPRNG/Qdrant/模型/当前 config，不改变 seed/score/rank。

### 12.3 Finding review

- 同一行多 finding 分别复核，不折叠。
- confirmed/false_positive、必填 note、长度/控制字符边界。
- 同 key重放、新 key重复、同 key异请求、两审核员并发只产生一个 final label 与一次 success audit。
- citation unavailable 可提交但保持显式；人工结论不改变 report/item/citation/finding。
- DB 直接 UPDATE/DELETE review 被触发器拒绝。

### 12.4 Clearance review

- clearance_confirmed/missed_issue、必填 note、纯 passed 无 finding 的 detail。
- sampling_audit 永不 UPDATE；结论只追加 sampling_review。
- 同 key/新 key/并发/kill 恢复与 finding review 同级门禁。
- missed_issue 不自动创建 finding、不改 row_result/report，只影响复核 summary 与后续评测输入。
- DB 直接 UPDATE/DELETE plan/sampling_audit/sampling_review 被拒绝。

### 12.5 API、RBAC 与租户

- 三角色 × config/plan/queue/detail/decision/summary 权限矩阵。
- 未登录 401、无权限 403、跨租户 404；viewer 不能通过直接 URL 读取复核详情。
- 分页/filter/sort/default order 稳定；mutation 后 pending 队列不跳项。
- private/no-store、统一 error shape、OpenAPI/client 连续二次无漂移。
- 审计 payload 机械扫描不含 raw row、note、reasoning、quote、PII、seed 或 key 明文。

### 12.6 UI、性能与交付

- 1440×1000 覆盖 §11.2 全状态、三角色与恶意文本，无页面级横向溢出。
- 5000 行 completed report 的 plan 创建 + 队列首屏/detail/decision：交互读取与提交 p95 < 2 秒；记录 SQL count/time、plan 算法耗时与 response size。
- 固定 seed 的 5000 行 F1→F2→F3→F4→F5 auto-plan 总耗时仍须 ≤900 秒；同时单独记录 F5 plan 时间，不能只复用旧 F1→F4 数字来声称达标。
- 后端 pytest/Ruff/format/mypy、双库 Alembic、迁移恢复/受保护约束、前端 test/typecheck/oxlint/Prettier/build、OpenAPI、pre-commit/gitleaks、依赖审计全部通过。

---

## 13. 实施检查点

本文件是 CP-F5.0–F5.5 的唯一规范来源。实现发现冲突时必须先修改本文件并在 §14 记录覆盖决定，再继续编码。

### 13.1 CP-F5.0 · 规格固化

**目标：** 固定两类标签、队列、随机抽检、不可变存储、事务幂等、API/UI 与 F6/F8 边界。

**交付物：** 本规格 §1–§13 与 CP-F5.1–F5.5 契约。

**非目标：** 不创建迁移、服务、路由、UI 或依赖，不预填未来测试数量。

**退出条件：** 无 P0/P1 未决项；sampling 语义与选择算法可机械复算；旧 skeleton 覆盖决定明确；`git diff --check` 与文档审查通过。

### 13.2 CP-F5.1 · 持久化 schema 与 ORM

**目标：** 用新增 `0007_f5_human_review.py` 落地 §7。

**具体交付物：** sampling config/plan/review/request、review/sampling_audit 强化、同租户复合 FK、RESTRICT、CHECK、唯一约束与不可变触发器。

**非目标：** 不实现 sampling 算法、服务、API 或 UI；不改 `0001`–`0006`。

**测试：** 空库/legacy 升级、preflight fail closed、往返/安全 downgrade、租户错配、不可变触发器、受保护约束反向测试、双库 Alembic check。

**退出条件：** 私有备份与校验完成，迁移/ORM/Ruff/mypy 通过，CP-F5.2 无需再决定字段或约束。

### 13.3 CP-F5.2 · 抽样核心、计划与复核服务

**目标：** 实现 §3–§8 的纯算法与服务事务。

**具体交付物：** config canonical/fingerprint、golden sampling algorithm、plan 原子创建、联合 query、两类一次性 decision、幂等/失败审计。

**非目标：** 不暴露 API/UI，不计算召回率/置信区间，不实现 F6/F8。

**测试：** §12.1–§12.4；幂等、并发、kill/restart、租户与不可变性为最高优先级。

**退出条件：** 固定 seed 可复算，所有业务 side effect 最多一次，F4 快照零改写，completed replay 零外部模型/检索调用。

### 13.4 CP-F5.3 · API 与 OpenAPI 契约

**目标：** 实现 §9–§10 的强类型、权限驱动 API。

**具体交付物：** config/plan/queue/detail/decision/summary 路由、Pydantic 判别联合、稳定错误、private/no-store、OpenAPI/client。

**非目标：** 不实现 UI、批量提交、分派或评测导出。

**测试：** §12.5 后端部分；三角色、跨租户、幂等 key、分页与错误 shape。

**退出条件：** 路由无业务逻辑/直接 SQL；OpenAPI/client 连续二次无 diff；API 集成测试通过。

### 13.5 CP-F5.4 · 桌面复核台

**目标：** 将 `/review` 占位页替换为 §11 的桌面工作流。

**具体交付物：** sampling config/plan 状态、联合队列、同屏 detail、两类 decision 表单、查询失效与权限 UI。

**非目标：** 不做 mobile/tablet、批量复核、任务认领、撤销更正或指标仪表盘。

**测试：** 组件/集成测试、恶意文本、安全缓存、三角色、normal/empty/loading/error/conflict；1440×1000 真实 Chrome。

**退出条件：** 原始行/理由/引用同屏，提交不可修改提示明确，无页面级横向溢出，前端所有静态/测试/build 门禁通过。

### 13.6 CP-F5.5 · 契约与交付门禁

**目标：** F5 全量回归、迁移/受保护约束、契约、安全、5000 行性能与桌面交付门禁。

**具体交付物：** 后端全量、前端全量、双库 Alembic、OpenAPI 二次、pre-commit/gitleaks、依赖审计、固定 seed 5000 行 sampling/review、1440×1000 视觉证据。

**非目标：** 不降低阈值、不跳过失败、不修改受保护基础设施、不提前进入 F6/F7/F8。

**退出条件：** 所有命令零退出；两类标签、抽样可复算、追加写、幂等/中断恢复、RBAC/PII 与 p95 <2 秒通过；状态文件写入真实数量后才推进 F5 完成。

---

## 14. 实际落地记录

### CP-F5.0 实际落地记录（2026-07-29）

- 新增本规格，固定 finding review 与 clearance sampling 两类不可混用的标签语义；finding 使用 `confirmed|false_positive`，被放行抽检使用 `clearance_confirmed|missed_issue`。
- 抽检采用版本化数据配置、一次性 CSPRNG seed 与 `sha256-rank-v1` 稳定排序；新 report 在成功事务内自动创建 plan，只有 legacy completed report 使用显式 plan mutation。候选总体、样本量整数公式、UUID/row byte encoding 与复算门禁均已冻结，GET 不产生抽样副作用。
- 现有 `sampling_audit` 被固定为不可变选择事实；legacy decision 列不再作为 F5 写路径，抽检结论追加到独立 `sampling_review`。`review` 同样是一条 finding 一次性最终标签；更正/撤销留待未来显式 supersession 设计。
- F5 只消费 completed F4 snapshot，队列沿用 `high_attention > manual_attention > clearance_sample`，不读取 F6 correlation finding、不计算 F8 severity，不改写 finding/report/citation/row_result。
- 固定 CP-F5.1–F5.5 的迁移、服务、API、桌面 UI 与交付门禁；本检查点未创建迁移、代码、路由、UI、依赖或测试数量。

### CP-F5.1 实际落地记录（2026-07-29）

- 只新增 `0007_f5_human_review.py`，未改写 `0001`–`0006`。迁移在任何 DDL 前检查 legacy `review`/`sampling_audit` 必须为空；否则 fail closed，不猜测 report/config/seed provenance。downgrade 在任何 DDL 前检查全部 F5 事实，存在任何数据即拒绝。
- 落地 `review_sampling_config`/`review_sampling_plan`/`sampling_review`/`review_plan_request`，强化 `review` 与 `sampling_audit`。finding review 通过 report item/finding/report/file/tenant 复合身份闭合；clearance sample/review 通过 plan/report/file/expense row/row result/tenant 复合身份闭合；config 快照参数直接纳入 plan→config 复合 FK，防止持久化快照与版本漂移。
- 原 `row_result(file_version_id,row_no)`、`sampling_audit(file_version_id,row_no)` 与 F3/F4 唯一/FK/CHECK 均保留；仅为完整租户 FK 追加 `report_item`/`expense_row`/`row_result` 冗余唯一键。六类 F5 表的 UPDATE/DELETE 由数据库触发器统一拒绝，全部 F5 FK 为 `ON DELETE RESTRICT`，legacy sampling decision/reviewer/reviewed_at 由 CHECK 强制全 NULL。
- 默认库 pre-0007 full/schema/affected-data custom archive 位于 gitignored `data/private/backups/cp-f5.1/pre-0007-20260729-160753/`，均通过 `pg_restore --list` 与容器/本地 SHA-256 交叉校验；哈希依次为 `6c9baa69e6abe88ced0810f9cd510540c821bd160d94094765395a363cea3cd4`、`53b1a21488b6f609d9ce8d085f15229b579cade332f18f89ef8f75ea0baf80e0`、`188ea6f89bbcf9a5e15747c4e53ee828155f30ec7d06713ac2426798ea71c9cd`。
- 迁移/受保护约束定向 `36 passed`，后端全量 `355 passed, 1 skipped`；Ruff lint/format、strict mypy（106 源文件）、默认/测试双库 `0007 (head)` 与 Alembic 零漂移通过。本检查点未实现 sampling 算法、服务、API、UI 或 F6/F8。

### CP-F5.2 实际落地记录（2026-07-29）

- 新增 `backend/app/core/reviews/` 领域层，落地严格 Pydantic 内部模型、sampling config canonical/fingerprint、整数样本量公式、32-byte CSPRNG seed 与 `sha256-rank-v1` golden score/rank；UUID 使用 RFC 4122 network-order bytes，row number 使用 unsigned big-endian 8-byte 编码，候选输入顺序与 Python hash seed 不影响结果。
- config 服务在 Tenant NOWAIT 锁内执行连续版本与 expected-version CAS，同 key 同请求只读复用，同 key 异请求/陈旧版本显式冲突；config 与 decision request fingerprint 只保存 note/reason hash，不把原文写入审计。联合 queue/detail/summary 只读 completed F4 snapshot、原始行与已冻结 plan，不读取 F6/F8，不调用 Qdrant、embedding、rerank 或 LLM。
- 新 report 在既有 Tenant→FileVersion NOWAIT 与 F4 单事务内，于 report completed 前写入 plan、全部 `sampling_audit` 与 `sampling.plan_create`；缺 config 在任何 report 写入前返回 `SAMPLING_CONFIG_REQUIRED`。plan/sample/audit 任一故障使 report 全部回滚，只以独立事务写一条带稳定 `sampling_reason_code` 的 `batch.report_failed`。legacy completed report 通过显式 key ledger 补建/复用，新 key 不重抽样、不新增成功审计；legacy 系统故障独立写 `sampling.plan_failed`。
- finding review 仅接受 high/manual report item，clearance review 再校验 sample 绑定的 row_result 仍为 passed；两类结论均在 Tenant→FileVersion→target 锁序内一次性追加并与成功审计同事务，key 重放、新 key 重复、key 异请求、NOWAIT 并发与 kill/restart 均 fail closed。`sampling_audit` legacy decision 三列保持 NULL；任何人工结论均不改写 finding/report/item/citation/row_result，也不因 `missed_issue` 创建 finding。
- CP-F5.2/F4 定向 `39 passed`；后端全量 `386 passed, 1 skipped`；Ruff lint/format（155 文件）与 strict mypy（114 源文件）通过。未新增依赖、迁移、API/UI 或 OpenAPI/client 变更；下一检查点为 CP-F5.3。

### CP-F5.3 实际落地记录（2026-07-29）

- 新增 config/plan/queue/finding detail/sample detail/两类 decision/summary 共 10 个强类型 API，全部只做传输校验、auth/db 注入与现有 F5 服务调用，路由无直接 SQL。GET config 返回明确的 current/history；plan 用 `completed|legacy_not_initialized` 判别联合；queue item 与 decision request 按 `kind` 生成 OpenAPI `oneOf + discriminator.mapping`，不使用裸 JSON DTO。
- queue 支持 status/kind/report/file 白名单筛选、固定 default sort 与 1–200/非负 offset 分页；legacy finding item 明示 `sampling_status=legacy_not_initialized`。summary 补齐 finding/sample `{completed,total}` coverage；sample detail 的 ruleset fingerprint 修正为 completed report 冻结值，不读取当前规则或误用单行字段。
- 三角色权限完全沿用 permission 数据：auditor/configurator 可读并提交 review、仅 configurator 可创建 sampling config、viewer 无 review 入口；未认证 401、无权限 403、跨租户 report/finding/sample 404。四类 mutation 的 Idempotency-Key 从 8–128 字符 header 注入，首次 201、同 key 同请求 200，冲突保持稳定领域 code。
- F5 响应统一标记 `Cache-Control: private, no-store`，CORS 显式允许 `Idempotency-Key`；配置、decision/note、分页与 Pydantic 边界错误统一为 `{error:{code,message}}`。API 集成测试覆盖 RBAC、租户隔离、幂等重放/重复、legacy plan、分页/过滤、判别联合、缓存、CORS 与 success audit 无 note/key/seed 明文。
- CP-F5.3/F5 定向 `36 passed`；后端全量 `391 passed, 1 skipped`；Ruff lint、157 文件 format、strict mypy（115 源文件）与 OpenAPI `--check` 通过。OpenAPI/client 连续二次生成 SHA-256 均稳定；前端 `23 passed`、typecheck/oxlint/Prettier/生产 build 全绿。未新增依赖、迁移、UI、批量提交、分派、评测导出或 F6/F8；下一检查点为 CP-F5.4。

### CP-F5.4 实际落地记录（2026-07-29）

- `/review` 占位页替换为仅桌面的工业审计工作台：顶部展示 finding/sample 精确 coverage、当前 config 与 plan 状态；左侧按后端固定顺序消费 finding/clearance 判别联合队列，支持状态/类型/当前批次筛选与稳定分页；中部同屏展示原始行、规范化投影、冻结 reasoning/evidence、rule/version、citation 状态与逐字引用；右侧提供 config/legacy plan 控制；底部为两类互斥 decision、note 与“提交后不可修改”二次确认。
- 前端只消费 CP-F5.3 生成契约并新增 Zod 运行时表单边界，不复制裸 DTO。sampling config 校验 version/rate/min/max/reason，finding/sample 分别校验 `confirmed|false_positive` 与 `clearance_confirmed|missed_issue`，只对 false positive/missed issue 条件要求 note；正常 Unicode 原样保留，控制字符与长度越界在提交前拒绝。
- mutation 使用 8–128 字符 Idempotency-Key；成功后失效 config/plan/queue/detail/summary 并回到 offset 0。409 并发/已完成冲突也触发刷新并展示服务端 reviewer/reviewed_at/最终标签，不乐观伪造成功。pure passed sample 明示“系统未产生关注项”，citation unavailable 明示“制度依据未完成”；missed issue 只提示人工升级，不创建 finding 或改写机器快照。
- 权限完全由 `/api/auth/me` permissions 驱动：auditor/configurator 可读写 review 与补建 legacy plan，仅 configurator 可追加 sampling config，viewer 菜单隐藏且直接访问不发 review API。raw/note/reasoning/quote 只以文本节点展示，不写 localStorage/sessionStorage、URL 或 analytics。
- 新增 22 项 F5 前端组件/集成/Zod 测试，覆盖 normal/empty/loading/error/conflict、config missing/stale refresh、legacy plan、两类提交、query invalidation/offset reset、三角色、恶意文本与 completed server fact；前端全量 `10 files / 45 passed`，typecheck、oxlint、Prettier、生产 build 与 npm audit 全绿。真实 Google Chrome 精确 1440×1000 覆盖 auditor finding、auditor pure-passed sample、configurator、viewer 共 4 场景；document/body 横向溢出、script/img 执行、浏览器持久化与 viewer review API 请求均为 0，证据保存在 gitignored `data/private/cp-f5.4/`。未修改后端、OpenAPI、迁移、基础设施、批量/分派/撤销、F6/F7/F8 或指标仪表盘；下一检查点为 CP-F5.5。

### CP-F5.5 实际落地记录（2026-07-29）

- 5000 行首次交付测量发现 `list_review_queue` 在 report 筛选下仍全量装载 finding/sample ORM、于 Python 构造全部判别联合并排序后切页，真实 API queue p95 为 `21.590788s`，未通过 `<2s` 硬门禁。修复为 finding/sample 同形 SQL `UNION ALL`，数据库按冻结 attention/report time/row/rule/version/finding/rank/target 顺序稳定排序并执行 offset/limit，total 使用独立 count；pending/completed、kind/report/file 筛选、legacy sampling status 与返回判别联合保持不变。新增 SQL 分页/跨类型顺序/kind total/completed 回归后，queue p95 降至 `0.051330s`。
- 固定合成 seed=3500 与固定 sampling seed 的 5000 行 F1→F2→F3→F4→F5 auto-plan 总耗时 `115.895928s`，低于 900 秒；1045 finding、3955 passed，plan 在 `0.777962s` 内从 3955 个 eligible 选择 396 行，执行 400 条 SQL（累计 `0.464250s`），持久化 selection 通过机械复算。报告含 plan 为 `9.147214s`；report 本体 3151 条 SQL（累计 `4.416736s`）。
- 真实 ASGI API 串行测量：queue 25 次 p95 `0.051330s`、3 SQL/request、响应 24123 bytes；finding/sample detail 各 25 次 p95 `0.347350s`/`0.227890s`，响应 4126/2463 bytes；finding/sample decision 各 20 次 p95 `0.061819s`/`0.049018s`。四类最终标签各提交 10 次，审计 payload 机械扫描不含 note、Idempotency-Key 或 seed 明文；所有交互 p95 均 <2 秒。私有 harness 与 JSON 证据位于 `data/private/cp-f5.5/`。
- 迁移/抽样/复核/恢复定向 `56 passed`，F5 服务/API 定向 `19 passed`；后端全量 `392 passed, 1 skipped`，Ruff lint、157 文件 format 与 strict mypy（115 源文件）通过。默认/测试双库均为 `0007 (head)` 且 Alembic 零漂移；OpenAPI/client 连续二次生成哈希稳定（OpenAPI `c1498154...a57dd`，client `9285d691...04726`）。前端全量 `10 files / 45 passed`，typecheck、oxlint、Prettier、生产 build 通过；pre-commit/gitleaks 与 `pip-audit`/`npm audit` 均零漏洞。
- 真实 Google Chrome 精确 1440×1000 覆盖正常 finding/pure-passed sample、auditor/configurator/viewer，以及 config missing、plan legacy/creating/ready/error、queue empty/loading/error、citation unavailable、finding completed、sample missed_issue、mutation confirmation/submitting/success/error/conflict。15 份 metrics 与 25 张截图的 document/body 横向溢出、恶意 script/img 执行、localStorage/sessionStorage 均为 0；viewer review API 请求为 0。证据保存在 gitignored `data/private/cp-f5.5/`。
- 未修改 `0001`–`0007`、Docker/CI 基础设施、受保护唯一约束或追加写触发器；未降低阈值、跳过失败或提前进入 F6/F7/F8。CP-F5.5 退出条件全部满足，阶段 2 F5 状态推进为完成。
