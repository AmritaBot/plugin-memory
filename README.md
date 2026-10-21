# amrita_plugin_memory

基于向量数据库的长期记忆插件 — **三层记忆模型**：

| 层 | 名称          | 类比                       | 载体                             | 何时使用             |
| -- | ------------- | -------------------------- | -------------------------------- | -------------------- |
| L1 | 备忘录        | 恒常激活的持久记忆         | 插件自有 ORM 表（per-uni_id 单行） | 每轮常驻注入         |
| L2 | 向量记忆层    | 语义记忆（按线索提取）     | ChromaDB 向量库                   | LLM 按需检索         |
| L3 | 离线巩固循环  | 默认模式网络（DMN）        | 后台 ReAct Agent                  | 空闲窗口到达时整理   |

- **L1** — 一段千字内的用户元信息（身份、稳定偏好、用户要求的工具调用模式），由 LLM 维护，每轮注入 system prompt。
- **L2** — 向量化的长尾事实库，LLM 通过工具按需读写，ChromaDB 语义检索。
- **L3** — 用户聊过之后，在作息里的冷门时段触发一轮后台整理：去重、压缩、画像精炼、知识沉淀。

## 安装

```bash
ambot plugin add amrita_plugin_memory
```

前置依赖：Python 3.11–3.13 / AmritaBot 实例 / **Ollama 或 OpenAI 嵌入服务** / ChromaDB。

> 嵌入服务是**必需**前置 —— 本插件不自带嵌入模型。

## 快速开始

### 1. 配置环境变量（`.env`）

```env
VECTOR_DB_TYPE=local
EMBEDDING_MODEL_URL=http://127.0.0.1:11434
EMBEDDING_MODEL_NAME=auto
EMBEDDING_PROCTOL=ollama-embed
```

### 2. L1 + L2 开箱可用

无需额外配置。LLM 会在需要时自动调用 `write_memory` / `read_memory` / `update_memo` 等工具；
L1 备忘录一旦写入，此后每轮对话都会自动注入。

### 3. 开启 L3 离线巩固循环（可选）

编辑 `config/amrita_plugin_memory/config.toml`：

```toml
[subconscious]
enabled = true
target_user_id = "你的QQ号"
```

用户发过消息后，后台 Agent 会在**下一个空闲窗口**里整理记忆库 —— 默认按作息热度图挑冷门时段，而不是固定 30 分钟。

> **适用场景**：L3 专为**个人助理**场景设计 —— 单个 Bot 服务单个用户。
> 它会在后台持续调用 LLM，**每轮推理可能消耗数万 tokens**。
> 如果 Bot 服务于大量用户或对 token 成本敏感，建议保持 `enabled = false`。

允许 Agent 主动发私聊消息：

```toml
allow_send_to_user = true
```

关闭全局知识库以节省 token：

```toml
enable_knowledge = false
```

> 知识库当前依赖 L3 —— 当 `enabled = false` 时，知识库也会自动禁用。

### 4. 验证

```bash
ambot memory status      # 查看嵌入指纹、记忆分布与备份
```

观察日志中 `[Subconscious]` / `[Memory]` 前缀的输出：

```text
[Subconscious] Starting for user=你的QQ号
[Subconscious] Idle — waiting for user chat to trigger first run
```

用户发消息后会先看到 `Rescheduled: penalty=#1, ...`，里面带着选中的窗口
（星期几、起止时刻、长度、分层、放宽次数）。时间到了才有 `Cycle start`。
第一次运行时热度图还是空的，会直接落在 `idle_target_hour` 的固定窗口。

---

## 三层记忆模型

L1 是每轮注入的一段千字内元信息，LLM 用 `update_memo` 整篇改写；L2 是 ChromaDB 长尾事实库，
按需检索；L3 在空闲窗口里读 L2 与归档 session，做去重、精炼、画像与知识沉淀。
L1 临近上限时把细节下放到 L2，只留指针。

## L1 备忘录

`memo_max_chars`（默认 1000）是硬上限，所以主力写入是整篇替换：读当前 memo、融合新事实、
输出全文、服务端校验长度后落库。超长时工具拒绝写入并返回当前字数，由 LLM 自己压缩重试。

