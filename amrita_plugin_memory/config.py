# Configuration for your_plugin_name plugin
from typing import Literal

from amrita_core import ModelPreset
from nonebot import get_driver, get_plugin_config
from nonebot_plugin_localstore import get_plugin_data_dir
from nonebot_plugin_uniconf import BaseDataManager
from pydantic import BaseModel, Field

DATA_PATH = get_plugin_data_dir().resolve()
DATA_PATH.mkdir(parents=True, exist_ok=True)
VECTOR_DB_PATH = DATA_PATH / "vector_db.chroma"
PLUGIN_IM = "amrita_plugin_memory"


class ConfigFile(BaseModel):
    """配置文件"""

    short_term_expiry_days: int = Field(
        default=3, description="短期记忆的过期天数，默认为3天"
    )
    long_term_expiry_days: int = Field(
        default=30, description="长期记忆的过期天数，默认为30天"
    )
    permanent_expiry_days: int = Field(
        default=365, description="永久记忆的过期天数，默认为1年"
    )
    per_session_memory_limit: int = Field(
        default=50, description="每个会话的记忆数量限制，默认为50条"
    )


class EnvConfig(BaseModel):
    vector_db_type: Literal["local", "remote"] = Field(
        default="local", description="ChromaDB向量数据库类型"
    )
    vector_db_server: str = Field(
        default="127.0.0.1",
        description="ChromaDB向量数据库地址(当且仅当vector_db_type为remote时生效)",
    )
    vector_db_port: int = Field(
        default=8000,
        description="ChromaDB向量数据库端口(当且仅当vector_db_type为remote时生效)",
    )
    vector_db_server_ssl: bool = Field(
        default=False,
        description="ChromaDB向量数据库是否使用SSL(当且仅当vector_db_type为remote时生效)",
    )
    vector_db_remote_headers: dict = Field(
        default={},
        description="ChromaDB向量数据库远程服务器的请求头(当且仅当vector_db_type为remote时生效)",
    )
    vector_db_tenant: str = Field(
        default="default",
        description="ChromaDB向量数据库租户名称(当且仅当vector_db_type为remote时生效)",
    )
    vector_db_database: str = Field(
        default="default",
        description="ChromaDB向量数据库数据库名称(当且仅当vector_db_type为remote时生效)",
    )
    embedding_model_url: str = Field(
        default="http://127.0.0.1:11434", description="Embedding模型地址"
    )
    embedding_model_name: str = Field(default="auto", description="Embedding模型名称")
    embedding_proctol: Literal["openai", "ollama-embed"] = Field(
        default="ollama-embed", description="Embedding模型协议"
    )


class DataManager(BaseDataManager[ConfigFile]):
    _owner_name = PLUGIN_IM
    config: ConfigFile


@get_driver().on_startup
async def setup():
    await DataManager().safe_get_config()


def build_preset() -> ModelPreset:
    return ModelPreset(
        model=env_config.embedding_model_name, base_url=env_config.embedding_model_url
    )


env_config = get_plugin_config(EnvConfig)
