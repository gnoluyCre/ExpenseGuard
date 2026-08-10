# Spec 008 — Phase 3 F7 异常取证 Agent（ReAct）

**状态：** CP-F7.0–CP-F7.5 已完成
**日期：** 2026-08-10
**前置检查点：** F6 / CP-F6.5 已完成
**唯一规范来源：** 本文件覆盖早期 PRD/TechDesign 中与 F6 已落地身份模型冲突的 F7 骨架描述

---

## 1. 目标与范围

F7 只接收一个显式的 `detection_run_id + correlation_finding_id`，围绕 F6 已冻结的跨行统计候选运行一个受限 ReAct 取证循环。Agent 可以依中间结果选择下一项只读工具，但不能改变候选、原始行、制度、规则、复核或分级事实。每个已完成步骤都追加写入 `evidence_step`，最终必须形成一个显式终态，并明确区分“证据充分”“证据不足”“能力不可用”“达到步数上限”和“系统失败”。

F7 的完成必须证明以下不变量：

1. F7 不隐式读取“最新 detection run/finding”；请求、run、step、result 始终由完整租户复合身份闭合。
2. F7 不写 F3 `finding`，不更新 F6 `correlation_finding`，不填写 F8 severity；调查历史由独立、不可变事实承载。
3. tenant、PII 原文、凭据和隐藏推理不进入云模型请求、业务审计、trace 或错误信息。
4. 模型只能产出严格结构化的“调用一个白名单工具”或“终止”动作；工具全部只读且作用域由服务端注入。
5. 数据库中的 run/request/step/result 事实可幂等重放；崩溃恢复后不产生重复步骤、结果或审计。
6. 缺少真实 API 配置时应用正常启动，F7 能力显式为 `unavailable` 并转人工，绝不猜测结论。
7. 自动门禁不访问真实云模型或真实本地模型；全部模型路径由 deterministic/scripted provider 与本地 HTTP mock 验证。

### 1.1 明确在范围内

- 一个单点 ReAct agent、固定最大步数、严格结构化动作、LangGraph checkpoint 与 PostgreSQL 业务事实恢复。
- 四个只读工具：关联参与行、同员工历史、同供应商历史、制度条款检索。
- 稳定、租户隔离的 PII token 化；模型输入白名单与出站二次扫描。
- OpenAI-compatible HTTP provider、无网络 scripted provider、能力声明和人工回退。
- investigation run/request/evidence/result/token 持久化、强类型 service/API/OpenAPI 和桌面调查视图。
- 幂等、并发、kill/restart、跨租户、prompt 注入、逐字引用与 5000 行合成批次门禁。

### 1.2 明确不在范围内

- 不实现 F8 `severity_impact/severity_confidence`、代价敏感阈值或 grading snapshot。
- 不把 F7 结果自动写进 F5 queue/review，不生成 `confirmed/false_positive` 人工标签。
- 不更新、删除或补写 F3/F4/F5/F6 的不可变事实，不重新生成既有报告/XLSX。
- 不允许 Agent 写数据库业务对象、调用任意 SQL、访问文件系统、联网搜索、发消息或执行代码。
- 不做多 Agent、长期记忆、自主创建工具、任意 MCP、任意 URL 或模型生成 SQL。
- 不保存或展示 chain-of-thought；只保存结构化动作、工具事实、短审计摘要和终态。
- 不做时空 Tier 1 外部行程系统接入、地理编码或公网数据补全。
- 不做后台任务队列、批量调查、通知、移动端/平板端或 F7 导出格式。
- 不测试真实云 API、真实 vLLM、真实 embedding/rerank、真实客户数据或生产发布；见第 13 节。
- 不修改 `0001`–`0008`；schema 只能由新增 `0009` 前向迁移。

### 1.3 对旧设计与 skeleton 的覆盖决定

- `evidence_step` 当前 `finding_id -> finding.id` 与 F7 的 F6 `correlation_finding` 输入不相容。禁止创建伪单行 `finding` 作为桥接，也禁止把 correlation candidate 复制成 F3 finding。
- CP-F7.1 新增 `investigation_run`，并把 `evidence_step` 改为引用 investigation run；调查 run 再以完整复合 FK 引用 F6 correlation finding。
- 早期 TechDesign 的“F7 副作用：写 `finding + evidence_step`”由本规格覆盖为“追加 `investigation_run/request/evidence_step/investigation_result`”；F8 仍必须使用独立 grading snapshot。
- 旧 `evidence_step` 只是未启用的 skeleton。`0009` upgrade 必须在任何 DDL 前检查其为空；只要存在一行就 fail closed，不猜测该行应属于哪个 correlation finding/run。

