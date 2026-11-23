from __future__ import annotations

import contextlib
import uuid
from asyncio import to_thread
from collections import defaultdict
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime
from functools import wraps
from inspect import iscoroutinefunction
from typing import Any, Literal, TypeVar, overload
from uuid import UUID

import aiologic
import chromadb
from amrita_core.libchat import call_embedding
from amrita_core.types import EmbeddingChunk
from amrita_sense.weakcache import WeakValueLRUCache
from chromadb.api import ClientAPI
from chromadb.api.collection_configuration import (
    CreateCollectionConfiguration,
)
from chromadb.api.models.Collection import Collection
from chromadb.api.types import (
    CollectionMetadata,
    DataLoader,
    Embeddable,
    EmbeddingFunction,
    Loadable,
    Schema,
)
from chromadb.config import DEFAULT_DATABASE
from nonebot import get_driver, logger
from pydantic import BaseModel, Field
from pytz import utc

from .config import VECTOR_DB_PATH, DataManager, DecayConfig, build_preset, env_config
from .decay import (
    plan_action,
    rerank_by_scores,
    similarity_from_distance,
)
from .retrieval import bm25_scores, normalize_scores, weighted_rrf

T = TypeVar("T")

MEMORY_COLLECTION_NAME = "amrita_user_memory"
"""L2 向量记忆集合名"""

_collection_lock_pool: defaultdict[str, WeakValueLRUCache[str, aiologic.Lock]] = (
    defaultdict(lambda: WeakValueLRUCache(capacity=1024, loose_mode=True))
)


# 仅关键词部署时占位向量的宽度与取值
_PLACEHOLDER_DIM = 8
_PLACEHOLDER_VALUE = 1e-6

# None=还没试过，True=通，False=已判定不可用
_embed_state: bool | None = None


def embedding_state() -> bool | None:
    """嵌入服务的已知状态；显式关掉配置时恒为 False。

    这里不主动探测：状态只由真实请求的成败给出。
    """
    if env_config.embedding_enabled is False:
        return False
    return _embed_state


def embedding_available() -> bool:
    """是否还值得发嵌入请求。

    未知状态按可用处理 —— 第一次真实调用会给出答案，失败后由
    :func:`mark_embedding_failed` 记住，不再反复撞一个连不上的服务。
    """
    return embedding_state() is not False


def mark_embedding_ok() -> None:
    global _embed_state
    _embed_state = True


def mark_embedding_failed(exc: BaseException) -> None:
    """记录不可用，只在状态翻转时告警，避免每次调用刷一遍日志。"""
    global _embed_state
    if _embed_state is not False:
        logger.warning(f"[Memory] 嵌入模型不可用，L2 降级为仅关键词检索：{exc}")
    _embed_state = False


def reset_embedding_state() -> None:
    """忘掉结论，配置变更或模型恢复后重新尝试。"""
    global _embed_state
    _embed_state = None


def resolve_query_mode(mode: str | None) -> Literal["keyword", "vector", "hybrid"]:
    """把模式名解析成实际可执行的一种。

    已判定不可用时 auto/hybrid 退到 keyword，显式 vector 抛错 ——
    静默换成关键词等于换了检索语义。
    """
    normalized = (mode or "auto").strip().lower()
    if normalized == "keyword":
        return "keyword"
    if normalized not in {"auto", "hybrid", "vector"}:
        raise ValueError(f"unknown retrieval mode: {mode!r}")
    if embedding_state() is False:
        if normalized == "vector":
            raise RuntimeError(
                "嵌入模型不可用，vector 模式无法执行；请改用 auto 或 keyword"
            )
        return "keyword"
    return "vector" if normalized == "vector" else "hybrid"


def _first_inner(value: Any) -> list[Any]:
    """取 QueryResult 里 list-of-list 的内层；兼容 ndarray，缺失返回空表。"""
    if value is None:
        return []
    if hasattr(value, "tolist"):
        value = value.tolist()
    if isinstance(value, list) and value and isinstance(value[0], list):
        return list(value[0])
    return []


def get_lock(collection: str, uid: str) -> aiologic.Lock:
    if (lock := _collection_lock_pool[collection].get(uid)) is None:
        lock = aiologic.Lock()
        _collection_lock_pool[collection][uid] = lock
    return lock


