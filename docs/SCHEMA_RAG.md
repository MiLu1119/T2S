# Schema 混合检索、外键补全与 Value Grounding

## 1. 目标与边界

当数据库表数量增大时，把完整 Schema 放入每次 Prompt 会增加 Token、噪声和字段误选。`Retrieval.py` 实现了一个 SQLite 基础 Schema RAG，在 SQL 生成前按问题检索相关数据库知识。

当前版本包含：

- 表级混合检索：BM25 风格词法分数 + 字符 3-gram 向量余弦相似度；
- 字段级重排：对候选表中的字段再次排序；
- 主键、外键强制保留；
- 外键图最短路径搜索与中间表补全；
- 显式白名单控制的真实值样例（Value Grounding）；
- 检索事件写入 API 响应，便于调试和评测。

这里的“向量”是本地字符 n-gram 稀疏向量，不是神经网络 Embedding。它无需额外模型或向量数据库，适合作为可复现基线；尚未实现 PostgreSQL Schema RAG、语义 Embedding、租户/用户权限过滤和离线索引。

## 2. 工作流

```text
自然语言问题
  -> 读取 SQLite Schema + schema_metadata.json
  -> 表级混合召回（最多 top_tables，且过滤弱相关表）
  -> 字段重排（最多 top_columns）
  -> 保留 PK/FK
  -> 外键图补齐 JOIN 中间表
  -> 对白名单字段抽取有限 DISTINCT 样例
  -> 生成精简 Schema Context
  -> LLM 生成 SQL
  -> SQLGlot AST 校验与只读执行
```

如果所有表都没有可靠匹配，检索器保留分数最高的一张表，避免生成空上下文。`SCHEMA_CONTEXT_MODE=full` 可随时退回原有完整 Schema 模式。

### 2.1 Schema Catalog 构建

`SQLiteSchemaRetriever.load_catalog()` 使用只读 SQLite 连接读取：

- `sqlite_master`：获得业务表清单；
- `PRAGMA table_info`：获得字段名、类型和主键标记；
- `PRAGMA foreign_key_list`：获得外键关系；
- `schema_metadata.json`：补充中文业务描述和值抽样白名单。

目录按数据库绝对路径缓存在进程内，避免每次请求重复扫描。它只缓存结构信息，不缓存整表业务数据。

### 2.2 表级混合召回

每张表被组织为以下检索文档：

```text
表名 + 表描述 + 字段名 + 字段类型 + 字段描述
```

查询由“自然语言问题 + 可选 SQL 前缀”组成。综合分数为：

```text
score(table) = 0.65 × BM25_like + 0.35 × char_3gram_cosine + exact_table_bonus
```

BM25 风格分数负责精确字段词和业务术语匹配，字符 3-gram 余弦相似度用于处理部分匹配、命名差异和中英文描述。精确提到表名时额外加分。候选表除了受 `top_tables` 限制，还必须达到最高分的 20%，以避免为了凑满 top-k 强行加入无关表。

### 2.3 字段重排与结构字段保留

候选表内部按词项重叠、字符相似度和字段名精确命中重排，只保留前 `top_columns` 个普通字段。随后无条件补回：

- 主键；
- 候选表之间的外键源字段；
- 候选表之间的外键目标字段。

因此字段裁剪不会破坏 SQL JOIN 所需的连接键。

### 2.4 外键路径补全

所有表是图节点，外键是无向可遍历边。对初始候选表两两执行 BFS 最短路径搜索；若两张业务表之间必须经过中间表，则中间表作为 `bridge_tables` 自动加入上下文。

例如：

```text
orders -> customers -> regions
```

当问题直接命中 `orders` 和 `regions` 时，系统会补入 `customers`，并向模型明确提供完整 JOIN 路径。

### 2.5 Value Grounding

Value Grounding 用于解决用户表达与数据库真实枚举值不一致的问题，例如用户说“工程部门”，数据库中实际保存 `Engineering`。系统对获准字段执行带数量上限的 `SELECT DISTINCT`，把少量真实值加入 Schema Context。

该能力不扫描数值字段，也不会默认抽样全部文本字段。只有 `value_grounding_columns` 白名单内的文本、日期或布尔字段可以抽样。

### 2.6 与 ETC 的关系

基础 RAG 在 `get_schema_context` 节点、SQL 生成之前执行。ETC 是其上层动态触发策略：当模型生成过程出现熵上升趋势时，`dynamic_retrieve` 会把问题和当前 SQL 前缀组合为新查询，再调用同一个 `SQLiteSchemaRetriever`。

```text
基础 RAG：问题 -> 检索 -> 首次 SQL 生成
ETC 升级：生成中不确定性上升 -> 问题 + SQL 前缀 -> 二次检索 -> 重新生成
```

因此基础 RAG 负责“检索什么”，ETC 负责“什么时候再次检索”。

## 3. 配置

```dotenv
SCHEMA_CONTEXT_MODE=rag
SCHEMA_RAG_TOP_TABLES=2
SCHEMA_RAG_TOP_COLUMNS=8
SCHEMA_RAG_VALUE_SAMPLES=5
```

- `SCHEMA_CONTEXT_MODE`：`full` 或 `rag`；默认 `full`，保证向后兼容。
- `SCHEMA_RAG_TOP_TABLES`：初始候选表上限；外键桥接表不占此名额。
- `SCHEMA_RAG_TOP_COLUMNS`：每张表重排后的普通字段上限；必要 PK/FK 会额外保留。
- `SCHEMA_RAG_VALUE_SAMPLES`：每个获准字段最多抽取的不同值数量；设为 `0` 可完全关闭。

