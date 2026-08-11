# Spec 009 — Phase 3 F8 二维分级

> 本文件是 CP-F8.0–CP-F8.5 的唯一规格来源。后续实现若与早期 PRD、TechDesign、
> skeleton 注释或聊天记录冲突，以本文件为准；不得在实现阶段临时补充未记录的
> 分级语义。

## 1. 目标、输入与边界

F8 将已经冻结的 F3 确定性判定与 F6/F7 跨行调查事实，转换为独立、可复算、
可审计的二维分级快照：

- `severity_impact`：若问题成立，后果有多严重；范围 `0..3`。
- `severity_confidence`：现有证据对问题成立的支持程度；范围 `0..3`。
- `disposition`：按版本化代价配置计算出的操作分组，固定为
  `high_attention | manual_attention | cleared`。

一次 grading run 必须显式绑定：

1. 一个 completed F3 `validation_run`；
2. 同一 tenant/file 的一个 completed F6 `detection_run`；
3. 覆盖该 detection run 全部 correlation candidate 的显式 F7 manifest；
4. 一个不可变 `grading_config` 版本。

F7 manifest 对每个 correlation finding 恰有一项：要么绑定一个已有终态
`investigation_run`，要么显式标记 `not_run`。服务不得读取“最新调查”、当前 F7
配置或当前 provider 状态来补全输入。

### 1.1 在范围内

- 追加版本的 grading config、原子 grading run/request/item/row snapshot。
- 纯确定性二维分级与代价敏感 disposition 算法。
- 强类型 service/API/OpenAPI、配置台与批次综合风险桌面视图。
- 幂等、并发、故障注入、hard-kill 恢复、5000 行及安全门禁。

### 1.2 明确不在范围内

- 不 UPDATE 或回填 `finding.severity_*`、`correlation_finding.severity_*`；F6 的
  `severity_impact = 0 AND severity_confidence = 0` 约束保持不变。
- 不重生成、修改或重新解释 F4 `report_run/report_item/XLSX`，不改变其
  first-success-per-file 语义。
- 不把 F8 item 自动并入 F5 queue，不修改既有 `review`、抽检或人工标签语义。
- 不训练模型、不自动从制度或复核数据生成并发布阈值、不调用任何 LLM、
  embedding、rerank 或 Qdrant。
- 不实现移动端、通知、批量后台队列、生产发布或 F8 专用导出。若未来需要复核
  correlation grading，须另建追加写 `grading_review`，不得伪造 F3 finding 或复用
  现有 F5 外键。

---

## 2. 等级、来源与规范输入

### 2.1 等级语义

| level | impact | confidence |
|---:|---|---|
| 0 | 无可量化影响或仅能力缺口 | 未评估/无有效证据 |
| 1 | 低 | 低 |
| 2 | 中 | 中 |
| 3 | 高 | 高 |

等级是有序枚举，不是概率或金额。UI 必须同时展示两个维度，禁止拼成一个
“综合 severity”数字。

### 2.2 F3 manifest

- manifest header 固定保存 `schema_version=1,tenant_id,file_version_id,file_content_hash,
  file_revision_no,validation_run_id,mapping_version_id,ruleset_fingerprint,
  ruleset_manifest_fingerprint`，以及
  所有 `outcome in {flagged, unavailable}` 的 finding ID、rule kind、outcome、
  evidence fingerprint 和稳定排序。
- 每个 finding item 固定保存 `finding_id,row_no,rule_id,rule_version,rule_kind,outcome,
  reason_code,evidence_fingerprint`；先用既有 `RuleEvidence` 判别联合验证 `evidence_json`，
  outcome 只从强类型 evidence 读取，不从 `RowResult.verdict`、`Finding.kind/reasoning` 或裸
  dict 猜测。evidence fingerprint 是强类型 evidence `model_dump(mode="json")` 的 canonical
  SHA-256。顺序固定为 row_no、rule kind rank、rule_id、rule_version、finding_id。
- `ruleset_manifest_fingerprint` 是冻结 `validation_run.ruleset_manifest` 的 canonical SHA-256。
- `passed/exempted` finding 不生成 grading item；它们仍属于 F3 历史事实，不删除。
- `flagged` 的 confidence base 固定为 3，不依赖 F7 或模型。
- `unavailable` 的 confidence base 固定为 0，必须转人工，不伪装为未命中。

### 2.3 F6 manifest

- manifest 固定保存 detection run/config/input fingerprint、四项 capability declaration
  及全部 correlation finding ID/detector/finding key/evidence fingerprint/参与行 identity。
- capability item 保存 `declaration_id,detector,detector_version,status,config_fingerprint,
  reason_code,details_fingerprint,finding_count`，四种 detector 必须恰好各一项；details 先经
  F6 强类型 capability DTO 验证再 canonical hash。
- candidate item 保存 `correlation_finding_id,detector,detector_version,finding_key,
  evidence_fingerprint,participating_rows,first_row_no`；evidence 先经 `CorrelationEvidence`
  判别联合验证再 canonical hash。参与行按 ordinal 严格连续，candidate 顺序固定为 detector
  rank、first row、finding key、finding ID；capability finding_count 与实际 candidate 数一致。
- 每个 correlation finding 恰生成一个 grading item。
- capability 状态参与 confidence 上限：`enabled=3`、`degraded=2`、
  `unavailable=0`。

### 2.4 F7 manifest

- 按 correlation finding ID 稳定排序，数量与 F6 candidate 数完全相等且不可重复。
- `run` 项保存 investigation run/result ID、input/config/result fingerprint、outcome、
  `evidence_sufficient` 和引用 manifest；run 必须属于同一 candidate/detection/file/tenant。
