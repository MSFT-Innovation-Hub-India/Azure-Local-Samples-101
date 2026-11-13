import math
import ssl
from dataclasses import dataclass, replace
from typing import Protocol

import httpx
from qdrant_client import QdrantClient

from assistant.config import (
    EMBEDDING_DIMENSIONS, EMBEDDING_MODEL, EMBEDDING_REVISION, ModelOption, Settings, read_secret,
)


class DependencyError(RuntimeError):
    """A local dependency failed or returned an invalid response."""


class Embedder(Protocol):
    def embed(self, texts: list[str]) -> list[list[float]]: ...


@dataclass(frozen=True)
class TokenUsage:
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


@dataclass(frozen=True)
class CompletionResult:
    content: str
    usage: TokenUsage | None = None


def validate_vectors(vectors: object, count: int) -> list[list[float]]:
    if not isinstance(vectors, list) or len(vectors) != count:
        raise DependencyError("Embedding service returned the wrong number of vectors.")
    for vector in vectors:
        if not isinstance(vector, list) or len(vector) != EMBEDDING_DIMENSIONS:
            raise DependencyError("Embedding dimensions do not match the index.")
        if not all(
            isinstance(value, (int, float))
            and not isinstance(value, bool) and math.isfinite(value)
            for value in vector
        ):
            raise DependencyError("Embedding service returned invalid vector values.")
        if not any(vector):
            raise DependencyError("Embedding service returned a zero vector.")
    return vectors


class EmbeddingClient:
    def __init__(self, client: httpx.Client):
        self.client = client

    def embed(self, texts: list[str]) -> list[list[float]]:
        try:
            response = self.client.post("/embed", json={"texts": texts})
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise DependencyError("Local embedding request failed.") from exc
        if (
            not isinstance(payload, dict)
            or payload.get("model") != EMBEDDING_MODEL
            or payload.get("revision") != EMBEDDING_REVISION
        ):
            raise DependencyError("Embedding model does not match the index configuration.")
        return validate_vectors(payload.get("vectors"), len(texts))


class ModelClient:
    def __init__(self, client: httpx.Client, settings: Settings, api_key: str):
        self.client = client
        self.settings = settings
        self.api_key = api_key

    def complete(self, messages: list[dict[str, str]]) -> CompletionResult:
        try:
            response = self.client.post(
                self.settings.model_url,
                headers={"api-key": self.api_key},
                json={
                    "model": self.settings.model_id,
                    "messages": messages,
                    "max_tokens": 256,
                    "temperature": 0,
                },
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise DependencyError("Local model request failed; check model health and TLS.") from exc
        try:
            content = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise DependencyError("Local model returned no assistant message.") from exc
        if not isinstance(content, str) or not content.strip():
            raise DependencyError("Local model returned an empty assistant message.")
        usage = payload.get("usage")
        counts = None
        if usage is not None:
            fields = ("prompt_tokens", "completion_tokens", "total_tokens")
            if not isinstance(usage, dict) or any(
                not isinstance(usage.get(name), int)
                or isinstance(usage[name], bool) or usage[name] < 0
                for name in fields
            ):
                raise DependencyError("Local model returned invalid token usage.")
            counts = TokenUsage(
                usage["prompt_tokens"], usage["completion_tokens"], usage["total_tokens"],
            )
            if counts.total_tokens != counts.prompt_tokens + counts.completion_tokens:
                raise DependencyError("Local model returned inconsistent token usage.")
        return CompletionResult(content.strip(), counts)


class ModelRouter:
    """Sends a completion to the selected model's client; the first client is the default."""

    def __init__(self, clients: dict[str, ModelClient]):
        if not clients:
            raise ValueError("At least one model is required.")
        self.clients = clients
        self.default = next(iter(clients))

    def complete(self, messages: list[dict[str, str]], model: str | None = None) -> CompletionResult:
        client = self.clients.get(model or self.default)
        if client is None:
            raise DependencyError("The selected model is not configured.")
        return client.complete(messages)


def build_model_router(
    http: httpx.Client, settings: Settings, options: list[ModelOption],
) -> ModelRouter:
    return ModelRouter({
        option.id: ModelClient(
            http, replace(settings, model_url=option.url, model_id=option.model_id),
            read_secret(option.key_file),
        ) for option in options
    })


def qdrant_client(settings: Settings) -> QdrantClient:
    # This HTTP endpoint is a private ClusterIP, never a public database endpoint.
    return QdrantClient(
        url=settings.qdrant_url,
        api_key=read_secret(settings.qdrant_key_file),
        timeout=30,
        check_compatibility=True,
    )


def model_http_client(settings: Settings) -> httpx.Client:
    context = ssl.create_default_context(cafile=settings.model_ca_file)
    return httpx.Client(verify=context, timeout=180, trust_env=False)
