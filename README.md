# amrita_plugin_memory

Amrita 长期记忆插件 —— 基于 ChromaDB 向量数据库的智能记忆管理系统，让 AI 助手能够持久化地记住关于用户的重要信息。

## 技术栈

| 组件       | 技术                                                    |
| ---------- | ------------------------------------------------------- |
| 后端框架   | Python 3.10+ / NoneBot2 / Amrita Core                   |
| 向量数据库 | ChromaDB（支持本地 PersistentClient 与远程 HttpClient） |
| 嵌入模型   | OpenAI Embedding API / Ollama Embedding API             |
| 配置管理   | Pydantic + TOML                                         |
| 异步处理   | asyncio + aiologic + aiohttp                            |
| 代码质量   | Ruff + Pyright                                          |

## 架构概览

```
amrita_plugin_memory/
├── __init__.py    # 插件入口，注册依赖
├── config.py      # 配置模型（ConfigFile / EnvConfig / DataManager）
├── embed.py       # Ollama 嵌入适配器
├── vector.py      # ChromaDB 异步封装（WrappedClientAPI / AsyncUserMemory）
└── tools.py       # 工具函数定义与注册（write/read/update/delete/list）
```

- **config.py**: 定义配置文件（过期天数、记忆上限）和环境变量（数据库类型、嵌入模型地址等），通过 Pydantic 模型校验。
- **embed.py**: 实现 Ollama 协议的嵌入适配器，通过 HTTP API 调用 `/api/embed` 端点。
- **vector.py**: 对 ChromaDB 原生 API 进行 `asyncio.to_thread` 封装，提供异步的集合管理与记忆 CRUD。使用 `aiologic.Lock` 按用户粒度加锁，保证并发安全。
- **tools.py**: 定义 5 个 Function Calling 工具（`write_memory` / `read_memory` / `update_memory` / `delete_memory` / `list_memory`），注册到 Amrita 的工具系统中。通过 `scope` 参数支持群共享记忆与用户专属记忆的分区隔离。

## 功能特点

- **长期记忆存储**：将用户偏好、项目信息等持久化存入 ChromaDB 向量数据库
- **群/用户记忆隔离**：LLM 可通过 `scope` 参数选择存入群共享记忆（群内所有人可见）或用户专属记忆（仅自己可见）
- **语义检索**：基于嵌入向量的语义相似度检索，支持中英文关键词
- **智能标签**：支持使用自定义标签对记忆进行分类（如 `preference`、`project`）
- **重要性分级**：三级重要程度（`low` / `medium` / `high`），支持按级别过滤检索
- **完整 CRUD**：支持记忆的创建、查询、更新、删除和列表查看
- **并发安全**：按用户 ID 粒度加锁，确保同一用户的操作串行化
- **多租户支持**：通过 ChromaDB remote 模式支持远程向量数据库

## 工具函数

### 1. `write_memory` —— 写入记忆

将当前用户（或群组）的重要信息存入长期记忆。

| 参数         | 类型                            | 必需 | 描述                                                  |
| ------------ | ------------------------------- | ---- | ----------------------------------------------------- |
| `content`    | string                          | ✅   | 记忆内容，简洁明了                                    |
| `tags`       | string                          | ✅   | 分类标签，如 `preference`、`project`                  |
| `importance` | `"low"` / `"medium"` / `"high"` | ✅   | 重要性等级                                            |
| `scope`      | `"group"` / `"user"`            | ✅   | `group`=群共享，`user`=个人专属（私聊不可用 `group`） |

### 2. `read_memory` —— 检索记忆

从记忆库中按语义相似度检索用户相关信息。

| 参数         | 类型                            | 必需 | 默认值 | 描述                       |
| ------------ | ------------------------------- | ---- | ------ | -------------------------- |
| `query`      | string                          | ✅   | —      | 关键词字符串，空格分隔     |
| `top_k`      | integer                         | ❌   | `5`    | 返回结果数量               |
| `importance` | `"low"` / `"medium"` / `"high"` | ❌   | —      | 按重要性过滤               |
| `scope`      | `"group"` / `"user"`            | ✅   | —      | 检索范围：群共享或个人专属 |

### 3. `update_memory` —— 更新记忆

更新指定 ID 的记忆内容、标签或重要性。

