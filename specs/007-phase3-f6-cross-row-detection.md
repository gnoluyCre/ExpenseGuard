# Spec 007 — Phase 3 F6 跨行关联检测（统计层）

**状态：** CP-F6.0 规格固化完成 ✅
**日期：** 2026-07-29
**前置检查点：** F5 / CP-F5.5 已完成
**下一实施检查点：** CP-F6.1 持久化 schema 与 ORM

---

## 1. 目标与范围

F6 在 F2 已冻结的结构化解析结果上运行四类纯确定性统计检测器，发现单行规则无法识别的关联候选，并为每个候选保存全部参与行与可机械复算的证据。F6 是“统计挖候选 → F7 只读取证 → F8 二维分级”链路的第一段；本阶段只陈述统计事实，不把候选伪装成已证实违规。

四类 detector 固定为：

1. `split_invoice`：同一主体、商户、币种和配置日期窗口内，若多笔阈值邻近金额的合计达到审批阈值，则形成拆单候选。
2. `sequential_invoice`：按配置的精确分区键，在发票号的固定数字后缀中识别最大连续序列。
3. `frequency_anomaly`：在固定日历周期内，以精确有理数的 median/MAD 基线识别员工报销频次离群。
4. `spatiotemporal_tier0`：同一员工同日出现在配置明确声明为不相容的地点分区，形成时空冲突候选。

F6 的核心交付是以下可机械验证的不变量：

1. 相同 file revision、相同规范化输入快照、相同 detector config fingerprint 和相同算法版本，产生完全相同的 capability declarations、finding keys、参与行集合、证据 JSON 与稳定顺序。
2. 每次成功运行恰好产生四条能力声明；`enabled`、`degraded`、`unavailable` 都是正式产出，缺数据或样本不足不能静默当作“未发现异常”。
3. 每条关联候选至少包含两行，全部参与行必须由数据库复合外键证明属于同一 tenant/file revision；不能只相信 JSON 或整数数组。
4. 配置、运行、能力声明、关联候选、参与行和成功请求账本均为追加且不可变的事实；重放、并发和崩溃恢复后，同一运行的业务副作用最多一次。
5. F6 不调用 LLM、Qdrant、embedding、rerank、外部地图或网络；字段值不离开 PostgreSQL、服务进程和授权浏览器。
6. F6 不修改 F3 `row_result/finding`、F4 report/citation/export、F5 plan/sample/review，也不填写 F8 的 severity 结论。

### 1.1 明确不在 F6 范围内

- 不实现 F7 ReAct agent，不创建 `evidence_step`，不判断“证据是否充分”，不自动查制度或员工历史。
- 不实现 F8 `severity_impact/severity_confidence` 合成、风险分数或代价敏感阈值；legacy `correlation_finding` 两列在 F6 固定为 `0`，其唯一语义是“尚未分级”的兼容哨兵，API/UI 禁止把它展示、筛选或排序为真实 severity。F8 必须创建独立不可变 grading snapshot，不得 UPDATE F6 候选。
- 不把 correlation candidate 自动放入 F5 finding review 或 clearance sample，不产生 `confirmed/false_positive` 标签。
- 不回写或重新生成 F4 不可变报告/XLSX；F6 在页面上作为独立的“关联检测补充快照”呈现。
- 不实现时空冲突 Tier 1（出差申请/行程对照）、地理编码、距离/速度推断或商户地址联网补全；Tier 1 仍为显式外部接口缺口。
- 不做模糊姓名/商户合并、拼音匹配、编辑距离、无配置的地点猜测或任意正则执行。
- 不跨 file revision、root revision、租户或历史月份自动关联；跨批次历史取证属于 F7 的只读工具范围。
- 不做配置自动推荐、阈值自动标定、置信区间、评测集自动导出或模型训练。
- 不做移动端、平板、通知、任务分派、批量复核或新的导出格式。
- 不修改 `0001`–`0007`、`docker-compose.yml`、Dockerfile 或 CI workflow。

### 1.2 对旧设计与骨架的覆盖决定

- 覆盖旧 `correlation_finding.participating_row_nos ARRAY` 作为唯一身份依据的设计：CP-F6.1 新增 `correlation_finding_row`，以复合外键绑定 finding/run/file/tenant 与 `expense_row(file_version_id,row_no,tenant_id)`；API 参与行数组由该表按 `row_no` 重建。
- 覆盖旧表缺少 run/config 身份、允许同批同 detector 任意重复的设计：每个 finding 必须绑定 immutable `detection_run`，并由 unique `(detection_run_id, detector, finding_key)` 去重。
- 覆盖 `capability_declaration` 仅有 file/detector/reason 文本的设计：能力声明必须绑定 detection run 和 config fingerprint，并保存稳定 `reason_code` 与无 PII 的结构化 details；一个 run 每种 detector 恰好一条。
- 覆盖“能力只由字段状态决定”：字段可用性给出上限，实际 eligible coverage、地点映射覆盖、频次总体规模等运行时前置条件只能维持或降低状态，绝不能升级状态。
- 覆盖“缺字段就跳过 detector 且不落记录”：四种 detector 即使被配置关闭、缺字段、样本不足或零命中，也必须落 capability declaration。
- 覆盖“对拆单枚举所有金额子集”：F6 只做稳定分组与线性扫描，不做指数级 subset-sum，不声称找出唯一真实拆单组合。
- 覆盖“任意发票号可取数字做连号”：只接受整个已规范化发票号末尾的 ASCII 数字后缀，前缀和数字宽度均参与分区；无法无歧义解析的行显式计入排除原因。
- 覆盖“地点字符串不同即冲突”：只有 configurator 明确配置的 exact alias → zone 与无向 incompatible zone pair 才能形成冲突；未知地点不得猜测。
- 旧 F6 skeleton 表若已有业务数据，CP-F6.1 upgrade 必须在任何 DDL 前 fail closed；不得猜造 config/run/fingerprint 来迁移历史候选。

