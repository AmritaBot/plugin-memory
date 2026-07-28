"""潜意识工具处理器 — 模块导入时自动注册到全局 ToolsManager。"""

import asyncio
import json
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any

from amrita_core import on_tools
from amrita_core.libchat import call_completion, get_last_response
from amrita_core.types import CONTENT_LIST_TYPE, Message
from chromadb import GetResult
from jinja2 import Template
from nonebot import logger

from ..vector import AsyncUserMemory, MemoryMetadata, get_db_conn
from . import _state
from .consts import DEFAULT_SEND_PROMPT, ensure_prompt_file, load_character_prompt
from .schemas import (
    DELETE_MEMORY_SCHEMA,
    DUPLICATE_HELPER_SCHEMA,
    GET_MEMORY_STATS_SCHEMA,
    GET_PROFILE_SCHEMA,
    ITER_STOP_SCHEMA,
    KNOWLEDGE_CREATE_SCHEMA,
    KNOWLEDGE_DELETE_SCHEMA,
    KNOWLEDGE_LIST_SCHEMA,
    KNOWLEDGE_READ_SCHEMA,
    KNOWLEDGE_SEARCH_SCHEMA,
    KNOWLEDGE_SUGGEST_SCHEMA,
    KNOWLEDGE_UPDATE_SCHEMA,
    LIST_MEMORY_SCHEMA,
    READ_CHAT_CONTEXT_SCHEMA,
    READ_MEMORY_SCHEMA,
    READ_SESSIONS_SCHEMA,
    READ_SUGGESTIONS_SCHEMA,
    SEND_TO_USER_SCHEMA,
    UPDATE_MEMORY_SCHEMA,
    UPDATE_PROFILE_SCHEMA,
    WRITE_MEMORY_SCHEMA,
)

_SUBCONSCIOUS_TOOLS = _state.get_tools_manager()

#  辅助


def _make_operator() -> AsyncUserMemory:
    return AsyncUserMemory(get_db_conn())


def _get_partition_id() -> str:
    uid = _state.get_target_user_id()
    if not uid:
        raise RuntimeError("target_user_id not configured")
    return f"user_{uid}"


def _tools_err(message: str) -> str:
    return json.dumps(
        {"status": "error", "message": message}, ensure_ascii=False, indent=2
    )


def _tools_ok(**extra: Any) -> str:
    return json.dumps({"status": "success", **extra}, ensure_ascii=False, indent=2)


#  Handler


@on_tools(READ_MEMORY_SCHEMA, strict=True, bound_to=_SUBCONSCIOUS_TOOLS)
async def subconscious_read_memory(data: dict[str, Any]) -> str:
    ope = _make_operator()
    await ope.init()
    pid = _get_partition_id()
    try:
        top_k = int(data.get("top_k", 5))
        importance_raw = data.get("importance")
        if importance_raw is not None and importance_raw not in (
            "low",
            "medium",
            "high",
        ):
            importance_raw = None
        result = await ope.query_notes(
            pid,
            str(data["query"]),
            top_k=top_k,
            importance=(importance_raw if importance_raw is not None else None),
        )
        return json.dumps(result, ensure_ascii=False, indent=2, default=str)
    except Exception as e:
        logger.opt(exception=e, colors=True, raw=True).exception(
            f"subconscious_read_memory error: {e}"
        )
        return _tools_err(str(e))


@on_tools(WRITE_MEMORY_SCHEMA, strict=True, bound_to=_SUBCONSCIOUS_TOOLS)
async def subconscious_write_memory(data: dict[str, Any]) -> str:
    ope = _make_operator()
    await ope.init()
    pid = _get_partition_id()
    try:
        if data["importance"] not in ("low", "medium", "high"):
            return _tools_err(f"Excepted importance value, got {data['importance']}")
        meta = MemoryMetadata(
            importance=data["importance"], tags=str(data["tags"]), scope="user"
        )
        await ope.add_note(pid, str(data["content"]), metadata=meta)
        return _tools_ok(id=meta.memory_id)
    except Exception as e:
        logger.opt(exception=e, colors=True, raw=True).exception(
            f"subconscious_write_memory error: {e}"
        )
        return _tools_err(str(e))