| 参数         | 类型                            | 必需 | 描述            |
| ------------ | ------------------------------- | ---- | --------------- |
| `id`         | string                          | ✅   | 要更新的记忆 ID |
| `scope`      | `"group"` / `"user"`            | ✅   | 记忆所属范围    |
| `content`    | string                          | ❌   | 新的记忆内容    |
| `tags`       | string                          | ❌   | 新的标签        |
| `importance` | `"low"` / `"medium"` / `"high"` | ❌   | 新的重要性等级  |

### 4. `delete_memory` —— 删除记忆

删除指定 ID 的记忆。

| 参数    | 类型                 | 必需 | 描述            |
| ------- | -------------------- | ---- | --------------- |
| `id`    | string               | ✅   | 要删除的记忆 ID |
| `scope` | `"group"` / `"user"` | ✅   | 记忆所属范围    |

### 5. `list_memory` —— 列出记忆

列出当前用户的所有记忆（返回 scope、标签和 ID）。

| 参数    | 类型                 | 必需 | 默认值 | 描述                       |
| ------- | -------------------- | ---- | ------ | -------------------------- |
| `limit` | integer              | ❌   | `5`    | 返回数量限制               |
| `scope` | `"group"` / `"user"` | ✅   | —      | 列出范围：群共享或个人专属 |

## 配置选项

### 文件配置（`config/amrita_plugin_memory/config.toml`）

| 配置项                     | 类型 | 默认值 | 描述                   |
| -------------------------- | ---- | ------ | ---------------------- |
| `short_term_expiry_days`   | int  | `3`    | 短期记忆过期天数       |
| `long_term_expiry_days`    | int  | `30`   | 长期记忆过期天数       |
| `permanent_expiry_days`    | int  | `365`  | 永久记忆过期天数       |
| `per_session_memory_limit` | int  | `50`   | 每个会话的记忆数量上限 |

### 环境变量（`.env` 或系统环境变量）

| 变量                       | 类型                      | 默认值                   | 描述                      |
| -------------------------- | ------------------------- | ------------------------ | ------------------------- |
| `VECTOR_DB_TYPE`           | `local` / `remote`        | `local`                  | ChromaDB 数据库类型       |
| `VECTOR_DB_SERVER`         | string                    | `127.0.0.1`              | 远程 ChromaDB 地址        |
| `VECTOR_DB_PORT`           | int                       | `8000`                   | 远程 ChromaDB 端口        |
| `VECTOR_DB_SERVER_SSL`     | bool                      | `false`                  | 是否启用 SSL              |
| `VECTOR_DB_REMOTE_HEADERS` | dict                      | `{}`                     | 远程请求头                |
| `VECTOR_DB_TENANT`         | string                    | `default`                | 租户名称                  |
| `VECTOR_DB_DATABASE`       | string                    | `default`                | 数据库名称                |
| `EMBEDDING_MODEL_URL`      | string                    | `http://127.0.0.1:11434` | 嵌入模型地址              |
| `EMBEDDING_MODEL_NAME`     | string                    | `auto`                   | 嵌入模型名称              |
| `EMBEDDING_PROCTOL`        | `openai` / `ollama-embed` | `ollama-embed`           | 嵌入模型协议              |
| `EMBEDDING_MODEL_API_KEY`  | string                    | (空)                     | 嵌入模型 API 密钥（可选） |

## 安装与使用

### 前置条件

- Python 3.10 ~ 3.13
- 已部署的 [AmritaBot](https://github.com/AmritaBot) 实例
- （本地模式）无需额外组件
- （远程模式）已运行的 ChromaDB 服务端
- 已运行的嵌入模型服务（如 Ollama）

### 安装

```bash
ambot plugin install amrita_plugin_memory
```

### 配置

插件安装后会自动生成配置文件。你可以通过以下方式配置：

1. **WebUI 配置**：在 Amrita 的 Web 管理界面中直接修改
2. **手动编辑**：修改 `config/amrita_plugin_memory/config.toml`
3. **环境变量**：通过 `.env` 文件或系统环境变量设置数据库和嵌入模型参数

### 验证

启动后查看日志，确认向量数据库连接成功：

```
正在尝试向量数据库连接...
当前向量数据库类型: local
创建了向量数据库客户端！
向量数据库版本：x.x.x
完成。
```

## 开发

```bash
# 安装开发依赖
uv sync

# 代码格式化与检查
ruff format .
ruff check .

# 类型检查
pyright
```
