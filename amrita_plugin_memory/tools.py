import json
from datetime import datetime
from typing import Any

from amrita.plugins.chat.runtime import AmritaBotContext
from amrita_core import (
    FunctionDefinitionSchema,
    FunctionParametersSchema,
    FunctionPropertySchema,
    ToolContext,
    on_tools,
)
from chromadb import QueryResult
from nonebot import logger

from .config import DataManager
from .vector import AsyncUserMemory, MemoryMetadata, get_db_conn


def _get_session_id(ctx: ToolContext) -> str:
    """从 ToolContext 中提取会话 ID"""
    amrictx: AmritaBotContext = ctx.ctx.chat_object._hook_kwargs["amrita"]
    return amrictx["event"].get_session_id()


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
        },
        required=["content", "tags", "importance"],
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
        },
        required=["query"],
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
        },
        required=["id"],
    ),
)


DELETE_FUN = FunctionDefinitionSchema(
    name="delete_memory",
    description="删除指定unique_id的记忆",
    parameters=FunctionParametersSchema(
        type="object",
        properties={
            "id": FunctionPropertySchema(type="string", description="要删除的记忆ID")
        },
        required=["id"],
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
        },
        required=[],
    ),
)


@on_tools(WRITE_MEMORY_FUN, custom_run=True, strict=True)
async def w(ctx: ToolContext) -> str:
    session_id = _get_session_id(ctx)
    ope = _make_operator()
    await ope.init()
    logger.debug("开始写入记忆...")
    logger.debug(f"会话ID: {session_id}")
    logger.debug(f"记忆Payload {ctx.data['content']}")
    if (
        await ope.count_user_notes(session_id)
        >= (await DataManager().safe_get_config()).per_session_memory_limit
    ):
        return _err("当前会话的记忆数量已超过限制，请清理后再试")
    meta = MemoryMetadata(
        importance=ctx.data["importance"],
        tags=ctx.data["tags"],
    )
    await ope.add_note(session_id, ctx.data["content"], metadata=meta)
    dmp = meta.model_dump()
    dmp["status"] = "success"
    return json.dumps(dmp, ensure_ascii=False, indent=4)


@on_tools(READ_MEMORY_FUN, custom_run=True, strict=True)
async def r(ctx: ToolContext) -> str:
    session_id = _get_session_id(ctx)
    ope = _make_operator()
    await ope.init()
    logger.debug("开始检索记忆...")
    logger.debug(f"会话ID: {session_id}")
    try:
        res: QueryResult = await ope.query_notes(
            session_id,
            ctx.data["query"],
            top_k=ctx.data["top_k"],
            importance=ctx.data.get("importance"),
        )
        return json.dumps(res, ensure_ascii=False, indent=4)
    except Exception as e:
        logger.error(f"检索记忆时发生错误: {e}")
        return _err(f"检索记忆失败: {e!s}")


@on_tools(UPDATE_FUN, custom_run=True, strict=True)
async def update_memory(ctx: ToolContext) -> str:
    session_id = _get_session_id(ctx)
    ope = _make_operator()
    await ope.init()
    logger.debug("开始更新记忆...")
    logger.debug(f"会话ID: {session_id}")
    logger.debug(f"更新记忆ID: {ctx.data['id']}")

    try:
        result = await ope.get_all_notes(session_id, include=["metadatas", "documents"])
        if not result or not result.get("ids"):
            return _err("未找到指定ID的记忆")

        # 查找要更新的记忆
        ids = result["ids"]
        try:
            target_index = ids.index(ctx.data["id"])
        except ValueError:
            return _err("未找到指定ID的记忆")

        # 获取现有数据
        documents = result.get("documents") or []
        metadatas = result.get("metadatas") or []
        existing_doc = documents[target_index] if target_index < len(documents) else ""
        existing_meta = metadatas[target_index] if target_index < len(metadatas) else {}

        if not existing_meta:
            return _err("记忆元数据损坏")

        # 构建更新后的数据
        new_content = ctx.data.get("content", existing_doc)
        new_tags = ctx.data.get("tags", existing_meta.get("tags", ""))
        new_importance = ctx.data.get(
            "importance", existing_meta.get("importance", "medium")
        )

        # 保留原始创建时间
        created_at_str = existing_meta.get("created_at", "")
        if isinstance(created_at_str, str):
            try:
                created_at = datetime.fromisoformat(created_at_str)
            except ValueError:
                created_at = datetime.now()
        else:
            created_at = datetime.now()

        # 使用原生 update，原子操作，不会丢失数据
        meta = MemoryMetadata(
            memory_id=ctx.data["id"],
            importance=new_importance,
            tags=new_tags,
            created_at=created_at,
        )
        await ope.update_note(session_id, new_content, metadata=meta)

        return _ok(message="记忆更新成功", id=ctx.data["id"])
    except Exception as e:
        logger.error(f"更新记忆时发生错误: {e}")
        return _err(f"更新记忆失败: {e!s}")


@on_tools(DELETE_FUN, custom_run=True, strict=True)
async def delete_memory(ctx: ToolContext) -> str:
    session_id = _get_session_id(ctx)
    ope = _make_operator()
    await ope.init()
    logger.debug("开始删除记忆...")
    logger.debug(f"会话ID: {session_id}")
    logger.debug(f"删除记忆ID: {ctx.data['id']}")

    try:
        result = await ope.get_all_notes(session_id, include=["metadatas"])
        if (
            not result
            or not result.get("ids")
            or ctx.data["id"] not in (result.get("ids") or [])
        ):
            return _err("未找到指定ID的记忆")

        await ope.delete_note(session_id, ctx.data["id"])
        return _ok(message="记忆删除成功", id=ctx.data["id"])
    except Exception as e:
        logger.error(f"删除记忆时发生错误: {e}")
        return _err(f"删除记忆失败: {e!s}")


@on_tools(LIST_MEMORY_FUN, custom_run=True, strict=True)
async def list_memory(ctx: ToolContext) -> str:
    session_id = _get_session_id(ctx)
    ope = _make_operator()
    await ope.init()
    logger.debug("开始列出记忆...")
    logger.debug(f"会话ID: {session_id}")

    try:
        limit = ctx.data.get("limit", 5)
        result = await ope.get_all_notes(session_id, include=["metadatas", "documents"])

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
                    "tags": meta.get("tags", ""),
                    "importance": meta.get("importance", "medium"),
                    "content_preview": doc[:50] + "..." if len(doc) > 50 else doc,
                }
            )

        return _ok(memories=memories, total=len(ids))
    except Exception as e:
        logger.error(f"列出记忆时发生错误: {e}")
        return _err(f"列出记忆失败: {e!s}")