- citation item 固定为 `schema_version,clause_id,quote_start,quote_end,quote`，逐项与 F7 已
  机械验证的 result snapshot 比对，按 clause_id、quote_start、quote_end、quote 排序。
- `not_run` 项保存固定 reason code，不允许自由文本。
- 只接受已有唯一终态 result；in-progress、缺 result、跨 candidate 或 result identity 漂移
  均 fail closed。
- `sufficient/insufficient/unavailable/max_steps/failed` 保持 F7 原义；不得把后三者或
  `not_run` 转写为 `insufficient`。

三份 manifest 都以紧凑 canonical JSON（UTF-8、key 排序、稳定数组顺序、禁 float）
计算 SHA-256。未知字段、未知 enum、重复 identity、非 canonical 数值或 byte limit 超限
全部拒绝。

### 2.5 固定上限与 reason code

- F3 manifest 最多 5000 项；F6/F7 manifest 各最多 20000 项且数量必须相等。compact
  canonical UTF-8 上限分别为 F3 8 MiB、F6 16 MiB、F7 32 MiB；三份 manifest 与
  `input_fingerprint` 分别校验，不把超限输入截断后继续。数据库对 JSONB 文本施加同值
  上限作为第二道保护，但不得把 JSONB `::text` 描述为 compact canonical bytes。
- 单个 `evidence_snapshot` 必须是 JSON object，canonical UTF-8 上限 256 KiB；单个
  `reason_codes_json` 为 1..16 项、canonical UTF-8 上限 8 KiB。reason code 必须是以下
  全集的非空子集，按这里的固定顺序去重保存：
  1. `IMPACT_RULE_MAPPING`
  2. `IMPACT_DETECTOR_MAPPING`
  3. `CONFIDENCE_F3_FLAGGED`
  4. `CONFIDENCE_F3_UNAVAILABLE`
  5. `CONFIDENCE_F7_SUFFICIENT`
  6. `CONFIDENCE_F7_INSUFFICIENT`
  7. `CONFIDENCE_F7_UNAVAILABLE`
  8. `CONFIDENCE_F7_MAX_STEPS`
  9. `CONFIDENCE_F7_FAILED`
  10. `CONFIDENCE_F7_NOT_RUN`
  11. `CAPABILITY_ENABLED`
  12. `CAPABILITY_DEGRADED`
  13. `CAPABILITY_UNAVAILABLE`
  14. `CONFIDENCE_CAP_APPLIED`
  15. `COST_MATRIX_SELECTED`
  16. `OVERRIDE_IMPACT_3`
  17. `OVERRIDE_CONFIDENCE_0`
  18. `OVERRIDE_F3_UNAVAILABLE`
  19. `OVERRIDE_F7_NON_SUFFICIENT`
  20. `OVERRIDE_CAPABILITY_UNAVAILABLE`
  21. `FINAL_HIGH_ATTENTION`
  22. `FINAL_MANUAL_ATTENTION`
  23. `FINAL_CLEARED`
- `CONFIDENCE_CAP_APPLIED` 仅在 outcome mapping 高于 capability cap 时出现；具体 source
  kind、rule/detector/outcome、映射前后数值和初算 loss 放入冻结 `evidence_snapshot`，不把
  高基数事实编码进 reason code。所有 item 必须包含一种 impact 来源、一种 confidence
  来源、`COST_MATRIX_SELECTED` 和一种 final reason。安全覆盖只记录实际改变初算
  disposition 的 override reason，不记录未触发的规则。
- `grading_item_row.source_row_fingerprint` 使用域前缀
  `expenseguard-grading-source-row-v1\0`，payload 固定为 `schema_version=1,tenant_id,
  file_version_id,row_no,NormalizedExpenseRecord`；normalized record 必须先通过现有 Pydantic
  schema。不得直接 canonicalize 可能含 float/PII 表现差异的 `raw_json`，原始行只用于 UI
  展示并以该 fingerprint 对照其冻结 normalized projection。

---

## 3. CP-F8.1 — `0010` 持久化规范

只新增 `0010_f8_two_dimensional_grading.py`，不得修改 `0001`–`0009`。

### 3.1 迁移前置与回退

- DDL 前按既有流程备份默认库，产出 gitignored full/schema/affected-data archive，
  验证 SHA-256、`pg_restore --list` 与隔离恢复。
- upgrade 不回填 legacy severity，不读取当前配置生成业务事实。
- downgrade 在任一 F8 config/run/request/item/row 事实存在时于任何 DDL 前拒绝；
  无事实时才允许精确回到 `0009`。
- 所有新 FK 使用 `RESTRICT`；所有 F8 表拒绝 UPDATE/DELETE。

### 3.2 `grading_config`

至少包含：

- `id,tenant_id,version,definition_json,canonical_definition,config_fingerprint`
- `algorithm_version,created_by,change_reason,idempotency_key_hash,request_fingerprint,created_at`

约束：

- unique `(tenant_id,version)`、`(tenant_id,idempotency_key_hash)`、
  `(tenant_id,config_fingerprint)` 与供 run 复合 FK 使用的完整 snapshot identity。
- version 正整数；hash 为小写 64 位 SHA-256；canonical text 以 UTF-8 bytes 施加
  256 KiB 上限；JSONB 与 canonical text 必须语义相等。
- change reason 为 1..500 字符且无控制字符；actor 通过复合 FK 绑定 tenant。
- 配置创建使用 tenant NOWAIT 锁与 expected version CAS；相同 key 同请求 replay，
  同 key 异请求冲突。
- 新 Idempotency-Key 提交与既有 config 相同的 fingerprint 时稳定返回
  `GRADING_CONFIG_DUPLICATE`，不得创建重复版本或伪装 replay；本阶段不新增 config alias ledger。

