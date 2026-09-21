# VeriSQL-Agent Project Architecture

本文档记录当前项目架构、执行流程、核心代码文件和阶段进度。后续如果方法、训练策略、prompt 或评测口径发生变化，应优先更新本文档，避免代码实现和项目叙述脱节。

## 1. Project Goal

VeriSQL-Agent 的目标是构建一个企业级 Stateful Text-to-SQL Agent：

- 业务用户输入自然语言问题。
- Agent 根据数据库 schema、字段说明、取值样例和外部 evidence 生成 SQLite SQL。
- SQL 先经过安全检查，再只读执行。
- 如果失败，LangGraph 反思节点把错误信息带回生成节点重写。
- 如果成功，返回查询结果；多次失败后转人工。
- 训练阶段用 SFT + GRPO 提升 SQL 生成策略模型，训练 reward 与推理时 safety/executor/compare 基础设施共用。

核心设计原则是把两件事拆开：

- LangGraph 负责状态机、重试、自我纠错和转人工。
- SFT/GRPO 只负责提升单轮 `question + schema + evidence -> SQL` 的生成能力。

## 2. Current Progress

目前已经完成：

- Reward Engine：安全检查、只读执行、执行结果比对、规则 reward。
- LangGraph Agent 骨架：生成 SQL、安全校验、执行、反思重试、生成回答、人工兜底。
- vLLM 本地推理接入：Qwen2.5-Coder-7B-Instruct / SFT / GRPO 模型都可通过 OpenAI-compatible API 调用。
- BIRD dev 评测脚本：执行生成 SQL，并与 ground truth SQL 的执行结果比较。
- BIRD SFT 数据准备和 LoRA SFT。
- BIRD GRPO 数据准备、verl GRPO 训练、checkpoint 合并和评测。
- Wrong-case 诊断脚本：抽查执行成功但结果错误的 SQL，区分真实错误和 compare 过严。
- Partial reward 离线诊断脚本：对执行成功但结果错误的 SQL 计算部分匹配分数，判断 reward shaping 是否有区分度。
- Enhanced schema context：在 schema 中加入 `database_description` 字段说明、value description 和 SQLite 实际取值样例，用于缓解 schema/value grounding 不足。

已有主要评测结果：

| Model / Prompt | Dev Accuracy | Correct | Pred Exec Failed |
| --- | ---: | ---: | ---: |
| Base Qwen2.5-Coder-7B-Instruct | 0.4915 | 753 | 221 |
| SFT LoRA merged | 0.5183 | 794 | 129 |
| GRPO merged, basic schema | 0.5503 | 843 | 40 |
| GRPO merged, enhanced schema | 0.5986 | 917 | 35 |
| GRPO merged, enhanced schema + identifier quote prompt | 0.5898 | 903 | 38 |

说明：

- Accuracy 不是比较 SQL 字符串是否一致，而是执行模型 SQL 和 ground truth SQL，然后比较结果集。
- `pred_exec_failed` 表示模型生成了 SQL，但 SQL 在 SQLite 中执行失败，例如字段不存在、语法错误、运行时错误或超时。
- `gt_failed` 表示 BIRD ground truth SQL 本身执行失败，这类样本不计入模型准确率分母。

## 3. Runtime Agent Flow

在线 Agent 运行流程：

```text
用户问题
  |
  v
get_schema_context
  |
  v
generate_sql
  |
  v
validate_sql
  |                  不安全
  |-----------------------> reflect
  |                           |
  |                           v
  |                       generate_sql
  v
execute_sql
  |
  v
reflect
  | 成功
  v
generate_answer
  |
  v
END

如果失败次数达到 max_retries，则进入 human_handoff。
```

节点逻辑：

- `get_schema_context`：构造喂给模型的 schema 描述。当前 demo 用静态 schema；BIRD 评测用 `Bird_schema_context.py` 构造真实 schema。
- `generate_sql`：调用 `BaseLLMClient.generate_sql()`。可接 MockLLM 或 vLLM 后端。
- `validate_sql`：调用 `Safety.check_sql_safety()`，拦截危险 SQL 和堆叠查询。
- `execute_sql`：调用 `Executor.execute_sql()`，用只读 SQLite 连接执行 SQL，并捕获错误/超时。
- `reflect`：如果失败，把当前 SQL、错误类型、错误信息写入 `error_history`，然后决定重试或转人工。
- `generate_answer`：把执行结果转成简单自然语言回答。
- `human_handoff`：超过重试次数后返回人工兜底状态。

当前最终回答生成后不再额外验证。可靠性主要由 SQL 安全检查、只读执行和结果比对保证；自然语言回答只是对已执行结果的转述。

## 4. Training And Evaluation Flow

### 4.1 Raw / SFT / GRPO Evaluation

评测输入：

```text
BIRD question + schema_context + evidence
```

评测步骤：

