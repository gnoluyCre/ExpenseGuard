# Spec 003 — Phase 2 F2 Schema 映射与结构化解析

**状态：** CP-F2.0–CP-F2.2 已完成
**最近更新：** 2026-07-27
**后续检查点：** CP-F2.3 API、权限与审计

## 1. 目标与范围

F2 将 F1 保存在 `expense_row.raw_json` 的原始 Excel 行，按不可变的映射版本转换为可供 F3 及后续阶段消费的统一报销记录，并自动生成全部统一字段的三级可用性声明。

本阶段必须满足：

- 列名映射可保存、复用和版本化；修改配置产生新版本，不原地修改旧版本。
- 规范化结果先通过 Pydantic 模型校验，再写入 `expense_row.normalized_json`。
- 金额写为不使用指数形式的十进制字符串，日期写为 ISO `YYYY-MM-DD` 字符串。
- 行级数据错误进入错误清单，不阻断同批其他行，也不静默丢弃原始行。
- 系统错误回滚本次解析的整批写入；重解析失败时保留上一版成功结果。
- 同一批次和同一映射版本重复解析直接复用结果；换用新映射版本可确定性重解析。
- 字段可用性覆盖全部统一字段，状态仅为 `available`、`inferred`、`missing`。
- 租户范围由认证会话注入；API 不接收客户端提供的 `tenant_id`。

### 1.1 明确不在 F2 范围内

- 不接入 LangGraph，不读写 `row_result`，不执行 F3 确定性规则。
- 不调用 LLM，也不提供基于模型的字段猜测或结构化抽取。
- 不实现制度检索、报告、关联检测、能力声明或人工复核。
- 不新增独立配置管理页；F2.4 只扩展现有桌面端批次工作流。

### 1.2 对旧设计的覆盖决定

本规格是 F2 实施的直接契约，并覆盖 TechDesign 中两处已过时设计：

- 映射 API 使用本规格的 `/api/schema-mappings`，不再使用 `/api/tenants/{id}/schema-mapping`；租户只能来自认证会话。
- F2 不实现旧设计中的模型映射建议或 `/mapping-suggestion` 接口。表头签名精确匹配失败时进入人工映射，不调用模型猜测。

## 2. 术语与不变量

- **批次：** 一个 `file_version`；其身份和 F1 幂等语义保持不变。
- **原始行：** `expense_row.raw_json` 与 `row_no`，是不可覆盖的原始证据链。
- **表头签名：** 对批次全部原始列名按 Unicode 码点升序排列，以 UTF-8 编码的紧凑 JSON 数组计算 SHA-256，结果为 64 位小写十六进制字符串。列顺序变化不影响签名，列名大小写或内容变化会产生新签名。F1 已拒绝空表头和重复表头。
- **映射版本：** 一组源列到统一字段的映射，加上可用性阈值、文本/币种别名和确定性推断配置。版本一旦创建不可修改。
- **当前解析版本：** `file_version` 当前成功应用的映射版本。新版本重解析只有整批成功提交后才成为当前版本。

以下不变量不得被 F2 迁移或实现弱化：

- `expense_row` 的 `unique(file_version_id, row_no)`。
- `row_result` 的 `unique(file_version_id, row_no)`。
- `sampling_audit` 的既有唯一约束。
- `audit_log` 的追加写语义。
- 所有业务查询继续强制租户隔离。

## 3. 统一报销字段

统一字段集合固定为以下 12 项。F2.2 的字段可用性探测每次都必须为这 12 项各写一条结果，以覆盖旧状态。

| 字段 | 类型（规范化后） | 必填 | 规范化规则摘要 |
|---|---|---:|---|
| `amount` | decimal string | 是 | 金额规则见 4.1 |
| `expense_date` | ISO date string | 是 | 日期规则见 4.2 |
| `employee` | string / null | 否 | Unicode NFKC、去首尾空白、连续空白折叠为一个空格 |
| `expense_type` | string / null | 否 | 同普通文本 |
| `invoice_type` | string / null | 否 | 同普通文本 |
| `invoice_no` | string / null | 否 | NFKC、去全部 Unicode 空白、ASCII 字母转大写；保留其他字符和前导零 |
| `merchant` | string / null | 否 | 同普通文本 |
| `invoice_title` | string / null | 否 | 同普通文本 |
| `submission_date` | ISO date string / null | 否 | 日期规则见 4.2 |
| `location` | string / null | 否 | 同普通文本，可按已确认的确定性配置推断 |
| `currency` | string / null | 否 | 大写三字母币种代码；别名按版本配置转换，如 `RMB` → `CNY` |
| `description` | string / null | 否 | 同普通文本，保存费用说明/摘要 |

