# 数据源生命周期与凭证加密

## 目标与边界

本模块把数据源从“添加/删除”提升为可持续维护的资源：支持连接验证、编辑、独立更新凭证、启停、健康状态、运行时连接失效、共享授权与管理操作审计。适用于 SQLite 上传、MySQL 和 PostgreSQL 只读连接。

本阶段不包含定时巡检调度、多副本缓存失效广播、客户端证书托管、云 KMS 或正在执行查询的优雅排空；这些属于后续基础设施与安全加固。

## 数据模型

`identity.sqlite.data_sources` 在启动时幂等增加：

- `description`
- `health_status`：`unknown/healthy/unhealthy`
- `last_checked_at`、`last_success_at`
- `last_error`、`last_latency_ms`
- `table_count`、`schema_synced_at`
- 既有 `enabled`、`updated_at`

旧数据库无需手工建表或删除。公开响应包含状态和脱敏配置，不包含密文、明文密码或托管 SQLite 的真实服务器路径。

## 生命周期

```text
创建/上传
  -> 连接与 Schema 验证
  -> 凭证加密、健康状态落库
  -> 可查询

编辑连接/更新密码
  -> 使用候选配置先连接验证
  -> 验证成功后原子更新元数据
  -> 关闭该数据源全部进程内运行时
  -> 下一次查询重建连接池和 Agent

停用
  -> enabled=false
  -> 清理运行时缓存
  -> 查询入口返回 HTTP 409
```

仅修改名称和说明不要求重新输入密码。MySQL/PostgreSQL 编辑页面的空密码表示保留原凭证；新密码永远不会由 API 回显。

## PostgreSQL 密码治理

PostgreSQL DSN 必须使用无密码形式，例如：

```text
postgresql://reader@db.internal:5432/business
```

密码放在独立的 `password` 字段，使用 Fernet 加密后写入 `secret_ciphertext`。运行时只在内存中重建连接 DSN。历史记录若包含 `postgresql://user:password@host/db`，启动迁移会提取密码、清理 `config_json` 并加密保存。

## 主密钥轮换

正常配置：

```dotenv
DATA_SOURCE_MASTER_KEY=<current-key>
DATA_SOURCE_OLD_MASTER_KEYS=
```

轮换步骤：

1. 生成新密钥，把它写入 `DATA_SOURCE_MASTER_KEY`。
2. 临时把旧密钥写入 `DATA_SOURCE_OLD_MASTER_KEYS`；多个旧密钥用逗号分隔。
3. 启动应用。启动事务会尝试旧密钥并把全部历史凭证重新加密到新密钥。
4. 验证所有数据源连接。
5. 删除 `DATA_SOURCE_OLD_MASTER_KEYS` 后再次启动验证。

没有正确旧密钥时应用会明确启动失败，不会静默覆盖或丢弃凭证。身份库备份与主密钥必须分开保存。

## API

| 方法与路径 | 权限 | 行为 |
|---|---|---|
| `GET /api/data-sources` | `data_sources.read` | 可访问数据源和状态 |
| `POST /api/data-sources` | `data_sources.create` | 创建 MySQL/PostgreSQL |
| `POST /api/data-sources/upload-sqlite` | `data_sources.create` | 上传并校验 SQLite |
| `PATCH /api/data-sources/{id}` | `data_sources.update` | 编辑配置或独立更新密码 |
| `POST /api/data-sources/{id}/health` | `data_sources.update` | 实时连接、加载 Schema 并记录状态 |
| `PATCH /api/data-sources/{id}/enabled` | `data_sources.update` | 启用或停用 |
| `DELETE /api/data-sources/{id}` | `data_sources.delete` | 所有者删除资源和托管文件 |
| `GET /api/audit/resources` | `audit.read` | 管理操作审计 |

连接失败响应仅返回异常类型，例如 `连接失败（OperationalError）`。服务端日志和审计不得记录密码、完整认证 DSN 或 Authorization Header。

## Web 使用

数据源区域展示启用状态、健康状态、表数量和延迟。所有者可以：

- 添加 SQLite、MySQL 或 PostgreSQL；
- 编辑名称、说明和连接参数；
- 单独更新数据库密码；
- 手动检查连接；
- 启用/停用；
- 管理共享授权；
- 删除。

## 审计

独立审计库的 `resource_audit` 记录 `create/update/delete/connection_test/health_check/enable/disable/grant/revoke`。记录操作者、资源 ID、结果和脱敏详情，不保存凭证与完整 DSN。

## 验证

```bash
.venv/bin/python -m unittest tests.test_identity_sources tests.test_audit -v
.venv/bin/python -m unittest discover -s tests -v
node --check web/app.js
```

端到端验收应覆盖：上传 SQLite、编辑、健康检查、停用、停用后查询返回 409，以及管理操作出现在 `/api/audit/resources`。

MySQL/PostgreSQL 的自动测试覆盖配置拆分、加密和适配器边界。只有使用真实外部实例完成连接时，才可以声明对应环境的真实网络集成验证通过。

## 已知限制

- 健康检查目前由用户手动触发，没有后台定时任务。
- 运行时失效为单进程内存机制，多副本部署需要 Redis Pub/Sub 或消息总线广播。
- 更新或停用会立即关闭缓存连接；当前尚未实现对正在运行查询的引用计数与优雅排空。
- 未托管 PostgreSQL/MySQL 客户端证书，TLS 复杂参数需在后续证书管理阶段实现。
- SQLite 已限制文件大小，但还没有按用户累计存储配额和版本回滚。

