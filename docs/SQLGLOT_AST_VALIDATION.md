# SQLGlot AST 安全校验与 Schema 验证

## 1. 模块定位

本模块是 VeriSQL Agent 在线推理链路中的执行前校验层。它位于“大模型生成 SQL”和“数据库执行 SQL”之间，负责把 SQL 文本解析为抽象语法树（AST），从语法结构而不是关键词字符串判断查询是否安全，并根据目标 SQLite 数据库的真实 Schema 校验表名和字段名。

```text
用户问题
  ↓
LLM 生成 SQL
  ↓
SQLGlot AST 校验
  ├── 单语句限制
  ├── 只读查询限制
  ├── 危险 AST 节点检查
  ├── 真实表名检查
  ├── 真实字段检查
  └── 表别名解析
  ↓
SQLite 只读执行
  ↓
结果返回 / 错误驱动重写
```

SQLGlot 校验并不替代数据库的只读连接、执行超时和结果行数限制。当前系统采用多层防御：

1. SQLGlot 在执行前检查 SQL 结构和 Schema。
2. `Executor.py` 使用 SQLite `mode=ro` 只读连接。
3. 执行器限制运行时间和最大结果行数。
4. 校验或执行失败后，LangGraph 将结构化错误交给模型重写。

## 2. 相关文件

| 文件 | 作用 |
| --- | --- |
| `Sql_validator.py` | AST 解析、危险节点检查和 SQLite Schema 验证。 |
| `Graph.py` | 将 AST 校验接入 LangGraph，并将失败路由至反思节点。 |
| `State.py` | 定义 `ast_validation` 和 `validation_error_type` 状态字段。 |
| `Web_app.py` | 在 `/api/query` 响应中返回 AST 校验详情。 |
| `web/app.js` | 在 Web 运行轨迹中展示 AST 校验状态。 |
| `tests/test_sql_validator.py` | AST 与 Schema 验证单元测试。 |
| `Executor.py` | 校验通过后的 SQLite 只读执行防线。 |

运行依赖：

```text
sqlglot>=27,<29
```

当前已验证版本为 `sqlglot 28.10.1`。

## 3. 对外接口

核心函数：

```python
from Sql_validator import validate_sql_ast

result = validate_sql_ast(
    sql="SELECT name FROM employees",
    db_path="demo.sqlite",
    dialect="sqlite",
)
```

函数签名：

```python
def validate_sql_ast(
    sql: str,
    db_path: str,
    dialect: str = "sqlite",
) -> SQLValidationResult:
    ...
```

返回对象 `SQLValidationResult` 包含：

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `is_valid` | `bool` | 是否允许进入数据库执行。 |
| `error_type` | `str` | 失败分类，成功时为空字符串。 |
| `reason` | `str` | 可交给模型和日志系统的错误说明。 |
| `used_tables` | `list[str]` | AST 中引用的真实物理表。 |
| `used_columns` | `list[str]` | AST 中引用的字段，包括别名前缀。 |
| `suggestions` | `list[str]` | 表名或字段名的近似候选。 |

结果可以通过 `to_dict()` 转为 JSON 兼容字典：

```python
{
    "is_valid": True,
    "error_type": "",
    "reason": "",
    "used_tables": ["departments", "employees"],
    "used_columns": ["d.dept_id", "d.dept_name", "e.dept_id", "e.salary"],
    "suggestions": [],
}
```

## 4. 校验步骤

### 4.1 空 SQL 检查

空字符串或只包含空白字符的输入返回：

```json
{
  "is_valid": false,
  "error_type": "empty_sql",
  "reason": "SQL is empty"
}
```

### 4.2 SQLite 方言解析

模块默认使用：

```python
sqlglot.parse(sql, read="sqlite")
```

无法解析时返回 `syntax`，SQL 不会进入执行器。

### 4.3 单语句限制

一次请求只允许一条 SQL：

```sql
SELECT name FROM employees;
DROP TABLE employees;
```

以上输入会返回 `stacked_query`。

### 4.4 只读查询与危险节点检查

当前拒绝的 AST 节点包括：

- `Insert`
- `Update`
- `Delete`
- `Drop`
- `Create`
- `Alter`
- `Merge`
- `Command`
- `Transaction`

根节点还必须属于 SQLGlot 的 `Query` 类型，因此 `PRAGMA`、事务控制和其他非查询命令不会进入执行器。