---

## 2. 术语与全局不变量

| 术语 | 定义 |
|---|---|
| detector profile | 租户级、连续版本、一次冻结四类 detector 参数的不可变配置 |
| detection run | 一个 file revision 与一个 profile fingerprint 的成功、不可变运行快照 |
| capability declaration | 某个 run 中某个 detector 实际能做什么的正式声明 |
| correlation finding | 统计 detector 产出的关联候选，不等于已确认违规 |
| participating row | 构成候选证据的原始数据行；必须有物理复合 FK |
| eligible row | 成功解析且满足该 detector 行级必需字段/值域的行 |
| excluded row | 成功解析但因稳定原因不能进入该 detector 的行 |
| finding key | 对 detector/version/config/参与行/规范化证据做 canonical SHA-256 得到的稳定身份 |

全局不变量：

- tenant 只从认证 session 注入；API 不接受 tenant ID。
- F6 只消费当前 file revision 的 F2 `NormalizedExpenseRecord(schema_version=1)`、`field_availability` 和解析错误身份；原始 JSON 仅供 detail 展示，不参与检测逻辑。
- parse error 行永不进入候选，但计入 input/coverage 分母与 `PARSE_ERROR` 排除计数；不得静默丢弃。
- 所有金额使用 `Decimal`，日期使用语义有效 `date`，频次统计使用整数与 `Fraction`；禁止二进制浮点、数据库 `random()`、Python `hash()`、当前时间或 SQL 物理顺序影响结果。
- 文本分组只使用 F2 已做 NFKC/空白处理后的精确值及本 profile 的 exact alias；不得二次模糊规范化。
- detector profile fingerprint 不含 ID、版本、创建人、时间或 change reason；request fingerprint 另绑定 tenant、expected version、配置 fingerprint 与 change reason SHA-256。
- detection input fingerprint 至少绑定 file/version、mapping version、按 row_no 排序的 normalized/parse-error 身份以及 12 项 field availability 快照；运行重放先核对该 fingerprint。
- 已提交 profile/run/declaration/finding/participating-row/request/audit 不得 UPDATE/DELETE。
- 系统错误、数据库错误和实现异常必须使整次 run 失败；不能伪装成 `unavailable`。`unavailable` 只表示已知、可解释的数据或配置限制。

---

## 3. 端到端流程与既有功能边界

### 3.1 配置 detector profile

1. configurator 读取 current/history profile，在桌面表单中显式填写四类 detector 配置、`expected_current_version` 和 change reason。
2. 服务在 Tenant `FOR UPDATE NOWAIT` 锁内校验判别联合、alias/pair 一致性、数值边界和 canonical JSON，创建下一连续版本。
3. 同一事务写 profile、`detection.config_create` 无 PII 审计；同 key 同请求复用，同 key 异请求或 expected version 陈旧返回稳定 409。
4. 系统不提供隐式业务默认 profile。没有 version 1 时运行返回 `DETECTION_CONFIG_REQUIRED`。

### 3.2 执行 detection run

1. auditor/configurator 对已完成 F2 解析的 file revision 显式触发 detect。
2. 服务按 Tenant → FileVersion `FOR UPDATE NOWAIT` 锁序读取 current profile，并构造 input fingerprint。
3. 如果 `(file_version, profile_fingerprint)` 已有 completed run，则只读复用；新的 Idempotency-Key 追加 key→run alias，不重算 detector、不新增 finding 或 success audit。
4. 首次运行在内存中先完成四类纯函数计算与全量 Pydantic 校验，再在同一事务写 run、四条 declaration、全部 finding/row links、request ledger 与一次 `detection.run_complete`。
5. 任一步失败全部回滚；失败审计用独立短事务只写稳定 reason code、file/profile ID 与 hash，不写字段值或候选证据。

### 3.3 读取与展示

- batch 页/报告页只读装配最近一次实际 completed run，并同时比较 current profile fingerprint；若不同，明确显示 `config_stale`，不隐式重跑。
- finding list 只返回摘要、参与行数和有限 row preview；detail 分页读取全部 `correlation_finding_row` 与原始/规范化行，避免单个高频候选造成超大列表响应。
- F4 report、XLSX 和 F5 queue 保持字节级/语义级不变；F6 区域明确标注“统计候选，待人工/Agent 取证”。
- F7 后续只能读取一个显式 `detection_run_id/correlation_finding_id`，不得默认拼接“最新”候选；F8 后续以独立 grading snapshot 承载分级，禁止更新 F6 候选。

---

## 4. 强类型 profile 与版本语义

### 4.1 公共结构

profile 使用 Pydantic 判别联合，canonical JSON 固定 UTF-8、`sort_keys=True`、紧凑分隔符，Decimal 以无指数十进制字符串表示，集合转为排序且去重的数组：

```text
DetectionProfileDefinition
  schema_version = 1
  algorithm_bundle_version = "correlation-v1"
  detectors = exactly one of each detector kind, fixed enum order
```

每个 detector 共有：

- `type`：四种固定 discriminator 之一；
- `enabled`：显式布尔值；false 映射为 capability `unavailable/CONFIG_DISABLED`，不是删除 detector；
- `min_eligible_rows`：2–5000；
- `min_eligible_rate_bps`：1–10000，以全部 source rows（含 parse error）为分母；
- detector-specific algorithm version：由代码常量决定并写入 run/finding，不允许 API 调用方伪造。

