"""用户画像文件（Markdown）的路径与解析。

会话侧只读入口与潜意识循环共用同一份解析逻辑，
避免为读取画像而依赖运行器实例。
"""

from __future__ import annotations

from pathlib import Path
from typing import TypedDict

from nonebot import logger

from .config import DATA_PATH


class ProfileResult(TypedDict):
    """用户画像读取返回值。"""

    summary: str
    content: str
    total_lines: int
    range_start: int
    range_end: int


#: 画像文件路径
PROFILE_PATH: Path = DATA_PATH / "user_profile.md"


def read_profile(
    path: Path,
    start_line: int | None = None,
    end_line: int | None = None,
) -> ProfileResult:
    """解析画像 Markdown（summary + "---" + body），按行返回滑动窗口。

    与运行器解耦：潜意识循环未启动时，会话侧也能读到既有画像文件。
    """
    empty: ProfileResult = {
        "summary": "",
        "content": "",
        "total_lines": 0,
        "range_start": 0,
        "range_end": 0,
    }
    if not path.exists():
        return empty
    try:
        text = path.read_text(encoding="utf-8")
        parts = text.split("---", 1)
        summary = parts[0].strip() if parts else ""
        body = parts[1].strip() if len(parts) > 1 else ""
        body_lines = body.split("\n") if body else []
        total = len(body_lines)
        s = 0 if start_line is None else max(0, start_line)
        e = total if end_line is None else max(s, min(total, end_line))
        return {
            "summary": summary,
            "content": "\n".join(body_lines[s:e]),
            "total_lines": total,
            "range_start": s,
            "range_end": e,
        }
    except Exception as e:
        logger.warning(f"[Subconscious] Read profile failed: {e}")
        return empty