与关键词搜索相比，AST 能正确区分语法节点和字符串常量。例如：

```sql
SELECT 'PRAGMA is only text' AS note;
```

其中 `PRAGMA` 只是字符串内容，不会被误判为数据库命令。

### 4.5 加载真实 SQLite Schema

模块通过只读连接读取目标数据库：

```sql
SELECT name
FROM sqlite_master
WHERE type IN ('table', 'view')
  AND name NOT LIKE 'sqlite_%';
```

随后使用 `PRAGMA table_info(...)` 获取每张表或视图的字段。标识符经过双引号转义，避免 Schema 名称破坏读取语句。

内部 Schema 结构为：

```python
{
    "departments": {"dept_id", "dept_name"},
    "employees": {"emp_id", "name", "dept_id", "salary", "hire_date"},
    "sales": {"sale_id", "emp_id", "product", "amount", "sale_date"},
}
```

### 4.6 表名验证

模块提取 AST 中的 `Table` 节点，并与真实 Schema 比较。

输入：

```sql
SELECT name FROM employee;
```

返回：

```json
{
  "error_type": "unknown_table",
  "reason": "unknown table `employee`",
  "suggestions": ["employees"]
}
```

候选建议由 `difflib.get_close_matches` 生成，用于提高模型重写成功率。CTE 名称不作为物理表进行验证。

### 4.7 字段与表别名验证

对于：

```sql
SELECT e.salary
FROM employees AS e;
```

模块建立：

```text
e → employees
```

然后确认 `salary` 是否存在于 `employees`。

字段写错时：

```sql
SELECT e.emp_name FROM employees AS e;
```

返回：

```json
{
  "error_type": "unknown_column",
  "reason": "unknown column `emp_name` on table `employees`",
  "suggestions": ["name"]
}
```

无表前缀字段会在本次查询引用的所有物理表中查找。`SELECT` 输出别名不会被误认为数据库字段。

## 5. 错误类型

| `error_type` | 含义 | LangGraph 行为 |
| --- | --- | --- |
| `empty_sql` | 模型没有生成 SQL。 | 记录错误并重新生成。 |
| `syntax` | SQLGlot 无法解析。 | 将解析错误交给模型重写。 |
| `stacked_query` | 一次包含多条语句。 | 拒绝执行并重新生成。 |
| `unsafe_statement` | 写操作、命令或其他非只读语句。 | 拒绝执行并重新生成。 |
| `schema_load` | 无法读取目标数据库 Schema。 | 记录基础设施错误；当前会进入重试/兜底。 |
| `unknown_table` | SQL 引用了不存在的物理表。 | 携带候选表重新生成。 |
| `unknown_column` | 字段不存在于引用的表中。 | 携带候选字段重新生成。 |

## 6. LangGraph 集成细节

`Graph.py` 中的 `validate_sql` 节点调用：

```python
result = validate_sql_ast(state["current_sql"], state["db_path"])
```

并写入状态：

```python
{
    "safety_check_passed": result.is_valid,
    "safety_reason": result.reason,
    "validation_error_type": result.error_type,
    "ast_validation": result.to_dict(),
}
```

路由规则：

```text
validate_sql
  ├── is_valid=true  → execute_sql
  └── is_valid=false → reflect
```

`reflect` 将本轮 SQL、错误分类和错误说明写入 `error_history`：

```python
{
    "sql": "SELECT e.emp_name FROM employees e",
    "error": "unknown column `emp_name` on table `employees`",
    "error_type": "unknown_column",
}
```

下一轮 `generate_sql` 会把这段结构化错误加入 Prompt。重试次数达到上限或连续生成相同错误 SQL 时，工作流转入 `human_handoff`。

## 7. Web API 中的使用

查询接口：

```http
POST /api/query
Content-Type: application/json

{"question": "各部门的平均薪资是多少？"}
```

响应中的 `ast_validation` 示例：

```json
{
  "ast_validation": {
    "is_valid": true,
    "error_type": "",
    "reason": "",
    "used_tables": ["departments", "employees"],
    "used_columns": [
      "d.dept_id",
      "d.dept_name",
      "e.dept_id",
      "e.salary"
    ],
    "suggestions": []
  }
}
```

Web 页面在“Agent 运行轨迹”区域展示：

```text
AST 校验：通过 · departments, employees
```

如果最终 SQL 仍未通过校验，错误详情会同时出现在 `errors` 和人工兜底回答中。

