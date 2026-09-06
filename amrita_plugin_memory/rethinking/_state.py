"""模块级共享状态管理。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from amrita.plugins.chat.config import config_manager
from amrita_core import ModelPreset
from amrita_core.tools.manager import MultiToolsManager

if TYPE_CHECKING:
    from .runner import SubconsciousRunner
    from .types import PendingMsg

_runner: SubconsciousRunner | None = None
_pending_messages: list[PendingMsg] = []
# 惩罚计数器 — 每次用户聊天取消计划时 +1，整理成功后重置
_penalty_count: int = 0
# 知识建议队列 — 对话 LLM 提议创建/更新知识，潜意识 Agent 下一轮审查
_knowledge_suggestions: list[dict[str, str]] = []
# 隔离的工具管理器 — 所有 @on_tools 装饰器通过 bound_to 注册到这里，不污染全局 ToolsManager
_SUBCONSCIOUS_TOOLS = MultiToolsManager()


async def get_preset() -> ModelPreset:
    preset = await config_manager.get_preset(
        (await config_manager.safe_get_config()).preset, fix=True
    )
    if preset is None:
        raise RuntimeError("No model preset available")
    return preset


def get_runner() -> SubconsciousRunner | None:
    return _runner


def set_runner(r: SubconsciousRunner | None) -> None:
    global _runner
    _runner = r


def get_target_user_id() -> str:
    return _runner._config.target_user_id if _runner is not None else ""


def get_pending() -> list[PendingMsg]:
    return _pending_messages


def set_pending(msgs: list[PendingMsg]) -> None:
    global _pending_messages
    _pending_messages = msgs


def get_tools_manager() -> MultiToolsManager:
    return _SUBCONSCIOUS_TOOLS


def get_penalty_count() -> int:
    return _penalty_count


def set_penalty_count(n: int) -> None:
    global _penalty_count
    _penalty_count = n


def increment_penalty() -> int:
    global _penalty_count
    _penalty_count += 1
    return _penalty_count


def reset_penalty() -> None:
    global _penalty_count
    _penalty_count = 0


def get_knowledge_suggestions() -> list[dict[str, str]]:
    return _knowledge_suggestions


def set_knowledge_suggestions(suggestions: list[dict[str, str]]) -> None:
    global _knowledge_suggestions
    _knowledge_suggestions = suggestions


def add_knowledge_suggestion(suggestion: dict[str, str]) -> None:
    global _knowledge_suggestions
    _knowledge_suggestions.append(suggestion)