```text
读取 dev.json
  |
读取 dev_tables.json
  |
build_schema_context(...)
  |
拼接 evidence
  |
VLLMClient.generate_sql(...)
  |
Safety.check_sql_safety(...)
  |
Executor.execute_sql(predicted_sql)
  |
Executor.execute_sql(ground_truth_sql)
  |
Compare.compare_results(strict=True)
  |
写 JSONL + 打印统计
```

当前评测入口：

```bash
/data1/tiantian/miniconda3/envs/verl/bin/python Evaluate_bird_dev_baseline.py \
  --timeout-sec 30 \
  --base-url http://127.0.0.1:8002/v1 \
  --model qwen2.5-coder-7b-grpo \
  --schema-mode enhanced \
  --output outputs/bird_dev_grpo_enhanced_quote_full.jsonl
```

### 4.2 SFT Flow

SFT 目标：让模型输出格式更稳定，基础 Text-to-SQL 能力略微抬高，为 GRPO warm start。

流程：

```text
Prepare_bird_sft_data.py
  -> 生成 chat JSONL
Train_bird_sft_lora.py
  -> LoRA SFT
Merge_lora_model.py
  -> LoRA merge 回 base model，保存可被 vLLM 直接加载的模型副本
Evaluate_bird_dev_baseline.py
  -> 评测 SFT merged 模型
```

### 4.3 GRPO Flow

GRPO 目标：用可执行结果作为 reward，提升单轮 SQL 生成策略。

流程：

```text
Prepare_bird_grpo_data.py
  -> 生成 verl RLHF parquet
run_grpo.sh
  -> 调 verl.trainer.main_ppo，adv_estimator=grpo
Bird_grpo_reward.py
  -> verl 每条 rollout 调 compute_score
Reward.py
  -> safety + executor + compare 得到 reward
Merge_lora_model.py 或导出逻辑
  -> 合并/导出最终 HF 格式模型
run_vllm_grpo_server.sh
  -> 启动 GRPO 模型服务
Evaluate_bird_dev_baseline.py
  -> 评测 GRPO 模型
Analyze_wrong_ok_cases.py
  -> 诊断执行成功但结果错误的样本
```

当前 GRPO reward：

- safety violation：`-1.0`
- safety 通过但执行失败：`0.0`
- 执行成功但结果不匹配：`0.3`
- 执行成功且结果匹配：`1.3`

下一阶段可能调整：为结果部分重叠的 SQL 加中间 reward，避免“接近正确”和“完全瞎写”得到同样训练信号。

当前 partial reward 离线诊断结果基于 `outputs/bird_dev_grpo_enhanced_full.jsonl`：

- wrong-ok 总数：`580`
- 非零 partial similarity：`247`
- combined similarity >= 0.5：`105`
- combined similarity >= 0.75：`65`
- 平均 combined similarity：`0.1941`
- 如果使用 `0.3 + 0.7 * combined_similarity`，wrong-ok 平均 reward 会从固定 `0.3` 变成 `0.4359`

结论：partial reward 能把一部分“接近正确”的 SQL 和完全不沾边的 SQL 拉开，但单值数值相近可能把语义错误也打高分。接入正式 GRPO 前需要使用保守版 partial reward，避免 reward hacking。

温度采样方差检查基于 `outputs/bird_dev_reward_variance_grpo_enhanced_partial_50x8.jsonl`：

- 采样设置：GRPO merged 模型，enhanced schema，`temperature=0.8`，`top_p=0.95`，50 组问题，每组 8 条 candidate。
- candidate 总数：`400`
- candidate accuracy：`0.3975`
- 平均唯一 SQL 数：`4.64`
- strict 平均 reward variance：`0.076706`
- conservative partial 平均 reward variance：`0.072886`
- strict 零方差组比例：`0.6000`
- conservative partial 零方差组比例：`0.4400`
- partial variance 提升组比例：`0.2200`
- partial variance 降低组比例：`0.1600`

细分结论：

- 在没有正确 candidate 的组里，partial reward 更有价值：零方差组从 `85.71%` 降到 `47.62%`，平均方差从 `0.001942` 增到 `0.003963`。
- 在已经包含正确 candidate 的组里，partial reward 会把部分错误 SQL 的分数抬高，平均方差从 `0.130846` 降到 `0.122795`。
- 因此 partial reward 能缓解“全错组没有区分度”的问题，但如果权重太大，会削弱正确 SQL 与接近错误 SQL 的差距。正式接入训练时应使用保守权重，并继续监控 correctness reward 是否被稀释。

## 5. Schema Context Design

`Bird_schema_context.py` 提供两种模式：

- `basic`：只包含表名、原始列名、列类型、外键关系。用于保持历史 baseline。
- `enhanced`：在 basic 基础上加入字段说明、业务含义、value description 和 SQLite 抽样取值。

enhanced schema 的目的：