### 3.3 `grading_run`

至少包含：

- 完整 `id,tenant_id,file_version_id,validation_run_id,detection_run_id,grading_config_id`
  与 actor identity。
- config version/fingerprint/algorithm snapshot。
- `f3_manifest_json/fingerprint`、`f6_manifest_json/fingerprint`、
  `f7_manifest_json/fingerprint`、总 `input_fingerprint`。
- `status, deterministic_item_count, correlation_item_count, high/manual/cleared_count,
  completed_at, created_at`。

约束：

- F3/F6 run、file 与 tenant 使用完整复合 FK 闭合；config 使用完整 snapshot FK。
- 只持久化 `in_progress | completed`；completed 必须有 completed_at 且所有计数一致。
- 同一 `(tenant,file,validation,detection,config,input_fingerprint)` 最多一个业务 run；
  不同配置或任一 manifest 改变必须产生新 run，绝不覆盖旧 run。
- run 是不可变输入与输出摘要；不得保存 PII、API key、prompt 或 chain-of-thought。

### 3.4 `grading_request`

- 保存 tenant/file/run、idempotency key hash、request fingerprint 与 created_at。
- unique `(tenant_id,idempotency_key_hash)`；复合 FK 回到同一 grading run。
- completed replay 先按 ledger 读取历史 run，再校验 request/input identity；不得读取
  current config、最新 F7 run 或重新执行 grader。
- 新 key + 同完整业务 identity 只追加 alias request，不新增 run/item/audit complete。

### 3.5 `grading_item`

至少包含：

- `id,tenant_id,grading_run_id,file_version_id,source_kind`
- nullable `finding_id/validation_run_id/correlation_finding_id/detection_run_id/
  investigation_run_id/investigation_result_id`
- source kind/rule kind/detector/outcome/capability/F7 terminal snapshot 与各来源 fingerprint
- `severity_impact,severity_confidence,disposition,reason_codes_json,evidence_snapshot,
  item_fingerprint,first_row_no,created_at`

CHECK 与 FK：

- `source_kind=deterministic`：必须有 finding，禁止 correlation/investigation；finding 必须
  属于绑定的 validation/file/tenant，且 outcome 仅 flagged/unavailable。
- `source_kind=correlation`：必须有 correlation finding；investigation 可空，但若非空必须
  通过完整复合 FK 绑定同 candidate/detection/file/tenant，且 snapshot 对应唯一终态。
- impact/confidence 均为 `0..3`；disposition 仅三值；reason codes 为有界、非空、
  稳定排序的已知枚举数组；evidence 为有界 JSON object。
- deterministic 与 correlation 分别使用 partial unique index，保证每 grading run 中每个
  source 恰一项；item fingerprint 在 run 内唯一。
- `0010` 只为 F8 复合 FK 在既有来源表增加 catalog 约束，不更新来源事实：
  `finding(id,validation_run_id,file_version_id,tenant_id,rule_kind)`、
  `correlation_finding(id,detection_run_id,file_version_id,tenant_id,detector,
  detector_version,finding_key)`、`capability_declaration(id,detection_run_id,
  file_version_id,tenant_id,config_fingerprint,detector,detector_version,status)` 与
  `investigation_result(id,investigation_run_id,correlation_finding_id,detection_run_id,
  file_version_id,tenant_id,outcome,result_fingerprint)` 各新增一个命名复合 UNIQUE，并在
  空事实 downgrade 时精确移除。nullable `evidence_sufficient` 不进入 UNIQUE/FK，由延迟
  一致性触发器核对。

### 3.6 `grading_item_row`

- 保存 `id,tenant_id,grading_run_id,grading_item_id,file_version_id,row_no,ordinal,
  source_row_fingerprint,created_at`。
- deterministic item 恰一行；correlation item 必须与 F6 物理参与行集合完全相等。
- unique `(grading_item_id,ordinal)` 与 `(grading_item_id,row_no)`；ordinal 从 1 连续；
  复合 FK 同时闭合 item/run/file/tenant 与真实 expense row。
- 不在 JSON 数组中以参与行号替代本表；UI 分页与证据链以物理 row snapshot 为准。
- `0010` 使用 `DEFERRABLE INITIALLY DEFERRED` constraint trigger 在事务提交时机械校验：
  deterministic item 恰有一行且等于 `finding.row_no`；correlation item 的 row_no 集合与
  对应 F6 `correlation_finding_row` 集合双向相等；ordinal 恰为 `1..N`；completed run 的
  eligible F3/F6 source 集合、item、row 与 disposition/source counts 和摘要一致；
  `first_row_no=min(row_no)`；capability、F3 outcome 与 F7 terminal snapshot 均与冻结来源
  一致；提交后不得存在 `in_progress` run。普通 CHECK/FK 不能替代这些全集约束。触发器
  必须同时由 run/item/row INSERT 触发，测试以 `SET CONSTRAINTS ALL IMMEDIATE` 或真实
  COMMIT 强制执行，不能依赖未提交事务中的假绿。实现必须使用 transaction-local
  validated-run cache：每次 run/item/row INSERT 先使该 run 的缓存失效，deferred event
  只让同一事务内每个 run 做一次最终全量验证；提前 `SET CONSTRAINTS` 或后续事务追加
  child 会再次失效并重验。禁止为每个 row 重扫全集形成 O(N²) 提交路径。

### 3.7 审计与不可变

- config create 与 run complete 各追加一次无 PII audit；failed run 使用独立短事务只写
  安全 ID/hash/reason code，不保存 manifest 或原始异常文本。
- 五张 F8 表全部使用项目既有不可变 trigger 模式，UPDATE/DELETE 在 DB 层拒绝。
- 不新增级联删除，不弱化 `row_result`、`audit_log`、F4/F5/F6/F7 约束。

