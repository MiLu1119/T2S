# 独立 Embedding Schema 检索

## 选型结论

DeepSeek 官方 API 当前提供生成模型及 Chat/Responses/FIM 接口，没有 Embeddings endpoint，因此 `deepseek-flash` 继续负责 SQL 与 Catalog 描述，不被伪装成向量模型。

项目新增本地 CPU Embedding Provider：

```text
fastembed 0.7.x + ONNX Runtime
sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2
384 dimensions / approximately 220 MB
```

该模型支持多语言语义相似度，适合中文问题与英文 Schema 描述匹配。模型在服务启动时加载，查询期间不会将问题或 Schema 发送给新的第三方 API。

## 在线流程

```text
用户问题
  ├── BM25 词法分数
  ├── 字符 3-gram 分数
  ├── 多语言 Embedding 余弦相似度
  └── 精确表名奖励
          ↓
      分数融合
          ↓
  动态 Top-K + 关系图补全
```

Embedding 向量使用 L2 归一化，余弦相似度通过点积计算。表文档向量在数据源运行时内按文档 Hash 缓存；问题向量每次查询生成。Catalog 描述变化后运行时失效并重新建立向量缓存。

默认融合权重：Embedding `0.45`，其余权重由 BM25 与字符相似度分享。精确表名仍有独立奖励，避免向量语义覆盖用户明确指定的物理表名。

## 配置

```env
SCHEMA_EMBEDDING_ENABLED=true
SCHEMA_EMBEDDING_MODEL=sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2
SCHEMA_EMBEDDING_CACHE_DIR=models/fastembed
SCHEMA_EMBEDDING_WEIGHT=0.45
```

Docker 发布包自带 ONNX 模型，挂载到 `/app/models`，部署时不需要目标服务器访问 Hugging Face。关闭能力后自动退回 BM25 + 字符检索。

## 项目接入点

- `Embedding.py`：FastEmbed Provider、批量编码和归一化。
- `Retrieval.py`：文档向量缓存、问题编码和混合分数融合。
- `Config.py`：Provider 配置。
- `Web_app.py`：进程级模型复用，并传入每个数据源检索器。
- `scripts/build_binary_release.sh`：打包 ONNX 依赖和离线模型。

## 验证

```bash
python3 -m unittest tests.test_schema_catalog -v
python3 -m unittest discover -s tests -v
```

自动化测试使用确定性 Fake Embedder，验证在关键词无重合时语义向量仍能召回目标表；真实模型另做本地 CPU 冒烟测试，输出 384 维向量。

## 边界

- Embedding 提升的是 Schema 召回，不保证 SQL 推理一定正确。
- 企业独有缩写仍需要 AI Catalog 描述或人工语义层。
- 当前是进程内精确向量比较，适合几十到数千张表；更大规模再引入专用向量数据库或 ANN 索引。
- 模型首次加载增加内存和启动时间，查询向量编码也增加少量 CPU 延迟。
- 尚未使用固定问题集校准 `0.45` 权重，不能宣称具体准确率提升。

