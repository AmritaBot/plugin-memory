from nonebot import require

require("nonebot_plugin_localstore")
require("amrita.plugins.chat")
require("amrita.plugins.menu")
from . import config, embed, matchers, tools, vector

__all__ = ["config", "embed", "matchers", "tools", "vector"]
