# Configuration for your_plugin_name plugin
from amrita.config_manager import BaseDataManager
from nonebot import get_plugin_config
from pydantic import BaseModel


class ConfigFile(BaseModel):
    ...
    # Add your file configuration here

class DataManager(BaseDataManager):
    # This is a sample data manager
    config: ConfigFile
class Config(BaseModel):
    ...
    # Add your configuration here


# Get your config by using `get_plugin_config(Config)`

CONFIG = get_plugin_config(Config)
