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

前置依赖：Python 3.11+ / AmritaBot 实例 / **Ollama 或 OpenAI 嵌入服务** / ChromaDB。

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

数据流向：LLM 用 `update_memo` 整篇改写 L1；L1 临近上限时把细节下放到 L2；
L3 在空闲窗口里读 L2 与归档 session，做去重与精炼，顺带维护知识库和画像。
L3 的元状态（含惩罚计数与热度图）单独存在 `SubconsciousState`。

### L1 与 `extra_prompt` 的正交性

两者都会注入 system prompt，但来源、作者与优先级完全不同：

|          | `extra_prompt`                     | L1 备忘录                          |
| -------- | ---------------------------------- | ---------------------------------- |
| 作者     | **用户**逐字指定（`/prompt` 命令） | **LLM** 规范化提取                 |
| 管理方式 | 用户自己增删改                     | LLM 自主维护                       |
| 注入形态 | `<EXTRA>` 规则块，**冲突时忽略**   | `<MEMO>` 事实块，作为可信背景      |
| 语义     | 用户想要的行为指令                 | LLM 学到的关于该主体的元信息       |

插件**不会**读写 `extra_prompt` —— 那是用户的领地。

### 命名对照表

L3 在代码中沿用历史标识符 `subconscious` / `rethinking`（避免破坏性变更），
文档统一表述为"离线巩固循环"，其行为与神经科学的**默认模式网络（DMN）**高度对应：

| 代码标识符                          | DMN 对应特征                |
| ----------------------------------- | --------------------------- |
| `rethinking/` / `subconscious_*`     | 离线巩固循环、巩固循环工具  |
| 用户聊天 → `cancel_and_reschedule`   | 任务负激活：专注时 DMN 被抑制 |
| 空闲窗口到达 → `_run`                | DMN 在无任务时活跃          |
| 去重 / `ContextCompactor`            | 海马 → 皮层的系统巩固       |
| 画像构建（`*_profile`）              | 自传体记忆                  |
| 主动消息（`send_to_user`）           | 心智游移                    |

> 它**不是**弗洛伊德式的"潜意识"：触发确定性、工具显式、日志完整、输入输出全透明。
> 它也不是"常驻循环"——而是**事件驱动的单次任务**，跑完即空闲；用户聊天越频繁，它跑得越少。

---

## L1 备忘录

**定位**：每次对话都要用到的恒常信息 —— 身份（名字 / 证件 / 电话）、稳定偏好、
**用户要求的工具调用模式**。类比人类恒常激活的持久记忆。

### 关键约束：严格字数上限

`memo_max_chars`（默认 **1000** 字）是**硬上限**。因此主力写入原语是**整篇替换**：
`append_memo` 只是省事用的追加入口，最终仍受同一个字数校验约束。

`update_memo` 的语义是：

```text
LLM 读当前 memo → 融合新事实 → 输出新全文 → 服务端校验长度 → 落库
```

超长时工具**拒绝写入**并返回当前字数，由 LLM 自行压缩后重试。

### 内容规范

| 该放 L1                     | 该放 L2                         |
| --------------------------- | ------------------------------- |
| 身份信息、稳定偏好          | 具体事件、长尾偏好              |
| 用户要求的工具调用模式      | 项目细节、讨论记录              |
| 当前活跃上下文（高度浓缩）  | 任何不需要每轮都看到的内容      |

写入指南（写进 L3 的提示词）：

- 只写**陈述性事实**，不写祈使句规则（规则属于 `extra_prompt`）
- 内部按 `身份 > 偏好 > 工具模式 > 近期上下文` 排序，压缩时从尾部开始丢
- 临近上限时，细节应**下放 L2**，memo 只留指针（如"用户有 3 个项目"）

### 存储与注入

- **表**：`amrita_plugin_memory_user_memo`，主键 `user_id` = 框架 uni_id
- **群与个人是同一张表的不同行**：
  - `QQPlatform_Private_{qq}` / `user_{qq}` → 个人备忘录
  - `QQPlatform_Group_{群号}` / `group_{群号}` → 群备忘录
- **注入**：`on_precompletion` hook（priority=15），与 L3 **完全解耦**（L3 关闭时 L1 照常工作）
  - 私聊 → 注入个人 memo
  - **群聊 → 只注入群 memo**（发言人的个人 memo 不注入，避免隐私外泄）
  - 空 memo 不注入，行为自然降级
- **缓存**：进程内 `LRUCache(128)`，写入时穿透失效

### 工具

| 工具          | 说明                                       |
| ------------- | ------------------------------------------ |
| `read_memo`   | 读取当前作用域备忘录全文                   |
| `append_memo` | 在末尾追加一段，不覆盖已有条目             |
| `update_memo` | 整篇替换，超长则拒绝，返回当前字数         |

---

## L2 向量记忆层

**定位**：长尾事实库 —— 只在需要时检索，不常驻。类比需要线索提取的语义记忆。