`amount` 和 `expense_date` 必须存在显式源列映射，不允许依赖推断满足映射完整性。批次中某一行的必填值为空或非法只使该行失败，不使其他行失败。

普通文本空字符串归一化为 `null`。普通文本最大 512 个 Unicode code point，`description` 最大 2,000 个，`invoice_no` 最大 128 个；超限是行级数据错误，不静默截断。

### 3.1 `normalized_json` 结构

每个成功行写入下面的 Pydantic 校验结果；全部统一字段显式存在，缺失可选字段为 `null`：

```json
{
  "schema_version": 1,
  "mapping_version_id": "7f1d2a7e-3c72-4fcb-8c0f-12c24927845e",
  "amount": "1234.5",
  "expense_date": "2026-07-01",
  "employee": "E001 张三",
  "expense_type": "差旅",
  "invoice_type": "增值税电子普通发票",
  "invoice_no": "001234567890",
  "merchant": "示例酒店",
  "invoice_title": "示例公司",
  "submission_date": "2026-07-03",
  "location": "上海",
  "currency": "CNY",
  "description": "客户拜访住宿",
  "field_provenance": {
    "amount": {
      "mode": "mapped",
      "source_columns": ["报销金额"],
      "inference_rule_id": null
    },
    "location": {
      "mode": "inferred",
      "source_columns": ["商户名称"],
      "inference_rule_id": "location-from-merchant-v1"
    }
  }
}
```

`field_provenance` 只包含值非空的字段；`mode` 仅为 `mapped` 或 `inferred`。它不得复制原始字段值，避免在元数据中重复 PII。失败行的 `normalized_json` 必须为 `null`，原始证据仍保留在 `raw_json`。

## 4. 支持的输入与归一化

### 4.1 金额

接受：

- Python/Excel 数值（整数、有限浮点数或 `Decimal`）。浮点数必须通过其十进制字符串表示构造 `Decimal`，不得先做二进制浮点运算。
- 字符串整数或小数，如 `1234`、`1234.50`、`-20.00`。
- 合法三位分组的千分位，如 `1,234.50`；不接受 `12,34.50`。
- 一个可选的已配置币种代码或符号前缀，如 `CNY 1,234.50`、`¥1,234.50`。
- 会计负数括号，如 `(123.45)`。负号与括号不得同时出现。

拒绝：

- 空值、布尔值、NaN、Infinity、字符串科学计数法、混合币种标记、无法完整消费的尾随字符。
- 超过 18 位有效数字或超过 4 位小数的值。F2 不进行隐式四舍五入。

输出使用普通十进制字符串，不含千分位和币种符号，不使用指数形式；去除无意义的尾随零和小数点，`-0` 统一为 `0`。示例：`"1,234.5000"` → `"1234.5"`。

金额中的币种标记只用于验证和剥离，不会自动填充 `currency`，除非映射版本显式配置了相应的确定性推断规则。

### 4.2 日期

接受：

- `date` 或 `datetime` 值；只取日历日期，不进行时区换算。
- ISO 日期或日期时间字符串：`YYYY-MM-DD`、`YYYY-MM-DDTHH:mm:ss[.ffffff][Z|±HH:mm]`；日期时间同样只取字符串中写明的日历日期。
- 年在前且不歧义的日期：`YYYY/M/D`、`YYYY.M.D`、`YYYY年M月D日`、`YYYYMMDD`。

拒绝：

- 月日在前或日月在前的歧义形式，如 `03/04/2026`。
- 纯数字 Excel serial date。F1 使用 openpyxl 读取带日期格式的单元格时会保留日期语义；失去单元格日期元数据的裸 serial number 不允许猜测。
- 不存在的日期，以及早于 `1900-01-01` 或晚于批次 `uploaded_at` 的 UTC 日历日期后 366 天的日期。

