"""时间记忆退化：软衰减（检索重排）与硬分层（降级 / 清理）。

本模块只放纯逻辑，不碰 I/O，便于单独验证。
- 软衰减：检索时按 created_at 的年龄压低得分，不删数据。
- 硬分层：按 importance 决定寿命阈值，超龄先降级、降到 low 再清理。
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any, Literal

Importance = Literal["low", "medium", "high"]

#: importance 的序，越大寿命越长
IMPORTANCE_RANK: dict[str, int] = {"low": 0, "medium": 1, "high": 2}
#: 降级链，走到尽头即清理
DEMOTE_CHAIN: dict[str, str] = {"high": "medium", "medium": "low"}

_SECONDS_PER_DAY = 86400.0


def parse_created_at(value: Any) -> datetime | None:
    """解析元数据里的 created_at，统一成 aware UTC；不可解析返回 None。"""
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str) and value:
        try:
            dt = datetime.fromisoformat(value)
        except ValueError:
            return None
    else:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def age_days(created_at: datetime, now: datetime) -> float:
    """年龄（天），未来时间按 0 处理。"""
    return max((now - created_at).total_seconds() / _SECONDS_PER_DAY, 0.0)


def importance_half_life(base_days: float, importance: str, boost: float) -> float:
    """importance 越高，有效半衰期越长；low 为基准。"""
    return base_days * (1.0 + boost * IMPORTANCE_RANK.get(importance, 1))


def decay_factor(age: float, half_life: float, floor: float) -> float:
    """指数衰减因子，下限为 floor，避免旧记忆权重归零。"""
    if half_life <= 0:
        return 1.0
    return max(0.5 ** (age / half_life), floor)


def similarity_from_distance(distance: float) -> float:
    """把 Chroma 距离映射到 (0,1]，对 l2 / cosine 都单调。"""
    try:
        d = float(distance)
    except (TypeError, ValueError):
        d = 0.0
    return 1.0 / (1.0 + max(d, 0.0))


def decay_score(
    similarity: float,
    age: float,
    importance: str,
    base_half_life: float,
    boost: float,
    floor: float,
) -> float:
    """相似度 × 时间衰减因子。"""
    half_life = importance_half_life(base_half_life, importance, boost)
    return similarity * decay_factor(age, half_life, floor)


def tier_days(importance: str, short_days: float, long_days: float) -> float:
    """该 importance 层的寿命阈值（天）。medium 取 short/long 的几何中值。

    两个配置项写反时按大小重排，保证 low ≤ medium ≤ high 单调，
    否则 high 会比 medium 先过期。
    """
    low_days, high_days = sorted((float(short_days), float(long_days)))
    if importance == "low":
        return low_days
    if importance == "high":
        return high_days
    if high_days > low_days:
        return math.sqrt(low_days * high_days)
    return low_days


def plan_action(
    metadata: dict, now: datetime, short_days: float, long_days: float
) -> tuple[str, str | None]:
    """给出单条记忆的退化动作。

    Returns:
        (动作, 新 importance)，动作 ∈ {"keep", "demote", "purge"}。
        仅 demote 会带新的 importance。
    """
    created = parse_created_at(metadata.get("created_at"))
    if created is None:
        return "keep", None
    importance = str(metadata.get("importance", "medium"))
    if importance not in IMPORTANCE_RANK:
        importance = "medium"
    if age_days(created, now) <= tier_days(importance, short_days, long_days):
        return "keep", None
    nxt = DEMOTE_CHAIN.get(importance)
    if nxt is None:
        return "purge", None
    return "demote", nxt


def _inner_list(value: Any, size: int) -> list:
    """取 QueryResult 里 list-of-list 的内层；兼容 numpy 数组；缺失时补 None。"""
    if hasattr(value, "tolist"):  # chroma 的 embeddings 可能是 ndarray
        value = value.tolist()
    if isinstance(value, list) and value and isinstance(value[0], list):
        inner = list(value[0])
    else:
        inner = []
    if len(inner) < size:
        inner.extend([None] * (size - len(inner)))
    return inner


def rerank_query_result(
    result: Mapping[str, Any],
    top_k: int,
    base_half_life: float,
    boost: float,
    floor: float,
    now: datetime | None = None,
) -> dict[str, Any]:
    """按衰减得分重排 QueryResult，并截断到 top_k；meta 里回填 decay_score。"""
    now = now or datetime.now(timezone.utc)
    ids_nested = result.get("ids")
    if not ids_nested or not ids_nested[0]:
        return dict(result)
    ids = list(ids_nested[0])
    size = len(ids)
    metas = _inner_list(result.get("metadatas"), size)
    dists = _inner_list(result.get("distances"), size)

    scores: list[float] = []
    for i in range(size):
        meta = metas[i] if isinstance(metas[i], dict) else {}
        created = parse_created_at(meta.get("created_at"))
        age = age_days(created, now) if created else 0.0
        similarity = similarity_from_distance(dists[i])
        scores.append(
            decay_score(
                similarity,
                age,
                str(meta.get("importance", "medium")),
                base_half_life,
                boost,
                floor,
            )
        )

    order = sorted(range(size), key=lambda i: scores[i], reverse=True)[:top_k]

    out = dict(result)
    for key in ("ids", "documents", "metadatas", "distances", "embeddings"):
        value = result.get(key)
        if value is None:
            continue
        inner = _inner_list(value, size)
        out[key] = [[inner[i] for i in order]]

    if result.get("metadatas") is not None:
        reordered = []
        for i in order:
            meta = dict(metas[i]) if isinstance(metas[i], dict) else {}
            meta["decay_score"] = round(scores[i], 4)
            reordered.append(meta)
        out["metadatas"] = [reordered]
    return out
