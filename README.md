# VeriSQL Agent

基于 LangGraph 的 Stateful Text-to-SQL Agent，支持混合 Schema RAG、SQL 安全校验、只读执行、错误驱动重试、BIRD 评测以及 LoRA SFT/GRPO 训练。

## 核心能力

- LangGraph 状态机：Schema 检索、SQL 生成、AST 校验、成本检查、只读执行、失败反思与自动重写。
- Schema RAG：融合 BM25、字符 N-Gram 与多语言 Embedding，并支持外键路径补全和受控 Value Grounding。
- SQL 安全：使用 SQLGlot 做 AST、表字段和只读语句校验，数据库只读事务作为最终防线。
- 数据源：支持上传 SQLite 文件，并通过只读网络账号连接 MySQL、PostgreSQL。
- Web 工作台：提供多轮会话、SSE 流式进度、结果分页、CSV 导出、图表、反馈、审计和 Trace。
- 工程能力：RBAC、数据源凭证加密、Redis/LangGraph Checkpoint、OpenTelemetry 和 Docker Compose 部署。
- 模型接入：支持 Mock、本地 vLLM 以及 DeepSeek/OpenAI 等 OpenAI-compatible Chat Completions API。

在线工作流使用 SQLGlot 将模型 SQL 解析为 AST，在执行前拒绝写操作和堆叠语句，并校验 SQLite 中真实存在的表、字段及别名引用；SQLite 只读连接继续作为第二道安全防线。

技术文档：[`docs/SQLGLOT_AST_VALIDATION.md`](docs/SQLGLOT_AST_VALIDATION.md)

PostgreSQL 数据源、连接池与只读事务：[`docs/POSTGRESQL_RUNTIME.md`](docs/POSTGRESQL_RUNTIME.md)

Schema 混合检索、外键路径补全与 Value Grounding：[`docs/SCHEMA_RAG.md`](docs/SCHEMA_RAG.md)

审计日志、请求追踪与用户反馈：[`docs/AUDIT_AND_FEEDBACK.md`](docs/AUDIT_AND_FEEDBACK.md)

LangGraph Checkpoint、SQLite/Redis 会话持久化：[`docs/CHECKPOINT_AND_SESSIONS.md`](docs/CHECKPOINT_AND_SESSIONS.md)

SSE 节点级流式响应：[`docs/SSE_STREAMING.md`](docs/SSE_STREAMING.md)

OpenTelemetry / Langfuse Agent Tracing：[`docs/OPENTELEMETRY_LANGFUSE.md`](docs/OPENTELEMETRY_LANGFUSE.md)

用户登录与 SQLite/MySQL/PostgreSQL 数据源管理：[`docs/USER_AND_DATA_SOURCE_MANAGEMENT.md`](docs/USER_AND_DATA_SOURCE_MANAGEMENT.md)

正式用户生命周期、RBAC 与数据源共享授权：[`docs/USER_RBAC.md`](docs/USER_RBAC.md)

数据源生命周期、健康检查与凭证轮换：[`docs/DATA_SOURCE_LIFECYCLE_AND_CREDENTIALS.md`](docs/DATA_SOURCE_LIFECYCLE_AND_CREDENTIALS.md)

会话历史、结果分页、CSV 导出与图表：[`docs/WORKBENCH_HISTORY_RESULTS.md`](docs/WORKBENCH_HISTORY_RESULTS.md)

低置信度提示、EXPLAIN 成本检查、请求取消与管理中心：[`docs/QUERY_GUARDRAILS_ADMIN_CENTER.md`](docs/QUERY_GUARDRAILS_ADMIN_CENTER.md)

企业零售经营 SQLite 测试库：[`docs/ENTERPRISE_RETAIL_DATASET.md`](docs/ENTERPRISE_RETAIL_DATASET.md)

Docker Compose 一键部署：[`docs/DOCKER_DEPLOYMENT.md`](docs/DOCKER_DEPLOYMENT.md)

启用基础 Schema RAG：

```bash
export SCHEMA_CONTEXT_MODE=rag
export SCHEMA_RAG_TOP_TABLES=2
```

Schema RAG 当前用于 SQLite 数据源，采用 BM25、字符 N-Gram 与可选的本地多语言 Embedding 混合召回。真实字段值只有在 `schema_metadata.json` 的 `value_grounding_columns` 白名单中才会进入上下文。MySQL/PostgreSQL 已支持 Schema 加载、只读查询与成本检查，面向它们的语义 Schema RAG 仍待扩展。

## 三分钟启动 Web 演示

