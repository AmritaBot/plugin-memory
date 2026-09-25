"""``ambot memory`` 子命令：click 命令组 + 实现函数。

本模块只能在 Amrita 初始化、插件加载完成**之后**导入 —— 包 ``__init__`` 里的
``require(...)`` 需要插件加载器上下文（实测直接 ``import amrita_plugin_memory``
会抛 ``RuntimeError: Cannot load plugin "nonebot_plugin_localstore"!``）。

因此 entry point 声明为 ``amrita_plugin_memory.cli:memory [full_load]``：
``ambot-inlinectl`` 会先 ``amrita.init()`` + ``amrita.load_plugins()`` 再加载本
模块，并写入 ``AMBOT_COMMAND_CONTEXT=1``，使
:func:`amrita_plugin_memory.embedding.run_startup_check` 跳过启动检查 ——
否则维护命令可能被指纹确认挡在门外。
"""

from __future__ import annotations

import asyncio
import json
from collections import Counter
from pathlib import Path
from typing import Any

import click
from chromadb.api.models.Collection import Collection
from chromadb.api.types import Metadata

from .embedding import (
    backup_collection,
    compute_fingerprint,
    describe_current,
    describe_stored,
    list_backups,
    reindex_all,
    restore_from_backup,
    stored_fingerprint,
    write_fingerprint,
)
from .keys import KEY_SCHEMA_VERSION, KEY_SCHEMA_VERSION_META, migrate_collection_keys
from .vector import MEMORY_COLLECTION_NAME, get_db_conn


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _get_collection(*, create: bool = False) -> Collection | None:
    client = get_db_conn()
    names = {c.name for c in client.list_collections()}
    if MEMORY_COLLECTION_NAME not in names:
        if not create:
            return None
        return client.create_collection(MEMORY_COLLECTION_NAME)
    return client.get_collection(MEMORY_COLLECTION_NAME)


def _progress_printer() -> Any:
    def _progress(done: int, total: int) -> None:
        click.echo(f"  进度: {done}/{total}", err=True)

    return _progress


#  status


def cmd_status() -> None:
    """显示嵌入指纹、记忆分布与备份列表。"""
    collection = _get_collection()
    if collection is None:
        click.echo("记忆集合不存在（尚未写入任何记忆）")
    else:
        meta = collection.metadata or {}
        stored = stored_fingerprint(collection)
        current = compute_fingerprint()
        count = collection.count()

        click.echo(f"集合: {MEMORY_COLLECTION_NAME}")
        click.echo(f"记忆条数: {count}")
        click.echo(
            f"库中记录: {describe_stored(collection) if stored else '（无记录）'}"
        )
        click.echo(f"当前配置: {describe_current()}")
        click.echo(f"指纹: {stored or '（无记录）'}")
        if count == 0:
            click.echo("状态: 空集合（无需重映射，下次启动会写入指纹）")
        elif stored == current:
            click.echo("状态: ✅ 一致")
        else:
            click.echo("状态: ⚠️  不一致 —— 需运行 `ambot memory reindex`")

        key_ver = meta.get(KEY_SCHEMA_VERSION_META)
        if key_ver == KEY_SCHEMA_VERSION:
            click.echo(f"分区键版本: {key_ver}（最新）")
        else:
            click.echo(f"分区键版本: {key_ver or '（无记录）'} → 需迁移")

        _print_scope_counts(collection)

    backups = list_backups()
    if backups:
        click.echo(f"\n备份（{len(backups)} 份）:")
        for path in backups:
            click.echo(f"  {path.name}  ({path.stat().st_size // 1024} KB)")
    else:
        click.echo("\n备份: 无")


def _print_scope_counts(collection: Collection) -> None:
    result = collection.get(include=["metadatas"])
    metadatas: list[Metadata] = list(result.get("metadatas") or [])
    counter: Counter[str] = Counter()
    for meta in metadatas:
        scope_id = meta.get("user_id") if meta else None
        counter[str(scope_id) if scope_id is not None else "(未知)"] += 1
    if not counter:
        return
    click.echo("分布:")
    for scope_id, count in counter.most_common():
        click.echo(f"  {scope_id}: {count}")


#  reindex


