# 数据源级 Schema Catalog 与纠错检索

## 目标

本模块将原先只覆盖演示库的全局 `schema_metadata.json` 降级为种子信息，为每个 SQLite 数据源自动构建独立、持久化的语义目录。它解决上传数据库只有物理表字段、弱匹配时固定 Top-K 漏表，以及失败重试继续使用不完整 Schema 的问题。

## 接入流程

```text
SQLite 上传 / 首次查询 / 手动同步
  -> 读取表、视图、字段、类型、主键和外键
  -> 计算 Schema Hash
  -> 合并演示种子元数据（若表名相同）
  -> 从 snake_case / camelCase 标识符生成基础业务别名
  -> 根据 *_id、主键、表名和类型推断候选关系
  -> 按 data_source_id 写入 schema_catalog.sqlite
  -> SQLiteSchemaRetriever 使用该 Catalog 检索
```

Schema Hash 未变化时复用已有 Catalog；删除数据源时同步删除 Catalog。不同数据源即使存在同名表，也不会共用目录。

管理员可以从数据源卡片点击“语义”进入 Web 目录编辑器：同步物理结构、调用当前模型分批生成描述，或者人工修改表字段含义。AI 增强仅发送名称、类型、主键和关系，不发送真实字段值；每批最多八张表，返回内容必须经过原始表字段白名单校验后才能写入 Catalog。

## 检索变化

- 中文检索使用二元词组，避免单个汉字（例如“配”）导致虚假高分。
- 强匹配继续保持精简 Schema。
- 最佳分数较低时自动扩大到最多六张候选表，而非固定 Top-2。
- 得分接近时适度扩容，降低正确表处于截断边界后的概率。
- 置信度不低于 `0.75` 的推断关系可以参与外键路径补全；响应检索事件仍记录最终表、字段和路径。

## 错误驱动纠错检索

LangGraph 新增 `corrective_retrieve` 节点。遇到 `unknown_table`、`unknown_column`、`ambiguous_column`、Schema 或部分运行时错误时，流程变为：

```text
失败 -> reflect -> corrective_retrieve
     -> 问题 + 失败 SQL + 错误信息扩大检索
     -> 增量追加 Schema Context
     -> generate_sql
```

它与 ETC 无关；当前 `LLM_ENABLE_ETC=false` 时仍然生效。ETC 未来只负责生成阶段的动态触发。

## API

- `GET /api/data-sources/{source_id}/schema-catalog`：读取当前用户有权查看的数据源目录。
- `POST /api/data-sources/{source_id}/schema-catalog/sync`：拥有管理权限的用户重新同步 SQLite Catalog。
- `POST /api/data-sources/{source_id}/schema-catalog/enrich`：用当前模型生成全部或指定表的中文语义描述。
- `PATCH /api/data-sources/{source_id}/schema-catalog/descriptions`：保存人工修正的表字段描述。

同步响应只返回 Hash、表数量、推断关系数量和时间，不返回数据库凭证。

## 配置

```env
SCHEMA_CATALOG_DB_PATH=schema_catalog.sqlite
```

Docker 中应配置为 `/app/runtime/schema_catalog.sqlite`，由 `app_runtime` volume 持久化。

## 安全边界

- 自动 Profiling 目前只读取 Schema，不抽取任意真实业务值。
- Value Grounding 仍必须经过 `value_grounding_columns` 白名单。
- 推断关系只是检索提示，最终 SQL 仍必须通过 SQLGlot、EXPLAIN 和只读执行。
- Catalog 不保存数据库密码、DSN 密码或查询结果行。

## 验证

```bash
python3 -m unittest tests.test_schema_catalog -v
python3 -m unittest discover -s tests -v
```

可上传包含 `system_configs`、`config_key` 等字段的新 SQLite，然后查询“系统配置一共有多少项”。在 Catalog API 中应看到自动生成的“系统、配置、参数、设置”等别名。

## 当前限制与下一步

- 当前已支持 LLM 离线描述生成，但仍未接入独立 Embedding 模型；模型描述是推断信息，重要业务口径需要人工确认。
- Catalog 自动同步当前仅支持 SQLite；MySQL/PostgreSQL 仍使用完整 Schema。
- Web 已提供描述修正入口，但尚未实现版本对比、审批流和批量导入导出。
- Query Plan、多候选生成和执行后语义检查仍是后续阶段，不能把本模块描述为完整准确率优化方案。

## 真实端到端验收

在上传的 `jydlicensemgr.db`（42 张表、无正式外键）中，仅选择 `sys_apis` 的结构调用当前 DeepSeek 生成描述，未发送字段值。生成描述随后进入检索，对“系统接口一共有多少个？”的真实查询首次召回 `sys_apis`，模型生成：

```sql
SELECT COUNT(*) FROM `sys_apis` WHERE `deleted_at` IS NULL;
```

SQL 经 AST、成本检查和只读执行后返回 `183`，重试次数为 0。该案例证明链路工作正常，不代表整体准确率已经经过数据集评测。
