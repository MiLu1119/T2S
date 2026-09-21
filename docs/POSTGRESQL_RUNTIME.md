# PostgreSQL 接入、连接池与只读事务

## 1. 模块目标

该模块将 VeriSQL Agent 从固定的本地 `demo.sqlite` 扩展为可配置的数据源运行时。系统保留 SQLite 作为零配置演示后端，同时支持通过 PostgreSQL 连接池查询真实业务数据库。

它解决三个问题：

1. **真实数据源接入**：Agent 可以查询独立部署的 PostgreSQL，而不是只能查询本地文件。
2. **并发连接治理**：连接池复用数据库连接并限制最大并发，避免每个 Web 请求都重新连接数据库。
3. **数据库级只读保护**：所有模型 SQL 都在显式只读事务中执行，即使 AST 校验漏判也不能写入数据。

```text
Web/API 请求
  ↓
LangGraph 生成 SQL
  ↓
SQLGlot PostgreSQL AST + information_schema 校验
  ↓
从 psycopg 连接池借用连接
  ↓
BEGIN READ ONLY
  ↓
SET LOCAL statement_timeout
  ↓
执行查询并读取 max_rows + 1
  ↓
ROLLBACK
  ↓
连接归还连接池
```

## 2. 相关文件

| 文件 | 作用 |
| --- | --- |
| `Database.py` | SQLite/PostgreSQL 统一数据源接口、连接池和只读执行。 |
| `Config.py` | 数据库后端、DSN、允许 Schema 和连接池参数。 |
| `Graph.py` | 使用当前数据源构造 Schema Prompt、执行 AST 验证和 SQL 查询。 |
| `Sql_validator.py` | 支持外部 Schema 映射及 PostgreSQL 方言、限定表名验证。 |
| `Executor.py` | SQLite 执行器，现支持结果截断检测。 |
| `Web_app.py` | 在应用生命周期内打开/关闭连接池。 |
| `tests/test_database.py` | 连接池生命周期、只读事务、超时、回滚和截断测试。 |

依赖：

```text
psycopg[binary,pool]>=3.2,<4
```

当前验证版本：`psycopg 3.3.5`、`psycopg-pool 3.3.1`。

## 3. 统一数据源接口

`Database.py` 定义 `AgentDatabase` 协议：

```python
class AgentDatabase(Protocol):
    dialect: str
    display_name: str

    def open(self) -> None: ...
    def close(self) -> None: ...
    def get_schema(self) -> dict[str, set[str]]: ...
    def execute(
        self,
        sql: str,
        timeout_sec: float,
        max_rows: int,
    ) -> ExecutionResult: ...
```

当前实现：

- `SQLiteDatabase`：包装现有 SQLite 只读执行器。
- `PostgreSQLDatabase`：使用 psycopg 连接池和显式只读事务。

`build_database(settings)` 根据 `DATABASE_BACKEND` 创建对应实现，LangGraph 不需要了解底层连接方式。

## 4. 配置方法

### 4.1 保持 SQLite 演示模式

`.env`：

```env
DATABASE_BACKEND=sqlite
DB_MAX_ROWS=1000
```

未设置 `DATABASE_BACKEND` 时默认使用 SQLite。

### 4.2 使用 PostgreSQL

```env
DATABASE_BACKEND=postgresql
DATABASE_URL=postgresql://text2sql_reader:strong-password@db-host:5432/business_db
DATABASE_SCHEMAS=analytics,public

DB_POOL_MIN_SIZE=1
DB_POOL_MAX_SIZE=10
DB_POOL_TIMEOUT_SEC=5
DB_POOL_MAX_LIFETIME_SEC=1800
DB_MAX_ROWS=1000

SQL_TIMEOUT_SEC=5
```

配置解释：

