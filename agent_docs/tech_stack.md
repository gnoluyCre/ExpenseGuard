# 技术栈与工具

- **前端:** React 19 + Vite 8 + TypeScript 5.9(strict)+ Tailwind CSS v4 + shadcn/ui。仅桌面浏览器端(不做移动端 / 平板适配)。API 客户端由后端 OpenAPI 生成为类型化客户端。
- **后端:** Python 3.13 + FastAPI + Pydantic v2。编排层 LangGraph + PostgresSaver(checkpoint)。异步优先。
- **数据库:** PostgreSQL(业务表 + LangGraph checkpoint + 审计日志同库,不同 schema)+ SQLAlchemy + Alembic 迁移。
- **向量库:** Qdrant(制度条款向量;payload 复合过滤 tenant + effective_date/expiry_date)。经 `VectorStore` 接口抽象,可退回 PGVector。
- **样式:** Tailwind CSS + shadcn/ui。
- **认证:** 自建账号密码 + server-side session(HttpOnly + Secure + SameSite cookie;密码哈希 Argon2 或 bcrypt)+ RBAC 三角色(auditor / configurator / viewer)。8 小时闲置过期,不做 MFA。若已有 OIDC IdP 可改 SSO。
- **模型层:** OpenAI 兼容抽象(`LLMProvider` Protocol,base_url/model/api_key 全来自配置)。强模型 MVP 走云 API(DeepSeek / 通义),vLLM 本地自托管可切换(切换 = 改两个配置项)。embedding/rerank **本地自托管**(Qwen3-Embedding 小尺寸),检索层数据不出内网。
- **结构化输出:** Pydantic schema + XGrammar 约束解码(自托管)/ function calling(云 API),同一套 schema 双路径。
- **规则引擎:** JSON Logic / 决策表 —— 规则即数据,阈值白名单为配置非代码。
- **可观测:** Langfuse 或 Phoenix + OpenTelemetry;trace 按 task_id 聚合 token / 延迟 / 成本。
- **测试 / 评测:** pytest(单元 / 集成)+ Vitest(前端)+ 数据驱动 eval gate(基线阈值为空时 skip)+ 后续 DeepEval / promptfoo / Ragas。
- **部署:** Docker Compose 单机(api / frontend / postgres / qdrant / embedding / trace)。

## 安装与启动命令
```bash
# 起依赖服务
docker compose up -d          # postgres, qdrant

# 后端
cd backend
uv sync                       # 或 pip install -e .
uv run alembic upgrade head   # 应用迁移
uv run python -m app          # 开发服务器;Windows 不要直接用 uvicorn app.main:app

# 前端
cd frontend
npm install
npm run dev
```

## 数据模型(核心实体 —— 只定义形状与约束,DDL / 迁移由构建时生成)

> 关键约束加粗。完整字段见 `docs/TechDesign-ExpenseGuard-MVP.md` 的数据模型章节。所有业务表带 `tenant_id`。

| 实体 | 关键字段 | 索引 / 约束 |
|---|---|---|
| `tenant` | id, name | —— |
| `user` | id, tenant_id, username, password_hash, role | unique(tenant_id, username);role ∈ {auditor, configurator, viewer} |
| `file_version` | id, tenant_id, filename, content_hash, uploaded_by, uploaded_at | **unique(tenant_id, content_hash)** —— 内容哈希去重 |
| `expense_row` | id, file_version_id, row_no, 结构化字段…, raw_json | unique(file_version_id, row_no) |
| `row_result` | id, file_version_id, row_no, verdict, rule_version, computed_at | **unique(file_version_id, row_no) —— 幂等核心表,恢复时据此跳过** |
| `schema_mapping` | id, tenant_id, source_column, target_field, version | 可保存复用 |
| `rule_config` | id, tenant_id, rule_id, definition(JSON), version, effective_from | unique(tenant_id, rule_id, version) —— **判定结果引用具体版本** |
| `policy_document` | id, tenant_id, title, version, effective_date, expiry_date | index(tenant_id, effective_date) |
| `policy_clause` | id, document_id, clause_no, hierarchy_path, text | 向量存 Qdrant,**原文存 PG(引用校验需比对)** |
| `finding` | id, file_version_id, kind, severity_impact, severity_confidence, rule_id/clause_id, quote, reasoning | index(file_version_id, severity) —— 二维分级分列 |
| `correlation_finding` | id, file_version_id, detector, participating_row_nos(array), evidence_json | index(file_version_id) |
| `evidence_step` | id, finding_id, step_no, tool_name, tool_input, tool_output, timestamp | ReAct 每步落库 |
| `review` | id, finding_id, decision, reviewer_id, reviewed_at, note | unique(finding_id);decision ∈ {confirmed, false_positive} |
| `sampling_audit` | id, file_version_id, row_no, sampled_at, decision, reviewer_id | **被放行样本随机抽检 —— 漏放率可测量的唯一来源** |
| `field_availability` | id, file_version_id, field_name, status, evidence(JSON) | status ∈ {available, inferred, missing} |
| `capability_declaration` | id, file_version_id, detector, status, reason | status ∈ {enabled, degraded, unavailable} |
| `audit_log` | id, tenant_id, actor_id, action, target_type, target_id, payload_json, at | **追加写,不可静默修改** |

