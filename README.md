# amrita_plugin_memory

Amrita 长期记忆插件 - 一个智能记忆管理系统，用于帮助 AI 助手记住关于用户的长期信息。

## 技术栈

- **后端**: Python 3.x 插件架构
- **向量数据库**: ChromaDB (支持本地和远程模式)
- **嵌入模型**: 支持 OpenAI Embedding API 和 Ollama Embedding API
- **框架**: NoneBot2 + Amrita Core + AmritaBot
- **配置管理**: Pydantic + TOML
- **异步处理**: asyncio + aiologic + aiohttp

## 功能特点

- **长期记忆存储**：持久化存储关于用户的长期重要信息
- **智能标签系统**：支持使用标签对记忆进行分类管理
- **重要性分级**：支持设置记忆的重要程度（low/medium/high）
- **灵活检索**：支持按关键词检索记忆内容
- **完整管理**：支持记忆的增删改查和列表查看

## 工具函数

插件提供了五个主要工具函数：

### 1. write_memory

将重要信息存入长期记忆

参数：

- content: 记忆内容，简洁明了（必需）
- tags: 分类标签，如 `"preference"` 或 `"project"`（必需）
- importance: 重要性等级（必需，可选值：`"low"`, `"medium"`, `"high"`）

### 2. read_memory

从记忆库检索用户相关信息

参数：

- query: 关键词字符串，用空格分割（必需）
- top_k: 返回数量，默认5条（可选）
- importance: 重要性等级过滤（可选，可选值：`"low"`, `"medium"`, `"high"`）

### 3. update_memory

更新指定ID的记忆内容

参数：

- id: 要更新的记忆ID（必需）
- content: 新的记忆内容（可选）
- tags: 新的标签（可选）
- importance: 新的重要性等级（可选，可选值：`"low"`, `"medium"`, `"high"`）

### 4. delete_memory

删除指定ID的记忆

参数：

- id: 要删除的记忆ID（必需）

### 5. list_memory

列出当前用户的所有记忆

参数：

- limit: 返回数量限制，默认5条（可选）

## 配置选项

- `short_term_expiry_days`: 短期记忆的过期天数，默认为3天
- `long_term_expiry_days`: 长期记忆的过期天数，默认为30天
- `permanent_expiry_days`: 永久记忆的过期天数，默认为1年
- `per_session_memory_limit`: 每个会话的记忆数量限制，默认为50条

## 安装和使用

1. 安装插件：`amrita plugin install amrita_plugin_memory`
2. 配置插件：插件会自动生成配置文件，或者您可以在WebUI中配置。
