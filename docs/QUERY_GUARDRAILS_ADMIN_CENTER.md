# 查询风险护栏与管理中心

## 目标与边界

本模块在 SQLGlot AST 校验之后、真实执行之前运行数据库 `EXPLAIN`，并在查询结束后根据可观测信号生成低置信度提示；SSE 查询支持按 `request_id` 协作式取消。管理员可以在 Web 管理中心查看查询审计、Trace 摘要、资源操作、用户反馈和在线评测指标。

置信度是工程风险评分，不是模型输出正确率，也不是校准概率。在线评测的“反馈验证准确率”只使用已收到 `correct/incorrect` 用户反馈的请求作为分母，不等同于 BIRD Execution Accuracy。

## Agent 链路

```text
生成 SQL
  -> SQLGlot AST 与 Schema 校验
  -> EXPLAIN 成本检查
  -> 只读执行
  -> 反思 / 回答
  -> 置信度评分 + 审计与 Trace 摘要
```

涉及的实现：

- `Graph.py`：`check_cost` 与 `cancel_query` 节点和路由。
- `Database.py`：SQLite、PostgreSQL、MySQL 的 `explain()`；PostgreSQL 与 SQLite 的执行中取消。
- `Executor.py`：SQLite 线程执行、超时与 `connection.interrupt()` 取消。
- `Query_quality.py`：基于状态、重试、ETC、logprobs、结果截断和执行计划的可解释评分。
- `Cancellation.py`：按请求所有者隔离的进程内取消注册表。
- `Audit.py`：Trace ID、Token、置信度、成本、取消状态和节点时间线持久化。
- `Web_app.py`：取消与管理 API、统一响应和 RBAC。
- `web/`：结果警告、取消按钮和管理中心页面。

## 成本检查

SQLite 使用 `EXPLAIN QUERY PLAN`，可以识别未命中索引的表扫描和临时 B-Tree，但 SQLite 不提供可靠的数值成本或估算行数。PostgreSQL 使用 `EXPLAIN (FORMAT JSON, COSTS TRUE)`；MySQL 使用 `EXPLAIN FORMAT=JSON`。计划仅在通过只读 AST 校验后生成。

```env
SQL_COST_ENFORCE=false
SQL_COST_MAX_ESTIMATED_COST=100000
SQL_COST_MAX_ESTIMATED_ROWS=1000000
SQL_COST_MAX_FULL_SCANS=3
```

默认只提示，不拦截，以兼容现有查询。启用 `SQL_COST_ENFORCE=true` 后，超过阈值的 SQL 不会进入真实执行，而是进入反思重写；达到重试上限后转人工。`EXPLAIN` 失败会记录为 `available=false` 并降级继续执行，不会把数据库驱动差异误判成危险 SQL。

## 低置信度提示

响应新增：

```json
{
  "confidence": {
    "score": 0.62,
    "level": "low",
    "low_confidence": true,
    "reasons": ["SQL 经历 2 次失败重写"],
    "method": "heuristic_runtime_signals",
    "calibrated_probability": false
  },
  "cost_check": {
    "available": true,
    "allowed": true,
    "level": "medium",
    "full_scans": 1,
    "warnings": ["执行计划包含 1 次未命中索引的全表扫描"]
  }
}
```

阈值由 `CONFIDENCE_WARNING_THRESHOLD` 控制，默认 `0.65`。没有 logprobs 时系统仍能用重试、执行状态、截断和成本信号提示风险，但会明确记录熵无法校准。

## 请求取消

SSE `start` 事件会先返回 `request_id`。浏览器点击“取消请求”后调用：

```http
POST /api/requests/{request_id}/cancel
```

接口要求 `query.execute` 权限，并校验请求所有者；具有 `audit.read` 的管理员可以取消其他用户的运行中请求。SQLite 可用 `interrupt()` 中断正在执行的 SQL，PostgreSQL 可调用驱动的 `cancel()`。模型 HTTP 请求和 MySQL 驱动执行只能在节点边界协作式停止，不能保证立刻中断底层网络调用。浏览器 SSE 断开也会设置取消标记。

取消注册表位于当前应用进程内，因此多副本部署需要换成 Redis pub/sub 或任务队列控制面；当前 Compose 单应用副本不受影响。

## 管理中心与权限

具有 `audit.read` 权限的用户才能看到入口和调用管理 API。当前角色中 `admin`、`superadmin` 具备该权限。

- `GET /api/admin/overview`：请求量、成功率、平均耗时、Token、低置信度、取消和反馈统计。
- `GET /api/admin/traces`：查询审计与 Trace 摘要，包括节点时间线，但不保存 Prompt、结果行或凭证。
- `GET /api/audit/resources`：数据源创建、编辑、健康检查、授权等资源操作。
- `GET /api/admin/feedback`：用户反馈列表。
- `GET /api/admin/evaluation`：基于已反馈请求的在线弱标签评测。

反馈仍是弱标签，不能未经复核直接作为 SFT 正样本或 GRPO Reward。完整 BIRD 评测仍应使用离线脚本和隔离的数据集。

## 验证

```bash
python3 -m unittest discover -s tests -v
```

重点自动化覆盖：SQLite EXPLAIN、长查询取消、取消请求所有权、低置信度解释、审计迁移与管理指标、SSE 节点标签。Web 端验证步骤：

1. 以 `superadmin/admin` 登录并运行查询，执行流程应出现“正在检查 SQL 执行成本”。
2. 结果轨迹应显示“置信度”和“成本检查”；有风险时答案上方显示黄色提示。
3. 查询运行时点击“取消请求”，最终状态应为 `cancelled`。
4. 点击顶部“管理中心”，切换查询与 Trace、资源审计、反馈和在线评测四个页签。

## 已知限制

- SQLite 的成本级别是基于执行计划操作的启发式判断，不是优化器数值成本。
- MySQL 当前只能在执行前后检查取消标记，执行中的硬取消依赖后续引入连接 ID 与 `KILL QUERY` 控制连接。
- 外部模型 SDK 调用不能被 Python `threading.Event` 强制终止；取消会阻止后续节点和数据库执行。
- 管理中心保存 Trace 摘要与真实 SSE 时间线，不是完整 OpenTelemetry Span 浏览器；完整 Span 仍由配置的 OTLP/Langfuse 后端承载。

二进制直接运行时，相对的审计、身份、checkpoint 和 workbench 路径以当前工作目录（或 `VERISQL_RUNTIME_DIR`）为基准，不会写入 PyInstaller 的临时解包目录。Docker 发布包继续使用 `/app/runtime` 持久化卷。
