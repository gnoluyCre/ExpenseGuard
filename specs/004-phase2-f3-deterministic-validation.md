# Spec 004 — Phase 2 F3 确定性校验

**状态：** CP-F3.0 规格已固化
**最近更新：** 2026-07-28
**后续检查点：** CP-F3.1 持久化 schema 与 ORM

## 1. 目标与范围

F3 消费 F2 已通过 Pydantic 校验并写入 `expense_row.normalized_json` 的统一报销记录，以不可变、数据驱动的规则版本完成五类硬规则校验：限额、票种、时效、抬头和发票号查重。

本阶段必须满足：

- 相同 `normalized_json`、相同映射版本和相同规则集快照产生逐字段一致的结果。
- 每条命中或不可判定记录逻辑 `rule_id`、原始 `row_no` 和结构化证据；已选中配置时必须同时记录具体 `rule_version`，早于首个生效版本时以 null 版本和 `RULE_NOT_EFFECTIVE` 显式留痕。
- 阈值、允许集合和例外全部来自强类型配置，不在代码中硬编码客户制度值。
- 规则依赖不足时显式转人工，绝不把“无法判断”当作通过。
- `finding` 与对应 `row_result` 在同一事务中提交；同一批次重放、并发请求或崩溃重启不得产生重复副作用。
- 租户内全历史发票号查重必须确定、隔离且可复现。

### 1.1 明确不在 F3 范围内

- 不接入 LLM、RAG、制度条款检索或条款引用；这些属于 F4。
- 不实现连号、拆单、频次、时空冲突或模糊重复；这些属于 F6。
- 不实现发票联网验真、OCR 纠错或商户/金额组合模糊匹配。
- 不实现 F8 的二维风险标定；F3 只输出确定性 verdict 和规则事实。
- 不允许任意 JSON Logic 表达式、任意变量路径、代码、正则、网络调用或插件。
- 不允许在同一 `file_version` 上换映射或换规则重算；需要对相同原始证据应用新映射或新规则时，必须走 §7.4 的显式派生版本流程，不能覆盖历史结果。

### 1.2 对旧设计的覆盖决定

- 本规格以五类强类型 Pydantic 判别联合和决策表实现“规则即数据”，覆盖 TechDesign 中开放式 JSON Logic 的候选设计；配置仍然版本化、数据驱动，但不允许任意运算符。
- 规则 API 使用会话注入租户的 `/api/rules`，覆盖旧设计的 `/api/tenants/{id}/rules`；客户端永远不能指定租户。

## 2. 术语与不变量

- **规则标识：** `rule_id` 是租户内稳定业务标识；规则语义变化创建新 `version`，不原地修改旧版本。
- **规则配置指纹：** 对 `{rule_id, effective_from, canonical definition}` 计算 SHA-256，结果为 64 位小写十六进制字符串；相同 definition 改变生效日仍是新版本。
- **规则集清单：** 本批次实际可选择的有序 `(rule_id, version, config_fingerprint)` 集合。
- **规则集指纹：** 对 `schema_version`、选择算法版本、`mapping_version_id` 和有序规则集清单计算 SHA-256。
- **首次快照冻结：** 批次第一次成功校验时冻结规则集清单和指纹；后续规则发布、停用或补录不改变历史结果。
- **行级结果：** 每个成功解析的原始行最多一条 `row_result`；每个实际命中或不可判定规则可有一条 `finding`。

以下既有约束不得弱化或删除：

- `expense_row` 的 `unique(file_version_id, row_no)`。
- `row_result` 的 `unique(file_version_id, row_no)`。
- `rule_config` 的 `unique(tenant_id, rule_id, version)`。
- `audit_log` 的追加写语义。
- 所有业务读取的租户 fail-closed 隔离。

`row_result.rule_version` 在 F3 中固定存放 64 位规则集指纹；具体命中规则版本继续写入 `finding.rule_id` 和 `finding.rule_version`。

## 3. 行级输出语义

行级 `verdict` 只有以下三种：

| verdict | 语义 |
|---|---|
| `passed` | 所有适用且启用的规则均完成求值，未命中，也没有不可判定规则 |
| `manual_review` | 未命中违规，但至少一条应执行规则因缺字段、来源不满足或配置无匹配项而不可判定 |
| `flagged` | 至少一条规则确定性命中；即使同时存在不可判定规则，聚合 verdict 仍为 `flagged` |

聚合优先级固定为 `flagged > manual_review > passed`。所有不可判定事实仍必须保留，不能因为同一行已有命中而丢弃。

F2 解析失败行不进入规则求值，不创建 `row_result`，在批次摘要中计入 `parse_failed_count` 并保留 F2 错误清单；它们不得计入 `passed_count`。

## 4. 强类型规则配置

### 4.1 通用结构

`rule_config.definition` 使用 Pydantic 判别联合，所有模型 `extra="forbid"` 且冻结。通用字段为：

```json
{
  "schema_version": 1,
  "kind": "limit",
  "enabled": true,
  "require_direct": false,
  "exemptions": []
}
```

- `kind` 仅允许 `limit`、`invoice_type`、`timeliness`、`invoice_title`、`invoice_duplicate`。
- `enabled=false` 表示该版本从其 `effective_from` 起显式停用；停用也必须创建新版本，求值时对该规则显式产生 `RULE_DISABLED` 不可判定事实，不能静默当作通过。
- `require_direct=true` 时，任一规则依赖字段的 provenance 不是 `mapped` 即转 `manual_review`。
- `effective_from` 必填；同一 `rule_id` 的版本号由服务端单调递增分配。
- 既有 `is_active` 仅作兼容字段，新 API 不允许原地切换；F3 新建版本统一写 `is_active=true`，启停由不可变 definition 表达。

API 保存前先完成规范化、排序和 Pydantic 校验，再对 `{rule_id, effective_from, canonical definition}` 计算指纹。相同 `rule_id` 的最新指纹一致时返回幂等复用，不创建版本或审计；definition 或生效日变化均追加新版本。

### 4.2 统一例外结构

例外是 OR-of-AND 精确条件组：`exemptions` 中任一组匹配即跳过该规则；组内 `all` 条件必须全部匹配。

