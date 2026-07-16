"""模块级共享状态管理。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from amrita.plugins.chat.config import config_manager
from amrita_core import ModelPreset
from amrita_core.tools.manager import MultiToolsManager

if TYPE_CHECKING:
    from .runner import SubconsciousRunner

_runner: SubconsciousRunner | None = None
_pending_messages: list[dict[str, Any]] = []
# 隔离的工具管理器 — 所有 @on_tools 装饰器通过 bound_to 注册到这里，不污染全局 ToolsManager
_SUBCONSCIOUS_TOOLS = MultiToolsManager()


async def get_preset() -> ModelPreset:
    return await config_manager.get_preset(
        (await config_manager.safe_get_config()).preset
    )


def get_runner() -> SubconsciousRunner | None:
    return _runner


def set_runner(r: SubconsciousRunner | None) -> None:
    global _runner
    _runner = r


def get_target_user_id() -> str:
    return _runner._config.target_user_id if _runner is not None else ""


def get_pending() -> list[dict[str, Any]]:
    return _pending_messages


def set_pending(msgs: list[dict[str, Any]]) -> None:
    global _pending_messages
    _pending_messages = msgs


def get_tools_manager() -> MultiToolsManager:
    return _SUBCONSCIOUS_TOOLS