@on_tools(UPDATE_MEMORY_SCHEMA, strict=True, bound_to=_SUBCONSCIOUS_TOOLS)
async def subconscious_update_memory(data: dict[str, Any]) -> str:
    ope = _make_operator()
    await ope.init()
    pid = _get_partition_id()
    try:
        result = await ope.get_all_notes(pid, include=["metadatas", "documents"])
        mem_id = str(data["id"])
        if not result or not result.get("ids"):
            return _tools_err("未找到任何记忆")
        ids: list[str] = result["ids"]
        try:
            idx = ids.index(mem_id)
        except ValueError:
            return _tools_err(f"未找到ID为 {mem_id} 的记忆")
        metadatas: Sequence[Mapping[str, Any]] = result.get("metadatas") or [
            {} for _ in ids
        ]
        documents: list[str] = result.get("documents") or ["" for _ in ids]
        existing_doc = documents[idx] if idx < len(documents) else ""
        existing_meta = metadatas[idx] if idx < len(metadatas) else {}
        new_content = str(data.get("content", existing_doc))
        new_tags = str(data.get("tags", existing_meta.get("tags", "")))
        new_importance = str(
            data.get("importance", existing_meta.get("importance", "medium"))
        )
        if new_importance not in ("low", "medium", "high"):
            new_importance = "medium"
        created_at_str = str(existing_meta.get("created_at", ""))
        try:
            created_at = datetime.fromisoformat(created_at_str)
        except ValueError:
            created_at = datetime.now(timezone.utc)
        meta = MemoryMetadata(
            memory_id=mem_id,
            importance=new_importance,
            tags=new_tags,
            scope="user",
            created_at=created_at,
        )
        await ope.update_note(pid, new_content, metadata=meta)
        return _tools_ok(message="记忆更新成功", id=mem_id)
    except Exception as e:
        logger.opt(exception=e, colors=True, raw=True).exception(
            f"subconscious_update_memory error: {e}"
        )
        return _tools_err(str(e))


@on_tools(DELETE_MEMORY_SCHEMA, strict=True, bound_to=_SUBCONSCIOUS_TOOLS)
async def subconscious_delete_memory(data: dict[str, Any]) -> str:
    ope = _make_operator()
    await ope.init()
    pid = _get_partition_id()
    try:
        result = await ope.get_all_notes(pid, include=["metadatas"])
        mem_id = str(data["id"])
        if not result or not result.get("ids"):
            return _tools_err("未找到任何记忆")
        ids: list[str] = result["ids"]
        if mem_id not in ids:
            return _tools_err(f"未找到ID为 {mem_id} 的记忆")
        await ope.delete_note(pid, mem_id)
        return _tools_ok(message="记忆删除成功", id=mem_id)
    except Exception as e:
        logger.opt(exception=e, colors=True, raw=True).exception(
            f"subconscious_delete_memory error: {e}"
        )
        return _tools_err(str(e))


