"""自定义 Backend — 为潜意识循环提供隔离的工具和内存池。"""

from __future__ import annotations

from typing import Any, ClassVar

from amrita_core.base.backend import AbilityBackend, MemoryBackend
from amrita_core.contexts import AbilityContext
from amrita_core.preset import MultiPresetManager
from amrita_core.tools.manager import MultiToolsManager, ToolsManager
from amrita_core.tools.mcp import ClientManager, MultiClientManager
from amrita_core.types import MemoryModel
from nonebot import logger

from ..config import SubconsciousConfig
from . import _state


def _get_global_tools() -> ToolsManager:
    return ToolsManager()


class SubconsciousBackend(AbilityBackend, MemoryBackend):
    """为潜意识循环提供隔离的工具和内存池。"""

    _instance: ClassVar[SubconsciousBackend | None] = None
    _tools_manager: MultiToolsManager
    _presets: MultiPresetManager
    _memory: MemoryModel

    def __new__(cls, *args: Any, **kwargs: Any):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self, config: SubconsciousConfig):
        if not hasattr(self, "_tools_manager"):
            self._config = config
            self._presets = MultiPresetManager()
            self._memory = MemoryModel()
            self._register_tools()

    def _register_tools(self) -> None:
        # 所有工具已通过 @on_tools(bound_to=_state.get_tools_manager()) 注册到隔离的 MultiToolsManager
        # 这里只需拉取它们的 ToolData 用于 debug 校验
        self._tools_manager = _state.get_tools_manager()
        # allowed_tools 从全局 ToolsManager 拉取额外工具
        gm = _get_global_tools()
        for tool_name in self._config.allowed_tools:
            if (td := gm.get_tool(tool_name)) is not None:
                self._tools_manager.register_tool(td)
            else:
                logger.warning(f"[Subconscious] Tool '{tool_name}' not found")

    async def load_ability_all(self, session_id: str) -> AbilityContext:
        return AbilityContext(
            tools=self._tools_manager, presets=self._presets, mcp=ClientManager()
        )

    async def load_mcp_clients(self, session_id: str) -> MultiClientManager:
        return ClientManager()

    async def load_tools(self, session_id: str) -> MultiToolsManager:
        return self._tools_manager

    async def load_presets(self, session_id: str) -> MultiPresetManager:
        return self._presets

    async def load_memory(self, session_id: str) -> MemoryModel:
        return MemoryModel(messages=[], abstract="")

    async def commit_memory(self, session_id: str, memory: MemoryModel) -> None:
        self._memory = memory