输出统一为 `YYYY-MM-DD`。未来 366 天上限是数据质量保护，不是 F3 的费用时效规则；使用不可变的批次上传时间而不是运行当天，保证稍后重解析仍可复现。

### 4.3 文本、发票号与币种

- 文本归一化不得修改 `raw_json`。
- 所有字符串先做 Unicode NFKC；普通文本折叠连续 Unicode 空白。
- 发票号必须按字符串处理；不得转为整数或移除前导零。F1 已因 Excel 单元格本身为数值而丢失的显示前导零无法恢复，系统不得猜补。
- 币种别名表属于映射版本配置；输出必须匹配 `^[A-Z]{3}$`。默认配置可含 `人民币`、`RMB`、`￥`、`¥` 到 `CNY` 的显式别名，但不能由代码根据租户静默猜测。

## 5. 映射版本与推断配置

### 5.1 完整性规则

保存映射版本前必须同时满足：

- 每个 `source_column` 必须存在于目标批次的表头集合。
- 同一映射版本内，一个源列最多映射到一个目标字段。
- 同一映射版本内，一个目标字段最多由一个源列直接映射。
- `target_field` 必须属于第 3 节的固定字段集合。
- `amount`、`expense_date` 两个必填目标必须各有显式映射。
- 推断规则只能写入未被直接映射的可选目标字段，且不得形成依赖环；首版不做“直接值不足时再推断”的混合来源。

映射保存与解析都重新校验完整性，不能只依赖前端校验。

### 5.2 版本与复用

- `schema_mapping_version` 的版本号在同一租户内从 1 全局单调递增。采用租户级作用域是为了保留 0002 已受保护的 `unique(tenant_id, source_column, version)`；同一表头的版本允许因其他表头先创建版本而出现不连续，但顺序仍由版本号和创建时间确定。
- 保存配置不执行 UPDATE；内容不同于该表头最新版本时创建下一版本。
- 为使 `PUT` 重试幂等，若规范化后的完整配置与该表头最新版本完全相同，则返回该版本并标记 `reused_existing=true`，不创建空洞版本。
- 版本的内容指纹由映射条目、阈值、别名和推断配置的规范化 JSON 计算 SHA-256；`created_at`、`created_by` 和版本号不参与指纹。
- 自动复用只允许表头签名完全相同的版本；系统不得仅凭相似列名自动应用映射。
- 旧版本可读取和显式用于重解析，但不可编辑或删除。修改旧配置的 UI 动作实际提交一个新版本。

### 5.3 确定性推断

F2 不使用 LLM。首版只支持以下可审计推断类型：

- `constant`：为可选目标提供固定值；首版仅允许目标为 `currency`。
- `literal_lookup`：按配置顺序，在一个或多个已映射源文本中做 NFKC 后的字面量包含匹配，输出配置值；首个命中生效。适用于从商户/销方名称推断地点等场景。

每条规则必须有租户内稳定的 `rule_id`、目标字段、源字段、类型、按序参数和输出值。规则不得执行任意代码、正则表达式、网络请求或模型调用。无命中只产生空值，不构成行级解析错误；证据计入字段可用性。

## 6. 字段可用性

### 6.1 阈值

每个映射版本保存以下阈值，范围均为 `[0, 1]`：

- `available_min_non_null_rate`：默认 `0.80`。
- `inferred_min_success_rate`：默认 `0.80`。

分母始终为批次 F1 `row_count`，包括解析失败行；这能避免通过排除坏行虚高可用率。零行批次在 F1 已被拒绝。

对每个统一字段按以下优先级判定：

1. 存在直接映射，且成功规范化为非空值的行数 / `row_count` 达到直接可用阈值 → `available`。
2. 否则（即没有直接映射），存在确定性推断规则，且由该规则得到非空值的行数 / `row_count` 达到推断阈值 → `inferred`。
3. 其他情况 → `missing`。

直接映射存在但低于阈值时状态为 `missing`，不得用隐藏的备用推断将其标成 `inferred`。`amount` 和 `expense_date` 即使低于阈值也不会阻断整个批次提交，但对应坏行必须出现在错误清单，字段状态按上述规则如实计算。

### 6.2 证据结构

`field_availability.evidence` 使用以下版本化结构，不保存原始报销值或 PII 样本：