@on_tools(LIST_MEMORY_SCHEMA, strict=True, bound_to=_SUBCONSCIOUS_TOOLS)
async def subconscious_list_memory(data: dict[str, Any]) -> str:
    ope = _make_operator()
    await ope.init()
    pid = _get_partition_id()
    try:
        limit = int(data.get("limit", 10))
        result: GetResult = await ope.get_all_notes(
            pid, include=["metadatas", "documents"]
        )
        if not result or not result.get("ids"):
            return _tools_ok(memories=[], total=0)
        ids: list[str] = result["ids"]
        documents: list[str] = result.get("documents") or ["" for _ in ids]
        metadatas: Sequence[Mapping[str, Any]] = result.get("metadatas") or [
            {} for _ in ids
        ]
        memories: list[dict[str, Any]] = []
        for i, doc_id in enumerate(ids):
            if i >= limit:
                break
            meta = metadatas[i] if i < len(metadatas) else {}
            doc = documents[i] if i < len(documents) else ""
            memories.append(
                {
                    "id": doc_id,
                    "tags": meta.get("tags", ""),
                    "importance": meta.get("importance", "medium"),
                    "content_preview": doc[:80] + "..." if len(doc) > 80 else doc,
                }
            )
        return _tools_ok(memories=memories, total=len(ids))
    except Exception as e:
        logger.opt(exception=e, colors=True, raw=True).exception(
            f"subconscious_list_memory error: {e}"
        )
        return _tools_err(str(e))


@on_tools(ITER_STOP_SCHEMA, strict=True, bound_to=_SUBCONSCIOUS_TOOLS)
async def subconscious_iter_stop(data: dict[str, Any]) -> str:
    logger.info(
        f"[Subconscious] iter_stop: summary={str(data.get('summary', ''))[:80]}"
    )
    return json.dumps(
        {"status": "acknowledged", "message": "推理循环已确认结束"}, ensure_ascii=False
    )


@on_tools(SEND_TO_USER_SCHEMA, bound_to=_SUBCONSCIOUS_TOOLS)
async def subconscious_send_to_user(data: dict[str, Any]) -> str:
    runner = _state.get_runner()
    if runner is None or not runner._config.allow_send_to_user:
        return _tools_err("allow_send_to_user 未启用")
    intent = str(data.get("intent", ""))
    if not intent:
        return _tools_err("intent 不能为空")
    memory_context = str(data.get("memory_context", ""))
    try:
        content = await _generate_send_content(intent, memory_context)
    except Exception as e:
        logger.opt(exception=e, colors=True, raw=True).exception(
            f"[Subconscious] Generate send content failed: {e}"
        )
        return _tools_err(str(e))
    # 持久化：保证消息在多次对话中连贯
    ts = datetime.now(timezone.utc).isoformat()
    pending = _state.get_pending()
    pending.append({"content": content, "timestamp": ts})
    await runner._save_pending_to_repo()
    # 即时发送
    try:
        from nonebot import get_bot

        bot = get_bot()
        await bot.send_private_msg(
            user_id=int(runner._config.target_user_id), message=content
        )
        logger.info(f"[Subconscious] Message sent: {content[:80]}")
        return _tools_ok(status="sent", content=content, timestamp=ts)
    except Exception as e:
        logger.warning(f"[Subconscious] Send failed (queued): {e}")
        return _tools_ok(status="queued", content=content, timestamp=ts)


@on_tools(READ_CHAT_CONTEXT_SCHEMA, strict=True, bound_to=_SUBCONSCIOUS_TOOLS)
async def subconscious_read_chat_context(data: dict[str, Any]) -> str:
    uid_str = _state.get_target_user_id()
    if not uid_str:
        return _tools_err("target_user_id not configured")
    try:
        user_id = int(uid_str)
    except ValueError:
        return _tools_err(f"invalid target_user_id: {uid_str}")
    limit = int(data.get("limit", 10))
    try:
        from nonebot_plugin_amrita.memory import CachedUserDataRepository

        repo = CachedUserDataRepository()
        mem = await repo.get_memory(f"user_{user_id}")
        messages = mem.memory_json.messages
        recent = messages[-limit:] if len(messages) > limit else messages
        return json.dumps(
            {
                "total_messages": len(messages),
                "recent": [m.model_dump(mode="json") for m in recent],
            },
            ensure_ascii=False,
            indent=2,
        )
    except Exception as e:
        logger.opt(exception=e, colors=True, raw=True).exception(
            f"subconscious_read_chat_context error: {e}"
        )
        return _tools_err(str(e))


#  消息生成


