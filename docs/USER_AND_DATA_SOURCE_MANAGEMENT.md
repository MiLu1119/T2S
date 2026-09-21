# 用户系统与数据源管理

## 1. 功能边界

该模块把原先写死的演示数据库升级为用户名下的数据源。当前 Web 支持登录、管理员创建用户、HttpOnly 会话、用户隔离的数据源列表、连接测试、创建和删除，以及查询时选择数据源。

在线查询支持：

- SQLite：用户从浏览器上传 `.sqlite`、`.sqlite3` 或 `.db` 文件，服务端隔离保存；
- MySQL：独立只读账号、只读事务、超时和返回行限制；
- PostgreSQL：后端适配和 API 已保留，当前 Web 创建表单先开放 SQLite/MySQL。

管理员仍可通过 API 注册 `SQLITE_ALLOWED_ROOTS` 白名单目录中的既有文件；普通 Web 流程使用上传方式，不向用户暴露服务器路径。

### SQLite 上传安全流程

```text
multipart upload
  -> 扩展名检查
  -> 分块写入临时文件并限制总大小
  -> SQLite 3 文件头检查
  -> PRAGMA quick_check
  -> 至少存在一张业务表/视图
  -> 移入 user_id 隔离目录
  -> 注册数据源
```

文件使用随机 UUID 保存，不采用用户文件名作为服务器路径。原始文件名仅作为展示元数据。失败时临时文件会删除，公开 API 不返回托管文件的服务器路径。用户删除上传型数据源时，服务仅在确认目标仍位于托管上传根目录后删除对应副本；通过管理员白名单注册的既有 SQLite 文件不会被删除。

## 2. 身份与会话

`Identity.py` 使用独立 SQLite 元数据库保存用户、会话和数据源元数据：

- 密码通过 scrypt 加盐哈希，不保存明文；
- 登录成功后签发随机、有有效期、服务端可撤销的会话；
- 浏览器 Cookie 使用 `HttpOnly` 和 `SameSite=Strict`；
- 首次启动从 `WEB_USERNAME/WEB_PASSWORD` 创建管理员，之后验证走用户表；
- `POST /api/users` 只有 admin 可以创建用户。

Basic Auth 暂时保留为 API 向后兼容入口。正式 HTTPS 部署应设置独立的 `SESSION_SECRET`、`DATA_SOURCE_MASTER_KEY`，并把 Cookie 的 Secure 策略随部署配置打开。

## 3. 数据源凭证

MySQL 密码使用 Fernet 对称加密，密钥由 `DATA_SOURCE_MASTER_KEY` 派生。公开 API 永远只返回 `has_secret`，不返回密文或明文密码。

身份库和业务数据库相互隔离。删除数据源会删除加密凭证并关闭当前进程缓存的运行时连接。

主密钥支持通过 `DATA_SOURCE_OLD_MASTER_KEYS` 执行启动时重加密轮换，步骤见 `DATA_SOURCE_LIFECYCLE_AND_CREDENTIALS.md`。生产环境仍应通过密钥管理系统分别保存身份库备份和密钥。

## 4. 查询隔离

查询请求增加 `data_source_id`。服务端校验该 ID 属于当前用户，再构建数据库适配器和 LangGraph：

```text
authenticated user
  -> owned data_source_id
  -> encrypted credential decrypt
  -> read-only database adapter
  -> schema load / AST validation
  -> LangGraph query
```

Checkpoint key 使用 `username:data_source_id:thread_id`，因此同一用户切换数据库时不会复用其他数据库的历史 SQL。

## 5. MySQL 安全执行

`MySQLDatabase` 使用 PyMySQL，限制并发连接数量。连接后：

1. 设置 session 为只读事务；
2. 设置 `MAX_EXECUTION_TIME`；
3. 显式启动 `READ ONLY` 事务；
4. 最多读取 `DB_MAX_ROWS + 1` 行；
5. 无论成功或失败都关闭连接，成功查询回滚只读事务。

SQLGlot 使用 `mysql` 方言并根据 `information_schema.columns` 校验真实表字段。数据库账号仍必须由管理员配置为真正的只读最小权限账号，应用层只读不能替代数据库权限。

## 6. API

- `POST /api/auth/login`、`POST /api/auth/logout`、`GET /api/auth/me`；
- `POST /api/users`；
- `GET /api/data-sources`；
- `POST /api/data-sources/test`；
- `POST /api/data-sources`；
- `POST /api/data-sources/upload-sqlite`（multipart：`name` + `file`）；
- `DELETE /api/data-sources/{id}`；
- `POST /api/query` 和 `/api/query/stream` 接收 `data_source_id`。

## 7. 配置

```dotenv
IDENTITY_DB_PATH=identity.sqlite
SESSION_SECRET=<random-secret>
SESSION_TTL_HOURS=12
SESSION_COOKIE_SECURE=true
DATA_SOURCE_MASTER_KEY=<independent-random-key>
SQLITE_ALLOWED_ROOTS=/srv/verisql/databases,/srv/verisql/demo
SQLITE_UPLOAD_DIR=data/uploads
SQLITE_UPLOAD_MAX_MB=100
```

为兼容现有部署，未设置两个 secret 时会回退到 `WEB_PASSWORD`。该降级只适合内部演示，不推荐生产使用。

## 8. 验证

```bash
python -m unittest tests.test_identity_sources -v
python -m unittest discover -s tests -v
python Test_reward_engine.py
```

MySQL 真实验证需要一个可访问的 MySQL 实例和只读账号。没有 MySQL 服务时，自动化测试只验证配置、凭证和隔离边界，不能宣称真实 MySQL 已完成端到端连接。

## 9. 已知限制

- 当前身份存储适合单服务或共享文件卷部署，大规模多实例应迁移到 PostgreSQL/企业 IdP；
- 尚未提供忘记密码、MFA、OIDC/LDAP、用户禁用管理 UI；
- 当前 Web 表单已经开放 PostgreSQL 创建入口，密码必须与无密码 DSN 分开提交；
- 数据源运行时缓存是单进程内缓存，尚无跨实例失效广播；
- 已支持数据源编辑、手动健康检查和同步时间记录，尚未实现后台定时 Schema 同步；
- 已支持用户级数据源共享，尚未实现组织租户及表/列/行级 RBAC。

## 10. 为什么 MySQL 不采用文件上传

SQLite 是单文件嵌入式数据库，上传文件后应用可以只读打开。MySQL 是独立运行的数据库服务，其数据目录包含 InnoDB 系统表空间、redo/undo 日志和版本相关状态，不能安全地把某个 `.ibd` 或 MySQL 数据目录当成普通文件上传并查询。

因此当前 MySQL 流程是连接已有实例：主机、端口、库名、只读用户、密码和 TLS。若未来需要支持 `.sql` dump，应作为独立的“托管导入”服务实现：上传对象存储、病毒与 SQL 检查、异步任务、临时容器/独立数据库、资源配额、导入日志、失败回滚和生命周期清理。它不能直接导入用户提供的 SQL 到本项目正在使用的系统数据库。