```json
{
  "schema_version": 1,
  "mapping_version_id": "7f1d2a7e-3c72-4fcb-8c0f-12c24927845e",
  "total_rows": 1000,
  "direct": {
    "configured": true,
    "source_columns": ["消费城市"],
    "non_null_count": 620,
    "non_null_rate": "0.6200",
    "threshold": "0.8000"
  },
  "inference": {
    "configured": true,
    "rule_ids": ["location-from-merchant-v1"],
    "success_count": 910,
    "success_rate": "0.9100",
    "threshold": "0.8000"
  },
  "selected_basis": "inference"
}
```

比率用固定四位十进制字符串，避免 JSON 浮点差异；`selected_basis` 仅为 `direct`、`inference`、`none`。每次成功解析以 upsert 覆盖该批次全部 12 个字段的探测结果，不保留已不适用的旧字段状态。

## 7. 解析状态、幂等与事务

`file_version.parse_status` 取值：

- `unparsed`：从未成功解析。
- `parsed`：当前映射版本解析完成且失败行数为 0。
- `parsed_with_errors`：当前映射版本解析完成且至少一行数据失败。
- `failed`：首次解析遇到系统错误，且没有可保留的旧成功结果。

并发处理中状态以数据库行锁为准，不引入一个可能在崩溃后残留的持久化 `parsing` 状态。

### 7.1 解析算法与重复调用

1. 校验当前租户可见批次和映射版本，校验表头签名及映射完整性。
2. 对 `file_version` 执行 `SELECT ... FOR UPDATE NOWAIT`。锁冲突立即返回 `409 BATCH_PARSE_IN_PROGRESS`。
3. 若当前成功解析版本等于请求版本，返回已保存计数和状态，`reused_existing=true`，不重写行、可用性或审计日志。
4. 在一个数据库事务中，从 `raw_json` 解析每行：
   - 成功行写完整 `normalized_json`，清空旧行级错误字段。
   - 数据错误行将 `normalized_json` 置 `null`，写稳定错误码、用户安全摘要和结构化详情。
   - 覆盖全部字段可用性结果。
   - 最后更新 `file_version` 的当前映射版本、解析状态和解析时间。
5. 提交成功后追加一条不含 PII 的解析审计记录。

同版本重复调用不得刷新 `parsed_at`。新版本重解析必须从不可变的 `raw_json` 开始，不得以旧 `normalized_json` 为输入。

### 7.2 系统错误回滚

- 数据库异常、程序缺陷或无法归类的基础设施错误属于系统错误，必须回滚本次事务中所有行、可用性和 `file_version` 变更。
- 若批次已有成功结果，新版本重解析失败后继续指向旧映射版本并保留旧状态、旧 `normalized_json` 和旧可用性。
- 若批次从未成功解析，可在回滚后用独立短事务将状态记为 `failed`；不得写半批结果。
- 失败审计使用独立事务追加，payload 只含批次 ID、请求映射版本 ID 和稳定错误分类，不含异常堆栈或报销字段。内部异常详情只进服务端日志。

## 8. 行级错误契约

失败行保留 `raw_json`，并使用：

- `parse_error_code = "ROW_VALIDATION_FAILED"`
- `parse_error`：用户安全的摘要，如“该行有 2 个字段无法解析”。保留该现有列用于兼容 F1 API。
- `parse_error_detail`：结构化详情，不复制原始值。

示例：

```json
{
  "schema_version": 1,
  "mapping_version_id": "7f1d2a7e-3c72-4fcb-8c0f-12c24927845e",
  "errors": [
    {
      "field": "amount",
      "code": "AMOUNT_INVALID_FORMAT",
      "source_column": "报销金额",
      "message": "金额格式无法识别"
    },
    {
      "field": "expense_date",
      "code": "REQUIRED_VALUE_MISSING",
      "source_column": "费用日期",
      "message": "必填日期为空"
    }
  ]
}
```

详情中的稳定字段错误码：