def cmd_reindex(*, assume_yes: bool = False) -> None:
    """用当前嵌入模型全量重映射。"""
    collection = _get_collection()
    count = collection.count() if collection is not None else 0
    if count == 0:
        click.echo("记忆集合为空，无需重映射（将写入指纹）")
    else:
        click.echo(f"将重新嵌入 {count} 条记忆")
        click.echo(f"  从: {describe_stored(collection) if collection else '?'}")
        click.echo(f"  到: {describe_current()}")
        if not assume_yes and not click.confirm("确认继续？", default=False):
            raise click.Abort()

    done = _run(reindex_all(progress=_progress_printer()))
    click.echo(f"完成，已重新嵌入 {done} 条记忆")


#  migrate-keys


def cmd_migrate_keys(*, dry_run: bool = False) -> None:
    """把分区键迁移到当前 Amrita 的 uni_id 格式。"""
    client = get_db_conn()
    names = {c.name for c in client.list_collections()}
    if MEMORY_COLLECTION_NAME not in names:
        click.echo("记忆集合不存在，无需迁移")
        return
    collection = client.get_collection(MEMORY_COLLECTION_NAME)
    changed = migrate_collection_keys(collection, dry_run=dry_run)
    if dry_run:
        click.echo(f"[dry-run] 需要改写 {changed} 条记忆的分区键")
    else:
        click.echo(f"完成，已改写 {changed} 条记忆的分区键")


#  reset-fingerprint


def cmd_reset_fingerprint() -> None:
    """只写入当前指纹、不重嵌入（逃生舱）。"""
    collection = _get_collection(create=True)
    assert collection is not None
    write_fingerprint(collection)
    click.echo(f"已写入指纹: {compute_fingerprint()}")
    click.echo(f"  记录为: {describe_current()}")
    click.echo("⚠️  未重新嵌入 —— 若模型确实已变更，检索结果将不准确")


#  backup


def cmd_backup_list() -> None:
    """列出备份。"""
    backups = list_backups()
    if not backups:
        click.echo("无备份")
        return
    click.echo(f"备份（{len(backups)} 份）:")
    for path in backups:
        info = _backup_info(path)
        click.echo(
            f"  {path.name}  {info.get('count', '?')} 条  {info.get('created_at', '?')}"
        )


def _backup_info(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return {
        "count": len(payload.get("ids") or []),
        "created_at": payload.get("created_at", "?"),
        "collection": payload.get("collection", "?"),
    }


def cmd_backup_create() -> None:
    """手动创建一份备份。"""
    collection = _get_collection()
    if collection is None or collection.count() == 0:
        click.echo("记忆集合为空，无需备份")
        return
    path = backup_collection(collection)
    click.echo(f"已备份到 {path}")


def cmd_backup_restore(file: str) -> None:
    """从备份文件恢复（用当前嵌入模型重新嵌入）。"""
    path = Path(file).expanduser()
    if not path.is_file():
        raise click.ClickException(f"备份文件不存在: {path}")
    info = _backup_info(path)
    click.echo(f"从 {path.name} 恢复 {info.get('count', '?')} 条记忆")
    done = _run(restore_from_backup(path, progress=_progress_printer()))
    click.echo(f"完成，已恢复 {done} 条记忆")


#  click 命令组


@click.group()
def memory() -> None:
    """amrita_plugin_memory 维护命令"""


@memory.command("status")
def memory_status() -> None:
    """显示嵌入指纹、记忆分布与备份列表"""
    cmd_status()


@memory.command("reindex")
@click.option("--yes", is_flag=True, help="跳过确认")
def memory_reindex(yes: bool) -> None:
    """用当前嵌入模型全量重映射"""
    cmd_reindex(assume_yes=yes)


@memory.command("migrate-keys")
@click.option("--dry-run", is_flag=True, help="只统计不改写")
def memory_migrate_keys(dry_run: bool) -> None:
    """迁移分区键到当前 Amrita 的 uni_id 格式"""
    cmd_migrate_keys(dry_run=dry_run)


@memory.command("reset-fingerprint")
def memory_reset_fingerprint() -> None:
    """只写入当前指纹，不重新嵌入（逃生舱）"""
    cmd_reset_fingerprint()


@memory.group("backup")
def memory_backup() -> None:
    """备份管理"""


@memory_backup.command("list")
def memory_backup_list() -> None:
    """列出备份"""
    cmd_backup_list()


@memory_backup.command("create")
def memory_backup_create() -> None:
    """手动创建一份备份"""
    cmd_backup_create()


@memory_backup.command("restore")
@click.argument("file")
def memory_backup_restore(file: str) -> None:
    """从备份文件恢复（用当前嵌入模型重新嵌入）"""
    cmd_backup_restore(file)