放身份、稳定偏好、用户指定的工具调用模式；具体事件与项目细节放 L2。只写陈述性事实，
不写祈使句规则 —— 那是 `extra_prompt` 的地盘，插件不读也不写它。

表是 `amrita_plugin_memory_user_memo`，主键为框架 uni_id，个人与群各占一行。
注入走 `on_precompletion`（priority=15），与 L3 解耦：私聊注入个人 memo，
群聊只注入群 memo（不含发言人的个人 memo），空 memo 不注入。

工具三个：`read_memo`、`append_memo`（末尾追加）、`update_memo`（整篇替换）。

## L2 向量记忆层

`scope` 表达语义，与分区键是两个维度：

| `scope` | 含义                       | 可用场景 |
| ------- | -------------------------- | -------- |
| `user`  | 个人专属（群聊私聊互通）   | 任意     |
| `group` | 群共享（群内所有人可见）   | 仅群聊   |

分区键跟随 Amrita 的 `make_uni_id`（旧版 `user_{qq}`，新版 `QQPlatform_Private_{qq}`），
插件不硬编码，升级后自动跟随；存量 Key 由启动检查幂等迁移。

每条记忆带 `user_id` / `scope` / `tags` / `importance` / `created_at`，检索支持 `top_k`
与 `importance` 过滤，结果附带距离（ChromaDB 默认 l2，越小越相似）。

时间退化分两段：检索期按 `1/(1+d) × max(0.5^(age/half_life), floor)` 重排后截回 `top_k`
（只重排不删）；清理期按 importance 的寿命阈值扫描，超龄先降级，降到 `low` 仍超龄才删除。
两段都受 `[decay].enabled` 控制。

## L3 离线巩固循环（DMN）

复用 Core 的 `REACT_ONLY`，`SubconsciousRunner` 只注入隔离的 `SubconsciousBackend`
与 `ReActAgentStrategy`。每轮第一件事必须调 `subconscious_get_context`（精确时间、
距上轮间隔、上轮摘要、待审建议、库规模），system prompt 因此只放不变量 ——
同一天内逐字节一致，能吃到 provider 的前缀缓存。

触发不用定时自循环：用户发消息就给所在时槽加一次热度、惩罚 +1、重挑窗口。
默认按 7 天 × 12 槽的热度图找冷段，`penalty` 的含义是"要求多长的冷段"而非延迟分钟数；
没有历史时落在 `idle_target_hour`。关掉 `use_idle_window_planner` 回退成固定指数退避。

整理职责：去重合并、重要性校准与标签统一、画像精炼、知识沉淀、临近上限时压缩 L1。

## 嵌入模型与重映射

向量只在同一模型下可比，换模型会让旧向量静默劣化且不报错。插件在集合 metadata 里记
`sha256(protocol|model|base_url)`，每次启动校验；不一致时按 `EMBED_MISMATCH_POLICY`
处理：`auto` 备份后删集合重建重嵌，`ask` 交互确认（非 TTY 拒绝加载插件），`never` 仅告警。
指纹只看配置，同名模型被服务端换掉检测不到，用 `ambot memory reindex` 手动处理。

---

## 工具参考

### 表层工具（对话 LLM 可用）

| 工具                | 参数                                          |
| ------------------- | --------------------------------------------- |
| `write_memory`      | content, tags, importance(enum), scope(enum)  |
| `read_memory`       | query, top_k(5), importance?, scope(enum)     |
| `update_memory`     | id, scope, content?, tags?, importance?       |
| `delete_memory`     | id, scope                                     |
| `list_memory`       | limit(10, 上限50), offset, scope              |
| `read_memo`         | —（读取当前作用域备忘录全文）                 |
| `append_memo`       | content（在备忘录末尾追加一段）               |
| `update_memo`       | content（L1 备忘录整篇替换）                  |
| `knowledge_list`    | —                                             |
| `knowledge_read`    | kid, start_line?, end_line?                   |
| `knowledge_search`  | query, top_k?                                 |
| `knowledge_suggest` | action, title, summary, body, reason          |
| `read_profile`      | start_line?, end_line?                        |

