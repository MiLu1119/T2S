# OpenTelemetry 与 Langfuse Agent Tracing

## 1. 目标

该模块为一次 Text-to-SQL 请求建立完整 Trace，并将 Schema RAG、模型生成、AST 校验、数据库执行、反思、Checkpoint 和 SSE 表示为嵌套 Span，用于定位错误和延迟瓶颈。

Tracing 不参与 SQL 决策，也不能替代审计日志：

- Trace 回答“一次请求内部每一步发生了什么、耗时多久”；
- Audit 回答“谁在什么时候执行了什么、最终结果与反馈是什么”；
- Checkpoint 负责恢复多轮会话状态。

## 2. Trace 结构

```text
HTTP POST /api/query/stream
└─ verisql.query.stream
   ├─ verisql.checkpoint.read
   ├─ verisql.schema.retrieve
   ├─ verisql.llm.generate_sql
   ├─ verisql.sql.validate
   ├─ verisql.sql.execute
   ├─ verisql.agent.reflect
   ├─ verisql.answer.generate
   └─ verisql.checkpoint.write / pending_writes
```

FastAPI 使用 OpenTelemetry instrumentation 产生 HTTP Server Span；项目代码补充领域 Span。同步接口根 Span 为 `verisql.query`，SSE 后台执行根 Span 为 `verisql.query.stream`。

Web 页面同时提供一个面向使用者的常驻执行流程面板。它通过 SSE 展示真实完成的 LangGraph 节点、节点间墙钟耗时、总耗时、模型调用次数、Token usage、RAG/AST/Redis 状态和 `trace_id`。该面板是轻量可观测视图，不是把 Langfuse 控制台嵌入页面；`trace_id` 用于进一步到 OpenTelemetry/Langfuse 后端查看完整 Span 树。

## 3. 采集属性

根请求：

- `verisql.request_id`；
- `langfuse.session.id`（对应公开 `thread_id`）；
- `user.id`；
- 数据库后端、Schema 模式、模型名；
- 最终状态、重试次数、行数和截断标记。

Schema RAG：

- 召回表数量、桥接表数量；
- 召回表名；
- initial/dynamic 检索阶段。

模型：

- `gen_ai.operation.name=generate_sql`；
- `gen_ai.request.model`；
- 历史轮数、重试次数；
- ETC 是否触发、logprobs 是否可用。

SQL：

- AST 是否有效、错误类型；
- 使用表数量和表名；
- 执行成功、结果行数、截断和分类错误。

Checkpoint：

- 后端类型；
- 读取是否命中；
- pending writes 数量；
- write/delete 的耗时和异常。

## 4. 隐私策略

当前手工 Span 明确不记录：

- 自然语言问题正文；
- 完整 Prompt 和 Schema Context；
- 生成 SQL 正文；
- 查询结果数据行和最终答案；
- Value Grounding 样例值；
- API Key、Redis 密码、DSN 和 Authorization Header；
- 服务端异常堆栈作为 Span 属性。

表名会作为诊断属性发送。如果表名本身属于敏感元数据，生产环境应进一步关闭 `verisql.schema.tables` 和 `verisql.sql.tables`，只保留数量或哈希。

FastAPI instrumentation 未配置请求/响应 Header 采集；不要在部署环境中额外开启 Authorization Header 捕获。

## 5. 配置

```dotenv
TRACING_ENABLED=true
TRACING_EXPORTER=console
OTEL_SERVICE_NAME=verisql-agent
OTEL_ENVIRONMENT=development
OTEL_SAMPLE_RATIO=1.0
```

`TRACING_EXPORTER`：

- `none`：创建 Span 但不导出，适合关闭外发；
- `console`：输出到服务端标准输出，用于本地验证；
- `otlp`：使用 OTLP/HTTP exporter，可连接 Langfuse 或其他 OTel 后端。

采样率范围为 0–1。生产环境可降低采样率，但错误 Trace 的独立采样策略尚未实现。

## 6. Langfuse 接入

Langfuse 支持 OTLP 接收。除上述配置外设置：