@overload
def any_to_thread(
    func: Callable[..., Awaitable[T]], /, *args, **kwargs
) -> Awaitable[T]: ...
@overload
def any_to_thread(func: Callable[..., T], /, *args, **kwargs) -> Awaitable[T]: ...


def any_to_thread(func: Callable[..., Awaitable[T] | T], /, *args, **kwargs) -> Any:
    if not callable(func):
        raise TypeError("func must be callable")
    if iscoroutinefunction(func):
        return func(*args, **kwargs)
    return to_thread(func, *args, **kwargs)


def get_db_conn() -> ClientAPI:
    match env_config.vector_db_type:
        case "local":
            db = chromadb.PersistentClient(path=VECTOR_DB_PATH)
        case "remote":
            db = chromadb.HttpClient(
                host=env_config.vector_db_server,
                port=env_config.vector_db_port,
                tenant=env_config.vector_db_tenant,
                database=env_config.vector_db_database,
                headers=env_config.vector_db_remote_headers,
                ssl=env_config.vector_db_server_ssl,
            )
        case _:
            raise ValueError("无效的向量数据库类型")
    return db


@get_driver().on_startup
async def _setup():
    logger.info("正在尝试向量数据库连接...")
    logger.info(
        f"当前向量数据库类型: {env_config.vector_db_type}",
    )
    db = get_db_conn()
    logger.info("创建了向量数据库客户端！")
    logger.info(f"向量数据库版本：{db.get_version()}")
    logger.info(f"所有可用集合：{db.list_collections()}")
    logger.info("完成。")


