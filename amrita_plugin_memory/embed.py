from collections.abc import Iterable, Sequence
from typing import Any, Literal

import aiohttp
from amrita_core.protocol import ModelAdapter
from amrita_core.types import EmbeddingChunk
from openai import AsyncOpenAI


class OllamaEmbeddingAdapter(ModelAdapter):
    async def call_embed(
        self, texts: Iterable[str], **kwargs
    ) -> Sequence[EmbeddingChunk]:
        endp = "/api/embed"
        preset = self.preset
        url = (
            preset.base_url
            if preset.base_url.endswith(endp)
            else preset.base_url.rstrip("/") + endp
        )
        if isinstance(texts, str):
            texts = [texts]
        payload = {"model": preset.model, "input": texts, **kwargs}
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload) as response:
                response.raise_for_status()
                data: dict[str, Any] = await response.json()
                if data.get("error"):
                    raise RuntimeError(f"Ollama error: {data['error']}")

                return tuple(
                    EmbeddingChunk(
                        embedding=chunk,
                        index=index,
                    )
                    for index, chunk in enumerate(data["embeddings"])
                )

    @staticmethod
    def get_adapter_protocol() -> Literal["ollama-embed"]:
        return "ollama-embed"

    @staticmethod
    def get_type() -> Literal["embed"]:
        return "embed"


class OpenAIEmbeddingAdapter(ModelAdapter):
    async def call_embed(
        self, texts: Iterable[str], **kwargs
    ) -> Sequence[EmbeddingChunk]:
        ps = self.preset
        client = AsyncOpenAI(
            base_url=ps.base_url, api_key=ps.api_key, max_retries=5, timeout=60
        )
        if isinstance(texts, str):
            texts = [texts]
        texts = list(texts)
        embedd = await client.embeddings.create(
            input=texts, model=ps.model, **kwargs, timeout=60
        )
        return [EmbeddingChunk.model_validate(data) for data in embedd.data]

    @staticmethod
    def get_adapter_protocol() -> Literal["openai-embed"]:
        return "openai-embed"

    @staticmethod
    def get_type() -> Literal["embed"]:
        return "embed"
