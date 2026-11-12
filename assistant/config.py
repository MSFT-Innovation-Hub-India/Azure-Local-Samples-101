import os
from dataclasses import dataclass
from pathlib import Path


EMBEDDING_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
EMBEDDING_REVISION = "d13954661f83248295ba75c1ed411eef3b7b936e"
EMBEDDING_DIMENSIONS = 384
INDEX_ALIAS = "assistant-pdf-active"
CHUNK_SIZE = 900
CHUNK_OVERLAP = 120


def read_secret(path: str) -> str:
    value = Path(path).read_text(encoding="utf-8").strip()
    if not value:
        raise ValueError(f"Credential file is empty: {path}")
    return value


@dataclass(frozen=True)
class Settings:
    qdrant_url: str = "http://qdrant:6333"
    qdrant_key_file: str = "/run/credentials/qdrant-key"
    embedding_url: str = "http://embeddings:8001"
    model_url: str = (
        "https://qwen2-5-0-5b-cpu.foundry-local-operator."
        "svc.cluster.local:5000/v1/chat/completions"
    )
    model_id: str = "qwen2.5-0.5b-instruct-generic-cpu"
    model_key_file: str = "/run/credentials/model-key"
    model_ca_file: str = "/run/trust/ca.crt"
    static_dir: str = "/app/frontend/dist"

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(**{
            field: os.environ[field.upper()]
            for field in cls.__dataclass_fields__
            if field.upper() in os.environ
        })
