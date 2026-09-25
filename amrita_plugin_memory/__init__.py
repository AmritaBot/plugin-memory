from nonebot import require

require("nonebot_plugin_localstore")
require("nonebot_plugin_apscheduler")
require("nonebot_plugin_amrita")
require("nonebot_plugin_orm")
require("amrita.plugins.chat")
require("amrita.plugins.menu")
from . import config, embed, matchers, memo, models, rethinking, tools, vector

__all__ = [
    "config",
    "embed",
    "matchers",
    "memo",
    "models",
    "rethinking",
    "tools",
    "vector",
]
