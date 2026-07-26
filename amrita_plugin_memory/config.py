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


class SubconsciousConfig(BaseModel):
    """常驻推理循环（潜意识层）配置 — 实验性功能"""

    enabled: bool = Field(default=False, description="是否启用常驻推理循环")
    target_user_id: str = Field(
        default="", description="目标用户ID（MVP仅支持单用户），为空则不启动"
    )
    allowed_tools: list[str] = Field(
        default_factory=list,
        description="额外可用工具名列表，从全局 ToolsManager 查找，不存在仅告警",
    )
    max_iterations: int = Field(
        default=10, ge=1, le=50, description="单次推理最大 ReAct 循环步数"
    )
    loop_detect_threshold: int = Field(
        default=3, ge=2, le=10, description="连续相同工具调用次数阈值，触发后注入提示"
    )
    rethink_base_delay_minutes: int = Field(
        default=30, ge=1, description="用户聊天后首次计划延迟（分钟）"
    )
    rethink_penalty_multiplier: float = Field(
        default=1.5, ge=1.0, le=10.0, description="取消惩罚指数倍率"
    )
    rethink_max_delay_minutes: int = Field(
        default=1440, ge=60, le=10080, description="惩罚延迟上限（分钟），默认1天"
    )
    prompt_file: str = Field(
        default="prompt/subconscious_main.md.jinja2",
        description="主推理提示词文件名，相对于 config/amrita_plugin_memory/",
    )
    prompt_send_file: str = Field(
        default="prompt/subconscious_send.md.jinja2",
        description="主动消息生成提示词文件名，相对于 config/amrita_plugin_memory/",
    )
    prompt_knowledge_file: str = Field(
        default="prompt/knowledge_guide.md.jinja2",
        description="知识库使用指南文件名，相对于 config/amrita_plugin_memory/",
    )
    prompt_profile_file: str = Field(
        default="prompt/profile_guide.md.jinja2",
        description="画像构建指南文件名，相对于 config/amrita_plugin_memory/",
    )
    enable_memory_compress: bool = Field(
        default=True, description="是否在每次运行后压缩持久化摘要"
    )
    allow_send_to_user: bool = Field(
        default=False, description="是否允许潜意识主动向用户发送消息"
    )
    memory_warn_threshold: int = Field(
        default=100,
        ge=10,
        description="ChromaDB 记忆总量超过此值时，在 prompt 中注入压缩提示",
    )
    max_abstracts: int = Field(
        default=5,
        ge=1,
        le=20,
        description="保留最近 N 轮摘要的滑动窗口大小",
    )
    knowledge_max_chars: int = Field(
        default=10000,
        ge=100,
        le=100000,
        description="全局知识库单条正文最大字符数",
    )
    knowledge_collection_name: str = Field(
        default="amrita_global_knowledge",
        description="ChromaDB 知识库 collection 名称",
    )


class ConfigFile(BaseModel):
    """配置文件"""

    short_term_expiry_days: int = Field(
        default=7, description="短期记忆的过期天数，默认为7天"
    )
    long_term_expiry_days: int = Field(
        default=90, description="长期记忆的过期天数，默认为90天"
    )
    per_session_memory_limit: int = Field(
        default=100, description="每个会话的记忆数量限制，默认为50条"
    )
    subconscious: SubconsciousConfig = Field(
        default_factory=SubconsciousConfig, description="常驻推理循环配置"
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
    embedding_model_api_key: str = Field(
        default="", description="Embedding模型API密钥(可选，默认为空)"
    )


class DataManager(BaseDataManager[ConfigFile]):
    _owner_name = PLUGIN_IM
    config: ConfigFile


@get_driver().on_startup
async def setup():
    await DataManager().safe_get_config()


def build_preset() -> ModelPreset:
    return ModelPreset(
        model=env_config.embedding_model_name,
        base_url=env_config.embedding_model_url,
        api_key=env_config.embedding_model_api_key,
        protocol=env_config.embedding_proctol,
    )


env_config = get_plugin_config(EnvConfig)