async def _generate_send_content(intent: str, memory_context: str) -> str:
    runner = _state.get_runner()
    if runner is None:
        raise RuntimeError("Runner not initialized")
    prompt_path = (runner._prompt_dir / runner._config.prompt_send_file).resolve()
    ensure_prompt_file(prompt_path, DEFAULT_SEND_PROMPT)
    character_prompt = await load_character_prompt()
    template: Template = Template(prompt_path.read_text(encoding="utf-8"))
    system_content: str = await asyncio.to_thread(
        template.render,
        intent=intent,
        memory_context=memory_context,
        current_time=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        character_prompt=character_prompt,
    )
    preset = await _state.get_preset()
    messages: CONTENT_LIST_TYPE = [
        Message(role="system", content=system_content),
        Message(
            role="user",
            content=f"请说一句自然的问候/关心/分享的话。意图：{intent}。"
            + (f" 相关记忆：{memory_context}" if memory_context else ""),
        ),
    ]
    response = await get_last_response(call_completion(messages, preset=preset))
    return response.content.strip()


#  压缩辅助工具


@on_tools(DUPLICATE_HELPER_SCHEMA, strict=True, bound_to=_SUBCONSCIOUS_TOOLS)
async def subconscious_duplicate_helper(data: dict[str, Any]) -> str:
    """返回指定范围内全部记忆 + LLM 合并指导 prompt。

    Python 侧不做语义去重，只做过滤排序和 prompt 生成，
    把"哪些是重复的"的判断交给 LLM。
    """
    ope = _make_operator()
    await ope.init()
    pid = _get_partition_id()
    try:
        result = await ope.get_all_notes(pid, include=["metadatas", "documents"])
        if not result or not result.get("ids"):
            return _tools_ok(
                total=0,
                instruction="记忆库为空，无需整理。",
                memories=[],
            )
        ids: list[str] = result["ids"]
        documents: list[str] = result.get("documents") or ["" for _ in ids]
        metadatas: Sequence[Mapping[str, Any]] = result.get("metadatas") or [
            {} for _ in ids
        ]

        # 过滤
        tag_filter = data.get("tag")
        imp_filter = data.get("importance")
        sort_by = str(data.get("sort_by", "created_at"))
        items: list[dict[str, Any]] = []
        for i, doc_id in enumerate(ids):
            meta = metadatas[i] if i < len(metadatas) else {}
            doc = documents[i] if i < len(documents) else ""
            if tag_filter and str(meta.get("tags", "")) != tag_filter:
                continue
            if imp_filter and str(meta.get("importance", "")) != imp_filter:
                continue
            items.append(
                {
                    "id": doc_id,
                    "content": doc,
                    "tags": meta.get("tags", ""),
                    "importance": meta.get("importance", "medium"),
                    "created_at": str(meta.get("created_at", "")),
                    "length": len(doc),
                }
            )

        # 排序
        if sort_by == "importance":
            importance_order = {"high": 0, "medium": 1, "low": 2}
            items.sort(key=lambda x: importance_order.get(str(x["importance"]), 1))
        elif sort_by == "length":
            items.sort(key=lambda x: x["length"], reverse=True)
        else:
            items.sort(key=lambda x: str(x.get("created_at", "")))

        # 生成指导 prompt
        tag_hint = f" tag='{tag_filter}'" if tag_filter else ""
        imp_hint = f" importance={imp_filter}" if imp_filter else ""
        instruction = (
            f"以下是{tag_hint}{imp_hint}的全部 {len(items)} 条记忆。"
            f"请逐条阅读，识别内容重复或高度相似的条目。\n\n"
            f"对于每组重复条目：\n"
            f"1. 用 subconscious_update_memory 将最重要的那一条更新为合并后的精炼内容\n"
            f"2. 用 subconscious_delete_memory 删除其余冗余条目\n\n"
            f"注意事项：\n"
            f"- 仅合并真正重复/高度相似的条目，不要合并不同主题的记忆\n"
            f"- 合并时保留更丰富的细节，去除重复表述\n"
            f"- 完成后调用 subconscious_get_memory_stats 验证总量是否下降"
        )
        return _tools_ok(
            total=len(items),
            instruction=instruction,
            memories=items,
        )
    except Exception as e:
        logger.opt(exception=e, colors=True, raw=True).exception(
            f"subconscious_duplicate_helper error: {e}"
        )
        return _tools_err(str(e))