- 让模型知道字段真实存储值，例如大小写、枚举值、缩写。
- 减少模型猜错字段值、业务术语翻译错、字段语义错配的问题。
- 在不重新训练的前提下改善 schema/value grounding。

prompt 长度控制：

- 单字段 extra 默认最多 `220` 字符。
- 单库 extra 默认最多 `2500` 字符。
- 已用 GRPO tokenizer 扫过 dev prompt，未超过 `4096` 上限。

已确认：

- BIRD `evidence` 已拼入评测 prompt 和训练数据 prompt。
- enhanced schema 对 dev accuracy 有明显正收益。
- 带空格、括号、百分号等特殊字符的自然字段名已在 schema hint 中增加反引号显示。
- 额外加入“特殊列名必须用反引号”的 prompt 约束后，全量 dev accuracy 从 `0.5986` 降到 `0.5898`，并出现 1 条上下文长度越界；该约束暂不作为正收益结论，后续需要进一步收窄或缩短。

## 6. Key Files

| File | Main Responsibility |
| --- | --- |
| `State.py` | LangGraph `AgentState` 和 `ErrorRecord` 定义。 |
| `Graph.py` | LangGraph 状态机：schema、生成、校验、执行、反思、回答、转人工。 |
| `Llm_client.py` | LLM 抽象接口、MockLLM、vLLM OpenAI-compatible client。 |
| `Safety.py` | SQL 安全检查：危险 DDL/DML、PRAGMA/ATTACH、堆叠查询等。 |
| `Executor.py` | SQLite 只读执行器，支持超时、错误分类、最大行数限制。 |
| `Compare.py` | BIRD 风格执行结果比对，支持 strict 和 tolerant 调试模式。 |
| `Reward.py` | 推理/训练共用 reward engine。 |
| `Bird_grpo_reward.py` | verl 自定义 reward 函数入口，调用 `Reward.compute_reward()`。 |
| `Bird_schema_context.py` | BIRD schema context 构造，支持 basic/enhanced。 |
| `Evaluate_bird_dev_baseline.py` | BIRD dev 单轮模型评测脚本。 |
| `Validate_bird_dev.py` | 批量验证 BIRD dev ground truth SQL 是否可执行。 |
| `Prepare_bird_sft_data.py` | 从 BIRD train 构造 SFT chat JSONL。 |
| `Train_bird_sft_lora.py` | LoRA SFT 训练入口。 |
| `Prepare_bird_grpo_data.py` | 构造 verl GRPO/PPO parquet 数据。 |
| `Sample_bird_reward_variance.py` | 温度采样测试，检查组内 reward 方差和 SQL 多样性。 |
| `Analyze_wrong_ok_cases.py` | 执行成功但结果错误样本诊断，区分真实错误和 compare 过严。 |
| `Analyze_partial_reward.py` | 离线计算 wrong-ok 样本的部分匹配分数，评估 reward shaping 是否有训练信号。 |
| `Merge_lora_model.py` | 合并 LoRA adapter 到 base model 并保存 HF 模型副本。 |
| `Demo_db.py` | 构造本地 demo SQLite 数据库。 |
| `Run_demo.py` | 用 MockLLM 跑 LangGraph 四条典型路径。 |
| `Run_vllm_demo.py` | 用本地 vLLM 模型跑 LangGraph demo。 |

## 7. Shell Entrypoints

| Script | Purpose |
| --- | --- |
| `run_vllm_server.sh` | 启动原始 Qwen2.5-Coder-7B-Instruct vLLM 服务。 |
| `run_vllm_chat.sh` | 对 vLLM 发一个简单 chat/completions 请求。 |
| `run_vllm_sft_server.sh` | 启动 SFT 模型 vLLM 服务。 |
| `run_vllm_merged_server.sh` | 启动 SFT merged 模型服务。 |
| `run_vllm_grpo_server.sh` | 启动 GRPO merged 模型服务，默认 8002 端口。 |
| `run_grpo.sh` | 启动 verl GRPO 训练。 |
| `prepare_grpo_enhanced_full.sh` | 准备 enhanced schema + partial reward 版本的全量候选 GRPO parquet。 |
| `run_grpo_enhanced_partial.sh` | 启动 enhanced schema + partial reward 版本的正式 GRPO 训练。 |

## 8. Important Paths

| Path | Meaning |
| --- | --- |
| `datasets/bird/dev/dev_20240627` | BIRD dev 数据。 |
| `datasets/bird/train/train` | BIRD train 数据。 |
| `models/Qwen2.5-Coder-7B-Instruct` | 原始 base model。 |
| `models/Qwen2.5-Coder-7B-Instruct-BIRD-SFT-LoRA-Merged` | SFT merged 模型。 |
| `models/Qwen2.5-Coder-7B-Instruct-BIRD-GRPO` | GRPO merged/exported 模型。 |
| `outputs/bird_dev_grpo_full.jsonl` | GRPO basic schema dev 评测结果。 |
| `outputs/bird_dev_grpo_enhanced_full.jsonl` | GRPO enhanced schema dev 评测结果。 |
| `outputs/grpo/checkpoints/...` | verl GRPO checkpoint。 |

