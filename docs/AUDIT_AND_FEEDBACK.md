# 审计日志与用户反馈记录

## 1. 功能目标

该模块为每次 Text-to-SQL 请求生成唯一 `request_id`，记录从问题进入系统到最终 SQL 执行完成的关键元数据，并允许用户对结果提交“正确/错误”反馈。数据可用于问题追踪、失败分析、线上评测样本沉淀和后续 SFT/GRPO 数据筛选。

审计数据保存在独立的 SQLite 文件中，不写入被查询的业务数据库，也不会改变 SQLGlot 校验和业务数据库只读事务。

## 2. 技术路线

```text
POST /api/query
  -> UUID request_id
  -> 写入 in_progress 审计记录
  -> LangGraph / Schema RAG / LLM / AST / SQL 执行
  -> 更新状态、SQL、耗时、错误类型、命中表和返回行数
  -> QueryResponse 返回 request_id

用户点击“正确/错误”
  -> POST /api/feedback
  -> 校验 request_id 存在
  -> 按 request_id + username 写入或更新反馈
```

实现位于 `Audit.py`。每次数据库操作使用短连接，启用 SQLite WAL 与外键约束，避免跨 FastAPI 工作线程共享同一个 SQLite connection。

## 3. 数据模型

### `query_audit`

核心字段：

- `request_id`：一次 Agent 请求的 UUID；
- `thread_id`：所属 LangGraph 多轮会话，用于把多次请求串联分析；
- `created_at` / `completed_at`：UTC ISO-8601 时间；
- `username`：通过 Basic Auth 的用户名；
- `question`：脱敏后的自然语言问题，可通过配置关闭；
- `provider` / `model`：模型运行信息；
- `database_backend` / `schema_context_mode`：数据库与上下文模式；
- `status`：`in_progress`、`success`、`human_handoff` 或 `provider_error`；
- `sql_text`：最终 SQL；
- `retry_count`、`error_types_json`：重试与分类错误；
- `retrieved_tables_json`：Schema RAG 命中的表；
- `result_row_count` / `result_truncated`：结果规模元数据；
- `latency_ms`：接口内部 Agent 执行耗时。

### `query_feedback`

- `(request_id, username)` 为联合主键；
- `verdict` 只允许 `correct` 或 `incorrect`；
- `comment` 最长 1000 字符；
- 同一用户再次反馈会覆盖 verdict/comment 并更新 `updated_at`。

## 4. 明确不记录的数据

默认不保存：

- SQL 查询返回的具体数据行；
- 完整 Schema Prompt 和 LLM Prompt；
- LLM API Key、Authorization Header、数据库密码；
- token logprobs 的完整原始响应；
- 客户端 IP。

问题、SQL 和反馈文本写入前会对常见 `api_key`、`token`、`password`、`secret`、Bearer token 和 Ark Key 形式进行替换。正则脱敏不能替代业务侧的数据分类，因此生产环境若问题可能包含敏感业务文本，应设置 `AUDIT_STORE_QUESTION=false`。

## 5. 配置

```dotenv
AUDIT_ENABLED=true
AUDIT_DB_PATH=audit.sqlite
AUDIT_RETENTION_DAYS=30
AUDIT_STORE_QUESTION=true
```

- 相对路径以 `VeriSQL-Agent` 项目目录为基准；
- 启动时创建表和索引，并删除超过保留天数的记录；
- `AUDIT_ENABLED=false` 时查询功能仍可工作，但反馈和审计查询接口返回不可用；
- 审计写入异常采用 fail-open：记录服务端异常日志，但不阻断主查询。

## 6. API

### 查询接口

```http
POST /api/query
```

响应新增：

```json
{"request_id": "8a7d...", "status": "success"}
```

### 提交或修改反馈

```http
POST /api/feedback
Content-Type: application/json

{
  "request_id": "8a7d...",
  "verdict": "incorrect",
  "comment": "部门名称正确，但排序不符合要求"
}
```

不存在的 `request_id` 返回 404；关闭审计时返回 503。

### 查看最近审计记录

```http
GET /api/audit/queries?limit=50
```

`limit` 在存储层被限制为 1–100。当前项目只有单一 Basic Auth 管理员，因此该接口对认证用户开放；接入多用户权限后，应单独增加 `audit:read` 管理权限。

## 7. 项目中的应用

- `Web_app.py/lifespan`：初始化独立审计存储并执行保留期清理；
- `Web_app.py/query`：请求前落 `in_progress`，完成或异常后更新；
- `Web_app.py/feedback`：保存用户反馈；
- `Web_app.py/audit_queries`：提供最近记录查询；
- `web/index.html` 与 `web/app.js`：结果区域提供正确/错误按钮及备注框；
- `Config.py`：解析审计配置；
- `tests/test_audit.py`：验证生命周期、反馈更新、脱敏及关闭问题存储。

反馈数据后续可用于：

- 按 `incorrect + error_types` 组织失败案例分析；
- 构造人工复核队列；
- 评估 Schema RAG 表召回与最终执行正确率之间的关系；
- 经人工确认后转为 SFT/偏好学习样本。用户反馈本身不是标准答案，不能未经复核直接作为 GRPO Reward。

## 8. 验证方法

```bash
python -m unittest tests.test_audit -v
python -m unittest discover -s tests -v
python Test_reward_engine.py
```

HTTP 验证流程：

```bash
# 1. 发起查询并保存响应中的 request_id
curl -u superadmin:admin -H 'Content-Type: application/json' \
  -d '{"question":"各部门的平均薪资是多少？"}' \
  http://127.0.0.1:8080/api/query

# 2. 使用该 request_id 提交反馈
curl -u superadmin:admin -H 'Content-Type: application/json' \
  -d '{"request_id":"替换为返回值","verdict":"correct","comment":"结果已核对"}' \
  http://127.0.0.1:8080/api/feedback

# 3. 查看记录
curl -u superadmin:admin \
  'http://127.0.0.1:8080/api/audit/queries?limit=10'
```

## 9. 已知限制

- 当前是单机 SQLite 审计库，不适合多个 Web 副本并发写入；生产集群应迁移至 PostgreSQL/日志平台。
- 当前只有保留天数清理，没有按容量、租户或合规冻结策略归档。
- `in_progress` 记录可帮助发现进程中断，但当前没有后台任务将长期未完成记录标记为 `aborted`。
- 尚未实现审计管理页面、条件检索、CSV 导出、指标看板和 RBAC。
- 反馈是用户主观标注，不等同于 BIRD execution accuracy 或经过验证的标准答案。
