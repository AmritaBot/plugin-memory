import json
from datetime import datetime

from amrita.plugins.chat.runtime import AmritaChatObject
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
    assert isinstance(ctx.ctx.chat_object, AmritaChatObject)
    nb_event = ctx.ctx.chat_object.event
    ope = AsyncUserMemory(get_db_conn())
    await ope.init()
    logger.debug("开始写入记忆...")
    logger.debug(f"会话ID: {nb_event.get_session_id()}")
    logger.debug(f"记忆Payload {ctx.data['content']}")
    if (
        await ope.count_user_notes(nb_event.get_session_id())
        > (await DataManager().safe_get_config()).per_session_memory_limit
    ):
        return json.dumps(
            {
                "status": "error",
                "message": "当前会话的记忆数量已超过限制，请清理后再试",
            },
            ensure_ascii=False,
            indent=4,
        )
    meta = MemoryMetadata(
        importance=ctx.data["importance"],
        tags=ctx.data["tags"],
    )
    await ope.add_note(
        nb_event.get_session_id(),
        ctx.data["content"],
        metadata=meta,
    )
    dmp = meta.model_dump()
    dmp["status"] = "success"
    return json.dumps(dmp, ensure_ascii=False, indent=4)


@on_tools(READ_MEMORY_FUN, custom_run=True, strict=True)
async def r(ctx: ToolContext) -> str:
    assert isinstance(ctx.ctx.chat_object, AmritaChatObject)
    nb_event = ctx.ctx.chat_object.event
    ope = AsyncUserMemory(get_db_conn())
    await ope.init()
    logger.debug("开始检索记忆...")
    logger.debug(f"会话ID: {nb_event.get_session_id()}")
    res: QueryResult = await ope.query_notes(
        nb_event.get_session_id(),
        ctx.data["query"],
        top_k=ctx.data["top_k"],
        importance=ctx.data.get("importance"),
    )
    return json.dumps(res, ensure_ascii=False, indent=4)


@on_tools(UPDATE_FUN, custom_run=True, strict=True)
async def update_memory(ctx: ToolContext) -> str:
    assert isinstance(ctx.ctx.chat_object, AmritaChatObject)
    nb_event = ctx.ctx.chat_object.event
    ope = AsyncUserMemory(get_db_conn())
    await ope.init()
    logger.debug("开始更新记忆...")
    logger.debug(f"会话ID: {nb_event.get_session_id()}")
    logger.debug(f"更新记忆ID: {ctx.data['id']}")

    try:
        result = await ope.get_all_notes(
            nb_event.get_session_id(), include=["metadatas", "documents"]
        )
        if not result or not result.get("ids"):
            return json.dumps(
                {
                    "status": "error",
                    "message": "未找到指定ID的记忆",
                },
                ensure_ascii=False,
                indent=4,
            )

        # 查找要更新的记忆
        target_index = -1
        ids = result["ids"]
        for i, doc_id in enumerate(ids):
            if doc_id == ctx.data["id"]:
                target_index = i
                break

        if target_index == -1:
            return json.dumps(
                {
                    "status": "error",
                    "message": "未找到指定ID的记忆",
                },
                ensure_ascii=False,
                indent=4,
            )

        # 获取现有数据，处理可能为None的情况
        documents = result.get("documents") or []
        metadatas = result.get("metadatas") or []

        existing_doc = documents[target_index] if target_index < len(documents) else ""
        existing_meta = metadatas[target_index] if target_index < len(metadatas) else {}

        # 确保有必要的元数据
        if not existing_meta:
            return json.dumps(
                {
                    "status": "error",
                    "message": "记忆元数据损坏",
                },
                ensure_ascii=False,
                indent=4,
            )

        # 构建更新后的数据
        new_content = ctx.data.get("content", existing_doc)
        new_tags = ctx.data.get("tags", existing_meta.get("tags", ""))
        new_importance = ctx.data.get(
            "importance", existing_meta.get("importance", "medium")
        )

        # 处理创建时间
        created_at_str = existing_meta.get("created_at", "")
        if isinstance(created_at_str, str) and created_at_str:
            # 处理ISO格式的时间字符串
            if created_at_str.endswith("Z"):
                created_at_str = created_at_str.replace("Z", "+00:00")
            created_at = datetime.fromisoformat(created_at_str)
        else:
            created_at = datetime.now()
        await ope.delete_note(nb_event.get_session_id(), ctx.data["id"])
        meta = MemoryMetadata(
            memory_id=ctx.data["id"],
            importance=new_importance,
            tags=new_tags,
            created_at=created_at,
        )
        await ope.add_note(
            nb_event.get_session_id(),
            new_content,
            metadata=meta,
        )

        return json.dumps(
            {"status": "success", "message": "记忆更新成功", "id": ctx.data["id"]},
            ensure_ascii=False,
            indent=4,
        )
    except Exception as e:
        logger.error(f"更新记忆时发生错误: {e}")
        return json.dumps(
            {
                "status": "error",
                "message": f"更新记忆失败: {e!s}",
            },
            ensure_ascii=False,
            indent=4,
        )