要求 Python 3.10+。默认使用 Mock 模型，不需要 GPU、模型文件或 API Key。

```bash
cd VeriSQL-Agent
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn Web_app:app --host 127.0.0.1 --port 8080
```

访问 `http://127.0.0.1:8080`，API 文档位于 `/docs`。

## Docker 一键部署

```bash
cp .env.docker.example .env
# 修改 .env 中的登录密码、三个随机密钥和模型 API 配置
chmod +x deploy.sh
./deploy.sh
```

应用、Redis、用户元数据、审计记录、会话和上传数据库均由 Compose 管理。完整说明见 [`docs/DOCKER_DEPLOYMENT.md`](docs/DOCKER_DEPLOYMENT.md)。

登录后可在左侧选择数据源、上传 SQLite 文件，或添加 MySQL/PostgreSQL 只读连接。查询会话按用户和数据源隔离，数据库密码加密后存储在独立的 identity 数据库中。

查询执行前会运行跨数据库 `EXPLAIN` 成本检查；结果页展示可解释的置信度风险信号。流式查询运行期间可以协作式取消。`admin`/`superadmin` 可从顶部“管理中心”查看审计、Trace 摘要、反馈和基于已反馈样本的在线评测；该在线指标不等同于 BIRD Execution Accuracy。

Web 工作台采用桌面三栏、平板双栏加 Trace 抽屉、移动端单栏加双侧抽屉的响应式布局，支持 SQL 复制、输入快捷键、管理抽屉与 Trace 明细。实现和验收说明见 [`docs/VISUAL_DESIGN_RESPONSIVE.md`](docs/VISUAL_DESIGN_RESPONSIVE.md)。

SQLite 数据源会按 `data_source_id` 自动建立持久化 Schema Catalog，弱匹配时动态扩大召回；表字段类失败会触发错误驱动的补充检索。技术边界和验证方法见 [`docs/SCHEMA_CATALOG_AND_CORRECTIVE_RETRIEVAL.md`](docs/SCHEMA_CATALOG_AND_CORRECTIVE_RETRIEVAL.md)。

Schema 检索可启用本地多语言 Embedding，与 BM25 和字符检索融合；DeepSeek 仍只用于生成。模型选型、离线部署和配置见 [`docs/SCHEMA_EMBEDDING_RETRIEVAL.md`](docs/SCHEMA_EMBEDDING_RETRIEVAL.md)。

## 模型接入

密钥只从服务端环境变量读取，不会返回浏览器。`.env.example` 给出了所有配置项。

### Mock 模式

```bash
export LLM_PROVIDER=mock
uvicorn Web_app:app --host 127.0.0.1 --port 8080
```

### 本地 vLLM

```bash
export LLM_PROVIDER=local
export LLM_BASE_URL=http://127.0.0.1:8000/v1
export LLM_MODEL=qwen2.5-coder-7b
uvicorn Web_app:app --host 127.0.0.1 --port 8080
```

### 外部 API

适用于 OpenAI 及兼容 Chat Completions 协议的服务：

```bash
export LLM_PROVIDER=external
export LLM_BASE_URL=https://api.openai.com/v1
export LLM_MODEL=YOUR_MODEL_NAME
export LLM_API_KEY=YOUR_API_KEY
uvicorn Web_app:app --host 127.0.0.1 --port 8080
```

非 OpenAI-compatible 服务只需在 `Runtime.py` 增加 adapter，无需修改 LangGraph。

## ETC 动态检索

```bash
export LLM_ENABLE_ETC=true
```

接口支持 token `top_logprobs` 时，系统计算截断 entropy 序列，通过一阶和二阶变化检测不确定性上升趋势。触发后根据问题和 SQL 前缀检索表、字段、外键和有限样例值，并加入上下文重新生成 SQL。若 API 不支持 logprobs，客户端会自动降级为普通生成，Agent 仍保留执行错误驱动的重写流程。

当前 OpenAI-compatible 接口是在一次 completion 返回后分析 entropy，再携带检索上下文重新生成。严格的 token 流式中断需要进一步接入 vLLM/Transformers decoding hook。

## 测试

```bash
python3 -m unittest discover -s tests -v
python3 Test_reward_engine.py
```

## 训练环境

Web/推理只需 `requirements.txt`。SFT/GRPO 额外安装：

```bash
pip install -r requirements-training.txt
```

`vllm`、`verl`、CUDA 和 PyTorch 需按目标 GPU 环境匹配安装。部分历史训练脚本仍包含 `/data1/tiantian` 路径，运行训练前需改为当前机器路径。
