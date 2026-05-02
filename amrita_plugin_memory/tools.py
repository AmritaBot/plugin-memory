import json

from amrita.plugins.chat.runtime import AmritaChatObject
from amrita_core import (
    FunctionDefinitionSchema,
    FunctionParametersSchema,
    FunctionPropertySchema,
    ToolContext,
    on_tools,
)
from nonebot import logger

from .vector import AsyncUserMemory, MemoryMetadata, get_db_conn

WRITE_MEMORY_FUN = FunctionDefinitionSchema(
    name="write_memory",
    description="将当前会话（群或者用户）的重要信息存入长期记忆",
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
            "group_if": FunctionPropertySchema(
                type="boolean",
                description="如果你认为当前是在群内，并且此记忆是为群存入的，请将此参数设置为True，如果只是为最后一条消息的用户，请将此参数设置为False",
                default=False,
            ),
        },
        required=["content", "expiry_hint", "importance"],
    ),
)
READ_MEMORY_FUN = FunctionDefinitionSchema(
    name="read_memory",
    description="从记忆库检索当前用户相关信息",
    parameters=FunctionParametersSchema(
        type="object",
        properties={
            "query": FunctionPropertySchema(
                type="string",
                description="字符串，用空格分割关键词",
            ),
            "limit": FunctionPropertySchema(
                type="integer", description="返回数量，默认5条"
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
                type="array",
                items=FunctionPropertySchema(type="string", description="标签名称"),
                description="新的标签列表（如不修改可不传）",
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
    description="列出当前用户的所有记忆(返回tag+id)",
    parameters=FunctionParametersSchema(
        type="object",
        properties={
            "limit": FunctionPropertySchema(
                type="integer", description="返回数量，默认5条", default=5
            ),
        },
        required=["limit"],
    ),
)


@on_tools(WRITE_MEMORY_FUN, custom_run=True, strict=True)
async def w(ctx: ToolContext):
    assert isinstance(ctx.ctx.chat_object, AmritaChatObject)
    nb_event = ctx.ctx.chat_object.event
    ope = AsyncUserMemory(get_db_conn())
    await ope.init()
    logger.debug("开始写入记忆...")
    logger.debug(f"会话ID: {nb_event.get_session_id()}")
    logger.debug(f"记忆Payload {ctx.data['content']}")
    meta = MemoryMetadata(
        importance=ctx.data["importance"],
        tags=ctx.data["tags"],
        user_type=(
            "group"
            if getattr(nb_event, "group_id", None) and ctx.data["group_if"]
            else "private"
        ),
    )
    await ope.add_note(
        nb_event.get_session_id(),
        ctx.data["content"],
        metadata=meta,
    )
    dmp = meta.model_dump()
    dmp["status"] = "success"
    return json.dumps(dmp, ensure_ascii=False, indent=4)