```json
{
  "exemption_id": "approved-executives-v1",
  "all": [
    {"field": "expense_type", "value": "差旅"},
    {"field": "currency", "value": "CNY"}
  ]
}
```

- `field` 仅允许枚举型规范化字段 `expense_type`、`invoice_type`、`currency`；员工、商户、抬头、地点、描述、日期、金额和发票号不得作为例外条件。
- 所有值使用与 F2 对应字段相同的规范化规则。
- 同组字段不得重复，空组、空值、重复 `exemption_id` 和重复条件组均拒绝。
- 例外匹配只写 `exemption_id`，审计 payload 不写条件值。缺失字段视为该例外不匹配，不改变规则本身的依赖判定。

例外求值顺序固定为：先按配置顺序检查例外组，再检查主规则依赖与决策表。例外组的全部条件匹配即可输出 `exempted`，此时主规则字段即使缺失也不转人工。`require_direct` 同时约束例外条件字段和主规则依赖：例外值匹配但来源为 inferred 时输出 `INFERRED_FIELD_NOT_ALLOWED`，不能把它当作未匹配后继续判定；例外条件字段缺失则该组单纯不匹配，继续检查下一组/主规则。

配置规模限制固定为：请求 canonical UTF-8 JSON 最大 256 KiB；`rule_id`/`exemption_id` 最大 128 字符；普通分类与抬头值最大 512 Unicode code point；例外组最多 100 个、每组最多 3 个条件；thresholds/allowances/policies 各最多 500 项；单个允许集合及 `allowed_titles` 最多 500 项。超过限制统一返回 `RULE_CONFIG_INVALID`，不得静默截断。

### 4.3 provenance 规则

- `mapped` 字段始终可供规则求值。
- `inferred` 默认可求值，finding 证据必须标记 provenance 和 inference rule ID。
- `require_direct=true` 且依赖字段为 `inferred` 时，该规则输出不可判定。
- 依赖字段为 null、缺少 provenance 或结构不合法时输出不可判定，不尝试从 `raw_json` 猜测。

## 5. 五类规则

### 5.1 限额 `limit`

配置包含非空 `thresholds` 决策表，每项为：

```json
{"expense_type": "差旅", "currency": "CNY", "max_amount": "5000"}
```

- `(expense_type, currency)` 在同一版本内唯一，使用 F2 规范化值精确匹配。
- `max_amount` 是大于零、非指数形式的十进制字符串，canonical form 不保留无意义尾零。
- 依赖字段为 `amount`、`expense_type`、`currency`。
- `amount > max_amount` 命中 `limit_exceeded`；等于或低于阈值通过。
- 没有匹配阈值时不可判定，reason code 为 `LIMIT_THRESHOLD_NOT_CONFIGURED`。

### 5.2 票种 `invoice_type`

配置包含非空 `allowances` 决策表，每项为：

```json
{"expense_type": "差旅", "allowed_invoice_types": ["增值税电子普通发票"]}
```

- `expense_type` 在同一版本内唯一；允许票种集合必须非空、去重并按 Unicode 码点排序。
- 依赖字段为 `expense_type`、`invoice_type`，使用 F2 规范化值精确匹配，不做包含或模糊匹配。
- 票种不在允许集合时命中 `invoice_type_not_allowed`。
- 没有对应费用类型配置时不可判定，reason code 为 `INVOICE_TYPE_POLICY_NOT_CONFIGURED`。

### 5.3 时效 `timeliness`

配置包含非空 `policies` 决策表，每项为：

```json
{"expense_type": "差旅", "max_calendar_days": 30}
```

- `expense_type` 在同一版本内唯一，`max_calendar_days` 为 0–3660 的整数。
- 依赖字段为 `expense_type`、`expense_date`、`submission_date`。
- 计算自然日差 `submission_date - expense_date`；大于阈值命中 `claim_submitted_late`，等于阈值通过。
- 日差为负时不可判定，reason code 为 `SUBMISSION_BEFORE_EXPENSE_DATE`。
- 没有对应费用类型配置时不可判定，reason code 为 `TIMELINESS_POLICY_NOT_CONFIGURED`。

### 5.4 抬头 `invoice_title`

配置包含非空、去重并排序的 `allowed_titles`。依赖字段为 `invoice_title`；使用 F2 规范化值做完整字符串精确匹配。

- 不在允许集合时命中 `invoice_title_not_allowed`。
- 空值不可判定，reason code 为 `INVOICE_TITLE_MISSING`。
- 不做别名展开、包含匹配或相似度比较；企业别名必须作为允许值显式配置。

### 5.5 发票号查重 `invoice_duplicate`

该规则没有可变匹配口径；配置只使用通用字段。依赖字段为 `invoice_no`。

- 仅对非空 F2 规范化发票号做租户内完整字符串精确匹配。
- 全历史候选范围为同租户所有成功解析的 `expense_row`；解析失败行不参与。
- “同一原始证据”身份固定为 `(root_file_version_id, row_no)`：revision 1 的 root 是自身，派生 revision 继承同一 root。同 lineage 的同一物理行互不作为重复候选。
- 对每个其他 root lineage，只选快照时 `revision_no` 最大且成功解析的 revision 参与本次查重；当前批次所在 lineage 全部排除。validation run 把这些实际候选 `file_version_id` 写入不可变 `validation_dependency`。
- F2 不得重解析任何已被历史校验依赖的来源批次；需要变更时创建派生版本，使历史查重输入本身也被冻结。
- 同号记录按 root revision 1 的 `(uploaded_at, id, row_no)` 升序确定唯一首条；首条不命中，后续 root lineage 命中 `invoice_duplicate`。
- 同批多次、跨批次重复使用同一排序；跨租户永不互见。
- 前导零、大小写和空白已由 F2 规范化确定；F3 不再二次改写。规范化结果不同即不是重复。
- 缺失发票号不可判定，reason code 为 `INVOICE_NO_MISSING`。

## 6. 有效版本选择与快照

首次校验在租户锁内按每行 `expense_date` 选择规则版本：