---

## 2. 术语、身份与终态

| 术语 | 定义 |
|---|---|
| investigation run | 对一个显式 F6 candidate 执行一次调查的不可变输入/配置身份 |
| evidence step | 一个已完成的模型动作，以及可选只读工具调用的脱敏输入与结果快照 |
| investigation result | 一个 run 唯一、不可变的显式终态 |
| request ledger | 把 tenant 内的 Idempotency-Key 与原始请求指纹绑定到 run |
| business facts | `investigation_*`、`evidence_step`、PII token 与 audit；恢复时优先于 checkpoint |
| checkpoint | LangGraph 的运行进度加速器；不是业务完成或幂等性的事实来源 |

终态固定为：

| outcome | `evidence_sufficient` | 语义 |
|---|---:|---|
| `sufficient` | `true` | 模型通过合法 terminate 动作明确判断现有证据充分 |
| `insufficient` | `false` | 模型明确判断现有证据不足，转人工 |
| `unavailable` | `null` | 缺少 provider/PII 配置或已知能力前置条件，未猜测结论 |
| `max_steps` | `null` | 达到固定步数仍未合法终止，转人工 |
| `failed` | `null` | 超时、模型响应无效、工具或系统异常，转人工 |

`unavailable/max_steps/failed` 不能伪装成 `insufficient`。只有 `sufficient/insufficient` 是证据充分性判断。

---

## 3. CP-F7.1 — `0009` 持久化规范

### 3.1 迁移前置与回退

- 只新增 `0009_f7_anomaly_investigation.py`，不得修改旧迁移。
- `upgrade()` 第一项操作必须查询 `evidence_step`；非空时在任何 DDL 前抛错并保持 schema 完全不变。
- 修改 schema 前按项目既有流程生成 gitignored 私有 full/schema/affected-data 备份，验证 SHA-256、`pg_restore --list` 和隔离恢复。
- `downgrade()` 必须在任一 F7 run/request/step/result/token 事实存在时于任何 DDL 前拒绝；无事实时才能恢复精确的 `0008` skeleton。
- 所有新业务 FK 使用 `RESTRICT`；不得削弱 `row_result`、`sampling_audit`、`audit_log` 或 F6 追加写约束。

### 3.2 `investigation_run`

必须至少包含：

- `id, tenant_id, correlation_finding_id, detection_run_id, file_version_id, actor_id, created_at`
- `agent_version, action_schema_version, prompt_template_version`
- `provider_kind, provider_model, max_steps, timeout_seconds`
- `redaction_version, input_fingerprint, config_fingerprint`

约束：

- 复合 FK `(correlation_finding_id,detection_run_id,file_version_id,tenant_id)` 指向 F6 `uq_correlation_finding_identity`。
- actor 通过 `(actor_id,tenant_id)` 绑定本租户用户。
- hash 均为小写 64 位 SHA-256；版本/模型名/枚举有长度、字符集与非控制字符约束；`max_steps` 固定允许 `1..12`。
- 表是不可变输入快照，拒绝 UPDATE/DELETE。它不保存 API key、原始 prompt、PII 原文或 chain-of-thought。
- 允许同一 candidate 产生多次显式调查历史；幂等边界是 request ledger，不以“最新一次”覆盖旧 run。

### 3.3 `investigation_request`

- 包含 `tenant_id, investigation_run_id, correlation_finding_id, detection_run_id, file_version_id, idempotency_key_hash, request_fingerprint, created_at`。
- unique `(tenant_id,idempotency_key_hash)`；完整复合 FK 回到 investigation run。
- 同 key + 同 fingerprint 返回原 run；同 key + 异 fingerprint 返回稳定冲突；replay 不读取当前 provider 配置、不调用模型/工具。
- request ledger 追加且不可变，拒绝 UPDATE/DELETE。

### 3.4 `evidence_step` 的精确改造