class WrappedClientAPI:
    __client: ClientAPI
    tenant: str
    database: str

    def __init__(self, client: ClientAPI):
        self.__client = client

    async def list_collections(
        self,
        limit: int | None = None,
        offset: int | None = None,
    ) -> Sequence[Collection]:
        """List all collections.
        Args:
            limit: The maximum number of entries to return. Defaults to None.
            offset: The number of entries to skip before returning. Defaults to None.

        Returns:
            Sequence[Collection]: A list of collections.

        Examples:
            ```python
            await client.list_collections()
            # [collection(name="my_collection", metadata={})]
            ```
        """
        return await to_thread(self.__client.list_collections, limit, offset)

    async def create_collection(
        self,
        name: str,
        schema: Schema | None = None,
        configuration: CreateCollectionConfiguration | None = None,
        metadata: CollectionMetadata | None = None,
        embedding_function: EmbeddingFunction[Embeddable] | None = None,
        data_loader: DataLoader[Loadable] | None = None,
        get_or_create: bool = False,
    ) -> Collection:
        """Create a new collection with the given name and metadata.
        Args:
            name: The name of the collection to create.
            metadata: Optional metadata to associate with the collection.
            embedding_function: Optional function to use to embed documents.
                                Uses the default embedding function if not provided.
            get_or_create: If True, return the existing collection if it exists.
            data_loader: Optional function to use to load records (documents, images, etc.)

        Returns:
            Collection: The newly created collection.

        Raises:
            ValueError: If the collection already exists and get_or_create is False.
            ValueError: If the collection name is invalid.

        Examples:
            ```python
            await client.create_collection("my_collection")
            # collection(name="my_collection", metadata={})

            await client.create_collection("my_collection", metadata={"foo": "bar"})
            # collection(name="my_collection", metadata={"foo": "bar"})
            ```
        """
        return await to_thread(
            self.__client.create_collection,
            name,
            schema,
            configuration,
            metadata,
            embedding_function,
            data_loader,
            get_or_create,
        )

    async def get_collection(
        self,
        name: str,
        embedding_function: EmbeddingFunction[Embeddable] | None = None,
        data_loader: DataLoader[Loadable] | None = None,
    ) -> Collection:
        """Get a collection with the given name.
        Args:
            name: The name of the collection to get
            embedding_function: Optional function to use to embed documents.
                                Uses the default embedding function if not provided.
            data_loader: Optional function to use to load records (documents, images, etc.)

        Returns:
            Collection: The collection

        Raises:
            ValueError: If the collection does not exist

        Examples:
            ```python
            await client.get_collection("my_collection")
            # collection(name="my_collection", metadata={})
            ```
        """
        return await to_thread(
            self.__client.get_collection,
            name,
            embedding_function,
            data_loader,
        )

    async def get_collection_by_id(
        self,
        id: UUID,
        embedding_function: EmbeddingFunction[Embeddable] | None = None,
        data_loader: DataLoader[Loadable] | None = None,
    ) -> Collection:
        """Get a collection by its ID.

        Args:
            id: The UUID of the collection to get.
            embedding_function: Optional function to use to embed documents.
                                Uses the default embedding function if not provided.
            data_loader: Optional function to use to load records (documents, images, etc.)

        Returns:
            Collection: The collection

        Raises:
            NotFoundError: If no collection with the given ID exists.

        Examples:
            ```python
            await client.get_collection_by_id(uuid.UUID("..."))
            # collection(name="my_collection", metadata={})
            ```
        """
        return await to_thread(
            self.__client.get_collection_by_id,
            id,
            embedding_function,
            data_loader,
        )

    async def get_or_create_collection(
        self,
        name: str,
        schema: Schema | None = None,
        configuration: CreateCollectionConfiguration | None = None,
        metadata: CollectionMetadata | None = None,
        embedding_function: EmbeddingFunction[Embeddable] | None = None,
        data_loader: DataLoader[Loadable] | None = None,
    ) -> Collection:
        """Get or create a collection with the given name and metadata.
        Args:
            name: The name of the collection to get or create
            metadata: Optional metadata to associate with the collection. If
            the collection already exists, the metadata provided is ignored.
            If the collection does not exist, the new collection will be created
            with the provided metadata.
            embedding_function: Optional function to use to embed documents
            data_loader: Optional function to use to load records (documents, images, etc.)

        Returns:
            The collection

        Examples:
            ```python
            await client.get_or_create_collection("my_collection")
            # collection(name="my_collection", metadata={})
            ```
        """
        return await to_thread(
            self.__client.get_or_create_collection,
            name,
            schema,
            configuration,
            metadata,
            embedding_function,
            data_loader,
        )

    async def set_tenant(self, tenant: str, database: str = DEFAULT_DATABASE) -> None:
        """Set the tenant and database for the client. Raises an error if the tenant or
        database does not exist.

        Args:
            tenant: The tenant to set.
            database: The database to set.

        """
        return await to_thread(self.__client.set_tenant, tenant, database)

    async def set_database(self, database: str) -> None:
        """Set the database for the client. Raises an error if the database does not exist.

        Args:
            database: The database to set.

        """
        return await to_thread(self.__client.set_database, database)

    @staticmethod
    def clear_system_cache() -> None:
        TypeError("Not implemented")


class MemoryMetadata(BaseModel):
    memory_id: str = Field(
        description="记忆ID", default_factory=lambda: uuid.uuid4().hex
    )
    tags: str = Field(description="标签")
    importance: Literal["low", "medium", "high"] = Field(description="重要程度")
    scope: Literal["group", "user"] = Field(
        default="user", description="记忆范围：群共享(group)或个人专属(user)"
    )
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(utc), description="创建时间"
    )