| code | 含义 |
|---|---|
| `REQUIRED_VALUE_MISSING` | 必填字段为空 |
| `AMOUNT_INVALID_FORMAT` | 金额格式、分组或字符非法 |
| `AMOUNT_OUT_OF_RANGE` | 金额精度或有效位超限 |
| `DATE_INVALID_FORMAT` | 日期格式不支持或日期不存在 |
| `DATE_OUT_OF_RANGE` | 日期超出允许的数据质量范围 |
| `TEXT_TOO_LONG` | 文本超过字段上限 |
| `CURRENCY_INVALID` | 币种无法按版本别名归一化为三字母代码 |

错误顺序按第 3 节统一字段顺序固定，保证重复解析响应稳定。可选字段格式非法同样使该行进入错误清单，不能静默置空；仅“没有配置推断命中”可作为正常空值。

## 9. API 契约

所有 API 使用既有统一错误响应：

```json
{
  "error": {
    "code": "MAPPING_REQUIRED_FIELD_MISSING",
    "message": "映射缺少必填字段：amount"
  }
}
```

所有路径参数 `id` 均为 `file_version_id`。所有响应只返回当前会话租户内的数据。

### 9.1 `GET /api/schema-mappings?file_version_id={id}`

权限：`CONFIG_READ`。返回批次表头及同表头签名的映射版本，按版本降序；不存在匹配时 `versions=[]`。

```json
{
  "file_version_id": "uuid",
  "header_signature": "sha256-hex",
  "source_columns": ["费用日期", "报销金额", "员工"],
  "versions": [
    {
      "id": "uuid",
      "version": 2,
      "created_at": "2026-07-27T10:00:00Z",
      "created_by": "uuid",
      "is_current_for_batch": false,
      "mappings": [
        {"source_column": "费用日期", "target_field": "expense_date"},
        {"source_column": "报销金额", "target_field": "amount"}
      ],
      "availability_thresholds": {
        "available_min_non_null_rate": "0.8000",
        "inferred_min_success_rate": "0.8000"
      },
      "currency_aliases": {"RMB": "CNY"},
      "inference_rules": []
    }
  ]
}
```

### 9.2 `PUT /api/schema-mappings`

权限：`CONFIG_WRITE`。请求不得含 `tenant_id` 或客户端指定的 `version`。

```json
{
  "file_version_id": "uuid",
  "mappings": [
    {"source_column": "费用日期", "target_field": "expense_date"},
    {"source_column": "报销金额", "target_field": "amount"}
  ],
  "availability_thresholds": {
    "available_min_non_null_rate": "0.8000",
    "inferred_min_success_rate": "0.8000"
  },
  "currency_aliases": {"人民币": "CNY", "RMB": "CNY"},
  "inference_rules": []
}
```

创建新版本返回 `201`；内容与最新版本相同返回 `200`。响应为完整版本对象并增加 `reused_existing: boolean`。保存成功追加 `schema_mapping_version.create` 审计，payload 仅含版本 ID、版本号、表头签名、映射目标字段列表，不记录原始样本值。

### 9.3 `POST /api/batches/{id}/parse`

权限：`BATCH_IMPORT`。

请求：

```json
{"mapping_version_id": "uuid"}
```

成功或部分失败均返回 `200`：

```json
{
  "file_version_id": "uuid",
  "mapping_version_id": "uuid",
  "mapping_version": 2,
  "status": "parsed_with_errors",
  "total_rows": 1000,
  "success_count": 987,
  "error_count": 13,
  "parsed_at": "2026-07-27T10:05:00Z",
  "reused_existing": false
}
```

审计 action 为 `batch.parse`，payload 只含上述 ID、版本、状态和计数。失败重试与同版本复用不重复写成功审计。

### 9.4 `GET /api/batches/{id}/parse-errors`

权限：`BATCH_READ`。查询参数：`offset` 默认 0、最小 0；`limit` 默认 50、范围 1–200。按 `row_no` 升序返回：

```json
{
  "file_version_id": "uuid",
  "mapping_version_id": "uuid",
  "total": 13,
  "offset": 0,
  "limit": 50,
  "items": [
    {
      "row_no": 8,
      "raw_json": {"费用日期": null, "报销金额": "abc"},
      "parse_error_code": "ROW_VALIDATION_FAILED",
      "parse_error": "该行有 2 个字段无法解析",
      "parse_error_detail": {"schema_version": 1, "errors": []}
    }
  ]
}
```

`raw_json` 仅对具备 `BATCH_READ` 且同租户的用户返回；日志和审计不得记录该内容。

