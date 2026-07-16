"""潜意识工具处理器 — 模块导入时自动注册到全局 ToolsManager。"""

import asyncio
import json
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any

from amrita_core import on_tools
from amrita_core.libchat import call_completion, get_last_response
from amrita_core.preset import PresetManager
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
    ITER_STOP_SCHEMA,
    LIST_MEMORY_SCHEMA,
    READ_CHAT_CONTEXT_SCHEMA,
    READ_MEMORY_SCHEMA,
    SEND_TO_USER_SCHEMA,
    UPDATE_MEMORY_SCHEMA,
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
        logger.error(f"subconscious_read_memory error: {e}")
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
        logger.error(f"subconscious_write_memory error: {e}")
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
        logger.error(f"subconscious_update_memory error: {e}")
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
        logger.error(f"subconscious_delete_memory error: {e}")
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
        logger.error(f"subconscious_list_memory error: {e}")
        return _tools_err(str(e))


@on_tools(ITER_STOP_SCHEMA, strict=True, bound_to=_SUBCONSCIOUS_TOOLS)
async def subconscious_iter_stop(data: dict[str, Any]) -> str:
    runner = _state.get_runner()
    if runner is not None:
        runner._iter_stop_result = {
            "next_time": data.get("next_time"),
            "delay_seconds": data.get("delay_seconds"),
            "summary": data.get("summary", ""),
        }
    logger.info(
        f"[EXP Subconscious] iter_stop: summary={str(data.get('summary', ''))[:80]}"
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
        logger.error(f"[EXP Subconscious] Generate send content failed: {e}")
        return _tools_err(str(e))
    ts = datetime.now(timezone.utc).isoformat()
    pending = _state.get_pending()
    pending.append({"content": content, "timestamp": ts})
    await runner._save_pending_to_repo()
    logger.info(f"[EXP Subconscious] Message queued: {content[:80]}")
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
        from amrita.plugins.chat.utils.app import CachedUserDataRepository

        repo = CachedUserDataRepository()
        mem = await repo.get_memory(user_id, is_group=False)
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
        logger.error(f"subconscious_read_chat_context error: {e}")
        return _tools_err(str(e))


#  消息生成


async def _generate_send_content(intent: str, memory_context: str) -> str:
    runner = _state.get_runner()
    if runner is None:
        raise RuntimeError("Runner not initialized")
    prompt_path = (runner._prompt_dir / "subconscious_send_prompt.txt").resolve()
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
    preset = PresetManager().get_default_preset()
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
        logger.error(f"subconscious_duplicate_helper error: {e}")
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
        logger.error(f"subconscious_get_memory_stats error: {e}")
        return _tools_err(str(e))
