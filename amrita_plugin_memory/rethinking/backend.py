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
from .schemas import _SUBCONSCIOUS_TOOL_FUNCTIONS


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
            self._tools_manager = MultiToolsManager()
            self._presets = MultiPresetManager()
            self._memory = MemoryModel()
            self._register_tools()

    def _register_tools(self) -> None:
        tm = ToolsManager()
        for schema in _SUBCONSCIOUS_TOOL_FUNCTIONS:
            if (td := tm.get_tool(schema.function.name)) is not None:
                self._tools_manager.register_tool(td)
        if self._config.allow_send_to_user:
            if (td := tm.get_tool("subconscious_send_to_user")) is not None:
                self._tools_manager.register_tool(td)
        if (td := tm.get_tool("subconscious_read_chat_context")) is not None:
            self._tools_manager.register_tool(td)
        if (td := tm.get_tool("subconscious_duplicate_helper")) is not None:
            self._tools_manager.register_tool(td)
        if (td := tm.get_tool("subconscious_get_memory_stats")) is not None:
            self._tools_manager.register_tool(td)
        for tool_name in self._config.allowed_tools:
            if (td := tm.get_tool(tool_name)) is not None:
                self._tools_manager.register_tool(td)
            else:
                logger.warning(f"[EXP Subconscious] Tool '{tool_name}' not found")

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
