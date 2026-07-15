"""潜意识工具 Schema 定义。"""

from amrita_core.tools.models import (
    FunctionDefinitionSchema,
    FunctionParametersSchema,
    FunctionPropertySchema,
    ToolFunctionSchema,
)

READ_MEMORY_SCHEMA = FunctionDefinitionSchema(
    name="subconscious_read_memory",
    description="从当前用户的记忆库中检索相关信息",
    parameters=FunctionParametersSchema(
        type="object",
        properties={
            "query": FunctionPropertySchema(
                type="string", description="搜索关键词用空格分割"
            ),
            "top_k": FunctionPropertySchema(
                type="integer", description="返回数量默认5条", default=5
            ),
            "importance": FunctionPropertySchema(
                type="string",
                description="重要性过滤可选",
                enum=["low", "medium", "high"],
                nullable=True,
            ),
        },
        required=["query"],
    ),
)

WRITE_MEMORY_SCHEMA = FunctionDefinitionSchema(
    name="subconscious_write_memory",
    description="将当前用户的重要信息存入长期记忆",
    parameters=FunctionParametersSchema(
        type="object",
        properties={
            "content": FunctionPropertySchema(
                type="string", description="记忆内容简洁明了"
            ),
            "tags": FunctionPropertySchema(
                type="string", description="分类标签如 preference、project"
            ),
            "importance": FunctionPropertySchema(
                type="string", description="重要性等级", enum=["low", "medium", "high"]
            ),
        },
        required=["content", "tags", "importance"],
    ),
)

UPDATE_MEMORY_SCHEMA = FunctionDefinitionSchema(
    name="subconscious_update_memory",
    description="更新指定ID的记忆内容",
    parameters=FunctionParametersSchema(
        type="object",
        properties={
            "id": FunctionPropertySchema(type="string", description="要更新的记忆ID"),
            "content": FunctionPropertySchema(
                type="string", description="新的记忆内容"
            ),
            "tags": FunctionPropertySchema(type="string", description="新的标签"),
            "importance": FunctionPropertySchema(
                type="string", description="重要性等级", enum=["low", "medium", "high"]
            ),
        },
        required=["id"],
    ),
)

DELETE_MEMORY_SCHEMA = FunctionDefinitionSchema(
    name="subconscious_delete_memory",
    description="删除指定ID的记忆",
    parameters=FunctionParametersSchema(
        type="object",
        properties={
            "id": FunctionPropertySchema(type="string", description="要删除的记忆ID")
        },
        required=["id"],
    ),
)

LIST_MEMORY_SCHEMA = FunctionDefinitionSchema(
    name="subconscious_list_memory",
    description="列出当前用户的所有记忆",
    parameters=FunctionParametersSchema(
        type="object",
        properties={
            "limit": FunctionPropertySchema(
                type="integer", description="返回数量默认10条", default=10
            )
        },
        required=[],
    ),
)

ITER_STOP_SCHEMA = FunctionDefinitionSchema(
    name="subconscious_iter_stop",
    description="结束本次潜意识推理循环。必须在本轮所有整理工作完成后调用。设置下次运行时间。",
    parameters=FunctionParametersSchema(
        type="object",
        properties={
            "next_time": FunctionPropertySchema(
                type="string", description="下次运行的 ISO 8601 时间戳", nullable=True
            ),
            "delay_seconds": FunctionPropertySchema(
                type="integer", description="距现在多少秒后运行", nullable=True
            ),
            "summary": FunctionPropertySchema(
                type="string", description="本轮做了什么工作的简要摘要"
            ),
        },
        required=["summary"],
    ),
)

SEND_TO_USER_SCHEMA = FunctionDefinitionSchema(
    name="subconscious_send_to_user",
    description=(
        "向用户主动发送消息。仅在确定需要与用户交流时使用。每轮最多调用一次。"
        "调用后本轮不再整理记忆应立即 iter_stop。"
        "何时使用：用户长时间未上线、忆起重要往事想分享、想询问用户近况。"
        "何时不用：只是整理记忆时、用户刚聊过天时、没有特别的交流欲望时。"
    ),
    parameters=FunctionParametersSchema(
        type="object",
        properties={
            "intent": FunctionPropertySchema(
                type="string", description="想对用户说的话的意图摘要"
            ),
            "memory_context": FunctionPropertySchema(
                type="string", description="相关的记忆上下文", default=""
            ),
        },
        required=["intent"],
    ),
)

READ_CHAT_CONTEXT_SCHEMA = FunctionDefinitionSchema(
    name="subconscious_read_chat_context",
    description="获取当前用户最近的聊天记录上下文。",
    parameters=FunctionParametersSchema(
        type="object",
        properties={
            "limit": FunctionPropertySchema(
                type="integer", description="返回最近多少条消息默认10", default=10
            )
        },
        required=[],
    ),
)

# 工具注册常量

DUPLICATE_HELPER_SCHEMA = FunctionDefinitionSchema(
    name="subconscious_duplicate_helper",
    description=(
        "获取记忆库中指定范围的全部记忆条目，返回压缩指导 prompt。"
        "调用后应仔细分析返回内容中重复/高度相似的条目，"
        "用 subconscious_update_memory 合并精炼，subconscious_delete_memory 删除冗余。"
        "仅在需要主动整理/压缩记忆库时调用，不要每轮都调用。"
    ),
    parameters=FunctionParametersSchema(
        type="object",
        properties={
            "tag": FunctionPropertySchema(
                type="string", description="按标签过滤，不传则返回全部"
            ),
            "importance": FunctionPropertySchema(
                type="string",
                enum=["low", "medium", "high"],
                description="按重要性过滤",
            ),
            "sort_by": FunctionPropertySchema(
                type="string",
                enum=["created_at", "importance", "length"],
                default="created_at",
                description="排序方式",
            ),
        },
        required=[],
    ),
)

GET_MEMORY_STATS_SCHEMA = FunctionDefinitionSchema(
    name="subconscious_get_memory_stats",
    description="获取记忆库统计概览：总数、重要性分布、标签分布、时间范围。用于判断是否需要压缩。",
    parameters=FunctionParametersSchema(type="object", properties={}, required=[]),
)

_SUBCONSCIOUS_TOOL_FUNCTIONS: list[ToolFunctionSchema] = [
    ToolFunctionSchema(function=schema, type="function", strict=True)
    for schema in [
        READ_MEMORY_SCHEMA,
        WRITE_MEMORY_SCHEMA,
        UPDATE_MEMORY_SCHEMA,
        DELETE_MEMORY_SCHEMA,
        LIST_MEMORY_SCHEMA,
        ITER_STOP_SCHEMA,
        DUPLICATE_HELPER_SCHEMA,
        GET_MEMORY_STATS_SCHEMA,
    ]
]

_SEND_TOOL_SCHEMA = ToolFunctionSchema(
    function=SEND_TO_USER_SCHEMA, type="function", strict=False
)

_CHAT_CONTEXT_TOOL_SCHEMA = ToolFunctionSchema(
    function=READ_CHAT_CONTEXT_SCHEMA, type="function", strict=True
)

_SUBCONSCIOUS_TOOL_NAMES = {s.function.name for s in _SUBCONSCIOUS_TOOL_FUNCTIONS} | {
    "subconscious_send_to_user",
    "subconscious_read_chat_context",
}