1. 对每个 `rule_id` 仅考虑 `effective_from <= expense_date` 的版本。
2. 选择最大 `effective_from`；同一天存在多个版本时选择最大 `version`。
3. 每个租户的五种 kind 各自只能有一个稳定逻辑 `rule_id`；某 kind 为零个或多个逻辑 rule ID 时，规则集不完整/歧义，整批返回 409 `RULESET_INVALID`，不产生结果。
4. 选择结果的 definition 若 `enabled=false`，该规则输出 reason code `RULE_DISABLED` 的不可判定 finding，行级 verdict 至少为 `manual_review`。
5. 行的 `expense_date` 早于该逻辑 rule ID 的首个 `effective_from` 时，输出 `RULE_NOT_EFFECTIVE` 不可判定 finding；此时 `rule_config_id` 和 `rule_version` 为 null，但 `rule_id` 与 `rule_kind` 必须存在。

快照 manifest 保存五个逻辑 rule family 的 `rule_id`/kind，以及本批选择时可见且实际可能选中的不可变配置 ID、版本、指纹和选择算法版本；某批全部行都早于首个生效版本时，该 family 的 `selected_versions` 可以为空，但 family 本身仍进入指纹。manifest 不保存每行 PII。重放时只允许从 manifest 中选择，后续补录的回溯生效版本不能进入历史批次。

规则集指纹 canonical 输入固定为：

```json
{
  "schema_version": 1,
  "selection_algorithm": "effective-on-expense-date-v1",
  "mapping_version_id": "...",
  "rule_families": [
    {
      "rule_id": "expense.limit",
      "kind": "limit",
      "selected_versions": [
        {"version": 1, "config_fingerprint": "..."}
      ]
    }
  ]
}
```

对象键排序，family 按 `(kind, rule_id)` 排序，版本按 `(version, config_fingerprint)` 排序，UTF-8 紧凑 JSON 后计算 SHA-256。配置的输入顺序变化不得改变指纹。

## 7. 执行、并发与幂等

### 7.1 前置条件

- `file_version.parse_status` 必须为 `parsed` 或 `parsed_with_errors`。
- 必须有当前 `mapping_version_id`，且不存在绑定其他映射版本的 validation run。
- 批次至少有一行成功解析记录；全批解析失败稳定返回 409，不创建校验结果。

### 7.2 原子事务

单次校验按以下顺序执行：

1. 对租户父行执行 `SELECT ... FOR UPDATE NOWAIT`，串行化该租户的规则保存和批次校验；锁冲突返回 409。
2. 对 `file_version` 执行 `SELECT ... FOR UPDATE NOWAIT`。
3. 若已有 completed validation run，直接返回已有摘要，`reused_existing=true`，不新增 finding、row_result 或审计。
4. 固化规则集 manifest 和指纹，并对全部成功解析行求值。
5. 每行通过 `process_row_once` 写入；该行全部 finding 与 `row_result` 使用同一 session 和事务。
6. 写 validation run 完成状态、计数和 `batch.validate` 审计后一次性提交整批事务。

任何系统异常回滚本次全部业务写入；随后使用独立短事务追加 `batch.validate_failed`。失败审计只含批次 ID、映射版本 ID、尝试的规则集指纹和稳定错误分类，不含异常文本或报销数据。

租户父行锁覆盖跨批次查重并防止两个批次在同一快照中互相不可见。导入事务只有提交后的成功解析行才进入候选集；更晚上传的批次在其校验时会看到更早已提交的批次。

F2 parse/reparse 在检查 `validation_run`/`validation_dependency` 到提交映射和规范化结果的整个事务内，也必须获取同一租户父行 `FOR UPDATE NOWAIT` 锁；否则“先检查无依赖、再与 validate 交错提交”会破坏冻结证据。全新上传仍走 F1 自身事务，不要求持有该锁。

### 7.3 防重复约束

F3 后续迁移必须给确定性 finding 增加部分唯一索引：在 `validation_run_id IS NOT NULL` 时，同一 `(validation_run_id, row_no, rule_id, rule_kind)` 最多一条。新增字段对 F3 finding 必填，对未来非规则 finding 可空；`rule_config_id` 可在 `RULE_NOT_EFFECTIVE` 时为空。现有 `finding.kind` 继续表示 `limit_exceeded` 等 finding 类型，新增 `rule_kind` 才表示五类规则。不可判定、命中与例外是单个规则互斥的最终 outcome，不为同一规则写两条。

仅依赖当前 `process_row_once` 的“先查、再 compute、最后 upsert”不足以防并发 compute；调用方必须先获得上述租户锁和批次锁。

### 7.4 派生文件版本、映射与规则更新

- validation run 完成后，F2 重解析接口必须拒绝在同一 `file_version` 上切换映射，返回 409 `BATCH_ALREADY_VALIDATED`。
- F2 重解析接口也必须拒绝修改任何已被 `validation_dependency` 引用的 `file_version`，返回 409 `BATCH_USED_BY_VALIDATION`。
- 发布新规则版本不影响已有 validation run。
- 普通上传继续按 `(tenant_id, content_hash)` 幂等返回 revision 1，不因文件名或重复请求创建副本。
- 显式 `POST /api/batches/{id}/revisions` 在租户锁内创建同一内容的下一 `revision_no`，并写 `source_file_version_id` 和 reason；不得由普通上传隐式触发。
- `reason=ruleset_change` 要求来源状态为 `parsed|parsed_with_errors` 且至少一行成功解析，否则返回 409 `BATCH_NOT_PARSED`。满足条件时原子复制原始行、当前 `normalized_json`、解析错误、字段可用性和 `mapping_version_id`，但不复制 row_result、finding、validation run 或审计事件；新版本可直接冻结新的规则快照。
- `reason=mapping_change` 时只复制不可变 `raw_json` 和 `row_no`，清空规范化结果、解析错误和映射引用，状态重置为 `unparsed`，再走 F2 映射/解析流程。
- revision 1 的 `root_file_version_id` 为 null（逻辑上 root=自身）；任意派生版本的 `root_file_version_id` 固定指向 revision 1，`source_file_version_id` 指向本次直接来源。无论从哪个 revision 发起派生，都不得创建新的 root lineage。
- 派生版本保留原始 `content_hash`。`0004` 将既有唯一约束迁移为 revision 1 的 `(tenant_id, content_hash)` 部分唯一索引，加 `(tenant_id, content_hash, revision_no)` 唯一约束；F1 普通上传始终只查询/创建 revision 1，因此同文件重复导入幂等语义不变。
- 派生创建写 `batch.revision_create` 审计，只记录源/目标批次 ID、reason、revision_no 和映射 ID，不记录行数据。