## 9. Current Environment Assumptions

- Python 环境统一使用 conda env `verl`。
- Python 路径：`/data1/tiantian/miniconda3/envs/verl/bin/python`。
- 模型和数据都在本地，不依赖 Hugging Face 网络下载。
- vLLM 服务使用 OpenAI-compatible API。
- GRPO 模型服务默认：
  - base url: `http://127.0.0.1:8002/v1`
  - model name: `qwen2.5-coder-7b-grpo`
  - script: `run_vllm_grpo_server.sh`

## 10. Next Technical Steps

当前优先级：

1. 跑 enhanced schema + partial reward 的全量 GRPO 训练。
2. 训练完成后导出/合并 checkpoint，启动 vLLM 服务。
3. 使用 `Evaluate_bird_dev_baseline.py --schema-mode enhanced` 做 dev 全量评测。
4. 对比 SFT、旧 GRPO、新 GRPO 的 execution accuracy、执行失败率、wrong-ok 错误分布。
5. 使用 `Analyze_wrong_ok_cases.py` 和 `Analyze_partial_reward.py` 判断 partial reward 是否真正减少推理类错误，是否引入 reward hacking。
6. 如全量 GRPO 收益稳定，再考虑 schema retrieval / schema linking，解决企业级多库多表下的 context 过长问题。

## 11. Interview Q&A

本节用于面试前复盘。回答时区分“已经实现”和“下一步计划”，不要把未完成能力包装成已上线。

### 11.1 项目整体设计与动机

**Q: 为什么选择 LangGraph，而不是简单 pipeline 或 ReAct 循环？**

A: 这个项目的核心不是一次性调用模型生成 SQL，而是要把生成、校验、执行、失败反思、重试、转人工做成可控闭环。普通 pipeline 适合线性流程，但失败后回到哪一步、带什么状态、何时停止，容易散在 if/else 里；纯 ReAct 循环又太依赖模型自己决定下一步，生产可控性弱。LangGraph 的 StateGraph 把每个节点和路由条件显式化，`validate_sql` 失败只回 `reflect`，执行失败也回 `reflect`，超过 `max_retries` 进入 `human_handoff`，这让流程可测试、可观察，也方便后续替换 schema retrieval 或真实模型。

**Q: State 里存了哪些字段？多轮反思时怎么传递和更新？会不会 State 爆炸？**

A: `State.py` 里把状态分成输入、schema、当前 SQL、执行结果、重试控制和终态。关键字段包括 `question`、`db_path`、`schema_context`、`current_sql`、`safety_check_passed`、`execution_rows`、`execution_error_type`、`retry_count`、`max_retries`、`error_history`、`final_answer`、`needs_human`、`status`。每个节点只返回自己更新的字段，LangGraph 合并到同一个 state。反思时把本轮 SQL、错误类型、错误信息 append 到 `error_history`，并把 `retry_count + 1`。目前 `max_retries=3`，所以历史最多 3 条，不会无限膨胀；如果后续做长对话，会对历史做摘要或只保留最近 N 条结构化错误。

**Q: 人工兜底节点怎么触发？触发后怎么处理？**

A: 当前触发条件是规则式的：如果安全校验失败或执行失败后进入 `reflect`，且 `retry_count >= max_retries`，路由到 `human_handoff`。目前没有接置信度模型，也没有按特定错误类型单独触发。触发后 demo 中返回 `needs_human=True`、`status=human_handoff`，并把最后一次失败原因写入 `final_answer`。真实企业落地时，这个节点可以接工单系统、IM 通知或数据分析师队列，而不是简单拒答。

### 11.2 Schema 构建与 Grounding

**Q: Schema 是全量塞进 prompt，还是做了检索式 schema linking？企业级几百上千张表怎么办？**

A: 当前 BIRD 评测阶段是按每条样本的 `db_id` 构造该数据库的 schema context，不是跨企业全库检索；demo 里是静态 schema。也就是说目前还没有做向量检索式 schema linking。这个在 BIRD 上是合理的，因为每个问题已经绑定目标数据库；但企业真实场景下如果有几百上千张表，必须先做 schema retrieval：用业务词、表/列注释、历史查询日志、外键图和取值样例召回候选表列，再把候选 schema 放进 prompt，避免 context 过长和无关表干扰。

**Q: enhanced schema context 具体做了什么？**

