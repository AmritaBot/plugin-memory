import json
from datetime import datetime
from typing import Any, cast

from amrita.plugins.chat.runtime import AmritaBotContext
from amrita.plugins.chat.utils.sql import get_uni_user_id
from amrita_core import (
    FunctionDefinitionSchema,
    FunctionParametersSchema,
    FunctionPropertySchema,
    ToolContext,
    on_tools,
)
from chromadb import QueryResult
from nonebot import logger
from nonebot.adapters.onebot.v11 import Event as OB11Event

from .config import DataManager
from .keys import Scope, resolve_scope_uni_id
from .memo import MemoTooLongError, set_memo
from .vector import AsyncUserMemory, MemoryMetadata, get_db_conn


def _get_event(ctx: ToolContext) -> OB11Event:
    """从 ToolContext 中提取 OneBot V11 原始事件"""
    assert ctx.ctx.chat_object
    amrictx: AmritaBotContext = ctx.ctx.chat_object._hook_kwargs["amrita"]
    return amrictx["event"]


def _resolve_scope_id(ctx: ToolContext, scope: str) -> str:
    """根据 scope 返回对应的分区 key（框架 uni_id）

    - scope="group": 群共享记忆，返回 f"QQPlatform_Group_{group_id}"
    - scope="user":  用户专属记忆，返回 f"QQPlatform_Private_{user_id}"（群聊私聊互通）
    """
    return resolve_scope_uni_id(_get_event(ctx), scope)


def _make_operator() -> AsyncUserMemory:
    """创建 AsyncUserMemory 实例"""
    return AsyncUserMemory(get_db_conn())


def _ok(**extra: Any) -> str:
    """构建成功响应 JSON"""
    return json.dumps({"status": "success", **extra}, ensure_ascii=False, indent=4)


def _err(message: str) -> str:
    """构建错误响应 JSON"""
    return json.dumps(
        {"status": "error", "message": message}, ensure_ascii=False, indent=4
    )


def _check_required(ctx: ToolContext, tool_name: str, *params: str) -> str | None:
    """校验必填参数，缺失时返回错误消息；全部通过返回 None"""
    for p in params:
        if p not in ctx.data or ctx.data[p] is None:
            return _err(f"调用 {tool_name} 工具必须带有 {p} 参数")
    return None


_SCOPE_PROP = FunctionPropertySchema(
    type="string",
    description=(
        "记忆范围：`group`=群共享(群内所有人可见)，"
        "`user`=个人专属(仅自己可见)。私聊中不可用`group`"
    ),
    enum=["group", "user"],
)


WRITE_MEMORY_FUN = FunctionDefinitionSchema(
    name="write_memory",
    description="将当前用户(或群组)的重要信息存入长期记忆",
    parameters=FunctionParametersSchema(
        type="object",
        properties={
            "content": FunctionPropertySchema(
                type="string", description="记忆内容，简洁明了"
            ),
            "tags": FunctionPropertySchema(
                type="string",
                description="分类标签，比如`preference`,`project`",
            ),
            "importance": FunctionPropertySchema(
                type="string",
                description="重要性等级",
                enum=["low", "medium", "high"],
            ),
            "scope": _SCOPE_PROP,
        },
        required=["content", "tags", "importance", "scope"],
    ),
)
READ_MEMORY_FUN = FunctionDefinitionSchema(
    name="read_memory",
    description="从记忆库检索当前用户(或群组)相关信息",
    parameters=FunctionParametersSchema(
        type="object",
        properties={
            "query": FunctionPropertySchema(
                type="string",
                description="字符串，用空格分割关键词",
            ),
            "top_k": FunctionPropertySchema(
                type="integer", description="返回数量，默认5条", default=5
            ),
            "importance": FunctionPropertySchema(
                type="string",
                description="重要性等级，Optional",
                enum=["low", "medium", "high"],
                nullable=True,
            ),
            "scope": _SCOPE_PROP,
        },
        required=["query", "scope"],
    ),
)


UPDATE_FUN = FunctionDefinitionSchema(
    name="update_memory",
    description="更新指定unique_id的记忆内容",
    parameters=FunctionParametersSchema(
        type="object",
        properties={
            "id": FunctionPropertySchema(type="string", description="要更新的记忆ID"),
            "content": FunctionPropertySchema(
                type="string", description="新的记忆内容（如不修改可不传）"
            ),
            "tags": FunctionPropertySchema(
                type="string",
                description="新的标签（如不修改可不传）",
            ),
            "importance": FunctionPropertySchema(
                type="string",
                description="重要性等级（如不修改可不传）",
                enum=["low", "medium", "high"],
            ),
            "scope": _SCOPE_PROP,
        },
        required=["id", "scope"],
    ),
)