四类配置全部必填；不允许只提交局部 patch。profile version 在 tenant 内从 1 连续递增，允许未来显式回退到与历史相同的参数，因此 profile fingerprint 不做 tenant 级唯一约束。

### 4.2 `split_invoice`

```text
type = "split_invoice"
approval_thresholds: currency code -> positive Decimal string
currency_mode: "field" | "fixed"
fixed_currency: required only for fixed mode
aggregate_operator: "gt" | "gte"
individual_floor_bps: 1..9999
date_window_days: 0..31
min_rows: 2..50
merchant_aliases: exact normalized merchant -> canonical merchant key
```

- hard dependencies：`amount`、`expense_date`、`employee`、`merchant`；`currency_mode=field` 另需 `currency`。
- fixed currency 是 configurator 对数据源币种的明确声明，不是运行时猜测；若行内出现与 fixed currency 冲突的非空币种，该行以 `CURRENCY_CONFLICT` 排除并降低 capability。
- threshold 必须覆盖运行中所有 eligible currency；未配置币种不能换算或套用其他币种阈值。`aggregate_operator` 显式决定合计金额严格大于还是大于等于 threshold，禁止代码内隐藏边界。

### 4.3 `sequential_invoice`

```text
type = "sequential_invoice"
min_sequence_length: 2..50
numeric_suffix_min_digits: 1..32
numeric_suffix_max_digits: min..32
partition_fields: ordered unique subset of employee | merchant | invoice_type
```

- hard dependency 始终含 `invoice_no`，再加 profile 显式选择的 partition fields；空 partition 是允许但必须由 configurator 明确保存的跨员工/商户扫描语义。
- 不接受任意 regex。parser 只把整个规范化值拆为“非空或空 prefix + 末尾 ASCII digits”；suffix 长度必须在配置范围内，prefix 与 suffix width 都参与序列分区。

### 4.4 `frequency_anomaly`

```text
type = "frequency_anomaly"
period: "calendar_week_monday" | "calendar_month"
min_population: 3..5000
absolute_min_count: 2..5000
mad_multiplier: positive Decimal string, max 100
mad_floor: positive Decimal string, max 5000
```

- hard dependencies：`employee`、`expense_date`。
- population 是同一日历 period 内拥有至少一条 eligible row 的 distinct employee 数；不同 period 独立建立基线，禁止把跨月数据混成一个总体。
- `mad_floor` 是 profile 的显式稳健分母下限，用于 MAD=0/过小总体，不是隐藏 fallback。

### 4.5 `spatiotemporal_tier0`

```text
type = "spatiotemporal_tier0"
location_aliases: exact normalized location -> canonical zone id
incompatible_zone_pairs: unique unordered pairs of distinct zone ids
min_zone_mapping_rate_bps: 1..10000
```

- hard dependencies：`employee`、`expense_date`、`location`。
- zone ID 为 1–64 字符的稳定配置标识，不存坐标推断；pair canonicalize 为 `(min,max)` 后排序去重。
- 每个 pair 的 zone 必须至少被一个 alias 引用；alias key/value 不能为空，禁止自冲突 pair。
- profile 不含出差申请、时间戳、距离或速度参数；这保证 Tier 0 不冒充 Tier 1。

整个 profile canonical JSON 不得超过 256 KiB；单个 alias map 最多 5000 项，incompatible pairs 最多 10000 项。超限是 `DETECTION_CONFIG_INVALID`，不得在运行时截断。

---

## 5. 能力声明机制

### 5.1 状态推导

每个 detector 按以下顺序推导，状态只能向下：

1. `enabled=false` → `unavailable/CONFIG_DISABLED`。
2. 任一 hard dependency 的 F2 status 为 `missing` → `unavailable/REQUIRED_FIELD_MISSING`。
3. 否则任一 dependency 为 `inferred` → 初始 `degraded/INFERRED_FIELD_USED`；全为 `available` → 初始 `enabled/READY`。
4. 计算行级 eligibility：eligible rows 少于 `min_eligible_rows` 或 eligible rate 低于配置 → `unavailable/INSUFFICIENT_ELIGIBLE_ROWS`，不运行算法。
5. 运行时前置条件可继续降低：
   - split 出现无 threshold 币种且剩余覆盖不足 → unavailable，否则 degraded；
   - frequency 没有任何 period 达到 `min_population` → unavailable；部分 period 被跳过 → degraded；
   - spatiotemporal zone mapping rate 低于配置 → unavailable；低于 100% 但达标 → degraded；
   - sequential 存在不可解析号 → degraded，但只要剩余 eligible coverage 达标仍运行；重复 serial 不排除，只是不增加 distinct sequence length。

优先级固定为 `unavailable > degraded > enabled`；reason code 取固定优先级中的第一项，全部成因仍以排序数组写入 details。不能因为零 finding 把 enabled 改成 unavailable，也不能因为找到 finding 把 degraded 升级为 enabled。

### 5.2 declaration 内容

每条 declaration 至少冻结：

- run/file/profile ID、profile fingerprint、detector/version；
- status、primary reason code、面向人的稳定中文 reason snapshot；
- 依赖字段及各自 F2 status；
- source/parsed/eligible/excluded counts 与 eligible rate；
- 按稳定 reason code 排序的 exclusion counts；
- detector-specific runtime counts（period population、zone mapping、serial parse、duplicate serial 等）；
- finding count。