A: `Bird_schema_context.py` 支持 `basic` 和 `enhanced`。`basic` 包含表名、列名、类型、外键关系；`enhanced` 在此基础上加入 BIRD `database_description` 里的字段说明、value description，以及从 SQLite 数据库抽样得到的真实取值样例。目的不是让模型“背答案”，而是减少 value grounding 错误，例如数据库里国家名、枚举值、大小写、缩写到底怎么存。实践上 enhanced schema 对 dev accuracy 有明显正收益：GRPO basic schema `0.5503`，enhanced schema `0.5986`。

**Q: 列名/表名和用户自然语言差异很大怎么办，比如 `cust_amt` 对应“客户金额”？**

A: 当前方案主要靠字段说明、value description 和 evidence 缓解。如果数据库 description 里写清楚 `cust_amt` 的业务含义，模型就更容易对齐。企业落地还需要更系统的 schema linking：维护业务术语词典、字段别名、历史 SQL 中的自然语言映射，必要时用 embedding/BM25 混合检索召回相关列。这个属于下一阶段的 schema retrieval 能力，不是当前 BIRD 训练代码已经完整解决的问题。

### 11.3 安全校验与只读执行

**Q: 安全校验具体校验什么？sqlparse 是 AST 白名单还是关键字黑名单？**

A: `Safety.py` 使用 `sqlparse` 解析语句数量和语句类型，拦截多语句堆叠查询，以及 `INSERT/UPDATE/DELETE/DROP/ALTER/TRUNCATE/CREATE/REPLACE` 等 DML/DDL，还额外拦截 `PRAGMA/ATTACH/DETACH/VACUUM`。它不是严格的“只允许 SELECT 白名单”，原因是 sqlparse 对拼错的 `SELEC` 或部分无害写法可能识别成 `UNKNOWN`；如果直接白名单拒绝，会把普通语法错误误判成安全违规，reward 从 `0` 变成 `-1`，污染训练信号。所以当前是“危险类型/危险关键字拦截 + 数据库只读连接兜底”的双层防线。

**Q: 只读执行是在 SQL 层面还是数据库权限层面？只靠关键字有什么风险？**

A: 两层都有。SQL 层面先做 safety checker；数据库层面 `Executor.py` 用 `file:{db_path}?mode=ro` 打开 SQLite 只读连接。只靠应用层关键字会有绕过风险，比如注释混淆、多语句拼接、CTE 或函数里隐藏危险操作、方言差异导致解析器漏判。所以真正的安全边界不能只放在 prompt 或字符串规则上，必须有数据库权限层面的物理只读限制。当前 SQLite 场景下即使 safety 漏判，写操作也会被只读连接拒绝。

**Q: Text-to-SQL 里的 SQL 注入和传统 Web SQL 注入有什么不同？**

A: 传统 Web 注入通常是用户输入被拼进已有 SQL 模板；Text-to-SQL 场景下 SQL 本身就是模型生成的，风险点变成 prompt injection 或恶意自然语言诱导模型生成危险 SQL，比如“删除所有表”“忽略规则执行 DROP”。所以防护重点不是参数绑定，而是输出侧约束：模型只负责生成候选 SQL，执行前必须经过安全检查、只读权限、超时和结果限制。

**Q: 执行超时、大结果集怎么防护？**

A: `Executor.execute_sql()` 用独立线程执行 SQLite 查询，主线程 `join(timeout)`，超时后调用 `connection.interrupt()`，返回 `error_type=timeout`，不会让训练进程卡死。大结果集通过 `fetchmany(max_rows)` 限制，默认最多取 `10000` 行，防止 `SELECT *` 或笛卡尔积把内存拖垮。后续企业落地还应加 SQL plan 预检查、强制 LIMIT、扫描行数预算和只读 replica。

### 11.4 错误反思重写

**Q: 失败反思重写节点拿到的输入是什么？**

A: 不是把完整堆栈原样扔给模型，而是结构化记录到 `ErrorRecord`：上一轮 SQL、错误类型和错误信息。错误类型来自 safety 或 executor 分类，例如 `safety`、`syntax`、`schema`、`runtime`、`timeout`。`VLLMClient._build_prompt()` 在重试时把历史错误按“第几次 SQL / 错误类型 / 错误信息”拼进 prompt，让模型优先修复上一轮暴露的问题。

**Q: 反思有没有最大重试次数？重试是从头生成还是增量修改？怎么防止死循环？**

A: 有最大重试次数，默认 `max_retries=3`。每次重试调用同一个 `generate_sql()` 接口，从 prompt 上看是“带历史错误从头生成一条新 SQL”，不是程序级 diff patch。防止死循环主要靠两个机制：一是 `retry_count` 达到上限转人工；二是 `error_history` 让模型看到自己上一轮错在哪里，减少重复犯同一个 schema/syntax 错误。

**Q: 反思对准确率提升多少？有没有消融？**