```dotenv
TRACING_EXPORTER=otlp
OTEL_EXPORTER_OTLP_ENDPOINT=https://cloud.langfuse.com/api/public/otel
OTEL_EXPORTER_OTLP_HEADERS=Authorization=Basic%20BASE64_PUBLIC_KEY_COLON_SECRET_KEY
```

自托管时将 endpoint 换成内部 Langfuse 地址。公钥、密钥及编码后的 Authorization 值只能进入 `.env` 或密钥管理系统，不得提交源码。

当前项目使用标准 OTLP exporter；没有 Langfuse 凭证时不会宣称 Langfuse UI 已收到 Trace。切换 exporter 后，应在 Langfuse 中核对 session、嵌套 Span、耗时和错误状态。

## 7. 项目接入点

- `Telemetry.py`：TracerProvider、采样、Console/OTLP exporter 和关闭 flush；
- `Web_app.py`：FastAPI instrumentation、同步/SSE 根 Span；
- `web/app.js`：实时节点时间线、运行指标和 Trace ID 展示；
- `Graph.py`：Agent 领域节点 Span；
- `Checkpoint.py`：Redis/SQLite checkpoint Span；
- `Config.py`：Tracing 配置；
- `tests/test_telemetry.py`：内存 exporter 验证 Span 层级和内容安全。

SDK 使用 `BatchSpanProcessor` 异步批量导出，服务关闭时由 lifespan 调用 `shutdown()` flush。

## 8. 验证

```bash
python -m unittest tests.test_telemetry -v
python -m unittest discover -s tests -v
python Test_reward_engine.py
```

自动化测试使用 `InMemorySpanExporter`：

1. 执行完整 Mock Agent；
2. 验证 Schema、LLM、AST、执行和回答 Span 存在；
3. 验证它们属于同一根 Trace；
4. 验证属性中没有问题正文、SQL 正文和结果值。

本地 Console 验证：

```bash
TRACING_ENABLED=true TRACING_EXPORTER=console \
uvicorn Web_app:app --host 127.0.0.1 --port 8080
```

发起一次请求后，服务端应输出 JSON Span；通过 `trace_id`、`parent_id` 检查父子关系。

### 当前环境验证记录（2026-09-18）

使用正式 Redis checkpoint、SSE 和 `ark-code-latest` 外部模型完成真实 Console Trace：

- HTTP Server Span、`verisql.query.stream` 与所有 Agent 子 Span 使用同一个 `trace_id`；
- Schema RAG、LLM、AST、SQLite 执行、回答和 Redis read/write 均有独立耗时；
- 本次模型生成 Span 约 2.97 秒，SQL 执行 Span 约 3.6 毫秒；
- 根 Agent Span 约 3.02 秒并标记 `status=success`、`row_count=3`；
- Redis 首次读取记录 `hit=false`，后续写入记录后端为 `PlainRedisSaver`；
- Console Span 属性中未出现问题正文、生成 SQL 正文、部门取值样例或结果行。

这些数字来自单次验证请求，只用于证明计时与父子关系正常，不作为性能基准。当前只验证了 Console exporter，尚未取得 Langfuse 凭证，因此没有验证 Langfuse UI/Cloud 展示。

## 9. 已知限制

- 当前没有 Langfuse API 凭证，因此尚未验证 Langfuse Cloud/UI 实际接收；
- 当前会提取 OpenAI-compatible 响应中的 prompt/completion/total token；provider 不返回 usage 时页面明确显示“未返回”；
- 尚未配置单价表，因此不能展示真实费用；
- 尚未实现 tail sampling、错误优先采样和独立 OTel Collector；
- SSE 客户端断开会设置协作式取消标记；外部模型网络请求和部分数据库驱动仍可能延迟到节点边界才停止；
- Console exporter 适合开发验证，不适合高流量生产环境。

## 10. 官方资料

- OpenTelemetry Python：<https://opentelemetry.io/docs/languages/python/>
- OpenTelemetry Context Propagation：<https://opentelemetry.io/docs/languages/python/propagation/>
- Langfuse Observability：<https://langfuse.com/docs/observability/overview>
- Langfuse OTLP：<https://langfuse.com/integrations/native/opentelemetry>