details 禁止 raw/normalized row、员工/商户/发票/地点原文、alias 原文、配置 change reason 或 Idempotency-Key。字段值证据只在 finding/detail 授权路径中读取。

---

## 6. 四类稳定检测算法

### 6.1 公共预处理

1. 输入按 `row_no ASC` 验证，无重复 row_no；每行严格解析 `NormalizedExpenseRecord`。
2. 对每个 detector 按配置构造 eligible/excluded，并累计稳定 reason code；算法只接收不可变 tuple。
3. group key 中的敏感文本不直接写 evidence；使用 canonical JSON 后 SHA-256 的 `group_key_fingerprint`。UI 从参与行回读原值。
4. detector 输出先通过严格 Pydantic schema 校验，再按 §7 计算 finding key、去重和排序。

### 6.2 拆单 `split-window-v1`

1. 行金额必须为正、严格小于对应币种 `approval_threshold`，且满足 `amount * 10000 >= threshold * individual_floor_bps`；其余行以稳定原因排除。
2. 按 `(employee exact, canonical merchant, currency)` 分区，分区内按 `(expense_date,row_no)` 排序。
3. 从最早未消费行作为 window anchor，贪心纳入 `expense_date - anchor_date <= date_window_days` 的后续行；超出窗口即结束当前窗口并从该行开始新窗口。每行恰好属于一个 window，禁止重叠滑窗和 subset-sum。
4. window 的行数不少于 `min_rows`，且金额合计满足 profile 的 `aggregate_operator` 与 threshold 时，产出一个包含该 window 全部 eligible rows 的候选。
5. evidence 冻结 currency、threshold、floor bps、date range、row count、canonical amount list 与 total；不声称这些行必然是一组真实拆单。

该算法为 O(n log n) 排序 + O(n) 扫描。日期窗口边界属于配置语义；若未来需要重叠滑窗或跨商户图聚类，必须升级 detector version，不能静默改变 v1。

### 6.3 连号 `invoice-sequence-v1`

1. 对 invoice_no 从末尾提取 ASCII numeric suffix；prefix、suffix width 与显式 partition field values 构成 partition key。
2. 同一 partition 内按 `(serial integer,row_no)` 排序，并聚合为 `serial → ordered rows`。重复 serial 不增加连续长度，但若该 serial 属于命中链，其全部行都进入参与行；F3 发票号查重仍是重复事实的负责模块。
3. 对 distinct serial 取差值恰好为 1 的最大链；长度不少于 `min_sequence_length` 时产出一个候选。不同最大链不重叠。
4. evidence 冻结 prefix fingerprint、suffix width、起止 serial 的等宽十进制字符串、sequence length 与 ordered row nos；原发票号只从授权 detail 回读。

算法不把中间缺号当连续，不把 Unicode 数字、内部数字片段或不同 prefix/width 强行拼接；重复号码本身不构成连号，但也不会掩盖其前后连续链。

### 6.4 频次异常 `frequency-mad-v1`

1. 按配置日历周期和 employee exact value 分组，得到 count 与 ordered row nos。
2. 每个 period 仅在 distinct employee population `>= min_population` 时建基线；否则该 period 计入 `INSUFFICIENT_POPULATION`。
3. 用 `Fraction` 精确计算 counts 的 median 与 absolute deviations 的 MAD，不经过 float 或数据库统计近似。
4. 对每个 employee-period，若 `count >= absolute_min_count` 且
   `count - median > 0` 且
   `(count - median) / max(MAD, mad_floor) >= mad_multiplier`，产出一个包含该员工该周期全部 eligible rows 的候选。
5. evidence 冻结 period key、population、count、median、MAD、effective denominator、multiplier 与比较两侧的 canonical rational/decimal 值。

等于阈值时命中；低于/等于 median 不命中。相同 counts、偶数总体、MAD=0、跨年 ISO week 与月末边界都必须有 golden vectors。

### 6.5 时空冲突 `spatiotemporal-pair-v1`

1. 用 exact `location_aliases` 将行地点映射到 zone；未知地点计入 `LOCATION_UNMAPPED`，不得模糊匹配。
2. 按 `(employee exact, expense_date)` 分组，聚合 zone → ordered rows。
3. 枚举该组实际出现 zone 的 canonical unordered pairs，仅当 pair 存在于 `incompatible_zone_pairs` 时命中。
4. 同一 employee-date 只产出一个候选，参与行是所有命中 pair 所涉及 zone 的全部行，按 row_no 去重排序；evidence 保存排序后的命中 zone pairs、日期和 zone→row count，不复制员工/地点原文。
5. 同 zone 多笔、未知 zone、未配置 pair 或仅一行不命中。

该结果只表示“同员工同日出现配置上不相容的地点分区”，不表示已证明物理不可能，也不推断出差违规。

---

## 7. 证据、指纹、去重与稳定排序

### 7.1 通用 evidence envelope

每个 detector 使用判别联合的 evidence schema，公共字段固定为：

```text
schema_version = 1
detector
detector_version
profile_fingerprint
group_key_fingerprint
facts                 # detector-specific typed object
reason_code = "STATISTICAL_CANDIDATE"
```

不得在 evidence 中保存自然语言模型结论、制度引用、severity、review decision、原始整行 JSON 或未经配置的推断。面向人的 reasoning 由稳定模板基于 typed facts 生成；模板文字不参与 finding identity。

### 7.2 finding key

```text
finding_key = SHA256(
  UTF8("expenseguard-correlation-finding-v1\0")
  + canonical(detector)
  + canonical(detector_version)
  + canonical(profile_fingerprint)
  + canonical(sorted participating row_nos)
  + canonical(typed evidence facts)
)
```

