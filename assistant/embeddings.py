import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastembed import TextEmbedding
from huggingface_hub import snapshot_download
from pydantic import BaseModel, Field

from assistant.clients import validate_vectors
from assistant.config import EMBEDDING_MODEL, EMBEDDING_REVISION


logger = logging.getLogger(__name__)


class EmbeddingRequest(BaseModel):
    texts: list[str] = Field(min_length=1, max_length=32)


def load_model(*, offline: bool) -> TextEmbedding:
    cache = os.environ.get("MODEL_CACHE", "/models")
    snapshot = snapshot_download(
        "Qdrant/all-MiniLM-L6-v2-onnx", revision=EMBEDDING_REVISION,
        cache_dir=cache, local_files_only=offline,
        allow_patterns=[
            "model.onnx", "config.json", "tokenizer.json",
            "tokenizer_config.json", "special_tokens_map.json",
        ],
    )
    return TextEmbedding(
        model_name=EMBEDDING_MODEL,
        cache_dir=cache,
        specific_model_path=snapshot,
        local_files_only=offline,
        threads=2,
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.model = load_model(offline=True)
    yield


app = FastAPI(title="Local document embeddings", lifespan=lifespan)


@app.get("/healthz")
def health() -> dict[str, str]:
    return {"status": "ok", "model": EMBEDDING_MODEL, "revision": EMBEDDING_REVISION}


@app.post("/embed")
def embed(request: EmbeddingRequest) -> dict:
    if any(not text.strip() or len(text) > 4000 for text in request.texts):
        raise HTTPException(422, "Each embedding input must contain 1-4000 characters.")
    try:
        vectors = [vector.tolist() for vector in app.state.model.embed(request.texts)]
        validate_vectors(vectors, len(request.texts))
    except (RuntimeError, ValueError) as exc:
        logger.exception("Local embedding execution failed")
        raise HTTPException(503, "Local embedding execution failed.") from exc
    return {"model": EMBEDDING_MODEL, "revision": EMBEDDING_REVISION, "vectors": vectors}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    load_model(offline=False)