@on_tools(GET_MEMORY_STATS_SCHEMA, strict=True, bound_to=_SUBCONSCIOUS_TOOLS)
async def subconscious_get_memory_stats(data: dict[str, Any]) -> str:
    """返回记忆库统计概览。"""
    ope = _make_operator()
    await ope.init()
    pid = _get_partition_id()
    try:
        result = await ope.get_all_notes(pid, include=["metadatas", "documents"])
        if not result or not result.get("ids"):
            return _tools_ok(
                total=0,
                by_importance={},
                by_tag={},
                oldest_created=None,
                newest_created=None,
                avg_length=0,
            )
        ids: list[str] = result["ids"]
        documents: list[str] = result.get("documents") or ["" for _ in ids]
        metadatas: Sequence[Mapping[str, Any]] = result.get("metadatas") or [
            {} for _ in ids
        ]

        by_importance: dict[str, int] = {}
        by_tag: dict[str, int] = {}
        total_length = 0
        oldest: str | None = None
        newest: str | None = None

        for i in range(len(ids)):
            meta = metadatas[i] if i < len(metadatas) else {}
            doc = documents[i] if i < len(documents) else ""
            imp = str(meta.get("importance", "medium"))
            tag = str(meta.get("tags", ""))
            by_importance[imp] = by_importance.get(imp, 0) + 1
            if tag:
                by_tag[tag] = by_tag.get(tag, 0) + 1
            total_length += len(doc)
            created_at = str(meta.get("created_at", ""))
            if created_at:
                if oldest is None or created_at < oldest:
                    oldest = created_at
                if newest is None or created_at > newest:
                    newest = created_at

        return _tools_ok(
            total=len(ids),
            by_importance=by_importance,
            by_tag=by_tag,
            oldest_created=oldest,
            newest_created=newest,
            avg_length=total_length // len(ids) if ids else 0,
        )
    except Exception as e:
        logger.opt(exception=e, colors=True, raw=True).exception(
            f"subconscious_get_memory_stats error: {e}"
        )
        return _tools_err(str(e))


#  全局知识库工具


def _get_kb_manager():
    """获取 KnowledgeBaseManager 实例，未初始化或已禁用则报错。"""
    runner = _state.get_runner()
    if runner is None:
        raise RuntimeError("SubconsciousRunner not initialized")
    if (
        not runner._config.enabled
        or not runner._config.enable_knowledge
        or runner._kb_manager is None
    ):
        raise RuntimeError(
            "全局知识库未启用（enabled=false 或 enable_knowledge=false）"
        )
    return runner._kb_manager


@on_tools(KNOWLEDGE_LIST_SCHEMA, strict=True)
@on_tools(KNOWLEDGE_LIST_SCHEMA, strict=True, bound_to=_SUBCONSCIOUS_TOOLS)
async def subconscious_knowledge_list(data: dict[str, Any]) -> str:
    try:
        kb = _get_kb_manager()
        items = await kb.list_all()
        return json.dumps(
            {"status": "success", "items": items, "total": len(items)},
            ensure_ascii=False,
            indent=2,
        )
    except Exception as e:
        logger.opt(exception=e, colors=True, raw=True).exception(
            f"knowledge_list error: {e}"
        )
        return _tools_err(str(e))