---

## 4. CP-F8.2 — 配置 schema 与纯确定性算法

### 4.1 `grading-config-v1`

Pydantic 判别模型 `schema_version=1`、`algorithm_version=cost-matrix-v1`，
`extra=forbid` 且 frozen。definition 必须完整包含：

- `impact_by_rule_kind`：F3 五个 rule kind 各映射 `0..3`，key 必须全集且无额外项。
- `impact_by_detector`：F6 四个 detector 各映射 `0..3`，key 必须全集且无额外项。
- `confidence_by_investigation_outcome`：`sufficient/insufficient/unavailable/max_steps/
  failed/not_run` 各映射 `0..3`，key 必须全集；`not_run` 必须恰为 0，避免可配置字段
  与显式未调查语义冲突。
- `capability_confidence_cap`：固定全集 enabled/degraded/unavailable，值不得高于 3/2/0。
- `impact_cost_units[0..3]`：四个 `0..1000000` 整数，level 0 必须为 0，随后严格递增。
- `confidence_issue_probability_bps[0..3]`：四个 `0..10000` 整数，level 0 必须为 0，
  随后严格递增。
- `false_negative_multiplier_bps`、`false_positive_cost_units`、
  `manual_review_cost_units`：正整数且均不超过 1000000。所有整数边界显式拒绝 bool、
  float 与字符串；loss 最大值保持在 signed 64-bit 范围内。
- `disposition_matrix`：完整 4×4 单元格，值为三种 disposition。

不提供内置业务默认配置。无 current config 时 F8 capability 明确为
`unavailable/GRADING_CONFIG_MISSING`；配置员必须通过 UI/API 创建首版。合成测试夹具
使用提交在测试代码中的明确 config，不能依赖环境变量或数据库残留。

### 4.2 代价矩阵的机械生成

对每个 `(impact,confidence)` 使用整数运算：

```text
clear_loss = impact_cost_units[impact]
             * false_negative_multiplier_bps
             * confidence_issue_probability_bps[confidence]

flag_loss = false_positive_cost_units
            * 10000
            * (10000 - confidence_issue_probability_bps[confidence])

review_loss = manual_review_cost_units * 10000 * 10000
```

选择 loss 最小的动作：`flag -> high_attention`、`review -> manual_attention`、
`clear -> cleared`；相等时以 `high_attention > manual_attention > cleared` 的保守顺序
破平。4×4 matrix 生成阶段只应用不依赖来源的前两项安全覆盖：

1. impact=3 不得 cleared；若初算 cleared，改为 manual。
2. confidence=0 不得 cleared。

item grader 从已验证的 matrix 取得初算结果后，再应用后三项来源安全覆盖：

3. F3 unavailable 必须 manual。
4. F7 `insufficient/unavailable/max_steps/failed/not_run` 必须 manual，不得 high 或 clear。
5. F6 capability unavailable 必须 manual。

config service 必须重新生成全部 16 格，并要求客户端提交的 disposition matrix 与结果
完全相等；不一致拒绝。这样 UI 可预览、审计可阅读，同时运行时不存在两套规则。

### 4.3 item 算法

- F3 flagged：impact 取 rule mapping；confidence 固定 3。
- F3 unavailable：impact 取 rule mapping；confidence 固定 0。
- correlation：impact 取 detector mapping；confidence 为 investigation outcome mapping 与
  capability cap 的较小值。只有 manifest 中显式 `not_run` 才按 0；缺 manifest entry、
  nullable 字段与 entry kind 不一致或未知/矛盾终态均拒绝。
- disposition 仅查已机械校验的 4×4 matrix 并应用来源安全覆盖。
- reason codes 必须能逐项解释 impact mapping、confidence base、cap、override 与最终动作。

纯核心只接受 frozen DTO，返回 frozen DTO；不访问数据库、网络、环境变量、当前时间、
random、进程 hash 或 locale。排序固定为 disposition rank、impact desc、confidence desc、
first row、source kind、source ID。相同 canonical config + manifests 必须产生相同 item
fingerprint、顺序和结果。

---

## 5. CP-F8.3 — Service、幂等与恢复

### 5.1 创建顺序

1. 校验 actor/tenant/idempotency key 与显式 run/config/F7 manifest 形状。
2. 按 `Tenant -> FileVersion NOWAIT` 固定锁序进入事务。
3. 若 ledger 已存在，按历史 snapshot 校验并 replay；不读取 current config。
4. 加载显式 F3/F6/F7 业务事实，构造三份 canonical manifest；任何 drift fail closed。
5. 在写入前于内存运行纯 grader，并机械验证 item/row 全集、fingerprint 与 counts。
6. 单事务追加 completed run、全部 item、全部 item row、request ledger 与唯一
   `grading.run_complete` audit。
7. 任一步失败回滚全部业务事实；另开 tenant session 追加安全 failed audit。

新 key 先构建权威 manifests/input fingerprint 并查询完整 business identity：若历史 run 已
存在，仅追加 alias request，不调用 grader、不新增 item/row/complete audit；即使该 run 的
config 已非 current 也允许 alias。只有确需创建新 run 时才要求显式 config 等于 tenant
current config，否则返回 `GRADING_CONFIG_STALE`。

不持久化半成品 item。`in_progress` 只允许作为事务内部状态，事务提交后数据库中只能看见
完整 completed run。F8 无模型外部副作用，其 exactly-once 边界覆盖全部业务写入。

### 5.2 错误语义

稳定错误至少包含：config missing/stale、source run not found/not completed、tenant/file
mismatch、F7 manifest incomplete/duplicate/drift、input drift、idempotency conflict、lock
conflict、invalid config、internal rollback。消息不得包含 PII、manifest 内容、密钥或 SQL。