## 8. 持久化计划（CP-F3.1 输入）

新增迁移固定为 `0004_f3_deterministic_validation.py`，不得修改 `0001`–`0003`。迁移至少包含：

- 新增 `validation_run`：继承 `TenantScopedMixin`，保存 `file_version_id`、`mapping_version_id`、规则集指纹、manifest JSON、状态、各 verdict/解析失败计数、完成时间和触发人；`file_version_id` 唯一，file/mapping 引用均使用 `(id, tenant_id)` 复合外键。
- 新增 `validation_dependency`：继承 `TenantScopedMixin`，以 `(validation_run_id, depended_file_version_id)` 唯一并分别使用带 tenant 的复合外键，冻结查重候选来源。
- `file_version` 增加 `revision_no`、`source_file_version_id`、`root_file_version_id`、`revision_reason`、可空 `revision_request_key_hash` 和 `revision_request_fingerprint`，按 §7.4 调整内容哈希约束；source/root 均使用带 tenant 的复合外键，`(tenant_id, source_file_version_id, revision_request_key_hash)` 在 key 非空时唯一。
- `rule_config` 增加可空 `config_fingerprint`、可空 `created_by`、非空 `backfilled_legacy`（默认 false）和 `unique(id, tenant_id)`；历史行回填 `backfilled_legacy=true` 且 fingerprint/created_by 保持 null，不能猜测 kind。F3 API 新建版本必须写非空 fingerprint/created_by 和 `backfilled_legacy=false`。
- `finding` 增加 `validation_run_id`、`rule_kind`、可空 `rule_config_id`、结构化 `evidence_json` 和确定性 finding 唯一约束；validation/rule 引用均使用带 tenant 的复合外键，保留现有 `rule_id/rule_version` 作为稳定展示与审计快照。
- 如 PostgreSQL JSONB 全历史发票号查询不能满足性能门禁，增加从 `normalized_json->>'invoice_no'` 派生的租户范围表达式索引；不得复制或改写原始证据列。
- 除按 §7.4 精确替换 `file_version(tenant_id, content_hash)` 唯一约束外，只允许增加列、表、索引和约束；替换后普通上传的 revision 1 幂等性必须有数据库部分唯一索引兜底。不得弱化 `row_result`、`sampling_audit` 或 `audit_log`。

升级前按受保护区要求备份相关 schema/table；测试库必须执行无派生数据的 `upgrade 0004 → downgrade 0003 → upgrade 0004`、`alembic check` 和受保护约束反向验证。一旦存在 revision > 1，downgrade 必须安全拒绝，不能删除派生数据或勉强恢复 0003 的内容哈希唯一约束；生产降级只能从 pre-0004 完整备份恢复到隔离库，替换原库仍需人工明确批准。

## 9. API 契约

### 9.1 规则配置

```text
GET /api/rules?rule_id={optional}&latest_only={bool}
PUT /api/rules
```

- GET 返回当前租户可见的不可变版本，默认 `latest_only=true`；稳定按 `rule_id, version` 排序。`rule_id` 最长 128，`latest_only` 仅接受布尔值。
- PUT 请求固定为 `{ "rule_id": string, "effective_from": "YYYY-MM-DD", "definition": RuleDefinition }`，不接受 `tenant_id`、客户端版本号、`is_active` 或创建人；服务端分配版本并从会话写创建人。
- PUT 响应固定包含 `id`、`rule_id`、`version`、`effective_from`、`config_fingerprint`、`definition`、`created_by`、`created_at` 和 `reused_existing`。
- 最新 canonical 指纹相同时返回 200 并复用；创建新版本返回 201。
- 非法 kind、未知字段、重复决策键、非法十进制、空允许集合或非法例外返回 422 和稳定领域错误码。

### 9.2 批次校验与结果

```text
POST /api/batches/{id}/validate
POST /api/batches/{id}/revisions
GET  /api/batches/{id}/validation
GET  /api/batches/{id}/findings?page={n}&page_size={n}&verdict={optional}
```

POST 不接受规则版本或租户 ID，由服务端首次选取并冻结。响应至少包含：

```json
{
  "file_version_id": "...",
  "mapping_version_id": "...",
  "ruleset_fingerprint": "...",
  "total_row_count": 100,
  "evaluated_row_count": 98,
  "passed_count": 70,
  "flagged_count": 20,
  "manual_review_count": 8,
  "parse_failed_count": 2,
  "reused_existing": false
}
```

计数不变量：`total_row_count = evaluated_row_count + parse_failed_count`，且 `evaluated_row_count = passed_count + flagged_count + manual_review_count`。

revision 请求固定为 `{ "reason": "ruleset_change" | "mapping_change" }`，并要求 8–128 字符的 `Idempotency-Key` 请求头；数据库保存 key 的 SHA-256 和 canonical 请求指纹。首次创建返回 201 和新批次 ID、`source_file_version_id`、`root_file_version_id`、`revision_no`、reason、解析状态及映射版本；同一 source + key + 请求指纹重试返回 200 并复用，同 key 不同 reason/请求指纹返回 409 `IDEMPOTENCY_KEY_REUSED`。

findings 的 `page` 默认 1 且最小 1，`page_size` 默认 50、范围 1–200，`verdict` 仅允许 `flagged|manual_review` 并按关联 `row_result.verdict` 过滤，不按 evidence outcome 过滤。结果使用稳定排序 `(row_no, rule_id, rule_version NULLS FIRST, kind, id)`，返回规则 outcome、reason code、结构化证据和原始行号；只有 `BATCH_READ` 用户可读取业务证据。

### 9.3 错误 shape 与状态码

所有错误继续使用 `{ "error": { "code": "...", "message": "..." } }`。