class AsyncUserMemory:
    api: WrappedClientAPI
    _collection_name: str = MEMORY_COLLECTION_NAME
    _collection: Collection
    _placeholder_dim: int | None = None

    def __init__(
        self,
        client: ClientAPI,
        collection_name: str = MEMORY_COLLECTION_NAME,
    ) -> None:
        self.api = WrappedClientAPI(client)
        self._collection_name = collection_name

    async def init(self):
        if not hasattr(self, "_collection"):
            self._collection = await self.api.get_or_create_collection(
                self._collection_name,
            )

    @staticmethod
    def require_init(
        func,
    ):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            self: AsyncUserMemory = args[0]
            await self.init()
            return await func(*args, **kwargs)

        return wrapper

    async def _embed(self, texts: Sequence[str]) -> list[list[float]] | None:
        """嵌入文本；请求失败就记不可用并返回 None，由调用方决定怎么降级。"""
        if not embedding_available():
            return None
        try:
            vectors: Sequence[EmbeddingChunk] = await call_embedding(
                list(texts), build_preset()
            )
            if len(vectors) != len(texts):
                raise RuntimeError(
                    f"Embedding count mismatch: got {len(vectors)} for {len(texts)}"
                )
        except Exception as exc:
            mark_embedding_failed(exc)
            return None
        mark_embedding_ok()
        return [list(v.embedding) for v in vectors]

    async def _placeholder_vector(self) -> list[float]:
        """仅关键词部署用的占位向量。

        Chroma 每行都要有向量，这里给方向恒定的极小值：不参与排序，只满足
        写入约束。宽度沿用库里已有向量，避免同一集合混入不同维度被拒。
        """
        if self._placeholder_dim is None:
            dim = 0
            with contextlib.suppress(Exception):
                probe = await any_to_thread(
                    self._collection.get, include=["embeddings"], limit=1
                )
                embeddings = probe.get("embeddings")
                if embeddings is not None and len(embeddings):
                    dim = len(embeddings[0])
            self._placeholder_dim = dim or _PLACEHOLDER_DIM
        return [_PLACEHOLDER_VALUE] * self._placeholder_dim

    async def _keyword_candidates(
        self, where: dict[str, Any], limit: int
    ) -> tuple[list[str], list[str], list[dict]]:
        """拉取分区内的候选，供本地 BM25 打分。"""
        raw = await any_to_thread(
            self._collection.get,
            include=["documents", "metadatas"],
            where=where,
            limit=limit,
        )
        ids = list(raw.get("ids") or [])
        documents = list(raw.get("documents") or [])
        metadatas = [
            meta if isinstance(meta, dict) else {}
            for meta in (raw.get("metadatas") or [])
        ]
        return ids, documents, metadatas

    @require_init
    async def add_note(self, user_id: str, note_text: str, metadata: MemoryMetadata):
        async with get_lock(self._collection_name, user_id):
            vectors = await self._embed([note_text])
            embeddings = vectors or [await self._placeholder_vector()]
            await any_to_thread(
                self._collection.add,
                ids=[metadata.memory_id],
                metadatas=[
                    {
                        "scope": metadata.scope,
                        "user_id": user_id,
                        "tags": metadata.tags,
                        "importance": metadata.importance,
                        "created_at": metadata.created_at.isoformat(),
                    }
                ],
                embeddings=embeddings,
                documents=[note_text],
            )

    @require_init
    async def update_note(self, user_id: str, note_text: str, metadata: MemoryMetadata):
        """更新指定记忆，使用 ChromaDB 原生 update 保证原子性"""
        async with get_lock(self._collection_name, user_id):
            vectors = await self._embed([note_text])
            embeddings = vectors or [await self._placeholder_vector()]
            await any_to_thread(
                self._collection.update,
                ids=[metadata.memory_id],
                metadatas=[
                    {
                        "scope": metadata.scope,
                        "user_id": user_id,
                        "tags": metadata.tags,
                        "importance": metadata.importance,
                        "created_at": metadata.created_at.isoformat(),
                    }
                ],
                embeddings=embeddings,
                documents=[note_text],
            )

    @require_init
    async def query_notes(
        self,
        user_id: str,
        query_text: str,
        importance: Literal["low", "medium", "high"] | None = None,
        top_k: int = 5,
        include: chromadb.Include = ["metadatas", "documents", "distances"],
        decay: DecayConfig | None = None,
        mode: str | None = None,
        vector_weight: float | None = None,
    ) -> dict[str, Any]:
        """按关键词、向量或两者融合检索记忆。

        mode 为空取配置默认值；vector_weight 只在 hybrid 生效，默认 0.6。
        没有可用嵌入模型时 auto/hybrid 退化为 keyword，显式 vector 报错。
        返回结构沿用 QueryResult，另附 retrieval 说明实际模式。
        """
        cfg = await DataManager().safe_get_config()
        policy = decay or cfg.decay
        requested = (mode or cfg.retrieval.default_mode).strip().lower()
        resolved = resolve_query_mode(requested)
        weight = cfg.retrieval.vector_weight if vector_weight is None else vector_weight
        weight = min(max(float(weight), 0.0), 1.0)
        where: dict[str, Any] = {
            "user_id": user_id,
            **({"importance": importance} if importance else {}),
        }
        fetch_k = max(top_k, 1)
        if policy.enabled and policy.overfetch > 1:
            # 候选放大留给衰减重排，和纯向量路径保持一致
            fetch_k = max(top_k * policy.overfetch, top_k)

        async with get_lock(self._collection_name, user_id):
            records: dict[str, dict[str, Any]] = {}
            vector_rank: list[str] = []
            if resolved in {"vector", "hybrid"}:
                query_vector = await self._embed([query_text])
                if query_vector is None:
                    if requested == "vector":
                        raise RuntimeError(
                            "嵌入模型不可用，vector 模式无法执行；"
                            "请改用 auto 或 keyword"
                        )
                    # 混合少一路还能出结果，降级成关键词并如实标注
                    resolved = "keyword"
                else:
                    raw = await any_to_thread(
                        self._collection.query,
                        query_vector[0],
                        [query_text],
                        include=["documents", "metadatas", "distances"],
                        n_results=fetch_k,
                        where=where,
                    )
                    hit_ids = _first_inner(raw.get("ids"))
                    hit_docs = _first_inner(raw.get("documents"))
                    hit_metas = _first_inner(raw.get("metadatas"))
                    hit_dists = _first_inner(raw.get("distances"))
                    for i, rid in enumerate(hit_ids):
                        records[rid] = {
                            "document": hit_docs[i] if i < len(hit_docs) else "",
                            "metadata": hit_metas[i] if i < len(hit_metas) else {},
                            "distance": hit_dists[i] if i < len(hit_dists) else 0.0,
                        }
                    vector_rank = list(hit_ids)

            keyword_rank: list[str] = []
            keyword_relevance: dict[str, float] = {}
            if resolved in {"keyword", "hybrid"}:
                k_ids, k_docs, k_metas = await self._keyword_candidates(
                    where, cfg.retrieval.keyword_candidate_limit
                )
                if k_ids:
                    raw_scores = bm25_scores(query_text, k_docs)
                    norm = normalize_scores(raw_scores)
                    # 零分说明一个词项都没命中，进结果只会是噪声
                    order = sorted(
                        (i for i in range(len(k_ids)) if raw_scores[i] > 0),
                        key=lambda i: norm[i],
                        reverse=True,
                    )
                    keyword_rank = [k_ids[i] for i in order]
                    keyword_relevance = {k_ids[i]: norm[i] for i in order}
                    for i in order:
                        if k_ids[i] in records:
                            continue
                        records[k_ids[i]] = {
                            "document": k_docs[i] if i < len(k_docs) else "",
                            "metadata": k_metas[i] if i < len(k_metas) else {},
                            "distance": 1.0 - norm[i],
                        }

            relevance: dict[str, float]
            if resolved == "hybrid":
                fused = weighted_rrf(vector_rank, keyword_rank, weight)
                final_ids = [rid for rid, _ in fused]
                relevance = dict(fused)
            elif resolved == "keyword":
                final_ids = keyword_rank
                relevance = keyword_relevance
            else:
                final_ids = vector_rank
                relevance = {
                    rid: similarity_from_distance(records[rid]["distance"])
                    for rid in final_ids
                }
            final_ids = final_ids[:fetch_k]

            payload: dict[str, Any] = {
                "ids": [final_ids],
                "documents": [[records[rid]["document"] for rid in final_ids]],
                "metadatas": [[records[rid]["metadata"] for rid in final_ids]],
                "distances": [[records[rid]["distance"] for rid in final_ids]],
            }
            out: dict[str, Any]
            if policy.enabled:
                out = rerank_by_scores(
                    payload,
                    [relevance.get(rid, 0.0) for rid in final_ids],
                    top_k,
                    policy.half_life_days,
                    policy.importance_boost,
                    policy.score_floor,
                )
            else:
                out = {key: [inner[:top_k]] for key, (inner,) in payload.items()}
            # 调用方没要的字段不外泄
            for key in ("documents", "metadatas", "distances"):
                if key not in include:
                    out.pop(key, None)
            out["retrieval"] = {
                "mode": resolved,
                "vector_weight": round(weight, 3),
            }
            if requested in {"auto", "hybrid"} and resolved == "keyword":
                out["retrieval"]["degraded"] = "embedding_unavailable"
            return out

    @require_init
    async def get_all_notes(
        self,
        user_id: str,
        include: chromadb.Include = ["metadatas", "documents"],
        ids: list[str] | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> chromadb.GetResult:
        async with get_lock(self._collection_name, user_id):
            if ids is not None:
                return await any_to_thread(
                    self._collection.get, include=include, ids=ids
                )
            kwargs: dict = {"include": include, "where": {"user_id": user_id}}
            if limit is not None:
                kwargs["limit"] = limit
            if offset is not None:
                kwargs["offset"] = offset
            return await any_to_thread(self._collection.get, **kwargs)

    @require_init
    async def delete_note(self, user_id: str, doc_id: str):
        async with get_lock(self._collection_name, user_id):
            await any_to_thread(
                self._collection.delete,
                ids=[doc_id],
                where={"user_id": user_id},
            )

    @require_init
    async def delete_user_all_notes(self, user_id: str):
        async with get_lock(self._collection_name, user_id):
            await any_to_thread(
                self._collection.delete,
                where={"user_id": user_id},
            )

    @require_init
    async def count_user_notes(self, user_id: str) -> int:
        async with get_lock(self._collection_name, user_id):
            result = await any_to_thread(
                self._collection.get, where={"user_id": user_id}, include=[]
            )
            return len(result["ids"])

    @require_init
    async def sweep_decay(self, policy: DecayConfig) -> dict[str, int]:
        """扫描记忆库：超龄先降级，降到 low 仍超龄则清理。

        分两趟：先只读分页扫描，把候选按分区归组；再逐分区持锁复核并应用。
        只读扫描避免边扫边删导致 offset 漂移，锁内复核避免覆盖并发编辑。
        """
        now = datetime.now(utc)
        scan_limit = policy.sweep_batch_limit
        page_size = max(1, min(1000, scan_limit))
        offset = 0
        scanned = 0
        candidates: defaultdict[str, list[str]] = defaultdict(list)

        while scanned < scan_limit:
            page = await any_to_thread(
                self._collection.get,
                include=["metadatas"],
                limit=min(page_size, scan_limit - scanned),
                offset=offset,
            )
            ids = page.get("ids") or []
            if not ids:
                break
            metas = page.get("metadatas") or []
            for i, doc_id in enumerate(ids):
                meta = (
                    dict(metas[i])
                    if i < len(metas) and isinstance(metas[i], dict)
                    else {}
                )
                action, _ = plan_action(
                    meta, now, policy.short_term_days, policy.long_term_days
                )
                if action != "keep":
                    candidates[str(meta.get("user_id", ""))].append(doc_id)
            scanned += len(ids)
            offset += len(ids)

        demoted = 0
        purged = 0
        for user_id, doc_ids in candidates.items():
            async with get_lock(self._collection_name, user_id):
                # 扫描到应用之间可能已被编辑或删除，锁内重新读一遍再决策
                fresh = await any_to_thread(
                    self._collection.get, ids=doc_ids, include=["metadatas"]
                )
                fresh_ids = fresh.get("ids") or []
                fresh_metas = fresh.get("metadatas") or []
                demote_ids: list[str] = []
                demote_metas: list[dict] = []
                purge_ids: list[str] = []
                for i, doc_id in enumerate(fresh_ids):
                    meta = (
                        dict(fresh_metas[i])
                        if i < len(fresh_metas) and isinstance(fresh_metas[i], dict)
                        else {}
                    )
                    action, new_importance = plan_action(
                        meta, now, policy.short_term_days, policy.long_term_days
                    )
                    if action == "demote" and new_importance:
                        meta["importance"] = new_importance
                        demote_ids.append(doc_id)
                        demote_metas.append(meta)
                    elif action == "purge":
                        purge_ids.append(doc_id)
                if demote_ids:
                    await any_to_thread(
                        self._collection.update,
                        ids=demote_ids,
                        metadatas=demote_metas,
                    )
                if purge_ids:
                    await any_to_thread(self._collection.delete, ids=purge_ids)
                demoted += len(demote_ids)
                purged += len(purge_ids)

        return {
            "scanned": scanned,
            "demoted": demoted,
            "purged": purged,
            "kept": max(scanned - demoted - purged, 0),
        }