@on_tools(KNOWLEDGE_READ_SCHEMA, strict=True)
@on_tools(KNOWLEDGE_READ_SCHEMA, strict=True, bound_to=_SUBCONSCIOUS_TOOLS)
async def subconscious_knowledge_read(data: dict[str, Any]) -> str:
    try:
        kb = _get_kb_manager()
        result = await kb.read(
            str(data["kid"]),
            start_line=data.get("start_line"),
            end_line=data.get("end_line"),
        )
        if "error" in result:
            return _tools_err(str(result["error"]))
        return json.dumps({"status": "success", **result}, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.opt(exception=e, colors=True, raw=True).exception(
            f"knowledge_read error: {e}"
        )
        return _tools_err(str(e))


@on_tools(KNOWLEDGE_CREATE_SCHEMA, strict=True, bound_to=_SUBCONSCIOUS_TOOLS)
async def subconscious_knowledge_create(data: dict[str, Any]) -> str:
    try:
        kb = _get_kb_manager()
        body = str(data["body"])
        if len(body) > 10000:
            return _tools_err(f"Body too long ({len(body)} chars, max 10000)")
        kid = await kb.create(
            title=str(data["title"]),
            summary=str(data["summary"]),
            body=body,
        )
        return _tools_ok(kid=kid)
    except Exception as e:
        logger.opt(exception=e, colors=True, raw=True).exception(
            f"knowledge_create error: {e}"
        )
        return _tools_err(str(e))


@on_tools(KNOWLEDGE_UPDATE_SCHEMA, strict=True, bound_to=_SUBCONSCIOUS_TOOLS)
async def subconscious_knowledge_update(data: dict[str, Any]) -> str:
    try:
        kb = _get_kb_manager()
        kid = str(data["kid"])
        title = data.get("title")
        summary = data.get("summary")
        body = data.get("body")
        if title is None and summary is None and body is None:
            return _tools_err("At least one of title/summary/body must be provided")
        if body is not None and len(str(body)) > 10000:
            return _tools_err(f"Body too long ({len(str(body))} chars, max 10000)")
        result = await kb.update(
            kid,
            title=str(title) if title is not None else None,
            summary=str(summary) if summary is not None else None,
            body=str(body) if body is not None else None,
        )
        if result != "ok":
            return _tools_err(result)
        return _tools_ok(message="知识更新成功", kid=kid)
    except Exception as e:
        logger.opt(exception=e, colors=True, raw=True).exception(
            f"knowledge_update error: {e}"
        )
        return _tools_err(str(e))


@on_tools(KNOWLEDGE_DELETE_SCHEMA, strict=True, bound_to=_SUBCONSCIOUS_TOOLS)
async def subconscious_knowledge_delete(data: dict[str, Any]) -> str:
    try:
        kb = _get_kb_manager()
        kid = str(data["kid"])
        result = await kb.delete(kid)
        if result != "ok":
            return _tools_err(result)
        return _tools_ok(message="知识删除成功", kid=kid)
    except Exception as e:
        logger.opt(exception=e, colors=True, raw=True).exception(
            f"knowledge_delete error: {e}"
        )
        return _tools_err(str(e))


@on_tools(KNOWLEDGE_SEARCH_SCHEMA, strict=True)
@on_tools(KNOWLEDGE_SEARCH_SCHEMA, strict=True, bound_to=_SUBCONSCIOUS_TOOLS)
async def subconscious_knowledge_search(data: dict[str, Any]) -> str:
    try:
        kb = _get_kb_manager()
        top_k = int(data.get("top_k", 5))
        items = await kb.search(str(data["query"]), top_k=top_k)
        return json.dumps(
            {"status": "success", "items": items, "total": len(items)},
            ensure_ascii=False,
            indent=2,
        )
    except Exception as e:
        logger.opt(exception=e, colors=True, raw=True).exception(
            f"knowledge_search error: {e}"
        )
        return _tools_err(str(e))


#  知识建议（对话 LLM → 潜意识 Agent 审查管线）


@on_tools(KNOWLEDGE_SUGGEST_SCHEMA, strict=True)
async def knowledge_suggest(data: dict[str, Any]) -> str:
    """对话 LLM 提交知识建议，加入队列等待潜意识 Agent 审查。"""
    action = str(data["action"])
    suggestion = {
        "action": action,
        "title": str(data["title"]),
        "summary": str(data["summary"]),
        "body": str(data["body"]),
        "reason": str(data.get("reason", "")),
        "kid": str(data["kid"]) if action == "update" and data.get("kid") else "",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    _state.add_knowledge_suggestion(suggestion)
    # 触发持久化
    runner = _state.get_runner()
    if runner is not None:
        await runner._save_pending_to_repo()
    logger.info(
        f"[Subconscious] Knowledge suggestion queued: "
        f"action={action}, title={suggestion['title'][:50]}"
    )
    return _tools_ok(
        message=(
            "知识建议已提交，后台记忆管家将在下一轮推理中审查。"
            if action == "create"
            else "更新建议已提交，后台记忆管家将在下一轮推理中审查。"
        ),
        action=action,
    )


@on_tools(READ_SUGGESTIONS_SCHEMA, strict=True, bound_to=_SUBCONSCIOUS_TOOLS)
async def subconscious_read_suggestions(data: dict[str, Any]) -> str:
    """潜意识 Agent 读取待审查的知识建议并清空队列。"""
    suggestions = list(_state.get_knowledge_suggestions())
    _state.set_knowledge_suggestions([])
    # 持久化空队列
    runner = _state.get_runner()
    if runner is not None:
        await runner._save_pending_to_repo()
    logger.info(
        f"[Subconscious] Read {len(suggestions)} knowledge suggestions, queue cleared"
    )
    return json.dumps(
        {"status": "success", "suggestions": suggestions, "total": len(suggestions)},
        ensure_ascii=False,
        indent=2,
    )


#  Session 与用户画像工具


def _get_runner():
    """获取 SubconsciousRunner 实例，未初始化则报错。"""
    runner = _state.get_runner()
    if runner is None:
        raise RuntimeError("SubconsciousRunner not initialized")
    return runner


@on_tools(READ_SESSIONS_SCHEMA, strict=True, bound_to=_SUBCONSCIOUS_TOOLS)
async def subconscious_read_sessions(data: dict[str, Any]) -> str:
    try:
        runner = _get_runner()
        n = int(data.get("n", 5))
        items = await runner._read_recent_sessions(n)
        return json.dumps(
            {"status": "success", "sessions": items, "total": len(items)},
            ensure_ascii=False,
            indent=2,
        )
    except Exception as e:
        logger.opt(exception=e, colors=True, raw=True).exception(
            f"read_sessions error: {e}"
        )
        return _tools_err(str(e))


@on_tools(GET_PROFILE_SCHEMA, strict=True, bound_to=_SUBCONSCIOUS_TOOLS)
async def subconscious_get_profile(data: dict[str, Any]) -> str:
    try:
        runner = _get_runner()
        profile = await runner._read_profile(
            start_line=data.get("start_line"),
            end_line=data.get("end_line"),
        )
        return json.dumps(
            {"status": "success", **profile},
            ensure_ascii=False,
            indent=2,
        )
    except Exception as e:
        logger.opt(exception=e, colors=True, raw=True).exception(
            f"get_profile error: {e}"
        )
        return _tools_err(str(e))


@on_tools(UPDATE_PROFILE_SCHEMA, strict=True, bound_to=_SUBCONSCIOUS_TOOLS)
async def subconscious_update_profile(data: dict[str, Any]) -> str:
    try:
        runner = _get_runner()
        await runner._update_profile(
            summary=str(data["summary"]),
            new_lines=str(data["new_lines"]),
            start_line=data.get("start_line"),
            end_line=data.get("end_line"),
        )
        return _tools_ok(message="画像增量更新成功")
    except Exception as e:
        logger.opt(exception=e, colors=True, raw=True).exception(
            f"update_profile error: {e}"
        )
        return _tools_err(str(e))