### 5.3 查询

- current endpoint 可返回该 file 最近实际 grading run，并同时给出相对 current config、
  F3/F6 run 的 stale flags；比较基准固定为 tenant 最新 grading config、该 file 当前 completed
  validation run（F3 现为 file 唯一）和该 file 按 created_at/id 排序的最新实际 detection run，
  响应同时返回三个基准 ID。创建 endpoint 绝不使用 current/latest 隐式补参。
- list/detail 全部显式 tenant predicate，数据库分页与 exact count；不得内存全量切片。
- item row 以 ordinal/row_no/id 稳定分页；详情只从 snapshot 与冻结来源读取。

### 5.4 恢复门禁

故障点至少覆盖 run、部分 deterministic item、部分 correlation item、部分 row、request、
success audit。另在 success audit 后以子进程 `os._exit` hard-kill，fresh process 使用同 key
重试，证明最终 run/request/item/row/complete audit 最多一次。不同 key 同业务 identity 只
产生 alias request；config/source/input 改变产生新 run或 fail closed，不修改历史。

---

## 6. CP-F8.4 — API、OpenAPI 与桌面 UI

### 6.1 API

固定 endpoint：

- `GET /api/v1/grading-configs/current`
- `GET /api/v1/grading-configs?limit=&offset=`
- `POST /api/v1/grading-configs`
- `POST /api/v1/files/{file_version_id}/grading-runs`
- `GET /api/v1/files/{file_version_id}/grading-runs/current`
- `GET /api/v1/grading-runs/{grading_run_id}`
- `GET /api/v1/grading-runs/{grading_run_id}/items`
- `GET /api/v1/grading-items/{grading_item_id}`
- `GET /api/v1/grading-items/{grading_item_id}/rows?limit=&offset=`

写 endpoint 要求 8..128 字符 `Idempotency-Key`；config create 还要求 expected version 与
change reason。run create body 必须显式给 validation/detection/config ID 和完整 F7 manifest。

item list 支持 source kind、rule kind、detector、impact、confidence、disposition、F7 outcome
过滤；只支持固定 default sort。所有 schema 使用 Pydantic 判别联合与 bounded collection，
响应加 `Cache-Control: private, no-store`。

权限沿用既有 permission 数据：config read/write 使用规则配置读写权限；run create 使用
批次写权限；grading run/item 查询使用批次读权限。不新增硬编码角色判断。未登录、无权限、
跨租户分别稳定为 401/403/404；路由只做 transport adapter，不含 SQL 或分级逻辑。

### 6.2 UI

- 配置员看到“二维分级配置”：完整 impact/confidence 映射、代价参数、机械生成的 4×4
  matrix 预览、版本历史、change reason 与 stale 状态；安全覆盖不可关闭。
- 批次页新增独立“综合分级”工作流，不改 F4 报告或 F5 复核页。auditor 可提交显式
  manifest 创建/replay run；viewer 只读且不得渲染可触发 POST 的控件。
- 列表同时显示 impact 与 confidence badge、disposition、source、首行和 F7 状态；详情同屏
  展示全部参与行、F3/F6/F7 来源、能力 cap、reason codes、调查终态/steps 链接及已验证引用。
- 明确区分 config missing、absent、stale、loading、empty、conflict、degraded、unavailable、
  error；禁止展示或解释 legacy severity `0/0`。
- 所有外部数据以 React 文本节点渲染；不使用 raw HTML，不把响应写 local/session storage。

修改 Pydantic 契约后必须导出 OpenAPI 并生成前端 client；两次连续生成字节稳定。

---

## 7. CP-F8.5 — 测试、安全、性能与交付门禁

### 7.1 迁移与约束

- 私有备份 hash/list/隔离恢复；`0010` upgrade、空事实精确 downgrade、有事实 downgrade
  guard、重复 upgrade、默认/测试双库 head 与 Alembic 零漂移。
- config/run/request/item/row 的复合 tenant FK、unique/check/byte limit/RESTRICT/immutable
  trigger 正反例；跨租户、错 file/run/source、缺行/多行全部失败。
- 证明 `finding`、`correlation_finding`、F4/F5/F6/F7 事实及旧 migration 文件零更新。

### 7.2 纯核心与 service

- 五 rule、四 detector、六 F7 outcome、三 capability、0/3 边界、全部 16 个 matrix cell。
- integer loss、保守 tie-break、五项安全覆盖、matrix mismatch、未知/重复/超限输入。
- deterministic 路径模型调用严格为 0；grader 全路径网络调用严格为 0。
- 同 config/input 的 canonical/fingerprint/order 稳定；DB 返回顺序、时区、locale、进程变化
  不影响结果。
- 原子创建、same/different key、alias、NOWAIT/固定锁序、source/config/input drift、零 item、
  六类 fault 与 hard-kill fresh-process 恢复全部通过。

### 7.3 API、前端与安全

- 401/403/404/409/422/503、三角色、跨租户、exact total、过滤/排序、恶意 Unicode/控制
  字符/超长 JSON、cache header 与错误无 PII。
- OpenAPI/client 连续二次稳定；后端/前端 strict 类型，前端禁 `any`，运行时 Zod fail closed。
- Chrome 1440×1000 覆盖 config/history、run absent/current/stale/replay/conflict、16 格 matrix、
  两类 source、全部 F7 终态、长参与行、empty/loading/error 与三角色；页面级横向溢出、
  非模块 script/image 执行、恶意标记、local/session storage 均为 0。
- gitleaks 全历史、pre-commit、`pip-audit --strict`、`npm audit --audit-level=high` 全绿。