- 删除旧 `fk_evidence_step_finding_id_finding`、`finding_id` 和 `unique(finding_id,step_no)`。
- 新增 `investigation_run_id, correlation_finding_id, detection_run_id, file_version_id`，并保留 `tenant_id,step_no,tool_name,tool_input,tool_output,created_at`。
- 新增 `action_kind, action_schema_version, model_action, decision_summary, payload_fingerprint`。
- unique `(investigation_run_id,step_no)`；`step_no` 从 1 开始且不超过 run 的 `max_steps`，服务层还须验证连续无洞。
- 复合 FK 将 step 绑定到同一 run/candidate/file/tenant，使用 `RESTRICT`。
- `model_action/tool_input/tool_output` 只允许 JSON object；每项施加 UTF-8 字节上限。`tool_name` 仅可为空于 terminate 动作，工具动作必须是四个固定名称之一。
- step 只在一次模型动作及可选工具读取均完成后追加提交，拒绝 UPDATE/DELETE。不得保存隐藏思维链或未经脱敏的 provider payload。

### 3.5 `investigation_result`

- 包含完整 run/candidate/file/tenant 身份、`outcome,evidence_sufficient,summary,reason_code,citations_json,result_fingerprint,completed_at,created_at`。
- unique `(investigation_run_id)`；复合 FK 使用 `RESTRICT`；表拒绝 UPDATE/DELETE。
- CHECK 强制第 2 节 outcome/bool 对应关系、非空 bounded summary、安全 reason code、引用 JSON schema 版本和 hash 格式。
- `citations_json` 只能包含已通过 F4 机械逐字校验的 clause ID、版本、quote 与定位；校验失败的引用不得写入 result 或返回 UI。

### 3.6 `pii_token`

- 包含 `id,tenant_id,token_version,pii_kind,source_hmac,token,created_at`。
- unique `(tenant_id,token_version,pii_kind,source_hmac)` 与 `(tenant_id,token)`；跨 tenant 不共享 identity。
- 只保存使用服务端密钥计算的 keyed-HMAC 指纹与展示 token，不复制姓名、工号、证件号、手机号、供应商原文。
- token 事实追加且不可变；发生 hash/token 冲突必须 fail closed。
- token 格式固定包含类型与版本，不暴露原文长度或可逆编码。

---

## 4. Provider、配置与零真实网络

### 4.1 配置

Settings 使用以下键；`.env.example` 只写键名和安全说明，不写真实值：

- `LLM_PROVIDER=disabled|openai_compatible`，默认 `disabled`
- `LLM_BASE_URL`
- `LLM_API_KEY`
- `LLM_MODEL`
- `LLM_TIMEOUT_SECONDS`，默认 30、范围 1..120
- `LLM_VALIDATION_RETRIES`，默认 1、范围 0..2
- `INVESTIGATION_MAX_STEPS`，默认 6、范围 1..12
- `PII_TOKENIZATION_KEY`
- `PII_TOKENIZATION_VERSION`，默认 `1`

缺 provider、base URL、key、model 或 PII HMAC key 时，应用仍须启动；F7 capability 返回 `unavailable` 与稳定 reason code，触发调查得到 `unavailable` 终态且不发网络请求。Secret 类型的 repr、校验错误、日志和 audit 不得出现原值。

### 4.2 强类型 provider

- `LLMProvider` 是通用 Protocol，只接受已脱敏、已限长的消息 DTO 与同一份 Pydantic action schema，返回结构化 action 和无敏感 usage 元数据。
- `OpenAiCompatibleProvider` 使用现有 `httpx`，调用配置 base URL 下的 `/chat/completions`，通过 Bearer header 与 JSON-schema structured output 约束动作；不新增特定厂商 SDK。
- URL 必须是合法 `http/https`，禁止内嵌凭据；API key 只出现在授权 header，不持久化。
- timeout、429、5xx、网络错误、非 JSON、schema 错误、未知工具和重复/越界字段必须映射为稳定错误。结构校验仅按配置重试，耗尽后进入 `failed`，禁止自由文本兜底。
- `ScriptedLLMProvider` 是 dev/test 的确定性无网络实现；它按测试脚本返回动作或错误，不用于生产质量判断。

### 4.3 自动测试的网络禁令

