from nonebot import require

require("nonebot_plugin_localstore")
require("nonebot_plugin_apscheduler")
require("amrita.plugins.chat")
require("amrita.plugins.menu")
from . import config, embed, matchers, rethinking, tools, vector

__all__ = ["config", "embed", "matchers", "rethinking", "tools", "vector"]