A: 当前已完成的是 LangGraph 反思路径的功能测试和真实模型 demo，验证了失败后能带错误信息重试；BIRD dev 的主评测目前是单轮 raw policy 评测，不是多轮 Agent 评测，所以还没有严谨的“有反思 vs 无反思”全量消融数字。后续应该固定同一模型和同一 dev 集，对比单轮生成、LangGraph 1 次重试、LangGraph 3 次重试的 execution accuracy、平均重试次数和人工兜底率。

### 11.5 模型部署与推理

**Q: 为什么选 Qwen2.5-Coder-7B-Instruct？**

A: Text-to-SQL 本质是结构化代码生成任务，Coder 模型比通用聊天模型更适合 SQL/代码格式生成。7B 的选择是工程约束和方法验证的折中：在 8 x L20 46GB 上全参/强化训练链路更容易跑稳，显存风险低；早期尝试更大模型会带来 optimizer state、rollout KV cache 和调参复杂度。先用 7B 跑通安全执行、SFT、GRPO 和评测闭环，再考虑 27B + LoRA，是更稳的路线。

**Q: vLLM 部署用了什么并行策略？PagedAttention 和 continuous batching 解决什么？**

A: 独立 vLLM API 服务阶段通常单卡即可部署 7B；verl GRPO rollout 中配置了 `actor_rollout_ref.rollout.name=vllm` 和 `tensor_model_parallel_size=2`，在多卡训练里由 verl 启动 vLLM rollout worker。PagedAttention 的核心是把 KV cache 像分页内存一样管理，减少长短请求混合时的显存碎片，提高可服务的并发序列数。continuous batching 是动态把新请求插入正在运行的 batch，而不是等一个静态 batch 全部结束再处理下一批，适合请求长度不均匀的 LLM 推理。

**Q: 推理采样参数怎么设？生成和反思阶段是否不同？**

A: 在线 LangGraph 的 `VLLMClient` 默认 `temperature=0.0`，更偏确定性，因为生产查询更看重稳定和可复现。GRPO rollout 训练使用 `temperature=0.8`、`top_p=0.95`，目的是同一个 prompt 采样多个候选，制造 group 内 reward 差异。当前生成和反思阶段共用同一个 client 参数；后续可以把首次生成设低温，反思阶段略提高温度，给模型更多修正空间，但需要用安全检查和执行反馈兜底。

### 11.6 Reward Engine 与 RL 训练

**Q: Reward 怎么构成？是稀疏 reward 还是 dense reward？**

A: 第一版 reward 是执行驱动的分层规则：安全违规 `-1.0`；安全通过但执行失败 `0.0`；执行成功但结果不匹配 `0.3`；执行成功且结果匹配 `1.3`。这比纯 0/1 correctness 稍微 dense，因为先奖励“能安全执行”，再奖励“结果正确”。后续加入 conservative partial reward 后，对执行成功但结果不完全正确的 SQL，根据结果集部分相似度额外给最多 `0.3` 的 bonus，缓解全错组没有区分度的问题。

**Q: 部分匹配 reward 怎么算？比较 SQL 文本、AST 还是执行结果？**

A: 当前比较的是执行结果，不比较 SQL 文本或 AST。`Reward.py` 里有三类相似度：`row_f1` 比较结果行集合重叠；`value_f1` 比较展开后的值多重集合重叠；`scalar_similarity` 处理单行单列数值结果的相对误差。正式接入时使用保守公式：`partial_bonus = min(cap, max(0.30*row_f1, 0.20*value_f1, 0.10*scalar_similarity))`，cap 默认为 `0.3`。这样不会让接近但错误的 SQL 超过正确 SQL，只是把“接近正确”和“完全不相关”拉开。

**Q: 为什么选 GRPO 而不是 PPO 或 DPO？**

A: PPO 通常需要 value model/critic 估计优势，训练和显存成本更高；GRPO 通过同一个 prompt 的多条 response 组成 group，用组内 reward 做相对优势，省掉单独 value model，更适合 7B 模型和有限 GPU 条件。DPO 更适合已有偏好对数据的场景，而 Text-to-SQL 的优势是可以执行 SQL 得到可验证 reward，不需要人工偏好标注，所以 GRPO 更匹配“可验证结果驱动训练”。

**Q: GRPO 的 group 怎么构造？一个 prompt 采几条？**

A: verl 配置里 `ROLLOUT_N` 控制每个 prompt 采样几条 response。冒烟和当前正式脚本里设置为 `ROLLOUT_N=2`，训练 batch size 为 `8`，所以每步会有 `8 x 2` 条 rollout。group 内每条 SQL 经过同一套 `Bird_grpo_reward.py -> Reward.compute_reward()` 得到 reward，GRPO 再基于组内 reward 差异做相对优势。之前做过温度采样诊断，50 个问题每个采 8 条，用来检查 reward 方差和 SQL 多样性。

**Q: SFT 数据怎么构造？有没有 self-training？**