- 单元、集成、API、浏览器和 5000 行门禁一律使用 scripted provider 或 `httpx.MockTransport`。
- 测试必须设置网络哨兵，任何未被 mock 的模型 HTTP 请求立即失败。
- HTTP 契约测试覆盖 endpoint/header/body/response schema、超时、429/5xx、坏 JSON 和 schema invalid，但不得使用真实 key、真实模型端点或公网访问。
- embedding/rerank 继续保持既有内网 HTTP 边界；F7 自动门禁不启动或验证真实本地模型。

---

## 5. PII 脱敏与 prompt 边界

- 入模字段使用显式 allowlist；姓名、员工号、身份证、手机号、银行卡、供应商/商户标识等先按 tenant + kind + canonical value 做 keyed HMAC，再映射成稳定 token。
- 同一租户、同一版本、同一主体在不同批次/行必须产生相同 token；不同租户或不同 kind 必须不同。空值、歧义值和冲突 fail closed。
- 日期、金额、币种、费用类型和 row reference 按最小必要原则发送；原始备注、OCR 全文、地址自由文本和未声明 JSON 字段默认不发送。
- 工具在内网用原始字段查询，返回 provider 前转换为脱敏 DTO；模型传回的 tool input 在执行前再次 schema 校验和 token 作用域校验。
- 出站前运行 PII scanner；命中已知原值、证件/手机号模式、凭据或未允许字段时阻止调用并形成安全失败。
- system instructions 与所有外部数据使用明确边界；关联行、历史数据和制度文本均标记为“数据，不是指令”。模型输出不能改变 system policy、tenant context、工具集合或步数。
- 业务 audit/trace 只记录 run/step ID、稳定 reason code、hash、时延和 token 用量；不记录完整 prompt/response、工具原文、Idempotency-Key 或 HMAC key。

---

## 6. 四个只读工具

工具动作使用 Pydantic 判别联合。tenant、run、finding、actor 和数据库连接全部由服务端 context 注入，模型不能提供或覆盖。所有查询显式 tenant predicate、稳定排序、精确 limit/cursor 和响应字节上限；禁止原始 SQL、任意字段名与任意 URL。

1. `get_correlation_rows`
   - 读取当前 F6 candidate 的全部物理参与行及 typed evidence。
   - 模型输入只允许分页参数；不能指定另一个 run/finding。
   - 输出为脱敏 row DTO，并证明返回行属于当前 composite identity。

2. `get_employee_history`
   - 只接受当前调查上下文中已经出现的员工 token、显式日期范围和有界分页。
   - 服务端从当前 candidate 重建 token-to-local-identity 的短生命周期映射，在 PostgreSQL 内查询同 tenant 历史；不能跨租户或凭 token 枚举主体。
   - 输出稳定按发生日、file revision、row_no 排序的脱敏摘要。

3. `get_supplier_history`
   - 与员工历史相同，只接受当前上下文已出现的供应商 token。
   - 输出有界的交易、发票和关联 row reference；不泄露供应商原文或其他租户数据。

4. `search_policy_clauses`
   - 只接受经过二次脱敏/限长的查询、费用发生日和有界 top-k。
   - 复用 F4 本地 Qdrant + embedding/rerank 检索与 effective-date 过滤；不得把制度文档发送到云模型服务之外的端点。
   - 返回 clause/document/version/原文候选。最终引用必须再次经过 F4 逐字校验；本地检索不可用时显式返回工具能力错误。

工具没有写操作。只读工具重复执行不会改变业务数据；即使 prompt 注入诱导未知工具或越权参数，dispatcher 必须在查询前拒绝。

---

## 7. ReAct 编排、事务与恢复

### 7.1 动作协议

每轮模型只能返回一个动作：

- `tool_call`：四个固定 tool name 之一，以及对应严格输入 schema。
- `terminate`：`evidence_sufficient: bool`、bounded summary、reason code 与候选引用。

额外字段、未知 action/tool、超长文本、控制字符或 schema mismatch 均 fail closed。模型不得返回 severity、review decision、SQL 或写操作。

### 7.2 执行顺序

