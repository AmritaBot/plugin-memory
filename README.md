# amrita_plugin_memory

Amrita 长期记忆插件 - 一个智能记忆管理系统，用于帮助 AI 助手记住关于用户的长期信息。

## 功能特点

- **长期记忆存储**：持久化存储关于用户的长期重要信息
- **智能标签系统**：支持使用标签对记忆进行分类管理
- **重要性分级**：支持设置记忆的重要程度（1-5级）
- **自动过期机制**：支持设置不同有效期（短期、长期、永久）
- **灵活检索**：支持按关键词和标签检索记忆内容

## 数据模型

- **LongTermMemory**：核心数据模型，存储记忆信息
  - unique_id: 记忆的唯一标识符
  - ins_id: 实例ID（通常为用户ID或群ID）
  - content: 记忆内容
  - tag: 分类标签（多个标签用空格分隔）
  - importance: 重要性等级（1-5级）
  - expired_at: 过期时间

## 工具函数

插件提供了四个主要工具函数：

### 1. write_memory

将重要信息存入长期记忆

参数：

- content: 记忆内容，简洁明了
- tags: 分类标签数组，如 `["preference", "project"]`
- importance: 重要性等级（1-5）
- expiry_hint: 保存时长（"short_term", "long_term", "permanent"）

### 2. read_memory

从记忆库检索用户相关信息

参数：

- query: 关键词（使用空格分割）
- tags: 标签过滤条件（可选）
- limit: 返回数量限制（默认5条）

### 3. update_memory

更新指定ID的记忆内容

参数：

- id: 要更新的记忆ID
- content: 新的记忆内容（可选）
- tags: 新的标签列表（可选）
- importance: 新的重要性等级（可选）
- expiry_hint: 新的过期提示（可选）

### 4. delete_memory

删除指定ID的记忆

参数：

- id: 要删除的记忆ID

## 配置选项

- `short_term_expiry_days`: 短期记忆的过期天数，默认为3天
- `long_term_expiry_days`: 长期记忆的过期天数，默认为30天
- `permanent_expiry_days`: 永久记忆的过期天数，默认为1年

## 安装和使用

1. 安装插件：`amrita plugin install amrita_plugin_memory`
2. 配置插件：插件会自动生成配置文件，或者您可以在WebUI中配置。
