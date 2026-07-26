"""共享类型定义。"""

from __future__ import annotations

from typing import TypedDict


class PendingMsg(TypedDict):
    """待发送消息条目。"""

    content: str
    timestamp: str


class KnowledgeEntry(TypedDict):
    """知识库索引条目——所有字段必须存在。"""

    kid: str
    title: str
    file_name: str
    summary: str
    total_lines: int
    char_count: int
    created_at: str
    updated_at: str


class KnowledgeListItem(TypedDict):
    """知识库列表返回值。"""

    kid: str
    title: str
    summary_preview: str
    total_lines: int
    char_count: int
    created_at: str
    updated_at: str


class KnowledgeReadResult(TypedDict, total=False):
    """知识库读取返回值（正常或错误）。"""

    kid: str
    title: str
    summary: str
    body_lines: list[str]
    total_lines: int
    range_start: int
    range_end: int
    error: str  # 仅在出错时存在


class KnowledgeSearchItem(TypedDict, total=False):
    """知识库搜索结果条目。"""

    kid: str
    title: str
    summary: str
    distance: float
    error: str  # 仅在出错时存在


class SessionSummary(TypedDict):
    """会话摘要返回值。"""

    id: int
    created_at: float
    summary: str
    message_count: int


class ProfileResult(TypedDict):
    """用户画像读取返回值。"""

    summary: str
    content: str
    total_lines: int
    range_start: int
    range_end: int