### 9.5 `GET /api/batches/{id}/field-availability`

权限：`BATCH_READ`。返回当前解析版本的 12 项结果，按第 3 节固定顺序：

```json
{
  "file_version_id": "uuid",
  "mapping_version_id": "uuid",
  "items": [
    {
      "field_name": "amount",
      "status": "available",
      "evidence": {"schema_version": 1, "selected_basis": "direct"}
    }
  ]
}
```

未成功解析的批次返回 `409 BATCH_NOT_PARSED`，不返回空数组冒充探测完成。

### 9.6 API 级稳定错误码

| HTTP | code | 场景 |
|---:|---|---|
| 404 | `BATCH_NOT_FOUND` | 批次不存在或不属于当前租户 |
| 404 | `MAPPING_VERSION_NOT_FOUND` | 映射版本不存在或不属于当前租户 |
| 409 | `MAPPING_HEADER_MISMATCH` | 映射版本与批次表头签名不同 |
| 409 | `BATCH_PARSE_IN_PROGRESS` | 同一批次正在被另一个事务解析 |
| 409 | `BATCH_NOT_PARSED` | 请求解析结果但批次尚无成功结果 |
| 422 | `MAPPING_REQUIRED_FIELD_MISSING` | 缺少 `amount` 或 `expense_date` 映射 |
| 422 | `MAPPING_SOURCE_COLUMN_UNKNOWN` | 源列不属于批次表头 |
| 422 | `MAPPING_SOURCE_COLUMN_DUPLICATED` | 一个源列被重复映射 |
| 422 | `MAPPING_TARGET_FIELD_DUPLICATED` | 一个目标字段被重复直接映射 |
| 422 | `MAPPING_TARGET_FIELD_UNKNOWN` | 目标字段不在统一字段集合 |
| 422 | `MAPPING_THRESHOLD_INVALID` | 阈值不在 `[0, 1]` |
| 422 | `MAPPING_INFERENCE_INVALID` | 推断类型、依赖或参数非法 |
| 500 | `BATCH_PARSE_INTERNAL_ERROR` | 系统错误且本次整批已回滚 |

认证与权限错误继续复用既有统一错误码和 401/403 处理。

## 10. 数据库迁移规格（CP-F2.1 输入）

迁移文件固定新增为 `backend/app/db/migrations/versions/0003_f2_schema_parsing.py`，不得修改 `0001` 或 `0002`。

### 10.1 目标变更

- 新增 `schema_mapping_version`：`id`、`tenant_id`、`header_signature`、`version`、`config_fingerprint`、`availability_thresholds`、`currency_aliases`、`inference_config`、`created_by`、`created_at`。新 API 创建的数据必须有 `created_by`；历史回填允许为空并有明确 legacy 标记。
- 将 `schema_mapping` 扩展为映射条目表，关联 `mapping_version_id`；增加 `unique(mapping_version_id, source_column)` 和 `unique(mapping_version_id, target_field)`。为避免弱化既有数据库约束，保留旧 `version`、`confidence` 列及 `uq_schema_mapping_tenant_id_source_column_version`：回填和新写入均令旧 `version` 与租户级父版本号一致，`confidence` 在 F2 中弃用并写 `null`。是否清理兼容列须走后续独立迁移评审，不属于 F2。
- `file_version` 新增可空 `mapping_version_id`、非空 `parse_status`（默认 `unparsed`）、可空 `parsed_at`。
- `expense_row` 新增可空 `normalized_json`、`parse_error_code`、`parse_error_detail`；保留现有 `parse_error`。
- 复用 `field_availability`，不创建替代表；其唯一约束保持不变。
- 所有新增业务表/关联保持 `tenant_id` 复合外键或现有租户隔离模式。

建议数据库约束：

- `unique(tenant_id, version)`；另建 `(tenant_id, header_signature)` 普通索引支持匹配查询。
- `unique(tenant_id, header_signature, config_fingerprint, version)` 不足以提供 PUT 幂等；服务层必须在锁定该表头最新版本后比较指纹。允许后续版本内容回退到历史配置。
- `parse_status` 使用受约束字符串枚举。
- `file_version.mapping_version_id` 使用 `ON DELETE RESTRICT`；版本不提供删除路径。