1. 在短事务中校验 actor/tenant/F6 composite identity、能力与 Idempotency-Key，创建 immutable run + request，或返回既有 run。
2. 不持有数据库锁或事务跨越模型 HTTP 调用。
3. 每轮先从业务库按 step_no 加载已提交 step；存在则验证 fingerprint 并复用，不再调用 provider/tool。
4. 不存在时，使用已提交 steps 重建脱敏上下文，调用 provider，校验 action；若为工具动作，执行一个只读工具。
5. 在独立短事务中以 unique `(run_id,step_no)` 追加完整 step。并发冲突时读取既有 step；payload fingerprint 相同则复用，不同则 fail closed。
6. 合法 terminate 在写入对应 evidence step 后，以独立短事务追加唯一 result 与 terminal audit。
7. 达到 `max_steps`、配置不可用或错误耗尽时，不猜测 evidence bool，追加对应 result 与 terminal audit。

### 7.3 LangGraph checkpoint 与业务事实

- 复用现有 `AsyncPostgresSaver` 与独立 `langgraph` schema；thread ID 固定为 investigation run ID，并使用 F7 checkpoint namespace。
- PostgreSQL public schema 的 request/step/result 是业务权威；checkpoint 不能证明业务步骤已提交。
- checkpoint 落后业务事实：从已提交 evidence steps 快进 state。
- checkpoint 领先业务事实：丢弃未有业务 fact 的进度，并从最后一个已提交 step 重放。
- result 已存在：立即返回，模型和工具调用次数必须为零。
- checkpoint 与业务 step fingerprint 不一致：安全失败并写无 PII audit，禁止合并或覆盖历史。

### 7.4 外部调用的幂等边界

数据库 side effects 对同一 request/run/step 至多一次。模型 API 不具备通用 exactly-once 协议：若进程在 API 返回后、step 提交前崩溃，恢复可能重复一次模型调用并产生额外费用。该窗口必须在代码、运维文档和 trace 中如实说明；不得用“数据库幂等”掩盖外部调用语义。重复响应仍不能产生重复 step/result/audit，工具因只读可安全重放。

---

## 8. Service 与错误语义

- 业务逻辑位于 `app/core/agent/`；路由不写 SQL，UI 不推导终态。
- service 输入必须包含 tenant/actor/detection run/correlation finding/idempotency key；不接受“最新”布尔开关。
- capability 至少返回 `enabled|unavailable`、reason code、provider kind/model、max steps 和本地 policy retrieval 状态；不得回显 base URL 中的凭据或 API key。
- 稳定错误至少覆盖：not found、tenant mismatch、idempotency conflict、provider unavailable、redaction unavailable、model timeout/rate limited/invalid、tool unavailable/invalid、max steps、checkpoint mismatch、concurrent conflict。
- 已知能力缺失生成 `unavailable` result；瞬时/实现错误生成 `failed` result。数据库身份/完整性错误必须回滚，不得伪装业务终态。
- 失败 audit 只记录安全 ID/hash/reason code；终态 result 与 terminal audit 同事务。

---

## 9. API 契约与 RBAC

推荐固定端点：

- `GET /api/v1/investigations/capability`
- `POST /api/v1/detection-runs/{detection_run_id}/findings/{correlation_finding_id}/investigations`
- `GET /api/v1/detection-runs/{detection_run_id}/findings/{correlation_finding_id}/investigations`
- `GET /api/v1/investigations/{investigation_run_id}`
- `GET /api/v1/investigations/{investigation_run_id}/steps?limit=&offset=`

契约：

- POST 强制 8..128 字符 `Idempotency-Key`；创建返回 201，replay 返回 200 并标记 `reused_existing`，异 payload 返回 409。
- POST 同步运行到终态；客户端断开/进程重启后以同 key 重试并从业务 facts 恢复。F7 不引入后台队列。
- list/detail/steps 使用数据库稳定排序、精确 total/limit/offset，不全量 ORM 装载后内存切片。
- viewer 可读；auditor/configurator 可触发，复用既有 permission 数据，不以角色字符串硬编码。未登录/无权限/跨租户分别稳定为 401/403/404。
- 所有响应 `Cache-Control: private, no-store`；schema 使用 Pydantic discriminated union，OpenAPI 是前端类型的唯一来源。
- API 不返回完整 prompt、HMAC/source fingerprint、key hash、provider secret、原始 PII 或 chain-of-thought。

---

## 10. 桌面 UI