## 8. 测试与验证

运行全部单元测试：

```bash
.venv/bin/python -m unittest discover -s tests -v
```

仅运行 AST 测试：

```bash
.venv/bin/python -m unittest tests.test_sql_validator -v
```

当前覆盖：

- 正常 JOIN 的表字段提取；
- 不存在的表和候选建议；
- 不存在的限定字段和候选建议；
- 写操作拦截；
- 堆叠语句拦截；
- 字符串关键词不误报；
- CTE 查询兼容。

手动调用：

```bash
.venv/bin/python - <<'PY'
from Sql_validator import validate_sql_ast

result = validate_sql_ast(
    "SELECT e.salary FROM employees e",
    "demo.sqlite",
)
print(result.to_dict())
PY
```

## 9. 与原 `Safety.py` 的关系

当前分工如下：

- 在线 LangGraph Agent：使用 `Sql_validator.py` 的 SQLGlot AST 校验。
- GRPO Reward Engine 与历史 BIRD 评测：暂时继续使用 `Safety.py` 的 `sqlparse` 校验。

保留历史训练口径是为了避免在没有重新评测的情况下改变 reward 分布。后续如果统一训练与在线口径，应：

1. 在 BIRD train/dev 上比较两种校验器的分类差异。
2. 检查语法错误、安全违规和 Schema 错误的 reward 是否发生变化。
3. 为 SQLGlot 版本建立固定回归测试。
4. 重新运行 baseline、SFT 和 GRPO reward 诊断。
5. 确认无异常后再让 `Reward.py` 切换到 SQLGlot。

## 10. 已知限制

### 10.1 派生表和复杂 CTE 字段血缘

当前会验证 CTE 内部引用的真实表和字段，但对 CTE/派生表输出字段采用保守策略，部分字段合法性最终由 SQLite 确认。

完整支持需要基于 SQLGlot scope/lineage 分析建立每个查询作用域的输出 Schema。

### 10.2 无限定字段的歧义

如果某字段同时存在于多张表中，当前验证器只确认字段至少存在，不主动判定歧义。SQLite 执行时仍会返回 `ambiguous column name`。

后续可增加 `ambiguous_column` 错误，并要求模型使用表别名限定字段。

### 10.3 不能判断业务语义正确性

AST 校验只能证明 SQL 在结构和 Schema 上合法，不能证明它正确回答了用户问题。例如下面两条查询都可能通过校验：

```sql
SELECT SUM(amount) FROM sales;
SELECT AVG(amount) FROM sales;
```

“总销售额”应该选择哪条，需要问题理解、业务指标定义、RAG 或 execution accuracy 判断。

### 10.4 不是性能评估器

AST 可以统计 JOIN、子查询和函数结构，但无法准确判断数据库执行成本。查询性能需要下一阶段的 `EXPLAIN` 成本检查。

### 10.5 当前只针对 SQLite

解析方言和 Schema 加载方式当前都是 SQLite。接入 PostgreSQL/MySQL 时需要分别实现：

- 对应 SQLGlot dialect；
- `information_schema`/系统目录读取；
- 数据库方言专属危险函数与命令策略；
- 大小写、默认 Schema 和搜索路径规则。

## 11. 后续扩展建议

推荐按以下顺序扩展：

1. 加入作用域分析，完整验证 CTE、子查询和派生表字段。
2. 检测无限定同名字段并返回 `ambiguous_column`。
3. 接入用户身份，按 AST 中的表和字段执行权限过滤。
4. 增加 JOIN 数量、子查询深度、递归 CTE 和函数白名单策略。
5. 在 AST 校验通过后执行 `EXPLAIN` 成本检查。
6. 为 PostgreSQL/MySQL 增加独立 Schema Provider。
7. 将 AST 错误类型映射为定向 Schema RAG：
   - `unknown_table` → 表级检索；
   - `unknown_column` → 字段级检索；
   - `ambiguous_column` → 表关系与别名提示；
   - JOIN 问题 → 外键路径检索。
8. 完成回归实验后，将 GRPO Reward Engine 迁移到同一校验器。

## 12. 安全边界

即使 AST 校验通过，也必须保持：

- 数据库账号或连接为只读；
- 查询超时；
- 最大结果行数；
- 并发和速率限制；
- 敏感表、字段和行级权限；
- 审计日志；
- 对外服务鉴权。

AST 是执行前的结构化安全网关，不是单一的最终安全边界。