- 不含数据库 UUID、created_at、reasoning 文案或调用顺序。
- 同一 run 内先按 key 去重；key 碰撞但 canonical payload 不同必须 fail closed，不能任取一条。
- DB unique `(detection_run_id,detector,finding_key)` 是最终防线。

### 7.3 稳定输出顺序

detector 顺序固定为 `split_invoice → sequential_invoice → frequency_anomaly → spatiotemporal_tier0`；detector 内按 `(first_row_no, participating_row_nos lexicographic, finding_key)`。API 只允许枚举 sort，不接受任意数据库列名；默认顺序与此一致，分页必须数据库执行并有稳定 tie-breaker。

---

## 8. 持久化计划（CP-F6.1 输入）

### 8.1 迁移策略

CP-F6.1 只新增 `0008_f6_cross_row_detection.py`，不得修改 `0001`–`0007`。默认开发库升级前必须停写并在 `data/private/backups/cp-f6.1/` 创建 full/schema/affected-data 可恢复备份，完成 `pg_restore --list` 与容器/本地 SHA-256 交叉验证。

upgrade 在任何 DDL 前检查旧 `correlation_finding`、`capability_declaration` 是否为空；非空即 fail closed。downgrade 在任一 F6 profile/run/request/declaration/finding/row 存在时于任何 DDL 前拒绝。所有新 FK 使用 `RESTRICT`，不得弱化 `row_result(file_version_id,row_no)`、`sampling_audit(file_version_id,row_no)` 或任何追加写触发器。

### 8.2 新增实体

`detection_config` 至少保存：

- tenant、连续 version、definition JSON、schema/bundle version、config fingerprint；
- created_by/created_at、change reason；
- idempotency key hash、request fingerprint；
- unique `(tenant_id,version)`、unique `(tenant_id,idempotency_key_hash)`；
- definition/fingerprint/hash 长度与版本 CHECK、复合 tenant/user FK、UPDATE/DELETE 拒绝触发器。

`detection_run` 至少保存：

- tenant/file/config IDs 与 config version/fingerprint snapshot；
- algorithm bundle version、input fingerprint、run fingerprint；
- source/parsed/error/finding counts、created_by/created_at/completed_at；
- unique `(file_version_id,config_fingerprint)`、完整复合 tenant/file/config/user FK；
- count 恒等式、hash 长度、版本一致性 CHECK 与 UPDATE/DELETE 拒绝触发器。

`detection_request` 保存 tenant/file/run、idempotency key hash、request fingerprint 与 created_at；unique `(tenant_id,idempotency_key_hash)`，完整复合 FK，UPDATE/DELETE 拒绝。首次运行与后续 alias key 都经 ledger；ledger 不保存明文 key。

### 8.3 强化 capability 与 correlation finding

`capability_declaration`：

- 新增 `detection_run_id`、config fingerprint、detector version、reason code、details JSON、finding count；
- unique `(detection_run_id,detector)`，复合绑定 run/file/tenant；
- detector/status/reason/hash/count CHECK；UPDATE/DELETE 拒绝；
- 旧 unique `(file_version_id,detector)` 替换为 run 级 unique，以允许同一 file 在新 profile 下显式重跑。

`correlation_finding`：

- 新增 `detection_run_id`、detector version、finding key、evidence schema version、reasoning snapshot；
- unique `(detection_run_id,detector,finding_key)`，复合绑定 run/file/tenant；
- detector/version/hash、evidence object、`severity_impact=0 AND severity_confidence=0` CHECK；
- legacy `participating_row_nos` 在确认 skeleton 空表后移除；UPDATE/DELETE 拒绝。

`correlation_finding_row`：

- finding/run/file/tenant IDs、row_no、ordinal；
- unique `(finding_id,row_no)`、unique `(finding_id,ordinal)`；ordinal 从 1 连续且对应 row_no ASC，由服务验证；
- 一条复合 FK 绑定 correlation finding identity，另一条复合 FK 绑定同 tenant/file 的 expense row；全部 RESTRICT；UPDATE/DELETE 拒绝。

为支撑上述 FK，只允许在当前表上增强必要的冗余 unique，不得删除或放宽既有约束。四类 detector 每 run 恰好一条 declaration 属于服务事务不变量，并由 completed run 写入前的 count/集合验证及集成测试证明；不使用跨表延迟触发器隐藏业务逻辑。

---

## 9. 服务、事务、幂等与恢复

### 9.1 分层

- `backend/app/core/detection/models.py`：profile/evidence/capability/finding 严格类型与 canonical schema。
- `canonical.py`、`capability.py` 与 `detectors/*.py`：纯函数，不访问 DB、网络、环境变量或当前时间。
- `config_service.py`、`run_service.py`、`query_service.py`：租户过滤、锁、快照装载、持久化和审计。
- FastAPI 路由只处理 request/response、权限与依赖注入，不直接查数据库。
- 前端只消费 OpenAPI 生成类型；配置表单使用 Zod，禁止 `any`。

### 9.2 锁序与原子事务

所有 F6 mutation 使用：

```text
Tenant FOR UPDATE NOWAIT
  → FileVersion FOR UPDATE NOWAIT（run 时）
  → existing DetectionRun/Request（需要时，稳定 UUID 顺序）
```

profile 保存只锁 Tenant。锁冲突稳定返回 409 `DETECTION_CONFLICT`，不等待至 HTTP 超时。