| HTTP | 稳定错误码 | 条件 |
|---:|---|---|
| 401 | `AUTH_REQUIRED` | 无有效会话 |
| 403 | `PERMISSION_DENIED` | 缺少所需权限 |
| 404 | `BATCH_NOT_FOUND` / `RULE_NOT_FOUND` | 不存在或属于其他租户 |
| 404 | `VALIDATION_NOT_FOUND` | 批次存在但尚未完成首次校验 |
| 409 | `BATCH_NOT_PARSED` | 批次尚未完成 F2 解析或全批解析失败 |
| 409 | `BATCH_VALIDATION_IN_PROGRESS` | 租户或批次锁冲突 |
| 409 | `BATCH_ALREADY_VALIDATED` | 已校验批次尝试换映射或原地重算 |
| 409 | `BATCH_USED_BY_VALIDATION` | 批次已被冻结为历史查重输入，禁止原地重解析 |
| 409 | `RULESET_INVALID` | 任一规则 kind 缺失或存在多个逻辑 rule ID，无法形成唯一快照 |
| 409 | `IDEMPOTENCY_KEY_REUSED` | 同一派生来源和 key 已绑定不同请求指纹 |
| 422 | `RULE_CONFIG_INVALID` | 强类型配置或领域约束失败 |
| 422 | `IDEMPOTENCY_KEY_INVALID` | 派生版本请求缺少或提供非法 Idempotency-Key |
| 500 | `BATCH_VALIDATE_INTERNAL_ERROR` | 未分类系统异常；业务事务已回滚 |

## 10. 权限、租户与审计

| 操作 | 权限 |
|---|---|
| 查看规则版本 | `CONFIG_READ` |
| 保存规则新版本 | `CONFIG_WRITE` |
| 触发校验 | `BATCH_IMPORT` |
| 创建派生文件版本 | `BATCH_IMPORT` |
| 查看校验摘要和 findings | `BATCH_READ` |

租户只从现有认证会话依赖注入，API 请求体和路径均不接受 `tenant_id`。跨租户资源统一表现为 404。

审计动作固定为：

- `rule_config.create`：记录规则配置 ID、`rule_id`、版本、kind、指纹和创建人。
- `batch.revision_create`：记录源/目标批次、reason、revision_no 和映射版本。
- `batch.validate`：记录批次、映射版本、规则集指纹和汇总计数。
- `batch.validate_failed`：记录批次、映射版本、规则集指纹和稳定错误分类。

审计不得写 definition 全文、例外值、normalized/raw JSON、员工、发票号、商户、抬头或异常文本。同版本幂等复用不重复追加成功审计。

## 11. Finding 证据结构

`evidence_json` 使用按 kind 判别的 Pydantic 模型，通用字段为：

- `schema_version=1`
- `outcome=flagged|unavailable|exempted`
- `rule_kind`、`reason_code`、`required_fields`
- `provenance`：按依赖字段保存 `mode`、`source_columns` 和 `inference_rule_id`；只包含该规则机械判定所需规范化值，不包含 `raw_json`。
- 可选 `exemption_id`。
- limit 专用 `amount`、`expense_type`、`currency`、`operator="gt"`、`max_amount`。
- invoice_type 专用 `expense_type`、`invoice_type`、`allowed_invoice_types_fingerprint`；具体集合从不可变规则版本读取，不在每行 evidence 重复复制。
- timeliness 专用 `expense_type`、`expense_date`、`submission_date`、`actual_calendar_days`、`max_calendar_days`。
- invoice_title 专用 `invoice_title`、`allowed_titles_fingerprint`；允许值从不可变规则版本读取，不复制到 evidence。
- invoice_duplicate 专用 `invoice_no`、`duplicate_of_file_version_id`、`duplicate_of_root_file_version_id` 和 `duplicate_of_row_no`。

通用不可判定 reason code 固定为 `MISSING_REQUIRED_FIELD`、`INFERRED_FIELD_NOT_ALLOWED`、`RULE_NOT_EFFECTIVE`、`RULE_DISABLED`；规则无决策表匹配时使用 §5 定义的专用 code。例外命中必须创建 `outcome=exempted` finding，reason code 为 `EXEMPTION_MATCHED`，但不提升行级 verdict；这样人工可区分“已机械通过”和“经配置豁免”。

`finding.rule_version` 将整数版本保存为无前导零的十进制字符串；`RULE_NOT_EFFECTIVE` 时为 null。F8 之前，F3 finding 的 `severity_impact` 和 `severity_confidence` 固定写 0，不在本阶段偷跑风险标定。

reasoning 文本由稳定模板从 evidence 渲染，不接收配置提供的任意模板，不调用模型。F4 展示时必须能从 evidence、规则版本和 `row_no` 还原判定链。

## 12. 验收场景

### 12.1 五类规则

- 每类覆盖通过、命中、等于边界、依赖字段缺失、inferred、`require_direct` 和例外匹配。
- 例外命中创建 exempted finding 但不提升 verdict；早于首个生效版本和显式 disabled 均转人工，零个/多个逻辑 rule ID 则整批拒绝。
- 限额覆盖精确费用类型/币种、十进制精度及未配置组合。
- 票种与抬头覆盖 NFKC 后精确匹配、集合去重和禁止模糊匹配。
- 时效覆盖 0 天、等于阈值、超阈值、闰日和负日差。
- 一行命中多条规则时只有一条 row_result，每条规则一条 finding，聚合为 flagged。

### 12.2 发票号查重

- 同批首条不标记、后续同号命中。
- 历史批次同号使新批次命中；历史结果不回写。
- null/空号转人工，不参与重复集合。
- 前导零或规范化后字符串不同不命中。
- 跨租户相同发票号互不影响。
- 稳定排序在重复运行和不同处理顺序下结果一致。
- 同一 root lineage 的派生 revision 不互相判重；其他 lineage 只使用快照时最高已解析 revision，并冻结对应 dependency。

### 12.3 快照、幂等与恢复

