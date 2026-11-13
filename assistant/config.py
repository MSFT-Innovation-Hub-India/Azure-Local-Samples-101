import json
import os
import re
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


MODEL_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9.-]{0,59}$")


@dataclass(frozen=True)
class ModelOption:
    """A selectable model. URL and key file stay on the backend; only public() is exposed."""
    id: str
    label: str
    description: str = ""
    url: str = ""
    model_id: str = ""
    key_file: str = ""

    def public(self) -> dict[str, str]:
        return {"id": self.id, "label": self.label, "description": self.description}


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
    models_file: str = "/run/models/models.json"
    static_dir: str = "/app/frontend/dist"

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(**{
            field: os.environ[field.upper()]
            for field in cls.__dataclass_fields__
            if field.upper() in os.environ
        })


def load_models(settings: Settings) -> list[ModelOption]:
    """Return the selectable models; the first entry is the default.

    Without a catalog file the single legacy model settings are used, so existing
    deployments keep working unchanged.
    """
    path = Path(settings.models_file)
    if not path.is_file():
        return [ModelOption(
            "default", settings.model_id, url=settings.model_url,
            model_id=settings.model_id, key_file=settings.model_key_file,
        )]
    try:
        entries = json.loads(path.read_text(encoding="utf-8"))["models"]
        options = [ModelOption(
            id=entry["id"], label=entry["label"], description=entry.get("description", ""),
            url=entry["url"], model_id=entry["model_id"], key_file=entry["key_file"],
        ) for entry in entries]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ValueError(f"Invalid model catalog: {path}") from exc
    ids = [option.id for option in options]
    if (
        not options or len(set(ids)) != len(ids)
        or any(not MODEL_ID_PATTERN.fullmatch(identifier) for identifier in ids)
        or any(not (option.label and option.url and option.model_id and option.key_file) for option in options)
    ):
        raise ValueError(f"Invalid model catalog: {path}")
    return options
