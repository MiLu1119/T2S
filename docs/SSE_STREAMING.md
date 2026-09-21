# SSE 流式响应

## 1. 目标与边界

Text-to-SQL 请求可能在模型生成、Schema RAG、SQL 校验或数据库执行阶段停留数秒。SSE 模块让 Web 页面在最终结果产生前持续收到 LangGraph 节点进度，避免用户只能等待一个无反馈的长请求。

当前实现流式传输的是“工作流进度 + 最终完整结果”，不是 LLM token 逐字输出。SQL 必须完整生成并通过 AST 校验后才能执行，因此页面不会提前展示未经校验的 SQL 片段。

原有 `POST /api/query` 同步接口继续保留，兼容已有调用方。

## 2. 接口

```http
POST /api/query/stream
Accept: text/event-stream
Content-Type: application/json

{
  "question": "各部门平均薪资是多少？",
  "thread_id": "可选会话ID"
}
```

因为请求需要 JSON body 和 Basic Auth，浏览器使用 `fetch()` + `ReadableStream` 消费响应，而不是只支持 GET 的原生 `EventSource`。

响应头：

```text
Content-Type: text/event-stream
Cache-Control: no-cache, no-transform
Connection: keep-alive
X-Accel-Buffering: no
```

## 3. 事件协议

### `start`

连接建立后立即返回请求标识：

```text
event: start
data: {"request_id":"...","thread_id":"...","message":"Agent 已开始运行"}
```

浏览器收到后立即保存 `thread_id`，即使后续失败也可以定位本次会话。

### `progress`

每个 LangGraph 节点完成后返回：

```text
event: progress
data: {"sequence":2,"node":"generate_sql","message":"正在生成 SQL","duration_ms":1450,"status":"completed"}
```

可能出现的节点：

- `get_schema_context`：Schema RAG；
- `generate_sql`：模型生成；
- `dynamic_retrieve`：ETC 二次检索；
- `validate_sql`：SQLGlot AST 校验；
- `execute_sql`：只读执行；
- `reflect`：错误分析与路由；
- `generate_answer`：结果整理；
- `human_handoff`：人工兜底。

`duration_ms` 是从上一个节点更新到当前节点完成的墙钟耗时，包含该 LangGraph superstep 周边的 checkpoint 开销，不等同于 OpenTelemetry 后端中某个 Span 的纯函数耗时。进度事件不包含 Prompt、Schema Context、字段样例、SQL 结果行或异常堆栈。

### `final`

成功完成状态图后，返回与同步 `QueryResponse` 相同的完整 JSON，包括：

- `request_id`、`thread_id`；
- 最终 SQL 和自然语言答案；
- columns/rows；
- 检索、AST、重试和截断信息。
- `trace_id`、总耗时、节点时间线、模型调用数和 provider 返回的 Token usage。

### `error`

状态图发生未处理异常时：

```text
event: error
data: {"request_id":"...","thread_id":"...","detail":"Agent execution failed"}
```

具体 provider 异常只写服务端日志和分类审计，不通过 SSE 暴露。

### 心跳

如果 15 秒没有节点完成，服务发送 SSE 注释：

```text
: heartbeat
```

它用于降低反向代理或负载均衡器关闭空闲连接的概率，前端无需处理。

## 4. 服务端实现

`Web_app.py/query_stream` 使用后台线程执行同步 LangGraph：

```text
async StreamingResponse
  -> asyncio.Queue
  -> worker thread
     -> graph.stream(stream_mode="updates")
     -> 节点更新写入 Queue
  -> async generator 编码 SSE
  -> final/error
```

采用后台线程是因为当前 LLM、数据库和 LangGraph 链路是同步实现；如果直接在事件循环中执行，会阻塞其他 FastAPI 请求。

SSE `start` 事件返回 `request_id` 后，客户端可以调用 `POST /api/requests/{request_id}/cancel`。取消标记会在 LangGraph 节点边界生效；SQLite 正在执行的 SQL 会通过 `interrupt()` 中断，PostgreSQL 会调用驱动 `cancel()`。浏览器断开 SSE 时服务端也会设置取消标记。已经发出的外部模型 HTTP 请求和 MySQL 正在执行的语句仍不能保证立即中断，详见 `QUERY_GUARDRAILS_ADMIN_CENTER.md`。

## 5. 审计与会话

- SSE 与同步接口共用 `prepare_query`、`complete_query` 和失败审计逻辑；
- 一次流式请求只产生一条 `request_id` 审计记录；
- `thread_id` 继续使用 Redis/LangGraph checkpoint；
- `final` 事件产生后可以使用同一 `request_id` 提交反馈；
- 客户端断线不会把已完成节点伪装为成功，最终异常记录为 `provider_error`。

## 6. 前端应用

`web/app.js`：

1. POST `/api/query/stream`；
2. 用 `ReadableStream.getReader()` 增量读取；
3. 处理可能被网络分片拆开的 SSE block；
4. 把每个 `progress` 追加到常驻“执行流程”时间线，并显示节点耗时；
5. 收到 `final` 后保留时间线，展示总耗时、模型调用、Token、RAG、AST、Redis 和 Trace ID；
6. 收到 `error` 或流提前结束时显示错误框。

所有展示文本继续经过 `textContent` 或 `escapeHtml`，不直接把 SSE 数据作为不受信任 HTML 注入页面。

## 7. 验证

自动化测试：

```bash
python -m unittest tests.test_sse -v
python -m unittest discover -s tests -v
python Test_reward_engine.py
```

命令行端到端验证：

```bash
curl -N -u superadmin:admin \
  -H 'Accept: text/event-stream' \
  -H 'Content-Type: application/json' \
  -d '{"question":"各部门的平均薪资是多少？"}' \
  http://127.0.0.1:8080/api/query/stream
```

应依次看到 `start`、多个 `progress` 和一个 `final`；异常路径看到 `error`。

### 当前环境验证记录（2026-09-18）

使用正式 Redis checkpoint 和 `ark-code-latest` 外部模型完成真实 SSE 请求，实际事件顺序为：

```text
start
progress: get_schema_context
progress: generate_sql
progress: validate_sql
progress: execute_sql
progress: reflect
progress: generate_answer
final
```

最终 SQL 正确完成部门与员工表 JOIN、平均薪资聚合和降序排列；审计只产生一条 `success` 记录，实测 Agent 延迟为 2769 ms。该数字仅是本次单请求实测，不代表稳定性能基准。

另使用不可达的本地模型地址验证失败路径：响应只包含 `start`、已完成节点的 `progress` 和脱敏的 `error`，没有向客户端发送异常堆栈；审计记录为 `provider_error`，错误分类为 `RuntimeError`。

## 8. 已知限制

- 当前不是 token streaming，而是 LangGraph 节点级 streaming；
- HTTP 中途断开会请求协作式取消，但无法强制终止已经发出的外部模型请求；
- 同一 `thread_id` 的并发请求仍应由客户端串行发送；
- 生产反向代理仍需关闭响应缓冲并设置足够长的 read timeout；
- 浏览器刷新后不支持重放已丢失的事件，当前未实现 SSE `Last-Event-ID`；
- `POST` SSE 不能直接使用浏览器原生 `EventSource`。