首次 run 必须在一个事务提交 run/declarations/findings/row links/request/success audit；不分 detector commit，不保留 partial run。纯计算在写入前完成，但在同一 Tenant/FileVersion 锁和事务边界内再次核对 input/config fingerprint，避免计算期间输入身份漂移。

### 9.3 Idempotency-Key

profile PUT 与 detect POST 要求 8–128 字符 key，只存 SHA-256：

- 同 key + 同 canonical 请求：200 返回既有 profile/run，不新增事实或审计。
- 同 key + 不同请求：409 `IDEMPOTENCY_KEY_REUSED`。
- 新 detect key + 同 file/profile fingerprint 已完成：200 复用 run，仅追加 key→run request alias；不重算、不新增 success audit。
- 新 key + profile 已变化：创建新的 run；旧 run 永久可读。
- 并发 unique violation 在 savepoint/领域异常中归一化，不泄漏 SQL、证据或字段内容。

### 9.4 失败与恢复

- kill 发生在任何 declaration/finding/row/audit 写入点时，主事务全回滚；重试产生同一 finding keys 和输出。
- completed replay 不读取 current config 以替换历史 snapshot，不调用 detector、Qdrant、模型、CSPRNG 或当前时间。
- 若已提交 run 的 input fingerprint 与重算身份不符，返回 `DETECTION_INPUT_DRIFT` 并 fail closed；不得更新历史 run。
- detector 纯函数抛出未分类异常时整批失败并记录无 PII `detection.run_failed`，不能只丢弃该 detector。
- 不通过删除 partial rows、UPDATE completed facts、放宽约束或跳过恢复测试来修复失败。

---

## 10. API 契约

所有错误统一 `{error:{code,message}}`；跨租户资源返回 404。raw/normalized/evidence/detail 响应使用 `Cache-Control: private, no-store`。

### 10.1 Config 与 run

| Method | Path | Permission | 语义 |
|---|---|---|---|
| GET | `/api/detection/configs` | `CONFIG_READ` | current/history profile，支持稳定分页 |
| PUT | `/api/detection/configs` | `CONFIG_WRITE` | expected version 创建下一不可变 profile；要求 Idempotency-Key |
| POST | `/api/batches/{file_version_id}/detect` | `BATCH_IMPORT` | 以 current profile 创建/复用 run；要求 Idempotency-Key |
| GET | `/api/batches/{file_version_id}/detection` | `BATCH_READ` | 最近实际 run、四项 capability、current/stale 状态 |
| GET | `/api/detection-runs/{run_id}` | `BATCH_READ` | 显式 run/config/input/count snapshot |

### 10.2 Finding 查询

| Method | Path | Permission | 语义 |
|---|---|---|---|
| GET | `/api/detection-runs/{run_id}/findings` | `BATCH_READ` | detector/capability 过滤、稳定分页的候选摘要 |
| GET | `/api/correlation-findings/{finding_id}` | `BATCH_READ` | typed evidence、reasoning、参与行分页与原始/规范化详情 |

list 默认 limit 50（1–200）、offset 非负；detector/status/sort 使用枚举。finding detail 的参与行分页独立使用 `row_limit/row_offset`，并返回 `{completed,total}` 式精确计数，不在 list 响应复制全量行。

### 10.3 稳定错误码

| HTTP | code 示例 |
|---|---|
| 401 | `AUTH_REQUIRED` |
| 403 | `PERMISSION_DENIED` |
| 404 | `BATCH_NOT_FOUND`, `DETECTION_RUN_NOT_FOUND`, `CORRELATION_FINDING_NOT_FOUND` |
| 409 | `DETECTION_CONFIG_REQUIRED`, `DETECTION_CONFIG_VERSION_CONFLICT`, `DETECTION_PREREQUISITE_REQUIRED`, `DETECTION_CONFLICT`, `DETECTION_INPUT_DRIFT`, `IDEMPOTENCY_KEY_REUSED` |
| 422 | `DETECTION_CONFIG_INVALID`, `REQUEST_VALIDATION_ERROR` |
| 500 | `DETECTION_RUN_FAILED` |

Pydantic schema 是唯一 API 事实来源；四类 config/evidence/declaration 使用 discriminator。模型变更后导出 OpenAPI 并生成前端 client，连续二次运行必须无 diff。

---

## 11. 权限、审计与安全

### 11.1 RBAC

- auditor：`CONFIG_READ`、`BATCH_IMPORT`、`BATCH_READ`，可查看 profile、触发 run、查看候选。
- configurator：auditor 全部能力 + `CONFIG_WRITE`，可创建 profile。
- viewer：只有既有 `BATCH_READ/REPORT_READ` 能力时可只读查看已完成 run；不能触发 detect 或修改配置。

沿用 permission 数据，不新增角色 if-else。前端只按 `/api/auth/me.permissions` 控制体验；后端每个 endpoint 独立鉴权。

### 11.2 审计白名单

至少包括：

- `detection.config_create`
- `detection.run_complete`
- `detection.run_failed`

payload 只含 tenant 内对象 ID、detector enum、版本、status/reason code、fingerprint/hash 与计数。禁止 raw/normalized row、employee/merchant/invoice/location、alias 原文、evidence/reasoning、change reason 原文、Idempotency-Key 明文、数据库异常、路径或 secret。

### 11.3 PII 与输入安全