### 7.4 固定 seed 5000 行

- 扩展既有 seed=3500 合成夹具，标签与 XLSX/业务输入继续物理分离。
- 执行 F1→F8 完整链路；F7 只用 deterministic scripted provider，禁止真实模型、真实 key
  和公网。F6 全部 candidate 必须在显式 F7 manifest 中恰出现一次。
- 机械核验 F3 风险 finding、F6 四类真值、F7 终态、F8 impact/confidence/disposition 与全部
  参与行；不得只断言“非空”。
- 记录各阶段耗时、总耗时、SQL 数/时间、item/row/manifest bytes、首次 run 与至少 25 次
  replay/list/detail p95、最大响应体；F1→F8 + 交互总耗时必须 `<=900s`，交互 p95 `<2s`。
- 性能证据与浏览器截图只放 `data/private/cp-f8.5/`；不得提交合成主体明文或私有产物。

### 7.5 全量工程门禁

- 后端：全量 pytest、Ruff lint/format check、strict mypy、双库 migration/head/zero drift。
- 前端：全量 tests、typecheck、oxlint、Prettier check、production build。
- contract、pre-commit、secret scan、依赖审计以及与 F3/F4/F5/F6/F7 的定向回归全部通过。
- 全部结果写入第 10 节实际落地记录，并更新 AGENTS/MEMORY 后，才可把 F8 标记完成。

---

## 8. Checkpoint 计划与退出条件

### CP-F8.0 — 规格固化

- **交付：** 本 Spec 009。
- **退出：** config、snapshot identity、算法、API/UI、恢复与门禁均无实施决策残留；
  `git diff --check` 通过。只新增规格，不实现 F8。

### CP-F8.1 — 持久化与迁移

- **交付：** 私有备份、`0010`、ORM、复合 FK、不可变 trigger 与迁移测试。
- **退出：** backup/upgrade/downgrade/guard/tenant/legacy zero-write/双库 zero drift 全绿；
  不实现 grader/service/API/UI。

### CP-F8.2 — 配置与纯核心

- **交付：** frozen config/source/result schema、canonicalizer、cost matrix 与纯 grader。
- **退出：** 16 格、来源/outcome/cap、整数 loss、安全覆盖、稳定 fingerprint 和零网络测试
  全绿；不访问数据库或 UI。

### CP-F8.3 — 原子 service、查询、幂等与恢复

- **交付：** config/run/query service、manifest builder、request ledger、原子 snapshot、audit。
- **退出：** 并发、alias、drift、fault、hard-kill/fresh restart 与 DB 分页门禁全绿；路由尚未暴露。

### CP-F8.4 — API、OpenAPI 与桌面工作流

- **交付：** 第 6 节 endpoint、generated client、配置页与批次综合分级页。
- **退出：** 权限/租户/cache/契约/前端静态/build/Chrome 全状态门禁通过；不改 F4/F5。

### CP-F8.5 — 契约、安全、性能与交付

- **交付：** 第 7 节全部机械证据、固定 seed 5000 行结果与落地记录。
- **退出：** 全部门禁零退出；确认零真实模型调用、零旧 severity/F4/F5 回填后，F8 才可
  标记完成并进入阶段 3/4 代码就绪收尾。

---

## 9. `external_validation_pending`

以下不阻塞“代码就绪 MVP”，但必须保持 `external_validation_pending`，不得描述为生产
验收完成：

1. 真实 OpenAI-compatible 云 API 的鉴权、延迟、限流、结构化输出兼容性、质量与成本。
2. 真实本地 vLLM 以及 embedding/rerank 镜像、离线权重、检索质量和资源占用。
3. 使用真实客户月度批次校准 impact cost、confidence probability、FN/FP 与人工复核成本；
   在完成随机放行抽检前，不得声称现有配置满足漏放率/误拦率目标。
4. 真实客户数据的 PII 审批、稳定 token 抽查、业务接受度与财务签字。
5. 实际生产部署、密钥注入、监控告警、真实 trace、发布和回滚演练。

离线必须证明：缺模型/API 配置时应用正常启动，F7 显式 unavailable，F8 对其按安全规则
转人工；F8 自身不发模型或检索网络请求。真实输入未到位不允许降低测试门禁或使用猜测
阈值替代外部验收。

---

## 10. 实际落地记录

### CP-F8.0 实际落地记录（2026-08-10）

- 新增本规格，固定 F8 使用独立不可变 config/run/request/item/row snapshot，显式绑定
  F3/F6/F7 manifest，不回填 legacy severity，不修改 F4/F5 历史语义。
- 固定数据驱动 impact/confidence mapping、整数代价矩阵、保守 tie-break 与不可弱化安全覆盖；
  无 current config 时显式 unavailable，不内置静默业务默认值。
- 固定纯确定性 grader、原子 ledger/alias/drift/hard-kill 恢复、强类型 API/UI 与固定 seed
  5000 行门禁；F8 全路径不调用模型或检索网络。
- 本检查点只新增规格文件，未创建迁移、ORM、service、API/UI、依赖、基础设施或 F8 行为。

2026-08-11 实施前只读审计补强了本规格而未改变 F8 产品边界：固定三份 manifest、
evidence 与 reason codes 的精确上限及 23 项 reason 顺序；把 4×4 通用覆盖与 item 来源
覆盖分开；明确 `not_run=0`、缺 manifest entry fail closed、代价整数上限、完整来源复合
identity 与 deferred snapshot constraint trigger。补强后实施者不再需要临时发明这些语义。

### CP-F8.1 实际落地记录（2026-08-11）

