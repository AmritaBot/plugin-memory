from amrita_core import (
    FunctionDefinitionSchema,
    FunctionParametersSchema,
    FunctionPropertySchema,
    on_tools,
)

WRITE_MEMORY_FUN = FunctionDefinitionSchema(
    name="write_memory",
    description="将当前用户的重要信息存入长期记忆",
    parameters=FunctionParametersSchema(
        type="object",
        properties={
            "content": FunctionPropertySchema(
                type="string", description="记忆内容，简洁明了"
            ),
            "tags": FunctionPropertySchema(
                type="array",
                items=FunctionPropertySchema(
                    type="string",
                    description="标签名称",
                ),
                description="分类标签，比如`preference`,`project`",
            ),
            "importance": FunctionPropertySchema(
                type="string",
                items=FunctionPropertySchema(
                    type="string", description="重要性等级`['low', 'medium', 'high']`"
                ),
                description="重要性等级",
                enum=["low", "medium", "high"],
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
                type="array",
                description="字符串列表，表示要检索的关键词词",
                items=FunctionPropertySchema(type="string", description="关键词"),
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
                items=FunctionPropertySchema(
                    type="string", description="重要性等级`['low', 'medium', 'high']`"
                ),
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

# TODO: Tool.