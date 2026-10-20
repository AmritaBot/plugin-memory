"""会话侧读取权限校验（知识库 / 用户画像）。

知识库：会话侧（对话 LLM）可以读全局知识库，但需要 LP 节点 ``memory.knowledge``；
``lp.admin`` 与 ``.env`` 里的超级用户始终持有该权限。

用户画像：只对离线巩固循环（DMN）的目标用户开放只读，
比对 ``subconscious.target_user_id``，不做权限节点扩散。

潜意识循环内部调用均不受此限制。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from nonebot import get_driver, logger

if TYPE_CHECKING:
    from nonebot.adapters.onebot.v11 import Event

#: 读取知识库所需的权限节点
KNOWLEDGE_NODE = "memory.knowledge"
#: 管理员节点，始终持有 KNOWLEDGE_NODE
ADMIN_NODE = "lp.admin"

_ENV_ADMINS = get_driver().config.superusers

_lp_warned = False


async def _has_node(event: Event, node: str) -> bool:
    """查询 LP 节点；LP 不可用时按无权限处理。"""
    global _lp_warned
    try:
        from amrita.plugins.perm.API.rules import any_has_permission
    except Exception:
        if not _lp_warned:
            _lp_warned = True
            logger.warning("[Memory] LitePerm 不可用，知识库读取仅对 .env 超级用户开放")
        return False
    try:
        return bool(await any_has_permission(node)(event))
    except Exception as e:
        logger.opt(exception=e, colors=True, raw=True).warning(
            f"[Memory] 权限节点 {node} 校验失败: {e}"
        )
        return False


async def can_read_knowledge(event: Event | None) -> bool:
    """是否允许读取全局知识库：超级用户 / lp.admin / memory.knowledge。"""
    if event is None:
        return False
    if event.get_user_id() in _ENV_ADMINS:
        return True
    if await _has_node(event, ADMIN_NODE):
        return True
    return await _has_node(event, KNOWLEDGE_NODE)


async def get_dmn_target_user_id() -> str:
    """当前 DMN 目标用户 ID：优先取运行器中实际生效的值，回退到配置。"""
    try:
        from .rethinking import _state

        uid = _state.get_target_user_id()
        if uid:
            return str(uid)
    except Exception as e:
        logger.debug(f"[Memory] 取 DMN 运行器失败: {e}")
    try:
        from .config import DataManager

        cfg = (await DataManager().safe_get_config()).subconscious
        return str(cfg.target_user_id or "")
    except Exception as e:
        logger.debug(f"[Memory] 取 DMN 配置失败: {e}")
        return ""


async def can_read_profile(event: Event | None) -> bool:
    """是否允许读取用户画像：仅 DMN 目标用户本人。"""
    if event is None:
        return False
    target = await get_dmn_target_user_id()
    if not target:
        return False
    return str(event.get_user_id()) == target


def get_event_from_tool_ctx(ctx: Any) -> Event | None:
    """从 ToolContext 里取出触发本次调用的原始事件。"""
    try:
        from amrita.plugins.chat.runtime import try_get_amrita_ctx

        amrita_ctx = try_get_amrita_ctx(ctx.ctx.chat_object)
        if amrita_ctx is None:
            return None
        return amrita_ctx["event"]
    except Exception as e:
        logger.debug(f"[Memory] 取事件失败: {e}")
        return None
