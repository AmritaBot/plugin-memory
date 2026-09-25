"""L2 向量层分区键 — 统一跟随已安装 Amrita 的 uni_id 格式。

Amrita 的会话 ID 存在以下两种格式，本模块需要同时识别：

- ``user_{qq}`` / ``group_{群号}``
- ``QQPlatform_Private_{qq}`` / ``QQPlatform_Group_{群号}``

本模块**不硬编码任一格式**，而是委托框架的 ``make_uni_id`` 生成，
并提供一个能识别两种格式的解析器，用于存量数据的 Key 迁移。
"""

from __future__ import annotations

import re
from typing import Literal

from amrita.plugins.chat.utils.sql import make_uni_id
from chromadb.api.models.Collection import Collection
from chromadb.api.types import Metadata
from nonebot import logger
from nonebot.adapters.onebot.v11 import Event as OB11Event

Scope = Literal["group", "user"]

# 集合 metadata 中的 Key 体系版本号，用于幂等迁移
KEY_SCHEMA_VERSION = 2
KEY_SCHEMA_VERSION_META = "key_schema_version"

_GROUP_KINDS = {"group", "Group"}
_PRIVATE_KINDS = {"user", "Private"}

# 同时匹配两种格式（可选平台前缀 + 类型 + 数字 payload）
_ANY_ID_RE = re.compile(
    r"^(?:[A-Za-z0-9]+_)?(?P<kind>Private|Group|Channel|user|group)_(?P<payload>[0-9]+)$"
)


def make_scope_id(payload: int | str, *, is_group: bool) -> str:
    """生成分区键 — 委托框架，自动跟随已安装 Amrita 的格式。"""
    return make_uni_id(int(payload), is_group=is_group)


def resolve_scope_uni_id(event: OB11Event, scope: str) -> str:
    """根据 scope 解析出分区键。

    - ``scope="group"``: 群共享记忆
    - ``scope="user"``:  用户专属记忆（群聊私聊互通）
    """
    if scope == "group":
        group_id = getattr(event, "group_id", None)
        if group_id is None:
            raise ValueError("当前不在群聊中，无法使用群共享记忆")
        return make_scope_id(group_id, is_group=True)
    if scope == "user":
        user_id = getattr(event, "user_id", None)
        if user_id is None:
            raise ValueError("Event has no user_id attribute")
        return make_scope_id(user_id, is_group=False)
    raise ValueError(f"无效的 scope: {scope}")


def to_current_format(raw: str) -> str | None:
    """把任意历史格式的分区键规范化到当前框架格式。

    已是当前格式时返回原值（幂等）；无法识别或本插件未使用的类型返回 None。
    """
    match = _ANY_ID_RE.match(raw)
    if match is None:
        return None
    kind = match.group("kind")
    payload = match.group("payload")
    if kind in _GROUP_KINDS:
        return make_scope_id(payload, is_group=True)
    if kind in _PRIVATE_KINDS:
        return make_scope_id(payload, is_group=False)
    return None


def needs_key_migration(collection: Collection) -> bool:
    """判断集合是否需要 Key 迁移。"""
    meta = collection.metadata or {}
    return meta.get(KEY_SCHEMA_VERSION_META) != KEY_SCHEMA_VERSION


def migrate_collection_keys(collection: Collection, *, dry_run: bool = False) -> int:
    """把集合内全部 ``user_id`` 元数据改写为当前框架格式。

    只改 metadata，不重算向量（embedding 与 metadata 解耦）。
    幂等：靠集合 metadata 的 ``key_schema_version`` 标记，已迁移则直接返回。

    Returns:
        被改写的记忆条数。
    """
    meta = collection.metadata or {}
    if meta.get(KEY_SCHEMA_VERSION_META) == KEY_SCHEMA_VERSION:
        return 0

    result = collection.get(include=["metadatas"])
    ids = result.get("ids") or []
    metadatas = result.get("metadatas") or []

    target_ids: list[str] = []
    target_metas: list[Metadata] = []
    for doc_id, raw_meta in zip(ids, metadatas):
        if not raw_meta:
            continue
        current = raw_meta.get("user_id")
        if not isinstance(current, str):
            continue
        converted = to_current_format(current)
        if converted is None or converted == current:
            continue
        target_ids.append(doc_id)
        target_metas.append({**raw_meta, "user_id": converted})

    if dry_run:
        return len(target_ids)

    if target_ids:
        collection.update(ids=target_ids, metadatas=target_metas)
        logger.info(f"[Memory] Key migrated for {len(target_ids)} memories")
    collection.modify(metadata={**meta, KEY_SCHEMA_VERSION_META: KEY_SCHEMA_VERSION})
    return len(target_ids)
