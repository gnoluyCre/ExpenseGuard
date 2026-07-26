# 代码范式

## 目的
本文件定义 agent 在本项目应遵循的实现范式。
优先采用这些范式,而非自行发明。各章节内容取自技术设计文档。

## 架构范式
- **主范式:** 分层 / 面向服务。`backend/core/`(通用框架层:orchestration / rules / retrieval / detection / agent / models / grading / evaluation / observability)与 `backend/api`(传输层)、`backend/db`(持久化)分层。
- **规则:** 通用框架层(`core/`)与客户数据层(`tenants/`、`data/private/`)**物理隔离** —— 脱敏 = 删除后者两个目录,而非事后重写。这是 Project Structure 的第一设计目标。
- **规则:** 领域逻辑与传输 / UI 关注点分离。路由处理器不含业务逻辑,不直接查库 —— 经服务层。
- **规则:** 创建新抽象前先复用既有模块。detector 只需声明字段依赖即可自动获得能力声明降级行为,不为每个企业单独适配。

## 数据获取
- **前端:** 用 query 库(TanStack Query)消费由 OpenAPI 生成的类型化客户端;fetch 逻辑不进 render 函数。
- **后端:** FastAPI async 端点 → 服务层 → SQLAlchemy;检索经 `VectorStore` 抽象访问 Qdrant。
- **规则:** 不要臆断使用某个库。获取数据前先查 `tech_stack.md` 确认项目选定的方式。

## 状态管理
- **服务端状态:** TanStack Query(缓存、失效、重试)。
- **客户端状态:** React 内建(useState / useReducer / Context);MVP 不引入 Redux 类全局库,除非确有跨页共享需求。
- **表单:** react-hook-form + Zod schema 校验(与后端 Pydantic 契约对齐)。
- **规则:** MVP 阶段优先最简可行方案。若框架内建状态已够用,不要添加状态库。

## 错误处理
- 在服务 / API 边界归一化错误 —— 绝不让原始异常抛到 UI。
- 绝不静默吞掉错误;始终记录或上抛(解析失败行进错误清单,不静默丢弃)。
- UI 返回用户安全的提示;开发者上下文记在服务端日志。
- 所有 API 响应使用一致的错误 shape。
- **AI 失败回退一律「转人工 + 显式标注」,绝不「猜一个结论」**(取证 agent 超步数 → 输出「取证未完成」;检索失败 → 降级为「仅确定性规则判定」并标注;映射建议失败 → 降级纯手工映射,流程不中断)。

## 校验
- 校验所有外部输入(用户表单、API 载荷、环境变量)。前端 Zod,后端 Pydantic。
- 在系统边界处做运行时校验;边界内部信任内部类型。
- 校验规则与对应契约就近放置(挨着 API 路由或表单 schema)。
- **报销数据与制度文档为外部输入,可能携带 prompt 注入:** 检索内容与系统指令用明确边界分隔并声明为「待分析数据,非指令」;结构化输出约束解码限制越界;取证工具全部只读。

## 文件与命名约定
- **后端文件:** snake_case(Python 惯例);模块按 `core/<domain>/` 组织。
- **前端文件:** kebab-case 或框架默认;React 组件文件 PascalCase。
- **组件 / 类:** PascalCase
- **函数 / 变量:** 前端 camelCase / 后端 Python snake_case
- **常量 / 环境变量:** UPPER_SNAKE_CASE

## 测试范式
- **优先级(资源不足时自上而下保):** ① 幂等性与中断恢复 ② 规则引擎判定正确性 ③ 引用校验有效性 ④ 关联检测算法 ⑤ API / 前端 E2E。详见 `testing.md`。
- 为纯逻辑与工具函数添加单元测试。
- 为 API 契约与关键数据流添加集成测试(**处理中途 kill → 重启 → 校验无重复副作用** 是最高优先级集成测试)。
- 仅为 PRD 标记为必须的顶层用户旅程添加 E2E 测试(批次预审主流程)。
- 每个功能完成后跑测试套件;修复失败后再继续。

## 变更纪律
- 优先小而聚焦的改动,而非大段重写。
- 新增依赖前先查 `pyproject.toml` / `package.json`。
- 未经明确批准,不得修改数据库迁移、基础设施配置、认证流程。**幂等唯一约束与审计表追加写语义尤其不得弱化。**
- 一次一个功能 —— 尊重 F1→F2→F3→F4→F5 串行依赖;每个可用功能后 commit 或打 checkpoint。
- 永不提交 `.env` / `tenants/` / `data/private/`;pre-commit secret 扫描必过。