### 分区键（uni_id）

分区键**跟随已安装 Amrita 的会话 ID 格式**（`amrita.plugins.chat.utils.sql.make_uni_id`）：

旧版是 `user_{qq}` / `group_{群号}`，新版换成
`QQPlatform_Private_{qq}` / `QQPlatform_Group_{群号}`。插件不硬编码任何一种，
而是委托框架的 `make_uni_id` 生成，Amrita 升级后自动跟随；存量 Key 由启动检查幂等迁移。

`scope` 参数表达语义，与 uni_id 类型是两个维度：

| `scope` | 含义                       | 可用场景   |
| ------- | -------------------------- | ---------- |
| `user`  | 个人专属（群聊私聊互通）   | 任意       |
| `group` | 群共享（群内所有人可见）   | 仅群聊     |

### 记忆元数据

每条记忆带 `user_id` / `scope` / `tags` / `importance` / `created_at`。
`importance` 分 `low` / `medium` / `high`，可在检索时作为过滤条件。
检索时额外回填 `decay_score`（时间退化得分），不参与过滤。

### 检索

`read_memory` 用嵌入向量做相似度搜索，支持 `top_k` 与 `importance` 过滤。
删除群共享记忆需管理员或群主权限。

检索结果附带**距离**（`distances`）——ChromaDB 默认使用 `l2`（平方欧氏距离）空间，
因此**数值越小越相似**（完全相同的向量为 `0`）。`/memory user search` 输出中的
"距离"即此值。

> 距离是原始度量而非归一化相似度：`l2` 空间下其取值范围取决于向量是否归一化，
> 因此不要跨模型比较绝对值，只用于同一查询内的相对排序。

### 时间记忆退化（软衰减 + 分层清理）

记忆不是永久等权 —— 越旧权重越低，超龄则逐级退化直至清理。分两段实现：

**软衰减（检索期）**：`query_notes` 先多取 `overfetch` 倍候选，再按

$$\text{score} = \frac{1}{1 + d} \times \max\left(0.5^{\,\text{age} / \text{half-life}},\ \text{floor}\right)$$

重排后截回 `top_k`。半衰期随 `importance` 放大：`low` 为 `half_life_days`，
`medium` 为 `1+boost` 倍，`high` 为 `1+2·boost` 倍。得分回填到元数据的
`decay_score` 字段，便于排查。这一步**只重排、不删数据**。

**硬分层（清理期）**：每条记忆按 `importance` 有一条寿命阈值 ——
`low` = `short_term_days`、`high` = `long_term_days`、`medium` 取两者几何中值。
后台任务按 `sweep_interval_hours` 扫描，超龄先 `high → medium → low` 逐级降级，
降到 `low` 仍超龄才删除。降级只改 `importance`，不重新嵌入。

| 动作     | 触发条件           | 效果                |
| -------- | ------------------ | ------------------- |
| `keep`   | `age ≤ 当前层阈值` | 不动                |
| `demote` | 超龄且非 `low`     | `importance` 降一级 |
| `purge`  | 超龄且已是 `low`   | 删除该条            |

`created_at` 缺失或不可解析时按 `keep` 处理，避免误删。
软衰减与分层清理都受 `[decay].enabled` 总开关控制。

---

## L3 离线巩固循环（DMN）

### 触发：事件驱动 + 空闲窗口规划

**不使用定时自循环** —— 只有目标用户发消息才会推一次计划。

用户每次聊天 → 取消当前计划 → 惩罚计数 +1 → 按新的门槛重新挑窗口。
推理成功后惩罚重置为 0；用户聊得越频繁，要求的冷段越长（**任务负激活**）。

默认走空闲窗口规划（`use_idle_window_planner = true`），由 `rethinking/schedule.py`
算出一个具体的本地时间窗：

- **热度图**：7 天 × 12 槽（2h），消息给所在槽 +1，全局层 τ=14 天、单片星期几层 τ=28 天
- **分层**：fine（单片星期几）/ mid（工作日-周末）/ coarse（全局），按活跃天数逐级降级
- **冷槽**：占比 ≤ 5% 且原始计数 ≤ 1；冷段环形扫描，跨零点的 22:00–08:00 不被切断
- **细化**：段内回到原始时间戳，合并连发噪声后二分求上界，扣 buffer 与预估耗时
- **penalty 语义**：是"要求多长的冷段"，不是"推迟多少分钟" —— `3 槽 × 1.5^(n-1)`
  封顶 6 槽，不足就减半放宽三次，仍不足取最长冷段，再不行退到热度最低的连续段
- **冷启动**：没有历史时落在 `idle_target_hour`（默认 03:00）的固定窗口

关掉 `use_idle_window_planner` 会回退成旧方案：
`delay = min(base × multiplier^(penalty-1), cap)`，默认 30min / 1.5 / 1440min。

### 每轮做什么

