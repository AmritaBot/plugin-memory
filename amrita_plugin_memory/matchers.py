"""NoneBot2 命令：/memory"""

from amrita.plugins.chat.utils.functions import split_message_into_chats
from amrita.plugins.menu.models import MatcherData
from amrita.utils.send import send_forward_msg
from nonebot import get_bot, get_driver, logger, on_command
from nonebot.adapters.onebot.v11 import (
    GroupMessageEvent,
    Message,
    MessageEvent,
    MessageSegment,
)
from nonebot.exception import FinishedException
from nonebot.matcher import Matcher
from nonebot.params import CommandArg

from .config import DataManager
from .keys import make_scope_id
from .memo import get_memo
from .vector import AsyncUserMemory, get_db_conn

memory_cmd = on_command(
    "memory",
    aliases={"记忆"},
    state=MatcherData(
        name="记忆管理",
        description="查看/搜索/删除长期记忆，回看备忘录，手动触发潜意识调度",
        usage="/memory (list|search|delete|memo|dmn) [参数]",
    ).model_dump(),
)


def _label(scope: str) -> str:
    return "👥群共享" if scope == "group" else "👤个人"


@memory_cmd.handle()
async def _(
    matcher: Matcher,
    event: MessageEvent,
    args: Message = CommandArg(),
):
    args_list = args.extract_plain_text().strip().split(maxsplit=1)
    sub = args_list[0].lower() if args_list else ""
    rest = args_list[1] if len(args_list) > 1 else ""

    if sub in ("memo", "备忘录"):
        uni_id = (
            make_scope_id(event.group_id, is_group=True)
            if isinstance(event, GroupMessageEvent)
            else make_scope_id(event.user_id, is_group=False)
        )
        content = await get_memo(uni_id)
        if not content:
            await matcher.finish("当前作用域还没有备忘录")
        await _send_memo_forward(matcher, event, content)
        await matcher.finish()

    if sub in ("dmn", "run", "触发"):
        await matcher.finish(await _trigger_dmn(event))

    ope = AsyncUserMemory(get_db_conn())
    await ope.init()

    if isinstance(event, GroupMessageEvent):
        available_scopes = {"group", "user"}
        partitions = {
            "group": make_scope_id(event.group_id, is_group=True),
            "user": make_scope_id(event.user_id, is_group=False),
        }
        role = event.sender.role if event.sender else "member"
    else:
        available_scopes = {"user"}
        partitions = {"user": make_scope_id(event.user_id, is_group=False)}
        role = None

    # 解析子命令: /memory [scope] [action] [args...]
    if sub in available_scopes:
        scope = sub
        action = rest.split(maxsplit=1)[0] if rest else ""
        action_args = rest.split(maxsplit=1)[1] if rest and " " in rest else ""
    elif sub in ("list", "search", "delete"):
        # 老格式兼容 /memory list 等 -> 默认以个人范围执行
        scope = "user"
        action = sub
        action_args = rest
    else:
        scope = None
        action = ""
        action_args = ""

    pid = partitions.get(scope) if scope else None

    if scope and pid and action in ("", "list"):
        await _handle_list(matcher, ope, pid, scope, action_args)
    elif scope and pid and action == "search":
        await _handle_search(matcher, ope, pid, scope, action_args)
    elif scope and pid and action == "delete":
        await _handle_delete(matcher, ope, pid, scope, role, action_args)
    else:
        hint = (
            "用法:\n"
            "/memory user list           — 列出个人记忆\n"
            "/memory user search <关键词> — 搜索个人记忆\n"
            "/memory user delete <ID>    — 删除个人记忆"
        )
        if "group" in available_scopes:
            hint += (
                "\n"
                "/memory group list          — 列出群共享记忆\n"
                "/memory group search <关键词> — 搜索群共享记忆\n"
                "/memory group delete <ID>    — 删除群共享记忆(仅管理/群主)"
            )
        await matcher.finish(hint)


async def _trigger_dmn(event: MessageEvent) -> str:
    """手动触发潜意识调度，返回给用户的提示文案。"""
    if str(event.get_user_id()) not in get_driver().config.superusers:
        return "仅超级用户可手动触发潜意识调度"

    cfg = (await DataManager().safe_get_config()).subconscious
    if not cfg.enabled:
        return "潜意识调度未启用：subconscious.enabled 为 false"
    if not cfg.target_user_id:
        return "潜意识调度未启用：subconscious.target_user_id 为空"

    from .rethinking import _state

    runner = _state.get_runner()
    if runner is None:
        return "潜意识调度未启动：钩子挂载失败或启动异常，检查日志"
    await runner.trigger_now()
    return "已触发，潜意识将在后台跑一轮"


MEMO_SEGMENT_CHARS = 600


def _pack_memo_nodes(content: str) -> list[str]:
    """按行装箱成转发节点，单行过长时再按句切，不从句子中间断开。"""
    units: list[str] = []
    for line in content.splitlines(keepends=True):
        if len(line) <= MEMO_SEGMENT_CHARS:
            units.append(line)
        else:
            units.extend(split_message_into_chats(line, max_length=MEMO_SEGMENT_CHARS))
    nodes: list[str] = []
    buf = ""
    for unit in units:
        if buf and len(buf) + len(unit) > MEMO_SEGMENT_CHARS:
            nodes.append(buf)
            buf = unit
        else:
            buf += unit
    if buf:
        nodes.append(buf)
    packed = [n.rstrip("\n") for n in nodes if n.strip()]
    return packed or [content]