- 在 correlation finding detail 中增加独立“异常取证”面板，不改 F5 queue 和 F6 candidate 语义。
- 展示 capability、显式 run/finding identity、调查历史、模型/agent 版本、步骤时间线、已调用工具的脱敏输入/事实摘要、最终 outcome 与人工回退说明。
- 不显示“思考过程”；UI 文案明确步骤摘要不是模型隐藏推理，也不把 `sufficient` 表述成已确认违规。
- auditor/configurator 在确认后触发；viewer 只读且不得发 POST。提交使用稳定 idempotency key，同一次 pending/retry 复用 key。
- 状态至少覆盖：capability loading/error/unavailable、无历史、运行中/恢复、sufficient、insufficient、max_steps、failed、conflict、长步骤分页和引用校验失败。
- 仅桌面 1440×1000；恶意 tool output/policy text 只能作为 React 文本节点呈现，不执行 HTML/script/image，不写 local/session storage。

---

## 11. 测试矩阵与机械门禁

### 11.1 迁移与数据库

- `0009` 在 evidence skeleton 非空时于任何 DDL 前失败；空库 upgrade、精确 downgrade、重复 upgrade、默认/测试双库 head 和 Alembic 零漂移。
- 复合 FK 的 file/run/finding/tenant/actor 正反例；跨租户伪造全部失败。
- request、step、result、token unique/check/byte limit/RESTRICT/immutability trigger 正反例。
- downgrade guard 查询全部 F7 事实表；备份 hash、可读性与隔离恢复通过。

### 11.2 Provider、PII 与工具

- scripted provider 的 tool/terminate/error 序列完全确定；HTTP mock 覆盖 request/response、timeout、429、5xx、坏 JSON、schema invalid 和重试耗尽。
- 全测试网络哨兵证明真实模型调用为 0；fixtures 不含真实 key 或客户数据。
- 同 tenant 同主体 token 一致、跨 tenant/kind/version 不同；碰撞、缺 key、未知字段、已知 PII 出站均 fail closed。
- 四工具的 tenant 注入、token 作用域、limit/date/cursor、稳定顺序、恶意参数、跨租户和响应字节上限。
- policy tool 的 effective-date、内网 provider 限制、检索降级和机械逐字引用；伪造/截断/跨版本 quote 不得进入 result。

### 11.3 编排、幂等与恢复

- tool→tool→terminate、立即 terminate、insufficient、unavailable、max steps、provider/tool failure 五类终止路径。
- 同 key replay、同 key 异 payload、并发同 key、不同 key 同 candidate 历史并存。
- 在 run/request、模型前后、工具前后、step insert、result/audit 前后注入故障；fresh-process restart 后无重复业务事实。
- hard process exit；checkpoint 领先/落后/缺失/损坏；result replay 的 provider/tool 调用严格为 0。
- step 连续、fingerprint 冲突 fail closed；未知工具和 prompt 注入不能产生白名单外调用或任何写副作用。

### 11.4 API、前端与契约

- 401/403/404/409/422/503、三角色、跨租户、分页/排序/total、cache header 和错误无 PII。
- OpenAPI 导出与 generated client 连续两次字节稳定，无手写漂移。
- 前端 normal/empty/loading/error/conflict/unavailable/sufficient/insufficient/max_steps/failed、历史分页、恶意文本和 viewer 零 POST。
- 真实 Chrome 1440×1000：页面级横向溢出 0、非模块 script/image 执行 0、恶意标记 0、local/session storage 0；证据只落 `data/private/`。

### 11.5 全量与性能

- 后端 `pytest`、`ruff check .`、`ruff format --check .`、`mypy app scripts`。
- 前端 `npm run test`、`npm run typecheck`、`npm run lint`、`npm run format -- --check`、`npm run build`。
- OpenAPI contract、pre-commit/gitleaks、`pip-audit --strict`、`npm audit --audit-level=high`。
- 固定 seed 5000 行 F1→F7 合成链路使用 scripted provider；记录候选数、调查数、step 数、SQL、p95、响应体和总耗时，整体不得突破 900 秒。测试标签与业务输入物理分离。

---

## 12. Checkpoint 计划与退出条件

### CP-F7.0 — 规格固化

- **交付：** 本 Spec 008，覆盖 scope、schema、provider/PII、工具、恢复、API/UI、门禁与外部验收边界。
- **退出：** 实施者无需再决定 evidence FK、终态、幂等权威、网络策略或 F8 边界；`git diff --check` 通过。