当前 Schema RAG 只支持 SQLite。PostgreSQL 配置为 `rag` 时，Web 运行时会把有效模式降级为 `full`，`GET /api/config` 返回实际生效的模式。

## 4. 元数据与 Value Grounding

项目根目录的 `schema_metadata.json` 为表和字段补充业务描述：

```json
{
  "tables": {
    "departments": {
      "description": "部门、组织架构、团队信息",
      "value_grounding_columns": ["dept_name"],
      "columns": {"dept_name": "部门名称"}
    }
  }
}
```

`value_grounding_columns` 是安全白名单。只有列在其中的字段才能读取样例值；未知数据库或未声明字段默认不抽取真实值。姓名、手机号、证件号、地址、密钥等字段不应加入白名单。生产环境还应在检索前实施行列权限和脱敏。

## 5. 项目接入点

- `Retrieval.py`：目录加载、混合评分、字段重排、外键路径、值样例和上下文渲染。
- `schema_metadata.json`：业务语义和 Value Grounding 白名单。
- `Config.py`：环境变量解析。
- `Web_app.py`：启动时构造检索器，并把有效模式传入状态图。
- `Graph.py/get_schema_context`：SQL 生成前执行基础检索。
- `Graph.py/dynamic_retrieve`：ETC 触发后复用同一个检索器，以问题和 SQL 前缀再次检索。

`POST /api/query` 的 `retrieval_events` 会返回 `stage`、候选表、桥接表、字段、JOIN 路径、grounding 值和分数；不会返回拼接后的完整 Prompt。

### 5.1 LangGraph 中的运行时数据流

```text
Web_app.query
  -> 初始化 AgentState(question, db_path, retrieval_events=[])
  -> Graph.get_schema_context
     -> SQLiteSchemaRetriever.retrieve
     -> state.schema_context
     -> state.retrieval_events += initial event
  -> Graph.generate_sql
     -> 外部 API / 本地模型 / Mock 模型
  -> Graph.validate_sql
     -> SQLGlot AST 表字段与安全校验
  -> Graph.execute_sql
     -> SQLite 只读执行
  -> Graph.generate_answer
  -> QueryResponse
```

检索模块不直接生成 SQL，也不绕过后续安全层。无论检索结果如何，模型生成的 SQL 都必须继续通过 AST 校验和数据库只读执行约束。

### 5.2 检索事件示例

```json
{
  "stage": "initial",
  "initial_tables": ["employees", "departments"],
  "bridge_tables": [],
  "foreign_key_paths": [["employees", "departments"]],
  "grounded_values": {
    "departments.dept_name": ["'Sales'", "'Engineering'", "'Marketing'"]
  },
  "scores": {
    "employees": 10.334147,
    "departments": 5.066153
  }
}
```

前端或评测脚本可以根据该结构分析表召回、桥接表命中、检索频率和后续 SQL 正确性。

## 6. 验证方法

自动化测试：

```bash
python -m unittest tests.test_runtime -v
python -m unittest discover -s tests -v
python Test_reward_engine.py
```

Web 验证：

```bash
curl -u superadmin:admin http://127.0.0.1:8080/api/config
curl -u superadmin:admin \
  -H 'Content-Type: application/json' \
  -d '{"question":"各部门的平均薪资是多少？"}' \
  http://127.0.0.1:8080/api/query
```

响应中应看到 `schema_context_mode` 为 `rag`，且 `retrieval_events[0].stage` 为 `initial`。上述问题应召回 `employees`、`departments` 及其外键路径。

### 6.1 已完成的真实外部模型验证

2026-09-17 使用项目当前配置的 `ark-code-latest` 外部模型完成了真实请求：

```text
问题：各部门的平均薪资是多少？请返回部门名称和平均薪资，并按平均薪资从高到低排序。
```

运行结果：

- 基础 RAG 召回 `employees`、`departments`；
- 检出路径 `employees -> departments`；
- Value Grounding 仅发送白名单字段 `departments.dept_name` 的三个样例；
- 模型生成正确的 JOIN、AVG、GROUP BY 和 ORDER BY SQL；
- SQLGlot AST 校验通过；
- SQLite 执行成功，重试次数为 0；
- 返回 Engineering 117500、Sales 88500、Marketing 78000。

本次结果只证明当前演示问题的端到端链路已经跑通，不代表已经完成 BIRD 全量对比实验，也不代表 RAG 带来了可量化准确率提升。检索收益仍需在固定模型、固定数据集和对照组下评测。

### 6.2 自动化覆盖

`tests/test_runtime.py` 当前覆盖：

- 中英文问题能召回目标表和字段；
- 无关的低分表不会为了填满 top-k 被强制加入；
- 多跳外键路径能补齐桥接表；
- Value Grounding 只读取显式白名单字段；
- 未授权的员工姓名不会进入检索上下文。

## 7. 已知限制与后续升级

- 目录当前随进程缓存，不会自动感知在线 DDL；可调用 `load_catalog(..., refresh=True)` 刷新。
- 字符 n-gram 不能替代领域 Embedding；大规模 Schema 应增加持久化向量索引和离线增量更新。
- Value Grounding 当前是白名单字段的有限样例，不是按查询条件构造的高精度值索引。
- 当前没有用户/租户级 Schema 权限过滤；在实现权限模块前不可把同一实例直接用于多租户生产数据。
- ETC 可将本模块从“查询前固定检索”升级为“生成时按不确定性二次检索”，两者是基础层和触发策略的关系，不是平替。