- 配置对象键或列表输入顺序变化不改变 canonical 指纹。
- 相同输入和规则集重复调用逐字段一致，`reused_existing=true`，不新增 finding、row_result 或审计。
- 新规则版本发布后，旧批次仍返回原指纹和结果。
- 同租户两个 validate 并发时一个执行，另一个稳定返回 409；无重复 compute 或 finding。
- validate 与 F2 reparse 并发时由同一租户锁串行，不能出现检查 dependency 后交错提交。
- 在行计算、finding 写入、row_result 写入、validation run 更新和审计前分别注入系统异常，整批业务结果均回滚；失败审计独立留痕且无 PII。
- 已完成校验后原地换映射/规则被拒绝；显式派生 file_version 可使用新映射或新规则，相同内容普通重传仍复用 revision 1。
- 被 `validation_dependency` 引用的历史批次不得重解析；派生 ruleset_change/mapping_change 分别验证复制与清空语义，且同一 Idempotency-Key 只创建一个 revision。
- ruleset_change 对未解析、failed 或零成功行来源稳定返回 409 且不创建 revision。
- 同一 Idempotency-Key 携带不同 reason 时稳定返回 409，不复用错误的派生 revision。
- 普通重复上传仍只返回 revision 1；派生 revision 不改变 F1 默认幂等行为。

### 12.4 API、权限与安全

- auditor/configurator/viewer 权限边界覆盖所有新接口；未登录、无权限和跨租户场景分别稳定返回 401/403/404。
- 配置 payload 拒绝未知字段、任意 JSON Logic、任意变量路径、代码和超长值。
- findings 证据足以机械复核，但审计 payload 不含 PII 或业务明细。
- 分页、稳定排序和汇总计数不变量均有集成测试。
- `validation_run`、依赖、派生来源和 rule_config/finding 引用均有跨租户复合外键反向测试。
- 0004 在无派生数据时可往返；存在 revision > 1 时 downgrade 安全拒绝且不删除数据。

## 13. 实施检查点

本文件是 CP-F3.0–F3.5 的唯一规范来源。以下实施契约只引用前文章节，不复制第二份 schema、API 或错误码定义；实现发现冲突时必须先修改本文件并在 §14 记录覆盖决定，再继续编码。

### 13.1 CP-F3.0 · 规格固化 ✅

**目标：** 固定 F3 的规则口径、版本快照、派生版本、verdict、证据、事务、API、权限和迁移边界。

**交付物：** 本规格 §1–§12，以及 CP-F3.1–F3.5 的实施契约。

**非目标：** 不创建迁移、模型、服务、API 或 UI，不预填后续检查点的测试数量和实现结果。

**前置条件：** F2 / CP-F2.5 完成；默认开发库经可恢复备份升级至 `0003` 且 `alembic check` 无漂移。

**测试场景：** 文档审查覆盖 §12 全部场景；核对既有 ORM、F1 内容哈希幂等、F2 重解析、RBAC 和审计约束是否可承载本设计。

**退出条件：** 规格无 P0/P1 未决项，CP-F3.1–F3.5 的输入输出可串联，`git diff --check` 通过。

### 13.2 CP-F3.1 · 持久化 schema 与 ORM

**目标：** 用新增迁移 `0004_f3_deterministic_validation.py` 落地 F3 持久化结构，为纯规则核心和编排服务提供稳定数据库契约。

**具体交付物：**

- 按 §8 新增 `validation_run`、`validation_dependency`，扩展 `file_version`、`rule_config` 和 `finding`；全部业务模型继承租户 mixin，并使用带 `tenant_id` 的复合外键。
- `file_version` 既有行回填 `revision_no=1`，source/root/reason/request key 为空；普通 revision 1 仍由部分唯一索引保证 `(tenant_id, content_hash)` 幂等，派生版本由三元唯一约束隔离。
- `rule_config` 增加 `config_fingerprint`、`created_by`、`backfilled_legacy` 和 `unique(id, tenant_id)`；历史行统一标记 `backfilled_legacy=true`，fingerprint/created_by 允许为空，禁止在迁移中猜测 kind。CP-F3.2 只允许新强类型版本进入规则快照。
- `finding` 的 F3 字段对既有非 F3 finding 保持可空；确定性 finding 使用 `validation_run_id IS NOT NULL` 的部分唯一索引实现 §7.3 语义。
- 同步 ORM、枚举、关系和迁移测试预期表/列/索引/约束；不修改 `0001`–`0003`。
- 完成 pre-0004 备份方案：完整库 custom archive 为恢复主件，schema-only 与受影响表 data-only 为定向证据；归档需通过 `pg_restore --list` 和 SHA-256 校验。

**非目标：** 不实现 Pydantic RuleDefinition、evaluator、指纹算法、业务服务、API 或前端；不读取或改写真实客户规则。

**前置条件：** 数据库位于 `0003 (head)`；现有 CP-F2.5 文档改动保留；应用处于停写窗口后才允许升级非测试库。

**测试场景：**

- 空库和含 legacy `rule_config`/file_version/finding 夹具的 `0003 → 0004` 升级。
- 无派生数据时 `upgrade 0004 → downgrade 0003 → upgrade 0004`；存在 revision > 1 时 downgrade 安全拒绝且不删除数据。
- revision 1 幂等、派生 revision 唯一、请求 key 唯一、确定性 finding 部分唯一和全部租户复合外键的正向/反向测试。
- 直查 `row_result`、`expense_row`、`sampling_audit` 唯一约束及 `audit_log` 追加写触发器未改变；`alembic check` 零漂移。

**退出条件：** 迁移目录测试、Ruff、格式检查和 strict mypy 通过；测试库往返与非测试库备份/升级证据完整；CP-F3.2 无需再决定存储字段或租户约束。

### 13.3 CP-F3.2 · 强类型规则与纯确定性核心

**目标：** 在 `backend/app/core/rules/` 实现无数据库、无网络、无当前时间依赖的强类型规则核心。

**具体交付物：**

- 按 §4 建立冻结、`extra="forbid"` 的五类 RuleDefinition 判别联合、例外模型、规模限制和配置规范化；legacy 未验证配置显式拒绝，不做猜测性转换。
- 实现 `{rule_id, effective_from, canonical definition}` 配置指纹，以及 §6 的 rule family manifest/ruleset 指纹；对象/集合输入顺序变化不得改变输出。
- 按 §5 实现五类 evaluator，并以统一 `RuleEvaluation` 返回 `passed|flagged|unavailable|exempted`。`passed` 的 evidence 固定为 null 且不持久化 finding；其余三种 outcome 必须返回对应强类型 evidence 和 reason code。行级 verdict 聚合遵循 §3。
- 实现 §6 的纯 `select_effective_rule_version` helper：输入一个逻辑 rule family 的不可变候选版本与 `expense_date`，返回选中版本或 `RULE_NOT_EFFECTIVE`；CP-F3.3 只负责从数据库装载候选、调用 helper 并冻结 manifest。
- 实现 §11 的五类 evidence 判别联合和稳定 reasoning renderer；不把大型允许集合复制到逐行 evidence。
- 所有金额使用 Decimal，日期使用 F2 ISO date，文本只消费 F2 规范化值；禁止隐式读取 `raw_json`。