@on_tools(DELETE_FUN, custom_run=True, strict=True)
async def delete_memory(ctx: ToolContext) -> str:
    assert isinstance(ctx.ctx.chat_object, AmritaChatObject)
    nb_event = ctx.ctx.chat_object.event
    ope = AsyncUserMemory(get_db_conn())
    await ope.init()
    logger.debug("开始删除记忆...")
    logger.debug(f"会话ID: {nb_event.get_session_id()}")
    logger.debug(f"删除记忆ID: {ctx.data['id']}")

    try:
        # 检查记忆是否存在
        result = await ope.get_all_notes(
            nb_event.get_session_id(), include=["metadatas"]
        )
        if (
            not result
            or not result.get("ids")
            or ctx.data["id"] not in (result.get("ids") or [])
        ):
            return json.dumps(
                {
                    "status": "error",
                    "message": "未找到指定ID的记忆",
                },
                ensure_ascii=False,
                indent=4,
            )

        await ope.delete_note(nb_event.get_session_id(), ctx.data["id"])
        return json.dumps(
            {"status": "success", "message": "记忆删除成功", "id": ctx.data["id"]},
            ensure_ascii=False,
            indent=4,
        )
    except Exception as e:
        logger.error(f"删除记忆时发生错误: {e}")
        return json.dumps(
            {
                "status": "error",
                "message": f"删除记忆失败: {e!s}",
            },
            ensure_ascii=False,
            indent=4,
        )


@on_tools(LIST_MEMORY_FUN, custom_run=True, strict=True)
async def list_memory(ctx: ToolContext) -> str:
    assert isinstance(ctx.ctx.chat_object, AmritaChatObject)
    nb_event = ctx.ctx.chat_object.event
    ope = AsyncUserMemory(get_db_conn())
    await ope.init()
    logger.debug("开始列出记忆...")
    logger.debug(f"会话ID: {nb_event.get_session_id()}")

    try:
        limit = ctx.data.get("limit", 5)
        result = await ope.get_all_notes(
            nb_event.get_session_id(), include=["metadatas", "documents"]
        )

        if not result or not result.get("ids"):
            return json.dumps(
                {"status": "success", "memories": [], "total": 0},
                ensure_ascii=False,
                indent=4,
            )
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

        return json.dumps(
            {"status": "success", "memories": memories, "total": len(ids)},
            ensure_ascii=False,
            indent=4,
        )
    except Exception as e:
        logger.error(f"列出记忆时发生错误: {e}")
        return json.dumps(
            {
                "status": "error",
                "message": f"列出记忆失败: {e!s}",
            },
            ensure_ascii=False,
            indent=4,
        )
