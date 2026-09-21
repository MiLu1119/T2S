# LangGraph Checkpoint 与 Redis 会话持久化

## 1. 目标

该模块为 Agent 增加两个不同层次的标识：

- `request_id`：一次 HTTP 查询，用于审计与反馈；
- `thread_id`：一段连续会话，用于 LangGraph 状态持久化和多轮追问。

相同用户携带相同 `thread_id` 再次查询时，系统恢复之前已完成轮次的问题、SQL 和状态摘要，用于理解“只看十万元以上”“再按人数排序”一类上下文依赖问题。服务重启后，只要 checkpoint 后端仍存在，会话也能恢复。

## 2. 架构

```text
Web 请求(question, thread_id?)
  -> 未提供 thread_id 时生成 UUID
  -> 内部 key = authenticated_user + ':' + thread_id
  -> LangGraph get_state 恢复 conversation_history
  -> 本轮瞬态状态重置
  -> graph.invoke(..., configurable.thread_id)
  -> 每个 super-step 自动 checkpoint
  -> 返回 request_id + thread_id
```

内部 checkpoint key 带认证用户名命名空间，避免两个用户使用相同公开 `thread_id` 时读到彼此会话。当前认证仍是单一 Basic Auth；扩展为多用户后必须继续保留该隔离规则。

## 3. 后端

### SQLite（默认）

```dotenv
CHECKPOINT_BACKEND=sqlite
CHECKPOINT_SQLITE_PATH=checkpoints.sqlite
```

使用官方 `langgraph-checkpoint-sqlite`。适用于本地开发、单机 Web 和功能演示，数据在服务重启后保留。它不适合多个应用副本高并发共享。

### 普通 Redis

```dotenv
CHECKPOINT_BACKEND=redis
REDIS_URL=redis://127.0.0.1:6379/0
CHECKPOINT_TTL_MINUTES=1440
```

`CHECKPOINT_BACKEND=redis` 使用项目实现的 `PlainRedisSaver`：

- 基于 Redis Hash，只保留每个 thread/namespace 的最新 checkpoint；
- TTL 以分钟为单位，并在读取时刷新；
- 适合多 Web 实例共享会话状态。

该模式不依赖 RedisJSON 或 RediSearch，适配当前项目已有的普通 Redis。它实现 LangGraph 同步执行所需的 latest checkpoint、pending writes、list、delete 和 TTL 接口；不提供跨 checkpoint 搜索和完整版本历史。

### Redis Stack / Redis 8 官方 Saver

```dotenv
CHECKPOINT_BACKEND=redis-stack
REDIS_URL=redis://:password@127.0.0.1:6379/0
```

该模式使用官方 `langgraph-checkpoint-redis` 的 `ShallowRedisSaver`，启动时调用 `setup()` 创建索引。它要求 RedisJSON 与 RediSearch；Redis 8 已内置，较旧版本应使用 Redis Stack。缺少模块时会明确启动失败，不会静默改用普通 Redis实现。

### 关闭

```dotenv
CHECKPOINT_BACKEND=none
```

此时每个请求仍能独立完成，但会话查询、删除和跨请求记忆不可用。

## 4. 会话状态设计

`AgentState.conversation_history` 每轮保存：

```json
{
  "question": "各部门的平均薪资是多少？",
  "sql": "SELECT ...",
  "status": "success"
}
```

最多将最近 `SESSION_HISTORY_TURNS` 轮送入下一次运行：

```dotenv
SESSION_HISTORY_TURNS=5
```

历史问题和 SQL 会同时参与：

- Schema RAG 表/字段召回；
- 首次 SQL 生成；
- ETC 触发后的问题 + SQL 前缀二次检索。

每一轮请求都会显式清空旧的 `schema_context`、SQL、错误、执行结果和检索轨迹，避免 checkpoint 把上一轮瞬态状态错误地当成本轮结果。

## 5. Checkpoint 数据最小化

`Checkpoint.py/SanitizedCheckpointer` 在官方 saver 写入前删除：

- `execution_rows`：数据库查询返回的具体数据行；
- `final_answer`：可能包含查询结果值的自然语言答案。

主请求运行时仍能正常返回这些字段，但持久化介质只保存恢复工作流所需的状态。会话历史只保存问题、SQL 和终态，不保存答案或结果行。

Web 查询工作台使用独立的 `WorkbenchStore` 短期保存结果行，用于分页、导出和图表；它不改变 Checkpoint 的过滤规则，也不属于审计存储。详细边界见 [`WORKBENCH_HISTORY_RESULTS.md`](WORKBENCH_HISTORY_RESULTS.md)。