**非目标：** 不访问 SQLAlchemy session，不保存规则版本或 finding，不实现批次快照、锁、审计、API、LangGraph 或 UI。

**前置条件：** CP-F3.1 ORM/schema 完成；F2 `NormalizedExpenseRecord` 与 provenance 结构保持当前契约。

**测试场景：**

- §12.1 五类规则的通过、命中、等于边界、缺字段、inferred/direct、disabled、未生效、例外和配置无匹配。
- 非指数十进制、闰日/负日差、精确票种/抬头、前导零发票号和禁止模糊匹配。
- canonical property tests：字典键、集合和配置输入顺序变化不改变指纹；生效日或任何有效字段变化必然改变指纹。
- 未知字段/运算符、重复决策键、非法规模、超长值、空集合和 legacy 配置稳定失败，不静默截断。
- 一行 0/1/多规则命中及 `flagged > manual_review > passed` 聚合；reasoning 可由 evidence 确定性重建。

**退出条件：** 纯逻辑测试覆盖全部 reason code 和边界，规则包定向覆盖率不低于 90%，Ruff、格式检查和 strict mypy 通过；CP-F3.3 只需编排这些纯函数。

### 13.4 CP-F3.3 · 快照、编排、幂等与审计

**目标：** 将 CP-F3.1 持久化结构与 CP-F3.2 纯规则核心组合成可恢复、可并发控制的租户级批次校验服务。

**具体交付物：**

- 实现规则版本追加保存、canonical 指纹幂等复用和租户内版本分配；legacy 配置不进入快照。
- 按 §6/§7 实现租户父行与批次 `FOR UPDATE NOWAIT` 锁、按费用发生日选择版本、首次成功快照、validation dependency 和整批原子事务。
- 将 `process_row_once` 作为 F3 第一个生产调用方；finding、row_result、validation run 和成功审计使用同一 session/事务，锁在 compute 前获取。
- 实现租户全历史发票号索引：排除当前 root lineage，每个其他 lineage 只选快照时最高已解析 revision，并按 root revision 1 稳定排序。
- 实现 ruleset_change/mapping_change 派生服务、Idempotency-Key 请求指纹和 F1 revision 1 兼容语义；不复制历史判定副作用。
- F2 parse/reparse 增加同一租户锁和 validation/dependency 防护；系统异常回滚业务事务后，以独立短事务追加无 PII `batch.validate_failed`。

**非目标：** 不暴露 HTTP 路由，不构建 React 页面，不接入 LangGraph、LLM、F4 条款或 F6 关联检测。

**前置条件：** CP-F3.1/3.2 全部门禁通过；数据库位于 `0004`；五个逻辑 rule family 可由测试夹具提供。

**测试场景：**

- §12.2 全部查重场景，以及 revision lineage 不自重复、最高 revision 选择和 dependency 冻结。
- 同批重复调用、规则发布后历史复用、同租户并发 validate、validate 与 F2 reparse 竞态、不同租户并行。
- 在规则求值、finding、row_result、validation run、dependency 和审计节点注入异常，验证整批回滚与失败审计独立留痕。
- 相同 source/key/request 指纹复用；同 key 不同请求稳定冲突；ruleset_change/mapping_change 的复制、清空和来源状态前置条件。
- 审计 payload 只含 ID、指纹、reason 和计数；跨租户规则、批次、dependency 和 duplicate 候选不可见。

**退出条件：** PostgreSQL 集成测试覆盖幂等、并发、中断/回滚和租户隔离；无重复 compute/finding/audit；受保护约束反向测试通过；服务层可直接被 CP-F3.4 路由调用。

### 13.5 CP-F3.4 · API、契约与桌面工作流

**目标：** 暴露 §9–§10 的类型化 API，并把规则配置与确定性校验接入现有桌面端工作流。

**具体交付物：**

- 新增 `/api/rules`、批次 validate/validation/findings/revisions 路由与 Pydantic 请求响应；租户只从会话注入，路由仅调用 CP-F3.3 服务。
- 按 §9.3 映射稳定 401/403/404/409/422/500 错误；GET validation 未执行、锁冲突、dependency 防护和 idempotency key 冲突均有明确响应。
- 导出 OpenAPI 并重新生成前端客户端；手写 API 类型不得复制生成 schema，外部输入继续在边界运行时校验。
- 将现有 `/rules` 占位页替换为 configurator 规则版本页：按五类展示最新/历史版本、强类型编辑表单、指纹/生效日/启停状态和追加新版本结果，不提供修改或删除旧版本。
- 扩展现有批次页增加“确定性校验”视图：展示规则集指纹、映射版本、四类计数、三态 verdict、分页 findings/evidence，并向有 `BATCH_IMPORT` 的用户提供 validate 和显式派生 revision 操作。
- auditor/configurator/viewer 的按钮和导航只由 permission 驱动；viewer 可读 validation/findings 但不能 validate/派生/保存规则。mutation 成功后失效批次、validation、findings、规则和列表缓存。

**非目标：** 不构建 F4 报告/条款引用、F5 复核队列或独立移动端；不在前端重新实现规则求值。

**前置条件：** CP-F3.3 服务契约稳定；现有认证/RBAC、TanStack Query、OpenAPI 客户端和批次四视图可复用。

**测试场景：**

- API 集成覆盖五个权限操作、三角色、未登录、跨租户、分页/稳定排序、全部领域错误码和无 PII 审计。
- 前端覆盖规则创建/幂等复用/校验失败、validate/复用、findings 分页、派生两种 reason、权限隐藏和 mutation 缓存刷新。
- 1440×1000 桌面 Chrome 视觉验证规则页与批次校验视图；长指纹、长规则名、空结果、loading/error 和 5000 行分页场景不溢出。
- OpenAPI 导出与客户端生成后再次运行无 diff，防止契约漂移。