### 幂等恢复语义(最高优先级)
```
workflow thread_id = file_version_id
每行处理前:SELECT row_result WHERE (file_version_id, row_no)
  命中 → 直接返回已有结果,不重复执行任何副作用
  未命中 → 执行 → INSERT ... ON CONFLICT DO NOTHING
```

### 时间维度检索(Qdrant filter)
```
tenant_id == :tenant
AND effective_date <= :expense_date
AND (expiry_date IS NULL OR expiry_date > :expense_date)
```
报告中必须标注实际使用的制度版本号与生效日期。

## 错误处理范式
```python
# 后端:在服务 / API 边界归一化错误,返回一致 shape;绝不静默吞掉。
# AI 相关失败一律「转人工 + 显式标注」,绝不猜结论。
from fastapi import HTTPException

class ExpenseGuardError(Exception):
    """领域错误基类。code 用于前端映射用户安全提示。"""
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message

def parse_row(raw: dict) -> ExpenseRow:
    try:
        return ExpenseRow.model_validate(raw)  # Pydantic 边界校验
    except ValidationError as e:
        # 解析失败行进错误清单并给出原因 —— 不静默丢弃(PRD 硬性要求)
        logger.warning("row parse failed", extra={"row": raw.get("row_no"), "err": str(e)})
        raise ExpenseGuardError("ROW_PARSE_FAILED", "该行字段无法解析,已记入错误清单") from e
```

## 结构化输出示例
```python
# 同一 Pydantic 模型既约束 LLM 输出(function calling / XGrammar),又作 API 响应模型。
from pydantic import BaseModel, Field

class Verdict(BaseModel):
    clause_id: str
    quote: str = Field(description="制度条款逐字引用,须与检索到的原文机械匹配")
    reasoning: str
    severity_impact: int = Field(ge=0, le=3)
    severity_confidence: int = Field(ge=0, le=3)

def verify_quote(v: Verdict, clause_text: str) -> Verdict | None:
    # 机械式逐字校验:不通过则拒绝该引用,而非原样呈现
    return v if v.quote.strip() in clause_text else None
```

## 样式与组件示例(shadcn/ui)
```tsx
// 复核台核心:原始行 + 判定理由 + 条款引用 同屏联动。证据优先于结论。
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";

export function FindingPanel({ finding }: { finding: Finding }) {
  return (
    <Card>
      <CardHeader className="flex-row items-center justify-between">
        <CardTitle>行 {finding.rowNo} · {finding.kind}</CardTitle>
        <Badge variant={finding.severityImpact >= 2 ? "destructive" : "secondary"}>
          impact {finding.severityImpact} / conf {finding.severityConfidence}
        </Badge>
      </CardHeader>
      <CardContent className="grid grid-cols-3 gap-4">
        <RawRow data={finding.rawRow} />
        <Reasoning text={finding.reasoning} />
        <ClauseQuote clauseId={finding.clauseId} quote={finding.quote} />
      </CardContent>
    </Card>
  );
}
```
