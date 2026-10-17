"""NoneBot2 命令：/memory"""

from amrita.plugins.menu.models import MatcherData
from nonebot import get_bot, get_driver, logger, on_command
from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message, MessageEvent
from nonebot.exception import FinishedException
from nonebot.matcher import Matcher
from nonebot.params import CommandArg

from .keys import make_scope_id
from .memo import get_memo
from .vector import AsyncUserMemory, get_db_conn

memory_cmd = on_command(
    "memory",
    aliases={"记忆"},
    state=MatcherData(
        name="记忆管理",
        description="查看/搜索/删除长期记忆",
        usage="/memory (list|search|delete) [参数]",
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
    from .rethinking import _state

    runner = _state.get_runner()
    if runner is None:
        return "潜意识调度未启动：检查 enabled 与 target_user_id"
    await runner.trigger_now()
    return "已触发，潜意识将在后台跑一轮"


MEMO_PAGE_CHARS = 600


async def _send_memo_forward(
    matcher: Matcher, event: MessageEvent, content: str
) -> None:
    """把备忘录按段拆成合并转发节点，失败则退回纯文本。"""
    chunks = [
        content[i : i + MEMO_PAGE_CHARS]
        for i in range(0, len(content), MEMO_PAGE_CHARS)
    ] or [""]
    self_id = str(event.self_id)
    nodes = [
        {
            "type": "node",
            "data": {
                "name": "备忘录",
                "uin": self_id,
                "content": f"第 {i + 1}/{len(chunks)} 段\n{chunk}",
            },
        }
        for i, chunk in enumerate(chunks)
    ]
    try:
        bot = get_bot(self_id)
        if isinstance(event, GroupMessageEvent):
            await bot.call_api(
                "send_group_forward_msg", group_id=event.group_id, messages=nodes
            )
        else:
            await bot.call_api(
                "send_private_forward_msg", user_id=event.user_id, messages=nodes
            )
    except Exception as e:
        logger.warning(f"[Memo] forward failed, fallback to text: {e}")
        await matcher.send(content)


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
