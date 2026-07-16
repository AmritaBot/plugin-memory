"""Hook 注册与生命周期管理。"""

from amrita_core.types import Message
from nonebot import get_driver, logger

from ..config import DataManager
from . import _state


async def _setup_subconscious_hook() -> None:
    from amrita.plugins.chat.runtime import try_get_amrita_ctx
    from amrita_core.hook.event import PreCompletionEvent
    from amrita_core.hook.on import on_precompletion

    pre_chat_hook = on_precompletion(priority=10, block=False)

    @pre_chat_hook.handle()
    async def _subconscious_pre_hook(event: PreCompletionEvent) -> None:
        runner = _state.get_runner()
        if runner is None:
            return
        amrita_ctx = try_get_amrita_ctx(event.chat_object)
        if amrita_ctx is None:
            return
        if str(amrita_ctx["event"].user_id) != runner._config.target_user_id:
            return

        if runner._config.pause_on_user_chat:
            await runner.pause_due_to_user_activity()

        pending = _state.get_pending()
        if pending and runner._config.allow_send_to_user:
            msgs = list(pending)
            _state.set_pending([])
            await runner._save_pending_to_repo()
            for entry in msgs:
                content = entry.get("content", "")
                ts = entry.get("timestamp", "")
                if not content:
                    continue
                inject = Message(
                    role="system",
                    content=f"[系统提示] 在 {ts} 时，你（潜意识）曾想对用户说：{content}\n现在用户发来了新消息，请自然地将这个背景融入你的回复中。",
                )
                event.chat_object.data.messages.insert(0, inject)
                logger.info(
                    f"[EXP Subconscious] Injected pending msg: {content[:50]}..."
                )

    logger.debug("[EXP Subconscious] on_precompletion hook registered")


@get_driver().on_startup
async def _subconscious_startup() -> None:
    from .runner import SubconsciousRunner  # lazy to avoid circular import

    cfg = (await DataManager().safe_get_config()).subconscious
    if not cfg.enabled or not cfg.target_user_id:
        logger.debug("[EXP Subconscious] Not enabled or no target user, skip")
        return
    runner = SubconsciousRunner(cfg)
    _state.set_runner(runner)
    await runner.start()
    try:
        await _setup_subconscious_hook()
    except Exception as e:
        logger.warning(f"[EXP Subconscious] Hook setup failed: {e}")


@get_driver().on_shutdown
async def _subconscious_shutdown() -> None:
    runner = _state.get_runner()
    if runner is not None:
        runner.stop()
    _state.set_runner(None)