| 配置项 | 含义 |
| --- | --- |
| `DATABASE_BACKEND` | `sqlite` 或 `postgresql`。 |
| `DATABASE_URL` | PostgreSQL DSN，仅服务端读取。 |
| `DATABASE_SCHEMAS` | 允许暴露给 Agent 的 PostgreSQL Schema，逗号分隔。 |
| `DB_POOL_MIN_SIZE` | 连接池最小连接数。 |
| `DB_POOL_MAX_SIZE` | 最大连接数，也是数据库并发闸门。 |
| `DB_POOL_TIMEOUT_SEC` | 请求等待空闲连接的最长时间。 |
| `DB_POOL_MAX_LIFETIME_SEC` | 连接最大生命周期。 |
| `DB_MAX_ROWS` | 单次最多返回给 Agent/Web 的数据行数。 |
| `SQL_TIMEOUT_SEC` | 单条 SQL 的数据库级执行超时。 |

切换配置后必须重启服务：

```bash
.venv/bin/uvicorn Web_app:app --host 0.0.0.0 --port 8080
```

## 5. 数据库只读角色

代码中的只读事务不能替代数据库权限。生产环境必须为 Agent 创建专用只读角色。

示例（请由数据库管理员根据实际 Schema 调整）：

```sql
CREATE ROLE text2sql_reader LOGIN PASSWORD 'replace-with-a-strong-password';

GRANT CONNECT ON DATABASE business_db TO text2sql_reader;
GRANT USAGE ON SCHEMA analytics TO text2sql_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA analytics TO text2sql_reader;

ALTER DEFAULT PRIVILEGES IN SCHEMA analytics
GRANT SELECT ON TABLES TO text2sql_reader;

ALTER ROLE text2sql_reader SET default_transaction_read_only = on;
```

不要为该角色授予：

- `INSERT`
- `UPDATE`
- `DELETE`
- `TRUNCATE`
- `CREATE`
- `ALTER`
- `DROP`

`ALTER DEFAULT PRIVILEGES` 只影响之后由对应对象所有者创建的表，应确认执行该命令的角色与业务表所有者一致。

## 6. 连接池生命周期

FastAPI 启动时：

```python
app.state.database = build_database(settings)
app.state.database.open()
```

应用关闭时：

```python
app.state.database.close()
```

PostgreSQL 连接池使用：

```python
ConnectionPool(
    min_size=...,
    max_size=...,
    timeout=...,
    max_lifetime=...,
    kwargs={
        "autocommit": False,
        "options": "-c default_transaction_read_only=on",
    },
)
```

连接建立时就设置 `default_transaction_read_only=on`。每次执行仍显式使用 `BEGIN READ ONLY`，形成双重只读保护。

连接池限制的是数据库并发，不等同于 Web 限流。后续仍应增加用户级速率限制和任务队列保护。

## 7. PostgreSQL Schema 加载

数据源从 `information_schema.columns` 读取允许 Schema 中的表字段：

```sql
SELECT table_schema, table_name, column_name
FROM information_schema.columns
WHERE table_schema = ANY(%s)
ORDER BY table_schema, table_name, ordinal_position;
```

内部表示：

```python
{
    "analytics.orders": {"order_id", "customer_id", "amount"},
    "analytics.customers": {"customer_id", "name", "region_id"},
}
```

该 Schema 同时用于：

1. 构造发送给模型的实际数据库 Schema Prompt。
2. SQLGlot PostgreSQL 方言的表字段验证。
3. 限制模型只能看到 `DATABASE_SCHEMAS` 允许的命名空间。

Schema 结果在进程内缓存。数据库 DDL 发生变化后，当前版本需要重启服务，或调用 `get_schema(refresh=True)` 刷新。后续 Schema RAG/增量索引模块应接管刷新机制。

## 8. PostgreSQL AST 校验

PostgreSQL 模式使用：

```python
validate_sql_ast(
    sql,
    dialect="postgres",
    schema=database.get_schema(),
)
```

支持 Schema 限定表名：

```sql
SELECT o.amount
FROM analytics.orders AS o;
```

如果同一个短表名存在于多个 Schema：

```text
analytics.orders
archive.orders
```

而模型只写：

```sql
SELECT amount FROM orders;
```

验证器返回：

```json
{
  "error_type": "ambiguous_table",
  "reason": "table `orders` exists in multiple schemas; qualify it explicitly",
  "suggestions": ["analytics.orders", "archive.orders"]
}
```

## 9. 只读事务与数据库级超时

每次查询的执行顺序：

```sql
BEGIN READ ONLY;
SELECT set_config('statement_timeout', '5000', true);
-- 模型生成的 SELECT
ROLLBACK;
```

