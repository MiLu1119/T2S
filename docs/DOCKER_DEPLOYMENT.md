# Docker Compose 一键部署

## 1. 容器拓扑

```text
浏览器 :8080
    ↓
verisql app
    ├── /app/runtime       用户、审计元数据
    ├── /app/data/uploads  用户上传的 SQLite
    └── Redis              LangGraph checkpoint
```

Compose 启动两个服务：

- `app`：FastAPI、LangGraph、Text-to-SQL Web；
- `redis`：普通 Redis 7，开启密码和 AOF，用于会话 checkpoint。

业务 MySQL/PostgreSQL 不包含在 Compose 中。用户通过 Web 使用只读账号连接已有业务数据库。

## 2. 新服务器部署

安装 Docker Engine、Buildx 与 Compose plugin，将项目复制到服务器。当前用户必须能访问 Docker daemon。不要从其他环境复制包含真实密钥的 `.env`。

```bash
cd VeriSQL-Agent
cp .env.docker.example .env
```

至少修改：

```dotenv
WEB_PASSWORD=<登录密码>
SESSION_SECRET=<32位以上随机值>
DATA_SOURCE_MASTER_KEY=<独立随机值>
REDIS_PASSWORD=<仅含URL安全字符的随机值>
LLM_MODEL=<模型名>
LLM_BASE_URL=<OpenAI兼容API地址>
LLM_API_KEY=<API密钥>
```

建议生成三个独立随机值：

```bash
openssl rand -hex 32
openssl rand -hex 32
openssl rand -hex 32
```

启动：

```bash
chmod +x deploy.sh
./deploy.sh
```

脚本兼容 `docker compose` plugin 和 `docker-compose` 独立命令，会拒绝包含 `CHANGE_ME` 或 `YOUR_*` 占位符的配置，执行 Compose 配置校验、构建镜像、等待健康检查并显示服务状态。

直接使用 Compose 也可以：

```bash
docker compose up -d --build --wait
docker compose ps
```

访问 `http://服务器IP:8080`。如修改了 `WEB_PORT`，使用对应端口。

## 3. 持久化

以下 named volume 在重建容器后保留：

- `verisql_app_runtime`：`identity.sqlite`、`audit.sqlite`；
- `verisql_sqlite_uploads`：用户上传的 SQLite 文件；
- `verisql_redis_data`：Redis AOF checkpoint。

`docker compose down` 不删除数据。以下命令会永久删除 volume，禁止作为普通重启命令使用：

```bash
docker compose down -v
```

## 4. 运维命令

```bash
docker compose ps
docker compose logs -f --tail=200 app
docker compose restart app
docker compose up -d --build --wait
```

健康检查：

```bash
curl http://127.0.0.1:8080/api/health
```

## 5. 备份与迁移

停止写入后备份三个 volume。最简单的迁移方式是在旧服务器导出、在新服务器恢复同名 volume。必须同时安全迁移原来的 `DATA_SOURCE_MASTER_KEY`，否则已保存的 MySQL 密码无法解密。

外部业务 MySQL/PostgreSQL 数据不在这些 volume 中，需要由其自身备份系统负责。

## 6. HTTPS

Compose 默认直接暴露 HTTP，适合内网验证。对外服务应在前面部署 Nginx、Caddy 或负载均衡器终止 TLS，并设置：

```dotenv
SESSION_COOKIE_SECURE=true
```

反向代理需要关闭 SSE 响应缓冲并提高读取超时。如需信任代理传入的客户端地址，应在 Compose 中显式配置 Uvicorn 的可信代理范围，不要接受任意来源的 forwarded headers。

## 7. 安全说明

- `.env` 不进入镜像；
- Redis 不映射宿主机端口；
- 上传文件和运行元数据使用独立 volume；
- 容器内应用以非 root UID 10001 运行；
- 日志启用大小和文件数轮转；
- MySQL/PostgreSQL 必须使用只读最小权限账号；
- 当前 Compose 是单机部署，不等同于高可用集群。

## 8. 已知限制

- 当前身份元数据库是 SQLite，适合单实例；多副本部署应迁移到中心数据库；
- named volume 备份尚未封装为自动任务；
- 没有自带 Nginx/TLS 证书；
- 没有集成 Kubernetes、集中日志或自动扩缩容；
- 本地 vLLM/GPU 不在该 Compose 中，使用本地模型时需额外部署推理服务并配置可达地址。

## 9. 无源码二进制发布包

发布包 `verisql-agent-linux-amd64.tar.gz` 包含一个 PyInstaller 单文件 ELF、Compose、环境模板与部署脚本，不包含项目 `.py` 文件。目标服务器无需安装 Python、pip 或编译依赖，但仍需 Docker，并需联网拉取 `debian:bookworm-slim` 与 `redis:7.4-alpine`。

```bash
tar -xzf verisql-agent-linux-amd64.tar.gz
cd verisql-agent-linux-amd64
sha256sum -c SHA256SUMS
cp .env.example .env
# 修改配置
./deploy.sh
```

当前 ELF 构建目标是 `linux/amd64`，依赖 glibc，Compose 固定使用兼容的 Debian Bookworm 运行层。它不是可在 ARM64、Windows 或 Alpine/musl 上直接运行的通用二进制。单文件程序冷启动时需要先解包内嵌运行时，因此 Compose 健康检查保留 180 秒启动宽限。

### 当前构建验证记录（2026-09-20）

- ELF 被识别为 stripped `x86-64` Linux executable；
- 二进制约 58 MB，压缩发布包约 57 MB；
- 独立二进制以 Mock 模型、独立临时运行目录启动；
- `/api/health`、首页、`app.js`、登录和数据源列表均真实返回 HTTP 200；
- 发布包内 `.py` 文件数量为 0；
- 包内 `SHA256SUMS` 校验和 Compose 配置解析通过；
- 当前构建机无法访问 Docker daemon，因此没有在本机执行最终 Compose 容器启动；目标机仍需从 Docker Hub 拉取 Debian 与 Redis 运行镜像。