async def _send_memo_forward(
    matcher: Matcher, event: MessageEvent, content: str
) -> None:
    """以合并转发发送备忘录，沿用 Amrita 的 send_forward_msg。"""
    nodes = _pack_memo_nodes(content)
    try:
        await send_forward_msg(
            get_bot(str(event.self_id)),
            event,
            "备忘录",
            uin=str(event.self_id),
            msgs=[MessageSegment.text(i) for i in nodes],
        )
    except Exception as e:
        logger.warning(f"[Memo] forward failed, fallback to text: {e}")
        for node in nodes:
            await matcher.send(node)


async def _handle_list(
    matcher: Matcher,
    ope: AsyncUserMemory,
    partition_id: str,
    scope: str,
    arg: str = "",
):
    try:
        size = 10
        page = int(arg.strip()) if arg.strip().isdigit() else 1
        page = max(1, page)
        total = await ope.count_user_notes(partition_id)
        if total == 0:
            await matcher.finish(f"{_label(scope)} 暂无记忆")

        pages = (total + size - 1) // size
        page = min(page, pages)
        offset = (page - 1) * size
        result = await ope.get_all_notes(
            partition_id,
            include=["metadatas", "documents"],
            limit=size,
            offset=offset,
        )
        ids = result.get("ids") or []
        documents = result.get("documents") or [""] * len(ids)
        metadatas = result.get("metadatas") or [{}] * len(ids)

        lines = [f"📋 {_label(scope)} 记忆列表（第 {page}/{pages} 页，共 {total} 条）:"]
        for i, doc_id in enumerate(ids):
            meta = metadatas[i] if i < len(metadatas) else {}
            doc = documents[i] if i < len(documents) else ""
            preview = doc[:50] + "..." if len(doc) > 50 else doc
            lines.append(
                f"[{meta.get('importance', '-')}] [{doc_id}] "
                f"{preview} | tag: {meta.get('tags', '-')}"
            )
        if page < pages:
            lines.append(f"下一页: /memory {scope} list {page + 1}")

        await matcher.finish("\n".join(lines))
    except FinishedException:
        return
    except Exception as e:
        await matcher.finish(f"列出记忆失败: {e}")


async def _handle_search(
    matcher: Matcher,
    ope: AsyncUserMemory,
    partition_id: str,
    scope: str,
    query: str,
):
    if not query:
        await matcher.finish("请提供搜索关键词，例如: /memory user search 喜欢什么")

    try:
        res = await ope.query_notes(partition_id, query, top_k=10)
        if not res or not res.get("ids") or not res["ids"]:
            await matcher.finish(f"{_label(scope)} 未找到与「{query}」相关的记忆")

        flat_ids = res["ids"][0]
        if not flat_ids:
            await matcher.finish(f"{_label(scope)} 未找到与「{query}」相关的记忆")

        raw_docs = res.get("documents")
        flat_docs = raw_docs[0] if raw_docs else []
        raw_metas = res.get("metadatas")
        flat_metas = raw_metas[0] if raw_metas else []
        raw_dist = res.get("distances")
        distances: list[float] = raw_dist[0] if raw_dist else []

        lines = [f"🔍 {_label(scope)} 搜索「{query}」结果（距离越小越相似）:"]
        for i, doc_id in enumerate(flat_ids):
            meta = flat_metas[i] if i < len(flat_metas) else {}
            doc = flat_docs[i] if i < len(flat_docs) else ""
            dist = distances[i] if i < len(distances) else None
            dist_text = f"{dist:.4f}" if dist is not None else "-"
            preview = doc[:50] + "..." if len(doc) > 50 else doc
            lines.append(
                f"[{meta.get('importance', '-')}] [{doc_id}] "
                f"{preview} | tag: {meta.get('tags', '-')} | 距离: {dist_text}"
            )

        await matcher.finish("\n".join(lines))
    except FinishedException:
        return
    except Exception as e:
        await matcher.finish(f"搜索记忆失败: {e}")


async def _handle_delete(
    matcher: Matcher,
    ope: AsyncUserMemory,
    partition_id: str,
    scope: str,
    role: str | None,
    doc_id: str,
):
    if not doc_id:
        await matcher.finish("请提供要删除的记忆 ID")

    try:
        result = await ope.get_all_notes(partition_id, include=["metadatas"])
        all_ids: list[str] = result.get("ids") or []

        if doc_id in all_ids:
            resolved_id = doc_id
        else:
            matches = [mid for mid in all_ids if mid.startswith(doc_id)]
            if len(matches) == 0:
                await matcher.finish(
                    f"{_label(scope)} 未找到匹配的记忆: {doc_id}\n"
                    f"提示: 使用 /memory {scope} list 查看全部 ID"
                )
            elif len(matches) > 1:
                await matcher.finish(
                    f"{_label(scope)} 前缀 '{doc_id}' 匹配多条记忆，请提供更完整的 ID:\n"
                    + "\n".join(f"  - {m}" for m in matches)
                )
            resolved_id = matches[0]

        if scope == "group" and role == "member":
            await matcher.finish(
                f"❌ 无权删除{_label(scope)}记忆，仅管理员和群主可删除"
            )

        await ope.delete_note(partition_id, resolved_id)
        await matcher.finish(f"✅ 已删除 {_label(scope)} 记忆: [{resolved_id}]")
    except FinishedException:
        return
    except Exception as e:
        await matcher.finish(f"删除记忆失败: {e}")