说明：

- `BEGIN READ ONLY` 禁止事务中的写操作。
- `set_config(..., true)` 仅对当前事务生效。
- 超时由 PostgreSQL 主动取消查询，而不是只由 Python 放弃等待。
- 无论成功还是失败均执行 `ROLLBACK`，清理事务和局部配置后再将连接归还池。

错误分类：

| PostgreSQL 错误 | Agent `error_type` |
| --- | --- |
| `QueryCanceled` | `timeout` |
| `UndefinedTable` / `UndefinedColumn` / `AmbiguousColumn` | `schema` |
| `InsufficientPrivilege` / `ReadOnlySqlTransaction` | `permission` |
| `SyntaxError` | `syntax` |
| 其他异常 | `runtime` |

权限错误会被现有 LangGraph 直接路由至人工兜底，避免无意义重试。

## 10. 结果截断

数据库执行器读取：

```python
cursor.fetchmany(max_rows + 1)
```

若返回数量超过 `max_rows`：

- 只返回前 `max_rows` 行；
- `ExecutionResult.truncated = True`；
- Web API 返回 `"truncated": true`；
- 页面运行轨迹显示“结果截断：是”。

这避免了原来只读取 `max_rows` 行却无法判断结果是否完整的问题。

当前截断只保护返回内存，不会阻止数据库生成完整结果。更强的保护需要 AST 自动添加外层 `LIMIT` 或数据库游标/查询计划约束。

## 11. 测试方法

无需真实 PostgreSQL 即可运行连接池行为测试：

```bash
.venv/bin/python -m unittest tests.test_database -v
```

测试通过模拟连接池验证：

- 连接池打开和关闭；
- `information_schema` Schema 加载；
- `BEGIN READ ONLY`；
- `statement_timeout`；
- 查询结束后回滚；
- `max_rows + 1` 截断检测。

运行全部测试：

```bash
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python Test_reward_engine.py
```

真实 PostgreSQL 集成验证建议使用临时测试库，不要直接用生产库。至少验证：

1. 只读账号可以执行 `SELECT`。
2. `DELETE` 在数据库层被拒绝。
3. `pg_sleep` 或慢查询能被 `statement_timeout` 取消。
4. 超过 `DB_MAX_ROWS` 时返回截断标记。
5. 同时请求数超过连接池上限时不会创建无限连接。
6. 服务关闭后连接池正确释放。

## 12. 当前限制

- 当前没有可用 PostgreSQL 实例，因此已完成模拟池测试和 SQLite 回归，尚未完成真实 PostgreSQL 网络集成测试。
- Schema Prompt 当前包含允许 Schema 的全部表字段；大规模数据库仍需后续 Schema RAG。
- 当前尚未读取 PostgreSQL 外键、字段注释、枚举和样例值。
- Schema 缓存在 DDL 变化后不会自动失效。
- FastAPI 查询处理仍是同步调用，连接池可控制并发，但高并发版本可进一步迁移到 `AsyncConnectionPool`。
- 当前没有行级安全策略配置工具；生产环境可结合 PostgreSQL RLS。
- 已增加 `EXPLAIN (FORMAT JSON, COSTS TRUE)` 成本检查；默认告警、可配置为超过阈值时阻止执行。详见 `QUERY_GUARDRAILS_ADMIN_CENTER.md`。

## 13. 安全检查清单

切换真实 PostgreSQL 前必须确认：

- [ ] 使用专用只读数据库角色；
- [ ] 密码只存放于 `.env` 或密钥管理服务；
- [ ] `.env` 已被 `.gitignore` 排除；
- [ ] `DATABASE_SCHEMAS` 只包含允许访问的 Schema；
- [ ] 数据库安全组只允许应用服务器访问；
- [ ] 使用 TLS 数据库连接；
- [ ] 配置合理的连接池上限；
- [ ] 配置 `SQL_TIMEOUT_SEC` 和 `DB_MAX_ROWS`；
- [ ] 保留 SQLGlot AST 校验；
- [ ] 记录查询审计日志；
- [ ] 对敏感列实施列权限或视图隔离；
- [ ] 在测试库完成写操作拒绝和超时验证。