- 默认库迁移前 full/schema/affected-data custom archives 位于 gitignored
  `data/private/backups/mvp-loop/pre-0010-20260810-145851/`，三份均通过
  `pg_restore --list`。SHA-256（affected/full/schema）依次为
  `3A8F7B894BBF262B0949C44D23462CD4CB6A18C9291CF3F2C3F79B029BDFE345`、
  `D89CE3E7302A8558E98707F04049171D6630FE160F38470B2269EE2CDD3F6B5B`、
  `E8022D1C0EEE22A6DA82E319F1ECD36A279259559C6D287F561A21076DF59E6C`。
  full archive 已恢复到隔离库，验证 `0009`、行级幂等 unique 与 audit trigger 后移除隔离库。
- 新增 `0010` 与五张独立不可变 F8 表；四张既有来源表只增加供复合 FK 使用的命名
  UNIQUE，不更新任何旧事实。config/run/request/item/row 全部使用 tenant-bound `RESTRICT`
  identity、JSON/hash/branch/byte guards、partial unique、不可变 trigger；reason code 在 DB
  层验证已知、唯一和固定排序。
- `DEFERRABLE INITIALLY DEFERRED` constraint triggers 在提交边界核验 completed run、F3/F6
  来源全集、deterministic/F6 物理参与行集合、连续 ordinal、first row、摘要计数及
  capability/F7 snapshot；服务提交后不得留下半成品 `in_progress`。只读性能审计发现逐
  child 全量验证会形成 O(N²)，因此已改为普通 INSERT invalidation + `pg_temp` transaction-
  local validated-run cache；正常事务每 run 只全验一次，提前约束执行或后续事务追加 child
  会重新置 dirty 并再次验证。
- 默认库与测试库均为 `0010 (head)` 且 `alembic check` 零漂移。独立验证库完成
  `0009→0010→0009→0010` 空事实精确往返；写入 config 事实后 downgrade 在任何 DDL
  前稳定拒绝且库仍停留 `0010`。
- CP-F8.1 ORM/迁移/受保护约束定向 `28 passed`；Ruff、214 文件 format check、strict
  mypy（148 个源文件）和 `git diff --check` 通过。未实现 grader/service/API/UI，未改写
  legacy severity、F4/F5/F6/F7 事实或旧迁移。

### CP-F8.2 实际落地记录（2026-08-11）

- 新增独立纯核心 `app/core/grading/`：deep-frozen `extra=forbid` config/source/result DTO、
  F8 自有 domain-separated canonical SHA-256、纯整数 4×4 cost matrix 与 deterministic
  grader；不访问 DB、网络、环境、时间、random 或模型层。
- schema 精确覆盖五个 F3 rule、四个 F6 detector、六个 F7 outcome、三种 capability、
  strict `0..3` level、四元 cost/probability vector、完整 matrix 与 23 项固定 reason code。
  bool/float/string coercion、未知/重复 source、5001 行、超限 canonical 与 matrix mismatch
  均 fail closed；`not_run=0` 且 F7 三态 sufficiency 保持原义。
- 16 格使用 signed-64 范围内的整数 loss 和 `high > manual > clear` 保守破平；impact=3/
  confidence=0 两项 matrix 覆盖与 F3 unavailable/F7 non-sufficient/F6 unavailable 三项来源
  覆盖按固定顺序执行，只记录实际改变 disposition 的规则。item 同时保留 matrix 与最终动作。
- F8 core 定向 `43 passed`，覆盖 5 rules×2 outcome、4 detectors×6 F7 outcome×3 capability
  的 72 组合、16 格、tie、五项覆盖、5000/5001、最大 loss、恶意类型、Unicode/canonical、
  排序/指纹稳定与 socket 零网络；Ruff/format、grading target mypy 及全量 strict mypy
  （153 个源文件）通过。未实现 service/API/UI，未调用任何模型或检索网络。

### CP-F8.3 实际落地记录（2026-08-11）

- 新增 append-only config service、三份 authoritative manifest builder、原子 grading run
  service 与 tenant-scoped query service。config 创建具备 tenant NOWAIT、expected-version
  CAS、同键 replay、异请求冲突、同 fingerprint 新键稳定拒绝、current/history exact-count
  分页及原子成功/安全失败审计。
- F3/F6/F7 manifest 从显式 file/validation/detection/config/F7 request identity 批量加载，
  对 RuleEvidence、CorrelationEvidence、四项 capability、物理参与行、F7 terminal result/
  citation、来源行与 8/16/32 MiB 边界 fail closed。真实 PostgreSQL 审查发现并修复
  `detection_run.finding_count` 未与实际 candidate 全集核对的缺口；重复构建和逆序 F7 请求
  产生完全相同的 canonical manifest、顺序与 fingerprint。
- run service 自持事务并固定 `Tenant -> FileVersion NOWAIT` 锁序；同 key 历史 replay 不读取
  current config、不重建 manifest、不调用 grader；新 key 同完整 identity 只追加 alias request；
  仅真正新 run 校验 current config。completed run、全部 item、物理 row、request 与唯一
  `grading.run_complete` audit 单事务提交，item/row 以 250 条批量写入。
- 六个 pre-commit fault stage、`success_audit_written` 前提交 hard-kill 与
  `transaction_committed` 后提交 hard-kill 均由 fresh process 同 key 恢复，证明回滚边界和
  exactly-once；zero-item、input/source/config drift、锁/幂等冲突及安全失败审计通过。
- query service 返回 current config/validation/detection 三个基准 ID 与独立 stale flags；item
  全过滤、固定全序、exact total/数据库分页、跨租户 404、参与行 typed 回读与 source-row
  fingerprint 漂移关闭均通过，list/rows 查询固定三条 SQL，无 N+1。