**退出条件：** 后端 API 与前端定向测试通过；strict TypeScript、oxlint、Prettier 和生产构建通过；桌面视觉验证有记录；CP-F3.5 只负责完整回归和交付门禁。

### 13.6 CP-F3.5 · 契约与交付门禁

**目标：** 对 F3 全链路执行最终回归、性能、契约和安全门禁，并把项目状态推进到 F4 规格固化。

**具体交付物：**

- 执行后端 `pytest`、`ruff check .`、`ruff format --check .`、`mypy app scripts` 和迁移目录定向测试。
- 执行 OpenAPI 导出、前端 `gen:api`、`test`、`typecheck`、`lint`、`format:check` 和 `build`；生成物必须无二次漂移。
- 在测试库执行 `alembic check`，机械验证 `0004` 新约束和既有受保护约束；运行 pre-commit/secret 扫描，确认 `.env`、`tenants/`、`data/private/` 未入库。
- 使用 5000 行合成批次完成解析后执行五类校验，硬上限沿用全局要求的 15 分钟；记录耗时、查询数量、finding 数和规则集指纹，不把更严格的临时本机结果写成产品承诺。
- 重跑 CP-F3.4 的 1440×1000 桌面视觉场景，更新本规格 §14、`MEMORY.md` 和 `AGENTS.md`，记录实际测试数量、偏差和下一检查点。

**非目标：** 不为通过门禁而降低阈值、跳过失败测试、绕过 hooks，或提前实现 F4/F5。

**前置条件：** CP-F3.1–3.4 均完成并已在 §14 留下实际记录；默认开发库升级前已有可恢复 pre-0004 备份。

**测试场景：** §12 全量回归、F1/F2 幂等与重解析回归、认证/RBAC/租户隔离、并发/回滚、契约二次生成、生产构建、5000 行性能和桌面视觉。

**退出条件：** 所有命令零退出且无被忽略失败；性能不超过硬上限；契约生成物无漂移；F3 路线图标记完成，状态文件写入真实数量后方可进入 F4。

## 14. 实际落地记录

本节只追加已经完成的事实，不为未来检查点创建空结果或预测测试数量。每条记录包含日期、交付物、实现偏差/覆盖决定、验证命令与实际数量；若无偏差也要明确写“无”。

### CP-F3.0 实际落地记录（2026-07-28）

- `specs/004-phase2-f3-deterministic-validation.md` 成为 F3 CP-F3.0–F3.5 的唯一规范来源；未创建分散的检查点规格。
- 五类强类型规则、首次成功快照、派生 file revision、root lineage 查重、三态 verdict、证据、事务、API、权限、审计和迁移边界已固化。
- 规格经过两轮独立审查，派生 revision 自重复、查重依赖冻结、F2 reparse 锁、effective_from 指纹、租户复合外键、降级拒绝、例外顺序与 evidence 体积问题均已闭环；最终无 P0/P1 未决项。
- 实现偏差：相对旧 TechDesign 的开放式 JSON Logic，最终采用五类 Pydantic 判别联合 + 决策表；相对旧 `/api/tenants/{id}/rules`，最终采用会话注入租户的 `/api/rules`。
- 验证仅为文档与现有架构审查；未执行 CP-F3.1–3.5 的代码测试，也未预填测试数量。

### CP-F3.1 实际落地记录（2026-07-28）

- 新增且仅新增迁移 `0004_f3_deterministic_validation.py`：建立 `validation_run`、`validation_dependency`，扩展 `file_version`、`rule_config`、`finding`，同步 ORM、枚举、关系、复合租户外键、CHECK、唯一约束与部分唯一索引；`0001`–`0003` 未修改。
- legacy 回填已机械验证：既有 `file_version` 统一为 revision 1 且 lineage/request 字段为空；既有 `rule_config` 为 `backfilled_legacy=true` 且 fingerprint/creator 为空；既有非 F3 finding 的新增字段保持为空。隔离数据库已验证无派生数据的 `0003 → 0004 → 0003 → 0004`，以及存在 revision 2 时 downgrade 在任何 DDL 前拒绝、版本仍为 `0004` 且派生行保留。
- 关键决策：`validation_run.status` 固定为最小状态域 `in_progress|completed`，系统失败仍整批回滚而不持久化 failed run；manifest 列名固定为 `ruleset_manifest`；六项计数使用非负与两条恒等式 CHECK；所有 CP-F3.1 新增业务引用使用 `RESTRICT`，并新增 `app_user(id, tenant_id)` 冗余唯一约束，使 `created_by/triggered_by` 也由复合租户外键保护。
- 实现偏差/覆盖决定：无业务范围偏差；未添加可选的 `normalized_json->>'invoice_no'` 表达式索引，因为 §8 将其限定为性能门禁不足时才添加且本检查点没有不足证据。未实现 Pydantic/evaluator、服务、API、前端或 F4 功能。
- 默认开发库在停写状态下从 `0003` 升至 `0004`。升级前备份位于 gitignored 的 `data/private/backups/cp-f3.1/pre-0004-20260728-010204/`：完整库、public schema-only、受影响表 data-only 三份 custom archive 均通过 `pg_restore --list`，容器与本地副本 SHA-256 分别为 `a0ab28e50778295f049fbeba7fed25886c53bedb0ab0d3d121f9203ec1e65955`、`dd55c8c24a4b7262bf8b77245b7d02668ac2ad2b93f1cc70f1f5b495542b355c`、`4aa5a1935ae05f45ec28cee9bd08068affe4cbea2edc242340126b2ad174184a`。
- 验证结果：迁移目录 `27 passed`；后端全量 `149 passed, 1 skipped`（skip 为常驻待命 eval gate）；`ruff check .`、`ruff format --check .`、`python -m mypy app scripts`（67 个源文件）、测试库及默认开发库 `alembic check` 全部通过。Windows 上 `uv run alembic/mypy` 的 trampoline 路径解析失败，使用同一 uv 环境的 `uv run python -m alembic/mypy` 等价执行；无测试或门禁被跳过。
