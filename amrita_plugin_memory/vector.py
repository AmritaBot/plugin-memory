from __future__ import annotations

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
    DefaultEmbeddingFunction,
    Embeddable,
    EmbeddingFunction,
    Loadable,
    Schema,
)
from chromadb.config import DEFAULT_DATABASE
from nonebot import get_driver, logger
from pydantic import BaseModel, Field
from pytz import utc

from .config import VECTOR_DB_PATH, build_preset, env_config

T = TypeVar("T")
_collection_lock_pool: defaultdict[str, WeakValueLRUCache[str, aiologic.Lock]] = (
    defaultdict(lambda: WeakValueLRUCache(capacity=1024, loose_mode=True))
)


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
        embedding_function: chromadb.EmbeddingFunction[Embeddable]
        | None = DefaultEmbeddingFunction(),  # type: ignore
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
        embedding_function: EmbeddingFunction[Embeddable]
        | None = DefaultEmbeddingFunction(),  # pyright: ignore[reportArgumentType]
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
        embedding_function: EmbeddingFunction[Embeddable]
        | None = DefaultEmbeddingFunction(),  # type: ignore
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
        embedding_function: EmbeddingFunction[Embeddable]
        | None = DefaultEmbeddingFunction(),  # type: ignore
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
    _collection_name: str = "amrita_user_memory"
    _collection: Collection

    def __init__(
        self,
        client: ClientAPI,
        collection_name: str = "amrita_user_memory",
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

    @require_init
    async def add_note(self, user_id: str, note_text: str, metadata: MemoryMetadata):
        async with get_lock(self._collection_name, user_id):
            vector: Sequence[EmbeddingChunk] = await call_embedding(
                [note_text], build_preset()
            )
            if len(vector) == 0:
                raise RuntimeError("No embedding returned")
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
                embeddings=[vector[0].embedding],
                documents=[note_text],
            )

    @require_init
    async def update_note(self, user_id: str, note_text: str, metadata: MemoryMetadata):
        """更新指定记忆，使用 ChromaDB 原生 update 保证原子性"""
        async with get_lock(self._collection_name, user_id):
            vector: Sequence[EmbeddingChunk] = await call_embedding(
                [note_text], build_preset()
            )
            if len(vector) == 0:
                raise RuntimeError("No embedding returned")
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
                embeddings=[vector[0].embedding],
                documents=[note_text],
            )

    @require_init
    async def query_notes(
        self,
        user_id: str,
        query_text: str,
        importance: Literal["low", "medium", "high"] | None = None,
        top_k: int = 5,
        include: chromadb.Include = ["metadatas", "documents"],
    ) -> chromadb.QueryResult:
        async with get_lock(self._collection_name, user_id):
            queue_embedding = await call_embedding([query_text], build_preset())
            assert len(queue_embedding) == 1, "Invalid embedding vector length"
            return await any_to_thread(
                self._collection.query,
                queue_embedding[0].embedding,
                [
                    query_text,
                ],
                include=include,
                n_results=top_k,
                where={
                    "user_id": user_id,
                    **({"importance": importance} if importance else {}),
                },
            )

    @require_init
    async def get_all_notes(
        self, user_id: str, include: chromadb.Include = ["metadatas", "documents"]
    ) -> chromadb.GetResult:
        async with get_lock(self._collection_name, user_id):
            return await any_to_thread(
                self._collection.get,
                include=include,
                where={"user_id": user_id},
            )

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