DELETE_FUN = FunctionDefinitionSchema(
    name="delete_memory",
    description="删除指定unique_id的记忆",
    parameters=FunctionParametersSchema(
        type="object",
        properties={
            "id": FunctionPropertySchema(type="string", description="要删除的记忆ID"),
            "scope": _SCOPE_PROP,
        },
        required=["id", "scope"],
    ),
)


LIST_MEMORY_FUN = FunctionDefinitionSchema(
    name="list_memory",
    description="列出当前用户(或群组)的所有记忆(返回tag+id)",
    parameters=FunctionParametersSchema(
        type="object",
        properties={
            "limit": FunctionPropertySchema(
                type="integer", description="返回数量，默认5条", default=5
            ),
            "scope": _SCOPE_PROP,
        },
        required=["scope"],
    ),
)

UPDATE_MEMO_FUN = FunctionDefinitionSchema(
    name="update_memo",
    description=(
        "整篇替换当前作用域的备忘录——一段常驻注入系统提示的"
        "简短用户元信息（≤1000字），存放每次对话都需要的恒常事实，"
        "如身份、稳定偏好、用户要求的工具调用模式。"
        "调用前先精炼内容：只保留高价值事实，细节应写入 write_memory。"
        "超长会被拒绝，需压缩后重试"
    ),
    parameters=FunctionParametersSchema(
        type="object",
        properties={
            "content": FunctionPropertySchema(
                type="string", description="备忘录新全文，整篇替换旧内容"
            ),
        },
        required=["content"],
    ),
)


@on_tools(WRITE_MEMORY_FUN, custom_run=True, strict=True)
async def w(ctx: ToolContext) -> str:
    if err := _check_required(
        ctx, "write_memory", "content", "tags", "importance", "scope"
    ):
        return err
    scope: str = ctx.data["scope"]
    try:
        partition_id = _resolve_scope_id(ctx, scope)
    except ValueError as e:
        return _err(str(e))

    ope = _make_operator()
    await ope.init()
    logger.debug("开始写入记忆...")
    logger.debug(f"scope={scope}, partition_id={partition_id}")
    logger.debug(f"记忆Payload {ctx.data['content']}")
    if (
        await ope.count_user_notes(partition_id)
        >= (await DataManager().safe_get_config()).per_session_memory_limit
    ):
        return _err("当前会话的记忆数量已超过限制，请清理后再试")
    meta = MemoryMetadata(
        importance=ctx.data["importance"],
        tags=ctx.data["tags"],
        scope=cast(Scope, scope),
    )
    await ope.add_note(partition_id, ctx.data["content"], metadata=meta)
    dmp = meta.model_dump(mode="json")
    dmp["status"] = "success"
    return json.dumps(dmp, ensure_ascii=False, indent=4)


@on_tools(READ_MEMORY_FUN, custom_run=True, strict=True)
async def r(ctx: ToolContext) -> str:
    if err := _check_required(ctx, "read_memory", "query", "scope"):
        return err
    scope: str = ctx.data["scope"]
    try:
        partition_id = _resolve_scope_id(ctx, scope)
    except ValueError as e:
        return _err(str(e))

    ope = _make_operator()
    await ope.init()
    logger.debug("开始检索记忆...")
    logger.debug(f"scope={scope}, partition_id={partition_id}")
    try:
        res: QueryResult = await ope.query_notes(
            partition_id,
            ctx.data["query"],
            top_k=ctx.data.get("top_k", 5),
            importance=ctx.data.get("importance"),
        )
        return json.dumps(res, ensure_ascii=False, indent=4)
    except Exception as e:
        logger.opt(exception=e, colors=True, raw=True).exception(
            f"检索记忆时发生错误: {e}"
        )
        return _err(f"检索记忆失败: {e!s}")


@on_tools(UPDATE_FUN, custom_run=True, strict=True)
async def update_memory(ctx: ToolContext) -> str:
    if err := _check_required(ctx, "update_memory", "id", "scope"):
        return err
    scope: str = ctx.data["scope"]
    try:
        partition_id = _resolve_scope_id(ctx, scope)
    except ValueError as e:
        return _err(str(e))

    ope = _make_operator()
    await ope.init()
    logger.debug("开始更新记忆...")
    logger.debug(f"scope={scope}, partition_id={partition_id}")
    logger.debug(f"更新记忆ID: {ctx.data['id']}")

    try:
        result = await ope.get_all_notes(
            partition_id, include=["metadatas", "documents"]
        )
        if not result or not result.get("ids"):
            return _err("未找到指定ID的记忆")

        ids = result["ids"]
        try:
            target_index = ids.index(ctx.data["id"])
        except ValueError:
            return _err("未找到指定ID的记忆")

        documents = result.get("documents") or []
        metadatas = result.get("metadatas") or []
        existing_doc = documents[target_index] if target_index < len(documents) else ""
        existing_meta = metadatas[target_index] if target_index < len(metadatas) else {}

        if not existing_meta:
            return _err("记忆元数据损坏")

        new_content = ctx.data.get("content", existing_doc)
        new_tags = ctx.data.get("tags", existing_meta.get("tags", ""))
        new_importance = ctx.data.get(
            "importance", existing_meta.get("importance", "medium")
        )

        created_at_str = existing_meta.get("created_at", "")
        if isinstance(created_at_str, str):
            try:
                created_at = datetime.fromisoformat(created_at_str)
            except ValueError:
                created_at = datetime.now()
        else:
            created_at = datetime.now()

        meta = MemoryMetadata(
            memory_id=ctx.data["id"],
            importance=new_importance,
            tags=new_tags,
            scope=cast(
                Scope,
                existing_meta.get("scope", scope)
                if isinstance(existing_meta.get("scope"), str)
                else scope,
            ),
            created_at=created_at,
        )
        await ope.update_note(partition_id, new_content, metadata=meta)

        return _ok(message="记忆更新成功", id=ctx.data["id"])
    except Exception as e:
        logger.opt(exception=e, colors=True, raw=True).exception(
            f"更新记忆时发生错误: {e}"
        )
        return _err(f"更新记忆失败: {e!s}")


