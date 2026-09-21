# 用户系统与 RBAC

## 目标与边界

本模块为 VeriSQL Web 工作台提供本地用户认证、权限检查、用户生命周期和数据源级授权。当前是单工作空间模型，适合单个团队部署；不包含多租户计费、OIDC/LDAP/SSO、审批流或自定义角色编辑器。

## 架构

认证信息保存在独立的 `identity.sqlite` 中，不写入用户业务数据库。密码使用 scrypt 加盐哈希；会话 Cookie 只保存带 HMAC 签名的随机令牌，服务端只保存令牌哈希。外部数据库密码通过 `DATA_SOURCE_MASTER_KEY` 派生的 Fernet 密钥加密。

```text
users -> user_roles -> roles -> role_permissions
  |                                |
  +-- owns data_sources            +-- API permission check
              |
              +-- data_source_grants -> user (read/query/manage)
```

应用启动时会幂等创建 RBAC 表。旧版 `admin` 启动账号迁移为 `superadmin`，其他旧版 `user` 迁移为 `analyst`；旧字段保留用于兼容，不作为在线鉴权依据。

## 内置角色

| 角色 | 能力 |
|---|---|
| `superadmin` | 全部系统权限，启动账号自动获得 |
| `admin` | 用户管理、数据源管理、查询、审计查看；不能管理超级管理员 |
| `developer` | 创建和管理自己的数据源、共享数据源、运行查询 |
| `analyst` | 查询已授权数据源、管理自己的会话、提交反馈 |
| `viewer` | 仅查看可见数据源元数据，不能运行查询 |

权限在 `Identity.py` 的 `ROLE_PERMISSIONS` 中定义，通过 `Web_app.py::require_permission` 统一检查。新增受保护接口时必须先定义权限，并在业务逻辑开始前调用统一检查。

## 数据源授权

数据源所有者固定拥有 `manage`。所有者可以向其他用户授予：

- `read`：可在数据源列表查看元数据，不能查询。
- `query`：可查询；服务端可以解密连接凭证，但凭证不会返回浏览器。
- `manage`：可使用管理级访问；当前仍只有所有者能够再次授权或删除物理数据源。

## API

| 方法与路径 | 权限 | 用途 |
|---|---|---|
| `GET /api/auth/me` | 已登录 | 当前角色与展开后的权限 |
| `GET /api/roles` | `roles.read` | 查看内置角色和权限 |
| `GET /api/users` | `users.read` | 用户列表 |
| `POST /api/users` | `users.create` | 创建用户 |
| `PATCH /api/users/{id}` | `users.update` | 修改角色或启停 |
| `POST /api/users/{id}/reset-password` | `users.update` | 重置密码并撤销旧会话 |
| `DELETE /api/users/{id}` | `users.delete` | 删除无资源依赖的用户 |
| `GET/PUT /api/data-sources/{id}/grants` | `data_sources.share` | 查看或写入授权 |
| `DELETE /api/data-sources/{id}/grants/{user_id}` | `data_sources.share` | 撤销授权 |

用户被停用、重置密码后，已有服务端会话立即删除。用户不能停用、改角色或删除自己；超级管理员不能通过 API 删除，普通管理员不能创建或管理超级管理员。

## Web 使用

超级管理员或管理员登录后，顶部显示“用户与权限”。页面支持创建、启停、重置密码和删除用户。数据源所有者在数据源区域看到“共享与授权”，可以选择用户和访问级别。按钮是否显示仅用于交互提示，真正的安全边界始终是后端 403 和资源查询条件。

## 验证

```bash
.venv/bin/python -m unittest discover -s tests -v
```

手工验证：

1. 用 `superadmin` 创建 `analyst` 和 `viewer`。
2. 未授权时，两者的数据源列表不包含管理员私有数据源。
3. 授予 analyst `query` 后可以查询；授予 viewer `read` 后仍无法调用查询接口。
4. analyst 访问 `/api/users` 和 `/api/audit/queries` 应返回 403。
5. 停用 analyst 后，其旧 Cookie 和 Basic Auth 都不能继续访问。
6. 重启服务，确认旧用户、角色与授权关系仍在。

自动化覆盖密码和会话、权限矩阵、迁移幂等性、数据源隔离、授权等级、停用撤销会话和加密凭证。真实 HTTP 验证确认超级管理员接口返回 200、analyst 访问用户与审计接口返回 403。

## 安全与已知限制

- 生产环境必须使用高熵 `SESSION_SECRET` 与 `DATA_SOURCE_MASTER_KEY`，不得使用示例值。
- 启用 HTTPS 后设置 `SESSION_COOKIE_SECURE=true`。
- 当前没有 CSRF Token；SameSite Cookie 有基础防护，但生产化仍应补充 Origin/CSRF 校验。
- 当前没有登录失败锁定、MFA、OIDC/LDAP、密钥轮换和组织级租户隔离。
- 当前角色是代码内置并启动时同步，不支持 Web 自定义权限。
- 删除仍拥有数据源的用户会返回 409，必须先删除资源；资源转移尚未实现。