### 10.2 既有映射回填

现有 `schema_mapping` 是 Phase 1 骨架。迁移按 `(tenant_id, version)` 分组，每组：

1. 按 `source_column` 排序计算表头签名。
2. 创建一个 `schema_mapping_version`，使用默认阈值、空别名增量和空推断配置，标记 `backfilled_legacy=true`，`created_by=null`。
3. 将原条目关联到该版本；若历史脏数据造成同一组重复目标字段，迁移必须失败并输出诊断，不得静默选一条。

迁移不把 legacy 版本自动绑定到任何 `file_version`，因为旧表没有可靠的批次关联证据。首次解析仍需用户明确选择匹配版本。

`backend/tests/integration/test_migrations.py` 当前固定校验 18 张表；新增第 19 张表时必须同步预期表集合，并继续直查受保护的唯一约束和 `audit_log` 追加写触发器，而不是只比较 ORM metadata。

## 11. 备份、升级与降级

### 11.1 非测试环境升级前

运维人员必须在应用停写窗口执行并验证：

1. 记录当前 Alembic revision 和应用版本。
2. 使用 `pg_dump --schema-only` 备份业务 schema。
3. 使用 `pg_dump --data-only --table=schema_mapping --table=file_version --table=expense_row --table=field_availability` 备份相关表。
4. 将备份写到受控备份目录，不写入仓库；用 `pg_restore --list` 或等价方式确认归档可读。
5. 记录行数与关键约束，尤其是 `expense_row` 和 `row_result` 的行级唯一约束。

凭据必须来自环境变量或既有 PostgreSQL 凭据配置，不得写入命令历史或文档。

### 11.2 升级验证

在一次性测试数据库依次执行：

```text
alembic upgrade 0002
加载含 legacy schema_mapping 的夹具
alembic upgrade 0003
校验回填、列默认值、外键和唯一约束
alembic downgrade 0002
校验旧结构与 legacy 数据
alembic upgrade 0003
再次校验
alembic check
```

生产升级后校验表/索引存在、回填条目计数守恒、`file_version` 默认为 `unparsed`，以及受保护约束未改变，再恢复写流量。

### 11.3 降级语义

- `downgrade()` 只负责恢复 0002 可运行的 schema，不承诺把 F2 新配置无损压回旧模型。
- 降级前必须另行导出 `schema_mapping_version`、新版 `schema_mapping`、四个新增/变更列和 `field_availability`，并停止解析写入。
- 降级将丢弃 `normalized_json`、结构化错误详情、解析状态和 F2 版本元数据；`raw_json`、`row_no` 和 F1 批次仍保留。
- 如需重新升级，先升级到 0003，再从降级前导出恢复 F2 数据；不得用旧格式覆盖新版本。
- 任一步校验失败即停止发布并从升级前备份恢复，不带病继续。

## 12. 权限、租户与审计

| 操作 | 权限 |
|---|---|
| 查看映射 | `CONFIG_READ` |
| 保存新映射版本 | `CONFIG_WRITE` |
| 触发解析 | `BATCH_IMPORT` |
| 查看错误与字段可用性 | `BATCH_READ` |

- auditor 可查看/复用映射并解析，不能保存新版本。
- configurator 可查看、保存、复用并解析。
- viewer 可查看批次解析错误与可用性，不能查看映射配置接口或触发解析。
- 跨租户资源统一表现为 404，避免资源枚举。
- 映射创建、解析成功和解析系统失败写追加式审计；同版本复用不重复追加成功事件。
- 审计 payload 不含源列样本值、`raw_json`、`normalized_json`、员工、发票号、商户等 PII/业务明细。

## 13. 验收场景与退出条件

### 13.1 正常解析

- 完整映射下全部行规范化，金额和日期输出满足规范，状态为 `parsed`。
- 全部 12 个字段都有可用性记录和不含 PII 的证据。
- 映射与解析审计包含版本和计数，不含原始值。

### 13.2 部分失败

- 必填缺值、非法金额、歧义日期和超长文本只使对应行失败。
- 失败行保留 `raw_json`，`normalized_json=null`，错误清单可按行号分页读取。
- 其他行正常提交，批次状态为 `parsed_with_errors`，计数相加等于 `row_count`。

