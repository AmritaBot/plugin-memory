"""全局知识库管理器 — 文件 + JSON 索引 + ChromaDB 向量三方管理。"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, cast

from amrita_core.libchat import call_embedding
from amrita_core.types import EmbeddingChunk
from chromadb.api.models.Collection import Collection
from nonebot import logger

from ..config import DATA_PATH, build_preset
from ..vector import get_db_conn
from .types import (
    KnowledgeEntry,
    KnowledgeListItem,
    KnowledgeReadResult,
    KnowledgeSearchItem,
)

if TYPE_CHECKING:
    from collections.abc import Sequence


# Guard: 确保任何 `---` 行都能正确分割（不匹配文档内部的减号）
_SPLITTER = re.compile(r"^---\s*$", re.MULTILINE)


class KnowledgeBaseManager:
    """全局知识库。

    每条知识 = KNOWLEDGE_{kid}.md 文件 + knowledge_index.json 索引条目 + ChromaDB 向量。
    文件格式：```# 标题\\n摘要内容\\n---\\n正文内容```
    第一个 ``---`` 之前为摘要（向量化），之后为正文（文件读取）。
    """

    def __init__(
        self,
        data_dir: Path | None = None,
        collection_name: str = "amrita_global_knowledge",
        max_chars: int = 10000,
    ) -> None:
        self._data_dir = data_dir or DATA_PATH
        self._knowledge_dir = self._data_dir / "knowledge"
        self._index_path = self._data_dir / "knowledge_index.json"
        self._collection_name = collection_name
        self._max_chars = max_chars
        self._collection: Collection | None = None
        self._index: list[KnowledgeEntry] = []

    #  ── 生命周期 ──

    async def init(self) -> None:
        """创建目录，获取 ChromaDB collection。"""
        self._knowledge_dir.mkdir(parents=True, exist_ok=True)
        db = get_db_conn()
        self._collection = db.get_or_create_collection(self._collection_name)
        self._index = self._load_index()
        logger.debug(
            f"[KB] Initialized: dir={self._knowledge_dir}, "
            f"collection={self._collection_name}, entries={len(self._index)}"
        )

    async def validate_on_startup(self) -> None:
        """三方校验（文件↔JSON↔ChromaDB），以 JSON 索引为准自动修复差异。"""
        if self._collection is None:
            logger.warning("[KB] validate_on_startup called before init, skip")
            return

        index = self._load_index()
        index_map: dict[str, KnowledgeEntry] = {e["kid"]: e for e in index}

        # 扫描 knowledge/ 目录
        existing_files: set[str] = set()
        if self._knowledge_dir.exists():
            for f in self._knowledge_dir.iterdir():
                m = re.match(r"^KNOWLEDGE_(.+)\.md$", f.name)
                if m:
                    existing_files.add(m.group(1))

        vector_ids: set[str] = set(await self._get_all_vector_ids())
        changed = False

        # 1. 文件在，JSON 无 → 解析追加到 JSON + sync_to_vector
        for kid in existing_files - index_map.keys():
            await self._recover_orphan_file(kid, index, index_map)
            changed = changed or kid in {e["kid"] for e in index}

        # 2. JSON 在，文件无 → 从 JSON 删除 + remove_from_vector
        for kid in index_map.keys() - existing_files:
            index = [e for e in index if e["kid"] != kid]
            del index_map[kid]
            if kid in vector_ids:
                await self._remove_from_vector(kid)
            changed = True
            logger.info(f"[KB] Orphan index removed: {kid}")

        # 3. JSON+文件都在，向量缺失 → sync_to_vector
        for kid in (index_map.keys() & existing_files) - vector_ids:
            await self._sync_to_vector(kid, index_map[kid]["summary"])
            changed = True
            logger.debug(f"[KB] Missing vector restored: {kid}")

        # 4. 向量在，JSON 无 → remove_from_vector
        for kid in vector_ids - index_map.keys():
            await self._remove_from_vector(kid)
            changed = True
            logger.info(f"[KB] Dangling vector removed: {kid}")

        if changed:
            self._save_index(index)
            self._index = index
            logger.info(
                f"[KB] validate_on_startup repairs: {len(existing_files)} files, "
                f"{len(index)} index entries, {len(vector_ids)} vectors → synced"
            )
        else:
            logger.debug(
                f"[KB] validate_on_startup: all {len(index)} entries consistent"
            )

    #  ── 公开 API ──

    async def list_all(self) -> list[KnowledgeListItem]:
        """返回索引中全部知识条目（不含正文）。"""
        return [
            {
                "kid": e["kid"],
                "title": e["title"],
                "summary_preview": e["summary"][:80] + "..."
                if len(e["summary"]) > 80
                else e["summary"],
                "total_lines": e["total_lines"],
                "char_count": e["char_count"],
                "created_at": e["created_at"],
                "updated_at": e["updated_at"],
            }
            for e in self._index
        ]

    async def read(
        self,
        kid: str,
        start_line: int | None = None,
        end_line: int | None = None,
    ) -> KnowledgeReadResult:
        """读取指定知识条目，支持行数滑动窗口。

        返回格式：
        {
            "kid": str,
            "title": str,
            "summary": str,
            "body_lines": list[str],
            "total_lines": int,
            "range_start": int,
            "range_end": int,
        }
        """
        entry = self._find_entry(kid)
        if entry is None:
            return {"error": f"Knowledge not found: {kid}"}

        path = self._get_knowledge_path(kid)
        title, summary, body_lines = self._parse_knowledge_file(path)

        total = len(body_lines)
        if start_line is None:
            s = 0
        else:
            s = max(0, start_line)
        if end_line is None:
            e = total
        else:
            e = max(s, min(total, end_line))

        return {
            "kid": kid,
            "title": title,
            "summary": summary,
            "body_lines": body_lines[s:e],
            "total_lines": total,
            "range_start": s,
            "range_end": e,
        }

    async def create(self, title: str, summary: str, body: str) -> str:
        """创建新知识条目。返回 kid。

        Raises:
            ValueError: body 超过 max_chars
        """
        if len(body) > self._max_chars:
            raise ValueError(
                f"Body exceeds max_chars ({len(body)} > {self._max_chars})"
            )
        kid = uuid.uuid4().hex[:8]
        total_lines = body.count("\n") + 1
        entry = self._make_entry(kid, title, summary, total_lines, len(body))
        self._write_knowledge_file(self._get_knowledge_path(kid), title, summary, body)
        self._index.append(entry)
        self._save_index(self._index)
        await self._sync_to_vector(kid, summary)
        logger.info(f"[KB] Created: {kid} '{title}' ({len(body)} chars)")
        return kid

    async def update(
        self,
        kid: str,
        title: str | None = None,
        summary: str | None = None,
        body: str | None = None,
    ) -> str:
        """更新已有知识条目。至少一个可选参数非 None。

        返回 "ok" 或错误消息。
        """
        entry = self._find_entry(kid)
        if entry is None:
            return f"Knowledge not found: {kid}"

        if title is None and summary is None and body is None:
            return "At least one of title/summary/body must be provided"

        path = self._get_knowledge_path(kid)
        old_title, old_summary, old_body_lines = self._parse_knowledge_file(path)
        old_body = "\n".join(old_body_lines)

        new_title = title if title is not None else old_title
        new_summary = summary if summary is not None else old_summary
        new_body = body if body is not None else old_body

        if body is not None and len(body) > self._max_chars:
            return f"Body exceeds max_chars ({len(body)} > {self._max_chars})"

        self._write_knowledge_file(path, new_title, new_summary, new_body)
        total_lines = new_body.count("\n") + 1
        entry["title"] = new_title
        entry["summary"] = new_summary
        entry["total_lines"] = total_lines
        entry["char_count"] = len(new_body)
        entry["updated_at"] = datetime.now(timezone.utc).isoformat()
        self._save_index(self._index)

        if summary is not None:
            await self._sync_to_vector(kid, new_summary)

        logger.info(f"[KB] Updated: {kid} '{new_title}'")
        return "ok"

    async def delete(self, kid: str) -> str:
        """删除知识条目（文件 + 索引 + 向量）。返回 "ok" 或错误消息。"""
        entry = self._find_entry(kid)
        if entry is None:
            return f"Knowledge not found: {kid}"

        path = self._get_knowledge_path(kid)
        try:
            path.unlink(missing_ok=True)
        except OSError as e:
            logger.warning(f"[KB] Failed to delete file {path}: {e}")

        self._index = [e for e in self._index if e["kid"] != kid]
        self._save_index(self._index)
        await self._remove_from_vector(kid)
        logger.info(f"[KB] Deleted: {kid}")
        return "ok"

    async def search(self, query: str, top_k: int = 5) -> list[KnowledgeSearchItem]:
        """ChromaDB 语义搜索摘要，返回匹配的知识条目摘要。"""
        if self._collection is None:
            return [{"error": "KnowledgeBaseManager not initialized"}]

        try:
            vectors: Sequence[EmbeddingChunk] = await call_embedding(
                [query], build_preset()
            )
            if not vectors:
                return []
            result = await asyncio.to_thread(
                self._collection.query,
                query_embeddings=[vectors[0].embedding],
                n_results=min(top_k, len(self._index) if self._index else top_k),
                include=["metadatas", "distances"],
            )
        except Exception as e:
            logger.opt(exception=e, colors=True, raw=True).exception(
                f"[KB] Search failed: {e}"
            )
            return [{"error": str(e)}]

        raw_result: dict[str, list[list[object]]] = cast(
            dict[str, list[list[object]]], result
        )
        ids: list[str] = cast(list[str], raw_result.get("ids", [[]])[0])
        distances: list[float] = cast(list[float], raw_result.get("distances", [[]])[0])
        raw_metas: list[dict[str, object]] = cast(
            list[dict[str, object]], (raw_result.get("metadatas") or [[]])[0]
        )

        # 从索引获取完整信息
        index_map = {e["kid"]: e for e in self._index}
        items: list[KnowledgeSearchItem] = []
        for i, kid in enumerate(ids):
            entry: KnowledgeEntry | None = index_map.get(kid)
            meta: dict[str, object] = raw_metas[i] if i < len(raw_metas) else {}
            items.append(
                {
                    "kid": kid,
                    "title": entry["title"]
                    if entry is not None
                    else str(meta.get("title", "(unknown)")),
                    "summary": entry["summary"] if entry is not None else "",
                    "distance": distances[i] if i < len(distances) else -1,
                }
            )
        return items

    #  ── 内部方法 ──

    async def _recover_orphan_file(
        self,
        kid: str,
        index: list[KnowledgeEntry],
        index_map: dict[str, KnowledgeEntry],
    ) -> None:
        """尝试恢复孤立的文件（文件在但 JSON 索引中没有）。"""
        try:
            title, summary, _body_lines = self._parse_knowledge_file(
                self._get_knowledge_path(kid)
            )
            entry = self._make_entry(kid, title, summary, 0, 0)
            index.append(entry)
            index_map[kid] = entry
            await self._sync_to_vector(kid, summary)
            logger.info(f"[KB] Orphan file recovered: {kid}")
        except Exception as e:
            logger.warning(f"[KB] Failed to recover orphan file {kid}: {e}")

    def _find_entry(self, kid: str) -> KnowledgeEntry | None:
        for e in self._index:
            if e["kid"] == kid:
                return e
        return None

    def _get_knowledge_path(self, kid: str) -> Path:
        return self._knowledge_dir / f"KNOWLEDGE_{kid}.md"

    @staticmethod
    def _parse_knowledge_file(path: Path) -> tuple[str, str, list[str]]:
        """解析知识文件，返回 (title, summary, body_lines)。"""
        text = path.read_text(encoding="utf-8")
        # 第一行是标题（去掉 # 前缀）
        lines = text.split("\n")
        title = lines[0].lstrip("#").strip() if lines else ""
        rest = "\n".join(lines[1:])

        # 用第一个 --- 分割摘要和正文
        parts = _SPLITTER.split(rest, maxsplit=1)
        summary = parts[0].strip() if parts else ""
        body = parts[1].strip() if len(parts) > 1 else ""
        body_lines = body.split("\n") if body else []
        return title, summary, body_lines

    @staticmethod
    def _write_knowledge_file(path: Path, title: str, summary: str, body: str) -> None:
        """写入标准格式的知识文件。"""
        content = f"# {title}\n{summary}\n---\n{body}"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def _load_index(self) -> list[KnowledgeEntry]:
        if not self._index_path.exists():
            return []
        try:
            return json.loads(self._index_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            logger.warning(f"[KB] Corrupt index, resetting: {e}")
            return []

    def _save_index(self, index: list[KnowledgeEntry]) -> None:
        self._index_path.parent.mkdir(parents=True, exist_ok=True)
        self._index_path.write_text(
            json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    @staticmethod
    def _make_entry(
        kid: str, title: str, summary: str, total_lines: int, char_count: int
    ) -> KnowledgeEntry:
        now = datetime.now(timezone.utc).isoformat()
        return KnowledgeEntry(
            kid=kid,
            title=title,
            file_name=f"KNOWLEDGE_{kid}.md",
            summary=summary,
            total_lines=total_lines,
            char_count=char_count,
            created_at=now,
            updated_at=now,
        )

    async def _sync_to_vector(self, kid: str, summary: str) -> None:
        """将摘要向量化写入 ChromaDB（upsert）。"""
        if self._collection is None:
            return
        try:
            vectors: Sequence[EmbeddingChunk] = await call_embedding(
                [summary], build_preset()
            )
            if not vectors:
                logger.warning(f"[KB] No embedding returned for {kid}")
                return
            await asyncio.to_thread(
                self._collection.upsert,
                ids=[kid],
                embeddings=[list(vectors[0].embedding)],
                metadatas=[{"kid": kid}],
            )
        except Exception as e:
            logger.opt(exception=e, colors=True, raw=True).exception(
                f"[KB] Sync vector for {kid} failed: {e}"
            )

    async def _remove_from_vector(self, kid: str) -> None:
        """从 ChromaDB 删除向量。"""
        if self._collection is None:
            return
        try:
            await asyncio.to_thread(self._collection.delete, ids=[kid])
        except Exception as e:
            logger.opt(exception=e, colors=True, raw=True).exception(
                f"[KB] Remove vector for {kid} failed: {e}"
            )

    async def _get_all_vector_ids(self) -> list[str]:
        """获取 collection 中所有向量 ID。"""
        if self._collection is None:
            return []
        try:
            result = await asyncio.to_thread(self._collection.get, include=[])
            return list(result.get("ids", []))
        except Exception:
            return []