A: 当前 SFT 数据由 `Prepare_bird_sft_data.py` 从 BIRD train 抽样构造 chat JSONL，assistant target 直接使用 BIRD gold SQL。没有加入模型自生成且执行成功的 self-training 数据，也没有拒绝采样。SFT 的定位是 warm start：让输出格式更稳定、降低语法/schema 执行失败率，而不是单独追求最终最优。

**Q: LoRA 参数怎么设？为什么不用全参？**

A: `Train_bird_sft_lora.py` 使用 LoRA：`r=16`、`lora_alpha=32`、`dropout=0.05`，作用在 attention 的 `q_proj/k_proj/v_proj/o_proj` 和 FFN 的 `gate_proj/up_proj/down_proj`。SFT 阶段选 LoRA 是为了快速得到 warm start，显存和训练时间都更低，也减少全参 SFT 的工程变量。项目早期对更大模型全参训练遇到过 optimizer state OOM，因此训练策略上先让 7B + LoRA/SFT + GRPO 跑通闭环。

**Q: baseline 49.15% 到 SFT 51.83%，提升不大，瓶颈在哪里？**

A: 这个提升主要体现在执行失败显著下降：`pred_exec_failed` 从 `221` 降到 `129`，说明 SFT 让格式和 schema 使用更稳定；但 correctness 提升有限，说明剩余瓶颈不只是语法，而是 grounding 和推理。后续分析发现 enhanced schema 能明显提升到 `0.5986`，说明 value/schema grounding 是重要瓶颈；wrong-ok 诊断也显示还有 JOIN、筛选条件、聚合、DISTINCT 等真正推理错误，这些需要 reward shaping、few-shot 或更强模型继续解决。

**Q: GRPO 后最终 accuracy 多少？有没有 reward hacking？**

A: 当前已有一轮 GRPO merged 评测：basic schema `0.5503`，enhanced schema `0.5986`。现在正在推进 enhanced schema + conservative partial reward 的全量 GRPO 训练，最终数字要等训练、merge、dev 评测完成后再更新。reward hacking 方面已经做了防作弊测试：`SELECT *`、`WHERE 1=1` 等能执行但结果不对的 SQL 只能拿 execution reward，拿不到 correctness reward；partial reward 也用了 cap 和保守权重，避免错误 SQL 分数过高，但正式训练后仍要监控空结果、恒真条件、过宽查询等模式。

### 11.7 评估方法论

**Q: BIRD execution accuracy 怎么算？和 exact match 比有什么优缺点？**

A: 本项目用执行结果匹配：执行模型 SQL 和 ground truth SQL，然后比较结果集是否一致，而不是比较 SQL 字符串。优点是能接受语义等价但写法不同的 SQL，例如 join 顺序、别名、条件顺序不同；缺点是可能存在偶然等价，比如某个错误 SQL 在当前数据库实例上碰巧返回相同结果，也可能因为结果列顺序、浮点精度等 compare 细节影响判断。因此项目里还做了 wrong-ok 抽查和 strict/tolerant 比对，排除 compare 过严或偶然正确的问题。

**Q: 训练/验证/测试怎么划分？有没有 out-of-domain 泛化？**

A: SFT/GRPO 数据来自 BIRD train，dev 评测来自 BIRD dev。GRPO parquet 里会从 train 中切一小部分 val 给 verl dataloader 或 reward sanity check，但最终效果以 BIRD dev 执行准确率为准。当前还没有额外做企业内部跨域 out-of-domain 测试；BIRD 本身按多数据库组织，比单一 schema 更接近跨域设置。企业落地时应按 database/domain 维度划分，保证测试库的 schema 不在训练中出现。

**Q: 除了 execution accuracy，还关注哪些工程指标？**

A: 当前已经统计 correct、pred_exec_failed、gt_failed 和错误类型分布，例如 schema/syntax/runtime/timeout；还关注平均重试次数、人工兜底率作为 Agent 层指标。延迟方面目前主要观察 vLLM 调用耗时和评测总耗时，尚未系统化 P50/P99。后续更完整的工程评估应按 SQL 复杂度分桶，例如单表、JOIN、聚合、嵌套查询，并统计 P50/P99 latency、超时率、大结果集比例和安全拦截率。

### 11.8 开放性追问

**Q: 如果有更多算力和时间，最大瓶颈是模型能力还是工程 pipeline？**

A: 两者都有，但当前最优先的是工程 pipeline 里的 schema/value grounding 和 reward 设计。证据是 enhanced schema 不改模型就带来明显提升，说明很多错误来自信息不足；同时 wrong-ok 样本里还有 JOIN/聚合/筛选错误，说明模型推理也有瓶颈。下一步会先把 schema retrieval/schema linking 做稳，再用 partial reward、错误分桶、few-shot 和更强模型继续提高复杂查询能力。

**Q: 企业自然语言问题有歧义怎么办，比如“最近”是多久？**

