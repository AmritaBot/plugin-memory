"""ORM 模型 — L1 备忘录（UserMemo）。"""

from datetime import datetime

from nonebot_plugin_orm import Model
from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

_BOT_METADATA_TABLE = "amrita_user_metadata"


class UserMemo(Model):
    """per-uni_id 单行备忘录 — L1 记忆层（LLM 管理的常驻用户元信息）。

    主键 ``user_id`` 即框架会话 ID（``QQPlatform_Private_{qq}`` /
    ``QQPlatform_Group_{id}``），群与个人是同一张表中的不同行。
    ``content`` 为空串表示该 Scope 尚无备忘录。
    """

    __tablename__ = "amrita_plugin_memory_user_memo"
    user_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey(f"{_BOT_METADATA_TABLE}.user_id", ondelete="CASCADE"),
        primary_key=True,
    )
    content: Mapped[str] = mapped_column(Text, default="", nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.now, nullable=False
    )


class SubconsciousState(Model):
    """L3 潜意识循环的持久化元状态 — 单行 JSON payload。

    与用户数据无关（uid 为合成 ID），不复用 Bot 的 extra_prompt 字段。
    """

    __tablename__ = "amrita_plugin_memory_subconscious_state"
    uid: Mapped[str] = mapped_column(String(64), primary_key=True)
    payload: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.now, nullable=False
    )