需要注意：问题和生成 SQL 仍会进入 checkpoint，这是多轮语义所必需的。生产环境应设置合适的 Redis TTL、存储加密与访问控制；如果问题文本不允许持久化，应关闭 checkpoint，而不能只关闭审计问题记录。

## 6. 项目接入点

- `Checkpoint.py`：SQLite、普通 Redis、Redis Stack saver 工厂和持久化前脱敏包装；
- `Config.py`：后端、路径、TTL 和历史轮数配置；
- `Graph.py`：`compile(checkpointer=...)`，历史上下文拼接与轮次摘要写回；
- `State.py`：新增 `conversation_history`；
- `Web_app.py`：恢复状态、传递 `configurable.thread_id`、返回/查询/删除会话；
- `web/app.js`：使用 `localStorage` 保存当前 `thread_id`；
- `tests/test_checkpoint.py`：验证跨 graph 重建恢复、结果行不落盘和 Redis 缺配置失败。

## 7. API

### 新建会话或继续会话

不传 `thread_id` 会创建新会话：

```json
POST /api/query
{"question":"各部门平均薪资是多少？"}
```

响应：

```json
{
  "request_id":"一次请求ID",
  "thread_id":"会话ID",
  "status":"success"
}
```

继续追问：

```json
POST /api/query
{
  "thread_id":"上一次返回的会话ID",
  "question":"只保留平均薪资超过十万元的部门"
}
```

`thread_id` 只接受 1–128 位字母、数字、下划线或连字符。

### 查看会话摘要

```http
GET /api/sessions/{thread_id}
```

只返回 `conversation_history`，不返回底层 checkpoint、结果数据行或其他用户会话。

### 删除会话

```http
DELETE /api/sessions/{thread_id}
```

Web 页面“新会话”按钮会删除服务端当前 thread，并清理浏览器 `localStorage`。

## 8. 验证

```bash
python -m unittest tests.test_checkpoint -v
python -m unittest discover -s tests -v
python Test_reward_engine.py
```

SQLite 自动化测试执行以下过程：

1. 运行第一轮查询并保存 checkpoint；
2. 关闭 saver，模拟服务退出；
3. 重新打开同一个 SQLite 文件并重建 Graph；
4. 恢复历史并执行第二轮；
5. 验证历史轮数为 2；
6. 验证 checkpoint 中没有 `execution_rows` 和 `final_answer`。

普通 Redis 可直接验证 `PlainRedisSaver`；官方 `redis-stack` 路径则需要 Redis 8/Redis Stack。仅安装 Python 依赖和运行 Fake/Mock 测试不能宣称服务端链路已验证。

### 当前环境验证记录（2026-09-18）

当前服务器 Redis 为需要密码认证的普通 Redis，不提供 RediSearch（`FT.INFO` 不可用），因此正式环境选用 `CHECKPOINT_BACKEND=redis` 的 `PlainRedisSaver`，而不是 `redis-stack`。

已完成真实 Redis + 外部模型双轮验证：

1. 第一轮查询各部门平均薪资；
2. 第二轮只描述“基于上一轮，返回部门名称并按平均薪资降序”；
3. Redis 成功恢复第一轮问题和 SQL；
4. 外部模型生成包含部门名称和降序排序的正确 SQL；
5. 会话接口返回两轮历史；
6. 删除接口清除测试 thread；
7. 正式 `/api/config` 返回 `checkpoint_backend=redis`。

上述验证证明当前普通 Redis 会话链路已经跑通；`redis-stack` 官方 saver 路径仍未在本机验证，因为当前 Redis 缺少 Search/JSON 模块。

## 9. 已知限制

- 当前多轮语义采用“历史问题 + 历史 SQL”Prompt 拼接，没有单独的 query rewrite 模型；复杂指代仍可能失败。
- SQLiteSaver 会保存节点历史版本，虽然结果载荷已过滤，长期运行仍需要归档或切换 Redis shallow saver。
- 尚未实现 LangGraph `interrupt/resume` 人工审批节点；当前 checkpoint 用于轮次持久化和恢复，不代表已支持任意节点人工续跑。
- 同一 thread 的并发请求尚未增加分布式锁；客户端应串行发送同一会话请求。
- `redis-stack` 后端依赖 RedisJSON、RediSearch；普通 `redis` 后端不支持 checkpoint 搜索或完整版本历史。

## 10. 上游资料

- LangGraph 官方内存与持久化说明：<https://docs.langchain.com/oss/python/langgraph/add-memory>
- 官方 Redis checkpointer 实现：<https://github.com/redis-developer/langgraph-redis>