### 13.3 重复解析

- 同一批次和映射版本再次调用返回完全相同的结果计数，`reused_existing=true`。
- 不重写行、可用性、`parsed_at` 或成功审计。
- 并发调用最多一个获得锁，另一调用稳定返回 409，不产生半批数据。

### 13.4 换版本重解析

- 新版本始终从 `raw_json` 重算，并原子替换当前规范化结果、行错误和全部字段可用性。
- 成功后 `file_version` 指向新版本；旧映射版本仍可读并可用于以后确定性重解析。
- 新版本结果不得混有上一版已删除字段状态或行错误。

### 13.5 系统异常回滚

- 在行更新、可用性更新和 `file_version` 更新各阶段注入系统异常，均不留下本次半批写入。
- 初次解析失败后无任何 `normalized_json`；已有成功版本的重解析失败后旧结果完整保留。
- 失败审计可留痕，但不得依赖已回滚的业务事务，也不得包含异常详情或 PII。

CP-F2.0 的退出条件是本规格完整覆盖以上五类场景，并可直接作为 CP-F2.1–F2.4 的数据库、核心服务、API 与前端契约输入。CP-F2.0 不要求运行代码测试；文档审查和版本差异检查通过即可进入 CP-F2.1。

## 14. 实施检查点

CP-F2.1 实际落地记录（2026-07-27）：

- 新增 `0003_f2_schema_parsing.py` 与第 19 张业务表 `schema_mapping_version`。
- ORM 增加映射版本、租户复合外键、批次解析状态及行级规范化/结构化错误列。
- 版本号改为租户内全局递增，以保留 0002 的映射唯一约束；历史 `confidence` 值回填时原样保留。
- 在一次性测试库验证空库升级、legacy 回填、`upgrade → downgrade → upgrade`、重复目标脏数据事务回滚及 `alembic check`。
- PostgreSQL 迁移目录测试 20 passed；后端全量 71 passed、1 skipped。

CP-F2.2 实际落地记录（2026-07-27）：

- 新增 `backend/app/core/parsing/`：统一字段与严格配置模型、金额/日期/文本/发票号/币种归一化、映射完整性、确定性推断、全字段可用性及批次解析服务。
- 推断配置首版固定为严格判别联合：`constant` 使用 `rule_id/type/target_field/value`；`literal_lookup` 使用 `rule_id/type/target_field/source_fields/cases[{literal,value}]`。`source_fields` 是已直接映射的统一字段；不支持推断链、正则、代码、网络或模型调用。
- §6.2 的示例与 §5.1/§6.1 正文存在冲突：实现以正文为准，直接映射与推断目标互斥；直接映射低于阈值时为 `missing`，不回退为 `inferred`。
- 可用性 numerator 使用逐字段成功观察：一行因其他字段失败，不抹掉本字段已经成功的规范化结果；denominator 仍固定包含全部失败行的 `file_version.row_count`。
- 解析服务用 `SELECT ... FOR UPDATE NOWAIT` 和事务内保存点覆盖行结果、全部 12 项可用性及批次状态；同版本复用不刷新 `parsed_at`，新版本只从 `raw_json` 重算，系统异常保留旧成功结果。
- 新增纯逻辑测试 51 个、真实 PostgreSQL 服务集成测试 6 个；覆盖部分失败、同版本复用、换版本重解析、捕获异常后提交外层事务仍无半批写入，以及并发锁冲突。解析包定向覆盖率 91%；后端全量 128 passed、1 skipped；Ruff、格式检查和 strict mypy 全部通过。

- [x] **CP-F2.0：** 规格固化。
- [x] **CP-F2.1：** 数据库迁移与 ORM 持久化模型；`0003` 往返迁移、legacy 回填、脏数据回滚及 `alembic check` 已验证。
- [x] **CP-F2.2：** `backend/app/core/parsing/` 纯逻辑与解析服务；51 个单元测试、6 个服务集成测试、91% 定向覆盖率及后端全量门禁通过。
- [ ] **CP-F2.3：** API、权限、租户隔离与审计集成测试。
- [ ] **CP-F2.4：** 现有桌面批次页四视图工作流。
- [ ] **CP-F2.5：** 全量测试、契约同步、交付门禁和状态文件更新。