### CP-F7.1 — 持久化与迁移

- **交付：** 私有备份、`0009`、ORM、复合 FK、约束、append-only trigger、迁移/恢复测试。
- **退出：** 空 skeleton preflight、跨租户、不可变、downgrade guard、双库 head、Alembic 零漂移与后端静态门禁通过；不得实现 provider/API/UI。

### CP-F7.2 — Provider、脱敏与四只读工具

- **交付：** action/result schema、provider protocol + OpenAI HTTP/scripted 实现、Settings、stable tokenizer、四工具与安全边界。
- **退出：** 单元/集成测试证明零真实网络、零已知 PII 出站、工具只读/租户隔离、引用忠实；不得暴露 API/UI。

### CP-F7.3 — LangGraph 编排、service、幂等与恢复

- **交付：** graph/state、run service、request/step/result 原子追加、checkpoint reconciliation、terminal audit 与查询 service。
- **退出：** 全部故障点、并发、hard-kill、fresh restart、checkpoint 领先/落后和 completed replay 零调用通过；业务 DB 明确为权威。

### CP-F7.4 — API、OpenAPI 与桌面工作流

- **交付：** capability/trigger/history/detail/steps endpoints、generated client、权限驱动调查面板和全状态桌面 UI。
- **退出：** 路由零 SQL、三角色/跨租户/cache/分页/恶意输入、OpenAPI 二次稳定、前端静态/build 与 Chrome 全状态门禁通过。

### CP-F7.5 — 契约、安全与交付门禁

- **交付：** 第 11 节全部机械证据、固定 seed 5000 行性能结果、网络哨兵、prompt 注入语料、项目状态与实际落地记录。
- **退出：** 所有门禁零退出且结果写入本文件；确认未调用真实模型、未写 F8 grading、未修改旧事实后，才可把 F7 标记完成并自动进入 F8。

---

## 13. `external_validation_pending`

以下事项不阻塞“代码就绪 MVP”，但在真实输入到位前必须保持 `external_validation_pending`，不得描述为生产验收完成：

1. 使用真实 OpenAI-compatible 云 API 的鉴权、延迟、限流、结构化输出兼容性、模型质量与成本冒烟。
2. 使用真实本地 vLLM 路径的兼容性验证。
3. 使用真实本地 embedding/rerank 权重的制度检索质量与资源占用验证。
4. 使用真实客户月度批次的 PII 审批、脱敏抽查、取证质量和人工接受度验证。
5. 实际生产部署、密钥注入、监控告警、发布与回滚演练。

代码交付必须附一个默认不执行的真实 API smoke 入口和配置说明；没有凭据时只做 readiness 检查并返回 unavailable，不得让 CI、测试或本地默认启动尝试联网。

---

## 14. 实际落地记录

### CP-F7.0 实际落地记录（2026-08-10）

- 新增本规格，固定 F7 只能读取显式 F6 detection run/correlation finding，调查事实与 F3 finding、F6 candidate、F8 grading 物理分离。
- 决定由 `0009` 在空 skeleton 前提下把 `evidence_step` 从单行 finding FK 改接 investigation run；旧 skeleton 非空时必须在任何 DDL 前 fail closed。
- 固定 OpenAI-compatible provider + deterministic/scripted 测试路径、租户稳定 PII token、四只读工具、五种终态和业务 facts 优先于 LangGraph checkpoint 的恢复规则。
- 明确外部模型 API 无通用 exactly-once 保证；API 返回至 step 提交之间的崩溃可能重复模型计费，但不能形成重复数据库事实。
- 本检查点只新增规格文件，未创建迁移、ORM、provider、service、API/UI、依赖、基础设施或 F8 行为。

### CP-F7.1–CP-F7.3 实际落地记录（2026-08-10）