知识库的三个只读工具 `knowledge_list` / `knowledge_read` / `knowledge_search`
需要 LP 权限节点 `memory.knowledge`；`lp.admin` 与 `.env` 里的超级用户始终持有。
未授权时工具返回错误 JSON，不返回任何知识内容。

用户画像的只读工具 `read_profile` 走另一条口径：只对离线巩固循环的目标用户
（`subconscious.target_user_id`）开放，且只能读自己那份画像，不占用权限节点。
返回值与循环侧一致：`summary` + `content` 行窗口 + `total_lines` / `range_start` / `range_end`。
画像写入仍只发生在循环内部，表层不开放。

### 巩固循环工具（`rethinking/tools.py`，共 21 个 handler）

记忆与 session/画像工具注册在隔离的 `_SUBCONSCIOUS_TOOLS` 上，不污染全局工具管理器。
知识库的 `list`/`read`/`search` 双重注册：循环侧不受限，表层（对话 LLM）走
`memory.knowledge` 权限校验；`create`/`update`/`delete` 仅循环可用，
表层通过 `knowledge_suggest` 提交建议。

| 工具                             | 用途                                   |
| -------------------------------- | -------------------------------------- |
| `subconscious_read_memory`       | 语义检索                               |
| `subconscious_write_memory`      | 写入新记忆                             |
| `subconscious_update_memory`     | 更新指定 ID 记忆                       |
| `subconscious_delete_memory`     | 删除指定 ID 记忆                       |
| `subconscious_list_memory`       | 列出全部记忆                           |
| `subconscious_iter_stop`         | 结束本轮推理                           |
| `subconscious_send_to_user`      | 主动向用户发消息                       |
| `subconscious_read_chat_context` | 读取最近聊天记录                       |
| `subconscious_duplicate_helper`  | 去重辅助（返回记忆 + 合并指导 prompt） |
| `subconscious_get_context`       | 一次性拉取本轮环境（时间 / 间隔 / 摘要 / 待审建议 / 库规模） |
| `subconscious_get_memory_stats`  | 统计概览                               |
| `subconscious_knowledge_list`    | 列出全局知识条目                       |
| `subconscious_knowledge_read`    | 读取知识条目（支持行滑动）             |
| `subconscious_knowledge_create`  | 创建知识条目                           |
| `subconscious_knowledge_update`  | 更新知识条目                           |
| `subconscious_knowledge_delete`  | 删除知识条目                           |
| `subconscious_knowledge_search`  | 语义搜索知识库                         |
| `subconscious_read_suggestions`  | 读取待审查的知识建议（读取后清空）     |
| `subconscious_read_sessions`     | 读取归档 sessions（LLM 摘要）          |
| `subconscious_get_profile`       | 读取用户画像（行滑动窗口）             |
| `subconscious_update_profile`    | 增量更新用户画像                       |

---

## 命令参考

### 用户命令 `/memory`

```text
/memory user list [页码]      分页列出个人记忆（每页 10 条）
/memory user search <关键词>  搜索个人记忆（top_k=10）
/memory user delete <ID>      删除个人记忆
/memory group list [页码]     分页列出群共享记忆
/memory group search <关键词> 搜索群共享记忆
/memory group delete <ID>     删除群共享记忆（仅管理员 / 群主）
/memory memo                  以合并转发查看当前作用域备忘录
/memory dmn                   立即触发一轮巩固循环
```

`memo` 只读：查看谁都有权限，写入走工具。群里 `/memory memo` 看的是群备忘录，
私聊看的是个人备忘录。

`dmn`（别名 `run`、`触发`）是隐藏选项：不进菜单与 usage 提示，非管理员调用
按未知选项处理。鉴权顺序为超级用户白名单，其次 `lp.admin` 权限节点。
还需要 `subconscious.enabled = true` 且已配好 `target_user_id`。

兼容旧格式 `/memory list` 等，默认以个人范围执行。群聊里 `user` 与 `group`
两个范围都可用，私聊只有 `user`。

### 运维命令 `ambot memory`