| 阶段     | 内容                                                   |
| -------- | ------------------------------------------------------ |
| 取上下文 | Agent 第一步必须调 `subconscious_get_context`          |
| 记忆限幅 | Core `ContextCompactor` 截断超限消息并生成摘要         |
| ReAct    | Agent 调用 `subconscious_*` 工具执行整理               |
| 后处理   | 更新全局 usage、持久化元状态、重置惩罚、规划下次窗口   |

system prompt 只放不变量（角色、规则、质量口径、静态指南）。精确时间、距上轮间隔、
上轮摘要、待审建议数、记忆库规模这些会变的东西全部通过 `subconscious_get_context`
按需拉取 —— 这样同一天内渲染出的 system 逐字节一致，能吃到 provider 的前缀缓存，
也不会让模型拿一个循环开头算出的快照当决策依据。

Workflow 直接复用 Core 的 `REACT_ONLY`，不自建管线：`COMPACT_HISTORY` 节点在
Agent Loop 前跑 `ContextCompactor`，压缩排在渲染之前，摘要才进得来。
`SubconsciousRunner` 只是把 ChatObject 当数据容器，注入隔离的
`SubconsciousBackend` 与 `ReActAgentStrategy`。

### 整理职责

- **去重合并**：`subconscious_duplicate_helper` 返回待整理记忆 + 合并指导
- **重要性校准 / 标签管理**：统一 `low` / `medium` / `high` 与 tags 口径
- **画像精炼**：`*_profile` 行级增量维护用户画像
- **知识沉淀**：把可迁移的经验写入全局知识库
- **备忘录压缩**（L1 联动）：临近上限时把细节下放 L2

---

## 嵌入模型与重映射

### 为什么需要指纹

向量只有在**同一嵌入模型**下才可比。更换模型（`nomic-embed-text` → `bge-m3`）
甚至只是服务端同名模型被更新，旧向量都会退化为噪声 —— 但 ChromaDB 不会报错，
检索结果会静默劣化。

因此插件在集合 metadata 中记录**嵌入配置指纹**：

```
fingerprint = sha256(protocol | model | base_url)
```

随库走（远程 Chroma 同样生效），并在**每次启动**校验。

### 启动检查流程

检查在**插件 import 期**（同步）执行：

1. `EMBED_CHECK_ON_STARTUP=false` → 跳过；否则先做一次幂等的 Key 迁移
2. 集合为空 → 直接写入指纹，零打扰
3. 指纹一致 → 什么都不做
4. 指纹不一致 → 按 `EMBED_MISMATCH_POLICY`：
   - `never`：告警，保持现有数据
   - `auto`：备份 JSON → 删集合 → 重建 → 分批重嵌入 → 写新指纹
   - `ask`：stdin 是 TTY 才交互确认；非交互环境拒绝加载插件，Bot 照常运行

**重映射对维度变化天然免疫** —— 走"删集合 → 重建"路径，因此无需探测向量维度。

**失败中断**：保留半成品集合 + 备份 + **不写新指纹**，下次启动重新提示（幂等重试）。

相关变量见下方[环境变量](#环境变量env)。`ask` 策略在非交互环境（systemd / Docker）
会拒绝加载插件，这类部署建议显式设 `EMBED_MISMATCH_POLICY`。

### 已知限制与备份

模型名不变但服务端实际模型被换（ollama 同名 tag 更新）无法检测 —— 指纹只看配置，
这种情况用 `ambot memory reindex` 手动强制重映射。

重映射前会把集合全量内容（ids + documents + metadatas）落盘到
`backups/embed_backup_<时间戳>.json`，这是"删集合重建"路径唯一的回滚手段。
备份不含向量，恢复时用**当前**模型重新嵌入 —— 这正是恢复场景需要的。

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

插件使用 `nonebot_plugin_orm`，自带迁移链：

| revision         | 内容                          |
| ---------------- | ----------------------------- |
| `21f55abc2b90`   | `UserMemo` 表（分支根，`depends_on=072361e8936f`） |
| `6004d221a7de`   | `SubconsciousState` 表        |

迁移随包分发（`amrita_plugin_memory/migrations/`），并**依赖 Bot 的迁移链**
（`depends_on` 指向 `nonebot_plugin_amrita` 的初始迁移），保证外键引用有效。

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

| 组件       | 技术                                                        |
| ---------- | ----------------------------------------------------------- |
| 运行时     | Python 3.11+ / NoneBot2 / AmritaCore                        |
| 向量库     | ChromaDB（PersistentClient 或 HttpClient）                  |
| 嵌入       | OpenAI Embedding / Ollama Embedding                         |
| 调度       | nonebot_plugin_apscheduler（单次 date trigger + 周期 sweep） |
| 持久化     | nonebot_plugin_orm（插件自有模型 + 迁移链）                 |
| 配置       | Pydantic + uniconf（TOML）/ pydantic-settings（`.env`）      |
| 质量       | Ruff + Pyright                                              |

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
