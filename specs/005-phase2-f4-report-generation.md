# Spec 005 — Phase 2 F4 报告生成与制度条款引用

**状态：** CP-F4.3 Binding、引用核心与报告编排 ✅
**日期：** 2026-07-28
**前置检查点：** F3 / CP-F3.5 已完成
**下一实施检查点：** CP-F4.4 API、桌面工作流与 XLSX

---

## 1. 目标与范围

F4 把 F3 已冻结的确定性校验结果装配成可阅读、可导出、可追溯的批次报告，并建立制度文档从导入、条款切分、候选检索、人工确认到历史引用快照的完整证据链。

F4 的核心交付不是“生成一段看起来合理的文字”，而是以下四个可机械验证的不变量：

1. 每个可呈现的制度引用都绑定同租户、指定版本、在费用发生日有效的制度条款。
2. 引用文字是 PostgreSQL 中冻结条款原文的非空、连续、逐 Unicode code point 子串。
3. 逐字存在性不等于语义支持性；正式报告只接受配置员确认过的版本化 rule-policy binding。
4. 首次成功报告冻结 F3 validation、制度版本、binding、模板及导出语义；重放只读冻结快照，不重新检索或改写历史。

F4 同时补齐 PRD 所依赖但尚未实现的“制度文档已入库”能力：制度 family/version、源文件、确定性条款切分、Qdrant 索引、候选 binding 与发布状态。

### 1.1 明确不在 F4 范围内

- 不实现 F5 复核队列、confirmed/false_positive mutation、复核备注、分派或抽检操作。
- 不读取或展示 `correlation_finding`，不实现 F6 拆单、连号、频次或时空冲突。
- 不实现 F7 ReAct agent；F4 没有工具循环、agent step 或开放式推理。
- 不实现 F8 的 `severity_impact × severity_confidence` 二维分级、代价敏感阈值或风险评分。
- 不做 OCR。无文本层的扫描 PDF 必须显式失败并转人工，不猜测文字。
- 不做移动端、平板、PDF、CSV、邮件发送、定时报告或外部对象存储。
- 不修改 `0001`–`0004`、`docker-compose.yml`、Dockerfile 或 CI 基础设施。

### 1.2 对旧设计的覆盖决定

- 覆盖 TechDesign F4 的“模糊字符串匹配”：本规格只允许 §7 的严格逐字校验，禁止 fuzzy、trim 后匹配、Unicode/大小写/空白归一化、编辑距离或跨条款拼接。
- 覆盖 TechDesign F4 的“报告生成直接写 `finding`”：F3 finding 是冻结输入，F4 不回写 `finding.clause_id/quote/reasoning`，而是写独立 report snapshot。
- 覆盖 TechDesign 的“语义检索结果可直接成为制度依据”：检索只产生候选；正式依据必须来自版本化、人工确认且逐字校验通过的 `rule_policy_binding`。
- 覆盖旧的开放式云模型设想：制度原文、检索 query/candidate/result 不得发送云 LLM。F4 MVP 的报告装配不调用生成式云模型。
- F4 MVP 导出固定为 XLSX-only；PDF/CSV 留到后续打磨。

---

## 2. 术语与不变量

| 术语 | 定义 |
|---|---|
| policy family | 同一制度跨版本的稳定身份；展示标题不是身份 |
| policy document | 某一 family 的一个不可变内容版本及其半开生效区间 |
| policy clause | 可被完整展示和逐字引用校验的最小原文单元 |
| retrieval chunk | 仅用于 embedding/rerank 的条款内片段；不得替代 clause 原文 |
| rule-policy binding | 配置员确认某个 F3 `rule_config` 版本由某条制度条款的指定原文切片支持 |
| report run | 一个 file revision 的首次成功、不可变报告快照 |
| report item | 报告中的一条 finding 证据；同一行多个 finding 不折叠 |
| citation snapshot | 报告生成时复制的制度 family/document/clause/quote/offset/hash 展示证据 |
| attention group | F4 对 F3 verdict 的单轴操作分组，不是 F8 风险评分 |

全局不变量：

1. 所有业务数据都由 tenant filter 与复合租户外键双重隔离；跨租户一律 404/fail closed。
2. 制度区间统一为 `[effective_date, expiry_date)`；`expiry_date IS NULL` 表示无穷远。
3. 同一 tenant + family 在任一日期至多存在一个 published 版本；不同 family 可同时生效。
4. 缺少费用发生日时禁止回退“最新制度”，必须 `POLICY_EXPENSE_DATE_UNAVAILABLE`。
5. published document、clause、binding、completed report 及 completed export 不可原地修改或删除。
6. PostgreSQL 是制度原文、租户、版本、binding 与报告证据的唯一真源；Qdrant payload/text 不是引用真源。
7. 历史报告与 XLSX 只读 snapshot，不 live join 当前 Qdrant、当前 binding 或最新制度。
8. F3 `source_verdict` 永不因检索/引用失败而被改写；引用状态单独降级并显式标记需人工补齐引用。

---

## 3. 端到端用户与数据流程

### 3.1 制度发布

1. configurator 创建 policy family，稳定 `stable_key` 一经创建不可修改。
2. 上传 `.pdf`、`.docx` 或 UTF-8 `.txt`，提交 version 与生效日期；大小、页数、字符数和条款数上限来自 Settings。
3. 源文件先落 `data/private/` 或部署私有卷；PG 只存 storage key、MIME、size 与 SHA-256，不存绝对主机路径。
4. 本地解析器抽取原文并确定性切分 clause；扫描 PDF、加密文件、损坏文件或无法可靠识别条款边界时进入失败清单。
5. configurator 预览 clause 编号、层级、原文与 source locator；不得在 UI 内改写原文，修正必须上传新文件版本。
6. 发布事务核对 family 区间不重叠、内容哈希、切分 fingerprint 与创建者，然后写 index outbox。
7. 本地 embedding worker 以稳定 point ID 幂等 upsert Qdrant；全部 chunk 验证成功后 document 才变为 `published`。

### 3.2 条款 binding

1. configurator 选择一个 F3 `rule_config` 版本。
2. 系统从该 rule definition/evidence 模板生成无 PII 的稳定检索 query。
3. Qdrant 强制 tenant、有效日期与 active index generation filter，本地 reranker 稳定排序候选；published 状态最终以 PostgreSQL 二次校验为准。
4. 服务端返回候选 clause 及完整 PG 原文；配置员选择 clause 并框选非空连续 quote。
5. 服务端以 §7 逐字校验，保存 quote offsets/hash 和创建者；binding 追加写，不修改旧版本。
6. 检索候选只是配置辅助。未经确认的候选不得进入 report、XLSX、finding、audit payload 或日志。

### 3.3 报告生成

1. auditor/configurator 对已完成 F3 validation 的 file revision 发起 report。
2. 服务按既有锁序获取 `Tenant FOR UPDATE NOWAIT → FileVersion FOR UPDATE NOWAIT`。
3. 相同 report input fingerprint 已 completed 时直接返回；同 key 异请求返回 409。
4. 冻结 validation、ruleset、mapping、policy/binding/template manifest。
5. 对每个 F3 finding 按 `rule_config_id + expense_date` 解析 published binding；PG 二次验证 tenant、区间、clause/document/hash 与 quote。
6. 有 1–3 个完整有效 binding 时创建有序 verified citation snapshot；任一预期 binding 缺失/歧义/过期/hash 漂移时保留 finding、verdict 与原 attention group，整组 citation 标为 unavailable 且不展示部分引用。
7. 解析失败行单列；纯 passed 行只进入 summary count，不伪造 finding 或制度引用；F3 已持久化的 exempted finding 作为 informational outcome 的 report item 保留，其 attention group 仍按该行 verdict 映射。
8. report/item/parse-error/citation/snapshot/count/completed audit 在同一业务事务内提交；任一失败全部回滚，历史读取不再依赖 Qdrant。

### 3.4 XLSX 导出