需要本地安装含该命令组的 ambot-inlinectl（见 [开发](#开发)）。

| 命令                           | 作用                                             |
| ------------------------------ | ------------------------------------------------ |
| `ambot memory status`          | 显示指纹、记忆分布、分区键版本、备份列表         |
| `ambot memory reindex [--yes]` | 强制全量重映射并写新指纹                         |
| `ambot memory migrate-keys [--dry-run]` | 迁移分区键到当前 Amrita 的 uni_id 格式   |
| `ambot memory reset-fingerprint` | 只写指纹、不重嵌入（逃生舱）                   |
| `ambot memory backup list`     | 列出备份                                         |
| `ambot memory backup create`   | 手动创建备份                                     |
| `ambot memory backup restore <file>` | 从备份恢复（用当前模型重新嵌入）           |

> 这些命令会先设置 `EMBED_CHECK_ON_STARTUP=false` 再加载插件 ——
> 避免启动检查在 CLI 中触发交互，保证命令**永远可用**（逃生舱）。

---

## 持久化与状态恢复

### 存储

| 存储             | 技术                                                         | 存什么                                                                                             |
| ---------------- | ------------------------------------------------------------ | -------------------------------------------------------------------------------------------------- |
| L1 备忘录        | `UserMemo` 表（插件自有 ORM，主键 = uni_id）                 | LLM 维护的常驻用户元信息（≤ `memo_max_chars`）                                                     |
| 循环元状态       | `SubconsciousState` 表（插件自有 ORM，uid=`amrita_memory`）  | `total_runs`、`last_abstracts`、`pending_messages`、`knowledge_suggestions`、`penalty`、`idle_planner`（热度图）|
| Session 摘要缓存 | `LRUCache[int, str]`（最大 128 条）                          | session DB id → LLM 生成的摘要文本，避免重复调用 `ContextCompactor`                                     |
| L2 记忆          | ChromaDB `amrita_user_memory`                                | 向量 + documents + metadatas（含嵌入指纹与分区键版本）                                             |
| 用户画像         | `data/amrita_plugin_memory/user_profile.md`                  | Markdown 文件，`summary---body` 格式，行级增量更新                                                 |
| 全局知识库       | `data/amrita_plugin_memory/knowledge/` + `knowledge_index.json` + ChromaDB | 三方同步管理                                                                        |
| Token 统计       | `InsightsModel`（复用 Bot ORM）                              | 全局 prompt/completion token 累加                                                                  |

### ORM 与迁移

使用 `nonebot_plugin_orm`，两条迁移：`21f55abc2b90` 建 `UserMemo`、
`6004d221a7de` 建 `SubconsciousState`。迁移随包分发，`depends_on` 指向
`nonebot_plugin_amrita` 的初始迁移，保证外键有效。


### 生命周期

1. 启动：`run_startup_check()`（Key 迁移 + 指纹校验）→ `_load_state()`
2. 初始化知识库（`enable_knowledge` 为假时跳过）→ 进入空闲
3. 用户发消息：`cancel_and_reschedule()`，记一次热度观测、惩罚 +1、重挑窗口
4. 窗口到达：`_run()` → `_run_cycle()` 跑一轮 ReAct
5. 成功则 `reset_penalty()`，失败保留惩罚（重试保护），回到空闲

**跨重启连续性**：`penalty` 与 `idle_planner` 一起落在 `SubconsciousState.payload`，
重启后接着用，不会把已经累积的退避和热度图丢掉。

**升级迁移**：旧版 penalty 语义是"被推迟的次数"，与新语义（要求的冷段长度）不可换算。
启用空闲规划时若检测到 payload 里没有 `idle_planner` 键，清零一次并打日志。
`payload` 只追加新键，旧版本读不到就忽略，不写迁移脚本。

---

## 技术栈

Python 3.11–3.13 / NoneBot2 / AmritaCore，向量库 ChromaDB，嵌入走 OpenAI 或 Ollama，
调度 nonebot_plugin_apscheduler，持久化 nonebot_plugin_orm，配置 Pydantic + uniconf。


---

## 配置详解

### `config/amrita_plugin_memory/config.toml`

```toml
# L2 记忆
per_session_memory_limit = 100
memo_max_chars = 1000

# L2 时间记忆退化（软衰减 + 分层清理）
[decay]
enabled = true
short_term_days = 7
long_term_days = 90
half_life_days = 30.0
importance_boost = 1.0
overfetch = 3
score_floor = 0.05
sweep_enabled = true
sweep_interval_hours = 24
sweep_batch_limit = 5000

# L3 离线巩固循环
[subconscious]
enabled = false
target_user_id = ""
allowed_tools = []
max_iterations = 10
loop_detect_threshold = 3
rethink_base_delay_minutes = 30
rethink_penalty_multiplier = 1.5
rethink_max_delay_minutes = 1440

# 空闲窗口调度（关闭后回退到上面的固定延迟方案）
use_idle_window_planner = true
idle_timezone = "Asia/Shanghai"
idle_target_hour = 3
idle_base_slots = 3
idle_max_slots = 6

prompt_file = "prompt/subconscious_main.md.jinja2"
prompt_send_file = "prompt/subconscious_send.md.jinja2"
prompt_knowledge_file = "prompt/knowledge_guide.md.jinja2"
prompt_profile_file = "prompt/profile_guide.md.jinja2"
enable_memory_compress = true
allow_send_to_user = false
memory_warn_threshold = 100
max_abstracts = 5
knowledge_max_chars = 10000
knowledge_collection_name = "amrita_global_knowledge"
enable_knowledge = true
```

| 字段                         | 默认值  | 说明                                        |
| ---------------------------- | ------- | ------------------------------------------- |
| `decay.enabled`              | `true`  | 是否启用时间记忆退化（软衰减 + 分层清理）   |
| `decay.short_term_days`      | `7`     | `low` 层寿命阈值（天）                      |
| `decay.long_term_days`       | `90`    | `high` 层寿命阈值（天），`medium` 取几何中值 |
| `decay.half_life_days`       | `30.0`  | 软衰减基础半衰期（天），`low` 为基准        |
| `decay.importance_boost`     | `1.0`   | `high` 半衰期为 `1+2·boost` 倍              |
| `decay.overfetch`            | `3`     | 检索候选放大倍数                            |
| `decay.score_floor`          | `0.05`  | 衰减因子下限，避免旧记忆权重归零            |
| `decay.sweep_enabled`        | `true`  | 是否启用分层清理                            |
| `decay.sweep_interval_hours` | `24`    | 分层清理周期（小时）                        |
| `decay.sweep_batch_limit`    | `5000`  | 单次清理最多扫描的记忆条数                  |
| `per_session_memory_limit`   | `100`   | 单个分区（uni_id）的记忆条数上限            |
| `memo_max_chars`             | `1000`  | L1 备忘录字数硬上限                         |
| `enabled`                    | `false` | 是否启用离线巩固循环（以下除 `decay.*` / `per_session_memory_limit` / `memo_max_chars` 外均在 `[subconscious]` 节） |
| `target_user_id`             | `""`    | 目标用户 ID，为空则不启动                   |
| `allowed_tools`              | `[]`    | 额外可用工具（从全局拉取，不存在仅告警）    |
| `max_iterations`             | `10`    | 单轮 ReAct 最大步数（预留）                 |
| `loop_detect_threshold`      | `3`     | 传入 Core `loop_reasoning_trigger`          |
| `rethink_base_delay_minutes` | `30`    | 旧方案的延迟基数（仅 `use_idle_window_planner=false` 时生效） |
| `rethink_penalty_multiplier` | `1.5`   | 旧方案的惩罚指数倍率                        |
| `rethink_max_delay_minutes`  | `1440`  | 旧方案的延迟上限（1 天）                    |
| `prompt_file`                | 见上    | 主推理提示词模板                            |
| `prompt_send_file`           | 见上    | 主动消息生成模板                            |
| `prompt_knowledge_file`      | 见上    | 知识库使用指南                              |
| `prompt_profile_file`        | 见上    | 画像构建指南                                |
| `enable_memory_compress`     | `true`  | 传入 Core `enable_memory_abstract`          |
| `allow_send_to_user`         | `false` | 允许 Agent 主动发私聊消息                   |
| `memory_warn_threshold`      | `100`   | ChromaDB 超此数量注入压缩提示               |
| `max_abstracts`              | `5`     | 摘要滑动窗口大小                            |
| `knowledge_max_chars`        | `10000` | 知识条目单条正文上限                        |
| `knowledge_collection_name`  | `amrita_global_knowledge` | ChromaDB 知识库 collection |
| `enable_knowledge`           | `true`  | 是否启用全局知识库（需 `enabled=true`）     |

空闲窗口调度另有十余个调参项（各层衰减常数、冷槽阈值、收缩强度、buffer 与预估耗时），
默认值见 `config.py` 的 `SubconsciousConfig`，一般不需要动；要动就照上面 toml 的写法加行。

### 提示词模板

4 个 Jinja2 模板位于 `config/amrita_plugin_memory/prompt/`，首次缺失时自动从默认值创建：

| 文件                          | 用途         | 变量                                                     |
| ----------------------------- | ------------ | -------------------------------------------------------- |
| `subconscious_main.md.jinja2` | 主推理提示词 | `character_prompt`, `current_date`, `target_user_id`     |
| `subconscious_send.md.jinja2` | 主动消息生成 | `character_prompt`, `intent`, `memory_context`, `current_time` |
| `knowledge_guide.md.jinja2`   | 知识库指南   | `current_date`, `target_user_id`                         |
| `profile_guide.md.jinja2`     | 画像构建指南 | `current_date`, `target_user_id`                         |

主推理模板只接收不变量：`current_date` 是**日粒度**（`YYYY-MM-DD`，UTC），
精确时间由 `subconscious_get_context` 提供。旧模板里写的 `{{ current_time }}`
在指南文件中会被渲染成空串，不会原样漏进 system prompt。

### 环境变量（`.env`）

| 变量                       | 类型                | 默认值                 | 说明                              |
| -------------------------- | ------------------- | ---------------------- | --------------------------------- |
| `VECTOR_DB_TYPE`           | local/remote        | `local`                | ChromaDB 类型                     |
| `VECTOR_DB_SERVER`         | string              | `127.0.0.1`            | 远程地址                          |
| `VECTOR_DB_PORT`           | int                 | `8000`                 | 远程端口                          |
| `VECTOR_DB_SERVER_SSL`     | bool                | `false`                | 远程 SSL                          |
| `VECTOR_DB_REMOTE_HEADERS` | dict                | `{}`                   | 远程请求头                        |
| `VECTOR_DB_TENANT`         | string              | `default`              | 租户                              |
| `VECTOR_DB_DATABASE`       | string              | `default`              | 数据库                            |
| `EMBEDDING_MODEL_URL`      | string              | `http://127.0.0.1:11434` | 嵌入模型地址                    |
| `EMBEDDING_MODEL_NAME`     | string              | `auto`                 | 模型名                            |
| `EMBEDDING_PROCTOL`        | openai/ollama-embed | `ollama-embed`         | 协议                              |
| `EMBEDDING_MODEL_API_KEY`  | string              | (空)                   | API Key                           |
| `EMBED_CHECK_ON_STARTUP`   | bool                | `true`                 | 启动时是否检查嵌入指纹            |
| `EMBED_MISMATCH_POLICY`    | ask/auto/never      | `ask`                  | 嵌入模型变更时的处理策略          |
| `REEMBED_BATCH_SIZE`       | int                 | `64`                   | 重映射每批条数                    |
| `REEMBED_BACKUP_KEEP`      | int                 | `3`                    | 保留备份份数                      |

---

## 项目结构

```
amrita_plugin_memory/
├── __init__.py                # 插件入口：启动检查、指纹失配的 startup 处理
├── config.py                  # 配置模型（SubconsciousConfig / ConfigFile / EnvConfig）
├── models.py                  # ORM 模型（UserMemo / SubconsciousState）
├── keys.py                    # L2 分区键（跟随框架 uni_id + 存量 Key 迁移）
├── embedding.py               # 嵌入指纹 / 全量重映射 / 备份 / 启动检查
├── cli.py                     # `ambot memory` 命令实现
├── memo.py                    # L1 备忘录（读取 / 写入 / 注入 hook）
├── tools.py                   # 表层工具（write/read/update/delete/list/memo/knowledge/profile）
├── permissions.py             # 会话侧知识库读取授权（LP 节点 / 超级用户）
├── profile_store.py           # 画像路径与 Markdown 解析（纯函数，不依赖运行时）
├── decay.py                   # 时间记忆退化：软衰减重排 + 分层清理判定
├── tasks.py                   # 分层清理的 APScheduler 周期任务
├── vector.py                  # ChromaDB 封装（AsyncUserMemory / 锁池）
├── embed.py                   # 嵌入适配器（Ollama）
├── matchers.py                # /memory 命令
├── migrations/                # ORM 迁移（随包分发）
│   ├── 21f55abc2b90_init.py
│   └── 6004d221a7de_state.py
└── rethinking/                # L3 离线巩固循环
    ├── _state.py              # 模块级状态（runner / penalty / pending / 隔离工具管理器）
    ├── types.py               # TypedDict 定义
    ├── consts.py              # 默认 Prompt 模板 / ensure_prompt_file
    ├── backend.py             # SubconsciousBackend — 隔离的工具 + memory 后端
    ├── runner.py              # SubconsciousRunner — 核心编排器
    ├── hooks.py               # on_precompletion hook / 生命周期管理
    ├── schedule.py            # 空闲窗口规划（纯数据 + 纯函数，唯一时区解析入口）
    ├── tools.py               # 21 个巩固循环工具 handler
    ├── schemas.py             # ToolFunctionSchema 定义
    └── knowledge.py           # KnowledgeBaseManager — 三方同步知识库

config/amrita_plugin_memory/
├── config.toml                # 插件配置
└── prompt/                    # Jinja2 提示词模板

data/amrita_plugin_memory/
├── vector_db.chroma/          # ChromaDB 持久化
├── backups/                   # 重映射前的 JSON 备份
├── knowledge/                 # 知识文件（KNOWLEDGE_*.md）
├── knowledge_index.json       # 知识索引
└── user_profile.md            # 用户画像
```

## 开发

```bash
uv sync                                              # 安装依赖
uv run ruff check amrita_plugin_memory               # 代码检查
uv run ruff format --check amrita_plugin_memory      # 格式检查
uv run pyright --pythonpath .venv/bin/python amrita_plugin_memory   # 类型检查
```

仓库 `.venv` 里通常没有 pyright / ruff，所以要用 `--pythonpath` 指向仓库环境，
否则会因解析不到 amrita_core / chromadb 报一片假的 Import 错误。

约定与模块边界见 `AGENTS.md`。本仓库**不写单元测试**：验证以 ruff、pyright
零 error 加手动冒烟为准，纯逻辑写成可独立执行的形式，用临时脚本验证后不提交。

### ORM 迁移

用 **`ambot orm`** 生成迁移，不要用 `nb orm`（它不加载 Amrita 插件，我们的模型会被漏掉）：

```bash
uv run ambot orm revision -m "描述" --branch-label amrita_plugin_memory
uv run ambot orm upgrade && uv run ambot orm check
```

脚本落在工作区 `migrations/versions/amrita_plugin_memory/`，
要手动移进包内 `amrita_plugin_memory/migrations/` 才随包分发。

### `ambot memory` 命令

命令组在 `cli.py`，靠 entry point 暴露：

```toml
[project.entry-points."ambot.commands"]
memory = "amrita_plugin_memory.cli:memory [full_load]"
```

本插件包在加载器之外无法 import，所以 `ambot` 只在**命令真正被调用时**才
`amrita.init()` + `load_plugins()`；`--help` 阶段仅读元信息，不触发加载。
自举时会写 `AMBOT_COMMAND_CONTEXT=1`，插件据此跳过启动检查，
否则指纹确认会反过来挡住维护命令本身。

调试时装可编辑版本（`uv run` 会按 `uv.lock` 重同步并冲掉它，故加 `--no-sync`）：

```bash
uv pip install -e <ambot-inlinectl 仓库路径>
uv run --no-sync ambot memory status
```
