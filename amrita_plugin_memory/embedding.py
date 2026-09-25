"""嵌入模型指纹 — 检测模型变更并触发 L2 向量层全量重映射。

指纹由嵌入配置计算（协议 / 模型名 / 服务地址），存放在集合 metadata 中，
因此随库走（远程 Chroma 同样生效）。

指纹不匹配意味着向量空间已改变，旧向量不再可比 —— 必须重建集合、
用当前模型重新嵌入全部记忆。重建路径对维度变化天然免疫，
所以无需探测向量维度。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import click
from amrita_core.libchat import call_embedding
from chromadb.api import ClientAPI
from chromadb.api.models.Collection import Collection
from chromadb.api.types import Metadata
from nonebot import logger

from .config import DATA_PATH, build_preset, env_config
from .keys import migrate_collection_keys
from .vector import MEMORY_COLLECTION_NAME, get_db_conn

FINGERPRINT_META = "embed_fingerprint"
EMBED_PROTOCOL_META = "embed_protocol"
EMBED_MODEL_META = "embed_model"
EMBED_BASE_URL_META = "embed_base_url"

BACKUP_DIR = DATA_PATH / "backups"


class _RefuseToLoad(RuntimeError):
    """内部信号 — 需要拒绝加载插件。不对外抛出。

    对外统一转成内建 ``RuntimeError``：插件加载失败时父包处于半导入状态，
    loguru 无法 pickle 本模块定义的异常类，会产生噪声堆栈。
    """


def compute_fingerprint() -> str:
    """由当前嵌入配置计算指纹。"""
    raw = "|".join(
        (
            env_config.embedding_proctol,
            env_config.embedding_model_name,
            env_config.embedding_model_url,
        )
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def describe_current() -> str:
    return (
        f"{env_config.embedding_proctol} / "
        f"{env_config.embedding_model_name} @ {env_config.embedding_model_url}"
    )


def _fingerprint_meta() -> Metadata:
    return {
        FINGERPRINT_META: compute_fingerprint(),
        EMBED_PROTOCOL_META: env_config.embedding_proctol,
        EMBED_MODEL_META: env_config.embedding_model_name,
        EMBED_BASE_URL_META: env_config.embedding_model_url,
    }


def _merge_meta(collection: Collection, **extra: Any) -> None:
    """合并写入集合 metadata（Chroma 的 modify 是整体替换）。"""
    collection.modify(metadata={**(collection.metadata or {}), **extra})


def write_fingerprint(collection: Collection) -> None:
    """把当前嵌入配置的指纹写入集合 metadata。"""
    _merge_meta(collection, **_fingerprint_meta())


def stored_fingerprint(collection: Collection) -> str | None:
    """读取集合中记录的指纹，不存在时返回 None。"""
    value = (collection.metadata or {}).get(FINGERPRINT_META)
    return value if isinstance(value, str) else None


def describe_stored(collection: Collection) -> str:
    meta = collection.metadata or {}
    protocol = meta.get(EMBED_PROTOCOL_META, "?")
    model = meta.get(EMBED_MODEL_META, "?")
    base_url = meta.get(EMBED_BASE_URL_META, "?")
    return f"{protocol} / {model} @ {base_url}"


def _prune_backups(keep: int) -> None:
    backups = sorted(BACKUP_DIR.glob("embed_backup_*.json"))
    for stale in backups[:-keep] if keep > 0 else backups:
        stale.unlink(missing_ok=True)


def backup_collection(collection: Collection) -> Path:
    """把集合全量内容落盘为 JSON 备份（重映射唯一的回滚手段）。"""
    result = collection.get(include=["documents", "metadatas"])
    payload = {
        "collection": collection.name,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "ids": result.get("ids") or [],
        "documents": result.get("documents") or [],
        "metadatas": result.get("metadatas") or [],
    }
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    path = BACKUP_DIR / f"embed_backup_{stamp}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    _prune_backups(env_config.reembed_backup_keep)
    logger.info(f"[Memory] Embedding backup written: {path}")
    return path


def list_backups() -> list[Path]:
    return sorted(BACKUP_DIR.glob("embed_backup_*.json"))


async def _write_in_batches(
    collection: Collection,
    ids: list[str],
    documents: list[str],
    metadatas: list[Metadata],
    *,
    batch_size: int,
    progress: Callable[[int, int], None] | None = None,
) -> int:
    """分批嵌入并写入集合。"""
    total = len(ids)
    done = 0
    for start in range(0, total, batch_size):
        chunk_ids = ids[start : start + batch_size]
        chunk_docs = documents[start : start + batch_size]
        chunk_metas = metadatas[start : start + batch_size]
        texts = [doc if isinstance(doc, str) else "" for doc in chunk_docs]
        vectors = await call_embedding(texts, build_preset())
        if len(vectors) != len(texts):
            raise RuntimeError(
                f"Embedding count mismatch: got {len(vectors)} for {len(texts)} texts"
            )
        collection.add(
            ids=chunk_ids,
            embeddings=[list(v.embedding) for v in vectors],
            documents=texts,
            metadatas=chunk_metas,
        )
        done += len(chunk_ids)
        if progress is not None:
            progress(done, total)
    return done


async def reindex_all(
    *,
    batch_size: int | None = None,
    progress: Callable[[int, int], None] | None = None,
    client: ClientAPI | None = None,
) -> int:
    """用当前嵌入模型重新嵌入全部记忆（删集合 → 重建 → 分批重嵌入）。

    Returns:
        重新嵌入的记忆条数。
    """
    client = client or get_db_conn()
    collection = client.get_or_create_collection(MEMORY_COLLECTION_NAME)
    result = collection.get(include=["documents", "metadatas"])
    ids: list[str] = list(result.get("ids") or [])
    documents: list[str] = list(result.get("documents") or [])
    metadatas: list[Metadata] = list(result.get("metadatas") or [])
    total = len(ids)

    # 先备份：这是删除集合后唯一的回滚依据
    if total:
        backup_collection(collection)

    # 空集合：只需重建并写指纹，无需调用嵌入模型
    if total == 0:
        client.delete_collection(MEMORY_COLLECTION_NAME)
        fresh = client.create_collection(MEMORY_COLLECTION_NAME)
        write_fingerprint(fresh)
        logger.info("[Memory] Collection was empty, fingerprint written")
        return 0

    client.delete_collection(MEMORY_COLLECTION_NAME)
    fresh = client.create_collection(MEMORY_COLLECTION_NAME)
    done = await _write_in_batches(
        fresh,
        ids,
        documents,
        metadatas,
        batch_size=batch_size or env_config.reembed_batch_size,
        progress=progress,
    )
    write_fingerprint(fresh)
    logger.info(f"[Memory] Reindexed {done} memories with {describe_current()}")
    return done


async def restore_from_backup(
    path: Path,
    *,
    batch_size: int | None = None,
    progress: Callable[[int, int], None] | None = None,
    client: ClientAPI | None = None,
) -> int:
    """从备份文件恢复集合（用当前嵌入模型重新嵌入）。

    Returns:
        恢复的记忆条数。
    """
    raw = await asyncio.to_thread(path.read_text, encoding="utf-8")
    payload = json.loads(raw)
    ids: list[str] = list(payload.get("ids") or [])
    documents: list[str] = list(payload.get("documents") or [])
    metadatas: list[Metadata] = list(payload.get("metadatas") or [])

    client = client or get_db_conn()
    existing = {c.name for c in client.list_collections()}
    if MEMORY_COLLECTION_NAME in existing:
        # 恢复前先备份当前状态，避免误操作不可逆
        backup_collection(client.get_collection(MEMORY_COLLECTION_NAME))
        client.delete_collection(MEMORY_COLLECTION_NAME)
    fresh = client.create_collection(MEMORY_COLLECTION_NAME)

    done = await _write_in_batches(
        fresh,
        ids,
        documents,
        metadatas,
        batch_size=batch_size or env_config.reembed_batch_size,
        progress=progress,
    )
    write_fingerprint(fresh)
    logger.info(f"[Memory] Restored {done} memories from {path.name}")
    return done


def _run_async(coro: Any) -> Any:
    """在同步上下文（import 期）执行协程；已有事件循环时给出明确指引。"""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    raise _RefuseToLoad(
        "检测到嵌入模型变更，但当前处于运行中的事件循环内，无法执行重映射。"
        "请在终端运行 `ambot memory reindex`。"
    )


def _confirm_reindex(reason: str, stored_desc: str) -> bool:
    """交互确认；非 TTY 时抛出 _RefuseToLoad。"""
    if not sys.stdin.isatty():
        raise _RefuseToLoad(
            f"{reason}\n"
            "当前为非交互环境，无法确认。请在终端运行 `ambot memory reindex` "
            "或设置 EMBED_MISMATCH_POLICY=auto / never，"
            "或设置 EMBED_CHECK_ON_STARTUP=false 跳过检查。"
        )
    return click.confirm(
        f"{reason}\n"
        f"  库中记录: {stored_desc}\n"
        f"  当前配置: {describe_current()}\n"
        "是否用当前模型重新嵌入全部记忆？（旧向量将与新模型不兼容）",
        default=True,
    )


def _do_reindex_with_progress() -> int:
    def _progress(done: int, total: int) -> None:
        click.echo(f"  重映射进度: {done}/{total}", err=True)

    return _run_async(reindex_all(progress=_progress))


def _check_fingerprint(collection: Collection) -> None:
    """指纹校验主逻辑（可能抛出 _RefuseToLoad）。"""
    current = compute_fingerprint()
    stored = stored_fingerprint(collection)

    # 空集合：直接采纳当前指纹，零打扰
    if collection.count() == 0:
        write_fingerprint(collection)
        logger.debug("[Memory] Empty collection, fingerprint written")
        return

    if stored == current:
        logger.debug("[Memory] Embedding fingerprint matches")
        return

    if stored is None:
        reason = "库中未记录嵌入模型信息，无法确认现有向量与当前配置是否一致。"
    else:
        reason = "嵌入模型已变更，现有向量与当前模型不兼容。"

    policy = env_config.embed_mismatch_policy
    if policy == "never":
        logger.warning(f"[Memory] {reason} 策略为 never，保持现有数据不变。")
        return
    if policy == "auto":
        logger.warning(f"[Memory] {reason} 策略为 auto，开始全量重映射。")
        _do_reindex_with_progress()
        return

    stored_desc = describe_stored(collection) if stored else "（无记录）"
    if not _confirm_reindex(reason, stored_desc):
        logger.warning("[Memory] 用户拒绝重映射，保持现有数据不变。")
        return
    _do_reindex_with_progress()


def run_startup_check() -> None:
    """启动检查：Key 迁移 + 嵌入指纹校验。在插件 import 期调用（同步）。

    Key 迁移与嵌入无关，始终执行（幂等且廉价）。指纹检查受
    ``EMBED_CHECK_ON_STARTUP`` 控制。

    仅当需要用户确认而环境不可交互时拒绝加载插件；其余异常
    （向量库不可达、嵌入服务故障等）只告警，不影响插件可用性。

    处于 ``ambot <cmd>`` 命令上下文（``AMBOT_COMMAND_CONTEXT``）时整体跳过：
    维护命令本就是为了修复状态，启动检查反过来会挡住它们（交互确认，
    甚至直接拒绝加载）。

    Raises:
        RuntimeError: 模型变更且无法确认时（插件将不被加载）。
    """
    if os.environ.get("AMBOT_COMMAND_CONTEXT"):
        logger.debug("[Memory] 命令上下文，跳过启动检查")
        return

    try:
        client = get_db_conn()
        collection = client.get_or_create_collection(MEMORY_COLLECTION_NAME)
    except Exception as e:
        logger.warning(f"[Memory] 启动检查跳过（无法连接向量库）：{e}")
        return

    try:
        migrate_collection_keys(collection)
    except Exception as e:
        logger.warning(f"[Memory] Key 迁移失败：{e}")

    if not env_config.embed_check_on_startup:
        logger.debug("[Memory] Embedding check disabled by config")
        return

    try:
        _check_fingerprint(collection)
    except _RefuseToLoad as e:
        logger.error(f"[Memory] 拒绝加载插件：{e}")
        raise RuntimeError(str(e)) from None
    except Exception as e:
        logger.warning(f"[Memory] 嵌入指纹检查跳过：{e}")
