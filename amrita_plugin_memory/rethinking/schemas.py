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
    description="结束本次潜意识推理循环。必须在本轮所有整理工作完成后调用。",
    parameters=FunctionParametersSchema(
        type="object",
        properties={
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

# ── 全局知识库工具 ──

KNOWLEDGE_LIST_SCHEMA = FunctionDefinitionSchema(
    name="subconscious_knowledge_list",
    description="列出全局知识库中的所有知识条目（不含正文），返回 id、标题、摘要预览、行数、时间戳",
    parameters=FunctionParametersSchema(type="object", properties={}, required=[]),
)

KNOWLEDGE_READ_SCHEMA = FunctionDefinitionSchema(
    name="subconscious_knowledge_read",
    description=(
        "读取指定知识条目的完整内容，支持按行滑动读取。"
        "首次读取不传 start_line/end_line 获取全文；"
        "内容过长时分段读取，用 start_line 和 end_line 控制窗口"
    ),
    parameters=FunctionParametersSchema(
        type="object",
        properties={
            "kid": FunctionPropertySchema(type="string", description="知识条目ID"),
            "start_line": FunctionPropertySchema(
                type="integer", description="起始行号（0-based），默认从第一行开始"
            ),
            "end_line": FunctionPropertySchema(
                type="integer", description="结束行号（不含），默认到末尾"
            ),
        },
        required=["kid"],
    ),
)

KNOWLEDGE_CREATE_SCHEMA = FunctionDefinitionSchema(
    name="subconscious_knowledge_create",
    description="创建新的全局知识条目。摘要用于语义搜索，正文用于详细阅读。单条正文不超过10000字符",
    parameters=FunctionParametersSchema(
        type="object",
        properties={
            "title": FunctionPropertySchema(
                type="string", description="知识标题，简洁明了"
            ),
            "summary": FunctionPropertySchema(
                type="string", description="知识摘要（≤200字），会被向量化用于语义搜索"
            ),
            "body": FunctionPropertySchema(
                type="string", description="知识正文，完整详细内容"
            ),
        },
        required=["title", "summary", "body"],
    ),
)

KNOWLEDGE_UPDATE_SCHEMA = FunctionDefinitionSchema(
    name="subconscious_knowledge_update",
    description="更新已有知识条目。至少传一个可选字段。若更新摘要则重新向量化",
    parameters=FunctionParametersSchema(
        type="object",
        properties={
            "kid": FunctionPropertySchema(
                type="string", description="要更新的知识条目ID"
            ),
            "title": FunctionPropertySchema(type="string", description="新的标题"),
            "summary": FunctionPropertySchema(type="string", description="新的摘要"),
            "body": FunctionPropertySchema(type="string", description="新的正文"),
        },
        required=["kid"],
    ),
)

KNOWLEDGE_DELETE_SCHEMA = FunctionDefinitionSchema(
    name="subconscious_knowledge_delete",
    description="删除指定ID的全局知识条目（同时删除文件和向量）",
    parameters=FunctionParametersSchema(
        type="object",
        properties={
            "kid": FunctionPropertySchema(
                type="string", description="要删除的知识条目ID"
            )
        },
        required=["kid"],
    ),
)

KNOWLEDGE_SEARCH_SCHEMA = FunctionDefinitionSchema(
    name="subconscious_knowledge_search",
    description="在全局知识库中语义搜索相关条目（仅匹配摘要），返回最相关的条目及其摘要",
    parameters=FunctionParametersSchema(
        type="object",
        properties={
            "query": FunctionPropertySchema(
                type="string", description="搜索查询，自然语言描述想找什么"
            ),
            "top_k": FunctionPropertySchema(
                type="integer", description="返回数量，默认5条"
            ),
        },
        required=["query"],
    ),
)

# ── Session 与用户画像工具 ──

READ_SESSIONS_SCHEMA = FunctionDefinitionSchema(
    name="subconscious_read_sessions",
    description=(
        "读取目标用户最近的归档会话列表（含摘要），了解用户近期在聊什么话题。"
        "每轮推理开始时先调用此工具掌握用户动态，再决定是否需要整理记忆"
    ),
    parameters=FunctionParametersSchema(
        type="object",
        properties={
            "n": FunctionPropertySchema(
                type="integer", description="返回最近 N 个会话，默认5"
            ),
        },
        required=[],
    ),
)

GET_PROFILE_SCHEMA = FunctionDefinitionSchema(
    name="subconscious_get_profile",
    description=(
        "读取用户画像：摘要（向量化可用）和完整正文。用 total_lines 判断是否需要分段读取。"
        "返回的 body 只包含当前窗口的内容——content 字段为实际返回的行段"
    ),
    parameters=FunctionParametersSchema(
        type="object",
        properties={
            "start_line": FunctionPropertySchema(
                type="integer", description="起始行号（0-based），默认全文"
            ),
            "end_line": FunctionPropertySchema(
                type="integer", description="结束行号（不含），默认全文"
            ),
        },
        required=[],
    ),
)

UPDATE_PROFILE_SCHEMA = FunctionDefinitionSchema(
    name="subconscious_update_profile",
    description=(
        "增量更新用户画像。省略 start_line/end_line 时追加到末尾；"
        "指定时替换 [start_line, end_line) 之间的行。"
        "渐进式构建——每次只修改有变化的部分"
    ),
    parameters=FunctionParametersSchema(
        type="object",
        properties={
            "summary": FunctionPropertySchema(
                type="string", description="画像摘要（≤200字），用于语义检索"
            ),
            "start_line": FunctionPropertySchema(
                type="integer", description="起始行号（0-based），省略时追加到末尾"
            ),
            "end_line": FunctionPropertySchema(
                type="integer", description="结束行号（不含），省略时追加到末尾"
            ),
            "new_lines": FunctionPropertySchema(
                type="string",
                description="新内容（多行文本，用换行符分隔）",
            ),
        },
        required=["summary", "new_lines"],
    ),
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
        KNOWLEDGE_LIST_SCHEMA,
        KNOWLEDGE_READ_SCHEMA,
        KNOWLEDGE_CREATE_SCHEMA,
        KNOWLEDGE_UPDATE_SCHEMA,
        KNOWLEDGE_DELETE_SCHEMA,
        KNOWLEDGE_SEARCH_SCHEMA,
        READ_SESSIONS_SCHEMA,
        GET_PROFILE_SCHEMA,
        UPDATE_PROFILE_SCHEMA,
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
