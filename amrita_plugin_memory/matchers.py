"""NoneBot2 命令：/memory"""

from amrita.plugins.menu.models import MatcherData
from nonebot import on_command
from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message, MessageEvent
from nonebot.matcher import Matcher
from nonebot.params import CommandArg

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

    ope = AsyncUserMemory(get_db_conn())
    await ope.init()

    if isinstance(event, GroupMessageEvent):
        partitions = [
            ("group", f"group_{event.group_id}"),
            ("user", f"user_{event.user_id}"),
        ]
        role = event.sender.role if event.sender else "member"
    else:
        partitions = [("user", f"user_{event.user_id}")]
        role = None

    if sub == "" or sub == "list":
        await _handle_list(matcher, ope, partitions)
    elif sub == "search":
        await _handle_search(matcher, ope, partitions, rest)
    elif sub == "delete":
        await _handle_delete(matcher, ope, partitions, role, rest)
    else:
        await matcher.finish(
            "用法:\n"
            "/memory           — 列出记忆\n"
            "/memory search <关键词> — 搜索记忆\n"
            "/memory delete <ID>    — 删除记忆"
        )


async def _handle_list(
    matcher: Matcher,
    ope: AsyncUserMemory,
    partitions: list[tuple[str, str]],
):
    try:
        all_lines = ["📋 记忆列表:"]
        total = 0
        for scope, pid in partitions:
            result = await ope.get_all_notes(pid, include=["metadatas", "documents"])
            if not result or not result.get("ids"):
                all_lines.append(f"  {_label(scope)}: (无)")
                continue

            ids = result["ids"]
            documents = result.get("documents") or [""] * len(ids)
            metadatas = result.get("metadatas") or [{}] * len(ids)
            total += len(ids)

            all_lines.append(f"  {_label(scope)} ({len(ids)} 条):")
            for i, doc_id in enumerate(ids):
                meta = metadatas[i] if i < len(metadatas) else {}
                doc = documents[i] if i < len(documents) else ""
                preview = doc[:50] + "..." if len(doc) > 50 else doc
                all_lines.append(
                    f"    [{meta.get('importance', '-')}] {doc_id[:8]}… "
                    f"{preview} | tag: {meta.get('tags', '-')}"
                )

        all_lines[0] = f"📋 记忆列表（共 {total} 条）:"
        await matcher.finish("\n".join(all_lines))
    except Exception as e:
        await matcher.finish(f"列出记忆失败: {e}")


async def _handle_search(
    matcher: Matcher,
    ope: AsyncUserMemory,
    partitions: list[tuple[str, str]],
    query: str,
):
    if not query:
        await matcher.finish("请提供搜索关键词，例如: /memory search 喜欢什么")

    try:
        all_lines = [f"🔍 搜索「{query}」结果:"]
        has_result = False
        for scope, pid in partitions:
            res = await ope.query_notes(pid, query, top_k=5)
            if not res or not res.get("ids") or not res["ids"]:
                continue

            flat_ids = res["ids"][0]
            if not flat_ids:
                continue
            has_result = True

            raw_docs = res.get("documents")
            flat_docs = raw_docs[0] if raw_docs else []
            raw_metas = res.get("metadatas")
            flat_metas = raw_metas[0] if raw_metas else []
            raw_dist = res.get("distances")
            distances: list[float] = raw_dist[0] if raw_dist else []  # type: ignore[assignment]

            all_lines.append(f"  {_label(scope)}:")
            for i, doc_id in enumerate(flat_ids):
                meta = flat_metas[i] if i < len(flat_metas) else {}
                doc = flat_docs[i] if i < len(flat_docs) else ""
                dist = distances[i] if i < len(distances) else 0.0
                preview = doc[:50] + "..." if len(doc) > 50 else doc
                all_lines.append(
                    f"    [{meta.get('importance', '-')}] {doc_id[:8]}… "
                    f"{preview} | tag: {meta.get('tags', '-')} | score: {dist:.3f}"
                )

        if not has_result:
            all_lines.append(f"  未找到与「{query}」相关的记忆")

        await matcher.finish("\n".join(all_lines))
    except Exception as e:
        await matcher.finish(f"搜索记忆失败: {e}")


async def _handle_delete(
    matcher: Matcher,
    ope: AsyncUserMemory,
    partitions: list[tuple[str, str]],
    role: str | None,
    doc_id: str,
):
    if not doc_id:
        await matcher.finish("请提供要删除的记忆 ID")

    # 先找到记忆属于哪个分区
    for scope, pid in partitions:
        result = await ope.get_all_notes(pid, include=["metadatas"])
        if doc_id in (result.get("ids") or []):
            # 群共享记忆只有 admin/owner 能删
            if scope == "group" and role == "member":
                await matcher.finish(
                    f"❌ 无权删除群共享记忆（{_label(scope)}），仅管理员和群主可删除"
                )
            await ope.delete_note(pid, doc_id)
            await matcher.finish(f"✅ 已删除 {_label(scope)} 记忆: {doc_id[:8]}…")
            return

    await matcher.finish(f"未找到记忆: {doc_id}")