- CP-F8.3 config/manifest/run/query 串行 PostgreSQL 门禁 `33 passed`；F8.1/F8.2/F8.3 相关
  Ruff/format 全绿，strict mypy `app scripts`（158 个源文件）通过。测试清理清单已加入 F8
  五表，避免跨测试残留造成幂等假绿。尚未暴露 API 或修改前端。

### CP-F8.4 实际落地记录（2026-08-11）

- 新增第 6 节固定的 9 个强类型 endpoint；配置读写分别沿用 `config:read/write`，run
  创建与查询沿用 `batch:import/read`，跨租户维持 404，全部成功和领域错误响应均为
  `Cache-Control: private, no-store`。路由只做 transport adaptation，源码不含 SQL 或分级
  业务逻辑。F3 摘要补充显式 `validation_run_id`，F7 暴露冻结 input/config/result
  fingerprint 与 typed citation，F8 manifest 不从 current/latest 隐式补参。
- OpenAPI 与生成客户端连续两次字节稳定：SHA-256 分别为
  `2B5DF168D112395D6C0F1F2F7111E5E3B5065CE9C1F5B04793343E286B0A0A24` 与
  `8E90C9EF90FA4760D460936E8463792F6615F9F396AFA2D5182987F0CB9FC77C`。API 定向覆盖
  401/403/404/409/422、三角色、跨租户、strict integer、过滤/exact total 与 no-store。
- 前端新增权限驱动的“二维分级配置”页和批次独立“综合分级”页签。配置页不内置业务
  默认值，完整暴露映射、整数成本、机械 4×4 matrix、current/history/CAS/replay；批次页
  分页收集完整 F6 candidate ID 集并显式写入 `not_run/INVESTIGATION_NOT_RUN` manifest，
  不猜测 F7 结果。viewer 无 POST 控件；列表/详情展示双轴、处置、来源、cap、reason、
  typed evidence、全部参与行和 investigation steps 链接，不读取或解释 legacy severity。
- 前端运行时 Zod 对完整 config、严格整数、矩阵一致性、Unicode/控制字符和未知字段
  fail closed。全量为 17 个文件、92 passed，strict TypeScript、oxlint、Prettier 与生产构建
  通过；分页按钮补充唯一可访问名称，消除同名控件导致的偶发误操作测试。
- 真实 Chrome 153、精确 1440×1000 覆盖 config current/missing/readonly/error 与 batch
  normal/viewer/absent/stale-empty/error/malicious 共 10 场景；全部页面级横向溢出、非模块
  script/image 执行、恶意标记和 local/session storage 均为 0。截图与机械 metrics 位于
  gitignored `data/private/cp-f8.4/`，已人工检查代表性的配置页与正常批次页。

### CP-F8.5 实际落地记录（2026-08-11）

- 固定 seed=3500 的 5000 行 F1→F8 链路重新生成并运行，总耗时 `230.203843s`，低于
  900 秒硬上限；F6 四类嵌入真值全部机械命中，F7 对 130 个 candidate 使用 deterministic
  scripted provider 形成 130 个 `insufficient` 终态，真实模型调用与外部 HTTP 均为 0。
- F8 首次原子 run 为 `4.906561s`/34 SQL（SQL 累计 `1.776360s`），产出 1180 个 item
  与 1916 条物理参与行：1045 deterministic + 130 correlation，处置为 5 high、130 manual、
  1045 cleared；来源全集、F7 manifest 一次且仅一次、参与行全集、summary arithmetic 与旧
  F3/F6 severity SHA-256 均机械一致。manifest F3/F6/F7 分别为 304954/158942/72616 bytes，
  item/row 总量为 2021200/827687 bytes。
- 25 次 F8 replay/list/detail/rows p95 分别为 `0.228635s`、`0.086100s`、`0.021453s`、
  `0.019709s`，均低于 2 秒；最大响应体以 list 的 61424 bytes 为上限。当前主机在加入
  `0010` 后重跑旧 F6 HTTP harness 时 detect initial/replay 为 `3.763388s`/`3.541288s`，高于
  F6 交付时的 `1.973916s`/`1.687987s`，但完整链路仍远低于 900 秒且 F8 交互门禁通过；
  该上游主机相关回归保留给阶段 3 全局性能收尾，不把它改写成 F8 通过数据。
- 后端全量 `594 passed, 1 skipped`，Ruff lint、237 文件 format check、strict mypy（159 个
  source）通过；默认/测试双库均为 `0010 (head)` 且 Alembic 零漂移。迁移往返、事实
  downgrade guard、复合租户身份、不可变 trigger、六故障点与 hard-kill/fresh-process
  已包含在全量门禁。前端全量 92 passed 及全部静态/构建门禁通过；生产构建仅保留已知
  585.75 kB 主 chunk 非阻断提示。
- `pip-audit --strict` 与前端完整/生产依赖 audit 均为零漏洞；gitleaks v8.30.1 扫描 37 个 commit、
  4.02 MB 后零泄漏。五个 F7 synthetic scoped-token 历史误报以 commit/path/rule/line 精确
  fingerprint 写入 `.gitleaksignore`，未使用路径或规则级宽免除。审计过程中发现 Redocly
  1.34.19 已要求修复后的 js-yaml 4.3.1，但旧 package override 仍强制 4.3.0；移除该过时
  override 并重新锁定后，完整 npm audit 从两条高危开发期公告降为 0，契约生成保持稳定。
- 未提交 `.env`、`tenants/`、`data/private/`，未修改既有迁移、基础设施、F4/F5 语义或
  legacy severity。真实云 API、本地模型、客户月度批次、真实 PII 审批与生产部署保持
  `external_validation_pending`；F8 代码就绪闭包完成，下一步进入阶段 3/4 工程收尾。