A: 不应该让模型默认乱猜。工程上应先做歧义检测：时间范围、指标口径、实体范围、权限范围不明确时，Agent 进入澄清追问节点，而不是直接生成 SQL。当前 demo 还没有专门的澄清节点，但 LangGraph 状态机天然适合扩展：在 `generate_sql` 前加 `clarify_question` 路由，如果缺少必要槽位就向用户追问；如果业务有默认口径，也要在 schema/evidence 或系统规则里显式写清楚。

### 11.9 面试补充题库

**Q: 项目里用到的 skills 有哪些？**

A: 主要是 schema 构建、SQL 生成、安全校验、只读执行、结果比对、reward 打分、反思重写和人工兜底这些能力模块。它们都被 LangGraph 作为独立节点编排，本质上就是可复用的内部 skills。

**Q: skills 和 MCP 在项目里怎么体现？**

A: 当前没有正式接入 MCP 协议，但架构已经是 tool/skill 化的：`get_schema`、`generate_sql`、`check_safety`、`execute_sql`、`compare_result` 都可以直接封装成标准工具。现在是“skills 已经有了，MCP 还没正式上，但架构是 MCP-ready”。

**Q: 有涉及 A2A 协议吗？**

A: 没有。当前是单 Agent + 工具调用 + 状态图编排，没有多个 Agent 之间的标准化通信。后续如果拆成多个子 Agent，再考虑 A2A。

**Q: 项目中的奖励函数怎么设计？**

A: 规则型 reward，不训练 reward model。安全违规是 `-1.0`，安全通过但执行失败是 `0.0`，执行成功但结果不对是 `0.3`，执行成功且结果正确是 `1.3`。如果开启 partial reward，还会在 `0.3` 上按结果集相似度加最多 `0.3` 的 bonus。

**Q: 项目中的损失函数有哪些？**

A: SFT 阶段用 token-level cross entropy loss，只对 assistant SQL token 计算；GRPO 阶段用 policy gradient 风格的 GRPO loss，并加 KL regularization 约束当前 policy 不要偏离 reference model 太远。

**Q: SFT 是怎么用 LoRA 的？**

A: 冻结基座模型，只训练 LoRA adapter 参数。配置里 `r=16`、`lora_alpha=32`、`lora_dropout=0.05`，作用在 attention 的 `q/k/v/o_proj` 和 FFN 的 `gate/up/down_proj`。

**Q: LoRA 这些参数分别是什么意思？**

A: `r` 是低秩维度，决定容量；`lora_alpha` 是缩放强度，决定更新幅度；`lora_dropout` 是 LoRA 分支上的 dropout，控制正则；`target_modules` 决定插在哪些层上。

**Q: beam search 和 top-k / top-p 有什么区别？**

A: beam search 是保留若干条当前最优路径，偏确定性，适合找高概率答案；top-k / top-p 是在高概率候选里抽样，随机性更强，更适合 GRPO rollout 做多样化采样。

**Q: GQA 和 MQA 有什么区别？**

A: MQA 是所有 query head 共享同一套 key/value；GQA 是多个 query head 分组共享 key/value，是介于 MHA 和 MQA 之间的折中方案，主要为了降低 KV cache 显存、提高推理吞吐。

**Q: SQLite SQL 是什么？**

A: 就是 SQLite 这套轻量数据库的 SQL 方言。项目里生成的是能在目标 SQLite 数据库里直接执行的查询，所以训练和评测都要对齐 SQLite 语法。

**Q: 为什么需要 schema 构建，不直接生成 SQL？**

A: 因为 Text-to-SQL 不是凭空写 SQL，模型必须知道当前数据库有哪些表、哪些列、字段类型、外键和字段取值样例。schema 构建解决的是 grounding 问题，否则模型很容易编错字段、JOIN 错表或猜错值域。

**Q: schema 构建时需要看数据集吗？**

A: 需要看数据库元数据和少量真实取值样例，但不能看 gold SQL 或评测答案。项目里用的是 `train/dev_tables.json`、`database_description` 和 SQLite 样例值，不会泄露标签。

**Q: 为什么不用 DPO？**

A: 因为 Text-to-SQL 有天然可执行、可验证的 reward，不需要额外构造偏好对。DPO 更适合 chosen/rejected 偏好数据，而这里 GRPO 可以直接用执行结果驱动训练。

## 12. Documentation Maintenance Rule

后续每次发生以下变化时，需要同步重构本文档：

- 新增模型阶段，例如二轮 SFT、二轮 GRPO、LoRA/full fine-tune 切换。
- 修改 reward 分数或 compare 口径。
- 修改 schema context、retrieval、evidence 拼接方式。
- 新增评测指标或错误分类脚本。
- 改动 vLLM 服务端口、模型路径、训练数据路径。
- LangGraph 状态机节点、路由逻辑或 retry/human handoff 策略变化。