- 迁移前已生成 gitignored 私有备份 `data/private/backups/mvp-loop/pre-0009-20260810/expenseguard-pre-0009.dump`；SHA-256 为 `04BDA0D07C6BBC1F53C2D138F00D27C0BE1A074447BAFF3BEB96652A956B56F2`，`pg_restore --list` 与隔离恢复均通过。默认库和测试库均位于 `0009 (head)`，`alembic check` 为零漂移。
- `0009`、ORM 与约束已落地：run/request/result/token/step 使用复合 tenant identity、`RESTRICT`、追加写触发器、evidence skeleton 前置保护和 downgrade guard；迁移与 ORM 正反例全部通过。
- 落地强类型 OpenAI-compatible HTTP provider、无网络 scripted provider、稳定 tenant-scoped HMAC token、出站 allowlist/scanner、四个 tenant-bound 只读工具及 policy retrieval 降级语义。原始 F6 `evidence_json` 不进入模型上下文；自动测试和性能门禁真实模型调用数均为 0。
- run service 实现五终态、request ledger 幂等、step/result/audit 原子追加与业务事实优先的 checkpoint reconciliation。hard-kill、并发、同 key replay/冲突及 completed replay 零 provider/tool/checkpoint 调用已验证；外部模型响应后、step 提交前的 exactly-once 缺口仍按第 7.4 节显式保留。
- FastAPI lifespan 已接入 PostgreSQL `AsyncPostgresSaver`；真实 PostgreSQL saver 的 setup/write/read 冒烟通过。未持有业务事务跨 provider 或工具调用。

### CP-F7.4 实际落地记录（2026-08-10）

- 新增 capability、trigger、history、detail、steps 五组强类型端点；路由经 service 层，具备 permission 驱动 RBAC、tenant 404、稳定分页和 `Cache-Control: private, no-store`。缺 PII key 的触发请求显式返回 503，默认启动与 readiness 不尝试模型网络。
- OpenAPI 与 generated client 连续两次字节稳定；最终 SHA-256 分别为 `74490D46C57D22B6B4BF16204EC6E910D47DDC0919D320687A6AF1A691DB5724` 和 `F43288C3AFA8773FAD0CCE8D7B34377B931A22053F920E09F29E022839BAF4EE`。
- finding detail 已增加权限驱动“异常取证”面板，顺序位于 typed statistical evidence 之后、物理参与行之前。覆盖 unavailable/empty、五终态、冲突、错误、viewer 零 POST 与恶意文本；外部内容仅按 React 文本节点呈现。
- Chrome 1440×1000 共验证 15 个 auditor/viewer/终态/恶意文本场景：document/body 横向溢出、script/image 节点执行、恶意标记和 local/session storage 均为 0。截图与指标仅保存于 `data/private/cp-f7.5/`。

### CP-F7.5 实际落地记录（2026-08-10）

- F7 定向核心测试 `295 passed`；后端全量 `506 passed, 1 skipped`；Ruff、211 文件格式检查及 strict mypy（145 个源文件）通过。前端 14 个测试文件 `57 passed`，typecheck、oxlint、Prettier 与生产 build 通过；pre-commit/gitleaks 和 `pip-audit --strict` 通过。
- 固定 seed=3500 的 5000 行 F1→F7 链路复用 F6 的 130 个 candidate，并以 scripted provider 完成 130 次调查、131 个 evidence step；F7 用时 `8.167681s`，run p95 `0.058576s`、最大 `0.128931s`，completed replay p95 `0.010836s` 且 provider 调用为 0；F1→F7 总耗时 `115.321029s`，低于 900 秒硬上限。结果位于 `data/private/cp-f7.5/perf-5000-result.json`。
- 默认 `model_api_smoke.py` 在 provider disabled 时退出码为 2，输出 `PROVIDER_DISABLED` 且 `network_attempted=false`；真实云 API、本地 embedding/rerank、客户批次与生产部署继续标记 `external_validation_pending`。
- 依赖审计例外：`npm audit --omit=dev --audit-level=high` 为 0；完整审计仍报告 2 条仅开发期的 `js-yaml` 高危公告，路径为 `openapi-typescript -> @redocly/openapi-core 1.x`。尝试升级 Redocly 2.x 或强制 js-yaml 5 均会破坏契约生成器，当前无兼容上游修复；该工具只解析仓库自行生成的 `openapi.json`，风险被窄化接受并在 `frontend/package.json` 留有跟踪说明，不得宣称完整 npm 审计全绿。
- 未调用真实模型、未写入任何 F8 grading 事实、未改写 F3/F4/F5/F6 既有事实；F7 CP-F7.0–CP-F7.5 至此闭包。