- F6 完全不调用 LLM；因此不需要为本阶段创建令牌映射，也不能借机把真实 PII 送入云 API。
- profile 的文本键/值做长度、控制字符和 Unicode 边界校验；保留正常 Unicode，不做会改变精确匹配语义的隐藏 case-fold。
- 不执行 configurator 输入的正则、表达式、JSON Logic、SQL 或代码；所有字段、period 和 detector 都是 enum。
- React 仅以文本节点展示 config/evidence/raw data，不使用未净化 HTML；`<script>`、`<img onerror>`、`javascript:` 与伪系统指令只显示为文本。
- 前端不得把 raw rows、evidence、alias map 或 profile change reason 写入 URL、localStorage/sessionStorage、analytics 或持久化缓存。

---

## 12. 桌面工作流

### 12.1 Detection config

在配置区域新增“关联检测”页：

- 顶部显示 current version/fingerprint、创建人/时间与四 detector 状态摘要；
- 四张强类型配置卡，展示字段依赖、算法版本、参数与 exact alias/pair 表格；
- 保存前展示 canonical 校验、expected version 和“新配置不修改历史 run”的确认；
- history 可只读展开，禁止编辑历史版本或从浏览器伪造 current。

### 12.2 Batch correlation supplement

在批次/报告桌面页新增独立区域：

- 顶部：run/profile/input fingerprint、current/config_stale、四项 capability 与 source/eligible/excluded/finding counts；
- 左侧：detector/status 筛选、稳定分页候选列表；
- 主区：统计候选提示、typed facts/reasoning、全部参与行计数与分页明细；
- unavailable/degraded 必须与“0 finding”视觉和文案区分；
- viewer 只读，auditor/configurator 可在 profile ready 时显式触发 detect；触发前二次确认其会创建不可变快照。

### 12.3 状态与视觉门禁

必须覆盖：config missing/current/history/stale/conflict；run absent/running mutation/completed/error/replayed；capability enabled/degraded/unavailable；finding normal/empty/loading/error；detail 大参与行集合；三角色权限。

精确 1440×1000 Chrome 验证无页面级横向溢出；长 hash/zone ID/发票 suffix 可复制且不遮挡操作。恶意文本不形成 DOM/导航/执行，浏览器持久化为零。F6 只做 desktop，不承诺移动/平板布局。

---

## 13. 验收场景

### 13.1 Profile 与 capability

- 四种 discriminator 恰好各一项；缺项/重复、unknown field、Decimal/基点/长度边界全部 fail closed。
- alias/pair canonical order、重复、未知 zone、自冲突、partition field 重复与 suffix digit 范围。
- 任意字段顺序改变不改变 config fingerprint；任一语义字段变化改变 fingerprint。
- available/inferred/missing 的组合、config disabled、parse error、eligible coverage 临界值、runtime 降级优先级。
- 每个 completed run 恰好四条 declaration；zero finding 仍有 enabled declaration，系统故障不产生 unavailable declaration。

### 13.2 Detector golden vectors

- split：金额/threshold 等号、floor 等号、负数/零/等于阈值、跨币种、alias、日期窗口首尾、贪心非重叠与无 subset enumeration。
- sequential：纯数字/前缀、前导零、宽度不同、Unicode/内部数字、duplicate serial、缺号、最大链、显式 partition。
- frequency：奇偶 median、Fraction MAD、MAD=0/floor、等于 multiplier、周一/跨年 ISO 周、月末、period population 不足。
- spatiotemporal：alias exact、未知地点、pair 无向 canonical、同 zone、多 pair 合并、同行去重、同员工同日边界。
- 输入行/字典/SQL 顺序、Python hash seed、locale/timezone 改变均不改变 golden finding keys/evidence/order。

### 13.3 持久化、幂等与恢复

- 空库/legacy preflight、迁移往返、安全 downgrade、复合 tenant/file/row 错配、RESTRICT 与不可变触发器。
- 同 key 同请求、同 key 异请求、同 file/profile 新 key alias、profile 变化新 run、两触发者并发。
- 在 run/declaration/部分 finding/部分 row/request/success audit 各故障点 kill，重启后全有或全无且最多一个 run/候选集合/成功审计。
- completed replay 不执行 detector、不读 current config、不调用模型/Qdrant/网络；input drift 显式失败。
- DB 直接 UPDATE/DELETE F6 事实被拒绝；既有 F3/F4/F5 受保护约束反向测试继续通过。

### 13.4 API、RBAC、PII 与 UI

- 三角色 × config/run/list/detail 权限矩阵；未登录 401、无权限 403、跨租户 404。
- pagination/filter/sort/tie-breaker、config_stale、四类判别联合、统一 error shape、private/no-store。
- OpenAPI/client 连续二次无漂移；路由无业务逻辑/SQL。
- audit/log/trace 机械扫描不含 raw、employee、merchant、invoice、location、alias、reasoning、key 或 change reason 明文。
- 1440×1000 覆盖 §12.3 全状态、长参与行列表和恶意文本，无页面级横向溢出、脚本执行或浏览器持久化。

### 13.5 合成数据、性能与交付

- 扩展 `backend/app/synth/` 实现拆单、连号、频次、时空冲突四类物理模式；标签存储与输入物理分离，含正负、边界、噪声和字段降级样本，禁止在输入中泄漏标签。
- 固定 seed 5000 行 F1→F2→F3→F4→F5→F6 总耗时仍须 ≤900 秒；单独记录 F6 纯算法/事务/SQL count/time、四 detector eligible/finding counts、list/detail p95 与 response size。
- detect mutation、list/detail 交互 p95 <2 秒；若首轮不达标，必须以 profile 与 SQL 计时定位，不提高产品阈值。
- 后端 pytest/Ruff/format/mypy、双库 Alembic、迁移/恢复/受保护约束、前端 test/typecheck/oxlint/Prettier/build、OpenAPI、pre-commit/gitleaks 与依赖审计全部通过。

