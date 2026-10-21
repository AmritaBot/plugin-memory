# AGENTS.md

给在本仓库工作的 AI 代理（以及新加入的人）看的约定。只写与改动直接相关的硬性要求，风格问题按现有代码对齐。

## 项目概况

- AmritaBot 长期记忆插件：L1 备忘录、L2 向量记忆、L3 离线巩固循环（DMN）三层。
- 表层工具（对话 LLM 可用）注册在全局 ToolsManager；`subconscious_*` 通过 `bound_to=_SUBCONSCIOUS_TOOLS` 注册到隔离管理器，不污染全局。
- 插件入口 `amrita_plugin_memory/__init__.py`，命令入口 `cli.py`（`ambot memory`），对话命令在 `matchers.py`（`/memory`）。

## 模块边界

| 位置 | 职责 |
| --- | --- |
| `config.py` | EnvConfig / ConfigFile（pydantic + uniconf），DATA_PATH 等路径常量 |
| `keys.py` | uni_id 生成与 scope 解析 |
| `vector.py` | ChromaDB 封装、AsyncUserMemory 记忆读写 |
| `embedding.py` / `embed.py` | 嵌入模型与指纹校验、重映射 |
| `memo.py` | L1 备忘录读写 |
| `decay.py` | 时间记忆退化与分层清理 |
| `permissions.py` | 会话侧读取权限校验 |
| `profile_store.py` | 画像路径与 Markdown 解析（纯函数，不依赖 nonebot 运行时） |
| `rethinking/` | DMN 循环：runner、tools、schemas、knowledge、schedule |
| `rethinking/schedule.py` | 空闲窗口规划（纯数据 + 纯函数，`resolve_timezone` 是全局唯一的时区解析入口） |

新增纯逻辑优先放进不依赖 nonebot 的模块（如 `profile_store.py`、`rethinking/schedule.py`），运行器只做编排。

## 环境与依赖

- Python 3.10–3.13，包管理只用 uv。改依赖后跑 `uv sync`，不要用 `pip install` 补装单包。
- 类型检查用 `pyright`，配置在 `pyproject.toml` 的 `[tool.pyright]`（standard 模式）。
- 本仓库 `.venv` 里通常没有 pyright / ruff，用 `--pythonpath .venv/bin/python` 指向仓库环境，否则会因解析不到 amrita_core / chromadb 报一片 Import 错误。

## 常用命令

```bash
uv run ruff check amrita_plugin_memory
uv run ruff format --check amrita_plugin_memory
uv run pyright --pythonpath .venv/bin/python amrita_plugin_memory
python -m py_compile amrita_plugin_memory/**/*.py
```

## 测试要求

本仓库不补单元测试，验证以静态检查加手动冒烟为准：

1. `ruff check` 与 `ruff format --check` 通过
2. `pyright` 零 error（用上面的 pythonpath 跑）
3. 改到的模块 `py_compile` 通过
4. 手动跑一遍改动的命令（`/memory`、`ambot memory`）或工具调用，并在 PR 里说明验证方式与结果

纯逻辑尽量写成可独立执行的形式，方便用 `python -c` 或临时脚本验证；不要把临时脚本提交进仓库。
本仓库无 `tests/` 目录，也不要新建。纯逻辑写成可独立执行的形式，用临时脚本验证后不提交。

## 工具注册的三条路径

- 表层可读、需权限：实现放 `rethinking/tools.py`，`@on_tools(SCHEMA, strict=True, custom_run=True)` 注册到全局，进函数先校验 `permissions.py` 里的判定。
- 仅循环可用：同上但带 `bound_to=_SUBCONSCIOUS_TOOLS`。
- 新工具要同时加入 `rethinking/schemas.py` 的 `_SUBCONSCIOUS_TOOL_FUNCTIONS`（若循环也要用），并在 README 的工具表里登记。

新增工具时注意：schema 的 `name` 是工具名，函数名可另取；`custom_run=True` 时函数签名收 `ToolContext`，返回值必须是字符串。

## 权限口径

- 知识库只读：`permissions.can_read_knowledge`，超用户 / `lp.admin` / `memory.knowledge` 节点三者之一。
- 用户画像只读：`permissions.can_read_profile`，仅 `subconscious.target_user_id` 本人，不扩散权限节点。
- 取值优先用 `_state.get_target_user_id()`（运行器实际生效值），回退 `DataManager` 配置。空值一律拒。

## 数据与持久化

- 画像：`DATA_PATH/user_profile.md`，格式 `summary` + `---` + 正文；行级增量更新，写入只发生在循环内部。
- 知识库：`DATA_PATH/knowledge/` + `knowledge_index.json` + ChromaDB，文件/索引/向量三方同步。
- DMN 元状态：`SubconsciousState` 表；向量记忆在 ChromaDB collection。
- 写入路径优先走读-改-写整体替换，避免嵌入失败时丢数据（参考 `25f1078`）。

## 代码注释风格

- 注释与 docstring 一律中文，单行短句，讲“为什么”和约束，不复述代码本身在做什么。
- 模块顶部 docstring 写职责、关键约束、与其他模块的边界；函数 docstring 写清参数语义与副作用。
- 不写分节注释（`# ---- 小节名`、`# ====`），用空行和函数拆分表达结构。
- 不写连续多行 `#` 注释；需要成段解释时写进模块或函数 docstring。
- 待办写成 `#  TODO(review #<issue>)：具体动作`。
- 类型注解必须齐全；pydantic 字段用 `Field(description=...)`，描述用中文。
- 不写注释掉的旧代码，不写“新增/修改”这类只在 diff 里有意义的注释。

## 配置改动

- 配置型在 `config.py`（pydantic + `nonebot_plugin_uniconf`）。新字段必须有中文 `description` 和保守默认值，默认值要保证旧配置文件行为不变。
- 新增开关涉及 DMN 行为时，同步检查 `matchers.py` 的 `/memory dmn` 前置校验与 README 的配置表。

## Git 与 PR

- 分支名用 `feat/` `fix/` `refactor/` 前缀；提交信息中文，首行说明改了什么，正文写动机与影响面。
- PR 拿到 approval 不等于授权合并；合并默认 squash，等明确指令再动。
- GitHub raw 域名可能不可达，取仓库内容走 `api.github.com`。
