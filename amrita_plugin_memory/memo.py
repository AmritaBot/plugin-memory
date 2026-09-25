"""L1 备忘录 — 常驻注入的 LLM 管理用户元信息。

- 存储：per-uni_id 单行（``QQPlatform_Private_{qq}`` / ``QQPlatform_Group_{id}``）
- 注入：pre-completion hook，群聊注入群 memo，私聊注入个人 memo
- 维护：LLM 通过 ``update_memo`` 工具整篇替换，服务端做字数硬校验
"""

from __future__ import annotations

from datetime import datetime

import aiologic
from amrita.plugins.chat.runtime import try_get_amrita_ctx
from amrita.plugins.chat.utils.sql import get_uni_user_id
from amrita_core.hook.event import PreCompletionEvent
from amrita_core.hook.on import on_precompletion
from amrita_core.types import Message
from nonebot import get_driver, logger
from nonebot_plugin_amrita.cache import LRUCache
from nonebot_plugin_amrita.database import UserMetadata
from nonebot_plugin_orm import get_session
from sqlalchemy import select

from .config import DataManager
from .models import UserMemo

# memo 读取缓存（进程内，写穿失效）
_cache: LRUCache[str, str] = LRUCache(128)
# 写操作串行锁（memo 写入频率极低，单锁足够）
_write_lock = aiologic.Lock()


class MemoTooLongError(ValueError):
    """备忘录超出字数上限。"""


async def get_memo(uni_id: str) -> str:
    """读取指定 Scope 的备忘录全文，空串表示不存在。"""
    if (cached := _cache.get(uni_id)) is not None:
        return cached
    async with get_session() as session:
        row = (
            await session.execute(select(UserMemo).where(UserMemo.user_id == uni_id))
        ).scalar_one_or_none()
    content = row.content if row is not None else ""
    _cache[uni_id] = content
    return content


async def set_memo(uni_id: str, content: str) -> None:
    """整篇替换指定 Scope 的备忘录（upsert）。

    Raises:
        MemoTooLongError: 超出 memo_max_chars 上限。
    """
    cfg = await DataManager().safe_get_config()
    if len(content) > cfg.memo_max_chars:
        raise MemoTooLongError(
            f"备忘录长度 {len(content)} 超过上限 {cfg.memo_max_chars}，请压缩后重试"
        )
    async with _write_lock:
        async with get_session() as session:
            async with session.begin():
                row = (
                    await session.execute(
                        select(UserMemo).where(UserMemo.user_id == uni_id)
                    )
                ).scalar_one_or_none()
                if row is None:
                    # 兜底确保 FK 存在（会话中 chat 插件通常已创建 metadata 行）
                    meta = (
                        await session.execute(
                            select(UserMetadata).where(UserMetadata.user_id == uni_id)
                        )
                    ).scalar_one_or_none()
                    if meta is None:
                        session.add(UserMetadata(user_id=uni_id))
                    session.add(UserMemo(user_id=uni_id, content=content))
                else:
                    row.content = content
                    row.version += 1
                    row.updated_at = datetime.now()
        _cache[uni_id] = content


def _format_memo(content: str) -> str:
    return (
        "以下是关于当前对话主体的备忘信息，位于<MEMO>块\n"
        "仅必要时可通过 update_memo 工具维护。\n"
        "<MEMO>\n"
        f"{content}\n"
        "</MEMO>"
    )


async def _setup_memo_hook() -> None:
    pre_chat_hook = on_precompletion(priority=15, block=False)

    @pre_chat_hook.handle()
    async def _memo_pre_hook(event: PreCompletionEvent) -> None:
        amrita_ctx = try_get_amrita_ctx(event.chat_object)
        if amrita_ctx is None:
            return
        # 群聊事件 → 群 uni_id；私聊事件 → 个人 uni_id
        uni_id = get_uni_user_id(amrita_ctx["event"])
        memo = await get_memo(uni_id)
        if not memo:
            return
        inject = Message(role="system", content=_format_memo(memo))
        event.chat_object.data.messages.insert(0, inject)
        logger.debug(f"[Memo] Injected memo for {uni_id} ({len(memo)} chars)")


@get_driver().on_startup
async def _memo_startup() -> None:
    try:
        await _setup_memo_hook()
    except Exception as e:
        logger.warning(f"[Memo] Hook setup failed: {e}")