1. 有 `REPORT_EXPORT` 的用户从 completed report 发起 XLSX 导出。
2. 生成器只读取 report snapshot 与引用的源行证据，不重新检索或重算报告。
3. 工作簿写临时文件，完成校验后原子 rename 到私有卷；PG 保存 artifact storage key 与 SHA-256。
4. 相同 report + template version 重试复用同一 export；生成成功审计最多一次，每次实际下载另记一次下载审计。

---

## 4. 制度 family、版本与发布状态

### 4.1 Policy family

`policy_family` 至少包含：

- `id`, `tenant_id`
- `stable_key`：1–128 字符稳定机器标识；tenant 内唯一
- `display_name`：展示名，可在新 family 版本策略外单独审计修改，但不参与身份
- `created_by`, `created_at`

必须有 `unique(tenant_id, stable_key)`、`unique(id, tenant_id)` 与 tenant 复合创建人 FK。

### 4.2 Document 版本

新增 `policy_source_blob`，以 `unique(tenant_id, content_sha256)` 对私有源文件去重，保存 storage key、MIME、size 与 hash；不同 family/version 可以引用同一 blob，但不能据此合并制度版本。

扩展既有 `policy_document`：

- `family_id`
- `source_blob_id`, `content_sha256`, `mime_type`, `size_bytes`
- `extracted_text_sha256`, `parser_version`, `chunker_version`
- `status = legacy_unpublished|draft|indexing|published|failed`
- `created_by`, `published_by`, `published_at`
- `failure_code`：稳定枚举，不保存异常全文

约束：

- 保留既有 `unique(tenant_id, title, version)`，不得放宽。
- 新增 `unique(tenant_id, family_id, version)`；内容去重由 `policy_source_blob` 承担，document 不设 tenant-wide content hash unique。
- 新增 `unique(id, tenant_id)`，所有下游使用复合租户 FK。
- CHECK `expiry_date IS NULL OR expiry_date > effective_date`。
- `0005` 安装并验证 `btree_gist`，以 GiST exclusion constraint 强制 published 行的 tenant/family 相等且 `daterange(effective_date, COALESCE(expiry_date, 'infinity'::date), '[)')` 不相交；同租户 family 锁只补充并发控制，不能替代数据库不变量。
- family/blob/hash/parser/chunker 等新增字段只允许 `legacy_unpublished` 行为空；新建行与其他状态必须由 CHECK 保证完整。legacy 行不得索引或绑定。
- published 内容、family、version、effective_date、hash 与 clause 集合不可修改。唯一允许的元数据收口是：发布同 family 的后继版本时，可在同一租户锁/事务中把前一 open-ended 版本的 `expiry_date` 从 NULL 单向设置为后继 `effective_date`，并追加审计；不得再次改动、延后、回退或造成历史区间重叠。

若前一版本已经有非空 expiry，后继版本必须显式满足不重叠约束，普通 API 不猜测改写。已完成报告保存 document 区间快照，因此上述一次性 open-ended 收口不会改变历史呈现；若要让既有批次应用后继版本，仍须走 `policy_change` 派生 revision。

### 4.3 Clause 与 retrieval chunk

扩展 `policy_clause`：

- `ordinal`：文档内稳定顺序
- `text_sha256`
- `source_locator_json`：页码/段落/字符范围等可恢复定位，不保存解析器临时路径
- `source_start`, `source_end`（若抽取器能提供稳定全文 offset）

条款原文 `text` 是 citation atom，published 后不可改。document 提供 `unique(id, tenant_id, family_id)`，clause 提供 `unique(id, tenant_id, document_id)`；所有下游用复合 FK 闭合 tenant/family/document/clause 关系，消除独立 ID 漂移风险。

新增 `policy_chunk`：

- `id`, `tenant_id`, `document_id`, `clause_id`
- `chunk_no`, `start_offset`, `end_offset`, `text`, `text_sha256`
- `chunker_version`

chunk 必须是同一 clause.text 的连续切片；不得跨 clause 拼接，不得为 embedding 改写文字。完整 clause 即使超出模型窗口也保留，只有 chunk 被进一步切分。

### 4.4 条款切分失败语义

- 可识别的显式条款编号按确定性规则切分。
- 无法可靠识别编号时，不生成猜测性 clause_no；document 保持 draft/failed 并返回稳定错误。
- 重复 clause_no、空 clause、offset 越界、文本 hash 不一致、条款数超限均 fail closed。
- 解析器与 chunker 必须版本化；版本变化会改变 document/index fingerprint，走独立升级与评测门禁。

---

## 5. Qdrant 索引、检索与候选 binding

### 5.1 VectorStore 抽象

`backend/app/core/retrieval/` 定义严格类型 `VectorStore` Protocol；业务服务不得直接依赖 Qdrant client。至少提供：

- `upsert_chunks(tenant_id, generation, chunks)`
- `search_candidates(tenant_id, generation, expense_date, query, top_k)`
- `verify_generation(tenant_id, generation)`

所有方法的 `tenant_id`、generation 与时间过滤参数必填，禁止默认空值或“全局搜索”。

### 5.2 Index generation 与 outbox

新增 `policy_index_generation`，冻结：

- collection/alias、generation number、vector size、distance
- embedding model family/id/revision/fingerprint
- rerank model family/id/revision/fingerprint
- parser/chunker version
- `source_manifest_fingerprint`, `expected_point_count`, `completed_point_count`
- status `building|active|failed|retired`

新增 `policy_document_index(document_id, index_generation_id, status, expected_point_count, completed_point_count, manifest_fingerprint)`，记录每份文档在每代索引的完整性。每个 tenant 至多一个 active generation。模型升级建立 new generation，并在 building 开始时冻结 published chunk manifest；building 期间新发布的文档只向当时 active generation 提交增量 job，不得静默加入已冻结的 building manifest。切换前在 tenant 锁内把当前 published manifest 与 frozen manifest 比较；若有 delta，必须显式创建新的 manifest revision 与 delta jobs、完成 count/hash/eval 后重新检查，不能带缺口切换。全量索引与最终 manifest 一致后才原子切换 active；不得在原 generation 中静默混用不同维度或模型。

新增 `policy_index_job` 作为 transactional outbox：

- unique `(chunk_id, index_generation_id, operation)`
- point ID 从 chunk UUID 稳定派生，重试 upsert 同一点
- 状态 `pending|running|completed|failed`、attempt count/limit、`available_at`、`lease_owner`、`lease_token`、`lease_expires_at` 与稳定 `last_failure_code`
- 不保存制度原文、异常全文、token 或密钥

worker 以 `FOR UPDATE SKIP LOCKED` 领取可用 job；租约过期的 running job 可安全回收，达到 retry limit 才进入 terminal failed，人工重试必须审计。Qdrant point 可在 document `indexing` 时写入，但服务层不得暴露该文档；只有全部 job 完成、point count/payload/hash 核对一致后，PG 才在事务内将 document 原子置为 `published`。可重试错误保持 `indexing`，PG commit 成功但 Qdrant 失败时重试 outbox；不得静默少向量。

