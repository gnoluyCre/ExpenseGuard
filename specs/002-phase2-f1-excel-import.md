# Spec 002 · Phase 2 F1 Excel 导入与文件版本管理

**状态:** 已完成。
**最近更新:** 2026-07-27

## 范围

F1 只交付 `.xlsx` 上传、内容哈希去重、`file_version` 创建/复用、原始行写入
`expense_row`、批次列表与详情展示。字段映射、金额/日期归一化、字段可用性探测、
规则校验、报告与复核台均留给 F2-F5。

## 接口

- `POST /api/batches`: multipart 字段名 `file`,要求 `batch:import`。
- `GET /api/batches`: 当前租户批次列表,要求 `batch:read`。
- `GET /api/batches/{file_version_id}`: 批次元数据与原始行摘要,要求 `batch:read`。

## 行与幂等语义

- 内容哈希为上传文件原始 bytes 的 SHA-256。
- 同租户同哈希命中已有 `file_version` 时返回既有批次,不重复插入 `expense_row`。
- `row_count` 统计表头后的非全空物理行,不含表头。
- `row_no` 保留 Excel 物理行号,因此首条数据通常为 2。
- 原始文件留存在 `data/private/uploads/{tenant_id}/{content_hash}.xlsx`,不进仓库。

## 验收

- 500-5000 行 `.xlsx` 可导入;499/5001 行、非 `.xlsx`、坏 workbook、空/重复表头返回稳定错误码。
- 上传成功后 `file_version` 与 `expense_row` 同事务落库;重复上传复用同一 `file_version_id` 且行数不翻倍。
- viewer 只能读批次,不能导入。
- OpenAPI 与前端类型同步。

## 实现记录

- 后端新增 `app.core.batches` 导入服务,复用既有 `FileVersion` / `ExpenseRow` 表,未新增迁移。
- 新增 `/api/batches` 路由:上传、列表、详情均走 `TenantDbDep`;上传要求 `batch:import`,列表/详情要求 `batch:read`。
- 上传文件以原始 bytes 计算 SHA-256,原始 `.xlsx` 保存在 `data/private/uploads/{tenant_id}/{content_hash}.xlsx`。
- 前端 `/batches` 已替换为桌面端批次页:上传控件、导入结果、历史列表与原始行预览;viewer 不显示上传入口。
- 已同步 `openapi.json` 与 `frontend/src/api/schema.d.ts`。

## 验证记录

- 已通过:后端 F1 单元测试、后端 unit/eval 回归、`ruff check`、`mypy app scripts`、OpenAPI check、前端批次页测试、前端全量测试、`npm run typecheck`、`npm run lint`、`npm run build`。
- 已通过:`backend/tests/integration/test_batches.py`。