---

## 14. 实施检查点

本文件是 CP-F6.0–F6.5 的唯一规范来源。实现发现冲突时必须先修改本文件并在 §15 记录覆盖决定，再继续编码；不得创建分散的 checkpoint spec。

### 14.1 CP-F6.0 · 规格固化 ✅

**目标：** 固定四类算法、版本化 profile、能力声明、证据身份、参与行完整性、事务幂等、API/UI 与 F7/F8 边界。

**交付物：** 本规格 §1–§14 与 CP-F6.1–F6.5 契约。

**非目标：** 不创建迁移、代码、路由、UI、依赖或未来测试数量。

**退出条件：** 无 P1 实施语义未决项；算法可机械复算且复杂度有界；旧 skeleton 覆盖决定明确；`git diff --check` 与文档审查通过。

### 14.2 CP-F6.1 · 持久化 schema 与 ORM

**目标：** 用新增 `0008_f6_cross_row_detection.py` 落地 §8。

**具体交付物：** detection config/run/request、capability/correlation 强化、参与行关系表、复合租户 FK、RESTRICT、CHECK、唯一约束与不可变触发器。

**非目标：** 不实现 detector、服务、API 或 UI；不改 `0001`–`0007`。

**测试：** §13.3 的迁移部分；空库/legacy preflight、往返/安全 downgrade、租户错配、参与行错配、不可变触发器与受保护约束反向测试。

**退出条件：** 私有备份与校验完成，迁移/ORM/Ruff/mypy 通过，CP-F6.2 无需再决定字段或约束。

### 14.3 CP-F6.2 · 强类型 profile、能力与纯 detector 核心

**目标：** 实现 §4–§7 的纯确定性领域层。

**具体交付物：** 判别联合、canonical fingerprint、capability lattice、四类 detector、typed evidence、finding key、golden vectors 与 F6 合成数据模式。

**非目标：** 不访问数据库/网络/当前时间，不实现服务、API/UI、F7 或 F8。

**测试：** §13.1–§13.2；核心 detection 包 statement/branch 覆盖目标不低于 90%，关键 canonical/finding-key helper 100%。

**退出条件：** 固定输入产生固定字节级 canonical 结果；无 float/hash/random/regex/subset-sum；Ruff/format/strict mypy 通过。

### 14.4 CP-F6.3 · Run 编排、查询、幂等与恢复

**目标：** 将 CP-F6.1 持久化与 CP-F6.2 纯核心组合为原子、可重放服务。

**具体交付物：** config service、input snapshot/fingerprint、run service、capability/finding 原子写入、request ledger、query service、成功/失败审计。

**非目标：** 不暴露 HTTP/UI，不修改 F4/F5，不调用 F7/F8/模型/Qdrant。

**测试：** §13.3 全部；并发、kill/restart、input drift、租户、四 declaration 完整性与 completed replay 零重算为最高优先级。

**退出条件：** 所有 F6 side effect 最多一次，失败全回滚，既有机器/人工快照零改写，服务可直接被 CP-F6.4 路由调用。

### 14.5 CP-F6.4 · API、契约与桌面工作流

**目标：** 实现 §10–§12 的强类型 API 与关联检测补充视图。

**具体交付物：** config/run/finding 路由、Pydantic discriminators、OpenAPI/client、配置页、batch correlation supplement、稳定分页与权限 UI。

**非目标：** 不修改 F4 XLSX/F5 queue，不做 review decision、mobile/tablet、Tier 1、F7/F8。

**测试：** §13.4；三角色、跨租户、缓存、恶意文本、normal/empty/loading/error/conflict/stale 与 1440×1000 Chrome。

**退出条件：** 路由无 SQL；degraded/unavailable/zero finding 清晰可辨；OpenAPI 二次无 diff；前端全部静态/测试/build 门禁通过。

### 14.6 CP-F6.5 · 契约与交付门禁

**目标：** F6 全量回归、迁移/约束、契约、安全、5000 行性能与桌面交付门禁。

**具体交付物：** §13 全部机械证据、固定 seed 5000 行四类 detector、p95/SQL/response size、1440×1000 全状态截图与项目状态记录。

**非目标：** 不降低阈值、跳过失败、修改受保护基础设施或提前进入 F7/F8。

**退出条件：** 所有命令零退出；四算法可复算、能力声明完整、参与行物理闭合、幂等/中断恢复/RBAC/PII/p95/900 秒门禁通过；写入真实数量后才将 F6 标记完成。

---

## 15. 实际落地记录

### CP-F6.0 实际落地记录（2026-07-29）

- 新增本规格，成为 CP-F6.0–F6.5 的唯一规范来源；固定四类 detector 的版本化 profile、能力状态 lattice、稳定算法、typed evidence/finding key、参与行复合 FK、原子 run/request ledger 与独立桌面补充视图。
- 关键边界：F6 候选不是已证实违规；不改写 F3/F4/F5，不进入现有复核标签，不调用模型/Qdrant，不计算 F8 severity；时空 Tier 1、模糊实体合并、跨批次历史关联留给后续明确阶段。
- 对旧 skeleton 的主要覆盖是以 `detection_run` 为身份中心、以 `correlation_finding_row` 物理闭合全部参与行，并把 capability 从可选 reason 文本升级为每 run 恰好四条的不可变结构化声明。旧 skeleton 有数据时 CP-F6.1 必须 fail closed。
- 本检查点仅修改规格与项目状态文件，没有创建迁移、服务、API、UI、依赖或测试数量。验证范围为现有架构/需求审查与 `git diff --check`。