Qdrant 官方文档确认 UUID point ID 与同 ID upsert 的幂等覆盖语义；F4 使用稳定 UUID，而不是让 client 生成随机 ID。Qdrant 的日期 range 与 payload index 只用于候选过滤，返回后仍必须 PG 二次校验。参考：[Qdrant Points](https://qdrant.tech/documentation/concepts/points/)、[Qdrant Filtering](https://qdrant.tech/documentation/search/filtering/)。

### 5.3 Payload 与时间过滤

payload 至少包含：

- `tenant_id`, `family_id`, `document_id`, `clause_id`, `chunk_id`
- `effective_day`, `expiry_day_exclusive`
- `document_content_sha256`, `clause_text_sha256`, `chunk_text_sha256`
- `index_generation`, `embedding_model_fingerprint`, `chunker_version`

日期编码固定为自 Unix epoch 起的整数 epoch-day，并写入 generation manifest；禁止混用 ISO 字符串或时间戳。PG NULL expiry 在 payload 中写 `9999-12-31` 对应的固定最大 epoch-day sentinel，过滤统一为：

```text
tenant_id == :tenant_id
AND index_generation == :active_generation
AND effective_day <= :expense_day
AND expiry_day_exclusive > :expense_day
```

### 5.4 检索 query 与稳定排序

binding 建议 query 只由 rule kind、reason code、费用类型、规则阈值语义和稳定模板生成；不读取 `raw_json`，不含员工、供应商、发票号、抬头或自由文本 PII。

候选顺序固定为：

1. rerank score 降序
2. vector score 降序
3. policy family stable_key
4. document effective_date
5. clause ordinal
6. chunk_no / chunk_id

`top_k`、cutoff 与最大候选数来自强类型配置并进入 fingerprint。分数低于 cutoff 只返回“无可靠候选”，不回退最新制度或模型记忆。

### 5.5 双重校验

Qdrant 返回后，服务端必须回 PG 验证：

- tenant/document/clause/chunk 关系闭包
- document `published`
- expense_date 落在 document 区间
- family 当日无重叠歧义
- generation/model/chunker/content hash 与 payload 一致
- chunk 是 clause 原文连续切片

任一失败都返回稳定 unavailable code，不得呈现候选。

---

## 6. Rule-policy binding

### 6.1 为什么必须有 binding

逐字校验只能证明 quote 存在于原文，不能证明条款语义支持某条规则。仅凭向量相似度或 rerank 分数直接把条款称为“判定依据”会制造可审计性假象。

因此 F4 MVP 的正式引用必须由 configurator 确认并保存版本化 binding。语义检索只降低配置成本，不替代责任主体确认。

### 6.2 Binding 结构

`rule_policy_binding` 至少包含：

- `id`, `tenant_id`
- `rule_config_id`
- `policy_family_id`, `policy_document_id`, `policy_clause_id`
- `quote_start`, `quote_end`, `quote`, `clause_text_sha256`
- `citation_order`：1–3
- `binding_fingerprint`
- `created_by`, `created_at`

约束：

- 所有引用使用复合 tenant FK 与 `ON DELETE RESTRICT`。
- unique `(tenant_id, binding_fingerprint)`；相同 canonical 内容重试复用。
- unique `(rule_config_id, policy_document_id, citation_order)`，且 `citation_order CHECK (citation_order BETWEEN 1 AND 3)`。
- 一个 rule_config 在给定费用日期可适用 1–3 个条款；保存时在 tenant 锁内校验数量、order 连续与无歧义，并用约束及并发反向测试保证闭包。
- 绑定对象必须是 published document；quote 必须先通过 §7。
- binding 追加写且不可修改/删除；新 rule_config 或新 policy version 需要新 binding。
- binding 本身不带独立生效日；报告使用 policy document 区间与 expense_date 判定是否适用。

### 6.3 缺 binding 与 policy change

- 没有适用 binding 时保留 F3 source verdict/reasoning 与既有 attention group，citation status=`unavailable`，reason=`POLICY_BINDING_NOT_FOUND`，并设置 `requires_manual_citation=true`。
- 多个同 order binding 同时适用视为歧义，fail closed。
- 发布新 policy version 不修改历史报告，也不自动把旧 binding 指向新条款。
- 已有 completed report 的批次要应用新 policy/binding，必须创建 `policy_change` 派生 file revision；不得原地 regenerate 覆盖历史。

---

## 7. 严格逐字引用校验

### 7.1 唯一合法判定

给定同租户、同 snapshot、claimed `policy_clause_id` 的 PostgreSQL `policy_clause.text`：

```python
verified = (
    len(quote) > 0
    and any(not character.isspace() for character in quote)
    and 0 <= start < end <= len(clause_text)
    and quote == clause_text[start:end]
)
```

`start`/`end` 是必填的 Unicode code point offset，`end` 为 exclusive；不接受仅提交 quote 后由服务端猜测第一次出现位置。

比较按 Python Unicode code point、区分大小写、连续子串执行。

### 7.2 明确禁止

- 禁止 `.strip()` 后匹配；纯空白 quote 也必须拒绝。
- 禁止 NFC/NFKC、casefold、全半角转换、空白/换行折叠、标点替换。
- 禁止编辑距离、模糊匹配、正则近似、语义匹配后声称逐字通过。
- 禁止跨 clause/document 拼接 quote。
- 禁止用 Qdrant payload text、LLM memory、网页内容或 source filename 作为比对真源。

### 7.3 未验证候选的处理

候选 quote 必然会出现在受控 binding 请求体与校验函数的瞬时内存中，但在验证前不得进入任何持久化或可观测面：

- 验证失败后不得写 `finding`、report、binding、audit payload、trace、checkpoint、cache 或日志。
- API 响应、UI 状态与 XLSX 不得把未验证选择标记或回显为 quote；受控配置 UI 仍可读取 PG clause 原文供框选。
- 只允许记录稳定 failure code、tenant-safe ID、attempt ID 与内容安全哈希。
- 只有验证通过后，quote/offset/hash 才可在同一事务中写入 binding 或 citation snapshot。
- binding endpoint 必须关闭请求体/access body/trace body 采集；Pydantic 4xx 错误不得回显 `input`、quote 或 clause text。

### 7.4 忠实性与正确性的边界

逐字校验只证明“原文存在”；configurator-confirmed binding 负责“该原文被确认支持此规则”。报告必须同时携带 `verification_status=verified_exact` 与 binding ID，不得把向量分数包装成制度确认。

---

## 8. 报告语义与关注分组

### 8.1 输入边界

F4 只消费：

- completed `validation_run`
- F3 `row_result`、deterministic `finding`、`expense_row` 与 parse_error
- 冻结的 rule_config/ruleset/mapping 指纹
- published policy/binding

F4 不重新求值 F3 规则，不回写 F3 finding，不读取 correlation finding。

### 8.2 Attention group

F4 定义单轴操作分组，不读取或计算 `severity_impact/severity_confidence`。`report_item` 同时保存 finding-level `source_outcome` 与 row-level `source_verdict`，同一行的全部 item 按 row verdict 进入同一组：

| row-level `source_verdict` / row 状态 | `attention_group` | 说明 |
|---|---|---|
| `flagged` | `high_attention` | 该行至少一个确定性规则命中；同一行其他 unavailable/exempted item 不拆组 |
| `manual_review` | `manual_attention` | 该行只有 F3 缺字段/无法自动判定等 unavailable finding |
| `passed` | `cleared` | 可含 exempted informational item；无 finding 时仅汇总、不创建 report item |
| parse error / 未处理 | `manual_attention` | 单列 parse error，不猜违规或通过 |

若 flagged row 的任一 finding 缺 verified binding，`source_verdict` 仍是 flagged、其全部 item 仍属 `high_attention`；缺引用的 item 另有 `citation_status=unavailable` 与 `requires_manual_citation=true`，不得把 flagged 降级或移组。

所有 report item（包括 unavailable/exempted outcome）都必须尝试附制度依据，`citation_status` 只允许 `verified|unavailable`，与 attention group 正交：只有全部 1–3 个有序 binding 都验证通过才是 `verified`；任一预期 citation 失败则整组为 `unavailable` 且不展示部分引用。纯 passed 无 finding 行和 parse error 不是 report item，因此没有 citation status。

### 8.3 行数与 finding 数

报告必须同时区分 row count 与 finding count：

```text
stored_row_count = validated_row_count + parse_error_row_count
validated_row_count = flagged_row_count + manual_review_row_count + passed_row_count
report_item_count = deterministic finding count
manual_attention_row_count = manual_review_row_count + parse_error_row_count
```

同一 row 的多个 finding（包括 exempted）分别形成 item，不能折叠丢证据。纯 passed 行不制造 finding，只在 summary 与可选 cleared row index 中计数。

### 8.4 稳定排序

report item 排序：

1. `high_attention`
2. `manual_attention`
3. `cleared`
4. `row_no`
5. `rule_id` ASC
6. `rule_version` ASC NULLS FIRST
7. `finding_id`

parse error 按 `row_no`、稳定 error code 排序。所有 API 分页与 XLSX 共用同一排序 helper。

### 8.5 Report fingerprint

fingerprint 至少绑定：

- tenant、file_version/revision、validation_run ID
- source content hash、mapping version、ruleset fingerprint
- ordered binding/policy manifest 与文本 hash
- report schema/template version
- attention mapping version

对象键和集合输入顺序变化不得改变 fingerprint；任何影响报告快照的有效字段变化必须改变 fingerprint。索引代际、embedding/rerank 与 chunker provenance 属于候选配置/binding 历史，不进入 report identity；仅模型代际变化不能让同一 file revision 产生第二份 report。

---

## 9. 持久化计划（CP-F4.1 输入）

CP-F4.1 只新增 `0005_f4_policy_reports.py`，不得修改 `0001`–`0004`。

### 9.1 新增/扩展实体

| 实体 | 核心职责 |
|---|---|
| `policy_family` | 稳定制度身份 |
| `policy_source_blob` | 私有源文件内容寻址与租户内去重 |
| `policy_document`（扩展） | 内容版本、区间、源文件 hash、解析/发布状态 |
| `policy_clause`（扩展） | citation atom、hash、ordinal、source locator、租户复合 FK |
| `policy_chunk` | clause 内检索切片 |
| `policy_index_generation` | 模型/collection/chunker 代际 |
| `policy_document_index` | 文档在每代索引中的 manifest/count/hash 完整性 |
| `policy_index_job` | PG→Qdrant transactional outbox |
| `rule_policy_binding` | 人工确认、逐字验证的规则-条款版本绑定 |
| `report_run` | file revision 的报告状态与冻结 manifest |
| `report_request` | report 生成请求的追加式幂等 key→请求/report 映射；允许 completed report 绑定后续新 key |
| `report_item` | finding 级快照 |
| `report_parse_error` | 未进入确定性校验行的解析错误快照 |
| `report_citation` | verified citation 展示快照 |
| `report_export` | XLSX artifact 幂等记录 |

### 9.2 Report run

`report_run` 至少包含：

- `id`, `tenant_id`, `file_version_id`, `validation_run_id`
- `status = in_progress|completed`
- `report_fingerprint`, `template_version`, `attention_mapping_version`
- `policy_manifest`, `binding_manifest`
- row/finding/citation/unavailable counts
- `created_by`, `created_at`, `completed_at`

约束：

- unique `(file_version_id)`：一个 file revision 只有一份首次成功报告。
- unique `(tenant_id, report_fingerprint)` 与 `unique(id, tenant_id)`。
- file/validation/creator 全部使用复合 tenant FK + `RESTRICT`。
- `in_progress` 只允许存在于尚未提交的业务事务中；completed 后 immutable，不持久化 failed/partial report。

`report_request` 至少包含 tenant、file version、report run、`idempotency_key_hash`、`request_fingerprint` 与创建时间；使用 `unique(tenant_id, idempotency_key_hash)` 和复合 tenant FK。它只保存 hash/ID，不保存请求体、原始行、quote 或制度原文，且与 report 创建/复用处于同一事务。该表追加写且不可修改/删除，用于同时满足“completed report + 新 key 复用”与“同 key + 异请求永久冲突”。

### 9.3 Report item 与 citation

`report_item` 快照：

- report/finding/file/row/rule IDs 与版本
- source outcome、row-level source verdict、reason code、reasoning snapshot、evidence snapshot
- attention group、citation status、requires_manual_citation
- source evidence `{file_version_id, content_hash, row_no}`
- unique `(report_run_id, finding_id)`

`report_citation` 快照：

- report_item、binding、family/document/clause IDs
- stable key、title、version、effective/expiry、clause_no/hierarchy 展示值
- clause text snapshot + SHA-256
- quote、start/end、quote SHA-256、`verified_exact`
- citation order

`report_parse_error` 快照保存 report/file/row IDs、稳定 error code、列名与安全 message，不猜测 normalized value；unique `(report_run_id, row_no, error_code, column_name)`。解析错误通过独立列表/API/XLSX 呈现，不伪装为已经验证的 report item。

历史引用使用 `RESTRICT` 与 snapshot 双保险。任何删除 published policy/binding/report 的操作均不在普通服务中提供。

### 9.4 Export

`report_export` 至少包含：

- `report_run_id`, `format=xlsx`, `template_version`
- `status=in_progress|completed|failed`
- `artifact_storage_key`, `artifact_sha256`, `size_bytes`
- `created_by`, `created_at`, `completed_at`, `failure_code`
- unique `(report_run_id, format, template_version)`

### 9.5 迁移安全

- pre-0005 非测试库必须先做可恢复 full/schema/affected-data 备份并验证 `pg_restore --list` 与 SHA-256。
- legacy policy rows 不得猜 family、hash、status 或 clause offset；统一显式标为 `legacy_unpublished`，需 configurator 重新发布后才能进入检索。
- 迁移先 preflight 既有 policy document/clause/finding 的 tenant 关系，发现错配立即停止；`0005` 以复合 tenant FK + `RESTRICT` 替换既有 policy_clause→document 的单列 CASCADE FK 与 finding→clause 的单列 SET NULL FK，不修改历史迁移文件。
- 已有 finding 的 `clause_id/quote/reasoning` 保留原值，不回填、不删除、不作为 F4 snapshot 真源。
- 机械反向验证 `row_result`、`sampling_audit`、`audit_log`、F3 unique/FK/check/append-only 未改变。
- downgrade 在存在 F4 published policy、binding、report 或 export 时必须在任何 DDL 前拒绝，不删除交付证据。
- CP-F4.3 不修改既有 `0001`–`0005`；新增 `0006` 仅扩展 `file_version.revision_reason` 的 `policy_change` 值域并新增追加式 `report_request`。非测试库升级前必须完成可恢复 full/schema/affected-data 备份、`pg_restore --list` 与 SHA-256 校验；downgrade 在存在 `policy_change` revision 或任一 `report_request` 时于任何 DDL 前拒绝。

---

## 10. 并发、幂等与恢复

### 10.1 锁序

制度发布、binding 保存、report 生成与 F2/F3 共享：

```text
Tenant FOR UPDATE NOWAIT
  → FileVersion FOR UPDATE NOWAIT（仅批次相关操作）
```

禁止反序获取。不同 tenant 可并行；同 tenant 冲突稳定返回 409。

### 10.2 Report 原子事务

1. 锁内校验 F3 completed、policy/binding 解析输入与 idempotency key；缺 binding 是可完成的 citation unavailable，不是整批生成失败。
2. 若 completed report 已存在则直接返回；否则在同一未提交事务中创建 `in_progress` report_run 并冻结 input fingerprint/manifest。
3. 只从 PostgreSQL 确认过的 binding 装配全部 report item、parse error、citation snapshot 与 count；Qdrant 不在报告关键路径。
4. 全部不变量验证后将 report 置为 completed，并在同一事务追加唯一一次 `batch.report_generate`。
5. 任一步失败都回滚 report/item/parse-error/citation/count/success audit 的全部写入；再用独立短事务追加无 PII `batch.report_failed`。

重试从冻结的 F3/PG 输入重新执行纯装配，不恢复或跳过部分 item。completed 重放不得访问 Qdrant/模型/时钟，不得增加 item/citation/audit/export；business side effect 最多一次。

### 10.3 Idempotency-Key

生成请求要求 8–128 字符 key；仅保存 SHA-256。请求 fingerprint 绑定 file revision、validation run、template version 与 report input fingerprint：

- 同 key + 同请求：200 复用。
- 同 key + 不同请求：409 `IDEMPOTENCY_KEY_REUSED`。
- completed report + 新 key：200 复用，不创建新 report。

每个经校验的 key 都在 `report_request` 中追加记录。首次生成时 request 与 report snapshot 同事务提交；completed report 使用新 key 重放时，只追加 key→既有 report 的映射，不新增 report/item/citation/成功审计，也不访问 Qdrant、模型或当前 binding。若 key 已存在，必须先比较冻结的 `request_fingerprint`；不一致即 409，不能因为目标批次已有 completed report 而绕过冲突检查。

若 file revision 已有 completed report，服务必须先按主键读取冻结 report，再以其 `report_fingerprint` 计算幂等响应；不得读取当前 binding/index/model 来重新推导 identity。新 policy/binding 只能通过 §10.4 的派生 revision 生效。

### 10.4 Policy change

F4 扩展 revision reason `policy_change`：复制 source revision 的原始/解析快照，不复制 F3/报告副作用。派生 revision 必须重新完成 F3 validation 后生成新 report；旧 report 不变。

该值域扩展由 CP-F4.3 的新增 `0006` 落地；禁止回写 `0004` 或 `0005`。`policy_change` 与 `ruleset_change` 一样要求来源已成功解析，但其 request fingerprint 使用独立 reason，不能与其他派生原因复用同一幂等请求。

### 10.5 Export 幂等

- 路径由 tenant/report/template/version 派生，不使用用户 filename 直接拼路径。
- 临时文件与最终路径必须位于已验证私有根目录内；原子 rename 前回读校验工作簿。
- completed artifact 存在且 hash 匹配时复用；缺失/hash 漂移时显式失败，不静默返回不同内容。
- export create 的 Idempotency-Key 同样只存 hash：同 key/同请求复用，同 key/异请求 409；artifact 成功审计最多一次，download endpoint 每次成功下载均追加一次只含 ID/hash 的审计。

---

## 11. API 契约

所有 tenant 从 session 注入；路径中不接收 tenant ID。错误统一 `{error:{code,message}}`。

### 11.1 Policy family/document/binding

| Method | Path | Permission | 语义 |
|---|---|---|---|
| GET | `/api/policies/families` | `CONFIG_READ` | 列出 family 与版本状态 |
| POST | `/api/policies/families` | `CONFIG_WRITE` | 创建 stable family |
| POST | `/api/policies/documents` | `CONFIG_WRITE` | multipart 上传 draft |
| GET | `/api/policies/documents/{id}` | `CONFIG_READ` | 文档、条款、索引状态 |
| POST | `/api/policies/documents/{id}/publish` | `CONFIG_WRITE` | 发布并触发幂等索引 |
| GET | `/api/rules/{rule_config_id}/policy-candidates?expense_date=YYYY-MM-DD` | `CONFIG_READ` | `expense_date` 必填的时间过滤候选，仅配置辅助 |
| POST | `/api/rules/{rule_config_id}/policy-bindings` | `CONFIG_WRITE` | 保存 verified binding |
| GET | `/api/rules/{rule_config_id}/policy-bindings` | `CONFIG_READ` | 读取 binding 历史 |

上传限制错误必须区分 MIME、大小、加密/扫描 PDF、解析、条款边界、区间重叠与索引失败。

### 11.2 Report

| Method | Path | Permission | 语义 |
|---|---|---|---|
| POST | `/api/batches/{file_version_id}/reports` | `BATCH_IMPORT` | 原子生成/复用报告；要求 Idempotency-Key |
| GET | `/api/batches/{file_version_id}/report` | `REPORT_READ` | 获取该 revision 报告摘要 |
| GET | `/api/reports/{report_id}/items` | `REPORT_READ` | 稳定分页；可按 attention/citation status 过滤 |
| GET | `/api/reports/{report_id}/parse-errors` | `REPORT_READ` | 稳定分页读取解析错误快照 |
| POST | `/api/reports/{report_id}/exports` | `REPORT_EXPORT` | 生成/复用 XLSX artifact；要求 Idempotency-Key |
| GET | `/api/report-exports/{export_id}/download` | `REPORT_EXPORT` | 只下载 completed artifact，不触发生成 |

列表统一返回 `{items,total,limit,offset}`，`limit` 有配置上限；sort 只接受白名单字段与 `asc|desc`，默认使用 §8.4。报告 POST 为同步原子操作：新建成功返回 201 completed，幂等复用返回 200 completed，不暴露持久化 partial polling；GET 未生成返回 404。policy publish 可返回 202 indexing。

report item schema 使用按 `citation_order` 升序的 `citations[]`。需要制度依据的 item 只有 1–3 条全部验证通过才返回数组与 `citation_status=verified`；任一失败时数组为空、status=`unavailable`，禁止返回部分成功引用。

### 11.3 状态码与错误码

| HTTP | 稳定 code 示例 |
|---|---|
| 401 | `AUTH_REQUIRED` |
| 403 | `PERMISSION_DENIED` |
| 404 | `POLICY_NOT_FOUND`, `REPORT_NOT_FOUND`, `BATCH_NOT_FOUND` |
| 409 | `POLICY_INTERVAL_OVERLAP`, `POLICY_INDEX_IN_PROGRESS`, `REPORT_IN_PROGRESS`, `REPORT_VALIDATION_REQUIRED`, `IDEMPOTENCY_KEY_REUSED`, `POLICY_BINDING_AMBIGUOUS` |
| 422 | `POLICY_FILE_UNSUPPORTED`, `POLICY_TEXT_UNAVAILABLE`, `POLICY_CLAUSE_INVALID`, `QUOTE_NOT_EXACT`, `REQUEST_VALIDATION_ERROR` |
| 500 | `POLICY_INDEX_FAILED`, `REPORT_GENERATION_FAILED`, `REPORT_EXPORT_FAILED` |

引用/检索 unavailable 通常是 completed report 内的显式 item 状态，不应把整个批次伪装成 500；只有未分类系统异常才 500。

### 11.4 OpenAPI

- Pydantic schema 是 API 与前端唯一事实来源；不得手写重复 DTO。
- download endpoint 的 XLSX 成功响应为 `application/vnd.openxmlformats-officedocument.spreadsheetml.sheet`，OpenAPI schema 为 binary string；export create endpoint 返回 JSON artifact 元数据。
- `Content-Disposition` 使用 RFC 5987 UTF-8 filename；filename 不参与服务端路径解析。
- binary endpoint 的错误仍返回统一 JSON ErrorResponse。
- 导出 OpenAPI 与生成客户端后必须连续二次运行无 diff。

---

## 12. 权限、审计与安全

### 12.1 RBAC

沿用现有 permission 数据：

- auditor：生成、查看、导出报告；可读 policy/binding。
- configurator：auditor 全部能力 + 上传/发布 policy、保存 binding。
- viewer：只查看/导出 completed report；不能生成、发布或 binding。

前端只按 permissions 显隐，后端每个 endpoint 仍独立鉴权。

### 12.2 审计白名单

允许的 action 至少包括：

- `policy.family_create`
- `policy.document_upload`
- `policy.document_publish`
- `policy.index_failed`
- `policy.binding_create`
- `batch.report_generate`
- `batch.report_failed`
- `report.export_generate`
- `report.export_download`
- `report.export_failed`

payload 只含 ID、hash/fingerprint、version、状态、计数、稳定 reason code。禁止原文、quote、raw/normalized row、PII、异常全文、storage 绝对路径、模型 prompt/response 或 secret。

### 12.3 数据不出内网

- source policy、clause/chunk、embedding、rerank、query/candidate/result 全部留在本地服务/Qdrant/PG。
- F4 report path 不调用云 LLM；provider 配置错误也不得把制度文本发送云端。
- embedding/rerank endpoint 只允许显式配置的 loopback/内网 allowlist；未知或外部地址 fail closed。测试必须断言 F4 report 路径云调用数为零，并覆盖 outbound allowlist。
- 若未来启用本地结构化 citation selector，其输出仍必须过 §7 且不能绕过 binding 责任。
- policy/expense 文本一律视为 data，不是 instruction；任何本地模型调用均严格 schema、无工具、无网络、无写权限。

### 12.4 UI 与 XLSX 注入

- React 只以文本节点呈现制度/报销内容，不使用未净化 HTML；测试覆盖 `<script>`、`javascript:` 与伪造系统指令文本。
- XLSX 所有外部文本写为 string cell；去除判定用的前导空格、tab、CR、LF 后若首字符为 `=`, `+`, `-`, `@`，必须做公式注入防护，但数据库中的原始行与 verified quote 不改写。
- 防护只发生在导出编码层；服务端保存的 verified quote 不改写。
- 工作簿禁止 formula、DDE、hyperlink、macro、external link、embedded object。

---

## 13. XLSX 导出契约

F4 MVP 只支持 `.xlsx`，固定工作表与顺序：

1. `摘要`
2. `关注项`
3. `原始行证据`
4. `解析错误`
5. `制度快照`

即使没有数据，每张表也必须保留固定标题行。

### 13.1 摘要

固定两列 `metric,value`，按预定义 metric 顺序包含 file/revision/content hash、validation/report/ruleset/mapping/policy/template fingerprints、生成时间、row/finding/citation counts 与 attention counts。不得只给百分比而省略分母。

### 13.2 关注项

每行一个 report item，固定列顺序为 `attention_group,source_verdict,source_outcome,row_no,finding_id,rule_id,rule_version,reason_code,reasoning,citation_status,requires_manual_citation,source_content_sha256`，随后是 `citation_1_*`、`citation_2_*`、`citation_3_*` 三组固定列；每组包含 `binding_id,family_stable_key,document_title,document_version,effective_date,expiry_date,clause_id,clause_no,hierarchy,quote,quote_start,quote_end,quote_sha256`。未使用 citation group 留空，不移动列。同一原始行多个 finding 保持多行。

### 13.3 原始行证据

只包含被 report item 或 parse error 引用的原始行，按 row_no 排序；固定前缀列为 `file_version_id,source_content_sha256,row_no`，动态原始列严格沿 F1 source header 的稳定顺序，重复/空 header 使用 F2 已冻结列标识，不按字母重排。不得静默省略 PII，但导出只在授权客户端完成，文件留在私有环境。

### 13.4 解析错误与制度快照

`解析错误` 固定列为 `row_no,error_code,column_name,message,source_content_sha256`。`制度快照` 每行一个 citation，固定列为 `report_item_id,citation_order,binding_id,family_id,family_stable_key,document_id,document_title,document_version,effective_date,expiry_date,document_content_sha256,clause_id,clause_no,hierarchy,clause_text,clause_text_sha256,quote,quote_start,quote_end,quote_sha256,verification_status`；不得将多个引用拼进一个单元格。

### 13.5 安全与可读性

- 冻结标题行、启用筛选、固定日期/Decimal 显示，不使用 float 重算金额。
- 用户/制度文本一律按 string 写入，应用 §12.4 注入防护。
- Excel 单元格 32,767 字符边界在 schema/导出前显式校验；禁止静默截断 quote 或原始证据。超限转稳定 export failure。
- openpyxl 或等价 reader 回读验证 sheet/column/order/type/count；成品检查无 formula/external link/vba。
- 相同 completed report 的语义内容必须稳定；artifact 完成后按 hash 复用。本阶段不承诺跨库版本重新打包得到逐字节相同 ZIP。

---

## 14. 桌面工作流

### 14.1 制度配置页

在配置区域新增制度管理：

- family/version 列表、effective/expiry、hash、draft/indexing/published/failed
- 上传与条款预览
- 索引进度/稳定失败原因
- rule version 的候选条款与 binding 历史
- verified quote 选择与 exact 验证反馈

不提供 published document/clause/binding 的编辑或删除。

### 14.2 批次报告页

在批次工作台增加“报告”视图，不进入 `/review`：

- validation 未完成
- 未生成
- 生成请求中（仅客户端请求状态）
- ready
- 无关注项
- citation unavailable / 需人工补齐引用
- 读取错误
- 导出中/导出错误

摘要区显示 attention row counts、finding count、verified/unavailable citation count、ruleset/policy/report fingerprint。关注项支持稳定分页与 evidence/citation 展开；row_no 可定位到既有原始数据视图。

### 14.3 视觉门禁

1440×1000 Chrome 覆盖：

- 5000 行摘要、长 policy/rule/clause ID、长 quote
- normal/empty/loading/error/degraded
- 三角色按钮/导航
- 分页、evidence/citation 展开与 XLSX 下载状态
- document 与关键容器无页面级横向溢出
- 恶意 HTML/URL/伪指令只按文本显示，下载态区分 artifact 生成与实际下载

F4 UI 不出现复核 decision/note/assignee/queue，不出现 correlation finding 或二维 severity badge。

---

## 15. 验收场景

### 15.1 Policy 与索引

- family/version/content hash 幂等；同 family 区间重叠正反向测试。
- expiry 边界：`effective <= date < expiry`；等于 expiry 不命中。
- 缺 expense_date 不取 latest。
- 跨租户 document/clause/chunk/job/generation FK 拒绝。
- PDF/DOCX/TXT 正常；扫描/加密/损坏/超限/重复 clause/空 clause 显式失败。
- PG commit 后 Qdrant 故障、worker kill、租约过期回收、重复 job/upsert、retry-limit/manual retry 后无重复逻辑点。
- building manifest 冻结且并发发布不混入；expected/completed count/hash/eval 全部满足后才可切换 generation，切换前后模型/chunker 不混用，旧 generation 不被原地覆盖。
- Qdrant 返回伪造 tenant/date/hash payload 时 PG 二次校验拦截。

### 15.2 Binding 与逐字引用

- 非空 exact substring 且必填 end-exclusive offsets 精确切片通过，保存 offsets/hash。
- 空串、纯空白、trim 后才匹配、NFC/NFKC 差异、大小写差异、全半角、换行折叠、标点替换、编辑距离、跨 clause 拼接全部拒绝。
- 未验证 quote 不出现在 DB/audit/log/trace/API/UI/XLSX。
- 相同 binding 重试复用；不同 quote/offset/hash 产生新 fingerprint 或拒绝冲突。
- 过期/未来/跨租户/未发布 clause 不能 binding。
- 检索高相似但未确认的候选不能成为正式 report citation。

### 15.3 Report

- 无 validation、validation 未完成、锁冲突、同 key 异请求稳定失败。
- 同批/同 key、同批/新 key、并发、process kill/restart 均最多一份 report 与一次成功审计。
- 同一行 0/1/多 finding；多 finding 不丢。
- row/finding/count 恒等式；parse error 显式出现，passed 不制造 finding。
- binding 完整时 1–3 条有序 citation 全部 verified；缺 binding/歧义/hash 漂移保留 source verdict 与原 attention group，整组 citation unavailable 且不展示部分引用。
- Qdrant 下线后 completed report 仍可读取和导出。
- 新 policy/binding 不改变历史 report；`policy_change` 派生 revision 才产生新快照。
- 跨租户 report/item/citation/export 不可见。

### 15.4 API/RBAC/OpenAPI

- 三角色 × policy create/upload/publish/bind × report generate/read/export 权限矩阵。
- 未登录 401、无权限 403、跨租户 404。
- pagination/filter/sort 稳定；全部错误 shape 与 code 固定。
- binary XLSX 成功与 JSON error content type 正确。
- OpenAPI/客户端连续二次生成无 diff。

### 15.5 XLSX/UI/性能

- 回读 5 张工作表、固定列/顺序/count/type，空表仍有标题；无 formula/DDE/hyperlink/macro/external link。
- 前导空格/tab/CR/LF 后的 `= + - @`、超长文本、中文 filename、长 quote 与 32,767 边界。
- completed export create 重放复用 artifact/hash，不重复生成审计；每次 download 都有下载审计。
- 固定 seed 的 5000 行 F1→F2→F3→F4→XLSX 全链路纳入全局 900 秒硬上限；分别记录报告装配耗时、SQL 查询数/耗时、XLSX 耗时与 artifact size。
- 1440×1000 全状态、权限和溢出复核。

---

## 16. 实施检查点

本文件是 CP-F4.0–F4.5 的唯一规范来源。实现发现冲突时先修改本文件并在 §17 记录覆盖决定，再继续编码。

### 16.1 CP-F4.0 · 规格固化

**目标：** 固定制度 family/version、发布/索引、binding、exact quote、report snapshot、XLSX 与实施边界。

**交付物：** 本规格 §1–§15 与 CP-F4.1–4.5 契约。

**非目标：** 不创建迁移、服务、路由、UI 或依赖，不预填未来测试数量。

**退出条件：** 无 P0/P1 未决项；TechDesign fuzzy 冲突已覆盖；F5/F6/F8 边界明确；`git diff --check` 与文档审查通过。

### 16.2 CP-F4.1 · 持久化 schema 与 ORM

**目标：** 用新增 `0005_f4_policy_reports.py` 落地 §9。

**具体交付物：** family/source-blob/document/clause 强化、chunk、generation/document-index/outbox、binding、report/item/parse-error/citation/export；全部复合 tenant FK、GiST 区间/唯一/check/RESTRICT、legacy 回填与安全 downgrade。

**非目标：** 不解析文件、不连 Qdrant、不生成报告/API/UI。

**测试：** 空库/legacy 升级、往返、安全 downgrade、区间重叠、租户错配、受保护约束反向测试、双库 `alembic check`。

**退出条件：** 备份/迁移/ORM/测试/Ruff/mypy 通过，CP-F4.2 无需再决定存储字段。

### 16.3 CP-F4.2 · 制度导入、发布与本地检索

**目标：** 实现 §3.1、§4、§5 的确定性 ingestion 与 Qdrant outbox。

**具体交付物：** 私有文件存储、PDF/DOCX/TXT 解析、clause/chunk、preview/publish、VectorStore、generation、local embedding/rerank、候选 binding query。

**非目标：** 不保存 binding、不生成报告、不做 UI/API（服务层测试可直调）。

**测试：** §15.1 全部；worker kill/retry、Qdrant 断连、跨租户与时间边界为最高优先级。

**退出条件：** published 文档可稳定检索，PG/Qdrant 一致性可恢复，原文/模型数据不出内网。

### 16.4 CP-F4.3 · Binding、引用核心与报告编排

**目标：** 实现 §6–§10 的纯 exact verifier、binding 与原子 report snapshot。

**具体交付物：** canonical fingerprints、binding 保存、report/item/parse-error/citation 原子装配、attention/count/sort、Idempotency-Key、policy_change revision、失败审计。

**非目标：** 不暴露 API/UI/XLSX，不实现 review/correlation/F8。

**测试：** §15.2–15.3 全部；引用反向测试覆盖率 100%；幂等/中断/并发/租户为门禁。

**退出条件：** completed report 在 Qdrant 离线时仍可稳定读取，未经验证 quote 无任何持久化/呈现路径。

### 16.5 CP-F4.4 · API、桌面工作流与 XLSX

**目标：** 实现 §11–§14。

**具体交付物：** policy/binding/report/export 路由与 Pydantic/OpenAPI；制度配置页、批次报告视图；XLSX-only 导出。

**非目标：** 不实现 F5/F6/F8、PDF/CSV/mobile。

**测试：** §15.4–15.5；前端 unit/integration、XLSX 回读/注入、1440×1000 全状态。

**退出条件：** API/前端/XLSX/权限通过；OpenAPI 二次无漂移；生产 build 与视觉记录完整。

### 16.6 CP-F4.5 · 契约与交付门禁

**目标：** F4 全量回归、迁移/受保护约束、契约、安全、5000 行性能与桌面交付门禁。

**具体交付物：** 后端 pytest/Ruff/format/mypy/迁移，前端 test/typecheck/lint/format/build，双库 Alembic、OpenAPI 二次、pre-commit/gitleaks、5000 行 report+XLSX、1440×1000。

**非目标：** 不降低阈值、不跳过失败、不改受保护基础设施、不提前进入 F5/F6。

**退出条件：** 所有命令零退出；历史报告快照、exact quote、XLSX 安全与全局 15 分钟上限通过；状态文件写入真实数量后才进入 F5。

---

## 17. 实际落地记录

本节只追加已经完成的事实，不为未来检查点预填结果或测试数量。

### CP-F4.0 实际落地记录（2026-07-28）

- 新增本规格，固定 F4 的制度 family/version、确定性切分、Qdrant generation/outbox、人工确认 binding、严格逐字引用、不可变 report snapshot、XLSX-only 和 CP-F4.1–4.5 实施契约。
- 覆盖 TechDesign 的 fuzzy quote 与 report 直接回写 finding：正式引用改为 PostgreSQL 原文 exact substring + configurator-confirmed binding，F4 不修改 F3 finding。
- 关键安全决定：制度原文与检索数据不进入云 LLM；F4 MVP 报告装配不依赖生成式云模型。检索失败或缺 binding 时保留 F3 verdict 并显式转人工，不猜测条款。
- 产品边界决定：导出只做 XLSX；PDF/CSV 不在 F4。attention group 只映射 F3 verdict，不使用 F8 二维 severity。
- 独立审查收口：报告改为 F3 风格的单业务事务全有或全无；引用状态与 row-level attention group 正交；每个 item 保存 row verdict 与 finding outcome；索引代际采用 frozen manifest + 显式 delta；XLSX 固定 5 张表，artifact 生成与下载分离。
- 实现代码、迁移、依赖、API、UI 与未来测试数量均未预填。

### CP-F4.1 实际落地记录（2026-07-28）

- 只新增 `0005_f4_policy_reports.py` 并同步 ORM：落地 policy family/source blob、document/clause 强化、clause 内 chunk、index generation/document manifest/outbox、configurator-confirmed binding、原子 report/item/parse-error/citation snapshot 与 XLSX artifact 元数据；未实现解析、Qdrant、服务、API、UI 或 XLSX 生成。
- legacy policy 安全策略已机械化：升级前 preflight clause→document 与 finding→clause tenant 闭包，错配则事务原子失败；既有 document 仅回填 `legacy_unpublished`，family/blob/hash/parser/chunker/creator/publish 字段及 clause ordinal/hash/locator/offset 均保持 NULL，finding 的 clause/quote/reasoning 原值保留。`legacy_unpublished` 不设 ORM/数据库默认，迁移后新 INSERT 被触发器拒绝。
- 约束闭包已落地：旧 policy_clause→document `CASCADE` 与 finding→clause `SET NULL` 被 0005 替换为复合 tenant FK + `RESTRICT`；family/document/clause/chunk/binding/report citation 使用复合候选键闭合身份。安装并验证 `btree_gist`，published document 使用 tenant/family/半开 `daterange` 的部分 GiST exclusion constraint。
- 不可变与恢复边界已落地：published document 仅允许一次 open-ended expiry 收口，published clause/chunk、source blob、binding、report snapshot 与 completed report/export 均由数据库触发器保护；downgrade 在 published policy、binding、report 或 export 存在时于任何 DDL 前拒绝。
- pre-0005 默认开发库备份位于 gitignored 的 `data/private/backups/cp-f4.1/pre-0005-20260728-172329/`。full/schema/affected-data custom archive 均通过 `pg_restore --list` 与容器/本地 SHA-256 交叉校验，哈希分别为 `906d07f68c7a5d68576b0ea9f3665e6e17853e3c0062c60799ab02bf62c1b450`、`618e4816e5bd6069f89458e87c741af4203a24cb3bc2e6d48f2945e4b188419e`、`42e046aa3aabc46b41352f5635c2bd12be51278061d055b3e54c90d8d99d172d`。
- 验证结果：CP-F4.1 定向 4 passed；迁移/幂等/恢复相关组 39 passed；后端全量 244 passed/1 skipped；Ruff lint/format、strict mypy（81 个源文件）、0005 往返、legacy/preflight/safe-downgrade、GiST expiry/重叠、跨租户 FK、RESTRICT、outbox unique、受保护约束反向测试及默认/测试双库 `alembic check` 全部通过。默认开发库与测试库均位于 `0005 (head)`。

### CP-F4.2 实际落地记录（2026-07-28）

- 新增 `core/policies/` 与 `core/retrieval/`：制度源文件按 tenant/SHA-256 存入私有目录，PG 仅保存相对 storage key；TXT 严格 UTF-8，PDF 拒绝加密/扫描文本层缺失，DOCX 做 zip 展开上限和确定性段落抽取。条款只接受显式编号边界，重复/空/歧义/越界/超限 fail closed；clause/chunk 均保留逐字切片、offset 与 hash。
- family、document draft、preview/publish 服务只使用现有 0005 schema；上传按 family/version/content/interval 幂等，审计 payload 仅含 ID、hash、版本与计数。发布冻结 document manifest 并写 transactional outbox，不保存 binding、不生成 report，也未增加 API/UI。
- `VectorStore` Protocol 与 Qdrant 实现强制 tenant/generation/epoch-day 半开区间过滤，使用 chunk UUID 作为稳定 point ID；既有 collection 会核对 vector size/distance 并创建 tenant/generation/date payload index。候选返回后按 PG tenant/document/clause/chunk 闭包、published 区间、generation/model/chunker/hash 与连续切片二次校验，随后用本地 rerank 稳定排序；缺日期、低于 cutoff 或任一 payload 漂移均显式 unavailable/空候选。
- outbox worker 使用 `FOR UPDATE SKIP LOCKED`、lease token、attempt limit 与稳定 failure code；已覆盖“Qdrant upsert 成功后进程退出”的租约回收和同点重放。terminal failure 会冻结同文档兄弟 job，人工 retry 复用原 job；active 增量发布会同步刷新被收口前序版本的 expiry payload，building generation 使用 frozen manifest、显式 delta revision 和原子 active/retired 切换，active/building job 可按 generation 路由到不同 collection。
- 本地模型提供 `fake`（仅 dev/test）和 Infinity HTTP 双路径；prod 禁止 fake，模型与 Qdrant URL 必须命中显式主机白名单且不得内嵌凭据。F4 路径没有云 LLM provider 或出网回退。
- 新增运行时依赖 `pypdf 6.x` 与 `python-docx 1.2.x`。真实 PostgreSQL/Qdrant 定向测试覆盖内容幂等、跨租户隐藏、expiry end-exclusive、伪造 payload、worker kill/retry、terminal/manual retry、generation rebuild/delta 与 collection provenance。最终后端全量 `269 passed, 1 skipped`；Ruff lint/format、strict mypy（93 个源文件）、默认/测试双库 `alembic check`、OpenAPI/前端客户端连续二次生成、pre-commit/gitleaks 与 `pip-audit --strict` 全部通过。

### CP-F4.3 实施覆盖决定（2026-07-28，编码前）

- 只读预检发现现有 `file_version.revision_reason` 只允许 `ruleset_change|mapping_change`，无法表达 §10.4 已冻结的 `policy_change`；同时 `report_run` 只保存单个幂等 key，无法同时长期满足“completed report + 新 key 复用”和“同 key + 异请求冲突”。
- 经实施计划确认，CP-F4.3 新增 `0006`，不修改既有迁移：扩展 `policy_change` 值域并新增追加式 `report_request` 幂等账本。升级前执行私有可恢复备份与校验；迁移、ORM、服务和并发/恢复测试必须共同证明新闭包不弱化 `row_result`、`sampling_audit`、`audit_log` 或既有 F4 不可变约束。

### CP-F4.3 实际落地记录（2026-07-28）

- 新增纯 `citations.py` 与 canonical binding/report fingerprint：exact verifier 只按 Python Unicode code point、必填 end-exclusive offsets 验证 PG clause 连续子串，不做 strip、Unicode normalization、casefold、全半角/空白/标点转换、搜索、编辑距离或跨 clause 拼接。失败统一为安全稳定错误，Pydantic binding 输入启用 `hide_input_in_errors`；sentinel 反向测试证明候选 quote 不进入异常、DB、audit 或日志。
- 新增 PG-only Binding 服务：configurator 在 tenant NOWAIT 锁内一次保存完整 1–3 条连续 order，校验同租户 rule/actor、published document、`effective <= expense_date < expiry`、family/document/clause 复合身份、clause hash 与 exact quote 后才写入；相同 canonical set 重放复用且成功审计最多一次，不同/歧义 set fail closed。Qdrant 候选、score 与模型不进入正式 binding。
- 新增原子 Report 服务：严格 `Tenant FOR UPDATE NOWAIT → FileVersion FOR UPDATE NOWAIT`，只消费 completed F3、row_result/finding/expense_row/parse error 与 PG binding；每个 finding 独立 item，同 row 多 finding 不折叠，passed 无 finding 只计数，parse error 独立 snapshot。任一预期引用失败时保留 source outcome/verdict/attention，整条 item citation unavailable 且零部分 citation；成功 report/item/parse-error/citation/count/`batch.report_generate` 同事务，fault 后全回滚并用独立短事务写无 PII `batch.report_failed`。completed replay/read 只读 snapshot，不访问 Qdrant、模型或当前 binding。
- 新增 `0006_f4_report_requests.py`，不修改 `0001`–`0005`：`file_version.revision_reason` 增加 `policy_change`；新增 append-only `report_request`，以 tenant/key hash 唯一、request fingerprint 和 report/file 复合 tenant FK 记忆首次及后续 alias key。downgrade 在任一 policy_change revision 或 report_request 存在时于 DDL 前拒绝。`policy_change` 复制 raw/normalized/parse-error/field-availability snapshot，不复制 validation/dependency/row_result/finding/report/export，必须重新 F3 后才能生成新报告。CP-F4.3 未暴露 binding/report/API/UI/XLSX；既有 revision API 仍只接受原有两种 reason，`policy_change` 等 CP-F4.4 显式接线。
- pre-0006 默认库备份位于 gitignored 的 `data/private/backups/cp-f4.3/pre-0006-20260728-190828/`。full/schema/file_version affected-data custom archive 均通过 `pg_restore --list` 与容器/本地 SHA-256 交叉验证，哈希分别为 `96614363fa9a5471c232535b5ecf69bedc6f1a0ef15e5bca3d0d7d55da08da3b`、`d7b4bb6cf55d03d734c992bbf9a5df8bb2a6635ff6f284ee0d420c0e1d0e5452`、`eb2f85a491edf4922fa6b68bf598da17babe83db367cd75959f1763aaf5c816a`。默认/测试双库均为 `0006 (head)` 且 Alembic 零漂移。
- 报告装配额外显式验证 actor tenant scope，并机械重算 `canonical_binding_fingerprint`；跨租户 actor 零报告/失败审计副作用，错误 binding fingerprint 整组引用降级为 `POLICY_BINDING_INTEGRITY_FAILED` 且不冻结部分引用。
- 验证结果：CP-F4.3 定向 `65 passed`；exact verifier `23 passed` 且 statement/branch coverage `100%`；后端全量 `318 passed, 1 skipped`；Ruff lint/format、strict mypy（99 个源文件）、0006 clean roundtrip/safe downgrade、双库 `alembic check`、受保护约束/租户隔离/锁冲突/幂等/中断恢复回归、OpenAPI/前端客户端连续二次生成无漂移、pre-commit/gitleaks 与 `pip-audit --strict` 全部通过。前端 `20 passed`，typecheck/oxlint/Prettier/生产 build 通过。
