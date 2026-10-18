from nonebot import get_driver, require
from nonebot.plugin import PluginMetadata

require("nonebot_plugin_localstore")
require("nonebot_plugin_apscheduler")
require("nonebot_plugin_amrita")
require("nonebot_plugin_orm")
require("amrita.plugins.chat")
require("amrita.plugins.menu")

__plugin_meta__ = PluginMetadata(
    name="Amrita长期记忆",
    description="长期记忆、备忘录与潜意识调度",
    usage="/memory (list|search|delete|memo|dmn) [参数]",
    homepage="https://github.com/AmritaBot/plugin-memory",
    type="application",
    supported_adapters={"~onebot.v11"},
)

from . import (
    config,
    embed,
    embedding,
    keys,
    matchers,
    memo,
    models,
    rethinking,
    tasks,
    tools,
    vector,
)

# import 期只做 Key 迁移与指纹比对（不重嵌入）：call_embedding 依赖的全局 AmritaConfig 此时尚未设置。
embedding.run_startup_check()


@get_driver().on_startup
async def _memory_deferred_fingerprint_check() -> None:
    """startup 期处理 import 期发现的指纹失配（此时 AmritaConfig 已就绪）。"""
    await embedding.run_deferred_check()


__all__ = [
    "config",
    "embed",
    "embedding",
    "keys",
    "matchers",
    "memo",
    "models",
    "rethinking",
    "tasks",
    "tools",
    "vector",
]
