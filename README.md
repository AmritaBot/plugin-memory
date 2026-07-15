# amrita_plugin_memory

Amrita 长期记忆插件 — 基于 ChromaDB 向量数据库的智能记忆管理系统。

**v0.2 新增：常驻推理循环（潜意识层）** — Agent 在空闲时自动整理记忆，基于 AmritaCore Agent 框架实现。

## 技术栈

| 组件       | 技术                                                |
| ---------- | --------------------------------------------------- |
| 后端框架   | Python 3.10+ / NoneBot2 / AmritaCore / AmritaSense  |
| 向量数据库 | ChromaDB（本地 PersistentClient / 远程 HttpClient） |
| 嵌入模型   | OpenAI Embedding API / Ollama Embedding API         |
| 调度引擎   | nonebot_plugin_apscheduler                          |
| 配置管理   | Pydantic + TOML                                     |
| 异步处理   | asyncio + aiologic + aiohttp                        |
| 代码质量   | Ruff + Pyright                                      |

### 核心模块

- **vector.py** — 对 ChromaDB 原生 API 进行 `asyncio.to_thread` 封装。按用户粒度加锁（`aiologic.Lock`），支持集合管理、语义检索、CRUD。
- **tools.py** — 5 个聊天 Function Calling 工具 + `/memory` 命令处理器。通过 `scope` 参数（group/user）实现分区隔离。
- **rethinking/** — 常驻推理循环。LLM 在后台自动扫描、合并、更新记忆，调用 `subconscious_iter_stop` 自调度下次时间。

### Harness 模式

`SubconsciousRunner` 不自己管理 `WorkflowInterpreter`，而是将 **ChatObject 作为数据容器** + 复用 Core 的 Agent 框架：

```
ChatObject(train, preset, backend=SubconsciousBackend, agent_strategy=ReActAgentStrategy)
  → 替换 _workflow, _interpreter
  → chat_obj.begin() → await chat_obj
  → AGENT_ENTRY → WHILE(SINGLE_STRATEGY_CALL).ACTION(REACT_COUNTER)
```

暂停时 `dump_interpreter()`，下次 `_run()` 时 `rebase_context()` 恢复，毫秒级断点续推理。

详见 [rethinking/README.md](amrita_plugin_memory/rethinking/README.md)。

## 功能

### 长期记忆

| 功能     | 说明                                         |
| -------- | -------------------------------------------- |
| 语义检索 | 嵌入向量相似度搜索，支持中日英               |
| 分区隔离 | `scope="user"` 个人 / `scope="group"` 群共享 |
| 重要性   | low / medium / high 三级，支持过滤           |
| 标签     | 自定义分类标签（preference、project 等）     |
| 过期     | 短/长/永久三级过期天数配置                   |
| 并发安全 | 用户 ID 粒度加锁                             |

### 常驻推理循环（实验性）

| 功能     | 说明                                    |
| -------- | --------------------------------------- |
| 自动整理 | LLM 后台去重、合并、标签补全            |
| 自调度   | `subconscious_iter_stop` 返回下次时间   |
| 暂停协调 | 目标用户聊天时自动暂停推理              |
| 主动消息 | 允许 LLM 向用户发起主动问候（配置开关） |

## 工具

### 聊天工具（`tools.py`）

五个工具可供 LLM 在对话中调用：

| 工具            | 参数                                         |
| --------------- | -------------------------------------------- |
| `write_memory`  | content, tags, importance(enum), scope(enum) |
| `read_memory`   | query, top_k(5), importance?, scope(enum)    |
| `update_memory` | id, scope, content?, tags?, importance?      |
| `delete_memory` | id, scope                                    |
| `list_memory`   | limit, scope                                 |

### 潜意识工具（`rethinking/tools.py`）

八个工具仅供 Underconscious Agent 使用，不暴露给聊天 LLM：

| 工具                             | 用途                              |
| -------------------------------- | --------------------------------- |
| `subconscious_read_memory`       | 检索记忆（硬编码 user scope）     |
| `subconscious_write_memory`      | 写入记忆                          |
| `subconscious_update_memory`     | 更新记忆                          |
| `subconscious_delete_memory`     | 删除记忆                          |
| `subconscious_list_memory`       | 列出全部记忆                      |
| `subconscious_iter_stop`         | 结束本轮，设置下次时间            |
| `subconscious_send_to_user`      | 主动消息（需 allow_send_to_user） |
| `subconscious_read_chat_context` | 读取用户最近聊天记录              |

## 配置

### `config/amrita_plugin_memory/config.toml`

```toml
# 记忆过期
short_term_expiry_days = 3
long_term_expiry_days = 30
permanent_expiry_days = 365
per_session_memory_limit = 50

# 潜意识推理
[subconscious]
enabled = false
experimental = true
target_user_id = ""
max_iterations = 10
loop_detect_threshold = 3
initial_delay_seconds = 60
default_interval_seconds = 600
pause_on_user_chat = true
pause_wakeup_min_seconds = 180
pause_wakeup_max_seconds = 480
allow_send_to_user = false
```

### 环境变量（`.env`）

| 变量                      | 类型                | 默认值                 | 说明          |
| ------------------------- | ------------------- | ---------------------- | ------------- |
| `VECTOR_DB_TYPE`          | local/remote        | local                  | ChromaDB 类型 |
| `VECTOR_DB_SERVER`        | string              | 127.0.0.1              | 远程地址      |
| `VECTOR_DB_PORT`          | int                 | 8000                   | 远程端口      |
| `EMBEDDING_MODEL_URL`     | string              | http://127.0.0.1:11434 | 嵌入模型      |
| `EMBEDDING_MODEL_NAME`    | string              | auto                   | 模型名        |
| `EMBEDDING_PROCTOL`       | openai/ollama-embed | ollama-embed           | 协议          |
| `EMBEDDING_MODEL_API_KEY` | string              | (空)                   | API Key       |

## 安装

```bash
ambot plugin install amrita_plugin_memory
```

前置要求：Python 3.10+ / AmritaBot 实例 / Ollama 或 OpenAI 嵌入服务 / 可选 ChromaDB 远程服务。

## 开发

```bash
uv sync                       # 安装依赖
ruff format . && ruff check . # 格式化 + 检查
pyright                       # 类型检查
```