@on_tools(DELETE_FUN, custom_run=True, strict=True)
async def delete_memory(ctx: ToolContext) -> str:
    if err := _check_required(ctx, "delete_memory", "id", "scope"):
        return err
    scope: str = ctx.data["scope"]
    try:
        partition_id = _resolve_scope_id(ctx, scope)
    except ValueError as e:
        return _err(str(e))

    ope = _make_operator()
    await ope.init()
    logger.debug("开始删除记忆...")
    logger.debug(f"scope={scope}, partition_id={partition_id}")
    logger.debug(f"删除记忆ID: {ctx.data['id']}")

    try:
        result = await ope.get_all_notes(partition_id, include=["metadatas"])
        if (
            not result
            or not result.get("ids")
            or ctx.data["id"] not in (result.get("ids") or [])
        ):
            return _err("未找到指定ID的记忆")

        await ope.delete_note(partition_id, ctx.data["id"])
        return _ok(message="记忆删除成功", id=ctx.data["id"])
    except Exception as e:
        logger.opt(exception=e, colors=True, raw=True).exception(
            f"删除记忆时发生错误: {e}"
        )
        return _err(f"删除记忆失败: {e!s}")


@on_tools(LIST_MEMORY_FUN, custom_run=True, strict=True)
async def list_memory(ctx: ToolContext) -> str:
    if err := _check_required(ctx, "list_memory", "scope"):
        return err
    scope: str = ctx.data["scope"]
    try:
        partition_id = _resolve_scope_id(ctx, scope)
    except ValueError as e:
        return _err(str(e))

    ope = _make_operator()
    await ope.init()
    logger.debug("开始列出记忆...")
    logger.debug(f"scope={scope}, partition_id={partition_id}")

    try:
        limit = ctx.data.get("limit", 5)
        result = await ope.get_all_notes(
            partition_id, include=["metadatas", "documents"]
        )

        if not result or not result.get("ids"):
            return _ok(memories=[], total=0)

        memories = []
        ids = result["ids"]
        documents = result.get("documents") or [""] * len(ids)
        metadatas = result.get("metadatas") or [{}] * len(ids)

        for i, doc_id in enumerate(ids):
            if i >= limit:
                break
            meta = metadatas[i] if i < len(metadatas) else {}
            doc = documents[i] if i < len(documents) else ""

            memories.append(
                {
                    "id": doc_id,
                    "scope": meta.get("scope", "user"),
                    "tags": meta.get("tags", ""),
                    "importance": meta.get("importance", "medium"),
                    "content_preview": doc[:50] + "..." if len(doc) > 50 else doc,
                }
            )

        return _ok(memories=memories, total=len(ids))
    except Exception as e:
        logger.opt(exception=e, colors=True, raw=True).exception(
            f"列出记忆时发生错误: {e}"
        )
        return _err(f"列出记忆失败: {e!s}")


@on_tools(UPDATE_MEMO_FUN, custom_run=True, strict=True)
async def update_memo(ctx: ToolContext) -> str:
    if err := _check_required(ctx, "update_memo", "content"):
        return err
    event: OB11Event = _get_event(ctx)
    uni_id = get_uni_user_id(event)
    content: str = ctx.data["content"]
    try:
        await set_memo(uni_id, content)
    except MemoTooLongError as e:
        return _err(str(e))
    except Exception as e:
        logger.opt(exception=e, colors=True, raw=True).exception(
            f"更新备忘录时发生错误: {e}"
        )
        return _err(f"更新备忘录失败: {e!s}")
    logger.debug(f"更新备忘录成功: uni_id={uni_id}, len={len(content)}")
    return _ok(message="备忘录已更新", chars=len(content))
