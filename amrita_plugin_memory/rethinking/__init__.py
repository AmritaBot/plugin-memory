"""潜意识推理循环（rethinking 子包）。

导入此包时触发工具注册和 hook 注册。
注意：避免模块级导入 tools/runner 等会触发循环导入的子模块。
这些模块的副作用（@on_tools 注册、hook 注册）通过 hooks 和 tools 内部的延迟导入自动生效。
"""

from . import _state, backend, hooks, nodes, schemas, workflow

__all__ = [
    "_state",
    "backend",
    "consts",
    "hooks",
    "nodes",
    "runner",
    "schemas",
    "tools",
    "workflow",
]
