# 会话历史、结果分页、导出与图表

## 目标与边界

本模块把单轮演示页面升级为可恢复的查询工作台：用户可以切换历史会话、继续多轮查询、回看每次 SQL 和结果，并对结果分页、导出 CSV 或在浏览器生成图表。

结果分页针对已经安全执行并受 `DB_MAX_ROWS` 限制的结果集，不会为每次翻页重新执行模型生成的 SQL。这保证翻页结果一致并避免重复模型费用，但不是面向百万行的数据库游标分页。

## 存储职责

```text
LangGraph Checkpoint
  保存可继续对话的安全状态摘要，不保存结果行

AuditStore
  保存合规审计、耗时、状态和反馈，不保存结果行

WorkbenchStore
  保存用户会话索引、查询记录
  短期保存分页/导出所需的结果行
```

`WorkbenchStore` 使用独立 `workbench.sqlite`，按 `user_id + data_source_id + thread_id` 隔离。结果缓存默认 1440 分钟，到期后分页、导出和图表不可用，但会话和 SQL 历史仍可见。

## 会话工作台

- 第一次查询自动创建会话，首个问题生成默认标题；
- “新会话”只清空当前选择，不再删除旧会话；
- 会话按最近更新时间排序；
- 支持恢复、重命名和明确删除；
- 会话内展示问题、SQL、状态、行数和耗时；
- 删除会话同时删除对应 LangGraph checkpoint、查询记录和短期结果缓存；
- 所有读取、改名和删除都按认证用户隔离。

## 结果分页

`GET /api/query-results/{request_id}` 接受 `page` 和 `page_size`，每页最多 200 行，并返回 `total_rows`、`total_pages` 与 `truncated`。同步查询和 SSE 查询共用同一保存逻辑。页面默认每页 50 行，可切换 20/50/100/200。

## CSV 导出

`GET /api/query-results/{request_id}/export.csv` 导出当前缓存结果：

- UTF-8 BOM，兼容中文 Excel；
- 只允许查询所属用户下载；
- 文件名使用服务端 `request_id`；
- 对以 `= + - @` 开头的文本添加单引号，降低 CSV 公式注入风险；
- 不重新执行数据库查询。

## 图表

图表在浏览器本地渲染，不调用第三方图表服务，支持柱状图、折线图和饼图。系统自动识别候选数值列，用户可选择分类列和数值列，使用当前页最多 200 行。

## API

| 接口 | 作用 |
|---|---|
| `GET /api/workbench/sessions` | 当前用户的会话列表 |
| `GET /api/workbench/sessions/{thread_id}/queries` | 会话查询记录 |
| `PATCH /api/workbench/sessions/{thread_id}` | 重命名会话 |
| `DELETE /api/sessions/{thread_id}` | 删除会话、checkpoint 和结果 |
| `GET /api/query-results/{request_id}` | 服务端分页 |
| `GET /api/query-results/{request_id}/export.csv` | CSV 导出 |

接口要求 `sessions.manage` 或 `query.execute` 权限。`request_id` 不是授权凭据，即使猜中其他用户的 ID 也无法访问结果。

## 配置

```dotenv
WORKBENCH_DB_PATH=workbench.sqlite
WORKBENCH_RESULT_TTL_MINUTES=1440
```

Docker 部署使用 `/app/runtime/workbench.sqlite`，由已有 `app_runtime` volume 持久化。

## 验证

```bash
.venv/bin/python -m unittest tests.test_workbench -v
.venv/bin/python -m unittest discover -s tests -v
node --check web/app.js
```

HTTP 验收覆盖：真实查询后生成会话、读取查询历史、5 行结果按 2 行分页得到 3 页，以及 CSV 表头和数据导出。

## 安全与已知限制

- 查询结果可能包含业务敏感数据，缓存文件必须位于受保护的持久化卷；默认 24 小时后清除结果行。
- 会话问题、答案和 SQL 属于产品历史，不等同于合规审计；部署方应结合数据分级制定删除周期。
- 当前缓存实现适合单机或共享文件卷；多副本应迁移到 PostgreSQL/Redis/对象存储组合。
- 当前分页上限是已执行结果的 `DB_MAX_ROWS`，大数据集需要受控的 SQL 重写或数据库游标方案。
- 图表是探索性可视化，不提供 BI 指标计算、仪表盘发布和图片导出。
- 当前导出 CSV；XLSX、JSON 和异步大文件导出尚未实现。

