"""模块级共享状态管理。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .runner import SubconsciousRunner

_runner: SubconsciousRunner | None = None
_pending_messages: list[dict[str, Any]] = []


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
