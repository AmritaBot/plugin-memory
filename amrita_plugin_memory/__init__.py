from nonebot import require

require("nonebot_plugin_localstore")
require("nonebot_plugin_apscheduler")
require("nonebot_plugin_amrita")
require("nonebot_plugin_orm")
require("amrita.plugins.chat")
require("amrita.plugins.menu")
from . import (
    config,
    embed,
    embedding,
    keys,
    matchers,
    memo,
    models,
    rethinking,
    tools,
    vector,
)

# 启动检查（import 期，同步）：Key 迁移 + 嵌入模型指纹校验。内部已做防御性处理：只有“需要确认但环境不可交互”会抛出异常，从而拒绝加载插件（刻意的失败快）；其余异常只告警。
embedding.run_startup_check()

__all__ = [
    "config",
    "embed",
    "embedding",
    "keys",
    "matchers",
    "memo",
    "models",
    "rethinking",
    "tools",
    "vector",
]
